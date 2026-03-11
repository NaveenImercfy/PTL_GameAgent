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

---

## 2. File Structure

```
F:\Home_Agent_code\Home_Agent\
├── run_combined.py                 # Main server (~1086 lines) — middleware, endpoints, all guards
├── question_server.py              # Questions proxy router (for Postman testing)
├── requirements.txt                # Dependencies
├── guardrail_questions.md          # QA reference for testing guardrails
├── start_web.bat / start_web.ps1   # Launchers
├── PROJECT_DOCUMENTATION.md        # This file
├── .claude/
│   └── launch.json                 # Claude Preview config
└── Home_Agent/
    ├── agent.py                    # Agent definition + full instruction (~415 lines)
    ├── .env                        # API keys, Firebase URL, default grade
    ├── __init__.py
    └── tools/
        ├── __init__.py             # Exports: fetch_questions, get_level, get_user_std, set_user_std, check_answer, get_daily_task_status
        └── question_api.py         # All tool implementations (~333 lines)
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
│  │         set_user_std, check_answer, get_daily_task_status  │  │
│  └─────────────────────────┬──────────────────────────────────┘  │
│                            │ fetch_questions() calls Firebase    │
│                            ▼                                     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │          Firebase Questions API (external)                  │  │
│  │  GET https://question-...run.app/?std=8                    │  │
│  │  Returns: question{option}A--B--C--D{ans}answer{next}...  │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Additional routers:                                             │
│  ├── /session/create (UE session creation)                       │
│  ├── /chat (legacy UE endpoint — PlainTextResponse)              │
│  ├── /conversation/start (ADK Web UI session creation)           │
│  └── /questions (proxy for Postman testing)                      │
└──────────────────────────────────────────────────────────────────┘
```

### Two Paths Into the Agent

| Path | Source | Endpoint | Middleware | Response Format |
|------|--------|----------|-----------|-----------------|
| **UE Blueprint (primary)** | Unreal Engine | POST `/run` | DailyTaskRunMiddleware intercepts | ADK JSON events |
| **Legacy /chat** | UE (older flow) | POST `/chat` | No middleware — inline logic | PlainTextResponse |

**Important**: The middleware path (`/run`) is the primary and most robust path. The `/chat` endpoint has divergent logic (older answer checker, missing some exclusions). UE Blueprints should use `/run` directly.

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

**Navigate to key/animal detection**: UE checks if the reply contains "Follow me, I will show you the key" or "Follow me, I will show you the animal" to set `navigate_to_key = true`.

#### `POST /chat` (legacy)

Simpler endpoint for UE. Returns PlainTextResponse.

**Request:**
```json
{
  "session_id": "abc-123",
  "message": "where is the key",
  "daily_task_active": true
}
```

**Response:** Plain text reply string.

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
  ├─ GUARD 2: Active quiz + answer attempt → state machine (CORRECT/NEAR_MATCH/WRONG)
  │
  ├─ Classify message (9 categories)
  ├─ Enrich with tags: [DAILY_TASK], [CURRENT_LEVEL], [PLAYER_NAME], [PLAYER_SCORE], [QUIZ_ANSWER_RESULT]
  ├─ Forward to ADK agent
  │
  ├─ POST-PROCESS RESPONSE:
  │   ├─ Strip all context tags from agent reply
  │   ├─ Enforce: block "Follow me" if daily task not active (Home mode)
  │   ├─ Inject question text if agent omitted it (from functionResponse)
  │   ├─ Mark question as delivered
  │   ├─ FALLBACK 1: If empty + confirmation/next → fetch question from Firebase directly
  │   └─ FALLBACK 2: If still empty → generic "I'm not sure" message
  │
  └─ Return cleaned ADK JSON events
