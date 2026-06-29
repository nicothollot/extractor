"""Memo-level result cache (D7).

Key = sha256(file bytes) + schema version (sha256 of master_schema.json) +
EXTRACTOR_VERSION — any change to the document, the schema or the extractor
code invalidates the entry. Stored in the same SQLite database as the file
index (its own table); --force bypasses reads, writes always happen.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pv_extractor.extract import EXTRACTOR_VERSION
from pv_extractor.io_guard import open_read
from pv_extractor.models import MemoResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    memo_json TEXT NOT NULL
);
"""


def init_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open_read(path) as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def schema_version(schema_path: str | Path) -> str:
    with open_read(schema_path) as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def cache_key(sha256: str, schema_ver: str) -> str:
    return f"{sha256}:{schema_ver}:{EXTRACTOR_VERSION}"


def get_cached(conn: sqlite3.Connection, key: str) -> MemoResult | None:
    row = conn.execute(
        "SELECT memo_json FROM extraction_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    result = MemoResult.model_validate_json(row[0])
    result.from_cache = True
    return result


def put_cached(conn: sqlite3.Connection, key: str, schema_ver: str, result: MemoResult) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extraction_cache "
        "(cache_key, file_path, file_sha256, schema_version, extractor_version, created_at, memo_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            result.file_path,
            result.file_sha256,
            schema_ver,
            EXTRACTOR_VERSION,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            result.model_dump_json(),
        ),
    )
    conn.commit()


def forget_by_sha256(conn: sqlite3.Connection, shas: list[str]) -> int:
    """Drop every cached extraction for the given file sha256s (so a future run
    re-extracts instead of reusing the result). Returns rows removed."""
    removed = 0
    for sha in shas:
        if not sha:
            continue
        cur = conn.execute("DELETE FROM extraction_cache WHERE file_sha256 = ?", (sha,))
        removed += cur.rowcount or 0
    conn.commit()
    return removed
