"""
Shared business logic for quiz processing, message classification, guards, and answer checking.

Extracted from run_combined.py to eliminate duplication between the middleware (/run)
and the /chat endpoint.  Both endpoints import from here instead of re-implementing
the same rules.
"""

from __future__ import annotations

import os
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from session_state import SessionState

# ---------------------------------------------------------------------------
# 1. CONSTANTS
# ---------------------------------------------------------------------------

KEY_REQUEST_WORDS: list[str] = [
    "find the key", "where is the key", "where's the key",
    "help me find the key", "show me the key", "help me key",
    "i need the key", "give me the key", "find key",
]

# Animal request words for forest mode (equivalent of key requests)
ANIMAL_REQUEST_WORDS: list[str] = [
    "find the animal", "where is the animal", "where's the animal",
    "help me find the animal", "show me the animal", "help me animal",
    "i need the animal", "find animal", "help me find animal",
]

_KEY_WORD_RE: re.Pattern[str] = re.compile(r'\bkey\b')
_ANIMAL_WORD_RE: re.Pattern[str] = re.compile(r'\banimal\b')

KEY_PHRASE_MW: str = "||SHOW_KEY"
ANIMAL_PHRASE_MW: str = "||SHOW_ANIMAL"

KEY_REWARD_MESSAGES: list[str] = [
    "Follow me, I'll show you where the key is!||SHOW_KEY",
    "Come with me, the key is this way!||SHOW_KEY",
    "Let's go, I know where the key is!||SHOW_KEY",
    "Stay with me, I'll take you to the key!||SHOW_KEY",
    "Follow me, the key is just ahead!||SHOW_KEY",
]

ANIMAL_REWARD_MESSAGES: list[str] = [
    "Follow me, I think the animal is hiding nearby!||SHOW_ANIMAL",
    "Come with me, I'll show you where the animal is!||SHOW_ANIMAL",
    "Let's go find that animal together!||SHOW_ANIMAL",
    "Follow me, the animal is this way!||SHOW_ANIMAL",
    "Stay close, I'll take you to the animal!||SHOW_ANIMAL",
]

HOME_GREETINGS: list[str] = [
    "Hey{name}! So happy to see you!",
    "Hi{name}! Welcome! What would you like to do today?",
    "Hello{name}! Glad you're here!",
    "Hi there{name}! Great to see you! What's on your mind?",
    "Oh hi{name}! I was hoping you'd come by! What's up?",
    "Hey{name}! Good to see a friendly face! Where should we start?",
    "Hi there{name}! The house is all yours to explore. What sounds fun?",
    "Hey{name}! Welcome back!",
    "Hello{name}! Yay, you're here! What do you feel like doing?",
    "Hey{name}! How's it going? Need any help or just hanging out?",
    "Hi{name}! So glad you're here!",
    "Hey{name}! Welcome to the house!",
]

FOREST_GREETINGS: list[str] = [
    "Hey{name}! Welcome to the forest! Ready for an adventure?",
    "Hi{name}! You made it!",
    "Hi{name}! This forest is full of surprises! Go and find the animals!",
    "Oh hi{name}! The forest is waiting for us!",
    "Hey there{name}! So glad you're here! What do you want to do first?",
    "Hi there{name}! I've been waiting for an explorer buddy!",
    "Hello{name}! The forest looks amazing today!",
    "Hey{name}! Welcome, explorer! Shall we start our adventure?",
    "Hello{name}! So happy you're here!",
]

CONFIRMATION_WORDS: set[str] = {
    "yes", "ok", "okay", "sure", "ready", "yeah", "yep", "yea", "ya",
    "ok ask me", "ask me", "yes ask me", "yes please", "go ahead",
    "yes please ask me the question", "ok ask me the question",
    "i am ready", "im ready", "i'm ready", "go", "alright",
    "yes please ask me", "ask me the question",
}

NEXT_QUESTION_WORDS: set[str] = {
    "next", "next question", "move to next question", "another question",
    "next one", "give me another question", "new question", "one more",
    "ask me another", "ask another question", "more questions",
    "help me to find next animal", "find next animal", "next animal",
    "help me find the next animal", "another animal",
    "lets start question", "let's start question", "start question",
    "start the question", "start quiz", "lets start", "let's start",
    "begin question", "begin the question", "lets go", "let's go",
}

