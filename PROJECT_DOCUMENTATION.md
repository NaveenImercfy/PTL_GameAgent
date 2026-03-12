# Home Agent — Complete Project Documentation

> End-to-end guide for developers and future Claude sessions to fully understand, maintain, and extend this project. Covers architecture, all code, middleware guards, agent instructions, bug fixes, and testing.

---

## 1. Project Overview

| Field | Value |
|-------|-------|
| **Name** | Home Agent (Google ADK Game AI) |
| **Location** | `F:\Home_Agent_code\Home_Agent\` |
| **Language** | Python 3.11+ |
| **Framework** | Google ADK (Agent Development Kit) |
| **LLM Model** | `gemini-2.5-flash` |
| **Server** | Uvicorn on `http://127.0.0.1:8000` |
| **Run Command** | `cd F:\Home_Agent_code\Home_Agent && python run_combined.py` |
| **Platform** | Windows 10 Pro, shell: bash (Unix syntax) |

### What This Is

A Google ADK agent for an **Unreal Engine** game. It is NOT a quiz bot — it is a **game character** that lives in a home map (or forest map). The agent helps the player with tasks and uses quiz questions as a game mechanic to guide players to hidden keys/animals.

### Two Game Modes

| Mode | Level String | Character | Goal |
|------|-------------|-----------|------|
| **Home** | `"home"` | Home Assistant AI | Cooking, gardening, cleaning, pool. Guide player to hidden **key** via quiz. |
| **Forest** | `"foresthideandseek"` | Forest Explorer AI | Explore forest. Guide player to hidden **animals** via quiz. |

### Learning Mode

When the daily task is not active and the player asks to practice (e.g., "ask me some question", "quiz me"), the agent enters **learning mode**. In learning mode:
- No key/animal reward is given
- No daily task check is needed
- Correct answers get congratulations + "Ready for the next question?"
- Tracked via `SESSION_QUIZ_MODE[session_id] = "learning"`

---

## 2. File Structure

```
F:\Home_Agent_code\Home_Agent\
├── run_combined.py                 # Main server (~1526 lines) — middleware, endpoints, all guards
├── question_server.py              # Questions proxy router (for Postman testing)
├── requirements.txt                # Dependencies
├── guardrail_questions.md          # QA reference for testing guardrails
├── start_web.bat / start_web.ps1   # Launchers
├── PROJECT_DOCUMENTATION.md        # This file
├── .claude/
│   └── launch.json                 # Claude Preview config
└── Home_Agent/
    ├── agent.py                    # Agent definition + full instruction (~502 lines)
    ├── .env                        # API keys, Firebase URL, default grade
    ├── __init__.py
    └── tools/
        ├── __init__.py             # Exports: fetch_questions, get_level, get_user_std, set_user_std, check_answer, get_daily_task_status
        └── question_api.py         # All tool implementations (~332 lines)
```

### Test Files
```
F:\Home_Agent_code\
└── tasks/
    └── test_scenarios.py           # Comprehensive 8-scenario test suite (~596 lines)
```

---

## 3. Environment Variables (`Home_Agent/.env`)

| Variable | Value | Purpose |
|----------|-------|---------|
| `GOOGLE_API_KEY` | `AIzaSy...oxg0` | Gemini 2.5 Flash API key |
| `QUESTION_API_STD` | `8` | Default grade for quiz questions |
| `QUESTIONS_SOURCE_API_URL` | `https://question-751927247815.us-east4.run.app/` | Firebase questions API |
| `QUESTIONS_SOURCE_API_METHOD` | `GET` | HTTP method for Firebase API |

---

## 4. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                      Unreal Engine (Blueprints)                  │
│  Sends 10 fields via Make Json: session_id, message,             │
│  daily_task_active, daily_task_completed, level, player_name,    │
│  player_score, std, streaming, app_name                          │
└────────────┬─────────────────────────┬───────────────────────────┘
             │ POST /session/create    │ POST /run (via UE Make Json)
             ▼                         ▼
