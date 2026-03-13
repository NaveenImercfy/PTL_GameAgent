"""
Combined server: ADK Web UI + /questions API + /conversation/start + /chat (Unreal Engine) on one port (default 8000).
Run from project root: python run_combined.py  OR  uvicorn run_combined:app --reload --port 8000
"""

import json
import os
import random
import re
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from session_state import create_session_store, create_question_store
from quiz_engine import (
    # Constants
    KEY_PHRASE_MW, ANIMAL_PHRASE_MW,
    KEY_REWARD_MESSAGES, ANIMAL_REWARD_MESSAGES,
    HOME_GREETINGS, FOREST_GREETINGS,
    CONFIRMATION_WORDS, NEXT_QUESTION_WORDS,
    # Functions
    classify_message, check_guards,
    process_answer, clean_reply, enforce_daily_task_guard,
    build_enriched_message, detect_learning_request,
    extract_question_from_events, detect_question_in_text,
    fetch_question_directly,
)

# ---------------------------------------------------------------------------
# Session + question stores (auto-detect: Firestore or in-memory)
# ---------------------------------------------------------------------------
store = create_session_store()
question_store = create_question_store()

# Base URL for internal API calls (Cloud Run uses PORT env var)
_PORT = int(os.environ.get("PORT", 8000))
_BASE_URL = f"http://127.0.0.1:{_PORT}"