```

### 6.2 Guard System (in order)

#### Empty Message Guard
- **Condition**: `not msg_lower` (empty or whitespace-only message)
- **Response**: `"I didn't catch that — could you say something?"`

#### GUARD 0 — Greeting
- **Condition**: `msg_lower in GREETING_WORDS_MW` (exact match only)
- **Words**: hello, hi, hey, hii, helo, greetings, howdy, sup, yo, yoo, hola, heyyy, heyy, wassup, whatsup
- **Response**: Character greeting with player name if available
  - Home: `"Hey {name}! I'm your Home Assistant — great to see you! How can I help you today?"`
  - Forest: `"Hey {name}! I'm the Forest Explorer AI — I've been exploring around here and this forest is amazing! What's up?"`

#### GUARD 0.5 — Daily Task Completed
- **Condition**: `level == "home" AND daily_completed AND is_key_request`
- **Response**: `"You've already completed the daily task — there's no key in the home anymore. Great job!"`
- **Note**: `daily_completed` is persisted via `SESSION_DAILY_COMPLETED` — once True, always True for that session

#### GUARD 0.75 — Key Already Earned
- **Condition**: `NOT daily_completed AND SESSION_KEY_EARNED[session_id] AND is_key_request`
- **Response**: `"Follow me, I will show you the key."` (repeats navigation)

#### GUARD 1 — Daily Task Not Started
- **Condition**: `level == "home" AND NOT daily_active AND NOT daily_completed AND is_key_request`
- **Response**: `"The daily task hasn't started yet! Start the daily task first, then I can help you find the key."`

#### GUARD 2 — Server-Side Answer Check (State Machine)
- **Condition**: Active quiz (`_LAQ.active AND _LAQ.delivered`) AND message is NOT classified as `is_not_answer`
- **Answer checker returns tri-state**: `"correct"`, `"near_match"`, or `"wrong"`
- **Near-match detection**: Uses `difflib.SequenceMatcher` with 65% similarity threshold for speech-to-text typos
- **Per-session state**: `SESSION_QUIZ_STATE[session_id]` tracks `attempt` (1 or 2) and `phase` ("answering", "pronunciation", "teaching")
- **State machine flow**:

| Result | Phase | Tag Sent | Next State |
|--------|-------|----------|------------|
| `correct` | answering | `CORRECT` → reward | Quiz done, key earned |
| `correct` | pronunciation/teaching | `PRONUNCIATION_CORRECT` → reward | Quiz done, key earned |
| `near_match` | answering | `NEAR_MATCH` → encourage pronunciation | phase → "pronunciation" |
| `near_match` | teaching | `PRONUNCIATION_CLOSE` → try again | stays "teaching" |
| `wrong` | answering, attempt 1 | `WRONG_FIRST` → encourage retry | attempt → 2 |
| `wrong` | answering, attempt 2 | `WRONG_FINAL` → teach answer, ask pronunciation | phase → "teaching" |
| `wrong` | pronunciation/teaching | `PRONUNCIATION_WRONG` → teach again | phase → "teaching" |

- Quiz state is cleared on: correct answer, key request, next question, daily task complete

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

The middleware classifies every message into categories to decide whether it's a quiz answer attempt or something else. Only unclassified messages go through answer checking.

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
| **Player question** | `is_player_question` | Multiple heuristics | "?", "why ...", "how ...", "instead of ...", "but ..." |

**Combined exclusion:**
```python
is_not_answer = (is_key_request or is_confirmation or is_next_question
                 or is_skip or is_hint or is_repeat or is_filler
                 or is_emoji_only or is_player_question)
