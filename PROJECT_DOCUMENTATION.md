# Home Agent — Complete Project Documentation

> This document is a handoff guide for another Claude (or developer) to fully understand, maintain, and extend this project. It covers architecture, all code, all bug fixes, and all user instructions.

---

## 1. Project Overview

| Field | Value |
|-------|-------|
| **Name** | Home Agent (Google ADK Game AI) |
| **Location** | `F:\Home_Agent_code\Home_Agent\` |
| **Language** | Python |
| **Framework** | Google ADK (Agent Development Kit) |
| **LLM Model** | `gemini-2.5-flash` |
| **Server** | Uvicorn on `http://127.0.0.1:8000` |
| **Run Command** | `cd F:\Home_Agent_code\Home_Agent && python run_combined.py` |
| **Platform** | Windows 10 Pro, shell: bash (Unix syntax) |

### What This Is

A Google ADK agent called **Home Assistant AI** for an **Unreal Engine** game. It is NOT a quiz bot — it is a **game character** that lives in a home map (or forest map). The agent helps the player with tasks and uses quiz questions as a game mechanic to guide players to hidden keys/animals.

### Two Game Modes

| Mode | Level String | Character | Goal |
|------|-------------|-----------|------|
| **Home** | `"home"` | Home Assistant AI | Help with cooking, gardening, cleaning, pool. Guide player to hidden **key** via quiz. |
| **Forest** | `"foresthideandseek"` | Forest Explorer AI | Help explore forest. Guide player to hidden **animals** via quiz. |

---

## 2. File Structure

```
F:\Home_Agent_code\Home_Agent\
├── run_combined.py                 # Main server (~890 lines) — middleware, endpoints, all guards
├── question_server.py              # Questions proxy router (for Postman testing)
├── requirements.txt                # Dependencies
├── guardrail_questions.md          # QA reference for testing guardrails
├── start_web.bat / start_web.ps1   # Launchers
├── .claude/
│   └── launch.json                 # Claude Preview config
└── Home_Agent/
    ├── agent.py                    # Agent definition + full instruction (~300 lines)
    ├── .env                        # API keys, Firebase URL, default grade
    ├── __init__.py
    └── tools/
        ├── __init__.py             # Exports: fetch_questions, get_level, get_user_std, set_user_std, check_answer, get_daily_task_status
        └── question_api.py         # All tool implementations (~320 lines)
```

---

## 3. Environment Variables (`Home_Agent/.env`)

```env
GOOGLE_API_KEY=AIzaSyDyQGkwBuBccdYvcOLoFHsixxC-b35oxg0
QUESTION_API_STD=8
QUESTIONS_SOURCE_API_URL=https://question-751927247815.us-east4.run.app/
QUESTIONS_SOURCE_API_METHOD=GET
```

| Variable | Purpose |
|----------|---------|
| `GOOGLE_API_KEY` | Gemini API key for the agent |
| `QUESTION_API_STD` | Default grade (standard) for questions |
| `QUESTIONS_SOURCE_API_URL` | Firebase questions API endpoint |
| `QUESTIONS_SOURCE_API_METHOD` | `GET` (query params) or `POST` (JSON body) |

---

## 4. Agent Identity & Instruction (`Home_Agent/agent.py`)

### Agent Definition (line 293)

```python
root_agent = Agent(
    model='gemini-2.5-flash',
    name='root_agent',
    description='Game AI that adapts to current level...',
    instruction=AGENT_INSTRUCTION,
    tools=[fetch_questions, get_level, get_user_std, set_user_std, check_answer, get_daily_task_status],
)
```

### Instruction Summary (280 lines)

The instruction defines two modes based on the `SYSTEM SETUP` message sent at session creation:

**HOME MODE:**
- Identity: Home Assistant AI — polite, helpful, calm
- Tasks: cooking, gardening, trash cleaning, pool cleaning, bringing water to Miss Lilly
- Key quest: When player asks "where is the key" AND daily task is active:
  1. Say intro: "I will ask you one question. If you answer correctly, I will show you the key. But remember, you will earn 5 gold coins instead of 10."
  2. WAIT for player to say yes/ready
  3. Call `fetch_questions()` to get a question
  4. Present question with options
  5. Correct answer: "Follow me, I will show you the key." (exact phrase, triggers `navigate_to_key: true`)
  6. Wrong answer: "Try again, you can do it."
