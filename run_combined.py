"""
Combined server: ADK Web UI + /questions API + /conversation/start + /chat (Unreal Engine) on one port (default 8000).
Run from project root: python run_combined.py  OR  uvicorn run_combined:app --reload --port 8000
"""

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# ASGI Middleware: Intercept POST /run to process daily_task_active from UE
# UE calls /run directly via Make Json.  ADK ignores unknown fields like
# "Daily Task Active", so this middleware:
#   1. Extracts daily_task_active from the request body
#   2. If false + player asks about key → returns immediate refusal (no agent call)
#   3. Checks LAST_ACTIVE_QUESTION for server-side answer validation
#   4. Enriches the message with [DAILY_TASK: ...] and [CURRENT_LEVEL: ...] tags
#   5. On the response side: strips echoed tags, enforces navigate_to_key guard
# ---------------------------------------------------------------------------
KEY_REQUEST_WORDS_MW = [
    "find the key", "where is the key", "where's the key",
    "help me find the key", "show me the key", "help me key",
    "i need the key", "give me the key", "find key",
]
# Standalone "key" handled separately with word-boundary matching (avoids monkey/turkey/donkey)
_KEY_WORD_RE = re.compile(r'\bkey\b')
KEY_PHRASE_MW = "follow me, i will show you the key"
ANIMAL_PHRASE_MW = "follow me, i will show you the animal"


CONFIRMATION_WORDS = {
    "yes", "ok", "okay", "sure", "ready", "yeah", "yep", "yea", "ya",
    "ok ask me", "ask me", "yes ask me", "yes please", "go ahead",
    "yes please ask me the question", "ok ask me the question",
    "i am ready", "im ready", "i'm ready", "go", "alright",
    "yes please ask me", "ask me the question",
}

NEXT_QUESTION_WORDS = {
    "next", "next question", "move to next question", "another question",
    "next one", "give me another question", "new question", "one more",
    "ask me another", "ask another question", "more questions",
    "help me to find next animal", "find next animal", "next animal",
    "help me find the next animal", "another animal",
    "lets start question", "let's start question", "start question",
    "start the question", "start quiz", "lets start", "let's start",
    "begin question", "begin the question", "lets go", "let's go",
}

# Words that mean "I don't want to answer" — NOT quiz answers
SKIP_WORDS = {
    "skip", "pass", "i don't know", "i dont know", "idk", "no idea",
    "no clue", "i give up", "give up", "i quit", "can't figure it out",
    "cant figure it out", "i'm stuck", "im stuck", "i have no idea",
    "skip this", "pass this", "skip question", "i surrender",
}

# Words that mean "give me a hint" — NOT quiz answers
HINT_WORDS = {
    "hint", "clue", "give me a hint", "give me a clue", "help me with this",
    "help me with the question", "i need a hint", "i need a clue",
    "can you help me", "explain this", "explain the question",
    "what does this mean", "help me answer",
}

# Words that mean "repeat the question" — NOT quiz answers
REPEAT_WORDS = {
    "repeat", "repeat the question", "say that again", "say it again",
    "what was the question", "what did you ask", "can you repeat",
    "again please", "tell me again", "i forgot the question",
    "repeat please", "one more time", "come again",
}

# Filler/acknowledgment messages — NOT quiz answers, not errors
FILLER_WORDS = {
    "lol", "haha", "hahaha", "lmao", "bruh", "bro", "hmm", "hmmm",
    "ok cool", "nice", "wow", "interesting", "oh", "ooh", "ahh",
    "k", "kk", "okay cool", "alright cool", "cool", "yay", "ohhh",
    "damn", "dang", "whoa", "omg", "oh my god", "oh wow",
}