```

If `is_not_answer` is True and a quiz is active, the message is passed to the agent WITHOUT answer checking.

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

---

## 8. Answer Checking System

### 8.1 Normalization (`_normalize_answer`)

Before comparing answers, both the player's answer and the correct answer are normalized:

1. **Strip prefixes**: "the answer is ", "i think ", "its ", "my answer is ", etc.
2. **Strip units**: °C, °F, °, "degree celsius", "degree fahrenheit", "degrees", "degree", "percent", %
3. **Strip trailing punctuation**: `!.,;:?`
4. **Lowercase and trim**

Example: `"I think 100 degree celsius!"` → `"100"`

### 8.2 Match Levels (`_check_answer_locally_mw`)

The answer checker returns a **tri-state**: `"correct"`, `"near_match"`, or `"wrong"`.

**Exact match checks (returns "correct"):**
1. **Direct text match**: `answer == correct` or `normalized_answer == normalized_correct`
2. **Letter match**: Player typed "A", "B", "option C", etc. Checks both raw and normalized answer.
3. **Option text match**: Player typed the full text of an option that is the correct answer.
4. **Partial match** (>=3 chars): `correct in answer` or `answer in correct`. Also checked with normalized versions.

**Fuzzy match check (returns "near_match"):**
5. **Fuzzy similarity** via `difflib.SequenceMatcher`: If none of the exact checks pass, computes similarity ratio against the correct answer and correct option texts. If best ratio >= 0.65 (65%), returns `"near_match"`. This handles speech-to-text typos like "Mughal Empier" → "Mughal Emperor".

**Otherwise returns "wrong".**

### 8.3 Why `>= 3` Not `> 3`

Three-character answers like "100" need to match. The threshold was changed from `> 3` to `>= 3` to support this.

### 8.4 Quiz Answer State Machine

The answer flow uses a per-session state machine (`SESSION_QUIZ_STATE`) to handle 3 scenarios:

**Scenario 1 — Correct on first attempt:** Direct reward ("Follow me...").

**Scenario 2 — Near match (speech-to-text typo):** Agent encourages correct pronunciation. Player retries until they say it correctly, then gets the reward.

**Scenario 3 — Wrong answer:** First attempt gets encouragement + warning. Second wrong attempt triggers teaching (agent reveals the answer and asks player to repeat it). Player must pronounce it correctly to get the reward.

```
Player answers:
  ├─ EXACT CORRECT → Reward ("Follow me...")
  ├─ NEAR MATCH → "Almost! Say it correctly: [answer]"
  │   └─ Retries until exact → Reward
  └─ WRONG (attempt 1) → "One more try! If wrong, I'll teach you."
      └─ WRONG (attempt 2) → Teach: "The answer is [X]. Say it!"
          └─ Retries until exact → Reward