- Daily task status: MUST check `[DAILY_TASK: ACTIVE/NOT_STARTED]` tags before offering quiz

**FOREST MODE:**
- Identity: Forest Explorer AI — adventurous, nature-loving
- Tasks: exploring forest, finding hidden animals
- Animal quest: Same flow as key quest but for animals
  - Intro: "I will show you the animal. But you need to answer a question from the syllabus. Do you want to try?"
  - Correct: "Follow me, I will show you the animal."
  - Wrong: "Try again, you can do it."

**SHARED RULES:**
- Answer validation via `[QUIZ_ANSWER_RESULT]` tags (server checks answers, not the LLM)
- Grade handling via `set_user_std()` / `get_user_std()`
- 14 guardrail categories: identity protection, child safety (ages 6-14), anti-cheating, system prompt attack defense, off-topic redirect, learning questions allowed, encouragement, etc.
- Response format: under 3 sentences, no markdown, no emojis, English only

---

## 5. Tools (`Home_Agent/tools/question_api.py`)

### Shared State

```python
LAST_ACTIVE_QUESTION: dict[str, Any] = {}
# Structure when active:
# {
#     "correct_answer": "Verb",
#     "options": ["Noun", "Pronoun", "Verb", "Adjective"],
#     "active": True,
#     "delivered": False  # True once question text is sent to player
# }
```

### Tool: `fetch_questions(std, subject, topic, tool_context)`

1. Resolves grade from: parameter > session state `user:std` > env `QUESTION_API_STD`
2. Calls Firebase API directly via GET (not through local `/questions` to avoid deadlock)
3. Parses response using `_parse_content()` (format: `question{option}A--B--C--D{ans}answer{next}...`)
4. Tracks asked questions in session state to prevent repeats
5. Stores chosen question in both session state AND `LAST_ACTIVE_QUESTION` dict
6. Sets `active=True`, `delivered=False`
7. Returns `{"question": "...", "options": [...], "correct_answer": "..."}`

### Tool: `check_answer(player_answer, tool_context)`

Server-side answer checking (middleware) is the PRIMARY check. This tool is a fallback for when the agent calls it directly. Uses identical logic to `_check_answer_locally_mw()`.

### Tool: `get_level(tool_context)`

Returns `{"level": "home"}` or `{"level": "foresthideandseek"}` from session state.

### Tool: `set_user_std(std, tool_context)`

Saves player's grade to session state. Returns confirmation message.

### Tool: `get_user_std(tool_context)`

Returns current grade from session state (default: 8).

### Tool: `get_daily_task_status(tool_context)`

Returns `{"daily_task_active": true/false}` from session state.

### Helper: `_parse_content(content)`

Parses Firebase response format:
```
What is gravity?{option}A force--A color--A sound--A shape{ans}A force{next}What is...
```
Returns list of `{"question", "options", "correct_answer"}` dicts.

---

## 6. API Endpoints

### POST `/session/create`

Creates a new ADK session and stores the level.

**Request:**
```json
{"std": 8, "level": "home"}
```

**Response:** Plain text session ID (e.g., `abc-123-xyz`)

**What it does:**
1. Creates ADK session via internal API
2. Stores `SESSION_LEVELS[session_id] = level`
3. Sends SYSTEM SETUP message to agent: `"SYSTEM SETUP: The current game level is '{level}'. Act accordingly..."`

### POST `/chat`

Simple chat endpoint (used for Postman testing, not by UE Blueprint).

**Request:**
```json
{"session_id": "abc-123", "message": "where is the key", "daily_task_active": true}
```

**Response:** Plain text reply from agent.

**Has its own guards:** greeting intercept, daily task check, answer check, question extraction, fallback.

### POST `/run` (via ASGI Middleware)

The primary endpoint used by the **Unreal Engine Blueprint**. The `DailyTaskRunMiddleware` intercepts this.

**Request (from UE Blueprint):**
```json
{
  "app_name": "Home_Agent",
  "user_id": "user",
  "session_id": "abc-123",
  "new_message": {"role": "user", "parts": [{"text": "hello"}]},
  "streaming": false,
  "daily_task_active": "true",
  "level": "home",
  "daily_task_completed": "false"
}
```

**Fields added by UE Blueprint (stripped by middleware before forwarding to ADK):**