SKIP_WORDS: set[str] = {
    "skip", "pass", "i don't know", "i dont know", "idk", "no idea",
    "no clue", "i give up", "give up", "i quit", "can't figure it out",
    "cant figure it out", "i'm stuck", "im stuck", "i have no idea",
    "skip this", "pass this", "skip question", "i surrender",
    "i can't pronounce", "i cant pronounce", "can't pronounce it",
    "cant pronounce it", "i can't say it", "i cant say it",
    "too hard to pronounce", "too difficult to pronounce",
}

HINT_WORDS: set[str] = {
    "hint", "clue", "give me a hint", "give me a clue", "help me with this",
    "help me with the question", "i need a hint", "i need a clue",
    "can you help me", "explain this", "explain the question",
    "what does this mean", "help me answer",
}

REPEAT_WORDS: set[str] = {
    "repeat", "repeat the question", "say that again", "say it again",
    "what was the question", "what did you ask", "can you repeat",
    "again please", "tell me again", "i forgot the question",
    "repeat please", "one more time", "come again",
}

FILLER_WORDS: set[str] = {
    "lol", "haha", "hahaha", "lmao", "bruh", "bro", "hmm", "hmmm",
    "ok cool", "nice", "wow", "interesting", "oh", "ooh", "ahh",
    "k", "kk", "okay cool", "alright cool", "cool", "yay", "ohhh",
    "damn", "dang", "whoa", "omg", "oh my god", "oh wow",
    # Casual conversation
    "just hanging out", "just chilling", "nothing much", "not much",
    "just vibing", "just looking around", "just exploring",
    "just walking around", "nothing", "nah", "nope", "no thanks",
    "i'm good", "im good", "all good", "just passing by",
    "just talking", "just chatting", "chillin", "sup",
}

CASUAL_PHRASES: list[str] = [
    "just hanging", "just chilling", "just vibing", "just looking",
    "just exploring", "just walking", "just passing", "just talking",
    "just chatting", "nothing much", "not much", "no thanks",
    "hanging out", "chilling out", "messing around", "goofing around",
    "having fun", "just playing", "just roaming", "wandering around",
]

GREETING_WORDS: set[str] = {
    "hello", "hi", "hey", "hii", "helo", "greetings", "howdy", "sup",
    "yo", "yoo", "hola", "heyyy", "heyy", "wassup", "whatsup",
}

GREETING_PHRASES: set[str] = {
    "hello how are you", "hi how are you", "hey how are you",
    "hello there", "hi there", "hey there",
    "how are you", "how are you doing", "how do you do",
    "good morning", "good afternoon", "good evening", "good night",
    "hello sir", "hi sir", "hello mam", "hi mam",
    "hello friend", "hi friend", "hey friend",
    "hello buddy", "hi buddy", "hey buddy",
    "what's up", "whats up", "wats up", "sup bro",
    "hii how are you", "helo how are you",
    "hello hello", "hi hi", "hey hey",
    "hi what's up", "hey what's up", "hello what's up",
    "hi whats up", "hey whats up", "hello whats up",
    "hey yo", "hi hi hi", "hello everyone",
}

LEARNING_PHRASES: list[str] = [
    "ask me a question", "ask me some question", "ask me question",
    "ask me questions", "general question",
    "quiz me", "test me", "test my knowledge",
    "practice question", "i want to learn", "i want to practice",
]


# ---------------------------------------------------------------------------
# 2. DATACLASSES
# ---------------------------------------------------------------------------

@dataclass
class MessageClassification:
    """Result of classifying a player message into intent categories."""
    is_key_request: bool
    is_greeting: bool
    is_confirmation: bool
    is_next_question: bool
    is_skip: bool
    is_hint: bool
    is_repeat: bool
    is_filler: bool
    is_emoji_only: bool
    is_player_question: bool
    is_game_state: bool
    is_conversational: bool
    is_not_answer: bool  # computed: OR of all above


@dataclass
class GuardResult:
    """Outcome of running the guard checks on a message.

    If ``intercepted`` is True, ``reply`` contains the response to send
    immediately (no agent call needed).
    """
    intercepted: bool
    reply: str | None = None


@dataclass
class AnswerProcessingResult:
    """Outcome of server-side answer validation.

    ``answer_prefix`` is the tag string to prepend to the agent message.
    Other flags tell the caller what session state to update.
    """
    answer_prefix: str = ""
    key_earned: bool = False
    clear_active_question: bool = False
    quiz_state_update: dict[str, Any] | None = None
    clear_quiz_state: bool = False
    clear_quiz_mode: bool = False


# ---------------------------------------------------------------------------
# 3. classify_message
# ---------------------------------------------------------------------------