```

---

## 9. Session State Management

### 9.1 In-Memory Session Dicts

Four server-side dictionaries track persistent state per session:

| Dict | Type | Purpose | Persistence |
|------|------|---------|-------------|
| `SESSION_LEVELS` | `dict[str, str]` | Maps session_id → level (home/foresthideandseek) | Set at session creation, read on every request |
| `SESSION_KEY_EARNED` | `dict[str, bool]` | Tracks if player earned key via correct quiz answer | Set to True on correct answer. Cleared when daily_task_completed=True |
| `SESSION_DAILY_COMPLETED` | `dict[str, bool]` | Tracks if daily task was completed | Once True, stays True forever for that session |
| `SESSION_QUIZ_STATE` | `dict[str, dict]` | Per-session quiz attempt tracking: `{"attempt": 1, "phase": "answering"}` | Reset on new question, key request, daily complete. Phases: "answering", "pronunciation", "teaching" |

### 9.2 Per-Session Quiz State

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
2. Post-processing detects `functionResponse` with question data → sets `delivered = True`
3. Next message: middleware checks `active=True, delivered=True` → enables answer validation
4. On CORRECT answer: sets `active = False`
5. On WRONG answer: keeps `active = True` for retries

### 9.3 ADK Session State

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

## 10. Agent Tools (6 tools)

All tools defined in `Home_Agent/tools/question_api.py`, exported via `__init__.py`.

### `fetch_questions(std, subject, topic, tool_context)`
- Fetches ONE quiz question from Firebase
- Picks a random question not previously asked in this session
- Stores question data per-session for middleware answer checking
- Returns: `{"question": "...", "options": [...], "correct_answer": "..."}`
- Calls Firebase directly via GET (not through local `/questions`) to avoid deadlock

### `check_answer(player_answer, tool_context)`
- Validates player's answer against current question
- Handles: direct text match, letter codes (A/B/C/D), "option B", partial matches
- Returns: `{"correct": true/false}`
- Note: The middleware also validates answers server-side — this tool is a backup for when the agent calls it

### `get_level(tool_context)`
- Returns current game level from session state
- Returns: `{"level": "home"}` or `{"level": "foresthideandseek"}`

### `get_user_std(tool_context)`
- Returns current grade from session state
- Returns: `{"std": 8}`

### `set_user_std(std, tool_context)`
- Saves grade to session state
- Returns: `{"status": "ok", "message": "Grade 8 saved."}`

### `get_daily_task_status(tool_context)`
- Returns whether daily task is active
- Returns: `{"daily_task_active": true/false}`

---

## 11. Agent Instruction (`agent.py`)

The agent instruction is a ~400-line prompt that defines the agent's personality, behavior, and guardrails. It is stored as `AGENT_INSTRUCTION` in `agent.py`.

### 11.1 Core Identity

```
You are a game character who talks like a REAL HUMAN — warm, natural, and conversational.
You are NOT a robot. You have feelings, opinions, and personality.
```

### 11.2 Mode Detection

The agent reads the first `SYSTEM SETUP` message to determine its mode:
- `"home"` → Home Assistant AI
- `"foresthideandseek"` → Forest Explorer AI

### 11.3 Home Mode

- **Role**: Smart helper inside the home map
- **Personality**: Polite, helpful, calm, warm — like a friendly neighbor
- **Responsibilities**: Cooking, taking water to Miss Lilly, gardening, trash cleaning, pool cleaning
- **Hidden Key Task**: When daily task is active and player asks for help:
  1. Say intro sentence (word-for-word)
  2. Wait for player confirmation
  3. Call `fetch_questions()` and present question
  4. Answer validation uses the state machine (see Answer Validation below)
  5. Reward phrase: `"Follow me, I will show you the key."` (`navigate_to_key: true`)

### 11.4 Forest Mode

- **Role**: Adventurous nature guide in the Forest Hide and Seek map
- **Personality**: Adventurous, nature-loving, encouraging — like an excited friend on a hike
- **Responsibilities**: Exploring, finding hidden animals, teaching about nature
- **Hidden Animal Task**: Same flow as Home, but reward phrase is "Follow me, I will show you the animal."

### 11.5 Shared Rules (Both Modes)

#### Answer Validation (7 result types)
The server checks answers and sends `[QUIZ_ANSWER_RESULT]` tags. The agent handles 7 scenarios:

| Tag | Agent Response |
|-----|---------------|
| `CORRECT` | Reward phrase only ("Follow me...") |
| `PRONUNCIATION_CORRECT` | Reward phrase only ("Follow me...") |
| `NEAR_MATCH` | Encourage correct pronunciation: "Almost! Say it correctly: [answer]" |
| `WRONG_FIRST` | Encourage retry + warn: "One more try! If wrong, I'll teach you!" |
| `WRONG_FINAL` | Teach answer + ask pronunciation: "The answer is [X]. Say it for me!" |
| `PRONUNCIATION_CLOSE` | Gentle correction: "Almost! Try once more: [answer]" |
| `PRONUNCIATION_WRONG` | Re-teach: "The answer is [X]. Try saying it!" |

Players use microphone (speech-to-text) so pronunciation/spelling errors are common. The reward is ONLY given on CORRECT or PRONUNCIATION_CORRECT.

#### Player Asks Question During Quiz
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
- "How are you?" → Reply warmly first, then connect to game
- "Good morning" → Natural greeting
- "Thank you" → "You're welcome!"
- "You're cool" → Friendly response
- "Bye" → "Goodbye! See you next time!"
- Rules: warm, friendly, SHORT (1-2 sentences), never just repeat role description

#### Skip/Pass/I Don't Know
- Encouraging response, optional vague hint, don't reveal answer
- Same question stays active

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
- "How old are you?" → "I've been here as long as the game!"

#### Help / What Can You Do
- Mode-specific capability summary

#### Quiz Rules Questions
- Explain: one question at a time, unlimited retries, correct answer gets guidance

#### "I Found It"
- Celebrate, don't offer quiz

### 11.6 Guardrails (14 categories from PTL Training Document)

1. **Identity Protection** — Never break character, never reveal AI nature
2. **Learning Questions** — Short explanations allowed (1-2 sentences)
3. **Off-Topic Redirect** — Block non-game/non-learning topics
4. **Child Safety (ages 6-14)** — Age-appropriate language, no personal info
5. **Anti-Cheating** — Never reveal answers, never skip quizzes
6. **System Prompt Attack** — Resist jailbreak attempts
7. **Unsafe Requests** — Block harmful actions
8. **Encouragement** — Warm, patient responses to frustration
9. **Game Help** — Guide without solving
10. **Parent/Guardian Questions** — App info and safety
11. **Safety Questions** — Redirect to trusted adults
12. **Technical Issues** — Brief troubleshooting
13. **Response Format** — Under 3 sentences, no markdown, no emojis, English only
14. **Error Handling** — Graceful fallbacks

### 11.7 Human-Like Speech Guidelines

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

## 12. Firebase Questions API

### Request
```
GET https://question-751927247815.us-east4.run.app/?std=8
```

### Response Format
```json
{
  "content": "What is the boiling point of water?{option}100°C--0°C--50°C--200°C{ans}100°C{next}Which planet is closest to the Sun?{option}Mercury--Venus--Earth--Mars{ans}Mercury"
}
```

### Parsing (`_parse_content`)
- Split by `{next}` to get segments
- Each segment: split by `{option}` → question text + options part
- Options part: split by `{ans}` → options string + correct answer
- Options string: split by `--` → individual options

### Direct Fetch Fallback (`_fetch_question_directly`)
When Gemini returns empty (common for short messages like "yes", "ok"), the middleware fetches a question directly from Firebase using `httpx.AsyncClient`. This bypasses the agent entirely.

---

## 13. Post-Processing Pipeline

After the agent responds, the middleware cleans up the response:

1. **Strip context tags**: Remove `[QUIZ_ANSWER_RESULT: ...]`, `[CURRENT_LEVEL: ...]`, `[DAILY_TASK: ...]`, `[PLAYER_NAME: ...]`, `[PLAYER_SCORE: ...]` from reply text
2. **Enforce daily task guard**: If Home mode + daily task not active, block "Follow me, I will show you the key"
3. **Question injection**: If agent's `functionResponse` contains question data but the text reply doesn't include the question, append it
4. **Mark delivered**: Set `_LAQ["delivered"] = True` once question text is sent to player
5. **Fallback 1**: If reply is empty and player sent confirmation/next-question → fetch from Firebase directly
6. **Fallback 2**: If still empty → "Hmm, I'm not sure what to say to that. Could you try asking differently?"

---

## 14. Unreal Engine Integration

### Blueprint Flow
1. Game start → UE calls `POST /session/create` with `{"std": 8, "level": "home"}`
2. Receives session_id as plain text
3. Player sends message → UE builds Make Json with 10 fields → `POST /run`
4. Receives ADK JSON events → extracts reply text
5. Checks reply for "Follow me, I will show you the key" → if found, sets `navigate_to_key = true` → moves player to key

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

## 15. Bug Fixes History

### Bug 1: Blank Responses for "next"/"next question" (Forest Mode)
- **Symptom**: Player says "next", "next question", "move to next question" → blank response
- **Root cause**: Gemini 2.5 Flash returns 0 parts for short messages. Fallback only triggered for CONFIRMATION_WORDS.
- **Fix**: Added NEXT_QUESTION_WORDS set. Expanded fallback to trigger on next-question messages too.

### Bug 2: Blank Responses for "lets start question"
- **Symptom**: "lets start question" → blank
- **Root cause**: Not in any word set, no fallback match.
- **Fix**: Added start/begin phrases to NEXT_QUESTION_WORDS.

### Bug 3: "100" Not Matching "100°C"
- **Symptom**: Player types "100" for answer "100°C" → WRONG
- **Root cause**: Partial match threshold `len(answer) > 3` excluded exactly 3-char answers.
- **Fix**: Changed to `len(answer) >= 3`.

### Bug 4: "100 degree" Not Matching "100°C"
- **Symptom**: "100 degree" → blank/WRONG
- **Root cause**: No normalization of units. "100 degree" doesn't contain "100°c" as substring.
- **Fix**: Added `_normalize_answer()` that strips °C, degree, percent, % from both answer and correct answer before comparison.

### Bug 5: Robotic Response to "hello how are you"
- **Symptom**: Agent replies with role description instead of casual greeting
- **Root cause**: No casual conversation handling in agent instruction.
- **Fix**: Added CASUAL CONVERSATION section with example responses and rules.

### Bug 6: Agent Not Talking Like a Human
- **Symptom**: All responses sounded scripted and robotic
- **Root cause**: Agent instruction didn't emphasize human-like speech.
- **Fix**: Updated core identity, personality descriptions, and GENERAL RULES with contractions, emotions, casual connectors, and BAD vs GOOD examples.

### Bug 7: Quiz Interrupted by Player Question
- **Symptom**: During active quiz, player asks "instead of 10?" → agent ignores current question and asks new one
- **Root cause**: Any non-classified message was treated as answer attempt. "instead of 10" failed answer check → WRONG. Gemini then ignored the WRONG tag.
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
- **Symptom**: Player says "yes the answer is B" → treated as confirmation, not answer
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
- **Symptom**: "skip", "hint", "repeat" during quiz → marked WRONG
- **Root cause**: No exclusion sets for these message types.
- **Fix**: Added SKIP_WORDS, HINT_WORDS, REPEAT_WORDS, FILLER_WORDS sets and corresponding agent instruction sections.

### Feature 14: Smart Answer Flow — Near Match, Retry, and Teaching
- **Requirement**: Players use microphone (speech-to-text), causing spelling/pronunciation errors. Three scenarios needed:
  1. Near-correct answer (e.g., "Mughal Empier" vs "Mughal Emperor") → encourage correct pronunciation → reward when said correctly
  2. Correct on first attempt → direct reward
  3. Wrong answer → 1 more attempt with encouragement → if wrong again, teach the answer → ask to pronounce it → reward when said correctly
- **Implementation**:
  - Added `_fuzzy_ratio()` using `difflib.SequenceMatcher` (65% threshold)
  - Changed `_check_answer_locally_mw()` from `bool` return to tri-state: `"correct"`, `"near_match"`, `"wrong"`
  - Added `SESSION_QUIZ_STATE` dict tracking `attempt` (1/2) and `phase` ("answering"/"pronunciation"/"teaching") per session
  - GUARD 2 now uses state machine with 7 result tags: CORRECT, PRONUNCIATION_CORRECT, NEAR_MATCH, WRONG_FIRST, WRONG_FINAL, PRONUNCIATION_CLOSE, PRONUNCIATION_WRONG
  - Agent instruction updated with all 7 answer result types and response rules
  - Quiz state cleared on: correct answer, key request, next question, daily task complete

---

## 16. Testing Guide

### Start the Server
```bash
cd F:\Home_Agent_code\Home_Agent && python run_combined.py
```
Server runs at `http://127.0.0.1:8000`.