| Field | Type | Purpose |
|-------|------|---------|
| `daily_task_active` | String `"true"`/`"false"` | Whether the daily task has started |
| `level` | String `"home"` or `"ForestHideAndSeek"` | Current game level |
| `daily_task_completed` | String `"true"`/`"false"` | Whether the daily task is fully done |

**Response:** ADK JSON events array containing agent reply.

### POST `/questions`

Proxy to Firebase for Postman testing. NOT used by the agent (agent calls Firebase directly).

### GET `/`

ADK Web UI for browser testing.

---

## 7. Middleware — `DailyTaskRunMiddleware` (the core logic)

This ASGI middleware intercepts ALL `POST /run` requests. It only activates when `daily_task_active` is present in the body (meaning it came from UE, not from ADK Web UI or `/chat`).

### Field Extraction (before guards run)

1. **`daily_task_active`** — extracted and converted to bool. Checks keys: `daily_task_active`, `Daily Task Active`, `dailyTaskActive`, `Daily_Task_Active`
2. **`level`** — extracted, lowercased, stored in `SESSION_LEVELS[session_id]`. Checks keys: `level`, `Level`, `level_name`, `Level Name`
3. **`daily_task_completed`** — extracted and converted to bool. If `True`, clears `SESSION_KEY_EARNED[session_id]` to prevent stale state.
4. All three fields are **popped** from the body so ADK doesn't see them.

### Guard Execution Order

```
GUARD 0      → Greeting intercept (level-based character greeting)
GUARD 0.5    → Daily task completed → "no key in the home"
GUARD 0.75   → Key already earned → "Follow me" (repeat navigate)
GUARD 1      → Daily task not started → refusal
GUARD 2      → Server-side answer check (correct/wrong prefix)
─── Forward to ADK agent ───
Post-process  → Strip tags, extract question, fallback fetch
```

### GUARD 0: Greeting Intercept

**Condition:** Message is a greeting word (`hello`, `hi`, `hey`, `hii`, `helo`, `greetings`, `howdy`, `sup`)

**Action:**
- Home level → "Hello! I am the Home Assistant AI, ready to help you in your home."
- Forest level → "Hello! I am the Forest Explorer AI, ready to guide you through this forest and help you find hidden animals!"

**Why:** Gemini 2.5 Flash sometimes responds as wrong character. This ensures correct greeting.

### GUARD 0.5: Daily Task Completed

**Condition:** `level == "home"` AND `daily_completed == True` AND message contains key request words

**Action:** "You have already completed the daily task. There is no key in the home. Great job!"

**Why:** When the player finishes the daily task, there's no key to find anymore.

### GUARD 0.75: Key Already Earned

**Condition:** `daily_completed is NOT True` AND `SESSION_KEY_EARNED[session_id] == True` AND message contains key request words

**Action:** "Follow me, I will show you the key."

**Why:** If the player already answered the quiz correctly but asks for the key again, repeat the navigate response. This guard does NOT fire when daily_task_completed is true.

### GUARD 1: Daily Task Not Started

**Condition:** `level == "home"` AND `daily_active == False` AND message contains key request words

**Action:** "The daily task has not started yet. Start the daily task first, then I can help you find the key!"

**Why:** Player can't access the key quest until the daily task begins.

### GUARD 2: Server-Side Answer Check

**Condition:** `LAST_ACTIVE_QUESTION["active"] == True` AND `LAST_ACTIVE_QUESTION["delivered"] == True` AND message is NOT a key request AND message is NOT a confirmation word

**Action:**
- Checks answer using `_check_answer_locally_mw()`
- **Correct:** Prepends `[QUIZ_ANSWER_RESULT: ... CORRECT ...]` to message, sets `SESSION_KEY_EARNED[session_id] = True`, clears active question
- **Wrong:** Prepends `[QUIZ_ANSWER_RESULT: ... WRONG ...]` to message, keeps question active for retry

**Special cases:**
- Clears active question when `daily_task_active=false` (HOME mode only, not Forest)
- Clears active question when player asks for key again (fresh quiz flow)

### Key Request Words

```python
KEY_REQUEST_WORDS_MW = ["key", "find the key", "where is the key", "help me find", "show me the key", "help key"]
```

### Confirmation Words