# Filler prefixes stripped before startswith checks
_FILLER_PREFIXES: list[str] = [
    "okay ", "ok ", "okk ", "okkk ", "so ", "well ", "hmm ", "umm ",
    "alright ", "yeah ", "yep ", "ya ", "hey ", "hi ",
]


def _strip_filler_prefix(msg_lower: str) -> str:
    """Strip a single leading filler prefix for question-detection heuristics."""
    for prefix in _FILLER_PREFIXES:
        if msg_lower.startswith(prefix):
            return msg_lower[len(prefix):].lstrip()
    return msg_lower


def classify_message(msg_lower: str, original_text: str) -> MessageClassification:
    """Classify a player message into all relevant intent categories.

    Parameters
    ----------
    msg_lower : str
        The player message, already ``.strip().lower()``-ed.
    original_text : str
        The original (un-lowered) message text.

    Returns
    -------
    MessageClassification
        A dataclass with boolean fields for every category.
    """

    # --- key request ---
    is_key_request = (
        any(w in msg_lower for w in KEY_REQUEST_WORDS)
        or bool(_KEY_WORD_RE.search(msg_lower))
        # Forest mode: "animal" requests are equivalent to "key" requests
        or any(w in msg_lower for w in ANIMAL_REQUEST_WORDS)
        or bool(_ANIMAL_WORD_RE.search(msg_lower))
    )

    # --- greeting ---
    is_greeting = msg_lower in GREETING_WORDS or msg_lower in GREETING_PHRASES

    # --- confirmation (exact match only) ---
    is_confirmation = msg_lower in CONFIRMATION_WORDS

    # --- next question ---
    is_next_question = msg_lower in NEXT_QUESTION_WORDS or any(
        w in msg_lower for w in [
            "next", "another", "more question", "new question",
            "start question", "begin question", "start quiz",
        ]
    )

    # --- skip / don't know ---
    is_skip = msg_lower in SKIP_WORDS or any(
        w in msg_lower for w in [
            "i don't know", "i dont know", "no idea", "give up",
            "i quit", "i surrender",
            "can't pronounce", "cant pronounce",
            "can't say it", "cant say it",
            "unable to pronounce", "not able to pronounce",
        ]
    )

    # --- hint ---
    is_hint = msg_lower in HINT_WORDS or any(
        w in msg_lower for w in ["hint", "clue", "help me with"]
    )

    # --- repeat ---
    is_repeat = msg_lower in REPEAT_WORDS or any(
        w in msg_lower for w in [
            "repeat", "say that again", "say it again",
            "what was the question", "one more time",
        ]
    )

    # --- filler ---
    is_filler = msg_lower in FILLER_WORDS or any(w in msg_lower for w in CASUAL_PHRASES)

    # --- emoji-only ---
    is_emoji_only = bool(msg_lower) and not any(c.isalnum() for c in msg_lower)

    # --- player question ---
    _stripped = _strip_filler_prefix(msg_lower)
    is_player_question = (
        "?" in original_text
        or any(_stripped.startswith(w) for w in [
            "why ", "how ", "what ", "when ", "where ", "who ",
            "can you", "can i", "could you", "tell me", "explain",
            "i don't", "i dont", "i want to know", "i have a question",
            "i want to ask", "please tell", "but ",
            "is there", "are there", "is it", "are you", "do you", "does ",
            "is the", "are the", "will you", "would you", "should i",
            "have you", "has the", "did you", "did the",
        ])
        or any(w in msg_lower for w in [
            "instead of", "how come", "why not", "what about",
            "i don't understand", "i dont understand", "not fair",
            "that's not", "thats not",
            "tell me about", "tell me more", "explain to me",
            "what is this", "what are you", "what do you",
            "about this", "about the ",
            "is there any", "are there any", "is there a ",
        ])
    )

    # --- game state ---
    is_game_state = any(w in msg_lower for w in [
        "i started", "i finished", "i completed", "i did",
        "daily task", "daily tasks", "task is done", "tasks are done",
        "i'm done", "im done", "i am done",
    ])

    # --- conversational ---
    is_conversational = (
        any(_stripped.startswith(w) for w in [
            "i am ", "i'm ", "im ", "i was ", "i like ", "i love ", "i hate ",
            "i want to ", "i wanna ", "i need to ", "i have to ", "i got ",
            "i think ", "i feel ", "i hope ", "i wish ", "i need ",
            "i just ", "i also ", "i already ", "i never ", "i always ",
            "i can ", "i can't ", "i cant ", "i could ", "i couldn't ",
            "i will ", "i won't ", "i wont ", "i would ", "i wouldn't ",
            "i should ", "i shouldn't ", "i shouldnt ",
            "just ", "let's ", "lets ", "let me ",
            "this is ", "that is ", "that's ", "thats ", "that was ",
            "this game ", "this place ", "this house ", "this home ",
            "you are ", "you're ", "youre ", "you look ", "you seem ",
            "you should ", "you can ", "you know ",
            "we should ", "we can ", "we could ", "let us ",
            "it's ", "its ", "it is ", "it was ",
            "show me ", "take me ", "bring me ", "give me ",
            "go to ", "go back", "come on", "come here",
            "please ", "thanks", "thank you", "thank ",
            "wow ", "nice ", "great ", "awesome ", "amazing ",
            "so cool", "so fun", "so nice", "so boring",
            "not bad", "not good", "no way", "no problem",
        ])
        or any(w in msg_lower for w in [
            "thank you", "thanks for", "no thanks", "good job",
            "well done", "nice one", "my name is", "my friend",
            "play with", "talk to", "hang out", "mess around",
            "bored", "having fun", "this is fun", "this is cool",
            "this is boring", "love this", "hate this",
            "want to play", "want to go", "want to see",
            "want to do", "want to talk", "want to know",
            "let's go", "lets go", "come with me",
            "good morning", "good afternoon", "good evening", "good night",
            "see you", "bye", "goodbye", "see ya",
        ])
    )

    # --- composite ---
    is_not_answer = (
        is_key_request or is_confirmation or is_next_question
        or is_skip or is_hint or is_repeat or is_filler
        or is_emoji_only or is_player_question or is_game_state
        or is_conversational
    )

    return MessageClassification(
        is_key_request=is_key_request,
        is_greeting=is_greeting,
        is_confirmation=is_confirmation,
        is_next_question=is_next_question,
        is_skip=is_skip,
        is_hint=is_hint,
        is_repeat=is_repeat,
        is_filler=is_filler,
        is_emoji_only=is_emoji_only,
        is_player_question=is_player_question,
        is_game_state=is_game_state,
        is_conversational=is_conversational,
        is_not_answer=is_not_answer,
    )


