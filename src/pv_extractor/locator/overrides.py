"""Locator override learning table (Phase 4, GUI screen 4).

When an analyst resolves an AMBIGUOUS locate by picking a candidate in the
GUI, the pick is recorded here keyed on the RESOLVED (client, deal, as-of
date, doc type) tuple. `locate()` consults the table first, so the same
pick is automatic on every later run — but the chosen file still goes
through the Phase-2 peek-verifier like any other winner (an override can
never smuggle HL work product past rule 2).

The table lives in the same SQLite index database as the `files` table; an
override whose file has dropped out of the index is ignored (and reported),
never trusted blindly.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timezone

from pv_extractor.indexer.db import record_from_row
from pv_extractor.logging_setup import log_event
from pv_extractor.models import FileRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS locator_overrides (
    client TEXT NOT NULL,
    deal TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client, deal, as_of_date, doc_type)
);
"""


# Additional source documents for one slot (Feature: multi-doc merge). The
# override table holds the PRIMARY pick per slot; this holds the EXTRA documents
# whose fields are merged into the same row by best confidence. Multiple rows
# per slot key; uniqueness on the full tuple so re-recording is idempotent.
_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS extra_source_docs (
    client TEXT NOT NULL,
    deal TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (client, deal, as_of_date, doc_type, file_path)
);
"""


def init_overrides(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.executescript(_EXTRA_SCHEMA)
    conn.commit()


def set_extra_docs(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    as_of_date: date,
    doc_type: str,
    file_paths: list[str],
) -> None:
    """Replace the extra-source-document set for one slot (idempotent)."""
    init_overrides(conn)
    conn.execute(
        "DELETE FROM extra_source_docs WHERE client=? AND deal=? AND as_of_date=? AND doc_type=?",
        (client, deal, as_of_date.isoformat(), doc_type),
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for path in dict.fromkeys(file_paths):  # de-dupe, preserve order
        conn.execute(
            "INSERT OR IGNORE INTO extra_source_docs "
            "(client, deal, as_of_date, doc_type, file_path, created_at) VALUES (?,?,?,?,?,?)",
            (client, deal, as_of_date.isoformat(), doc_type, path, now),
        )
    conn.commit()
    log_event(
        logger, "extra source docs set", client=client, deal=deal,
        as_of_date=as_of_date.isoformat(), doc_type=doc_type, count=len(file_paths),
    )


def lookup_extra_docs(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    as_of_date: date,
    doc_type: str,
) -> list[str]:
    """Extra source documents recorded for this exact resolved slot, if any."""
    init_overrides(conn)
    rows = conn.execute(
        "SELECT file_path FROM extra_source_docs "
        "WHERE client=? AND deal=? AND as_of_date=? AND doc_type=? ORDER BY created_at, file_path",
        (client, deal, as_of_date.isoformat(), doc_type),
    ).fetchall()
    return [r[0] for r in rows]


def record_override(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    as_of_date: date,
    doc_type: str,
    file_path: str,
    note: str | None = None,
) -> None:
    """Upsert one analyst pick (latest pick wins)."""
    init_overrides(conn)
    conn.execute(
        "INSERT INTO locator_overrides "
        "(client, deal, as_of_date, doc_type, file_path, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client, deal, as_of_date, doc_type) DO UPDATE SET "
        "file_path = excluded.file_path, note = excluded.note, "
        "created_at = excluded.created_at",
        (
            client, deal, as_of_date.isoformat(), doc_type, file_path, note,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    log_event(
        logger, "locator override recorded", client=client, deal=deal,
        as_of_date=as_of_date.isoformat(), doc_type=doc_type,
    )


def lookup_override(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    as_of_date: date,
    doc_type: str,
) -> str | None:
    """The recorded file_path for this exact resolved query, if any."""
    init_overrides(conn)
    row = conn.execute(
        "SELECT file_path FROM locator_overrides "
        "WHERE client = ? AND deal = ? AND as_of_date = ? AND doc_type = ?",
        (client, deal, as_of_date.isoformat(), doc_type),
    ).fetchone()
    return row[0] if row else None


def delete_override(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    as_of_date: date,
    doc_type: str,
) -> bool:
    init_overrides(conn)
    cur = conn.execute(
        "DELETE FROM locator_overrides "
        "WHERE client = ? AND deal = ? AND as_of_date = ? AND doc_type = ?",
        (client, deal, as_of_date.isoformat(), doc_type),
    )
    conn.commit()
    return cur.rowcount > 0


def list_overrides(conn: sqlite3.Connection) -> list[dict]:
    init_overrides(conn)
    rows = conn.execute(
        "SELECT client, deal, as_of_date, doc_type, file_path, note, created_at "
        "FROM locator_overrides ORDER BY client, deal, as_of_date"
    ).fetchall()
    return [
        {
            "client": r[0], "deal": r[1], "as_of_date": r[2], "doc_type": r[3],
            "file_path": r[4], "note": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def indexed_record_for_path(conn: sqlite3.Connection, file_path: str) -> FileRecord | None:
    """The current `files` row for an override target — None when the file
    has dropped out of the index (override must then be ignored)."""
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM files WHERE file_path = ?", (file_path,)
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    return record_from_row(row) if row is not None else None
