"""
Centralized session state management for the Home Agent.

Supports two backends:
- In-memory (local dev, single instance)
- Firestore (Cloud Run, multi-instance, persistent)

Auto-detects: uses Firestore if google-cloud-firestore is installed and
FIRESTORE_PROJECT_ID is set, otherwise falls back to in-memory.
"""

from __future__ import annotations

import os
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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for Firestore storage."""
        return {
            "session_id": self.session_id,
            "level": self.level,
            "key_earned": self.key_earned,
            "daily_completed": self.daily_completed,
            "daily_task_seen_active": self.daily_task_seen_active,
            "quiz_state": self.quiz_state,
            "quiz_mode": self.quiz_mode,
            "chat_history": self.chat_history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """Deserialize from a Firestore document."""
        return cls(
            session_id=data.get("session_id", ""),
            level=data.get("level", "home"),
            key_earned=data.get("key_earned", False),
            daily_completed=data.get("daily_completed", False),
            daily_task_seen_active=data.get("daily_task_seen_active", False),
            quiz_state=data.get("quiz_state"),
            quiz_mode=data.get("quiz_mode"),
            chat_history=data.get("chat_history", []),
        )


# ---------------------------------------------------------------------------
# In-memory store (local dev / single instance)
# ---------------------------------------------------------------------------

class SessionStore:
    """In-memory session store (single process only)."""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        """Get or auto-create session state."""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(session_id=session_id)
        return self._sessions[session_id]

    def save(self, session: SessionState):
        """Save session state (no-op for in-memory — already in dict)."""
        self._sessions[session.session_id] = session

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
            clean_agent = agent_text.replace("||SHOW_KEY", "").replace("||SHOW_ANIMAL", "").strip()
            history.append({"role": "agent", "text": clean_agent})
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


# ---------------------------------------------------------------------------
# Firestore-backed store (Cloud Run, multi-instance)
# ---------------------------------------------------------------------------

class FirestoreSessionStore(SessionStore):
    """Firestore-backed session store for multi-instance Cloud Run.

    Uses the same interface as SessionStore so run_combined.py doesn't
    need to change. Each session is a document in the 'agent_sessions'
    collection. Reads from Firestore on get(), writes on save()/setter calls.
    """

    def __init__(self, project_id: str | None = None, collection: str = "agent_sessions"):
        super().__init__()
        from google.cloud import firestore
        self._db = firestore.Client(project=project_id)
        self._collection = collection
        print(f"[SESSION] Using Firestore backend (collection='{collection}')")

    def _doc_ref(self, session_id: str):
        return self._db.collection(self._collection).document(session_id)

    def get(self, session_id: str) -> SessionState:
        """Get from Firestore, or create new if not found."""
        # Check in-memory cache first (avoid Firestore read on every call within same request)
        if session_id in self._sessions:
            return self._sessions[session_id]
        # Read from Firestore
        doc = self._doc_ref(session_id).get()
        if doc.exists:
            session = SessionState.from_dict(doc.to_dict())
        else:
            session = SessionState(session_id=session_id)
        self._sessions[session_id] = session
        return session

    def save(self, session: SessionState):
        """Write session state to Firestore."""
        self._sessions[session.session_id] = session
        self._doc_ref(session.session_id).set(session.to_dict())

    def delete(self, session_id: str):
        """Remove from Firestore and in-memory cache."""
        self._sessions.pop(session_id, None)
        self._doc_ref(session_id).delete()

    # Override setters to auto-save to Firestore
    def set_level(self, session_id: str, level: str):
        s = self.get(session_id)
        s.level = level
        self.save(s)

    def mark_key_earned(self, session_id: str):
        s = self.get(session_id)
        s.key_earned = True
        self.save(s)

    def mark_daily_completed(self, session_id: str):
        s = self.get(session_id)
        s.daily_completed = True
        self.save(s)

    def append_history(self, session_id: str, player_text: str, agent_text: str):
        """Append history and save to Firestore."""
        super().append_history(session_id, player_text, agent_text)
        self.save(self.get(session_id))


# ---------------------------------------------------------------------------
# Firestore-backed question store (replaces LAST_ACTIVE_QUESTIONS dict)
# ---------------------------------------------------------------------------

class QuestionStore:
    """In-memory question store (default — single instance)."""

    def __init__(self):
        self._questions: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str) -> dict[str, Any]:
        return self._questions.get(session_id, {})

    def set(self, session_id: str, data: dict[str, Any]):
        self._questions[session_id] = data

    def update_field(self, session_id: str, key: str, value: Any):
        if session_id in self._questions:
            self._questions[session_id][key] = value

    def delete(self, session_id: str):
        self._questions.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        return session_id in self._questions


class FirestoreQuestionStore(QuestionStore):
    """Firestore-backed question store for multi-instance Cloud Run."""

    def __init__(self, project_id: str | None = None, collection: str = "agent_questions"):
        super().__init__()
        from google.cloud import firestore
        self._db = firestore.Client(project=project_id)
        self._collection = collection
        print(f"[QUESTION_STORE] Using Firestore backend (collection='{collection}')")

    def _doc_ref(self, session_id: str):
        return self._db.collection(self._collection).document(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        # Check in-memory cache first
        if session_id in self._questions:
            return self._questions[session_id]
        doc = self._doc_ref(session_id).get()
        if doc.exists:
            data = doc.to_dict()
            self._questions[session_id] = data
            return data
        return {}

    def set(self, session_id: str, data: dict[str, Any]):
        self._questions[session_id] = data
        self._doc_ref(session_id).set(data)

    def update_field(self, session_id: str, key: str, value: Any):
        if session_id in self._questions:
            self._questions[session_id][key] = value
        # Also update Firestore
        self._doc_ref(session_id).update({key: value})

    def delete(self, session_id: str):
        self._questions.pop(session_id, None)
        self._doc_ref(session_id).delete()

    def has(self, session_id: str) -> bool:
        if session_id in self._questions:
            return True
        doc = self._doc_ref(session_id).get()
        return doc.exists


# ---------------------------------------------------------------------------
# Factory: auto-detect backend
# ---------------------------------------------------------------------------

def create_session_store() -> SessionStore:
    """Create the appropriate session store based on environment."""
    project_id = os.environ.get("FIRESTORE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    use_firestore = os.environ.get("USE_FIRESTORE", "").lower() in ("true", "1", "yes")

    if use_firestore and project_id:
        try:
            return FirestoreSessionStore(project_id=project_id)
        except Exception as e:
            print(f"[SESSION] Firestore init failed: {e} — falling back to in-memory")
            return SessionStore()
    else:
        print("[SESSION] Using in-memory backend (set USE_FIRESTORE=true + GOOGLE_CLOUD_PROJECT for Firestore)")
        return SessionStore()


def create_question_store() -> QuestionStore:
    """Create the appropriate question store based on environment."""
    project_id = os.environ.get("FIRESTORE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    use_firestore = os.environ.get("USE_FIRESTORE", "").lower() in ("true", "1", "yes")

    if use_firestore and project_id:
        try:
            return FirestoreQuestionStore(project_id=project_id)
        except Exception as e:
            print(f"[QUESTION_STORE] Firestore init failed: {e} — falling back to in-memory")
            return QuestionStore()
    else:
        print("[QUESTION_STORE] Using in-memory backend")
        return QuestionStore()