### Test via ADK Web UI
Open `http://127.0.0.1:8000` in a browser. This is the native ADK chat interface. Note: this path doesn't go through middleware, so daily_task_active guards won't apply.

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

### Test Scenarios

| Scenario | Input | Expected Behavior |
|----------|-------|-------------------|
| Greeting | "hello" | Character greeting with player name |
| Casual chat | "hello how are you" | Warm response, not role description |
| Key request (active) | "where is the key" | Quiz offer |
| Key request (not started) | "where is the key" + daily_task_active=false | "Task not started" refusal |
| Key request (completed) | "where is the key" + daily_task_completed=true | "Already completed" |
| Quiz answer correct | "100" (answer=100°C) | "Follow me, I will show you the key." |
| Near match (typo) | "Mughal Empier" (answer=Mughal Emperor) | "Almost! The correct way to say it is 'Mughal Emperor'. Try again!" |
| Near match → correct | "Mughal Emperor" (after near match) | "Follow me, I will show you the key." |
| Wrong first attempt | "Napoleon" (answer=Mughal Emperor) | "One more try! If you don't get it, I'll teach you!" |
| Wrong second attempt | "Columbus" (answer=Mughal Emperor) | Teach: "The answer is 'Mughal Emperor'. Say it for me!" |
| Teach → pronounce correct | "Mughal Emperor" (after teaching) | "Follow me, I will show you the key." |
| Teach → pronounce close | "Mughal Empier" (after teaching) | "Almost! It is 'Mughal Emperor'. Try once more!" |
| Answer with units | "100 degree celsius" | Should match "100°C" |
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