# ---------------------------------------------------------------------------
# 4. check_guards
# ---------------------------------------------------------------------------

def check_guards(
    cls: MessageClassification,
    session: SessionState,
    daily_task_active: bool,
    level: str,
    player_name: str = "",
) -> GuardResult:
    """Run ordered guard checks and return early if one fires.

    Parameters
    ----------
    cls : MessageClassification
        Pre-computed classification of the player message.
    session : SessionState
        The per-session state object.
    daily_task_active : bool
        Whether the daily task collider is currently active (from UE).
    level : str
        Current game level (``"home"`` or ``"foresthideandseek"``).
    player_name : str
        Optional player display name for personalised greetings.

    Returns
    -------
    GuardResult
        ``intercepted=True`` with a ``reply`` if a guard fired,
        ``intercepted=False`` otherwise.
    """

    # GUARD 0: greeting
    if cls.is_greeting:
        name_bit = f" {player_name}" if player_name else ""
        if level == "foresthideandseek":
            greeting = random.choice(FOREST_GREETINGS).format(name=name_bit)
        else:
            greeting = random.choice(HOME_GREETINGS).format(name=name_bit)
        return GuardResult(intercepted=True, reply=greeting)

    # GUARD 0.5: daily task completed + key request (home only)
    if level == "home" and session.daily_completed and cls.is_key_request:
        return GuardResult(
            intercepted=True,
            reply=(
                "You've already completed the daily task \u2014 "
                "there's no key in the home anymore. Great job!"
            ),
        )

    # GUARD 0.75: key already earned + key request
    # If the player already earned the key/animal this session, block any new quiz.
    # This fires regardless of daily_completed — once earned, no more quizzes.
    if session.key_earned and cls.is_key_request:
        if session.daily_completed:
            return GuardResult(
                intercepted=True,
                reply="You have already completed the daily task! Great job today!",
            )
        if level == "foresthideandseek":
            msg = random.choice(ANIMAL_REWARD_MESSAGES)
        else:
            msg = random.choice(KEY_REWARD_MESSAGES)
        return GuardResult(intercepted=True, reply=msg)

    # GUARD 1: key request while daily task not started (home only)
    if level == "home" and not daily_task_active and not session.daily_completed:
        if cls.is_key_request:
            return GuardResult(
                intercepted=True,
                reply=(
                    "The daily task hasn't started yet! "
                    "Start the daily task first, then I can help you find the key."
                ),
            )

    return GuardResult(intercepted=False)


# ---------------------------------------------------------------------------
# 5. Answer checking helpers
# ---------------------------------------------------------------------------

