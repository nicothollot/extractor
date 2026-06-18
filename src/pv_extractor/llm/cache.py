"""Claude Code response cache (rule 10).

Key = sha256(static prompt + payload hash + escalated field set + model id +
effort + LLM_VERSION). Re-running an unchanged memo never re-launches (or
re-pays for) a Claude Code session unless --force-llm bypasses the read.
Lives in the same SQLite database as the file index and the Phase-2 result
cache; workers open their own connections.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from pv_extractor.llm import LLM_VERSION
from pv_extractor.models import LlmUsage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    effort TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    response_json TEXT NOT NULL,
    usage_json TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_source TEXT NOT NULL DEFAULT 'estimated'
);
"""


def init_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def cache_key(
    static_prompt: str, payload_hash: str, field_headers: list[str], model_id: str, effort: str
) -> str:
    digest = hashlib.sha256()
    digest.update(static_prompt.encode("utf-8"))
    digest.update(payload_hash.encode("utf-8"))
    digest.update("\x1f".join(sorted(field_headers)).encode("utf-8"))
    digest.update(f"{model_id}\x1f{effort}\x1f{LLM_VERSION}".encode("utf-8"))
    return digest.hexdigest()


def get_cached(conn: sqlite3.Connection, key: str) -> dict | None:
    """{structured, session_id, usage, cost_usd, cost_source} or None."""
    row = conn.execute(
        "SELECT response_json, session_id, usage_json, cost_usd, cost_source "
        "FROM llm_cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "structured": json.loads(row[0]),
        "session_id": row[1],
        "usage": LlmUsage.model_validate_json(row[2]) if row[2] else None,
        "cost_usd": row[3],
        "cost_source": row[4],
    }


def put_cached(
    conn: sqlite3.Connection,
    key: str,
    *,
    model_id: str,
    effort: str,
    structured: dict,
    session_id: str | None,
    usage: LlmUsage | None,
    cost_usd: float,
    cost_source: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache "
        "(cache_key, model_id, effort, session_id, created_at, response_json, "
        " usage_json, cost_usd, cost_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key, model_id, effort, session_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(structured, sort_keys=True, ensure_ascii=False),
            usage.model_dump_json() if usage else None,
            cost_usd, cost_source,
        ),
    )
    conn.commit()
