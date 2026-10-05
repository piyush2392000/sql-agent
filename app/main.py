"""FastAPI backend: chat (JSON + SSE streaming), schema, health, and the static frontend."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from . import db, graph, llm  # noqa: E402  (after load_dotenv so env vars are visible)

STATIC = Path(__file__).resolve().parent.parent / "static"
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))

app = FastAPI(title="SQL Query AI Agent", version="1.0.0")
_hits: dict[str, deque] = defaultdict(deque)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=5000)
    session_id: str | None = None
    dialect: str = Field("sqlite", pattern="^(sqlite|postgres|mysql)$")
    execute: bool = True


def _rate_limit(request: Request) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0]
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.")
    q.append(now)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.get_schema()


@app.exception_handler(llm.LLMConfigError)
async def _llm_config(_req, exc):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_configured": bool(
        os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("AZURE_OPENAI_ENDPOINT"))}


@app.get("/api/schema")
def schema():
    return db.get_schema()


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    _rate_limit(request)
    sid = req.session_id or graph.new_session_id()
    response = await asyncio.to_thread(graph.run_turn, sid, req.message, req.dialect, req.execute)
    return {"session_id": sid, **response}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Server-Sent Events: one `step` event per finished graph node, then a `final` event."""
    _rate_limit(request)
    sid = req.session_id or graph.new_session_id()

    def events():
        yield f"event: session\ndata: {json.dumps({'session_id': sid})}\n\n"
        try:
            for kind, payload in graph.stream_turn(sid, req.message, req.dialect, req.execute):
                data = {"node": payload} if kind == "step" else {"session_id": sid, **(payload or {})}
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        except Exception as exc:  # surface errors to the UI instead of a dropped connection
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/history/{session_id}")
def history(session_id: str):
    return {"session_id": session_id, "history": graph.get_history(session_id)}


@app.post("/api/execute")
async def execute(body: dict, request: Request):
    """Re-run an already validated query (used for 'Run' after toggling execution off)."""
    from . import validator

    _rate_limit(request)
    dialect = body.get("dialect", "sqlite")
    v = validator.validate(body.get("sql", ""), dialect)
    if not v.valid:
        raise HTTPException(400, "; ".join(v.errors))
    return await asyncio.to_thread(db.execute_readonly, v.sqlite_sql)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