```python
CONFIRMATION_WORDS = {
    "yes", "ok", "okay", "sure", "ready", "yeah", "yep", "yea", "ya",
    "ok ask me", "ask me", "yes ask me", "yes please", "go ahead",
    "yes please ask me the question", "ok ask me the question",
    "i am ready", "im ready", "i'm ready", "go", "alright",
    "yes please ask me", "ask me the question",
}
```

### Post-Processing (after ADK responds)

1. Parse ADK JSON events
2. Strip context tags: `[QUIZ_ANSWER_RESULT: ...]`, `[CURRENT_LEVEL: ...]`, `[DAILY_TASK: ...]`
3. Scan `functionResponse` events for question data
4. If question found but not in visible text: inject question + options into response
5. Mark `LAST_ACTIVE_QUESTION["delivered"] = True`
6. **FALLBACK:** If agent returned empty (0 parts) for a confirmation message, call `_fetch_question_directly()` to get question from Firebase server-side

### Message Enrichment

Before forwarding to ADK, the middleware adds tags to the player's message:
```
[QUIZ_ANSWER_RESULT: ...] [DAILY_TASK: ACTIVE] [CURRENT_LEVEL: home] original message
```

---

## 8. Answer Checking Logic (`_check_answer_locally_mw`)

The function handles 4 answer formats:

### Format 1: Letter only — `"C"`, `"c"`, `"B)"`

```python
cleaned = answer.replace("option", "").replace(")", "").replace(".", "").strip()
# "c" → check letter_map["c"] → if it matches correct answer → True
```

### Format 2: Option + letter — `"Option C"`, `"option B"`

```python
# "option c" → remove "option" → "c" → letter_map lookup
```

### Format 3: Option + letter + text — `"option C: Verb"`

```python
# Letter check may fail due to length → falls through to partial match
# "verb" in "option c: verb" → True (via partial match)
```

### Format 4: Full answer text — `"Verb"`, `"A letter is written by her."`

```python
# Direct match: answer == correct → True
```

### Edge Case Warning

Partial match (`answer in correct`) can cause false positives if a wrong answer is a substring of the correct answer. Example: `"noun" in "pronoun"` → True. This is rare with real Firebase questions.

---

## 9. Firebase Questions API

**URL:** `https://question-751927247815.us-east4.run.app/`
**Method:** GET with query param `?std=8`

**Response format:**
```json
{
  "content": "What is the SI unit of force?{option}Joule--Watt--Newton--Pascal{ans}Newton{next}Which part of speech...{option}Noun--Pronoun--Verb--Adjective{ans}Verb{next}..."
}
```

**Parsing:** `_parse_content()` splits on `{next}`, then `{option}`, then `{ans}`, then `--` for options.

**Important:** The `fetch_questions` tool calls Firebase DIRECTLY via GET (not through the local `/questions` endpoint) to avoid self-referencing deadlock.

---

## 10. Unreal Engine Blueprint Integration

### Blueprint: `BP_HomeNPCAgent`

The Blueprint uses a **Make Json** node with 8 fields to build the `/run` request body:

| Index | Field Name | Type | Source Variable |
|-------|-----------|------|-----------------|
| 0 | `app_name` | String | `"Home_Agent"` (hardcoded) |
| 1 | `user_id` | String | User ID variable |
| 2 | `session_id` | String | Session ID variable |
| 3 | `new_message` | Object | Make Json (Role: "user", Parts: message array) |
| 4 | `streaming` | Boolean | `false` (unchecked) |
| 5 | `daily_task_active` | String | `Daily Task Active` variable |
| 6 | `level` | String | `Level Name` variable |
| 7 | `daily_task_completed` | String | `Home Mission C ?` / `Dailymissioncomplete?` variable |

### Session Creation