def _normalize_answer(s: str) -> str:
    """Strip units, punctuation, and common player prefixes from quiz answers."""
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
    # Normalise unit symbols
    s = s.replace("\u00b0c", "").replace("\u00b0f", "").replace("\u00b0", "")
    s = s.replace("degree celsius", "").replace("degree fahrenheit", "")
    s = s.replace("degrees", "").replace("degree", "")
    s = s.replace("percent", "").replace("%", "")
    # Strip trailing punctuation
    s = s.rstrip("!.,;:?")
    return s.strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    """Return similarity ratio (0.0-1.0) between two strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


_NEAR_MATCH_THRESHOLD: float = 0.65


def check_answer(answer_lower: str, correct_answer: str, options: list[str]) -> str:
    """Server-side answer check.

    Parameters
    ----------
    answer_lower : str
        Player's answer, already lowered and stripped.
    correct_answer : str
        The correct answer text from the question data.
    options : list[str]
        The list of option texts (A, B, C, D).

    Returns
    -------
    str
        ``"correct"``, ``"near_match"``, or ``"wrong"``.
    """
    correct = correct_answer.strip().lower()
    answer = answer_lower.strip()
    norm_answer = _normalize_answer(answer)
    norm_correct = _normalize_answer(correct)

    # --- EXACT match checks ---

    # Direct text match (original + normalized)
    if answer == correct or (norm_answer and norm_answer == norm_correct):
        return "correct"

    # Build letter -> option text map
    letter_map: dict[str, str] = {}
    for i, opt in enumerate(options):
        letter_map[chr(65 + i).lower()] = opt.strip().lower()

    # Player typed a letter: "A", "a", "option A", "A)", "a."
    for ans in [answer, norm_answer]:
        cleaned = ans.replace("option", "").replace(")", "").replace(".", "").strip()
        if len(cleaned) >= 1:
            first_char = cleaned[0]
            if first_char in letter_map and len(cleaned) <= 1 + len(letter_map.get(first_char, "")):
                if letter_map[first_char] == correct:
                    return "correct"

    # Player typed the full option text
    for opt_text in letter_map.values():
        if opt_text == correct:
            norm_opt = _normalize_answer(opt_text)
            if answer == opt_text or norm_answer == norm_opt:
                return "correct"

    # Partial match: correct answer is INSIDE player's answer
    if len(answer) >= 3 and correct in answer:
        return "correct"
    if len(norm_answer) >= 3 and norm_correct in norm_answer:
        return "correct"

    # Partial match: player's answer is INSIDE correct answer
    if len(answer) >= 3 and answer in correct:
        if len(correct) > 0 and len(answer) / len(correct) >= 0.75:
            return "correct"
        else:
            return "near_match"
    if len(norm_answer) >= 3 and norm_answer in norm_correct:
        if len(norm_correct) > 0 and len(norm_answer) / len(norm_correct) >= 0.75:
            return "correct"
        else:
            return "near_match"

    # --- NEAR MATCH (fuzzy) ---
    best_ratio = max(
        _fuzzy_ratio(norm_answer, norm_correct) if norm_answer else 0.0,
        _fuzzy_ratio(answer, correct),
    )

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


def looks_like_quiz_answer(msg: str, correct_answer: str, options: list[str]) -> bool:
    """Check if a message plausibly looks like a quiz answer attempt.

    Short messages (1-4 words) are always accepted.  Longer messages
    are only accepted if they contain the correct answer or an option text.
    """
    msg_lower = msg.strip().lower()
    words = msg_lower.split()
    word_count = len(words)

    # Single letter A-D
    cleaned = msg_lower.replace("option", "").replace(")", "").replace(".", "").replace(",", "").strip()
    if cleaned in ("a", "b", "c", "d"):
        return True

    # Very short (1-4 words)
    if word_count <= 4:
        return True

    # 5-6 words: could be an answer if it contains the correct answer or option text
    if word_count <= 6:
        correct_lower = correct_answer.strip().lower()
        if correct_lower and correct_lower in msg_lower:
            return True
        for opt in options:
            if opt.strip().lower() in msg_lower:
                return True
        # Allow answer-like prefixes
        answer_prefixes = [
            "the answer is", "i think it's", "i think its", "i think it is",
            "it is ", "it's ", "its ", "my answer is", "i say ", "i guess ",
            "option ", "i choose ", "i pick ",
        ]
        if any(msg_lower.startswith(p) for p in answer_prefixes):
            return True

    # 7+ words: only if contains actual answer text or option
    if word_count >= 7:
        correct_lower = correct_answer.strip().lower()
        if correct_lower and correct_lower in msg_lower:
            return True
        for opt in options:
            if opt.strip().lower() in msg_lower:
                return True
        return False

    # Default for 5-6 words without match
    return True


# ---------------------------------------------------------------------------
# 6. process_answer
# ---------------------------------------------------------------------------

def process_answer(
    original_text: str,
    msg_lower: str,
    cls: MessageClassification,
    session: SessionState,
    laq: dict[str, Any],
    level: str,
) -> AnswerProcessingResult:
    """Run the full server-side answer processing state machine.

    Parameters
    ----------
    original_text : str
        The player's original (un-lowered) message.
    msg_lower : str
        Lowered + stripped message.
    cls : MessageClassification
        Pre-computed classification.
    session : SessionState
        Per-session state object.
    laq : dict
        The active question dict (``active``, ``delivered``, ``correct_answer``, ``options``).
    level : str
        ``"home"`` or ``"foresthideandseek"``.

    Returns
    -------
    AnswerProcessingResult
        Contains the answer_prefix tag and flags for session state updates.
    """
    result = AnswerProcessingResult()

    if not laq.get("active") or not laq.get("delivered"):
        return result

    quiz_mode = session.quiz_mode or "learning"
    mode_tag = f" MODE: {quiz_mode.upper()}."

    correct_answer = laq.get("correct_answer", "")
    options = laq.get("options", [])

    # --- Handle skip / "I don't know" ---
    if cls.is_skip:
        qs = session.get_quiz_state()

        if qs.get("phase") in ("pronunciation", "teaching"):
            # Player can't pronounce and wants to move on
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                f"SKIP_PRONUNCIATION \u2014 the player cannot pronounce the answer and wants to move on.{mode_tag} "
                f"Say something encouraging like 'No worries! Let\\'s try a different question.' "
                f"Then call fetch_questions to get a NEW question for them.] "
            )
            result.clear_active_question = True
            result.clear_quiz_state = True
        else:
            # Player doesn't know the answer -- teach it
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                f"DONT_KNOW \u2014 the player does not know the answer.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Teach the player the correct answer, then ask them to say it back to you. "
                f"Do NOT give the reward yet \u2014 wait for them to pronounce it correctly.] "
            )
            result.quiz_state_update = {"attempt": qs.get("attempt", 1), "phase": "teaching"}

        return result

    # --- Non-answer messages during active quiz ---
    if cls.is_not_answer:
        skip_type = (
            "player_question" if cls.is_player_question else
            "confirmation" if cls.is_confirmation else
            "other"
        )
        result.answer_prefix = (
            f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
            f"NOT_AN_ANSWER \u2014 this is a {skip_type}, not a quiz answer. "
            f"Do NOT check their answer. Do NOT say 'not quite' or 'wrong'. "
            f"Respond to their message naturally, then gently remind them about the current quiz question.] "
        )
        return result

    # --- Actual answer attempt ---
    # Gatekeeper: does the message look like a quiz answer?
    if not looks_like_quiz_answer(msg_lower, correct_answer, options):
        result.answer_prefix = (
            f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
            f"NOT_AN_ANSWER \u2014 this is a conversational message, not a quiz answer. "
            f"Do NOT check their answer. Do NOT say 'not quite' or 'wrong'. "
            f"Respond to their message naturally, then gently remind them about the current quiz question.] "
        )
        return result

    # Gatekeeper passed -- run the actual answer check
    answer_result = check_answer(msg_lower, correct_answer, options)
    qs = session.get_quiz_state()
    level_tag = f" LEVEL: {level}." if level else ""

    if answer_result == "correct":
        if qs["phase"] in ("pronunciation", "teaching"):
            if quiz_mode == "key":
                result.answer_prefix = (
                    f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                    f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{mode_tag}{level_tag} "
                    f"You MUST reply with a friendly varied message ending with ||SHOW_KEY (home) or ||SHOW_ANIMAL (forest). "
                    f"Follow the REWARD FORMAT RULES. Do NOT say anything after the action tag.] "
                )
            else:
                result.answer_prefix = (
                    f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                    f"PRONUNCIATION_CORRECT! The player pronounced the answer correctly.{mode_tag} "
                    f"Congratulate them warmly and ask if they want another question.] "
                )
        else:
            if quiz_mode == "key":
                result.answer_prefix = (
                    f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                    f"The answer is CORRECT.{mode_tag}{level_tag} "
                    f"You MUST reply with a friendly varied message ending with ||SHOW_KEY (home) or ||SHOW_ANIMAL (forest). "
                    f"Follow the REWARD FORMAT RULES. Do NOT say anything after the action tag.] "
                )
            else:
                result.answer_prefix = (
                    f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                    f"The answer is CORRECT.{mode_tag} "
                    f"Congratulate them warmly and ask if they want another question.] "
                )

        if quiz_mode == "key":
            result.key_earned = True
            result.clear_quiz_mode = True
        result.clear_active_question = True
        result.clear_quiz_state = True

    elif answer_result == "near_match":
        if qs["phase"] == "teaching":
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                f"PRONUNCIATION_CLOSE \u2014 almost correct but not exact.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Ask the player to try pronouncing it one more time.] "
            )
            # Keep phase as teaching
        else:
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                f"NEAR_MATCH \u2014 the answer is very close but has a pronunciation/spelling error.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Encourage the player to say it correctly. Do NOT give the reward yet.] "
            )
            result.quiz_state_update = {"attempt": qs.get("attempt", 1), "phase": "pronunciation"}

    else:
        # WRONG
        if qs["phase"] in ("pronunciation", "teaching"):
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player said "{original_text}". '
                f"PRONUNCIATION_WRONG \u2014 not correct.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Teach the answer again and ask the player to say it.] "
            )
            result.quiz_state_update = {"attempt": qs.get("attempt", 1), "phase": "teaching"}
        elif qs.get("attempt", 1) == 1:
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                f"WRONG_FIRST \u2014 this is their first attempt.{mode_tag} "
                f"Encourage them to try one more time. Tell them if they get it wrong again, "
                f"you will teach them the answer.] "
            )
            result.quiz_state_update = {"attempt": 2, "phase": "answering"}
        else:
            result.answer_prefix = (
                f'[QUIZ_ANSWER_RESULT: The player answered "{original_text}". '
                f"WRONG_FINAL \u2014 second wrong attempt.{mode_tag} "
                f'The correct answer is "{correct_answer}". '
                f"Teach the player the correct answer, then ask them to say it back to you. "
                f"Do NOT give the reward yet \u2014 wait for them to pronounce it correctly.] "
            )
            result.quiz_state_update = {"attempt": qs.get("attempt", 2), "phase": "teaching"}

    return result


# ---------------------------------------------------------------------------
# 7. detect_learning_request
# ---------------------------------------------------------------------------

def detect_learning_request(
    msg_lower: str,
    is_key_request: bool,
    quiz_active: bool,
) -> bool:
    """Return True if the message is a learning-mode quiz request.

    Learning requests are messages like "ask me a question" or "quiz me"
    that are NOT key/animal requests and happen when no quiz is active.
    """
    if is_key_request:
        return False
    if quiz_active:
        return False
    return (
        any(phrase in msg_lower for phrase in LEARNING_PHRASES)
        or ("ask me" in msg_lower and "question" in msg_lower)
        or ("ask" in msg_lower and "question" in msg_lower)
    )


# ---------------------------------------------------------------------------
# 8. build_enriched_message
# ---------------------------------------------------------------------------

def build_enriched_message(
    original_text: str,
    answer_prefix: str,
    history_tag: str,
    cls: MessageClassification,
    daily_task_active: bool,
    level: str,
    player_name: str,
    player_score: str,
    laq: dict[str, Any],
    is_learning: bool,
) -> str:
    """Build the fully enriched message with all context tags prepended.

    Parameters
    ----------
    original_text : str
        The player's original message.
    answer_prefix : str
        The ``[QUIZ_ANSWER_RESULT: ...]`` tag, or empty string.
    history_tag : str
        The ``[CHAT_HISTORY: ...]`` tag, or empty string.
    cls : MessageClassification
        Pre-computed classification.
    daily_task_active : bool
        Whether the daily task collider is active.
    level : str
        Current game level.
    player_name : str
        Display name of the player (may be empty).
    player_score : str
        Current player score string (may be empty).
    laq : dict
        Active question dict.
    is_learning : bool
        Whether this is a learning-mode request.

    Returns
    -------
    str
        The enriched message string ready to inject into the ADK request.
    """
    # Task tag (only for key requests)
    task_tag = ""
    if cls.is_key_request:
        task_tag = "[DAILY_TASK: ACTIVE] " if daily_task_active else "[DAILY_TASK: NOT_STARTED] "

    level_tag = f"[CURRENT_LEVEL: {level}]"
    name_tag = f" [PLAYER_NAME: {player_name}]" if player_name else ""
    score_tag = f" [PLAYER_SCORE: {player_score}]" if player_score else ""
    learning_tag = " [QUIZ_MODE: LEARNING]" if is_learning else ""

    # Quiz status tag
    quiz_active_now = laq.get("active", False) and laq.get("delivered", False)
    if answer_prefix:
        quiz_status_tag = ""
    elif quiz_active_now:
        quiz_status_tag = " [QUIZ_ACTIVE: YES \u2014 a question has been asked, waiting for the player's answer]"
    else:
        quiz_status_tag = " [NO_QUIZ_ACTIVE \u2014 this is normal conversation, do NOT treat it as a quiz answer]"

    return (
        f"{history_tag}{answer_prefix}{task_tag}{level_tag}"
        f"{name_tag}{score_tag}{learning_tag}{quiz_status_tag} {original_text}"
    )


# ---------------------------------------------------------------------------
# 9. Response processing
# ---------------------------------------------------------------------------

# Pre-compiled patterns for stripping echoed tags from agent replies
_TAG_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\[QUIZ_ANSWER_RESULT:.*?\]\s*"),
    re.compile(r"\[CURRENT_LEVEL:.*?\]\s*"),
    re.compile(r"\[DAILY_TASK:.*?\]\s*"),
    re.compile(r"\[PLAYER_NAME:.*?\]\s*"),
    re.compile(r"\[PLAYER_SCORE:.*?\]\s*"),
    re.compile(r"\[QUIZ_MODE:.*?\]\s*"),
    re.compile(r"\[CHAT_HISTORY:.*?\]\s*"),
    re.compile(r"\[QUIZ_ACTIVE:.*?\]\s*"),
    re.compile(r"\[NO_QUIZ_ACTIVE[^\]]*\]\s*"),
]


def clean_reply(text: str) -> str:
    """Strip all echoed context tags from an agent reply."""
    for pattern in _TAG_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


def enforce_daily_task_guard(
    reply: str,
    level: str,
    daily_active: bool,
    quiz_mode: str | None,
) -> str:
    """Strip reward action tags if the daily task guard should block them.

    In HOME level, if the daily task is not active and quiz_mode is "key",
    strip ``||SHOW_KEY`` from the reply.  In FOREST level, strip
    ``||SHOW_ANIMAL`` similarly if applicable.
    """
    if level == "home" and not daily_active and KEY_PHRASE_MW in reply:
        cleaned = reply.replace(KEY_PHRASE_MW, "").strip()
        return (
            "The daily task has not started yet. "
            "Start the daily task first, then I can help you find the key!"
        )
    # Forest does not have the same daily_active guard, but keep the pattern available
    return reply


def extract_question_from_events(events: list[dict[str, Any]]) -> tuple[str, str]:
    """Scan ADK events for a question in a functionResponse.

    Returns
    -------
    tuple[str, str]
        ``(question_text_from_fr, question_core)`` where
        ``question_text_from_fr`` is the formatted question + options string,
        and ``question_core`` is just the question text.
        Both are empty strings if no question was found.
    """
    for event in events:
        content = event.get("content") or {}
        for part in content.get("parts", []):
            fr = part.get("functionResponse", {})
            resp_data = fr.get("response", {})
            if "question" in resp_data:
                question_core = resp_data["question"]
                options = resp_data.get("options", [])
                opts_str = "  ".join(
                    f"{chr(65 + i)}) {opt}" for i, opt in enumerate(options)
                )
                question_text_from_fr = f"Question: {question_core}  Options: {opts_str}"
                return question_text_from_fr, question_core
    return "", ""


def detect_question_in_text(text: str) -> bool:
    """Return True if *text* contains quiz option patterns (a)/b) or option a/option b)."""
    lower = text.lower()
    return (
        ("a)" in lower and "b)" in lower)
        or ("a. " in lower and "b. " in lower)
        or ("option a" in lower and "option b" in lower)
    )


async def fetch_question_directly(session_id: str = "") -> str | None:
    """Call Firebase directly to get a question when the agent fails to.

    This is the fallback path: if the Gemini agent returns empty for a
    confirmation / "next question" message, we fetch one ourselves.
    """
    from Home_Agent.tools.question_api import (
        QUESTION_API_URL,
        QUESTION_API_METHOD,
        _get_request_body_from_env,
        _parse_content,
        LAST_ACTIVE_QUESTION,
        LAST_ACTIVE_QUESTIONS,
    )

    body = _get_request_body_from_env()
    if "std" not in body:
        body["std"] = 8

    try:
        if QUESTION_API_METHOD == "GET":
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

        chosen = random.choice(questions)

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