async def _fetch_question_directly(session_id: str = "") -> str | None:
    """Call Firebase directly to get a question when the agent fails to."""
    from Home_Agent.tools.question_api import (
        QUESTION_API_URL, QUESTION_API_METHOD, _get_request_body_from_env,
        _parse_content, LAST_ACTIVE_QUESTION, LAST_ACTIVE_QUESTIONS,
    )
    import random as _random
    body = _get_request_body_from_env()
    if "std" not in body:
        body["std"] = 8
    try:
        if QUESTION_API_METHOD == "GET":
            import urllib.parse
            query = urllib.parse.urlencode({k: str(v) for k, v in body.items()})
            url = QUESTION_API_URL.rstrip("/") + ("?" + query if query else "")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(QUESTION_API_URL, json=body)
                resp.raise_for_status()
                data = resp.json()
        content = data.get("content", "")
        if not content:
            return None
        questions = _parse_content(content)
        if not questions:
            return None
        chosen = _random.choice(questions)
        # Store per-session + global fallback
        q_data = {
            "correct_answer": chosen["correct_answer"],
            "options": chosen["options"],
            "active": True,
            "delivered": True,
        }
        if session_id:
            LAST_ACTIVE_QUESTIONS[session_id] = q_data
        LAST_ACTIVE_QUESTION.update(q_data)
        options = chosen["options"]
        opts_str = "  ".join(f"{chr(65 + i)}) {opt}" for i, opt in enumerate(options))
        print(f"[SERVER] Direct question fetch: {chosen['question'][:60]}... answer={chosen['correct_answer']}")
        return f"Question: {chosen['question']}  Options: {opts_str}"
    except Exception as e:
        print(f"[SERVER] Direct question fetch failed: {e}")
        return None


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
        # (/chat → /run or ADK Web UI) — pass through untouched.
        if daily_active is None:
            await self._forward(scope, raw_body, send)
            return

        # Normalise to bool
        if isinstance(daily_active, str):
            daily_active = daily_active.lower() in ("true", "1", "yes")

        session_id = data.get("session_id", "")

        # Read level from body (sent by UE Blueprint) and save it
        for lkey in ("level", "Level", "level_name", "Level Name"):
            if lkey in data:
                raw_level = str(data.pop(lkey)).strip().lower()
                if raw_level:
                    SESSION_LEVELS[session_id] = raw_level
                    print(f"[MIDDLEWARE] Saved level='{raw_level}' for session {session_id[:8]}")
                break

        level = SESSION_LEVELS.get(session_id, "home")

        # Read daily_task_completed from body (sent by UE Blueprint)
        # PERSISTED: once True for a session, stays True forever (can't uncomplete a task)
        daily_completed = None
        for ckey in ("daily_task_completed", "Daily Task Completed",
                      "dailyTaskCompleted", "Daily_Task_Completed"):
            if ckey in data:
                daily_completed = data.pop(ckey)
                print(f"[MIDDLEWARE] Raw daily_task_completed='{daily_completed}' (type={type(daily_completed).__name__})")
                break
        if isinstance(daily_completed, str):
            daily_completed = daily_completed.lower() in ("true", "1", "yes")

        # Persist: once daily task is completed, remember it for the entire session
        if daily_completed:
            SESSION_DAILY_COMPLETED[session_id] = True
            SESSION_KEY_EARNED.pop(session_id, None)
            SESSION_QUIZ_STATE.pop(session_id, None)
            SESSION_QUIZ_MODE.pop(session_id, None)
        else:
            # Use persisted state if this request didn't send daily_task_completed
            daily_completed = SESSION_DAILY_COMPLETED.get(session_id, False)

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

        # --- GUARD: empty/whitespace message → gentle prompt ---
        if not msg_lower:
            await self._send_adk_response(scope, send, "I didn't catch that — could you say something?")
            return

        # Helper: check if message is a key request using word-boundary for "key"
        # (avoids false matches on "monkey", "turkey", "donkey", "keyboard")
        def _is_key_request_check(text: str) -> bool:
            if any(w in text for w in KEY_REQUEST_WORDS_MW):
                return True
            return bool(_KEY_WORD_RE.search(text))

        is_key_request = _is_key_request_check(msg_lower)

        # --- GUARD 0: greeting — return correct character greeting based on level ---
        GREETING_WORDS_MW = {
            "hello", "hi", "hey", "hii", "helo", "greetings", "howdy", "sup",
            "yo", "yoo", "hola", "heyyy", "heyy", "wassup", "whatsup",
        }
        if msg_lower in GREETING_WORDS_MW:
            player_name_tag = player_name if player_name else ""
            name_bit = f" {player_name_tag}!" if player_name_tag else "!"
            if level == "foresthideandseek":
                greeting = (
                    f"Hey{name_bit} I'm the Forest Explorer AI — "
                    f"I've been exploring around here and this forest is amazing! What's up?"
                )
            else:
                greeting = (
                    f"Hey{name_bit} I'm your Home Assistant — "
                    f"great to see you! How can I help you today?"
                )
            print(f"[MIDDLEWARE] Greeting intercepted — level={level}")
            await self._send_adk_response(scope, send, greeting)
            return

        # --- GUARD 0.5: daily task completed → no key in the home ---
        if level == "home" and daily_completed:
            if is_key_request:
                completed_msg = (
                    "You've already completed the daily task — "
                    "there's no key in the home anymore. Great job!"
                )
                print("[MIDDLEWARE] Intercepted key request — daily_task_completed=True")
                await self._send_adk_response(scope, send, completed_msg)
                return

        # --- GUARD 0.75: player already earned the key → repeat navigate response ---
        if not daily_completed and SESSION_KEY_EARNED.get(session_id) and is_key_request:
            navigate_msg = "Follow me, I will show you the key."
            print("[MIDDLEWARE] Key already earned — repeating navigate response")
            await self._send_adk_response(scope, send, navigate_msg)
            return

        # Set quiz mode to "key" when player asks for key/animal help
        if is_key_request and (daily_active or level == "foresthideandseek"):
            SESSION_QUIZ_MODE[session_id] = "key"
            print(f"[MIDDLEWARE] Quiz mode set to 'key' for session {session_id[:8]}")

        # --- GUARD 1: key request while daily task not started → immediate refusal ---
        if level == "home" and not daily_active and not daily_completed:
            if is_key_request:
                refusal = (
                    "The daily task hasn't started yet! "
                    "Start the daily task first, then I can help you find the key."
                )
                print("[MIDDLEWARE] Intercepted key request — daily_task_active=False")
                await self._send_adk_response(scope, send, refusal)
                return

        # --- GUARD 2: server-side answer check ---
        from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTIONS as _LAQS
        from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTION as _LAQ_GLOBAL
        # Use per-session question data, fall back to global
        _LAQ = _LAQS.get(session_id, _LAQ_GLOBAL)
        answer_prefix = ""

        # Clear active question if daily task is not active — HOME mode only.
        # BUT only clear if the quiz is in "key" mode — learning mode quizzes stay active.
        quiz_mode = SESSION_QUIZ_MODE.get(session_id, "learning")
        if level == "home" and not daily_active and _LAQ.get("active") and quiz_mode == "key":
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            SESSION_QUIZ_STATE.pop(session_id, None)
            SESSION_QUIZ_MODE.pop(session_id, None)
            print("[MIDDLEWARE] Cleared active question — daily_task_active is False, key mode (home mode)")

        # --- Classify the message to decide if it's an answer attempt ---
        # Only use exact-match for confirmations (not substring — avoids "yes the answer is B" being skipped)
        is_confirmation = msg_lower in CONFIRMATION_WORDS
        is_next_question = msg_lower in NEXT_QUESTION_WORDS or any(
            w in msg_lower for w in ["next", "another", "more question", "new question", "start question", "begin question", "start quiz"]
        )
        is_skip = msg_lower in SKIP_WORDS or any(
            w in msg_lower for w in ["i don't know", "i dont know", "no idea", "give up", "i quit", "i surrender"]
        )
        is_hint = msg_lower in HINT_WORDS or any(
            w in msg_lower for w in ["hint", "clue", "help me with"]
        )
        is_repeat = msg_lower in REPEAT_WORDS or any(
            w in msg_lower for w in ["repeat", "say that again", "say it again", "what was the question", "one more time"]
        )
        is_filler = msg_lower in FILLER_WORDS
        # Detect emoji-only messages (no alphanumeric content)
        is_emoji_only = bool(msg_lower) and not any(c.isalnum() for c in msg_lower)

        # Detect player questions/conversation
        is_player_question = (
            "?" in original_text
            or any(msg_lower.startswith(w) for w in [
                "why ", "how ", "what ", "when ", "where ", "who ",
                "can you", "can i", "could you", "tell me", "explain",
                "i don't", "i dont", "i want to know", "i have a question",
                "i want to ask", "please tell", "but ",
            ])
            or any(w in msg_lower for w in [
                "instead of", "how come", "why not", "what about",
                "i don't understand", "i dont understand", "not fair",
                "that's not", "thats not",
            ])
        )

        # Combined: anything that is NOT an answer attempt
        is_not_answer = (is_key_request or is_confirmation or is_next_question
                         or is_skip or is_hint or is_repeat or is_filler
                         or is_emoji_only or is_player_question)

        if is_not_answer and _LAQ.get("active") and _LAQ.get("delivered"):
            skip_type = (
                "key_request" if is_key_request else
                "confirmation" if is_confirmation else
                "next_question" if is_next_question else
                "skip" if is_skip else
                "hint" if is_hint else
                "repeat" if is_repeat else
                "filler" if is_filler else
                "emoji" if is_emoji_only else
                "player_question"
            )
            print(f"[MIDDLEWARE] Skipping answer check — type={skip_type}: '{original_text[:60]}'")

        # --- Determine quiz mode: "key" (reward on correct) or "learning" (no reward) ---
        quiz_mode = SESSION_QUIZ_MODE.get(session_id, "learning")
        mode_tag = f" MODE: {quiz_mode.upper()}."

        # --- Handle "I don't know" / skip / give up — teach the answer ---
        if is_skip and _LAQ.get("active") and _LAQ.get("delivered"):
            correct_answer = _LAQ.get("correct_answer", "")
            qs = SESSION_QUIZ_STATE.get(session_id, {"attempt": 1, "phase": "answering"})
            answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                f"DONT_KNOW — the player does not know the answer.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Teach the player the correct answer, then ask them to say it back to you. "
                f"Do NOT give the reward yet — wait for them to pronounce it correctly.] "
            )
            qs["phase"] = "teaching"
            SESSION_QUIZ_STATE[session_id] = qs
            print(f"[MIDDLEWARE] Player doesn't know — teaching answer: {correct_answer} (mode={quiz_mode})")

        if _LAQ.get("active") and _LAQ.get("delivered") and not is_not_answer:
            correct_answer = _LAQ.get("correct_answer", "")
            options = _LAQ.get("options", [])
            result = _check_answer_locally_mw(msg_lower, correct_answer, options)

            # Load or init per-session quiz state
            qs = SESSION_QUIZ_STATE.get(session_id, {"attempt": 1, "phase": "answering"})

            if result == "correct":
                # ── EXACT CORRECT (any phase) → reward (only in key mode) ──
                if qs["phase"] in ("pronunciation", "teaching"):
                    if quiz_mode == "key":
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                            f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{mode_tag} "
                            f"You MUST reply with the correct-answer phrase now. Do NOT say anything else.] "
                        )
                    else:
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                            f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{mode_tag} "
                            f"Congratulate them warmly and ask if they want another question.] "
                        )
                    print(f"[MIDDLEWARE] Pronunciation CORRECT (mode={quiz_mode})")
                else:
                    if quiz_mode == "key":
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                            f"The answer is CORRECT.{mode_tag} You MUST reply with the correct-answer phrase now. "
                            f"Do NOT say anything else.] "
                        )
                    else:
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                            f"The answer is CORRECT.{mode_tag} "
                            f"Congratulate them warmly and ask if they want another question.] "
                        )
                    print(f"[MIDDLEWARE] Answer CORRECT (expected: {correct_answer}, mode={quiz_mode})")
                # Only earn the key in "key" mode
                if quiz_mode == "key":
                    SESSION_KEY_EARNED[session_id] = True
                _LAQ["active"] = False
                SESSION_QUIZ_STATE.pop(session_id, None)
                if quiz_mode == "key":
                    SESSION_QUIZ_MODE.pop(session_id, None)

            elif result == "near_match":
                # ── NEAR MATCH (speech-to-text typo) → pronunciation correction ──
                if qs["phase"] == "teaching":
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                        f"PRONUNCIATION_CLOSE — almost correct but not exact.{mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Ask the player to try pronouncing it one more time.] "
                    )
                    print(f"[MIDDLEWARE] Teaching pronunciation CLOSE (expected: {correct_answer})")
                else:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                        f"NEAR_MATCH — the answer is very close but has a pronunciation/spelling error.{mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Encourage the player to say it correctly. Do NOT give the reward yet.] "
                    )
                    qs["phase"] = "pronunciation"
                    SESSION_QUIZ_STATE[session_id] = qs
                    print(f"[MIDDLEWARE] Answer NEAR_MATCH (expected: {correct_answer}) — pronunciation phase")

            else:
                # ── WRONG ──
                if qs["phase"] in ("pronunciation", "teaching"):
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                        f"PRONUNCIATION_WRONG — not correct.{mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Teach the answer again and ask the player to say it.] "
                    )
                    qs["phase"] = "teaching"
                    SESSION_QUIZ_STATE[session_id] = qs
                    print(f"[MIDDLEWARE] Teaching pronunciation WRONG (expected: {correct_answer})")
                elif qs["attempt"] == 1:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                        f"WRONG_FIRST — this is their first attempt.{mode_tag} "
                        f"Encourage them to try one more time. Tell them if they get it wrong again, "
                        f"you will teach them the answer.] "
                    )
                    qs["attempt"] = 2
                    SESSION_QUIZ_STATE[session_id] = qs
                    print(f"[MIDDLEWARE] Answer WRONG attempt 1 (expected: {correct_answer})")
                else:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                        f"WRONG_FINAL — second wrong attempt.{mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Teach the player the correct answer, then ask them to say it back to you. "
                        f"Do NOT give the reward yet — wait for them to pronounce it correctly.] "
                    )
                    qs["phase"] = "teaching"
                    SESSION_QUIZ_STATE[session_id] = qs
                    print(f"[MIDDLEWARE] Answer WRONG attempt 2 — teaching (expected: {correct_answer})")

        # If player asks for key again while a question is active, clear the old question
        # so they get a fresh quiz flow
        if is_key_request and _LAQ.get("active"):
            _LAQ["active"] = False
            _LAQ["delivered"] = False
            SESSION_QUIZ_STATE.pop(session_id, None)
            # NOTE: Do NOT clear SESSION_QUIZ_MODE here — key request already set it to "key" above
            print("[MIDDLEWARE] Cleared active question + quiz state — player asking for key again")

        # If player asks for next question, reset quiz state for fresh attempt tracking
        if is_next_question:
            SESSION_QUIZ_STATE.pop(session_id, None)

        # --- Enrich message with tags ---
        # Only add daily task tag for key-related messages (uses word-boundary "key" check).
        task_tag = ""
        if is_key_request:
            task_tag = "[DAILY_TASK: ACTIVE] " if daily_active else "[DAILY_TASK: NOT_STARTED] "
        level_tag = f"[CURRENT_LEVEL: {level}]"
        name_tag = f" [PLAYER_NAME: {player_name}]" if player_name else ""
        score_tag = f" [PLAYER_SCORE: {player_score}]" if player_score else ""

        # Detect learning requests and add explicit QUIZ_MODE tag
        LEARNING_PHRASES_MW = [
            "ask me a question", "ask me some question", "ask me question",
            "ask me questions", "general question",
            "quiz me", "test me", "test my knowledge",
            "practice question", "i want to learn", "i want to practice",
        ]
        is_learning_request_mw = (
            not is_key_request
            and not _LAQ.get("active")
            and (
                any(phrase in msg_lower for phrase in LEARNING_PHRASES_MW)
                or ("ask me" in msg_lower and "question" in msg_lower)
                or ("ask" in msg_lower and "question" in msg_lower)
            )
        )
        learning_tag_mw = " [QUIZ_MODE: LEARNING]" if is_learning_request_mw else ""
        if is_learning_request_mw:
            print(f"[MIDDLEWARE] Learning request detected — adding QUIZ_MODE: LEARNING tag")

        # Safety: ensure parts[0] exists before writing
        if not parts:
            parts.append({"text": original_text})
            new_message["parts"] = parts
        parts[0]["text"] = f"{answer_prefix}{task_tag}{level_tag}{name_tag}{score_tag}{learning_tag_mw} {original_text}"

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

        await self._forward(scope, modified_body, capture_send)

        resp_body = b"".join(response_body_parts)

        # --- Post-process: strip context tags from agent replies, return clean ADK JSON ---
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
                                t = p["text"]
                                t = re.sub(r"\[QUIZ_ANSWER_RESULT:.*?\]\s*", "", t).strip()
                                t = re.sub(r"\[CURRENT_LEVEL:.*?\]\s*", "", t).strip()
                                t = re.sub(r"\[DAILY_TASK:.*?\]\s*", "", t).strip()
                                t = re.sub(r"\[PLAYER_NAME:.*?\]\s*", "", t).strip()
                                t = re.sub(r"\[PLAYER_SCORE:.*?\]\s*", "", t).strip()
                                t = re.sub(r"\[QUIZ_MODE:.*?\]\s*", "", t).strip()
                                # Enforce: home + daily task not started → block key navigation
                                if level == "home" and not daily_active and KEY_PHRASE_MW in t.lower():
                                    t = (
                                        "The daily task has not started yet. "
                                        "Start the daily task first, then I can help you find the key!"
                                    )
                                p["text"] = t
                                if t:
                                    has_visible_text = True

                # Always scan for question data in functionResponse events.
                # The agent may say "I will ask you one question..." as text but
                # NOT include the actual question — extract it from functionResponse
                # and append it if not already present in visible text.
                question_text_from_fr = ""
                question_core_mw = ""
                for event in events:
                    content = event.get("content") or {}
                    for part in content.get("parts", []):
                        fr = part.get("functionResponse", {})
                        resp_data = fr.get("response", {})
                        if "question" in resp_data:
                            question_core_mw = resp_data["question"]
                            options = resp_data.get("options", [])
                            opts_str = "  ".join(
                                f"{chr(65 + i)}) {opt}" for i, opt in enumerate(options)
                            )
                            question_text_from_fr = f"Question: {question_core_mw}  Options: {opts_str}"
                            break
                    if question_text_from_fr:
                        break

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
                        # Append question to last agent event or create one
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
                    # Mark question as delivered so answer-check activates next turn
                    _LAQ["delivered"] = True
                    if session_id in _LAQS:
                        _LAQS[session_id]["delivered"] = True
                    print(f"[MIDDLEWARE] Question delivered to player")

                # FALLBACK: if agent returned empty AND user sent a confirmation
                # or "next question" request, fetch a question directly from Firebase.
                # Gemini 2.5 Flash sometimes returns empty responses (0 parts) for
                # short messages like "yes", "ok", "next", "next question", etc.
                if not has_visible_text and not question_text_from_fr:
                    is_confirmation_msg = msg_lower in CONFIRMATION_WORDS or any(w in msg_lower for w in ["yes", "ok", "sure", "ready", "ask me"])
                    is_next_question_msg = msg_lower in NEXT_QUESTION_WORDS or any(w in msg_lower for w in ["next", "another", "more question", "new question", "start question", "begin question", "start quiz"])
                    if is_confirmation_msg or is_next_question_msg:
                        trigger_type = "next_question" if is_next_question_msg else "confirmation"
                        print(f"[MIDDLEWARE] Agent returned empty for '{original_text}' — fetching question directly (type={trigger_type})")
                        direct_q = await _fetch_question_directly(session_id)
                        if direct_q:
                            events = [{
                                "author": "root_agent",
                                "content": {"parts": [{"text": direct_q}], "role": "model"},
                            }]
                            has_visible_text = True

                # FINAL FALLBACK: if STILL no visible text after all attempts,
                # return a helpful message instead of blank
                if not has_visible_text:
                    fallback_msg = "Hmm, I'm not sure what to say to that. Could you try asking differently?"
                    print(f"[MIDDLEWARE] Final fallback — no visible text, injecting generic response")
                    events = [{
                        "author": "root_agent",
                        "content": {"parts": [{"text": fallback_msg}], "role": "model"},
                    }]

                # Return cleaned ADK JSON events (same format as without daily_task_active)
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


