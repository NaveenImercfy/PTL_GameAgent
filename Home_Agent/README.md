# Home Agent (Quiz)

## One command (recommended): ADK + questions on port 8000

**Important:** For ADK and the questions API together on port 8000, you must run `python run_combined.py` (or `uvicorn run_combined:app --reload --port 8000`). Do **not** run `adk web` alone if you want POST /questions to work — `adk web` does not include the /questions route, so you will get 404.

From the project root (`d:\Home_Agent`), run a single server that serves both the ADK Web UI and the questions API:

```bash
python run_combined.py
```

Or double-click `start_web.bat` (or run `.\start_web.ps1` in PowerShell) from the project root.

Or with reload:

```bash
uvicorn run_combined:app --reload --port 8000
```

- **ADK Web UI:** http://127.0.0.1:8000
- **Questions API:** POST http://localhost:8000/questions (body e.g. `{"std": 8}`), GET http://localhost:8000/questions/health
- **Conversation start:** POST http://localhost:8000/conversation/start (body `{"std": 8}`) to set grade before opening chat. The agent never asks for grade; std is taken from the POST API body. After calling this, open the chat and send the grade as the first message (e.g. `8` or `Grade 8`) so the agent sets it for the session.

The quiz agent’s `fetch_questions` tool calls the same server. No second process needed.

## Optional: two processes (ADK on 8000, questions on 8001)

If you prefer to run the question server separately:

1. **Terminal 1 – questions API:**  
   `uvicorn question_server:app --reload --port 8001`  
   (from project root)

2. **Terminal 2 – ADK:**  
   `adk web`  
   Then open http://127.0.0.1:8000.

Set `QUESTION_API_URL=http://localhost:8001/questions` in `.env` (or in the environment) so the tool targets the separate questions server.

## Summary

| Mode    | Port | Command |
|---------|------|--------|
| Combined (recommended) | 8000 | `python run_combined.py` or `uvicorn run_combined:app --reload --port 8000` |
| Two processes          | 8000 + 8001 | `adk web` and `uvicorn question_server:app --port 8001`; set `QUESTION_API_URL=http://localhost:8001/questions` |

## curl (combined server on 8000)

```bash
curl http://localhost:8000/questions/health
curl -X POST http://localhost:8000/questions -H "Content-Type: application/json" -d "{\"std\": 8}"
curl -X POST http://localhost:8000/conversation/start -H "Content-Type: application/json" -d "{\"std\": 8}"
```
