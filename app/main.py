"""FastAPI app: the agent API plus the static chat UI.

Run:  uvicorn app.main:app --reload
"""
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.loop import ask, ask_stream
from app.agent.schemas import ChatResponse
from app.agent.tools import HAZARD_KEYS, t_get_methodology
from app.config import ACCESS_TOKEN, ANTHROPIC_MODEL
from app.data.hubs import REGIONS, get_hub
from app.scoring.scoring import get_scores, rank_hubs
from app.storage import db

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.save_score_snapshot(get_scores())  # compute once at startup and record it (enables change alerts)
    yield


app = FastAPI(title="Weather Risk Intelligence Agent", version="1.0", lifespan=lifespan)


# --- protection for a public URL: optional shared token + naive per-IP rate limit --------------
_hits: dict[str, deque] = defaultdict(deque)
RATE_LIMIT_PER_MIN = 15


def guard(request: Request, x_access_token: str | None = Header(default=None)):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(401, "Missing or invalid access token")
    ip = request.client.host if request.client else "?"
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Too many requests, slow down")
    q.append(now)


# --- API ----------------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(guard)])
def chat(req: ChatRequest) -> ChatResponse:
    # Sync def: FastAPI runs it in a threadpool, so the blocking Claude call doesn't block the event loop.
    return ask(req.message, req.session_id)


@app.post("/api/chat/stream", dependencies=[Depends(guard)])
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same agent, as Server-Sent Events: status/tool progress, answer text as it is generated, then the
    full validated response in a final `final` event. The UI uses this; /api/chat stays for API clients."""
    def events():
        for ev in ask_stream(req.message, req.session_id):
            if ev["type"] == "final":
                ev = {"type": "final", "response": ev["response"].model_dump()}
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    # A sync generator: Starlette iterates it in a threadpool, so the blocking Claude stream is fine.
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/scores")
def scores(hazard: str = "overall", region: str | None = None):
    if hazard not in ["overall", *HAZARD_KEYS]:
        raise HTTPException(400, f"hazard must be one of overall, {', '.join(HAZARD_KEYS)}")
    if region and region not in REGIONS:
        raise HTTPException(400, f"region must be one of {REGIONS}")
    return {"meta": get_scores()["meta"], "ranking": rank_hubs(hazard, region)}


@app.get("/api/hubs/{hub_id}")
def hub_detail(hub_id: str):
    hub = get_hub(hub_id)
    if not hub:
        raise HTTPException(404, "Unknown hub")
    return get_scores()["hubs"][hub["id"]]


@app.get("/api/methodology")
def methodology():
    return t_get_methodology()


@app.get("/health")
def health():
    return {"status": "ok", "model": ANTHROPIC_MODEL, "hubs": len(get_scores()["hubs"]),
            "weights_version": get_scores()["meta"]["weights_version"]}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