def _normalize_answer(s: str) -> str:
    """Strip units, punctuation, and common prefixes from quiz answers."""
    s = s.strip().lower()
    # Strip casual prefixes players add
    for prefix in [
        "the answer is ", "answer is ", "it is ", "it's ", "its ",
        "i think ", "i think it's ", "i think its ", "i believe ",
        "my answer is ", "that is ", "that's ", "thats ",
    ]:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Normalize unit symbols ↔ words
    s = s.replace("°c", "").replace("°f", "").replace("°", "")
    s = s.replace("degree celsius", "").replace("degree fahrenheit", "")
    s = s.replace("degrees", "").replace("degree", "")
    s = s.replace("percent", "").replace("%", "")
    # Strip trailing punctuation
    s = s.rstrip("!.,;:?")
    return s.strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    """Return similarity ratio (0.0–1.0) between two strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


_NEAR_MATCH_THRESHOLD = 0.65  # 65% similarity = close enough for speech-to-text typos


def _check_answer_locally_mw(answer_lower: str, correct_answer: str, options: list) -> str:
    """Answer check used by middleware.

    Returns: "correct", "near_match", or "wrong".
    """
    correct = correct_answer.strip().lower()
    answer = answer_lower.strip()
    norm_answer = _normalize_answer(answer)
    norm_correct = _normalize_answer(correct)

    # --- EXACT match checks (returns "correct") ---

    # Direct text match (original + normalized)
    if answer == correct or (norm_answer and norm_answer == norm_correct):
        return "correct"

    # Build letter → option text map
    letter_map = {}
    for i, opt in enumerate(options):
        letter_map[chr(65 + i).lower()] = opt.strip().lower()

    # Player typed a letter: "A", "a", "option A", "A)", "a."
    # Also check normalized answer to handle "the answer is c", "i think B"
    for ans in [answer, norm_answer]:
        cleaned = ans.replace("option", "").replace(")", "").replace(".", "").strip()
        if len(cleaned) >= 1:
            first_char = cleaned[0]
            if first_char in letter_map and len(cleaned) <= 1 + len(letter_map.get(first_char, "")):
                if letter_map[first_char] == correct:
                    return "correct"

    # Player typed the full option text (original or normalized)
    for opt_text in letter_map.values():
        if opt_text == correct:
            norm_opt = _normalize_answer(opt_text)
            if answer == opt_text or norm_answer == norm_opt:
                return "correct"

    # Partial match (original + normalized, >=3 chars to avoid false positives)
    if len(answer) >= 3 and (correct in answer or answer in correct):
        return "correct"
    if len(norm_answer) >= 3 and (norm_correct in norm_answer or norm_answer in norm_correct):
        return "correct"

    # --- NEAR MATCH checks (fuzzy — for speech-to-text typos) ---
    # Check against correct answer text
    best_ratio = max(
        _fuzzy_ratio(norm_answer, norm_correct) if norm_answer else 0.0,
        _fuzzy_ratio(answer, correct),
    )

    # Also check against each option text (player might say an option that is the answer)
    for opt_text in letter_map.values():
        if opt_text == correct:
            norm_opt = _normalize_answer(opt_text)
            best_ratio = max(
                best_ratio,
                _fuzzy_ratio(answer, opt_text),
                _fuzzy_ratio(norm_answer, norm_opt) if norm_answer else 0.0,
            )

    if best_ratio >= _NEAR_MATCH_THRESHOLD:
        return "near_match"

    return "wrong"

# Load .env before any imports that read QUESTIONS_SOURCE_API_URL
load_dotenv(Path(__file__).resolve().parent / "Home_Agent" / ".env")

from google.adk.cli.fast_api import get_fast_api_app

from question_server import questions_router
from Home_Agent.tools.question_api import LAST_ACTIVE_QUESTION

# Always use the directory containing this script (not cwd) so it works from any terminal
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

# Conversation start: create an ADK session with user:std pre-set in state so the agent
# immediately knows the student's grade without requiring an extra chat message.
CONVERSATION_STD_MIN, CONVERSATION_STD_MAX = 6, 10
CONVERSATION_APP_NAME = "Home_Agent"
CONVERSATION_USER_ID = "user"
conversation_router = APIRouter()


class ConversationStartBody(BaseModel):
    """Body for POST /conversation/start. Std is used to set session grade for the chat."""
    std: int


@conversation_router.post("/start")
async def conversation_start(body: ConversationStartBody):
    """POST /conversation/start with body {"std": 6..10}.
    Creates an ADK session with user:std pre-set in state and returns the session_id.
    The client should open the chat with the returned session_id — no extra first message needed.
    """
    std = body.std
    if not (CONVERSATION_STD_MIN <= std <= CONVERSATION_STD_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"std must be between {CONVERSATION_STD_MIN} and {CONVERSATION_STD_MAX}",
        )
    # Create a new ADK session with user:std stored in session state
    session_url = (
        f"http://127.0.0.1:8000/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            session_url,
            json={"state": {"user:std": std}},
        )
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
# Unreal Engine endpoints: /session/create  and  /chat
# Simple JSON request/response — no SSE, no complex ADK event format.
# ---------------------------------------------------------------------------
ue_router = APIRouter()

# In-memory map: session_id → level (bypasses ADK state which is unreliable for per-session values)
SESSION_LEVELS: dict[str, str] = {}

# Track which sessions have earned the key (correct quiz answer)
SESSION_KEY_EARNED: dict[str, bool] = {}

# Track which sessions have completed the daily task (persisted — once True, stays True)
SESSION_DAILY_COMPLETED: dict[str, bool] = {}

# Per-session quiz answer state machine:
#   "attempt": int (1 or 2) — which try the player is on
#   "phase": str — "answering" | "pronunciation" | "teaching"
# Default for new quiz: {"attempt": 1, "phase": "answering"}
SESSION_QUIZ_STATE: dict[str, dict] = {}

# Quiz mode: "key" (player asked for key/animal help → reward on correct)
#            "learning" (just practicing → no key/animal reward)
SESSION_QUIZ_MODE: dict[str, str] = {}

# Active question data is stored in Home_Agent.tools.question_api.LAST_ACTIVE_QUESTION
# (same Python process — direct memory access, no API needed)

KEY_LOCATION_PHRASE = "Follow me, I will show you the key"
ANIMAL_LOCATION_PHRASE = "Follow me, I will show you the animal"


def _check_answer_locally(player_answer: str, correct_answer: str, options: list) -> bool:
    """Check if the player's answer matches the correct answer. Handles letters, text, mixed."""
    answer = player_answer.strip().lower()
    correct = correct_answer.strip().lower()

    if answer == correct:
        return True

    letter_map = {}
    for i, opt in enumerate(options):
        letter_map[chr(65 + i).lower()] = opt.strip().lower()

    cleaned = answer.replace("option", "").replace(")", "").replace(".", "").strip()
    if len(cleaned) >= 1:
        first_char = cleaned[0]
        if first_char in letter_map and len(cleaned) <= 1 + len(letter_map.get(first_char, "")):
            if letter_map[first_char] == correct:
                return True

    for opt_text in letter_map.values():
        if answer == opt_text and opt_text == correct:
            return True

    if len(answer) > 3 and (correct in answer or answer in correct):
        return True

    return False