The Blueprint calls `POST /apps/Home_Agent/users/{user_id}/sessions/` (ADK's built-in route) to create sessions. The level is passed in the `/run` body on every message, NOT in session creation.

### Response Parsing

The Blueprint receives ADK JSON events and extracts the agent's text reply. It checks for "Follow me, I will show you the key" in the text to trigger navigation (equivalent to `navigate_to_key: true`).

---

## 11. In-Memory State (Module-Level Dicts)

```python
SESSION_LEVELS: dict[str, str] = {}        # session_id → "home" / "foresthideandseek"
SESSION_KEY_EARNED: dict[str, bool] = {}    # session_id → True (player answered quiz correctly)
LAST_ACTIVE_QUESTION: dict[str, Any] = {}   # Shared between fetch_questions tool and middleware
```

**Important:** These are in-memory and reset when the server restarts. `LAST_ACTIVE_QUESTION` is a single global dict (not per-session), so concurrent sessions can overwrite each other's question data.

---

## 12. All Bug Fixes & Changes Made Across Sessions

### Bug 1: Empty Response on Confirmation

**Problem:** When the player says "yes" / "ok" after the quiz offer, Gemini 2.5 Flash returns empty (0 parts, finishReason: STOP, 0 output tokens).

**Root Cause:** Gemini 2.5 Flash model consistently fails to respond to short confirmation messages.

**Fix:** Added server-side fallback `_fetch_question_directly()` in `run_combined.py`. When the agent returns empty for a confirmation message, the server calls Firebase directly and returns the question to the player.

---

### Bug 2: Answer Check Triggered Too Early

**Problem:** `LAST_ACTIVE_QUESTION` was set to `active=True` when `fetch_questions` ran, but BEFORE the question text was shown to the player. So "ok ask me" was treated as a wrong answer.

**Root Cause:** No distinction between "question fetched" and "question delivered to player".

**Fix:** Added `delivered` flag to `LAST_ACTIVE_QUESTION`. Set to `False` in `fetch_questions()`, set to `True` only after the question text is included in the response. GUARD 2 only checks answers when `delivered=True`.

---

### Bug 3: Retry Broken After Wrong Answer

**Problem:** After a wrong answer, `LAST_ACTIVE_QUESTION["active"]` was cleared, preventing the player from retrying.

**Root Cause:** Active flag was cleared on ALL answers (correct and wrong).

**Fix:** Only clear `active` on CORRECT answers. On wrong answers, keep `active=True` so the player can try again.

---

### Bug 4: Sticky "Try Again" — All Messages Treated as Answers

**Problem:** After a wrong answer, ALL subsequent messages (including "where is the key", "Now", regular chat) got "Try again, you can do it."

**Root Cause:** `LAST_ACTIVE_QUESTION["active"]` stayed `True` forever after a wrong answer, treating every single message as an answer attempt.

**Fix (3 parts):**
1. Skip answer check for key request words and confirmation words
2. Clear active question when `daily_task_active=false` (Home mode only)
3. Reset quiz when player asks for key again while a question is active

---

### Bug 5: Wrong Character Greeting in Forest Mode

**Problem:** In ForestHideAndSeek level, the agent responded as "Home Assistant AI" instead of "Forest Explorer AI".

**Root Cause:** The greeting intercept only existed in `/chat` endpoint, not in the middleware that processes `/run` requests. The Blueprint uses `/run`.

**Fix:** Added GUARD 0 (greeting intercept) to the middleware that checks `SESSION_LEVELS` for the session's level and returns the correct character greeting.

---

### Bug 6: Forest Quiz Broken — daily_task_active Clearing

**Problem:** In Forest mode, ALL quiz answers were marked wrong. The player tried all 4 options — all returned "Try again, you can do it."

**Root Cause:** Forest mode always sends `daily_task_active=false` (no daily mission concept). The middleware cleared `LAST_ACTIVE_QUESTION` whenever `daily_task_active=false`, killing the answer check. The message then went to the LLM which did its own (unreliable) answer checking.

**Fix:** Only clear active question based on `daily_task_active` in **HOME mode**. Forest mode quiz works independently of `daily_task_active`.

```python
# Before (broken):
if not daily_active and _LAQ.get("active"):

# After (fixed):
if level == "home" and not daily_active and _LAQ.get("active"):
```

---

### Bug 7: "Follow me" After Daily Task Completed

**Problem:** After completing the daily task, asking "where is the key" returned "Follow me, I will show you the key" instead of "You have already completed the daily task. There is no key in the home."

**Root Cause:** The player had previously answered the quiz correctly, setting `SESSION_KEY_EARNED[session_id] = True`. When `daily_task_completed=true` arrived later, GUARD 0.75 (key earned) fired before GUARD 0.5 (task completed) could catch it, because the stale key-earned flag was never cleared.

**Fix (2 parts):**
1. Clear `SESSION_KEY_EARNED[session_id]` whenever `daily_task_completed=true` arrives
2. Add `not daily_completed` condition to GUARD 0.75 as safety net

---

### Feature: Level from `/run` Body

**Problem:** The UE Blueprint didn't call `/session/create` with the level. Instead it passed the level in the `/run` body.

**Fix:** Added level extraction in the middleware — reads `level` from the `/run` body, lowercases it, and stores in `SESSION_LEVELS[session_id]`.

---

### Feature: `daily_task_completed` Field

**Problem:** When the player completes the daily task, the key no longer exists. The agent needed to know this.

**Fix:** Added `daily_task_completed` field extraction in middleware + GUARD 0.5 that returns "You have already completed the daily task. There is no key in the home. Great job!" when this field is `true` and the player asks about the key.

---

### Feature: `SESSION_KEY_EARNED` Tracking

**Problem:** After answering the quiz correctly and getting "Follow me, I will show you the key", if the player asks for the key again, they got "The daily task has not started yet" (because `daily_task_active` was now `false`).

**Fix:** Added `SESSION_KEY_EARNED` dict that tracks which sessions have earned the key. GUARD 0.75 checks this and repeats the navigate response if the key was already earned.

---

### Bug 8: "Where is the mango" Blocked by Daily Task Check

**Problem:** Player asked "where is the mango" (a normal gameplay question). The agent refused, saying the daily task hadn't started — treating a mango question like a key request.

**Root Cause:** The `[DAILY_TASK: NOT_STARTED]` tag was being added to ALL messages, not just key-related ones. The agent's instruction said to block everything when daily task is not started.

**Fix:** Only add the `[DAILY_TASK: ...]` tag for key-related messages (when `is_key_related` is True). Normal conversation messages get no daily task tag, so the agent responds freely.

```python
# Before (broken): tag added to ALL messages
task_tag = "[DAILY_TASK: ACTIVE] " if daily_active else "[DAILY_TASK: NOT_STARTED] "

# After (fixed): tag only for key-related messages
is_key_related = any(w in msg_lower for w in KEY_REQUEST_WORDS_MW)
task_tag = ""
if is_key_related:
    task_tag = "[DAILY_TASK: ACTIVE] " if daily_active else "[DAILY_TASK: NOT_STARTED] "
```

---

### Bug 9: Blank Responses for "Next Question" and "Start Question" Requests

**Problem:** In Forest Hide and Seek mode, after answering a quiz correctly, messages like "next", "next question", "move to next question", "help me to find next animal", and "lets start question" returned completely blank/empty responses (no text).

**Root Cause:** Gemini 2.5 Flash returns 0 parts (empty response) for these short messages. The existing fallback `_fetch_question_directly()` only triggered for `CONFIRMATION_WORDS` (like "yes", "ok", "sure"). Quiz navigation phrases were NOT in that set, so the fallback never fired.

**Fix (3 parts):**
1. Added `NEXT_QUESTION_WORDS` set with "next", "next question", "another question", "lets start question", "start quiz", "begin question", etc.
2. Expanded the empty-response fallback keywords to also check for "next", "another", "start question", "begin question", "start quiz"
3. Added `is_next_question` exclusion in GUARD 2 so these messages aren't treated as answer attempts

---

### Bug 10: "100" Not Matching "100°C" in Answer Check

**Problem:** Player answered "100" to "What is the boiling point of water?" where the correct answer is "100°C" (Option C). The answer was treated as wrong because the partial match check required `len(answer) > 3` but "100" is exactly 3 characters.

**Root Cause:** Partial matching threshold was `> 3` (4+ chars) to avoid single letters matching inside long option texts. But this was too strict — "100" (3 chars) couldn't match "100°C".

**Fix:** Changed threshold from `> 3` to `>= 3`. This allows 3-character answers like "100" to partially match "100°c" while still preventing 1-2 character false positives.

```python
# Before:
if len(answer) > 3 and (correct in answer or answer in correct):

# After:
if len(answer) >= 3 and (correct in answer or answer in correct):
```

---

### Feature: Player Name (`player_name`)

**Problem:** The user wanted the agent to address the player by name.

**Fix:** Added `player_name` field extraction in middleware (checks keys: `player_name`, `Player Name`, `playerName`, etc.). Adds `[PLAYER_NAME: <name>]` tag to messages. Agent instruction updated to use the player's name in greetings and responses.

---

### Feature: Player Score (`player_score`)

**Problem:** The user wanted the agent to know the player's coin score.

**Fix:** Added `player_score` field extraction in middleware (checks keys: `player_score`, `Player Score`, `playerScore`, etc.). Adds `[PLAYER_SCORE: <score>]` tag to messages. Agent instruction updated to reference the player's score when relevant.

---

## 13. All User Instructions Given

The user (game developer) provided these directives across sessions:

1. **"The agent is a home helper, NOT a quiz bot"** — The agent's primary role is as a game character with personality, not a question-asking machine.

2. **"When daily task is not active, block key requests"** — The server must prevent the agent from offering the quiz or revealing key location when the daily task hasn't started.

3. **"Correct answer reply must be exactly 'Follow me, I will show you the key'"** — This exact phrase triggers the Unreal Engine Blueprint to set `navigate_to_key: true` and move the player.

4. **"Wrong answer reply must be 'Try again, you can do it'"** — Keep the quiz active for retry.

5. **"Forest mode uses 'Follow me, I will show you the animal'"** — Different phrase for forest mode.

6. **"I pass the level name through the /run body"** — The Blueprint sends `level` as a field in the Make Json, not via `/session/create`.

7. **"daily_task_completed is sent as a string true/false"** — The Blueprint sends boolean values as strings through the Make Json String type pin.

8. **"When daily task is completed, tell the player there is no key"** — New feature: GUARD 0.5 blocks key requests when the daily task is finished.

9. **"The Blueprint uses /run endpoint, not /chat"** — All middleware protection must work on the `/run` path, not just `/chat`.

10. **"Fix the Forest greeting — it says Home Assistant instead of Forest Explorer"** — Greeting intercept needed in middleware, not just in `/chat`.

11. **"Fix: after wrong answer, all messages get 'Try again'"** — The sticky answer check bug where non-answer messages were treated as quiz answers.

12. **"Analyze answer formats: A, Option A, option A: xxxx, xxxx"** — Confirmed all 4 formats work via the `_check_answer_locally_mw()` function.

---

## 14. Server-Side Fallback: `_fetch_question_directly()`

When Gemini 2.5 Flash returns empty (0 parts) for short confirmations like "yes"/"ok", this function calls Firebase directly:

1. Reads API URL and method from environment
2. Calls Firebase via GET with `?std=8`
3. Parses response using `_parse_content()`
4. Selects random question
5. Stores in `LAST_ACTIVE_QUESTION` with `delivered=True`
6. Returns formatted question string: `"Question: ... Options: A) ... B) ... C) ... D) ..."`

---

## 15. Data Flow Diagrams

### Session Creation Flow
```
UE Blueprint
  │
  ├─ POST /apps/Home_Agent/users/{user}/sessions/  (ADK built-in)
  │    └─ Creates ADK session, returns session_id
  │
  └─ First /run call includes level field
       └─ Middleware stores SESSION_LEVELS[session_id] = level
```

### Chat Message Flow (via /run + Middleware)
```
UE Blueprint sends POST /run with:
  app_name, user_id, session_id, new_message,
  streaming, daily_task_active, level, daily_task_completed
         │
         ▼
┌─ DailyTaskRunMiddleware ──────────────────────┐
│                                                │
│  Extract & pop: daily_task_active, level,      │
│                 daily_task_completed            │
│                                                │
│  GUARD 0:   Greeting? → Character greeting     │
│  GUARD 0.5: Task done? → "No key in home"      │
│  GUARD 0.75: Key earned? → "Follow me"         │
│  GUARD 1:   Task not started? → Refusal        │
│  GUARD 2:   Active quiz? → Check answer        │
│                                                │
│  Enrich message with tags:                     │
│  [QUIZ_ANSWER_RESULT] [DAILY_TASK] [LEVEL]     │
│                                                │
│  Forward to ADK agent (gemini-2.5-flash)       │
│                                                │
│  Post-process response:                        │
│  - Strip tags                                  │
│  - Extract question from functionResponse      │
│  - Fallback: _fetch_question_directly()        │
│  - Mark question as delivered                  │
│                                                │
│  Return cleaned ADK JSON events                │
└────────────────────────────────────────────────┘
         │
         ▼
UE Blueprint parses response, checks for
"Follow me" to trigger navigation
```

### Quiz Flow (Complete)
```
1. Player: "where is the key"
   └─ GUARD 1 checks daily_task_active
   └─ If active: forwards to agent
   └─ Agent: "I will ask you one question..."

2. Player: "yes"
   └─ GUARD 2: confirmation word, skips answer check
   └─ Agent calls fetch_questions() → Firebase
   └─ OR: Agent returns empty → fallback _fetch_question_directly()
   └─ Response: "Question: ... Options: A) B) C) D)"
   └─ LAST_ACTIVE_QUESTION set: active=True, delivered=True

3. Player: "B" or "Verb" or "option B" or "option B: Verb"
   └─ GUARD 2: active=True, delivered=True
   └─ _check_answer_locally_mw() → correct/wrong
   └─ Correct: prefix [QUIZ_ANSWER_RESULT: CORRECT], SESSION_KEY_EARNED=True
   └─ Wrong: prefix [QUIZ_ANSWER_RESULT: WRONG], keep active
   └─ Agent responds: "Follow me..." or "Try again..."

4. Player asks for key again:
   └─ If key earned: GUARD 0.75 → "Follow me..."
   └─ If task completed: GUARD 0.5 → "No key in home"
   └─ If task not started: GUARD 1 → refusal
```

---

## 16. Dependencies

```
google-adk          # Google Agent Development Kit
python-dotenv       # Environment variable loading
fastapi             # Web framework
uvicorn[standard]   # ASGI server
httpx               # Async HTTP client (used in middleware)
```

---

## 17. Testing

### Run Server
```bash
cd F:\Home_Agent_code\Home_Agent && python run_combined.py
```

### Test Session Creation
```bash
curl -s http://127.0.0.1:8000/session/create -X POST \
  -H "Content-Type: application/json" \
  -d '{"std": 8, "level": "home"}'
# Returns: plain text session_id
```

### Test Chat via /run (simulating UE Blueprint)
```bash
curl -s -X POST http://127.0.0.1:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "Home_Agent",
    "user_id": "user",
    "session_id": "<SESSION_ID>",
    "new_message": {"role": "user", "parts": [{"text": "hello"}]},
    "streaming": false,
    "daily_task_active": "true",
    "level": "home",
    "daily_task_completed": "false"
  }'
```

### Test Scenarios

| Scenario | daily_task_active | daily_task_completed | Expected Response |
|----------|-------------------|---------------------|-------------------|
| Greeting (home) | any | any | "Hello! I am the Home Assistant AI..." |
| Greeting (forest) | any | any | "Hello! I am the Forest Explorer AI..." |
| Key request, task not started | `false` | `false` | "The daily task has not started yet..." |
| Key request, task active | `true` | `false` | Quiz offer: "I will ask you one question..." |
| Key request, task completed | any | `true` | "You have already completed the daily task..." |
| Key request, key already earned | any | `false` | "Follow me, I will show you the key." |
| Correct quiz answer | `true` | `false` | "Follow me, I will show you the key." |
| Wrong quiz answer | `true` | `false` | "Try again, you can do it." |

### ADK Web UI

Open `http://127.0.0.1:8000` in browser for interactive testing (no middleware protection — goes directly to agent).

---

## 18. Known Limitations

1. **`LAST_ACTIVE_QUESTION` is global** — not per-session. Concurrent sessions can overwrite each other's question data. Single-player is fine.

2. **Partial match edge case** — If a wrong answer is a substring of the correct answer (e.g., "noun" in "pronoun"), it may be incorrectly accepted. Rare with real Firebase questions.

3. **In-memory state** — `SESSION_LEVELS`, `SESSION_KEY_EARNED`, `LAST_ACTIVE_QUESTION` all reset on server restart.

4. **Gemini 2.5 Flash empty responses** — The model sometimes returns 0 parts for short messages. The server-side fallback handles this for quiz confirmations, but it could happen for other short messages too.

5. **String booleans from UE** — All boolean values from UE Blueprint arrive as strings (`"true"`/`"false"`), not JSON booleans. The server handles conversion for `daily_task_active` and `daily_task_completed`.

---

## 19. Training Document Reference

The original training document that defines agent identity, responsibilities, player responses, and hidden key logic is at:

`C:\Users\Uvashree\Downloads\Home and Hideandseek_AI_Agent_Training_Document.pdf`

This PDF was used to write the agent instruction in `agent.py`.
