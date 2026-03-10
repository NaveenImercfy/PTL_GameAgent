# Home Agent AI

A **Google ADK (Agent Development Kit)** game AI for an Unreal Engine game. The agent acts as an in-game character that helps players, teaches them through quiz questions, and guides them to hidden items when they answer correctly.

## What It Does

The agent adapts its personality based on the current game level:

| Level | Character | Role |
|-------|-----------|------|
| `home` | Home Assistant AI | Helps with cooking, gardening, cleaning, pool — guides players to a hidden key via quiz |
| `foresthideandseek` | Forest Explorer AI | Helps players find hidden animals in the forest via quiz |

**Hidden item flow:**
1. Player asks for help finding the key/animal
2. Agent asks one question fetched from Firebase
3. Correct answer → `"Follow me, I will show you the key/animal."` + `navigate_to_key: true`
4. Wrong answer → `"Try again, you can do it."`

---

## Project Structure

```
Home_Agent/
├── run_combined.py          # Main server — all endpoints on port 8000
├── question_server.py       # /questions proxy endpoint
├── requirements.txt
├── start_web.bat            # Windows launcher
├── start_web.ps1            # PowerShell launcher
└── Home_Agent/
    ├── agent.py             # Agent identity, personality, and instructions
    ├── .env                 # API keys and config
    └── tools/
        ├── __init__.py      # Tool exports
        └── question_api.py  # fetch_questions, set_user_std, check_answer, etc.
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Edit `Home_Agent/.env`:

```env
GOOGLE_API_KEY=your_google_api_key_here
QUESTION_API_STD=8
QUESTIONS_SOURCE_API_URL=https://question-751927247815.us-east4.run.app/
QUESTIONS_SOURCE_API_METHOD=GET
```

### 3. Run the server

```bash
cd F:\Home_Agent_code\Home_Agent
python run_combined.py
```

Or use the launchers:
- Double-click `start_web.bat`
- Run `.\start_web.ps1` in PowerShell

Server runs at: **http://127.0.0.1:8000**

ADK Web UI (for testing): **http://127.0.0.1:8000**

---

## API Endpoints

### Unreal Engine Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/session/create` | POST | Create a new session — returns `session_id` as plain text |
| `/chat` | POST | Send a message, get agent reply + navigation flag |

#### POST `/session/create`
```json
{ "std": 8, "level": "home" }
```
Response: plain text `session_id` (e.g. `abc-123-xyz`)

#### POST `/chat`
```json
{
  "session_id": "abc-123-xyz",
  "message": "where is the key",
  "daily_task_active": true
}
```
Response: plain text `true|||Follow me, I will show you the key.`

Format: `<navigate_to_key>|||<reply text>`

Split on `|||` in Unreal Engine Blueprints to get both values.

---

### Other Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/questions` | POST | Proxy to Firebase questions API |
| `/conversation/start` | POST | Create session with grade pre-set |
| `/run` | POST | ADK non-streaming agent call |
| `/run_sse` | POST | ADK streaming agent call |
| `/` | GET | ADK Web UI |

#### POST `/questions`
```json
{ "std": 8 }
```

#### POST `/conversation/start`
```json
{ "std": 8 }
```

---

## Unreal Engine Integration

- Uses **Blueprints** (no C++ required)
- CORS enabled for all origins
- Flow:
  1. On game start → `POST /session/create` with `std` and `level`
  2. Store the returned `session_id`
  3. On each player message → `POST /chat` with `session_id`, `message`, and `daily_task_active`
  4. Split response on `|||` → check first part for `true`/`false`
  5. If `true` → trigger navigation to key/animal location

---

## Daily Task Guard (Home Level)

In the home level, the agent will NOT help find the key unless the daily task has been started. Send `"daily_task_active": true` in the `/chat` request body when the player has triggered the daily task collider in-game.

- `daily_task_active: false` + key request → `"The daily task has not started yet. Start the daily task first, then I can help you find the key!"`
- `daily_task_active: true` + key request → agent asks a quiz question

---

## Firebase Questions API

- **URL:** `https://question-751927247815.us-east4.run.app/`
- **Method:** GET with `?std=8`
- **Response format:** `question{option}A--B--C--D{ans}answer{next}...`
- The agent fetches questions directly from Firebase (not through the local `/questions` endpoint) to avoid circular request deadlocks.

---

## Agent Model

- **Model:** `gemini-2.5-flash`
- **Framework:** Google ADK

---

## Testing with curl

```bash
# Health check
curl http://localhost:8000/questions/health

# Fetch questions
curl -X POST http://localhost:8000/questions \
  -H "Content-Type: application/json" \
  -d "{\"std\": 8}"

# Create session
curl -X POST http://localhost:8000/session/create \
  -H "Content-Type: application/json" \
  -d "{\"std\": 8, \"level\": \"home\"}"

# Send chat message
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"YOUR_SESSION_ID\", \"message\": \"hello\", \"daily_task_active\": false}"
```