# ---------------------------------------------------------------------------
# ASGI Middleware: Intercept POST /run to process daily_task_active from UE
# ---------------------------------------------------------------------------
class DailyTaskRunMiddleware:
    """Full-featured middleware for /run: daily task guard, answer check, reply cleanup."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method", "") != "POST"
            or not scope.get("path", "").endswith("/run")
        ):
            await self.app(scope, receive, send)
            return

        # Read full request body
        body_parts = []
        while True:
            message = await receive()
            body_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw_body = b"".join(body_parts)

        try:
            data = json.loads(raw_body)
        except Exception:
            await self._forward(scope, raw_body, send)
            return

        # Extract daily_task_active (UE may send various key names)
        daily_active = None
        for key in [
            "daily_task_active", "Daily Task Active",
            "dailyTaskActive", "Daily_Task_Active",
        ]:
            if key in data:
                daily_active = data.pop(key)
                break

        # If daily_task_active was NOT in the body this is an internal call
        # (/chat -> /run or ADK Web UI) — pass through untouched.
        if daily_active is None:
            await self._forward(scope, raw_body, send)
            return

        # Normalise to bool
        if isinstance(daily_active, str):
            daily_active = daily_active.lower() in ("true", "1", "yes")

        session_id = data.get("session_id", "")
        session = store.get(session_id)

        # STICKY daily_task_active: once UE sends True, remember it for the session.
        # This prevents timing issues where UE sends False on subsequent requests
        # even though the daily task was already started.
        if daily_active:
            session.daily_task_seen_active = True
        elif session.daily_task_seen_active:
            # UE says False but we've seen True before → trust the sticky value
            daily_active = True
            print(f"[MIDDLEWARE] daily_task_active=False from UE but was True before — using sticky True")

        # Read level from body (sent by UE Blueprint) and save it
        for lkey in ("level", "Level", "level_name", "Level Name"):
            if lkey in data:
                raw_level = str(data.pop(lkey)).strip().lower()
                if raw_level:
                    session.level = raw_level
                    print(f"[MIDDLEWARE] Saved level='{raw_level}' for session {session_id[:8]}")
                break

        level = session.level

        # Read daily_task_completed from body (sent by UE Blueprint)
        # PERSISTED: once True for a session, stays True forever
        daily_completed_val = None
        for ckey in ("daily_task_completed", "Daily Task Completed",
                      "dailyTaskCompleted", "Daily_Task_Completed"):
            if ckey in data:
                daily_completed_val = data.pop(ckey)
                print(f"[MIDDLEWARE] Raw daily_task_completed='{daily_completed_val}' (type={type(daily_completed_val).__name__})")
                break
        if isinstance(daily_completed_val, str):
            daily_completed_val = daily_completed_val.lower() in ("true", "1", "yes")

        # Persist: once daily task is completed, remember it for the entire session
        if daily_completed_val:
            session.daily_completed = True
            session.key_earned = False
            session.quiz_state = None
            session.quiz_mode = None

        daily_completed = session.daily_completed

        # Read player_name from body (sent by UE Blueprint)
        player_name = ""
        for nkey in ("player_name", "Player Name", "playerName", "Player_Name",
                      "username", "Username", "user_name", "User Name"):
            if nkey in data:
                player_name = str(data.pop(nkey)).strip()
                break

        # Read player_score from body (sent by UE Blueprint)
        player_score = ""
        for skey in ("player_score", "Player Score", "playerScore", "Player_Score"):
            if skey in data:
                player_score = str(data.pop(skey)).strip()
                break

        new_message = data.get("new_message", {})
        parts = new_message.get("parts", [])
        original_text = ""
        if parts and isinstance(parts[0], dict) and "text" in parts[0]:
            original_text = parts[0]["text"]

        # Skip system setup messages
        if original_text.startswith("SYSTEM SETUP:"):
            await self._forward(scope, json.dumps(data).encode(), send)
            return

        msg_lower = original_text.strip().lower()
        print(
            f"[MIDDLEWARE] /run: daily_task_active={daily_active}, "
            f"daily_task_completed={daily_completed}, "
            f"level={level}, message='{original_text[:80]}'"
        )

        # --- GUARD: empty/whitespace message ---
        if not msg_lower:
            await self._send_adk_response(scope, send, "I didn't catch that — could you say something?")
            return

        # --- Classify message ---
        cls = classify_message(msg_lower, original_text)

        # --- Run guards (greeting, daily completed, key earned, task not started) ---
        guard = check_guards(cls, session, daily_active, level, player_name)
        if guard.intercepted:
            tag = (
                "Greeting" if cls.is_greeting else
                "daily_task_completed" if (level == "home" and daily_completed and cls.is_key_request) else
                "key_already_earned" if (session.key_earned and cls.is_key_request) else
                "daily_task_not_started"
            )
            print(f"[MIDDLEWARE] {tag} intercepted — level={level}")
            store.append_history(session_id, original_text, guard.reply)
            await self._send_adk_response(scope, send, guard.reply)
            return

        # Set quiz mode to "key" when player asks for key/animal help
        if cls.is_key_request and (daily_active or level == "foresthideandseek"):
            session.quiz_mode = "key"
            print(f"[MIDDLEWARE] Quiz mode set to 'key' for session {session_id[:8]}")

        # --- Get active question data (per-session ONLY) ---
        from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTIONS as _LAQS
        from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTION as _LAQ_GLOBAL
        # Try question_store first (Firestore-backed), fall back to in-memory _LAQS
        _LAQ = question_store.get(session_id)
        if not _LAQ:
            _LAQ = _LAQS.get(session_id, {})

        # Clear active question if daily task is not active — HOME mode only.
        # Only clear if quiz is in "key" mode AND player is NOT answering the current question.
        quiz_mode = session.quiz_mode or "learning"
        _is_answering_quiz = _LAQ.get("active") and _LAQ.get("delivered") and not cls.is_key_request
        if level == "home" and not daily_active and _LAQ.get("active") and quiz_mode == "key" and not _is_answering_quiz:
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            session.clear_quiz()
            print("[MIDDLEWARE] Cleared active question — daily_task_active is False, key mode (home mode)")

        # --- GUARD 2: server-side answer check ---
        answer_result = process_answer(original_text, msg_lower, cls, session, _LAQ, level)
        answer_prefix = answer_result.answer_prefix

        # Apply state changes from process_answer
        if answer_result.clear_active_question:
            _LAQ["active"] = False
            _LAQ["delivered"] = False
        if answer_result.key_earned:
            session.key_earned = True
        if answer_result.clear_quiz_state:
            session.quiz_state = None
        if answer_result.clear_quiz_mode:
            session.quiz_mode = None
        if answer_result.quiz_state_update:
            session.quiz_state = answer_result.quiz_state_update

        # Log classification for non-answer messages during active quiz
        if cls.is_not_answer and _LAQ.get("active") and _LAQ.get("delivered") and not cls.is_skip:
            skip_type = (
                "key_request" if cls.is_key_request else
                "confirmation" if cls.is_confirmation else
                "next_question" if cls.is_next_question else
                "skip" if cls.is_skip else
                "hint" if cls.is_hint else
                "repeat" if cls.is_repeat else
                "filler" if cls.is_filler else
                "emoji" if cls.is_emoji_only else
                "game_state" if cls.is_game_state else
                "player_question"
            )
            print(f"[MIDDLEWARE] Skipping answer check — type={skip_type}: '{original_text[:60]}'")

        # If player asks for key again while a question is active, clear the old question
        if cls.is_key_request and _LAQ.get("active"):
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            session.clear_quiz_state_only()
            # NOTE: Do NOT clear quiz_mode here — key request already set it to "key" above
            print("[MIDDLEWARE] Cleared active question + quiz state — player asking for key again")

        # If player asks for next question, reset quiz state for fresh attempt tracking.
        if cls.is_next_question:
            qs_current = session.quiz_state or {}
            if qs_current.get("phase") in ("pronunciation", "teaching") and _LAQ.get("active"):
                _LAQ["active"] = False
                _LAQ["delivered"] = False
                print(f"[MIDDLEWARE] Player wants next question during {qs_current.get('phase')} phase — clearing active question")
            session.quiz_state = None

        # --- Detect learning request ---
        quiz_active_now = _LAQ.get("active", False) and _LAQ.get("delivered", False)
        is_learning = detect_learning_request(msg_lower, cls.is_key_request, quiz_active_now or bool(_LAQ.get("active")))
        if is_learning:
            session.quiz_mode = "learning"
            print(f"[MIDDLEWARE] Learning request detected — quiz_mode forced to 'learning'")

        # --- Build enriched message ---
        history_tag = store.build_history_tag(session_id)
        enriched = build_enriched_message(
            original_text, answer_prefix, history_tag, cls,
            daily_active, level, player_name, player_score,
            _LAQ, is_learning,
        )

        # Safety: ensure parts[0] exists before writing
        if not parts:
            parts.append({"text": original_text})
            new_message["parts"] = parts
        parts[0]["text"] = enriched

        modified_body = json.dumps(data).encode()

        # --- Capture the response so we can clean it up ---
        response_body_parts = []
        response_headers_sent = False
        original_status = 200
        original_headers = []

        async def capture_send(message_out):
            nonlocal response_headers_sent, original_status, original_headers
            if message_out["type"] == "http.response.start":
                original_status = message_out.get("status", 200)
                original_headers = list(message_out.get("headers", []))
                response_headers_sent = True
            elif message_out["type"] == "http.response.body":
                response_body_parts.append(message_out.get("body", b""))

        # Save quiz state before agent call — phantom quiz protection
        _was_active_before = _LAQ.get("active", False) and _LAQ.get("delivered", False)
        _saved_laq_mw = None
        if cls.is_not_answer and not cls.is_skip:
            _saved_laq_mw = dict(_LAQ)

        await self._forward(scope, modified_body, capture_send)

        # Re-fetch per-session question dict — agent may have called fetch_questions()
        # which creates _LAQS[session_id] that didn't exist before the agent call.
        if not _LAQ:
            _LAQ = _LAQS.get(session_id, {})
            if _LAQ:
                question_store.set(session_id, dict(_LAQ))
                print(f"[MIDDLEWARE] Re-fetched per-session question dict after agent call (session={session_id[:8]})")

        # --- Post-agent quiz state protection ---
        _is_active_after = _LAQ.get("active", False) and _LAQ.get("delivered", False)

        if cls.is_not_answer and not cls.is_skip and _saved_laq_mw is not None:
            if _was_active_before:
                # Case A: had active question before, restore it
                _LAQ.clear()
                _LAQ.update(_saved_laq_mw)
                if session_id in _LAQS:
                    _LAQS[session_id].clear()
                    _LAQS[session_id].update(_saved_laq_mw)
                print(f"[MIDDLEWARE] Restored quiz state after NOT_AN_ANSWER (correct_answer='{_saved_laq_mw.get('correct_answer', '')}')")
            elif _is_active_after:
                # Case B: NO active question before, but agent created one.
                # IMPORTANT: confirmations ("yes", "ok") and next-question requests
                # are LEGITIMATE quiz triggers — the agent is SUPPOSED to call
                # fetch_questions() for these. Only clear for other non-answer types.
                if cls.is_confirmation or cls.is_next_question:
                    print(f"[MIDDLEWARE] Agent fetched question after {('confirmation' if cls.is_confirmation else 'next_question')} — KEEPING (legitimate)")
                else:
                    _LAQ["active"] = False
                    _LAQ["delivered"] = False
                    if session_id in _LAQS:
                        _LAQS[session_id]["active"] = False
                        _LAQS[session_id]["delivered"] = False
                    print(f"[MIDDLEWARE] Cleared phantom quiz created by agent during non-answer message")

        # CRITICAL: phantom quiz block for normal conversation too
        if not _was_active_before and _is_active_after and not cls.is_not_answer and not answer_prefix:
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            if session_id in _LAQS:
                _LAQS[session_id]["active"] = False
                _LAQS[session_id]["delivered"] = False
            print(f"[MIDDLEWARE] PHANTOM QUIZ BLOCKED: agent created quiz during normal conversation (msg='{original_text[:50]}')")

        resp_body = b"".join(response_body_parts)

        # --- Post-process: strip context tags, detect questions, handle fallbacks ---
        try:
            events = json.loads(resp_body)
            if isinstance(events, list):
                has_visible_text = False
                for event in events:
                    author = event.get("author", "")
                    content = event.get("content")
                    if author != "user" and content:
                        ev_parts = content.get("parts", [])
                        for p in ev_parts:
                            if "text" in p:
                                t = clean_reply(p["text"])
                                # Enforce: home + daily task not started -> block key navigation
                                t = enforce_daily_task_guard(t, level, daily_active, session.quiz_mode)
                                p["text"] = t
                                if t:
                                    has_visible_text = True

                # Scan for question data in functionResponse events.
                # SKIP when NOT_AN_ANSWER was restored.
                question_text_from_fr = ""
                question_core_mw = ""
                if _saved_laq_mw is None:
                    question_text_from_fr, question_core_mw = extract_question_from_events(events)

                if question_text_from_fr:
                    # Check if the question is already in visible text parts
                    all_visible = " ".join(
                        p.get("text", "")
                        for ev in events
                        for p in (ev.get("content") or {}).get("parts", [])
                        if ev.get("author") != "user" and "text" in p
                    )
                    if question_core_mw and question_core_mw not in all_visible:
                        print(f"[MIDDLEWARE] Injecting question text into response")
                        injected = False
                        for event in reversed(events):
                            if event.get("author") != "user":
                                content = event.get("content")
                                if content and "parts" in content:
                                    content["parts"].append({"text": question_text_from_fr})
                                    injected = True
                                    break
                        if not injected:
                            events.append({
                                "author": "root_agent",
                                "content": {"parts": [{"text": question_text_from_fr}], "role": "model"},
                            })
                    # Mark question as delivered
                    _LAQ["delivered"] = True
                    if session_id in _LAQS:
                        _LAQS[session_id]["delivered"] = True
                    print(f"[MIDDLEWARE] Question delivered to player (via functionResponse)")

                # SAFETY NET: text-based question detection
                if not question_text_from_fr and _LAQ.get("active") and not _LAQ.get("delivered"):
                    all_visible_text = " ".join(
                        p.get("text", "")
                        for ev in events
                        for p in (ev.get("content") or {}).get("parts", [])
                        if ev.get("author") != "user" and "text" in p
                    )
                    if detect_question_in_text(all_visible_text):
                        _LAQ["delivered"] = True
                        if session_id in _LAQS:
                            _LAQS[session_id]["delivered"] = True
                        print(f"[MIDDLEWARE] Question delivered to player (via text detection — no functionResponse found)")

                # FALLBACK: agent returned empty for confirmation/next-question
                if not has_visible_text and not question_text_from_fr:
                    is_confirmation_msg = msg_lower in CONFIRMATION_WORDS or any(w in msg_lower for w in ["yes", "ok", "sure", "ready", "ask me"])
                    is_next_question_msg = msg_lower in NEXT_QUESTION_WORDS or any(w in msg_lower for w in ["next", "another", "more question", "new question", "start question", "begin question", "start quiz"])
                    if is_confirmation_msg or is_next_question_msg:
                        trigger_type = "next_question" if is_next_question_msg else "confirmation"
                        print(f"[MIDDLEWARE] Agent returned empty for '{original_text}' — fetching question directly (type={trigger_type})")
                        direct_q = await fetch_question_directly(session_id)
                        if direct_q:
                            events = [{
                                "author": "root_agent",
                                "content": {"parts": [{"text": direct_q}], "role": "model"},
                            }]
                            has_visible_text = True
                            _LAQ["delivered"] = True
                            if session_id in _LAQS:
                                _LAQS[session_id]["delivered"] = True
                            print(f"[MIDDLEWARE] Question delivered via direct fallback fetch")

                # FINAL FALLBACK: no visible text at all
                if not has_visible_text:
                    fallback_msg = "Hmm, I'm not sure what to say to that. Could you try asking differently?"
                    print(f"[MIDDLEWARE] Final fallback — no visible text, injecting generic response")
                    events = [{
                        "author": "root_agent",
                        "content": {"parts": [{"text": fallback_msg}], "role": "model"},
                    }]

                # Record conversation history
                final_reply_for_history = ""
                for ev in events:
                    if ev.get("author") != "user":
                        for p in (ev.get("content") or {}).get("parts", []):
                            if "text" in p and p["text"].strip():
                                final_reply_for_history = p["text"].strip()
                if original_text and final_reply_for_history:
                    store.append_history(session_id, original_text, final_reply_for_history)

                # Persist question state and session state to store (Firestore or in-memory)
                if _LAQ:
                    question_store.set(session_id, dict(_LAQ))
                store.save(session)

                # Return cleaned ADK JSON events
                new_body = json.dumps(events).encode()
                new_headers = []
                for k, v in original_headers:
                    kl = k.lower() if isinstance(k, str) else k
                    if kl in (b"content-length",):
                        continue
                    new_headers.append((k, v))
                new_headers.append((b"content-length", str(len(new_body)).encode()))

                await send({"type": "http.response.start", "status": 200, "headers": new_headers})
                await send({"type": "http.response.body", "body": new_body})
                return
        except Exception as e:
            print(f"[MIDDLEWARE] Response post-processing error (passing raw): {e}")

        # Fallback: send original response unchanged
        await send({"type": "http.response.start", "status": original_status, "headers": original_headers})
        await send({"type": "http.response.body", "body": resp_body})

    async def _forward(self, scope, body_bytes, send_fn):
        """Forward request with (possibly modified) body."""
        body_sent = False

        async def modified_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, modified_receive, send_fn)

    async def _send_adk_response(self, scope, send_fn, text: str):
        """Send an immediate fake ADK event JSON response without calling the agent."""
        fake_events = [{"author": "root_agent", "content": {"parts": [{"text": text}], "role": "model"}}]
        body = json.dumps(fake_events).encode()
        await send_fn({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send_fn({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Load .env before any imports that read QUESTIONS_SOURCE_API_URL
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / "Home_Agent" / ".env")

from google.adk.cli.fast_api import get_fast_api_app
from question_server import questions_router
from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTION, LAST_ACTIVE_QUESTIONS
import Home_Agent.tools.question_api as _question_api_module

# Wire question_store into question_api so fetch_questions() persists to Firestore
_question_api_module._question_store = question_store

# Always use the directory containing this script
agents_dir = str(Path(__file__).resolve().parent)
app = get_fast_api_app(
    agents_dir=agents_dir,
    web=True,
    use_local_storage=True,
)

# Allow Unreal Engine (and any local client) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(questions_router, prefix="/questions", tags=["questions"])

# ---------------------------------------------------------------------------
# Conversation start (ADK Web UI / Postman testing)
# ---------------------------------------------------------------------------
CONVERSATION_STD_MIN, CONVERSATION_STD_MAX = 6, 10
CONVERSATION_APP_NAME = "Home_Agent"
CONVERSATION_USER_ID = "user"
conversation_router = APIRouter()


class ConversationStartBody(BaseModel):
    std: int


@conversation_router.post("/start")
async def conversation_start(body: ConversationStartBody):
    """POST /conversation/start with body {"std": 6..10}."""
    std = body.std
    if not (CONVERSATION_STD_MIN <= std <= CONVERSATION_STD_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"std must be between {CONVERSATION_STD_MIN} and {CONVERSATION_STD_MAX}",
        )
    session_url = (
        f"{_BASE_URL}/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(session_url, json={"state": {"user:std": std}})
        resp.raise_for_status()
        session = resp.json()
    return {
        "ok": True,
        "std": std,
        "session_id": session["id"],
        "message": (
            f"Session created for grade {std}. "
            f"Open the chat with session_id={session['id']}. "
            f"The agent already knows your grade — just ask for a quiz."
        ),
    }


app.include_router(conversation_router, prefix="/conversation", tags=["conversation"])

# ---------------------------------------------------------------------------
# Unreal Engine endpoints: /session/create, /session/end, /chat
# ---------------------------------------------------------------------------
ue_router = APIRouter()

KEY_LOCATION_PHRASE = "||SHOW_KEY"
ANIMAL_LOCATION_PHRASE = "||SHOW_ANIMAL"


class UESessionCreateBody(BaseModel):
    std: int = 8
    level: str = "home"


class UEChatBody(BaseModel):
    session_id: str
    message: str
    daily_task_active: bool = False


class UESessionEndBody(BaseModel):
    session_id: str


@ue_router.post("/session/create", tags=["unreal-engine"])
async def ue_session_create(request: Request):
    """Create a new chat session for Unreal Engine.
    Response: plain text session_id string.
    """
    raw = await request.json()
    std = raw.get("std", raw.get("user:std", 8))
    level = raw.get("level", raw.get("user:level", "home"))
    if isinstance(std, str):
        try:
            std = int(std)
        except ValueError:
            std = 8
    print(f"[DEBUG] /session/create received: std={std}, level='{level}'")
    session_url = (
        f"{_BASE_URL}/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            session_url,
            json={"state": {"user:std": std, "user:level": str(level).strip().lower()}},
        )
        resp.raise_for_status()
        adk_session = resp.json()

    session_id = adk_session["id"]
    level = str(level).strip().lower()
    store.set_level(session_id, level)
    print(f"[DEBUG] Session {session_id[:12]}... stored level='{level}'")

    # Send initial setup message so the agent knows its level from message #1
    setup_msg = f"SYSTEM SETUP: The current game level is '{level}'. Act accordingly for all future messages."
    run_url = f"{_BASE_URL}/run"
    setup_payload = {
        "app_name": CONVERSATION_APP_NAME,
        "user_id": CONVERSATION_USER_ID,
        "session_id": session_id,
        "new_message": {"parts": [{"text": setup_msg}]},
        "streaming": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(run_url, json=setup_payload)
    print(f"[DEBUG] Setup message sent for level='{level}'")

    return PlainTextResponse(session_id)


@ue_router.post("/session/end", tags=["unreal-engine"])
async def ue_session_end(body: UESessionEndBody):
    """End a game session and clean up all server-side state."""
    sid = body.session_id
    store.delete(sid)
    question_store.delete(sid)
    LAST_ACTIVE_QUESTIONS.pop(sid, None)
    print(f"[DEBUG] Session {sid[:12]}... ended — all state cleared")
    return PlainTextResponse("ok")


@ue_router.post("/chat", tags=["unreal-engine"])
async def ue_chat(body: UEChatBody):
    """Send a chat message and get the agent's reply (non-streaming).

    Request:  {"session_id": "abc-123", "message": "where is key"}
    Response: plain text reply (with ||SHOW_KEY or ||SHOW_ANIMAL if applicable)
    """
    print(
        f"[DEBUG] /chat received: session_id={body.session_id[:12]}..., "
        f"message='{body.message}', daily_task_active={body.daily_task_active}"
    )
    session_id = body.session_id
    session = store.get(session_id)
    level = session.level
    msg_lower = body.message.strip().lower()

    # STICKY daily_task_active (mirrors middleware logic)
    if body.daily_task_active:
        session.daily_task_seen_active = True
    elif session.daily_task_seen_active:
        body.daily_task_active = True
        print(f"[DEBUG] daily_task_active=False from UE but was True before — using sticky True")

    # --- Classify and run guards ---
    cls = classify_message(msg_lower, body.message)

    # Greeting intercept
    if cls.is_greeting:
        if level == "foresthideandseek":
            greeting = random.choice(FOREST_GREETINGS).format(name="")
        else:
            greeting = random.choice(HOME_GREETINGS).format(name="")
        store.append_history(session_id, body.message.strip(), greeting)
        return PlainTextResponse(greeting)

    # Store daily_task_active in session state so get_daily_task_status tool can read it
    session_state_url = (
        f"{_BASE_URL}/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions/{session_id}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            state_resp = await client.get(session_state_url)
            if state_resp.status_code == 200:
                session_data = state_resp.json()
                current_state = session_data.get("state", {})
                current_state["daily_task_active"] = body.daily_task_active
                await client.patch(session_state_url, json={"state": current_state})
        except Exception as e:
            print(f"[DEBUG] Could not update daily_task_active in state: {e}")

    # Key request + daily task not active -> immediate refusal (Home mode)
    if level == "home" and not body.daily_task_active:
        if cls.is_key_request:
            refusal = "The daily task has not started yet. Start the daily task first, then I can help you find the key!"
            print(f"[DEBUG] Intercepted key request — daily_task_active is False")
            return PlainTextResponse(refusal)

    # Set quiz mode to "key" when player asks for key help
    if cls.is_key_request and body.daily_task_active:
        session.quiz_mode = "key"

    # --- Active question data (per-session ONLY — no global fallback) ---
    _LAQ = question_store.get(session_id)
    if not _LAQ:
        _LAQ = LAST_ACTIVE_QUESTIONS.get(session_id, {})

    # Clear active question if daily task is not active (home, key mode, not answering)
    quiz_mode = session.quiz_mode or "learning"
    _is_answering_quiz = _LAQ.get("active") and _LAQ.get("delivered") and not cls.is_key_request
    if level == "home" and not body.daily_task_active and _LAQ.get("active") and quiz_mode == "key" and not _is_answering_quiz:
        _LAQ["active"] = False
        _LAQ["delivered"] = False
        session.clear_quiz()
        print("[DEBUG] Cleared active question — daily_task_active is False, key mode (home mode)")

    # --- Server-side answer check ---
    answer_result = process_answer(body.message, msg_lower, cls, session, _LAQ, level)
    answer_prefix = answer_result.answer_prefix

    # Apply state changes from process_answer
    if answer_result.clear_active_question:
        _LAQ["active"] = False
        _LAQ["delivered"] = False
    if answer_result.key_earned:
        session.key_earned = True
    if answer_result.clear_quiz_state:
        session.quiz_state = None
    if answer_result.clear_quiz_mode:
        session.quiz_mode = None
    if answer_result.quiz_state_update:
        session.quiz_state = answer_result.quiz_state_update

    # If player asks for key again while a question is active, clear the old question
    if cls.is_key_request and _LAQ.get("active"):
        _LAQ["active"] = False
        _LAQ["delivered"] = False
        print("[DEBUG] Cleared active question — player asking for key again")

    # If player asks for next question during pronunciation/teaching, clear active question
    if cls.is_next_question:
        qs_current = session.quiz_state or {}
        if qs_current.get("phase") in ("pronunciation", "teaching") and _LAQ.get("active"):
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            print(f"[DEBUG] Player wants next question during {qs_current.get('phase')} phase — clearing active question")
        session.quiz_state = None

    # Detect learning request
    quiz_active_now = _LAQ.get("active", False)
    is_learning = detect_learning_request(msg_lower, cls.is_key_request, quiz_active_now)
    if is_learning:
        print(f"[DEBUG] Learning request detected — adding QUIZ_MODE: LEARNING tag")

    # Build enriched message
    history_tag = store.build_history_tag(session_id)
    level_prefix = f"[CURRENT_LEVEL: {level}] "
    task_prefix = ""
    if level == "home" and cls.is_key_request:
        task_prefix = "[DAILY_TASK: ACTIVE] " if body.daily_task_active else "[DAILY_TASK: NOT_STARTED] "
    learning_tag = "[QUIZ_MODE: LEARNING] " if is_learning else ""
    enriched_message = history_tag + answer_prefix + task_prefix + learning_tag + level_prefix + body.message

    # Save quiz state before agent call (NOT_AN_ANSWER protection)
    _saved_laq = None
    if cls.is_not_answer and _LAQ.get("active"):
        _saved_laq = dict(_LAQ)

    run_url = f"{_BASE_URL}/run"
    payload = {
        "app_name": CONVERSATION_APP_NAME,
        "user_id": CONVERSATION_USER_ID,
        "session_id": session_id,
        "new_message": {"parts": [{"text": enriched_message}]},
        "streaming": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(run_url, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        events = resp.json()

    # Re-fetch per-session question dict — agent may have called fetch_questions()
    if not _LAQ:
        _LAQ = question_store.get(session_id)
        if not _LAQ and session_id in LAST_ACTIVE_QUESTIONS:
            _LAQ = LAST_ACTIVE_QUESTIONS[session_id]
        if _LAQ:
            print(f"[DEBUG] Re-fetched per-session question dict after agent call (session={session_id[:8]})")

    # Extract the last agent text reply
    reply_text = ""
    for event in events:
        author = event.get("author", "")
        content = event.get("content")
        if author == "root_agent" and content:
            for part in content.get("parts", []):
                if "text" in part and part["text"].strip():
                    reply_text = part["text"].strip()

    # Restore quiz state if saved (NOT_AN_ANSWER protection)
    _was_active_before_chat = _saved_laq.get("active", False) if _saved_laq else False
    _is_active_after_chat = _LAQ.get("active", False)
    if _saved_laq is not None:
        if _was_active_before_chat:
            # Case A: had active question before → restore it (agent shouldn't have changed it)
            _LAQ.clear()
            _LAQ.update(_saved_laq)
            print(f"[DEBUG] Restored quiz state after NOT_AN_ANSWER (correct_answer='{_saved_laq.get('correct_answer', '')}')")
        elif _is_active_after_chat and (cls.is_confirmation or cls.is_next_question):
            # Case B: no question before, but agent LEGITIMATELY fetched one
            # after confirmation/next-question → KEEP it (don't restore empty state)
            print(f"[DEBUG] Agent fetched question after {('confirmation' if cls.is_confirmation else 'next_question')} — KEEPING (legitimate)")
        elif _is_active_after_chat:
            # Case C: no question before, agent unexpectedly fetched one → clear it
            _LAQ.clear()
            _LAQ.update(_saved_laq)
            print(f"[DEBUG] Cleared phantom quiz created by agent during non-answer message")

    # Scan for question data in functionResponse events (skip if restoring state)
    question_from_fr = ""
    question_core_text = ""
    if _saved_laq is None:
        question_from_fr, question_core_text = extract_question_from_events(events)

        if question_from_fr:
            if not reply_text:
                reply_text = question_from_fr
            elif question_core_text and question_core_text not in reply_text:
                reply_text = reply_text + " " + question_from_fr
            _LAQ["delivered"] = True
            question_store.update_field(session_id, "delivered", True)
            print(f"[DEBUG] Question delivered to player via /chat (functionResponse)")

        # SAFETY NET: text-based question detection
        if not question_from_fr and _LAQ.get("active") and not _LAQ.get("delivered"):
            if detect_question_in_text(reply_text or ""):
                _LAQ["delivered"] = True
                question_store.update_field(session_id, "delivered", True)
                print(f"[DEBUG] Question delivered to player via /chat (text detection)")

    # FALLBACK: agent returned empty for confirmation/next-question
    if not reply_text and not question_from_fr:
        if msg_lower in CONFIRMATION_WORDS or cls.is_next_question:
            print(f"[DEBUG] Agent returned empty for confirmation '{body.message}' — fetching question directly")
            direct_q = await fetch_question_directly(session_id)
            if direct_q:
                reply_text = direct_q
                _LAQ["delivered"] = True
                question_store.update_field(session_id, "delivered", True)
                print(f"[DEBUG] Question delivered via direct fallback fetch (/chat)")

    # Clean up echoed tags
    reply_text = clean_reply(reply_text)

    # Enforce daily task guard on reply
    final_quiz_mode = session.quiz_mode or "learning"
    if level == "home" and not body.daily_task_active and final_quiz_mode != "learning":
        reply_text = enforce_daily_task_guard(reply_text, level, body.daily_task_active, session.quiz_mode)

    # Record conversation history
    if body.message.strip() and reply_text:
        store.append_history(session_id, body.message.strip(), reply_text)

    # Persist session + question state (Firestore or in-memory)
    if _LAQ:
        question_store.set(session_id, dict(_LAQ))
    store.save(session)

    return PlainTextResponse(reply_text)


app.include_router(ue_router)

# Wrap the app with the daily_task_active middleware
# This MUST be after all routers are registered
app = DailyTaskRunMiddleware(app)

print(
    "Combined server: ADK + Questions API + Unreal Engine endpoints.\n"
    "  POST /session/create  — create a session (for UE)\n"
    "  POST /session/end     — end session & clear state (for UE)\n"
    "  POST /chat            — send message, get reply (for UE)\n"
    "  POST /questions       — fetch questions directly\n"
    "  POST /conversation/start — create session with std\n"
    f"  Server: {_BASE_URL}"
)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
