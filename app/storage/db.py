"""SQLite storage: conversation history, a TTL cache for on-demand API calls, and score snapshots.

One file, zero ops. Each connection is short-lived, so it is safe to use from FastAPI's threadpool.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    messages    TEXT NOT NULL            -- full Claude message history (JSON), append-only
);
CREATE TABLE IF NOT EXISTS chat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT,                    -- validated structured answer (JSON)
    tool_trace  TEXT,                    -- tools called + inputs (JSON)
    latency_ms  INTEGER,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS api_cache (
    key         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS score_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     TEXT NOT NULL,
    weights_version TEXT NOT NULL,
    hub_id          TEXT NOT NULL,
    overall_score   REAL NOT NULL,
    overall_tier    TEXT NOT NULL,
    hazard_scores   TEXT NOT NULL        -- {hazard: score}
);
"""


_initialized = False


@contextmanager
def connect():
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _initialized:  # create tables lazily so scripts/evals work without app startup
        conn.executescript(SCHEMA)
        _initialized = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


# --- sessions -----------------------------------------------------------------
def load_session(session_id: str) -> list[dict]:
    with connect() as c:
        row = c.execute("SELECT messages FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return json.loads(row["messages"]) if row else []


def save_session(session_id: str, messages: list[dict]) -> None:
    now = time.time()
    with connect() as c:
        c.execute(
            "INSERT INTO sessions (id, created_at, updated_at, messages) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
            (session_id, now, now, json.dumps(messages)),
        )


def log_chat(session_id: str, question: str, answer: dict | None, tool_trace: list,
             latency_ms: int, error: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO chat_log (session_id, ts, question, answer, tool_trace, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, time.time(), question, json.dumps(answer) if answer else None,
             json.dumps(tool_trace), latency_ms, error),
        )


# --- API cache ----------------------------------------------------------------
def cache_get(key: str, ttl_seconds: float):
    with connect() as c:
        row = c.execute("SELECT fetched_at, payload FROM api_cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row["fetched_at"] < ttl_seconds:
        return json.loads(row["payload"])
    return None


def cache_put(key: str, payload) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO api_cache (key, fetched_at, payload) VALUES (?, ?, ?)",
                  (key, time.time(), json.dumps(payload)))


# --- score snapshots ----------------------------------------------------------
def save_score_snapshot(scores: dict) -> None:
    meta = scores["meta"]
    with connect() as c:
        c.executemany(
            "INSERT INTO score_snapshots (computed_at, weights_version, hub_id, overall_score, overall_tier, "
            "hazard_scores) VALUES (?, ?, ?, ?, ?, ?)",
            [(meta["computed_at"], meta["weights_version"], h["hub_id"], h["overall_score"], h["overall_tier"],
              json.dumps({k: v["score"] for k, v in h["hazards"].items()}))
             for h in scores["hubs"].values()],
        )


def latest_score_snapshot(before: str | None = None) -> dict[str, dict]:
    """Most recent snapshot (optionally strictly before a timestamp), keyed by hub_id."""
    with connect() as c:
        q = "SELECT MAX(computed_at) AS t FROM score_snapshots" + (" WHERE computed_at < ?" if before else "")
        t = c.execute(q, (before,) if before else ()).fetchone()["t"]
        if not t:
            return {}
        rows = c.execute("SELECT * FROM score_snapshots WHERE computed_at = ?", (t,)).fetchall()
    return {r["hub_id"]: dict(r) for r in rows}
