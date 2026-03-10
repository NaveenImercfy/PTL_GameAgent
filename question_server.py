"""
FastAPI server for the /questions endpoint.
Run separately from the ADK server so it does not conflict with port 8000.

From project root (d:\\Home_Agent):
  uvicorn question_server:app --reload --port 8001
Then: POST http://localhost:8001/questions with body e.g. {"std": 9}

Questions content is fetched from QUESTIONS_SOURCE_API_URL. Use GET with query params (default) or POST with JSON body (QUESTIONS_SOURCE_API_METHOD=POST).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

# Load env for question server (Home_Agent/.env)
load_dotenv(Path(__file__).resolve().parent / "Home_Agent" / ".env")

app = FastAPI(title="Questions API", version="1.0")
questions_router = APIRouter()

QUESTION_API_PORT = 8001
SOURCE_REQUEST_TIMEOUT = 15


class QuestionsRequest(BaseModel):
    """API params for POST /questions. Forwarded to QUESTIONS_SOURCE_API_URL (GET query or POST body)."""
    std: int | None = None
    subject: str | None = None
    topic: str | None = None


def _build_payload(body: QuestionsRequest) -> dict:
    """Build payload from body, omitting None values."""
    payload = {}
    if body.std is not None:
        payload["std"] = body.std
    if body.subject is not None:
        payload["subject"] = body.subject
    if body.topic is not None:
        payload["topic"] = body.topic
    return payload


def _fetch_content_from_source(url: str, payload: dict, method: str) -> str:
    """Call source API with GET (query params) or POST (JSON body); return content string. Raises on error."""
    if method.upper() == "GET":
        query = urllib.parse.urlencode({k: str(v) for k, v in payload.items()})
        full_url = url.rstrip("/") + ("?" + query if query else "")
        req = urllib.request.Request(full_url, method="GET")
    else:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(req, timeout=SOURCE_REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Source API error: {e.code} {e.reason}",
        )
    except (urllib.error.URLError, OSError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"Source API unreachable: {e!s}",
        )
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "content" in data and isinstance(data["content"], str):
            return data["content"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], str):
            return data["data"]
    except json.JSONDecodeError:
        pass
    if isinstance(raw, str) and raw.strip():
        return raw
    raise HTTPException(status_code=502, detail="Source API did not return valid content")


@questions_router.post("")
def post_questions(body: QuestionsRequest):
    """POST /questions: forwards body to QUESTIONS_SOURCE_API_URL (GET or POST per env) and returns {"content": "..."}."""
    source_url = os.environ.get("QUESTIONS_SOURCE_API_URL", "").strip()
    if not source_url:
        raise HTTPException(
            status_code=503,
            detail="QUESTIONS_SOURCE_API_URL not configured",
        )
    method = os.environ.get("QUESTIONS_SOURCE_API_METHOD", "GET").strip().upper() or "GET"
    payload = _build_payload(body)
    content = _fetch_content_from_source(source_url, payload, method)
    return {"content": content}


@questions_router.get("/health")
def health():
    return {"status": "ok", "port": QUESTION_API_PORT}


# Standalone mode: mount router so uvicorn question_server:app still works
app.include_router(questions_router, prefix="/questions", tags=["questions"])