class UESessionCreateBody(BaseModel):
    """POST /session/create — create a session for Unreal Engine."""
    std: int = 8
    level: str = "home"


class UEChatBody(BaseModel):
    """POST /chat — send a message from Unreal Engine to the agent."""
    session_id: str
    message: str
    daily_task_active: bool = False  # Home: true when daily task collider triggered


@ue_router.post("/session/create", tags=["unreal-engine"])
async def ue_session_create(request: Request):
    """Create a new chat session for Unreal Engine.

    Accepts both field name formats:
      {"std": 8, "level": "home"}           ← correct format
      {"user:std": 8, "user:level": "home"} ← also accepted (UE sends this)

    Response: plain text session_id string (e.g. "abc-123-xyz")
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
        f"http://127.0.0.1:8000/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            session_url,
            json={"state": {"user:std": std, "user:level": str(level).strip().lower()}},
        )
        resp.raise_for_status()
        session = resp.json()
    # Store level in memory so /chat can inject it reliably
    session_id = session["id"]
    level = str(level).strip().lower()
    SESSION_LEVELS[session_id] = level
    print(f"[DEBUG] Session {session_id[:12]}... stored level='{level}'")

    # Send an initial setup message so the agent knows its level from message #1
    setup_msg = f"SYSTEM SETUP: The current game level is '{level}'. Act accordingly for all future messages."
    run_url = "http://127.0.0.1:8000/run"
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

    # Return plain text so UE Blueprint can use Result Body directly as SessionID
    return PlainTextResponse(session_id)


@ue_router.post("/chat", tags=["unreal-engine"])
async def ue_chat(body: UEChatBody):
    """Send a chat message and get the agent's reply (non-streaming).

    Request:  {"session_id": "abc-123", "message": "where is key"}
    Response: {"reply": "I will ask you one question...", "navigate_to_key": false}
    """
    print(
        f"[DEBUG] /chat received: session_id={body.session_id[:12]}..., "
        f"message='{body.message}', daily_task_active={body.daily_task_active}"
    )
    # Look up the level for this session and prepend it to the message
    level = SESSION_LEVELS.get(body.session_id, "home")
    level_prefix = f"[CURRENT_LEVEL: {level}] "

    # Intercept greeting messages — return correct character greeting based on stored level
    GREETING_WORDS = {"hello", "hi", "hey", "hii", "helo", "greetings", "howdy", "sup"}
    if body.message.strip().lower() in GREETING_WORDS:
        if level == "foresthideandseek":
            return PlainTextResponse(
                "Hello! I am the Forest Explorer AI, ready to guide you through this forest and help you find hidden animals!"
            )
        else:
            return PlainTextResponse(
                "Hello! I am the Home Assistant AI, ready to help you in your home."
            )

    # Store daily_task_active in session state so get_daily_task_status tool can read it
    session_state_url = (
        f"http://127.0.0.1:8000/apps/{CONVERSATION_APP_NAME}"
        f"/users/{CONVERSATION_USER_ID}/sessions/{body.session_id}"
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

    # --- SERVER-SIDE EARLY INTERCEPT ---
    # If daily_task_active is False in Home mode and player asks about key,
    # return refusal immediately WITHOUT sending to agent
    KEY_REQUEST_WORDS = ["key", "find the key", "where is the key", "help me find", "show me the key", "help key"]
    if level == "home" and not body.daily_task_active:
        msg_lower = body.message.strip().lower()
        if any(word in msg_lower for word in KEY_REQUEST_WORDS):
            refusal = "The daily task has not started yet. Start the daily task first, then I can help you find the key!"
            print(f"[DEBUG] Intercepted key request — daily_task_active is False")
            return PlainTextResponse(refusal)

    # --- SERVER-SIDE ANSWER CHECK ---
    # Only check answers if the question was actually delivered to the player
    answer_prefix = ""
    msg_lower_chat = body.message.strip().lower()

    # Clear active question if daily task is not active — HOME mode only.
    # BUT only clear if quiz is in "key" mode — learning quizzes stay active.
    chat_quiz_mode = SESSION_QUIZ_MODE.get(body.session_id, "learning")
    if level == "home" and not body.daily_task_active and LAST_ACTIVE_QUESTION.get("active") and chat_quiz_mode == "key":
        LAST_ACTIVE_QUESTION["active"] = False
        LAST_ACTIVE_QUESTION["delivered"] = False
        SESSION_QUIZ_MODE.pop(body.session_id, None)
        print("[DEBUG] Cleared active question — daily_task_active is False, key mode (home mode)")

    # Don't treat key requests or confirmations as answer attempts
    KEY_REQUEST_WORDS = ["key", "find the key", "where is the key", "help me find", "show me the key", "help key"]
    is_key_request_chat = any(w in msg_lower_chat for w in KEY_REQUEST_WORDS)

    # Set quiz mode to "key" when player asks for key help
    if is_key_request_chat and body.daily_task_active:
        SESSION_QUIZ_MODE[body.session_id] = "key"
    is_confirmation_chat = msg_lower_chat in CONFIRMATION_WORDS or any(
        w in msg_lower_chat for w in ["yes", "ok", "sure", "ready", "ask me", "now"]
    )

    # Detect skip/don't know phrases
    SKIP_WORDS_CHAT = {"skip", "pass", "i give up", "i surrender", "i quit"}
    is_skip_chat = msg_lower_chat in SKIP_WORDS_CHAT or any(
        w in msg_lower_chat for w in ["i don't know", "i dont know", "no idea", "give up"]
    )

    if LAST_ACTIVE_QUESTION.get("active") and LAST_ACTIVE_QUESTION.get("delivered") and not is_key_request_chat and not is_confirmation_chat:
        correct_answer = LAST_ACTIVE_QUESTION.get("correct_answer", "")
        options = LAST_ACTIVE_QUESTION.get("options", [])
        chat_mode = SESSION_QUIZ_MODE.get(body.session_id, "learning")
        chat_mode_tag = f" MODE: {chat_mode.upper()}."

        # Load quiz state for this session
        chat_qs = SESSION_QUIZ_STATE.get(body.session_id, {"attempt": 1, "phase": "answering"})

        if is_skip_chat:
            # Player doesn't know — teach the answer
            answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{body.message}". '
                f"DONT_KNOW — the player does not know the answer.{chat_mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Teach the player the correct answer, then ask them to say it back to you. "
                f"Do NOT give the reward yet — wait for them to pronounce it correctly.] "
            )
            chat_qs["phase"] = "teaching"
            SESSION_QUIZ_STATE[body.session_id] = chat_qs
            print(f"[DEBUG] Player doesn't know — teaching answer: {correct_answer} (mode={chat_mode})")
        else:
            result = _check_answer_locally_mw(msg_lower_chat, correct_answer, options)

            if result == "correct":
                if chat_qs["phase"] in ("pronunciation", "teaching"):
                    if chat_mode == "key":
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player said "{body.message}". '
                            f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{chat_mode_tag} "
                            f"You MUST reply with the correct-answer phrase now. Do NOT say anything else.] "
                        )
                    else:
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player said "{body.message}". '
                            f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{chat_mode_tag} "
                            f"Congratulate them warmly and ask if they want another question.] "
                        )
                    print(f"[DEBUG] Pronunciation CORRECT (mode={chat_mode})")
                else:
                    if chat_mode == "key":
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player answered "{body.message}". '
                            f"The answer is CORRECT.{chat_mode_tag} You MUST reply with the correct-answer phrase now. "
                            f"Do NOT say anything else.] "
                        )
                    else:
                        answer_prefix = (
                            f'[QUIZ_ANSWER_RESULT: The player answered "{body.message}". '
                            f"The answer is CORRECT.{chat_mode_tag} "
                            f"Congratulate them warmly and ask if they want another question.] "
                        )
                    print(f"[DEBUG] Answer CORRECT (expected: {correct_answer}, mode={chat_mode})")
                if chat_mode == "key":
                    SESSION_KEY_EARNED[body.session_id] = True
                LAST_ACTIVE_QUESTION["active"] = False
                SESSION_QUIZ_STATE.pop(body.session_id, None)
                if chat_mode == "key":
                    SESSION_QUIZ_MODE.pop(body.session_id, None)

            elif result == "near_match":
                if chat_qs["phase"] == "teaching":
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player said "{body.message}". '
                        f"PRONUNCIATION_CLOSE — almost correct but not exact.{chat_mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Ask the player to try pronouncing it one more time.] "
                    )
                    print(f"[DEBUG] Teaching pronunciation CLOSE (expected: {correct_answer})")
                else:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{body.message}". '
                        f"NEAR_MATCH — the answer is very close but has a pronunciation/spelling error.{chat_mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Encourage the player to say it correctly. Do NOT give the reward yet.] "
                    )
                    chat_qs["phase"] = "pronunciation"
                    SESSION_QUIZ_STATE[body.session_id] = chat_qs
                    print(f"[DEBUG] Answer NEAR_MATCH (expected: {correct_answer}) — pronunciation phase")

            else:
                # WRONG
                if chat_qs["phase"] in ("pronunciation", "teaching"):
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player said "{body.message}". '
                        f"PRONUNCIATION_WRONG — not correct.{chat_mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Teach the answer again and ask the player to say it.] "
                    )
                    chat_qs["phase"] = "teaching"
                    SESSION_QUIZ_STATE[body.session_id] = chat_qs
                    print(f"[DEBUG] Teaching pronunciation WRONG (expected: {correct_answer})")
                elif chat_qs["attempt"] == 1:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{body.message}". '
                        f"WRONG_FIRST — this is their first attempt.{chat_mode_tag} "
                        f"Encourage them to try one more time. Tell them if they get it wrong again, "
                        f"you will teach them the answer.] "
                    )
                    chat_qs["attempt"] = 2
                    SESSION_QUIZ_STATE[body.session_id] = chat_qs
                    print(f"[DEBUG] Answer WRONG attempt 1 (expected: {correct_answer})")
                else:
                    answer_prefix = (
                        f'[QUIZ_ANSWER_RESULT: The player answered "{body.message}". '
                        f"WRONG_FINAL — second wrong attempt.{chat_mode_tag} "
                        f'The correct answer is "{correct_answer}". '
                        f"Teach the player the correct answer, then ask them to say it back to you. "
                        f"Do NOT give the reward yet — wait for them to pronounce it correctly.] "
                    )
                    chat_qs["phase"] = "teaching"
                    SESSION_QUIZ_STATE[body.session_id] = chat_qs
                    print(f"[DEBUG] Answer WRONG attempt 2 — teaching (expected: {correct_answer})")

    # If player asks for key again while a question is active, clear the old question
    if is_key_request_chat and LAST_ACTIVE_QUESTION.get("active"):
        LAST_ACTIVE_QUESTION["active"] = False
        LAST_ACTIVE_QUESTION["delivered"] = False
        print("[DEBUG] Cleared active question — player asking for key again")

    # Add daily task status prefix — only for KEY requests in Home mode
    # (learning questions don't need daily task checks)
    task_prefix = ""
    if level == "home" and is_key_request_chat:
        if body.daily_task_active:
            task_prefix = "[DAILY_TASK: ACTIVE] "
        else:
            task_prefix = "[DAILY_TASK: NOT_STARTED] "

    # Detect learning requests and add explicit tag so the agent skips daily task check
    LEARNING_PHRASES_CHAT = [
        "ask me a question", "ask me some question", "ask me question",
        "ask me questions", "general question",
        "quiz me", "test me", "test my knowledge",
        "practice question", "i want to learn", "i want to practice",
    ]
    is_learning_request = (
        not is_key_request_chat
        and not LAST_ACTIVE_QUESTION.get("active")
        and (
            any(phrase in msg_lower_chat for phrase in LEARNING_PHRASES_CHAT)
            or ("ask me" in msg_lower_chat and "question" in msg_lower_chat)
            or ("ask" in msg_lower_chat and "question" in msg_lower_chat)
        )
    )
    learning_tag = "[QUIZ_MODE: LEARNING] " if is_learning_request else ""
    if is_learning_request:
        print(f"[DEBUG] Learning request detected — adding QUIZ_MODE: LEARNING tag")

    enriched_message = answer_prefix + task_prefix + learning_tag + level_prefix + body.message

    run_url = "http://127.0.0.1:8000/run"
    payload = {
        "app_name": CONVERSATION_APP_NAME,
        "user_id": CONVERSATION_USER_ID,
        "session_id": body.session_id,
        "new_message": {
            "parts": [{"text": enriched_message}],
        },
        "streaming": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(run_url, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        events = resp.json()

    # Extract the last agent text reply — forward pass so we keep overwriting with the latest text
    reply_text = ""
    for event in events:
        author = event.get("author", "")
        content = event.get("content")
        if author == "root_agent" and content:
            for part in content.get("parts", []):
                if "text" in part and part["text"].strip():
                    reply_text = part["text"].strip()

    # Always scan for question data in functionResponse events.
    # The agent may say "I will ask you one question..." but NOT present
    # the actual question — extract it from functionResponse and append.
    question_from_fr = ""
    question_core_text = ""
    for event in events:
        content = event.get("content") or {}
        for part in content.get("parts", []):
            fr = part.get("functionResponse", {})
            resp_data = fr.get("response", {})
            if "question" in resp_data:
                question_core_text = resp_data["question"]
                options = resp_data.get("options", [])
                opts_str = "  ".join(f"{chr(65 + i)}) {opt}" for i, opt in enumerate(options))
                question_from_fr = f"Question: {question_core_text}  Options: {opts_str}"
                break
        if question_from_fr:
            break
    if question_from_fr:
        if not reply_text:
            reply_text = question_from_fr
        elif question_core_text and question_core_text not in reply_text:
            # Agent said something but didn't include the question — append it
            reply_text = reply_text + " " + question_from_fr
        # If agent already included the question, just mark delivered (no append)
        LAST_ACTIVE_QUESTION["delivered"] = True
        print(f"[DEBUG] Question delivered to player via /chat")

    # FALLBACK: if agent returned empty AND user sent a confirmation,
    # fetch a question directly from Firebase (Gemini 2.5 Flash sometimes
    # returns empty responses for short confirmations like "yes", "ok")
    if not reply_text and not question_from_fr:
        msg_lower = body.message.strip().lower()
        if msg_lower in CONFIRMATION_WORDS or any(w in msg_lower for w in ["yes", "ok", "sure", "ready", "ask me"]):
            print(f"[DEBUG] Agent returned empty for confirmation '{body.message}' — fetching question directly")
            direct_q = await _fetch_question_directly()
            if direct_q:
                reply_text = direct_q

    # Clean up: strip any [QUIZ_ANSWER_RESULT: ...] tags the agent may echo back
    reply_text = re.sub(r"\[QUIZ_ANSWER_RESULT:.*?\]\s*", "", reply_text).strip()
    # Also strip [CURRENT_LEVEL: ...] and [DAILY_TASK: ...] tags if echoed
    reply_text = re.sub(r"\[CURRENT_LEVEL:.*?\]\s*", "", reply_text).strip()
    reply_text = re.sub(r"\[DAILY_TASK:.*?\]\s*", "", reply_text).strip()
    reply_text = re.sub(r"\[PLAYER_NAME:.*?\]\s*", "", reply_text).strip()
    reply_text = re.sub(r"\[PLAYER_SCORE:.*?\]\s*", "", reply_text).strip()
    reply_text = re.sub(r"\[QUIZ_MODE:.*?\]\s*", "", reply_text).strip()

    # Enforce daily task rules at server level:
    # In HOME level, do NOT allow navigation to key when daily_task_active is False,
    # UNLESS the quiz is in learning mode (learning mode should never produce this phrase,
    # but if it does, just let it through — learning mode has no key reward anyway).
    final_quiz_mode = SESSION_QUIZ_MODE.get(body.session_id, "learning")
    if level == "home" and not body.daily_task_active and final_quiz_mode != "learning":
        if KEY_LOCATION_PHRASE.lower() in reply_text.lower():
            reply_text = (
                "The daily task has not started yet. "
                "Start the daily task first, then I can help you find the key!"
            )

    return PlainTextResponse(reply_text)


app.include_router(ue_router)

# Wrap the app with the daily_task_active middleware
# This MUST be after all routers are registered
app = DailyTaskRunMiddleware(app)

print(
    "Combined server: ADK + Questions API + Unreal Engine endpoints.\n"
    "  POST /session/create  — create a session (for UE)\n"
    "  POST /chat            — send message, get reply (for UE)\n"
    "  POST /questions       — fetch questions directly\n"
    "  POST /conversation/start — create session with std\n"
    "  Server: http://127.0.0.1:8000"
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