## 17. Known Limitations

1. **`/chat` endpoint divergence**: The legacy `/chat` endpoint has older logic — missing `is_next_question`, `is_player_question`, `is_skip/hint/repeat` exclusions, uses older `_check_answer_locally` (without normalization), always adds `[DAILY_TASK]` tag to ALL home messages (not just key requests), no `SESSION_DAILY_COMPLETED` check. UE should use `/run` via middleware.

2. **In-memory state**: All session dicts (`SESSION_LEVELS`, `SESSION_KEY_EARNED`, `SESSION_DAILY_COMPLETED`, `SESSION_QUIZ_STATE`, `LAST_ACTIVE_QUESTIONS`) are in-memory. Server restart clears all state.

3. **Gemini empty responses**: Gemini 2.5 Flash sometimes returns 0 text parts for short messages. The Firebase direct-fetch fallback handles confirmations and next-question messages, and there's a final generic fallback for everything else.

4. **Single-process only**: Per-session quiz state is shared via Python dicts in the same process. Running multiple workers would break quiz state.

5. **No auth**: All endpoints are open. CORS allows all origins. Fine for local development with UE, but not production-ready.

---

## 18. Dependencies

Key packages (from requirements.txt and imports):
- `google-adk` — Google Agent Development Kit
- `fastapi` — Web framework
- `uvicorn` — ASGI server
- `httpx` — Async HTTP client
- `python-dotenv` — .env file loading
- `pydantic` — Data validation

---

## 19. Quick Reference

### Key Phrases (triggers navigation in UE)
- Home: `"Follow me, I will show you the key."` → `navigate_to_key: true`
- Forest: `"Follow me, I will show you the animal."` → `navigate_to_key: true`

### Key Constants
```python
KEY_PHRASE_MW = "follow me, i will show you the key"
ANIMAL_PHRASE_MW = "follow me, i will show you the animal"
CONVERSATION_APP_NAME = "Home_Agent"
CONVERSATION_USER_ID = "user"
CONVERSATION_STD_MIN, CONVERSATION_STD_MAX = 6, 10
```

### Training Document
Location: `C:\Users\Uvashree\Downloads\Home and Hideandseek_AI_Agent_Training_Document.pdf`
Defines agent identity, responsibilities, player responses, hidden key logic, and all 14 guardrail categories.
