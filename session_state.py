"""
Centralized session state management for the Home Agent.

Replaces 6 separate global dicts (SESSION_LEVELS, SESSION_KEY_EARNED,
SESSION_DAILY_COMPLETED, SESSION_QUIZ_STATE, SESSION_QUIZ_MODE,
SESSION_CHAT_HISTORY) with a single SessionStore that manages
SessionState objects per session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Constants for chat history management
MAX_HISTORY_ENTRIES = 20   # 10 turns x 2 (player + agent)
INJECT_HISTORY_TURNS = 5   # Inject last 5 turns into enriched message


@dataclass
class SessionState:
    """All per-session state in one place."""
    session_id: str
    level: str = "home"                          # "home" or "foresthideandseek"
    key_earned: bool = False                     # player answered correctly in key mode
    daily_completed: bool = False                # persisted — once True, stays True
    daily_task_seen_active: bool = False          # sticky — once UE sends True, stays True for session
    quiz_state: dict[str, Any] | None = None     # {"attempt": 1|2, "phase": "answering"|"pronunciation"|"teaching"}
    quiz_mode: str | None = None                 # "key" | "learning" | None
    chat_history: list[dict[str, str]] = field(default_factory=list)

    # --- Quiz State helpers ---

    def get_quiz_state(self) -> dict[str, Any]:
        """Return current quiz state, creating default if None."""
        if self.quiz_state is None:
            return {"attempt": 1, "phase": "answering"}
        return self.quiz_state

    def clear_quiz(self):
        """Clear quiz state and mode."""
        self.quiz_state = None
        self.quiz_mode = None

    def clear_quiz_state_only(self):
        """Clear just quiz_state (keep quiz_mode)."""
        self.quiz_state = None

    def clear_quiz_mode_only(self):
        """Clear just quiz_mode (keep quiz_state)."""
        self.quiz_mode = None


class SessionStore:
    """Thread-safe-ish in-memory session store (single process)."""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        """Get or auto-create session state."""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(session_id=session_id)
        return self._sessions[session_id]

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def delete(self, session_id: str):
        """Remove all state for a session (/session/end cleanup)."""
        self._sessions.pop(session_id, None)

    # --- Convenience setters ---

    def set_level(self, session_id: str, level: str):
        self.get(session_id).level = level

    def mark_key_earned(self, session_id: str):
        self.get(session_id).key_earned = True

    def mark_daily_completed(self, session_id: str):
        self.get(session_id).daily_completed = True

    # --- Chat History ---

    def append_history(self, session_id: str, player_text: str, agent_text: str):
        """Append a player+agent exchange to session history, capped at MAX_HISTORY_ENTRIES."""
        if not player_text and not agent_text:
            return
        history = self.get(session_id).chat_history
        if player_text:
            history.append({"role": "player", "text": player_text})
        if agent_text:
            # Strip action tags before storing
            clean_agent = agent_text.replace("||SHOW_KEY", "").replace("||SHOW_ANIMAL", "").strip()
            history.append({"role": "agent", "text": clean_agent})
        # Trim from front if over limit
        while len(history) > MAX_HISTORY_ENTRIES:
            history.pop(0)

    def build_history_tag(self, session_id: str) -> str:
        """Build a [CHAT_HISTORY: ...] tag from recent conversation turns."""
        history = self.get(session_id).chat_history
        if not history:
            return ""
        recent = history[-(INJECT_HISTORY_TURNS * 2):]
        parts = []
        for entry in recent:
            role = "Player" if entry["role"] == "player" else "Agent"
            text = entry["text"][:150]
            parts.append(f'{role}: "{text}"')
        return "[CHAT_HISTORY: " + " | ".join(parts) + "] "