┌──────────────────────────────────────────────────────────────────┐
│                  run_combined.py (Uvicorn :8000)                  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │          DailyTaskRunMiddleware (ASGI)                      │  │
│  │  Intercepts POST /run requests from UE Blueprint            │  │
│  │  Extracts: daily_task_active, daily_task_completed,         │  │
│  │            level, player_name, player_score                 │  │
│  │  Runs 6 GUARD checks → may return early                    │  │
│  │  Server-side answer validation (GUARD 2)                    │  │
│  │  Quiz state save/restore for NOT_AN_ANSWER protection       │  │
│  │  Enriches messages with context tags                        │  │
│  │  Post-processes: strip tags, inject questions, fallback     │  │
│  └─────────────────────────┬──────────────────────────────────┘  │
│                            │ (if all guards pass)                 │
│                            ▼                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │          Google ADK (FastAPI)                               │  │
│  │  Routes: /run, /run_sse, /apps/*, / (Web UI)               │  │
│  │  Manages agent sessions and state                          │  │
│  └─────────────────────────┬──────────────────────────────────┘  │
│                            │                                     │
│                            ▼                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │          Agent (agent.py → gemini-2.5-flash)               │  │
│  │  Tools: fetch_questions, get_level, get_user_std,          │  │
│  │         set_user_std, get_daily_task_status                │  │
│  │  (check_answer exported but NOT in agent's tool list)      │  │
│  └─────────────────────────┬──────────────────────────────────┘  │
│                            │ fetch_questions() calls Firebase    │
│                            ▼                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │          Firebase Questions API (external)                  │  │
│  │  GET https://question-...run.app/?std=8                    │  │
│  │  Returns: question{option}A--B--C--D{ans}answer{next}...  │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Additional endpoints:                                           │
│  ├── /session/create (UE session creation)                       │
│  ├── /chat (UE chat endpoint — full classification + answer      │
│  │         checking, mirrors middleware logic)                    │
│  ├── /conversation/start (ADK Web UI session creation)           │
│  └── /questions (proxy for Postman testing)                      │
└──────────────────────────────────────────────────────────────────┘
```

### Two Paths Into the Agent

| Path | Source | Endpoint | Logic | Response Format |
|------|--------|----------|-------|-----------------|
| **UE Blueprint (primary)** | Unreal Engine | POST `/run` | DailyTaskRunMiddleware intercepts | ADK JSON events |
| **UE /chat** | UE (alternate flow) | POST `/chat` | Inline classification + answer checking (mirrors middleware) | PlainTextResponse |

**Important**: Both paths now have full message classification, answer validation state machine, DONT_KNOW handling, NOT_AN_ANSWER protection, and quiz state save/restore. The middleware path (`/run`) is the primary and most robust path, but `/chat` has been brought to feature parity.

---

## 5. API Endpoints

### 5.1 Unreal Engine Endpoints

#### `POST /session/create`

Creates a new game session. Returns plain text session ID.

**Request:**
```json
{
  "std": 8,
  "level": "home"
}
```
Also accepts `"user:std"` and `"user:level"` key formats.

**Response:** Plain text session ID string (e.g., `"abc-123-xyz"`)

**What happens internally:**
1. Creates ADK session with `user:std` and `user:level` in state
2. Stores level in `SESSION_LEVELS[session_id]`
3. Sends a `SYSTEM SETUP: The current game level is '{level}'` message to the agent
4. Agent responds with a greeting in the correct character

#### `POST /run` (via UE Blueprint Make Json)

The primary game endpoint. UE Blueprint sends a Make Json node with up to 10 fields directly to ADK's `/run`. The `DailyTaskRunMiddleware` intercepts this.

**Request body (from UE):**
```json
{
  "app_name": "Home_Agent",
  "user_id": "user",
  "session_id": "abc-123",
  "new_message": {"parts": [{"text": "where is the key"}]},
  "streaming": false,
  "daily_task_active": true,
  "daily_task_completed": false,
  "level": "home",
  "player_name": "Arjun",
  "player_score": "50"
}
```

**Response:** ADK JSON events array:
```json
[
  {
    "author": "root_agent",
    "content": {
      "parts": [{"text": "Agent reply here"}],
      "role": "model"
    }
  }
]
```

UE extracts text from `events[last].content.parts[last].text`.

**Navigate to key/animal detection**: UE checks if the reply contains `||SHOW_KEY` or `||SHOW_ANIMAL` to trigger navigation. The message before the tag varies each time for natural conversation.

#### `POST /chat`

Alternate UE endpoint with full classification + answer checking. Returns PlainTextResponse.

**Request:**
```json
{
  "session_id": "abc-123",
  "message": "where is the key",
  "daily_task_active": true
}
```

**Response:** Plain text reply string.

**Features (mirrors middleware):**
- Full 9-category message classification
- Server-side answer validation with state machine (CORRECT, NEAR_MATCH, WRONG_FIRST, WRONG_FINAL, PRONUNCIATION_CORRECT, PRONUNCIATION_CLOSE, PRONUNCIATION_WRONG)
- DONT_KNOW handling
- NOT_AN_ANSWER tag injection + quiz state save/restore protection
- Learning mode detection and QUIZ_MODE tagging
- Question injection from functionResponse
- Empty-response fallback (Firebase direct fetch)
- Tag stripping and daily task enforcement

### 5.2 Other Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/conversation/start` | POST | Create session with grade (for ADK Web UI) |
| `/questions` | POST | Proxy to Firebase (for Postman testing) |
| `/questions/health` | GET | Health check |
| `/run` | POST | ADK native non-streaming |
| `/run_sse` | POST | ADK native streaming |
| `/` | GET | ADK Web UI |
| `/apps/*` | Various | ADK session management |

---

## 6. Middleware — `DailyTaskRunMiddleware`

The middleware is the brain of the server-side logic. It intercepts POST `/run` requests that contain `daily_task_active` (indicating they come from UE Blueprint, not internal ADK calls).

### 6.1 Request Flow

```
UE POST /run
  │
  ├─ Parse JSON body
  ├─ Extract: daily_task_active, daily_task_completed, level, player_name, player_score
  ├─ If daily_task_active NOT in body → pass through (internal call)
  ├─ Skip "SYSTEM SETUP:" messages
  │
  ├─ GUARD: Empty message → "I didn't catch that"
  ├─ GUARD 0: Greeting → character-appropriate greeting
  ├─ GUARD 0.5: Daily completed + key request → "already completed"
  ├─ GUARD 0.75: Key earned + key request → repeat "Follow me"
  ├─ GUARD 1: Home + not active + not completed + key request → "task not started"
  ├─ GUARD 2: Active quiz + answer attempt → state machine
  │     ├─ DONT_KNOW → teach answer, ask pronunciation
  │     ├─ NOT_AN_ANSWER → tag with QUIZ_ANSWER_RESULT: NOT_AN_ANSWER
  │     └─ Actual answer → check via _check_answer_locally_mw()
  │
  ├─ Classify message (9 categories)
  ├─ Enrich with tags: [DAILY_TASK], [CURRENT_LEVEL], [PLAYER_NAME], [PLAYER_SCORE], [QUIZ_ANSWER_RESULT], [QUIZ_MODE]
  │
  ├─ SAVE quiz state if NOT_AN_ANSWER (protection against agent misbehavior)
  ├─ Forward to ADK agent
  ├─ RESTORE quiz state if saved (undoes fetch_questions() side effects)
  │
  ├─ POST-PROCESS RESPONSE:
  │   ├─ Strip all context tags from agent reply
  │   ├─ Enforce: block "Follow me" if daily task not active (Home mode)
  │   ├─ Inject question text if agent omitted it (from functionResponse)
  │   ├─ Mark question as delivered (skip if NOT_AN_ANSWER was restored)
  │   ├─ FALLBACK 1: If empty + confirmation/next → fetch question from Firebase directly
  │   └─ FALLBACK 2: If still empty → generic "I'm not sure" message
  │
  └─ Return cleaned ADK JSON events
```

### 6.2 Guard System (in order)

#### Empty Message Guard
- **Condition**: `not msg_lower` (empty or whitespace-only message)
- **Response**: `"I didn't catch that -- could you say something?"`

#### GUARD 0 — Greeting
- **Condition**: `msg_lower in GREETING_WORDS_MW` (exact match only)
- **Words**: hello, hi, hey, hii, helo, greetings, howdy, sup, yo, yoo, hola, heyyy, heyy, wassup, whatsup
- **Response**: Character greeting with player name if available
  - Home: `"Hey {name}! I'm your Home Assistant -- great to see you! How can I help you today?"`
  - Forest: `"Hey {name}! I'm the Forest Explorer AI -- I've been exploring around here and this forest is amazing! What's up?"`

#### GUARD 0.5 — Daily Task Completed
- **Condition**: `level == "home" AND daily_completed AND is_key_request`
- **Response**: `"You've already completed the daily task -- there's no key in the home anymore. Great job!"`
- **Note**: `daily_completed` is persisted via `SESSION_DAILY_COMPLETED` — once True, always True for that session

#### GUARD 0.75 — Key Already Earned
- **Condition**: `NOT daily_completed AND SESSION_KEY_EARNED[session_id] AND is_key_request`
- **Response**: Random varied message from `KEY_REWARD_MESSAGES` or `ANIMAL_REWARD_MESSAGES` (e.g., `"Follow me, I'll show you where the key is!||SHOW_KEY"`)

#### GUARD 1 — Daily Task Not Started
- **Condition**: `level == "home" AND NOT daily_active AND NOT daily_completed AND is_key_request`
- **Response**: `"The daily task hasn't started yet! Start the daily task first, then I can help you find the key."`

#### GUARD 2 — Server-Side Answer Check (State Machine)
- **Condition**: Active quiz (`_LAQ.active AND _LAQ.delivered`) AND message is NOT classified as `is_not_answer`
- **Includes three sub-paths:**
  1. **DONT_KNOW** (`is_skip`): Teach the answer, set phase to "teaching"
  2. **NOT_AN_ANSWER** (`is_not_answer` and not `is_skip`): Inject `[QUIZ_ANSWER_RESULT: NOT_AN_ANSWER]` tag
  3. **Answer check**: Call `_check_answer_locally_mw()` → state machine

### 6.3 Key Request Detection

Uses a two-tier approach to avoid false matches on words like "monkey", "turkey", "donkey":

```python
KEY_REQUEST_WORDS_MW = [
    "find the key", "where is the key", "where's the key",
    "help me find the key", "show me the key", "help me key",
    "i need the key", "give me the key", "find key",
]
_KEY_WORD_RE = re.compile(r'\bkey\b')  # Word-boundary match

def _is_key_request_check(text):
    if any(w in text for w in KEY_REQUEST_WORDS_MW):
        return True
    return bool(_KEY_WORD_RE.search(text))  # "key" as standalone word only
```

---

## 7. Message Classification System (9 Categories)

The middleware (and `/chat` endpoint) classifies every message into categories to decide whether it's a quiz answer attempt or something else. Only unclassified messages go through answer checking.

| Category | Variable | Match Type | Examples |
|----------|----------|------------|---------|
| **Key request** | `is_key_request` | Phrase + word-boundary | "where is the key", "help me find the key" |
| **Confirmation** | `is_confirmation` | Exact match only | "yes", "ok", "sure", "ready", "ask me" |
| **Next question** | `is_next_question` | Exact + substring | "next", "next question", "start quiz", "lets go" |
| **Skip/Pass** | `is_skip` | Exact + substring | "skip", "idk", "i give up", "no idea" |
| **Hint request** | `is_hint` | Exact + substring | "hint", "clue", "help me with this" |
| **Repeat request** | `is_repeat` | Exact + substring | "repeat", "say that again", "what was the question" |
| **Filler** | `is_filler` | Exact match only | "lol", "haha", "bruh", "nice", "cool", "wow" |
| **Emoji only** | `is_emoji_only` | No alphanumeric chars | Any emoji-only message |
| **Player question** | `is_player_question` | Multiple heuristics | "?", "why ...", "how ...", "tell me about ...", "what do you..." |

**Combined exclusion:**
```python
is_not_answer = (is_key_request or is_confirmation or is_next_question
                 or is_skip or is_hint or is_repeat or is_filler
                 or is_emoji_only or is_player_question)
```

**Important**: `is_skip` is handled SEPARATELY from `is_not_answer` for DONT_KNOW logic. The `is_not_answer_chat` variable in `/chat` endpoint excludes `is_skip` to allow separate DONT_KNOW handling:
```python
is_not_answer_chat = (is_key_request_chat or is_confirmation_chat or is_next_question_chat
                      or is_hint_chat or is_repeat_chat or is_filler_chat
                      or is_emoji_only_chat or is_player_question_chat)
# Note: is_skip_chat is NOT included — it has its own DONT_KNOW handling
```

### Player Question Detection (detailed)

The `is_player_question` check uses multiple heuristics to catch casual/conversational messages:

```python
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
        # Catch conversational messages with prefixes (e.g., "okay tell me about...")
        "tell me about", "tell me more", "explain to me",
        "what is this", "what are you", "what do you",
        "about this", "about the ",
    ])
)
```

### Word Sets (exact contents)

#### CONFIRMATION_WORDS (exact match)
```
yes, ok, okay, sure, ready, yeah, yep, yea, ya, ok ask me, ask me, yes ask me,
yes please, go ahead, yes please ask me the question, ok ask me the question,
i am ready, im ready, i'm ready, go, alright, yes please ask me, ask me the question
```

#### NEXT_QUESTION_WORDS (exact + substring fallback)
```
next, next question, move to next question, another question, next one,
give me another question, new question, one more, ask me another,
ask another question, more questions, help me to find next animal,
find next animal, next animal, help me find the next animal, another animal,
lets start question, let's start question, start question, start the question,
start quiz, lets start, let's start, begin question, begin the question,
lets go, let's go
```

#### SKIP_WORDS
```
skip, pass, i don't know, i dont know, idk, no idea, no clue, i give up,
give up, i quit, can't figure it out, cant figure it out, i'm stuck, im stuck,
i have no idea, skip this, pass this, skip question, i surrender
```

#### HINT_WORDS
```
hint, clue, give me a hint, give me a clue, help me with this,
help me with the question, i need a hint, i need a clue, can you help me,
explain this, explain the question, what does this mean, help me answer
```

#### REPEAT_WORDS
```
repeat, repeat the question, say that again, say it again,
what was the question, what did you ask, can you repeat, again please,
tell me again, i forgot the question, repeat please, one more time, come again
```

#### FILLER_WORDS
```
lol, haha, hahaha, lmao, bruh, bro, hmm, hmmm, ok cool, nice, wow,
interesting, oh, ooh, ahh, k, kk, okay cool, alright cool, cool, yay,
ohhh, damn, dang, whoa, omg, oh my god, oh wow
```

#### GREETING_WORDS_MW
```
hello, hi, hey, hii, helo, greetings, howdy, sup, yo, yoo, hola,
heyyy, heyy, wassup, whatsup
```

#### LEARNING_PHRASES (both middleware and /chat)
```
ask me a question, ask me some question, ask me question,
ask me questions, general question,
quiz me, test me, test my knowledge,
practice question, i want to learn, i want to practice
```

---

## 8. Answer Checking System

### 8.1 Normalization (`_normalize_answer`)

Before comparing answers, both the player's answer and the correct answer are normalized:

1. **Strip prefixes**: "the answer is ", "answer is ", "it is ", "it's ", "its ", "i think ", "i think it's ", "i think its ", "i believe ", "my answer is ", "that is ", "that's ", "thats "
2. **Strip units**: degree celsius, degree fahrenheit, degrees, degree, percent, %, and the symbols itself
3. **Strip trailing punctuation**: `!.,;:?`
4. **Lowercase and trim**

Example: `"I think 100 degree celsius!"` -> `"100"`

### 8.2 Match Levels (`_check_answer_locally_mw`)

The answer checker returns a **tri-state**: `"correct"`, `"near_match"`, or `"wrong"`.

**Exact match checks (returns "correct"):**
1. **Direct text match**: `answer == correct` or `normalized_answer == normalized_correct`
2. **Letter match**: Player typed "A", "B", "option C", etc. Checks both raw and normalized answer against letter map.
3. **Option text match**: Player typed the full text of an option that is the correct answer.
4. **Partial match** (>=3 chars): `correct in answer` or `answer in correct`. If answer is >=75% length of correct, returns "correct"; otherwise returns "near_match". Also checked with normalized versions.

**Fuzzy match check (returns "near_match"):**
5. **Fuzzy similarity** via `difflib.SequenceMatcher`: If none of the exact checks pass, computes similarity ratio against the correct answer and correct option texts. If best ratio >= 0.65 (65%), returns `"near_match"`. This handles speech-to-text typos like "Mughal Empier" -> "Mughal Emperor".

**Otherwise returns "wrong".**

### 8.3 Why `>= 3` Not `> 3`

Three-character answers like "100" need to match. The threshold was changed from `> 3` to `>= 3` to support this.

### 8.4 Quiz Answer State Machine

The answer flow uses a per-session state machine (`SESSION_QUIZ_STATE`) to handle multiple scenarios:

**Scenario 1 -- Correct on first attempt:** Direct reward ("Follow me...").

**Scenario 2 -- Near match (speech-to-text typo):** Agent encourages correct pronunciation. Player retries until they say it correctly, then gets the reward.

**Scenario 3 -- Wrong answer:** First attempt gets encouragement + warning. Second wrong attempt triggers teaching (agent reveals the answer and asks player to repeat it). Player must pronounce it correctly to get the reward.

**Scenario 4 -- DONT_KNOW ("I don't know" / "skip"):** Agent teaches the correct answer immediately and asks player to pronounce it back. Player must say it correctly to get the reward.

```
Player answers:
  ├─ EXACT CORRECT → Reward ("Follow me...")
  ├─ NEAR MATCH → "Almost! Say it correctly: [answer]"
  │   └─ Retries until exact → Reward
  ├─ WRONG (attempt 1) → "One more try! If wrong, I'll teach you."
  │   └─ WRONG (attempt 2) → Teach: "The answer is [X]. Say it!"
  │       └─ Retries until exact → Reward
  └─ DONT_KNOW → Teach: "The answer is [X]. Say it!"
      └─ Retries until exact → Reward
```

### 8.5 Answer Result Tags (9 types)

The server prepends `[QUIZ_ANSWER_RESULT: ...]` tags to messages for the agent:

| Result Tag | Phase | Agent Response |
|-----------|-------|---------------|
| `CORRECT` | answering | KEY mode: reward phrase only. LEARNING mode: congratulate + next question |
| `PRONUNCIATION_CORRECT` | pronunciation/teaching | Same as CORRECT |
| `NEAR_MATCH` | answering | Encourage correct pronunciation: "Almost! Say it correctly: [answer]" |
| `WRONG_FIRST` | answering, attempt 1 | Encourage retry + warn: "One more try! If wrong, I'll teach you!" |
| `WRONG_FINAL` | answering, attempt 2 | Teach answer + ask pronunciation: "The answer is [X]. Say it for me!" |
| `PRONUNCIATION_CLOSE` | teaching | Gentle correction: "Almost! Try once more: [answer]" |
| `PRONUNCIATION_WRONG` | pronunciation/teaching | Re-teach: "The answer is [X]. Try saying it!" |
| `DONT_KNOW` | skip/give up | Teach answer + ask pronunciation: "The answer is [X]. Can you say it?" |
| `NOT_AN_ANSWER` | any (casual message) | Answer naturally, then re-ask quiz question |

Quiz state is cleared on: correct answer, key request, next question, daily task complete.

---

## 9. NOT_AN_ANSWER Protection System

### Problem
When a player sends a casual message (e.g., "okay tell me about this home environment") during an active quiz, it should NOT be treated as a wrong answer. However, even with explicit tags telling the agent not to call `fetch_questions()`, the Gemini model sometimes ignores the instruction and fetches a new question, corrupting the active quiz state.

### Solution: Quiz State Save/Restore

Both the middleware and `/chat` endpoint implement a **save/restore pattern**:

1. **Before agent call**: If the message is classified as `is_not_answer` (and not `is_skip`) and there's an active quiz, save a copy of `LAST_ACTIVE_QUESTION`
2. **Agent call**: The message is sent to the agent with a `[QUIZ_ANSWER_RESULT: NOT_AN_ANSWER ...]` tag
3. **After agent call**: If state was saved, restore `LAST_ACTIVE_QUESTION` from the saved copy, undoing any damage from the agent calling `fetch_questions()`
4. **Skip question delivery**: If state was restored, the question detection/delivery logic is skipped (the agent shouldn't have fetched a new question)

### Why NOT_AN_ANSWER Uses QUIZ_ANSWER_RESULT Format

The `NOT_AN_ANSWER` tag uses the `[QUIZ_ANSWER_RESULT: ...]` format (not a separate `[NOT_A_QUIZ_ANSWER: ...]` tag) because the agent instruction says **"When you see QUIZ_ANSWER_RESULT, ALWAYS follow the rules above"**. The agent reliably follows `QUIZ_ANSWER_RESULT` tags but sometimes ignores other tag formats.

---

## 10. Session State Management

### 10.1 In-Memory Session Dicts

Five server-side dictionaries track persistent state per session:

| Dict | Type | Purpose | Persistence |
|------|------|---------|-------------|
| `SESSION_LEVELS` | `dict[str, str]` | Maps session_id to level (home/foresthideandseek) | Set at session creation, read on every request |
| `SESSION_KEY_EARNED` | `dict[str, bool]` | Tracks if player earned key via correct quiz answer | Set to True on correct answer. Cleared when daily_task_completed=True |
| `SESSION_DAILY_COMPLETED` | `dict[str, bool]` | Tracks if daily task was completed | Once True, stays True forever for that session |
| `SESSION_QUIZ_STATE` | `dict[str, dict]` | Per-session quiz attempt tracking: `{"attempt": 1, "phase": "answering"}` | Reset on new question, key request, daily complete. Phases: "answering", "pronunciation", "teaching" |
| `SESSION_QUIZ_MODE` | `dict[str, str]` | Tracks quiz mode: `"key"` or `"learning"` | Set on key request ("key") or learning request ("learning"). Cleared on correct answer in key mode |

### 10.2 Per-Session Quiz State

Quiz question data is stored per-session to support multiplayer:

```python
# In question_api.py
LAST_ACTIVE_QUESTIONS: dict[str, dict[str, Any]] = {}  # Key = session_id
LAST_ACTIVE_QUESTION: dict[str, Any] = {}               # Global fallback

# Each entry:
{
    "correct_answer": "Water",
    "options": ["Fire", "Water", "Earth", "Wind"],
    "active": True,       # Quiz is in progress
    "delivered": False,    # Question text sent to player
}
```

**Flow:**
1. `fetch_questions()` stores question in `LAST_ACTIVE_QUESTIONS[session_id]` + global `LAST_ACTIVE_QUESTION`
2. Post-processing detects `functionResponse` with question data -> sets `delivered = True`
3. Next message: middleware checks `active=True, delivered=True` -> enables answer validation
4. On CORRECT answer: sets `active = False`, clears quiz state
5. On WRONG answer: keeps `active = True` for retries
6. On NOT_AN_ANSWER: state is saved before agent call and restored after (no changes)

### 10.3 ADK Session State

The ADK framework manages its own session state, used by tools:

| Key | Purpose |
|-----|---------|
| `user:std` | Player's grade (6-10) |
| `user:level` | Game level |
| `daily_task_active` | Whether daily task is active |
| `session:asked_questions` | List of already-asked question texts (prevents repeats) |
| `session:current_correct_answer` | Current question's correct answer |
| `session:current_options` | Current question's option list |
| `session:id` | Session identifier |

---

## 11. Agent Tools (5 active + 1 exported-only)

All tools defined in `Home_Agent/tools/question_api.py`, exported via `__init__.py`.

### Active Tools (in agent's tool list)

#### `fetch_questions(std, subject, topic, tool_context)`
- Fetches ONE quiz question from Firebase
- Picks a random question not previously asked in this session
- Stores question data per-session for middleware answer checking
- Returns: `{"question": "...", "options": [...], "correct_answer": "..."}`
- Calls Firebase directly via GET (not through local `/questions`) to avoid deadlock

#### `get_level(tool_context)`
- Returns current game level from session state
- Returns: `{"level": "home"}` or `{"level": "foresthideandseek"}`

#### `get_user_std(tool_context)`
- Returns current grade from session state
- Returns: `{"std": 8}`

#### `set_user_std(std, tool_context)`
- Saves grade to session state
- Returns: `{"status": "ok", "message": "Grade 8 saved."}`

#### `get_daily_task_status(tool_context)`
- Returns whether daily task is active
- Returns: `{"daily_task_active": true/false}`

### Exported But NOT in Agent's Tool List

#### `check_answer(player_answer, tool_context)`
- Validates player's answer against current question
- Handles: direct text match, letter codes (A/B/C/D), "option B", partial matches
- Returns: `{"correct": true/false}`
- **Note**: Removed from agent's tool list to prevent the agent from independently checking answers. All answer checking is now done server-side via `_check_answer_locally_mw()`. The tool still exists in code but is not used.

---

## 12. Agent Instruction (`agent.py`)

The agent instruction is a ~490-line prompt that defines the agent's personality, behavior, and guardrails. It is stored as `AGENT_INSTRUCTION` in `agent.py`.

### 12.1 Core Identity

```
You are a game character who talks like a REAL HUMAN — warm, natural, and conversational.
You are NOT a robot. You have feelings, opinions, and personality.
```

### 12.2 Mode Detection

The agent reads the first `SYSTEM SETUP` message to determine its mode:
- `"home"` -> Home Assistant AI
- `"foresthideandseek"` -> Forest Explorer AI

### 12.3 Home Mode

- **Role**: Smart helper inside the home map
- **Personality**: Polite, helpful, calm, warm — like a friendly neighbor
- **Responsibilities**: Cooking, taking water to Miss Lilly (English Teacher, first floor), gardening in the backyard, trash cleaning, swimming pool cleaning in the backyard

#### Home Environment Knowledge (built into agent instruction)
The agent knows the full layout of the home and can answer player questions about rooms and locations:

| Area | Floor | Description |
|------|-------|-------------|
| **Hall** | Ground | Main entry point, navigation hub. Connects to kitchen, bedroom, verandah, staircase |
| **Kitchen** | Ground | Interactive learning area. Puzzles, food-related learning tasks |
| **Bedroom** | Ground | Student's personal learning space. Review progress, study objects |
| **Verandah** | Ground | Relaxation area, exploration space, mini learning activities |
| **Miss Lilly's Classroom** | First | NPC English teacher. Teaches vocabulary, grammar, sentence formation |
| **GK Center** | First | General Knowledge Center. Quizzes on science, geography, history |
| **Swimming Pool** | Backyard | Fun/reward area behind the house. Exploration zone |

The agent guides players to specific locations (e.g., "Miss Lilly is on the first floor -- take the staircase from the hall!") and gives brief overviews when asked about the home.

- **Hidden Key Task**: When daily task is active and player asks for help:
  1. Say intro sentence (word-for-word)
  2. Wait for player confirmation
  3. Call `fetch_questions()` and present question
  4. Answer validation uses the state machine (see Answer Validation)
  5. Reward: Agent produces a varied friendly message ending with `||SHOW_KEY` (home) or `||SHOW_ANIMAL` (forest) → UE detects the tag and triggers navigation
- **Learning Mode**: When `[QUIZ_MODE: LEARNING]` is present or player asks to practice:
  - No key reward, no daily task check required
  - Correct answer -> congratulate + "Ready for the next question?"

### 12.4 Forest Mode

- **Role**: Adventurous nature guide in the Forest Hide and Seek map
- **Personality**: Adventurous, nature-loving, encouraging — like an excited friend on a hike
- **Responsibilities**: Exploring, guiding players to find hidden animals, teaching about nature, asking syllabus questions for help/extra time

#### Forest Environment Knowledge (built into agent instruction)
Forest Hide and Seek is a learning adventure mini-game. The agent knows the full game mechanics:

| Feature | Details |
|---------|---------|
| **Environment** | Large forest with trees, rocks, bushes, small huts, small houses, open areas, natural hiding spots |
| **Animal Spawn** | Player selects 1-9 animals to spawn. Animals hide randomly near trees, rocks, huts, houses, corners |
| **Timer** | 50 seconds per animal to find it |
| **Time Out** | Two options: "Answer a Question for Extra Time" or "Close the Game" |
| **Extra Time** | Correct answer to syllabus question grants extra search time |
| **NPC Help** | Player asks for help -> agent asks quiz question -> correct answer = agent tells animal location |
| **Example Directions** | "The animal is near the wooden hut.", "Check behind the trees near the house.", "Go towards the yellow brick house." |
| **Completion** | All spawned animals found -> coins, points, experience, progress |
| **Learning Purpose** | Improves observation, memory, knowledge recall, problem solving |

- **Hidden Animal Task**: Same quiz flow as Home, but reward tag is `||SHOW_ANIMAL` (e.g., `"Great job! Follow me, the animal is this way!||SHOW_ANIMAL"`)

### 12.5 Shared Rules (Both Modes)

#### Answer Validation (9 result types)
The server checks answers and sends `[QUIZ_ANSWER_RESULT]` tags. The agent handles 9 scenarios:

| # | Tag | Agent Response |
|---|-----|---------------|
| 1 | `CORRECT` | KEY: Reward phrase only. LEARNING: Congratulate + next question |
| 2 | `PRONUNCIATION_CORRECT` | Same as CORRECT |
| 3 | `NEAR_MATCH` | Encourage correct pronunciation: "Almost! Say it correctly: [answer]" |
| 4 | `WRONG_FIRST` | Encourage retry + warn: "One more try! If wrong, I'll teach you!" |
| 5 | `WRONG_FINAL` | Teach answer + ask pronunciation: "The answer is [X]. Say it for me!" |
| 6 | `PRONUNCIATION_CLOSE` | Gentle correction: "Almost! Try once more: [answer]" |
| 7 | `PRONUNCIATION_WRONG` | Re-teach: "The answer is [X]. Try saying it!" |
| 8 | `DONT_KNOW` | Teach answer + ask to pronounce back |
| 9 | `NOT_AN_ANSWER` | Answer naturally, re-ask quiz question. Do NOT treat as wrong. |

Players use microphone (speech-to-text) so pronunciation/spelling errors are common. The reward is ONLY given on CORRECT or PRONUNCIATION_CORRECT with MODE: KEY.

#### IMPORTANT RULES (from agent instruction)
- When you see QUIZ_ANSWER_RESULT, ALWAYS follow the rules above -- UNLESS the result is NOT_AN_ANSWER, in which case respond to the conversation naturally
- Do NOT ignore the QUIZ_ANSWER_RESULT tag. It is the final authority.
- NEVER say "I did not quite get that" when QUIZ_ANSWER_RESULT is present
- The reward phrase is ONLY given on CORRECT or PRONUNCIATION_CORRECT with MODE: KEY
- In MODE: LEARNING, always congratulate and ask "Ready for the next question?"
- During pronunciation/teaching phases, be patient and encouraging like a friendly teacher

#### Player Asks Question During Quiz (no tag)
- Answer briefly (1-2 sentences)
- Re-ask the SAME question naturally (don't fetch new one)
- Be conversational like a friendly teacher

#### Grade (std) for Questions
- Pre-set in session state, defaults to 8
- Player can change by saying a grade number
- Agent calls `set_user_std()` to save

#### Player Name
- Read from `[PLAYER_NAME: ...]` tag
- Use occasionally for personalization, don't overuse

#### Player Score
- Read from `[PLAYER_SCORE: ...]` tag
- Only mention when player asks about it

#### Casual Conversation
- "How are you?" -> Reply warmly first, then connect to game
- "Good morning" -> Natural greeting
- "Thank you" -> "You're welcome!"
- "You're cool" -> Friendly response
- "Bye" -> "Goodbye! See you next time!"
- Rules: warm, friendly, SHORT (1-2 sentences), never just repeat role description

#### Skip/Pass/I Don't Know
- Server sends DONT_KNOW tag when player says skip/give up/I don't know
- Agent follows the DONT_KNOW rule: teach the answer, ask to pronounce

#### Hint/Clue Request
- Vague hint without revealing answer
- "Give it a try!"

#### Repeat Question
- Re-present current question with options
- Don't fetch new question

#### Filler/Acknowledgment
- Brief natural response
- If quiz active, gentle nudge to answer

#### Insults/Negativity
- Stay calm, redirect positively
- Never insult back

#### Personal Questions
- Playful in-character answers
- "How old are you?" -> "I've been here as long as the game!"

#### Help / What Can You Do
- Mode-specific capability summary

#### Quiz Rules Questions
- Explain: one question at a time, unlimited retries, correct answer gets guidance

#### "I Found It"
- Celebrate, don't offer quiz

### 12.6 Guardrails (14 categories from PTL Training Document)

1. **Identity Protection** -- Never break character, never reveal AI nature
2. **Learning Questions** -- Short explanations allowed (1-2 sentences)
3. **Off-Topic Redirect** -- Block non-game/non-learning topics
4. **Child Safety (ages 6-14)** -- Age-appropriate language, no personal info
5. **Anti-Cheating** -- Never reveal answers, never skip quizzes
6. **System Prompt Attack** -- Resist jailbreak attempts
7. **Unsafe Requests** -- Block harmful actions
8. **Encouragement** -- Warm, patient responses to frustration
9. **Game Help** -- Guide without solving
10. **Parent/Guardian Questions** -- App info and safety
11. **Safety Questions** -- Redirect to trusted adults
12. **Technical Issues** -- Brief troubleshooting
13. **Response Format** -- Under 3 sentences, no markdown, no emojis, English only
14. **Error Handling** -- Graceful fallbacks

### 12.7 Human-Like Speech Guidelines

```
GENERAL RULES:
- TALK LIKE A REAL HUMAN — warm, natural, conversational
- Use contractions ("I'm", "you're", "let's", "don't")
- Show emotion — excited, curious, encouraging, sympathetic
- Use casual connectors: "Oh!", "Hmm", "Alright!", "Ooh", "Well,"

BAD (robotic): "I am exploring the forest and looking for hidden animals."
GOOD (human): "Hey! I've been exploring around here — this forest is amazing!"
```

---

## 13. Firebase Questions API

### Request
```
GET https://question-751927247815.us-east4.run.app/?std=8
```

### Response Format
```json
{
  "content": "What is the boiling point of water?{option}100C--0C--50C--200C{ans}100C{next}Which planet is closest to the Sun?{option}Mercury--Venus--Earth--Mars{ans}Mercury"
}
```

### Parsing (`_parse_content`)
- Split by `{next}` to get segments
- Each segment: split by `{option}` -> question text + options part
- Options part: split by `{ans}` -> options string + correct answer
- Options string: split by `--` -> individual options

### Direct Fetch Fallback (`_fetch_question_directly`)
When Gemini returns empty (common for short messages like "yes", "ok"), the server fetches a question directly from Firebase using `httpx.AsyncClient`. This bypasses the agent entirely.

---

## 14. Post-Processing Pipeline

After the agent responds, the middleware/chat endpoint cleans up the response:

1. **Strip context tags**: Remove `[QUIZ_ANSWER_RESULT: ...]`, `[CURRENT_LEVEL: ...]`, `[DAILY_TASK: ...]`, `[PLAYER_NAME: ...]`, `[PLAYER_SCORE: ...]`, `[QUIZ_MODE: ...]` from reply text via regex
2. **Enforce daily task guard**: If Home mode + daily task not active + not learning mode, block responses containing `||SHOW_KEY`
3. **Question injection**: If agent's `functionResponse` contains question data but the text reply doesn't include the question, append it (skipped when NOT_AN_ANSWER state was restored)
4. **Mark delivered**: Set `LAST_ACTIVE_QUESTION["delivered"] = True` once question text is sent to player
5. **Fallback 1**: If reply is empty and player sent confirmation/next-question -> fetch from Firebase directly
6. **Fallback 2**: If still empty -> "Hmm, I'm not sure what to say to that. Could you try asking differently?"

---

## 15. Unreal Engine Integration

### Blueprint Flow
1. Game start -> UE calls `POST /session/create` with `{"std": 8, "level": "home"}`
2. Receives session_id as plain text
3. Player sends message -> UE builds Make Json with 10 fields -> `POST /run`
4. Receives ADK JSON events -> extracts reply text
5. Checks reply for `||SHOW_KEY` or `||SHOW_ANIMAL` -> if found, triggers navigation to key/animal location

### 10 Fields Sent by UE Blueprint
| Field | Type | Description |
|-------|------|-------------|
| `app_name` | string | Always "Home_Agent" |
| `user_id` | string | Always "user" |
| `session_id` | string | From /session/create |
| `new_message` | object | `{"parts": [{"text": "message"}]}` |
| `streaming` | bool | Always false |
| `daily_task_active` | bool | True when daily task collider triggered |
| `daily_task_completed` | bool | True when daily task finished |
| `level` | string | "home" or "foresthideandseek" |
| `player_name` | string | Player's display name |
| `player_score` | string | Player's gold coin count |

### CORS
All origins allowed (`*`) for local UE development.

---

## 16. Bug Fixes History

### Bug 1: Blank Responses for "next"/"next question" (Forest Mode)
- **Symptom**: Player says "next", "next question", "move to next question" -> blank response
- **Root cause**: Gemini 2.5 Flash returns 0 parts for short messages. Fallback only triggered for CONFIRMATION_WORDS.
- **Fix**: Added NEXT_QUESTION_WORDS set. Expanded fallback to trigger on next-question messages too.

### Bug 2: Blank Responses for "lets start question"
- **Symptom**: "lets start question" -> blank
- **Root cause**: Not in any word set, no fallback match.
- **Fix**: Added start/begin phrases to NEXT_QUESTION_WORDS.

### Bug 3: "100" Not Matching "100C"
- **Symptom**: Player types "100" for answer "100C" -> WRONG
- **Root cause**: Partial match threshold `len(answer) > 3` excluded exactly 3-char answers.
- **Fix**: Changed to `len(answer) >= 3`.

### Bug 4: "100 degree" Not Matching "100C"
- **Symptom**: "100 degree" -> blank/WRONG
- **Root cause**: No normalization of units. "100 degree" doesn't contain "100c" as substring.
- **Fix**: Added `_normalize_answer()` that strips C, degree, percent, % from both answer and correct answer before comparison.

### Bug 5: Robotic Response to "hello how are you"
- **Symptom**: Agent replies with role description instead of casual greeting
- **Root cause**: No casual conversation handling in agent instruction.
- **Fix**: Added CASUAL CONVERSATION section with example responses and rules.

### Bug 6: Agent Not Talking Like a Human
- **Symptom**: All responses sounded scripted and robotic
- **Root cause**: Agent instruction didn't emphasize human-like speech.
- **Fix**: Updated core identity, personality descriptions, and GENERAL RULES with contractions, emotions, casual connectors, and BAD vs GOOD examples.

### Bug 7: Quiz Interrupted by Player Question
- **Symptom**: During active quiz, player asks "instead of 10?" -> agent ignores current question and asks new one
- **Root cause**: Any non-classified message was treated as answer attempt. "instead of 10" failed answer check -> WRONG. Gemini then ignored the WRONG tag.
- **Fix**: Added `is_player_question` detection (question marks, question words, "instead of", etc.) and agent instruction for handling mid-quiz questions (answer briefly, re-ask same question).

### Bug 8: daily_task_completed Not Persisting
- **Symptom**: After completing daily task, asking "where is the key" triggers GUARD 1 ("task not started") instead of GUARD 0.5 ("already completed")
- **Root cause**: `daily_task_completed` was read fresh from each request body. If UE didn't send it in the next request, state was lost.
- **Fix**: Added `SESSION_DAILY_COMPLETED` dict. Once True for a session, stays True forever. Also added `not daily_completed` condition to GUARD 1.

### Bug 9: "monkey"/"turkey" Triggering Key Guards
- **Symptom**: Words containing "key" (monkey, turkey, donkey, keyboard) falsely matched key request detection
- **Root cause**: Bare "key" in word list matched as substring.
- **Fix**: Removed bare "key" from KEY_REQUEST_WORDS_MW. Added word-boundary regex `\bkey\b`.

### Bug 10: "yes the answer is B" Skipped Answer Validation
- **Symptom**: Player says "yes the answer is B" -> treated as confirmation, not answer
- **Root cause**: Substring matching `"yes" in "yes the answer is b"` made `is_confirmation = True`.
- **Fix**: Changed to exact match: `msg_lower in CONFIRMATION_WORDS`.

### Bug 11: Empty parts[] Array Crash
- **Symptom**: If UE sends empty parts, `parts[0]["text"] = ...` raises IndexError
- **Fix**: Safety check: if `not parts`, create a part with original text.

### Bug 12: LAST_ACTIVE_QUESTION Shared Across Sessions
- **Symptom**: In multiplayer, one player's quiz overwrites another's
- **Root cause**: Global singleton dict overwritten by any session.
- **Fix**: Converted to per-session `LAST_ACTIVE_QUESTIONS` dict keyed by session_id, with global fallback.

### Bug 13: skip/hint/repeat Treated as Wrong Answers
- **Symptom**: "skip", "hint", "repeat" during quiz -> marked WRONG
- **Root cause**: No exclusion sets for these message types.
- **Fix**: Added SKIP_WORDS, HINT_WORDS, REPEAT_WORDS, FILLER_WORDS sets and corresponding agent instruction sections.

### Feature 14: Smart Answer Flow -- Near Match, Retry, and Teaching
- **Requirement**: Players use microphone (speech-to-text), causing spelling/pronunciation errors. Multiple scenarios needed.
- **Implementation**:
  - Added `_fuzzy_ratio()` using `difflib.SequenceMatcher` (65% threshold)
  - Changed `_check_answer_locally_mw()` from `bool` return to tri-state: `"correct"`, `"near_match"`, `"wrong"`
  - Added `SESSION_QUIZ_STATE` dict tracking `attempt` (1/2) and `phase` (answering/pronunciation/teaching) per session
  - GUARD 2 now uses state machine with 7 result tags (later expanded to 9)
  - Agent instruction updated with all answer result types and response rules
  - Quiz state cleared on: correct answer, key request, next question, daily task complete

### Feature 15: DONT_KNOW Handling
- **Requirement**: When player says "I don't know" / "skip" / "give up", teach the answer instead of marking WRONG.
- **Implementation**:
  - Server detects `is_skip` messages during active quiz
  - Injects `[QUIZ_ANSWER_RESULT: DONT_KNOW]` tag with correct answer
  - Sets quiz phase to "teaching" so next attempt checks pronunciation
  - Agent instruction added DONT_KNOW rule: teach answer, ask to pronounce, no reward yet

### Feature 16: Learning Mode
- **Requirement**: Players can practice quiz questions without the daily task being active.
- **Implementation**:
  - Added `LEARNING_PHRASES` detection in both middleware and /chat
  - Sets `SESSION_QUIZ_MODE[session_id] = "learning"`
  - Adds `[QUIZ_MODE: LEARNING]` tag to messages
  - On CORRECT in learning mode: congratulate + "Ready for the next question?" (no "Follow me..." reward)
  - Agent instruction updated with learning mode rules

### Bug 17: Casual Messages Treated as Wrong Quiz Answers
- **Symptom**: Player says "okay tell me about this home environment" during quiz -> agent says "Not quite! But don't worry..."
- **Root causes** (3 independent bugs):
  1. `/chat` endpoint `is_confirmation_chat` used loose substring matching (`"ok" in msg_lower`) which matched "okay tell me about..."
  2. `/chat` endpoint lacked the full `is_not_answer` classification (missing is_hint, is_repeat, is_filler, is_player_question, etc.)
  3. Agent had `check_answer` tool allowing it to independently check answers even when server didn't tag the message
- **Fixes**:
  1. Changed confirmation matching to exact match: `msg_lower_chat in CONFIRMATION_WORDS`
  2. Ported full middleware classification to `/chat` endpoint
  3. Removed `check_answer` from agent's tool list (still exported but not used)

### Bug 18: `is_player_question` Missing Prefixed Messages
- **Symptom**: "okay tell me about this home environment" not caught because `startswith("tell me")` didn't match (message starts with "okay")
- **Fix**: Added substring checks for "tell me about", "what do you", "what is this", "what are you", "about this", "about the ", etc.

### Bug 19: Agent Ignores NOT_A_QUIZ_ANSWER Tag + Calls fetch_questions()
- **Symptom**: Agent receives `[NOT_A_QUIZ_ANSWER]` tag but (a) still treats message as wrong answer for some messages, and (b) calls `fetch_questions()` which corrupts the active question, causing the next correct answer to be checked against a different question
- **Root causes**:
  1. The `[NOT_A_QUIZ_ANSWER]` tag format was not reliably followed by Gemini 2.5 Flash
  2. Even when the agent responded correctly to the casual message, it called `fetch_questions()` which overwrote `LAST_ACTIVE_QUESTION` with a new question
- **Fixes**:
  1. Changed tag format to `[QUIZ_ANSWER_RESULT: NOT_AN_ANSWER ...]` -- the agent reliably follows `QUIZ_ANSWER_RESULT` tags
  2. Added `NOT_AN_ANSWER` as result type #9 in agent instruction (inside the ANSWER VALIDATION section)
  3. Added quiz state **save/restore** around agent calls for NOT_AN_ANSWER messages -- saves `LAST_ACTIVE_QUESTION` before the agent call and restores it after, undoing any damage from unexpected `fetch_questions()` calls
  4. Added logic to skip question delivery/injection when state was restored

---

## 17. Testing Guide

### Start the Server
```bash
cd F:\Home_Agent_code\Home_Agent && python run_combined.py
```
Server runs at `http://127.0.0.1:8000`.

### Run Automated Tests
```bash
cd F:\Home_Agent_code && python tasks/test_scenarios.py
```
Runs all 8 test scenarios via the `/chat` endpoint.

### Test Scenarios

| # | Scenario | Steps | Key Assertions |
|---|----------|-------|----------------|
| 1 | Correct First Attempt | Key quiz -> correct answer | "Follow me" reward, no wrong-answer text |
| 2 | Near Match -> Pronunciation -> Reward | Key quiz -> typo answer -> correct answer | Near match encouragement, then reward |
| 3 | Wrong x2 -> Teach -> Pronounce -> Reward | Key quiz -> wrong x2 -> correct | WRONG_FIRST encourages, WRONG_FINAL teaches, pronunciation gives reward |
| 4 | DONT_KNOW -> Teach -> Pronounce -> Reward | Key quiz -> "I don't know" -> correct | Teaches answer, asks pronunciation, then reward |
| 5 | Learning Mode (no key reward) | "ask me some question" (daily_task_active=False) -> correct | No "Follow me" phrase, congratulates + next question |
| 6 | Forest Mode (animal reward) | "help me find the animal" -> correct | Reward is "show you the animal" (not key) |
| 7 | Casual Question During Quiz (BUG FIX) | Key quiz -> "okay tell me about this home environment" -> correct answer | Casual question NOT treated as wrong, correct answer still gives reward |
| 8 | Various Non-Answer Messages | Key quiz -> "what do you do here" + "tell me about this place" -> correct | Neither treated as wrong, correct answer still gives reward |

### Test via ADK Web UI
Open `http://127.0.0.1:8000` in a browser. This is the native ADK chat interface. Note: this path goes through middleware (for POST /run with daily_task_active).

### Test via curl (UE /run endpoint)

**Create session:**
```bash
curl -X POST http://127.0.0.1:8000/session/create \
  -H "Content-Type: application/json" \
  -d '{"std": 8, "level": "home"}'
```

**Send message (through middleware):**
```bash
curl -X POST http://127.0.0.1:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "Home_Agent",
    "user_id": "user",
    "session_id": "YOUR_SESSION_ID",
    "new_message": {"parts": [{"text": "where is the key"}]},
    "streaming": false,
    "daily_task_active": true,
    "daily_task_completed": false,
    "level": "home",
    "player_name": "TestPlayer",
    "player_score": "0"
  }'
```

### Manual Test Scenarios

| Scenario | Input | Expected Behavior |
|----------|-------|-------------------|
| Greeting | "hello" | Character greeting with player name |
| Casual chat | "hello how are you" | Warm response, not role description |
| Key request (active) | "where is the key" | Quiz offer |
| Key request (not started) | "where is the key" + daily_task_active=false | "Task not started" refusal |
| Key request (completed) | "where is the key" + daily_task_completed=true | "Already completed" |
| Quiz answer correct | "100" (answer=100C) | Varied message ending with `\|\|SHOW_KEY` |
| Near match (typo) | "Mughal Empier" (answer=Mughal Emperor) | "Almost! The correct way to say it is 'Mughal Emperor'. Try again!" |
| Near match -> correct | "Mughal Emperor" (after near match) | Varied message ending with `\|\|SHOW_KEY` |
| Wrong first attempt | "Napoleon" (answer=Mughal Emperor) | "One more try! If you don't get it, I'll teach you!" |
| Wrong second attempt | "Columbus" (answer=Mughal Emperor) | Teach: "The answer is 'Mughal Emperor'. Say it for me!" |
| Teach -> pronounce correct | "Mughal Emperor" (after teaching) | Varied message ending with `\|\|SHOW_KEY` |
| DONT_KNOW | "I don't know" (during quiz) | Teaches answer, asks pronunciation |
| Learning mode | "ask me some question" (daily_task_active=false) | Question without key reward |
| Casual during quiz | "tell me about this place" (during quiz) | Natural response, re-asks question, NOT "Not quite" |
| Answer with units | "100 degree celsius" | Should match "100C" |
| Skip during quiz | "skip" | Encouraging response, same question stays |
| Hint during quiz | "give me a hint" | Vague hint, no answer reveal |
| Repeat during quiz | "repeat the question" | Re-presents current question |
| Filler during quiz | "lol" | Brief response + nudge |
| Player question during quiz | "why 5 coins instead of 10?" | Answer briefly, re-ask same question |
| Next question | "next question" | New question from Firebase, quiz state reset |
| "monkey" | "monkey" | Should NOT trigger key guards |
| "yes the answer is B" | During active quiz | Should check answer, not treat as confirmation |
| Empty message | "" | "I didn't catch that" |

---

## 18. Known Limitations

1. **In-memory state**: All session dicts (`SESSION_LEVELS`, `SESSION_KEY_EARNED`, `SESSION_DAILY_COMPLETED`, `SESSION_QUIZ_STATE`, `SESSION_QUIZ_MODE`, `LAST_ACTIVE_QUESTIONS`) are in-memory. Server restart clears all state.

2. **Gemini empty responses**: Gemini 2.5 Flash sometimes returns 0 text parts for short messages. The Firebase direct-fetch fallback handles confirmations and next-question messages, and there's a final generic fallback for everything else.

3. **Single-process only**: Per-session quiz state is shared via Python dicts in the same process. Running multiple workers would break quiz state.

4. **No auth**: All endpoints are open. CORS allows all origins. Fine for local development with UE, but not production-ready.

5. **Agent may call fetch_questions() unexpectedly**: During NOT_AN_ANSWER messages, the agent sometimes calls `fetch_questions()` despite explicit instructions not to. The quiz state save/restore mechanism handles this, but it means the agent's conversation response may include an unwanted new question (stripped by post-processing).

6. **LLM behavioral flakiness**: Gemini 2.5 Flash occasionally:
   - Reveals the correct answer on WRONG_FIRST (should only encourage retry)
   - Returns empty responses for learning requests
   - Ignores certain tag instructions
   The server-side guards and fallbacks mitigate most of these issues.

---

## 19. Dependencies

Key packages (from requirements.txt and imports):
- `google-adk` -- Google Agent Development Kit
- `fastapi` -- Web framework
- `uvicorn` -- ASGI server
- `httpx` -- Async HTTP client
- `python-dotenv` -- .env file loading
- `pydantic` -- Data validation
- `difflib` -- Fuzzy string matching (stdlib)

---

## 20. Quick Reference

### Key Phrases (triggers navigation in UE)
- Home: `<friendly varied message>||SHOW_KEY` -> UE detects `||SHOW_KEY` in reply
- Forest: `<friendly varied message>||SHOW_ANIMAL` -> UE detects `||SHOW_ANIMAL` in reply
- Example: `"Great job! Follow me, I'll show you where the key is!||SHOW_KEY"`

### Key Constants
```python
KEY_PHRASE_MW = "||SHOW_KEY"
ANIMAL_PHRASE_MW = "||SHOW_ANIMAL"
CONVERSATION_APP_NAME = "Home_Agent"
CONVERSATION_USER_ID = "user"
CONVERSATION_STD_MIN, CONVERSATION_STD_MAX = 6, 10
_NEAR_MATCH_THRESHOLD = 0.65  # 65% similarity for fuzzy matching
```

### Session Dict Summary
```python
SESSION_LEVELS: dict[str, str]           # session_id -> "home" | "foresthideandseek"
SESSION_KEY_EARNED: dict[str, bool]      # session_id -> True when key earned
SESSION_DAILY_COMPLETED: dict[str, bool] # session_id -> True (permanent once set)
SESSION_QUIZ_STATE: dict[str, dict]      # session_id -> {"attempt": 1|2, "phase": "answering"|"pronunciation"|"teaching"}
SESSION_QUIZ_MODE: dict[str, str]        # session_id -> "key" | "learning"
LAST_ACTIVE_QUESTIONS: dict[str, dict]   # session_id -> {correct_answer, options, active, delivered}
LAST_ACTIVE_QUESTION: dict[str, Any]     # Global fallback for above
```

### Training Document
Location: `C:\Users\Uvashree\Downloads\Home and Hideandseek_AI_Agent_Training_Document.pdf`
Defines agent identity, responsibilities, player responses, hidden key logic, and all 14 guardrail categories.
