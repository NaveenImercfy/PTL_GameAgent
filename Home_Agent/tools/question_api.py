"""
Custom ADK tool to fetch quiz questions from the FastAPI endpoint.
"""

import json
import os
import random
import urllib.parse
import urllib.request
from typing import Any

try:
    from google.adk.tools import ToolContext
except ImportError:
    ToolContext = None  # type: ignore[misc, assignment]

# Shared dict: stores the latest active question PER SESSION.
# Key = session_id, Value = {correct_answer, options, active, delivered}
# Written by fetch_questions(), read by run_combined.py middleware.
# Both run in the same Python process so this is directly shared.
LAST_ACTIVE_QUESTIONS: dict[str, dict[str, Any]] = {}

# Backward-compat alias — points to the LAST session that fetched a question.
# Used as fallback when session_id is unknown.
LAST_ACTIVE_QUESTION: dict[str, Any] = {}

# Call the source API directly (avoids deadlock when running on the same server as the agent).
# Uses QUESTIONS_SOURCE_API_URL (Firebase) with GET query params by default.
# Falls back to QUESTION_API_URL if QUESTIONS_SOURCE_API_URL is not set.
QUESTION_API_URL = os.environ.get("QUESTIONS_SOURCE_API_URL") or os.environ.get("QUESTION_API_URL", "http://localhost:8001/questions")
QUESTION_API_METHOD = os.environ.get("QUESTIONS_SOURCE_API_METHOD", "GET").strip().upper()


def _get_request_body_from_env() -> dict[str, Any]:
    """Build POST body from env only (same as Postman: std, subject, topic). No hardcoded defaults."""
    body: dict[str, Any] = {}
    std = os.environ.get("QUESTION_API_STD")
    if std is not None and std.strip() != "":
        try:
            body["std"] = int(std.strip())
        except ValueError:
            body["std"] = std.strip()
    for key, env_key in (("subject", "QUESTION_API_SUBJECT"), ("topic", "QUESTION_API_TOPIC")):
        val = os.environ.get(env_key)
        if val is not None and val.strip() != "":
            body[key] = val.strip()
    return body


QUESTION_REQUEST_BODY = _get_request_body_from_env()


def _parse_content(content: str) -> list[dict[str, Any]]:
    """Parse the content string into structured question objects."""
    questions = []
    segments = content.split("{next}")
    
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
            
        if "{option}" not in segment or "{ans}" not in segment:
            continue
            
        parts = segment.split("{option}")
        question_text = parts[0].strip()
        
        rest = parts[1]
        options_part, ans_part = rest.split("{ans}", 1)
        options = [opt.strip() for opt in options_part.split("--") if opt.strip()]
        correct_answer = ans_part.strip()
        
        questions.append({
            "question": question_text,
            "options": options,
            "correct_answer": correct_answer,
        })
    
    return questions


def get_level(tool_context: "ToolContext") -> dict[str, str]:
    """
    Returns the current game level/map name from session state.

    The level is set when the session is created from Unreal Engine.
    Possible values: "home" (Home map) or "ForestHideAndSeek" (Forest Hide and Seek map).

    Use this tool at the START of a conversation to know which mode you are in,
    so you can behave as the correct character (Home Assistant AI vs Forest Explorer AI).

    Returns:
        A dict with the current level name.
    """
    level = "home"  # default
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        raw = tool_context.state.get("user:level")
        if raw is not None:
            level = str(raw).strip().lower()
    return {"level": level}


def get_user_std(tool_context: "ToolContext") -> dict[str, Any]:
    """
    Returns the current grade (std) saved in session state.

    Use this when the player asks "which grade", "which std", "which class",
    "from which syllabus are you asking questions" or similar.

    Returns:
        A dict with the current grade number.
    """
    std = 8  # default
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        raw = tool_context.state.get("user:std")
        if raw is not None:
            try:
                std = int(raw) if not isinstance(raw, int) else raw
            except (TypeError, ValueError):
                pass
    return {"std": std}


def set_user_std(std: int, tool_context: "ToolContext") -> dict[str, str]:
    """
    Saves the user's grade (standard) for this session so quiz questions can be fetched for that grade.

    Call this when the user tells you their grade/class (e.g. "9", "class 10", "I'm in 8th").
    After saving, use fetch_questions() without arguments to get questions for that grade.

    Args:
        std: The user's grade/standard (e.g. 6, 7, 8, 9, 10).

    Returns a short confirmation message.
    """
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state["user:std"] = std
    return {"status": "ok", "message": f"Grade {std} saved. You can ask for a quiz anytime."}


def fetch_questions(
    std: int | None = None,
    subject: str | None = None,
    topic: str | None = None,
    tool_context: "ToolContext | None" = None,
) -> dict[str, Any]:
    """
    Fetches ONE quiz question from the questions API. Each call returns a DIFFERENT question
    that has not been asked before in this session. Ask this question to the player exactly as
    returned — do NOT pick or filter, just use it directly.

    Args:
        std: Grade/standard for questions (e.g. 6, 7, 8, 9, 10). If omitted, uses session user:std or .env.
        subject: Optional subject (e.g. "math", "science"). If omitted, uses QUESTION_API_SUBJECT from .env.
        topic: Optional topic (e.g. "algebra"). If omitted, uses QUESTION_API_TOPIC from .env.

    Returns a single question dict with:
    - question: The question text
    - options: List of answer choices
    - correct_answer: The correct answer to validate user responses

    Use this tool when the user wants a quiz. Call fetch_questions() without arguments
    so the tool uses the grade they already provided via set_user_std.
    """
    # Resolve std from session state when not passed
    if std is None and tool_context is not None and getattr(tool_context, "state", None) is not None:
        raw = tool_context.state.get("user:std")
        if raw is not None:
            try:
                std = int(raw) if not isinstance(raw, int) else raw
            except (TypeError, ValueError):
                pass
    if std is None:
        env_std = os.environ.get("QUESTION_API_STD")
        if env_std is not None and str(env_std).strip() != "":
            try:
                std = int(str(env_std).strip())
            except ValueError:
                pass
    body = {**_get_request_body_from_env()}
    if std is not None:
        body["std"] = std
    if subject is not None:
        body["subject"] = subject
    if topic is not None:
        body["topic"] = topic
    body = {k: v for k, v in body.items() if v is not None}
    if "std" not in body:
        return {"error": "Grade (std) is not set. Please tell me your grade first (e.g. 'I am in grade 8')."}
    if QUESTION_API_METHOD == "GET":
        query = urllib.parse.urlencode({k: str(v) for k, v in body.items()})
        url = QUESTION_API_URL.rstrip("/") + ("?" + query if query else "")
        request = urllib.request.Request(url, method="GET")
    else:
        data_bytes = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            QUESTION_API_URL,
            data=data_bytes,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, "read") else str(e)
        return {"error": f"Failed to fetch questions (HTTP {e.code}): {error_body}"}
    except (urllib.error.URLError, OSError) as e:
        return {"error": f"Could not reach the questions server: {e}"}
    content = data.get("content", "")
    if not content:
        return {"error": f"No questions found for grade {body.get('std')}. The questions server returned an empty response."}
    questions = _parse_content(content)
    if not questions:
        return {"error": "Could not parse any questions from the server response."}

    # --- Pick ONE question that was NOT asked before in this session ---
    asked: list[str] = []
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        asked = tool_context.state.get("session:asked_questions", [])
        if not isinstance(asked, list):
            asked = []

    # Filter out previously asked questions
    unseen = [q for q in questions if q["question"] not in asked]
    if not unseen:
        # All questions exhausted — reset and start over
        asked = []
        unseen = questions

    # Pick one random question
    chosen = random.choice(unseen)

    # Save to session state so it won't repeat
    asked.append(chosen["question"])
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state["session:asked_questions"] = asked
        # Save the current question's correct answer and options for check_answer
        tool_context.state["session:current_correct_answer"] = chosen["correct_answer"]
        tool_context.state["session:current_options"] = chosen["options"]

    # Save to per-session dict so middleware can check answers for the correct session
    session_id = ""
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        session_id = tool_context.state.get("session:id", "") or ""
    q_data = {
        "correct_answer": chosen["correct_answer"],
        "options": chosen["options"],
        "active": True,
        "delivered": False,  # Set to True once question text is sent to player
    }
    if session_id:
        LAST_ACTIVE_QUESTIONS[session_id] = q_data
    # Also update global fallback for backward compat
    LAST_ACTIVE_QUESTION.update(q_data)
    print(f"[DEBUG][fetch_questions] Saved active question (session={session_id[:8] if session_id else 'global'}): correct_answer='{chosen['correct_answer']}'")

    return chosen


def get_daily_task_status(tool_context: "ToolContext") -> dict[str, Any]:
    """
    Returns whether the daily task is currently active for this session.

    In HOME mode, the player must start the daily task before they can ask for help
    finding the key. Call this tool when the player asks about the key to check
    if the daily task is active.

    Returns:
        {"daily_task_active": true} if the daily task has started.
        {"daily_task_active": false} if the daily task has NOT started yet.
    """
    active = False
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        raw = tool_context.state.get("daily_task_active")
        if raw is not None:
            active = bool(raw)
    return {"daily_task_active": active}


def check_answer(player_answer: str, tool_context: "ToolContext") -> dict[str, Any]:
    """
    Checks if the player's answer to the current quiz question is correct.
    ALWAYS call this tool when the player replies after you asked a quiz question.
    Do NOT try to check the answer yourself — call this tool and use its result.

    Args:
        player_answer: The player's reply (e.g. "B", "Water", "b water", "option B").

    Returns:
        {"correct": true} if the answer is right, {"correct": false} if wrong.
    """
    correct_answer = ""
    options: list[str] = []
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        correct_answer = tool_context.state.get("session:current_correct_answer", "")
        options = tool_context.state.get("session:current_options", [])
        if not isinstance(options, list):
            options = []

    if not correct_answer:
        return {"correct": False, "error": "No active question found."}

    answer = player_answer.strip().lower()
    correct = correct_answer.strip().lower()

    # Direct match: player typed the answer text (e.g. "water" == "Water")
    if answer == correct:
        return {"correct": True}

    # Check if player typed just a letter (A/B/C/D) or "option A" etc.
    letter_map = {}
    for i, opt in enumerate(options):
        letter = chr(65 + i).lower()  # a, b, c, d
        letter_map[letter] = opt.strip().lower()

    cleaned = answer.replace("option", "").replace(")", "").replace(".", "").strip()
    if len(cleaned) >= 1:
        first_char = cleaned[0]
        if first_char in letter_map and len(cleaned) <= 1 + len(letter_map.get(first_char, "")):
            if letter_map[first_char] == correct:
                return {"correct": True}

    for opt_text in letter_map.values():
        if answer == opt_text and opt_text == correct:
            return {"correct": True}

    if len(answer) > 3 and (correct in answer or answer in correct):
        return {"correct": True}

    return {"correct": False}
    