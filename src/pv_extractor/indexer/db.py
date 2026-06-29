"""SQLite store for the file index.

One `files` table (15 export-mirror columns plus the Python-derived ones),
a `scan_errors` table, learning/profile tables (`deal_finder_feedback`,
`doc_type_profiles`, `doc_search_feedback`), and an external-content FTS5
index over the normalized file name and folder path kept in sync by
triggers. Upserts are keyed on file_path via ON CONFLICT DO UPDATE so row
ids stay stable across re-ingests and incremental rescans.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from pv_extractor.io_guard import assert_write_allowed, is_under_pv_root
from pv_extractor.models import DealEvidence, DealFolder, FileRecord, ScanError, SourceClass, VersionSignal

_FILE_COLUMNS: tuple[str, ...] = (
    # export-mirror columns
    "file_name",
    "file_path",
    "folder_path",
    "parent_folder",
    "extension",
    "size_bytes",
    "modified_time",
    "depth_from_pv_root",
    "normalized_file_name",
    "normalized_folder_path",
    "normalized_full_path",
    "contains_memo_keyword",
    "contains_q4_2025_signal",
    "contains_q1_2026_signal",
    "archive_or_old_flag",
    # derived columns
    "client",
    "deal",
    "date_folder",
    "as_of_date",
    "source_class",
    "is_archive",
    "version_rank",
    "version_number",
    "copy_number",
    "version_raw",
    "is_cloud_placeholder",
    "is_zero_byte",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    folder_path TEXT NOT NULL,
    parent_folder TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER,
    modified_time TEXT,
    depth_from_pv_root INTEGER,
    normalized_file_name TEXT NOT NULL,
    normalized_folder_path TEXT NOT NULL,
    normalized_full_path TEXT NOT NULL,
    contains_memo_keyword INT NOT NULL,
    contains_q4_2025_signal INT NOT NULL,
    contains_q1_2026_signal INT NOT NULL,
    archive_or_old_flag INT NOT NULL,
    client TEXT,
    deal TEXT,
    date_folder TEXT,
    as_of_date TEXT,
    source_class TEXT NOT NULL,
    is_archive INT NOT NULL,
    version_rank INT,
    version_number INT,
    copy_number INT,
    version_raw TEXT,
    is_cloud_placeholder INT NOT NULL,
    is_zero_byte INT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_client_deal ON files (client, deal);
CREATE INDEX IF NOT EXISTS idx_files_client_deal_asof ON files (client, deal, as_of_date);
CREATE TABLE IF NOT EXISTS scan_errors (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deal_folders (
    id INTEGER PRIMARY KEY,
    client TEXT NOT NULL,
    deal TEXT NOT NULL,
    folder_paths TEXT NOT NULL,
    confidence REAL NOT NULL,
    method TEXT NOT NULL,
    evidence TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE (client, deal)
);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deal_finder_feedback (
    id INTEGER PRIMARY KEY,
    client TEXT NOT NULL,
    deal TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'add_folder' | 'remove_folder' | 'merge' | 'split' | 'rename'
    folder_path TEXT,               -- relative path acted on (NULL for rename)
    payload TEXT,                   -- JSON: new name, merge target, etc.
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dff_client ON deal_finder_feedback (client);
CREATE TABLE IF NOT EXISTS doc_type_profiles (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    query_seed TEXT,
    spec TEXT NOT NULL,             -- JSON DocTypeSpec
    builtin INT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_search_feedback (
    id INTEGER PRIMARY KEY,
    profile_slug TEXT NOT NULL,
    file_path TEXT NOT NULL,
    label INT NOT NULL,             -- +1 accepted, -1 rejected
    context TEXT,                   -- JSON: client/deal/period at decision time
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dsf_profile ON doc_search_feedback (profile_slug);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    normalized_file_name, normalized_folder_path,
    content='files', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS files_fts_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, normalized_file_name, normalized_folder_path)
    VALUES (new.id, new.normalized_file_name, new.normalized_folder_path);
END;
CREATE TRIGGER IF NOT EXISTS files_fts_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, normalized_file_name, normalized_folder_path)
    VALUES ('delete', old.id, old.normalized_file_name, old.normalized_folder_path);
END;
CREATE TRIGGER IF NOT EXISTS files_fts_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, normalized_file_name, normalized_folder_path)
    VALUES ('delete', old.id, old.normalized_file_name, old.normalized_folder_path);
    INSERT INTO files_fts(rowid, normalized_file_name, normalized_folder_path)
    VALUES (new.id, new.normalized_file_name, new.normalized_folder_path);
END;
"""

_UPSERT_SQL = (
    f"INSERT INTO files ({', '.join(_FILE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_FILE_COLUMNS))}) "
    "ON CONFLICT(file_path) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in _FILE_COLUMNS if col != "file_path")
)


def open_db(db_path: Path, pv_root: str) -> sqlite3.Connection:
    """Open (creating parents) a database that must live OFF the share."""
    assert_write_allowed(db_path, pv_root)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        # WAL needs shared memory the filesystem may not provide (network paths
        # / some mounts raise 'disk I/O error'). Fall back to a rollback journal
        # so writes still work — slower, no WAL concurrency, but never crashes.
        # The GUI also relocates such DBs to local disk (see relocate_db_if_needed).
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def db_supports_wal(db_path: Path) -> bool:
    """Can this path host a proper WAL SQLite DB at this runtime? False for
    network/cross-boundary paths (e.g. \\\\wsl.localhost from native Windows, a
    UNC share) where WAL's shared-memory file can't be created. Non-destructive:
    only toggles journal mode; cleans up a file it had to create just to probe."""
    p = Path(db_path)
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    conn = None
    ok = False
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        ok = bool(row) and str(row[0]).lower() == "wal"
    except sqlite3.Error:
        ok = False
    finally:
        if conn is not None:
            conn.close()
        if not ok and not existed:  # don't litter a stray empty DB where the probe failed
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    Path(str(p) + suffix).unlink()
                except OSError:
                    pass
    return ok


def _clone_db(src_path: Path, dst_path: Path) -> None:
    """Clone an index DB to a new path via the SQLite online-backup API — works
    even if the source is mid-WAL and yields a clean DELETE-journal copy. The
    source is opened read-only, so this is safe across a network path."""
    src = sqlite3.connect(f"file:{Path(src_path).as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def relocate_db_if_needed(configured: Path, local_fallback: Path) -> dict:
    """Return a db path that actually works for read+write here. If `configured`
    can't host a WAL DB at this runtime (network/cross-boundary path), fall back
    to `local_fallback` on local disk, cloning an existing index over so nothing
    is lost. Returns {path, relocated, from, detail}."""
    configured = Path(configured)
    if db_supports_wal(configured):
        return {"path": configured, "relocated": False, "from": None, "detail": ""}
    local = Path(local_fallback)
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # even the fallback dir is unwritable — leave the configured path and let
        # open_db's journal fallback try; surface the reason.
        return {"path": configured, "relocated": False, "from": None,
                "detail": f"index path {configured} is not writable and the local fallback failed: {exc}"}
    if local.exists():
        detail = (f"configured index {configured} can't be written here (network path); "
                  f"using the existing local index at {local}")
    elif configured.exists():
        try:
            _clone_db(configured, local)
            detail = f"copied index from {configured} to local disk at {local} (network path can't host SQLite WAL)"
        except Exception as exc:  # noqa: BLE001
            detail = (f"configured index {configured} is unreadable for copy ({exc}); "
                      f"starting a fresh local index at {local}")
    else:
        detail = f"configured index {configured} is not usable here; using a fresh local index at {local}"
    return {"path": local, "relocated": True, "from": str(configured), "detail": detail}


def open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """Read-only connection for the GUI's view endpoints. Opens with
    ``mode=ro`` and does NOT switch on WAL — WAL needs shared-memory and fails
    with 'disk I/O error' on network filesystems (e.g. a DB reached over
    \\\\wsl.localhost from native Windows, or a UNC share), so a viewer should
    never force it. Raises sqlite3.Error if the file can't be opened."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables, indexes, the FTS5 index and its sync triggers (idempotent)."""
    conn.executescript(_SCHEMA)
    conn.commit()


def _record_params(record: FileRecord) -> tuple:
    version = record.version_signal
    return (
        record.file_name,
        record.file_path,
        record.folder_path,
        record.parent_folder,
        record.extension,
        record.size_bytes,
        record.modified_time.isoformat(timespec="seconds") if record.modified_time else None,
        record.depth_from_pv_root,
        record.normalized_file_name,
        record.normalized_folder_path,
        record.normalized_full_path,
        int(record.contains_memo_keyword),
        int(record.contains_q4_2025_signal),
        int(record.contains_q1_2026_signal),
        int(record.archive_or_old_flag),
        record.client,
        record.deal,
        record.date_folder,
        record.as_of_date.isoformat() if record.as_of_date else None,
        record.source_class.value,
        int(record.is_archive),
        version.rank if version else None,
        version.version_number if version else None,
        version.copy_number if version else None,
        version.raw if version else None,
        int(record.is_cloud_placeholder),
        int(record.is_zero_byte),
    )


def insert_records(conn: sqlite3.Connection, records: Iterable[FileRecord], batch_size: int) -> int:
    """Upsert records in executemany chunks, keyed on file_path with stable
    ids (ON CONFLICT DO UPDATE, never INSERT OR REPLACE). Returns the count."""
    total = 0
    iterator = iter(records)
    while chunk := list(itertools.islice(iterator, batch_size)):
        conn.executemany(_UPSERT_SQL, [_record_params(record) for record in chunk])
        conn.commit()
        total += len(chunk)
    return total


def record_from_row(row: sqlite3.Row) -> FileRecord:
    """Round-trip a `files` row back into a FileRecord (VersionSignal included)."""
    version = None
    if row["version_rank"] is not None:
        version = VersionSignal(
            rank=row["version_rank"],
            version_number=row["version_number"],
            copy_number=row["copy_number"],
            raw=row["version_raw"] or "",
        )
    return FileRecord(
        file_name=row["file_name"],
        file_path=row["file_path"],
        folder_path=row["folder_path"],
        parent_folder=row["parent_folder"],
        extension=row["extension"],
        size_bytes=row["size_bytes"],
        modified_time=datetime.fromisoformat(row["modified_time"]) if row["modified_time"] else None,
        depth_from_pv_root=row["depth_from_pv_root"],
        normalized_file_name=row["normalized_file_name"],
        normalized_folder_path=row["normalized_folder_path"],
        normalized_full_path=row["normalized_full_path"],
        contains_memo_keyword=bool(row["contains_memo_keyword"]),
        contains_q4_2025_signal=bool(row["contains_q4_2025_signal"]),
        contains_q1_2026_signal=bool(row["contains_q1_2026_signal"]),
        archive_or_old_flag=bool(row["archive_or_old_flag"]),
        client=row["client"],
        deal=row["deal"],
        date_folder=row["date_folder"],
        as_of_date=date.fromisoformat(row["as_of_date"]) if row["as_of_date"] else None,
        source_class=SourceClass(row["source_class"]),
        is_archive=bool(row["is_archive"]),
        version_signal=version,
        is_cloud_placeholder=bool(row["is_cloud_placeholder"]),
        is_zero_byte=bool(row["is_zero_byte"]),
        row_id=row["id"],
    )


def fts_candidates(conn: sqlite3.Connection, match_expr: str, limit: int) -> list[FileRecord]:
    """FTS5 prefilter: best `limit` rows matching `match_expr` (bm25 order)."""
    rows = conn.execute(
        "SELECT files.* FROM files_fts JOIN files ON files.id = files_fts.rowid "
        "WHERE files_fts MATCH ? ORDER BY rank LIMIT ?",
        (match_expr, limit),
    ).fetchall()
    return [record_from_row(row) for row in rows]


def distinct_clients(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT client FROM files WHERE client IS NOT NULL ORDER BY client"
    ).fetchall()
    return [row["client"] for row in rows]


def deals_for_client(conn: sqlite3.Connection, client: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT deal FROM files WHERE client = ? AND deal IS NOT NULL ORDER BY deal",
        (client,),
    ).fetchall()
    return [row["deal"] for row in rows]


def replace_deal_folders(conn: sqlite3.Connection, client: str, deals: list[DealFolder]) -> None:
    """Replace a client's discovered deal folders atomically (one client's
    discovery is always recomputed whole, never patched row by row)."""
    discovered_at = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM deal_folders WHERE client = ?", (client,))
    conn.executemany(
        "INSERT INTO deal_folders (client, deal, folder_paths, confidence, method, evidence, "
        "discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                client,
                d.name,
                json.dumps(d.folder_paths),
                d.confidence,
                d.method,
                d.evidence.model_dump_json(),
                discovered_at,
            )
            for d in deals
        ],
    )
    conn.commit()


def deal_folders_for_client(conn: sqlite3.Connection, client: str) -> list[DealFolder]:
    rows = conn.execute(
        "SELECT * FROM deal_folders WHERE client = ? ORDER BY confidence DESC, deal",
        (client,),
    ).fetchall()
    return [
        DealFolder(
            client=row["client"],
            name=row["deal"],
            folder_paths=json.loads(row["folder_paths"]),
            confidence=row["confidence"],
            method=row["method"],
            evidence=DealEvidence.model_validate_json(row["evidence"]),
        )
        for row in rows
    ]


def deal_folder_path(conn: sqlite3.Connection, client: str, deal: str) -> str | None:
    """First discovered folder path for a (client, deal), or None. Used by the
    GUI so the swap/add-file picker can open in the deal folder."""
    row = conn.execute(
        "SELECT folder_paths FROM deal_folders WHERE client = ? AND deal = ? "
        "ORDER BY confidence DESC LIMIT 1",
        (client, deal),
    ).fetchone()
    if row is None:
        return None
    paths = json.loads(row["folder_paths"])
    return paths[0] if paths else None


def _llm_discovery_meta_key(client: str) -> str:
    return f"llm_discovery:{client}"


def record_llm_discovery(
    conn: sqlite3.Connection, client: str, *, model: str, effort: str, deals: int
) -> None:
    """Stamp that an LLM-assisted deal discovery just ran for `client`, in the
    index_meta kv table. This is independent of whether any individual
    deal_folders row carries a claude-code method — a pass that only
    CORROBORATES heuristic deals (the common case) leaves every row 'heuristic',
    so the per-row method scan alone misses it. Lets the GUI reliably warn /
    offer to reuse before paying for a re-run."""
    set_meta(
        conn,
        _llm_discovery_meta_key(client),
        json.dumps({
            "model": model,
            "effort": effort,
            "at": datetime.now().isoformat(timespec="seconds"),
            "deals": deals,
        }),
    )


def last_llm_discovery(conn: sqlite3.Connection, client: str) -> dict | None:
    """The most recent LLM-assisted deal discovery for a client:
    {method, model, effort, at, deals}, or None if it was never LLM-assisted.
    Lets the GUI warn before re-running a discovery that already exists.

    Prefers the index_meta event stamp (recorded on every successful LLM
    assist, including corroboration-only passes); falls back to the legacy
    per-deal claude-code method scan for clients discovered before the stamp
    existed."""
    stamped = get_meta(conn, _llm_discovery_meta_key(client))
    if stamped:
        try:
            payload = json.loads(stamped)
            model = str(payload.get("model", ""))
            effort = str(payload.get("effort", ""))
            return {
                "method": f"claude-code:{model}:{effort}",
                "model": model,
                "effort": effort,
                "at": payload.get("at"),
                "deals": int(payload.get("deals", 0)),
            }
        except (ValueError, TypeError):
            pass  # corrupt stamp: fall through to the per-row scan
    row = conn.execute(
        "SELECT method, discovered_at FROM deal_folders "
        "WHERE client = ? AND method LIKE 'claude-code:%' "
        "ORDER BY discovered_at DESC LIMIT 1",
        (client,),
    ).fetchone()
    if row is None:
        return None
    method = row["method"]
    # method format: claude-code:<model>:<effort>
    parts = method.split(":")
    model = parts[1] if len(parts) > 1 else ""
    effort = parts[2] if len(parts) > 2 else ""
    count = conn.execute(
        "SELECT COUNT(*) FROM deal_folders WHERE client = ? AND method LIKE 'claude-code:%'",
        (client,),
    ).fetchone()[0]
    return {"method": method, "model": model, "effort": effort, "at": row["discovered_at"], "deals": count}


def update_file_deals(conn: sqlite3.Connection, assignments: dict[int, str | None]) -> int:
    """Rewrite files.deal by row id. Callers pass only rows whose value
    actually changes (they hold the current records already)."""
    if assignments:
        conn.executemany(
            "UPDATE files SET deal = ? WHERE id = ?",
            [(deal, row_id) for row_id, deal in assignments.items()],
        )
        conn.commit()
    return len(assignments)


def files_for_client(conn: sqlite3.Connection, client: str) -> list[FileRecord]:
    rows = conn.execute("SELECT * FROM files WHERE client = ?", (client,)).fetchall()
    return [record_from_row(row) for row in rows]


def as_of_dates_for_deal(conn: sqlite3.Connection, client: str, deal: str) -> list[date]:
    rows = conn.execute(
        "SELECT DISTINCT as_of_date FROM files "
        "WHERE client = ? AND deal = ? AND as_of_date IS NOT NULL ORDER BY as_of_date",
        (client, deal),
    ).fetchall()
    return [date.fromisoformat(row["as_of_date"]) for row in rows]


def file_meta_under(conn: sqlite3.Connection, path_prefix: str) -> dict[str, tuple[int | None, str | None]]:
    """file_path -> (size_bytes, modified_time TEXT) for every indexed file
    under `path_prefix` (case-insensitive, separator-agnostic containment)."""
    meta: dict[str, tuple[int | None, str | None]] = {}
    for row in conn.execute("SELECT file_path, size_bytes, modified_time FROM files"):
        if is_under_pv_root(row["file_path"], path_prefix):
            meta[row["file_path"]] = (row["size_bytes"], row["modified_time"])
    return meta


def delete_paths(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    conn.executemany("DELETE FROM files WHERE file_path = ?", ((path,) for path in paths))
    conn.commit()


def add_scan_error(conn: sqlite3.Connection, error: ScanError) -> None:
    conn.execute(
        "INSERT INTO scan_errors (path, error_type, message, seen_at) VALUES (?, ?, ?, ?)",
        (error.path, error.error_type, error.message, error.seen_at.isoformat(timespec="seconds")),
    )
    conn.commit()


def scan_errors_under(conn: sqlite3.Connection, path_prefix: str) -> list[ScanError]:
    rows = conn.execute("SELECT path, error_type, message, seen_at FROM scan_errors ORDER BY id").fetchall()
    return [
        ScanError(
            path=row["path"],
            error_type=row["error_type"],
            message=row["message"],
            seen_at=datetime.fromisoformat(row["seen_at"]),
        )
        for row in rows
        if is_under_pv_root(row["path"], path_prefix)
    ]


def count_files(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a key/value pair into index_meta (used for per-root last-scan
    timestamps that drive the opt-in quick rescan)."""
    conn.execute(
        "INSERT INTO index_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def all_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM index_meta")}


def record_deal_feedback(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    action: str,
    folder_path: str | None = None,
    payload: str | None = None,
    created_at: str,
) -> int:
    """Insert one deal_finder_feedback row (one analyst correction). `payload`
    is a pre-serialized JSON string (or None). Returns the new row id."""
    cur = conn.execute(
        "INSERT INTO deal_finder_feedback "
        "(client, deal, action, folder_path, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (client, deal, action, folder_path, payload, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_deal_feedback(conn: sqlite3.Connection, client: str) -> list[dict]:
    """All recorded corrections for one client, oldest first."""
    rows = conn.execute(
        "SELECT id, client, deal, action, folder_path, payload, created_at "
        "FROM deal_finder_feedback WHERE client = ? ORDER BY id",
        (client,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "client": row["client"],
            "deal": row["deal"],
            "action": row["action"],
            "folder_path": row["folder_path"],
            "payload": row["payload"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def delete_deal_feedback(conn: sqlite3.Connection, feedback_id: int) -> bool:
    cur = conn.execute("DELETE FROM deal_finder_feedback WHERE id = ?", (feedback_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# doc_type_profiles — learnable DocTypeSpec store for Smart Search (Phase B).
# Conn-first thin accessors; the spec JSON is serialized/validated by
# search/doc_type_spec.py (this layer only round-trips strings).
# ---------------------------------------------------------------------------


def upsert_doc_type_profile(
    conn: sqlite3.Connection,
    *,
    slug: str,
    label: str,
    spec_json: str,
    query_seed: str | None,
    builtin: bool,
    created_at: str,
    updated_at: str,
) -> None:
    """Upsert one doc-type profile keyed on slug. created_at is preserved on
    update (only updated_at/label/spec/query_seed/builtin change)."""
    conn.execute(
        "INSERT INTO doc_type_profiles "
        "(slug, label, query_seed, spec, builtin, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "label = excluded.label, query_seed = excluded.query_seed, "
        "spec = excluded.spec, builtin = excluded.builtin, "
        "updated_at = excluded.updated_at",
        (slug, label, query_seed, spec_json, int(builtin), created_at, updated_at),
    )
    conn.commit()


def get_doc_type_profile(conn: sqlite3.Connection, slug: str) -> dict | None:
    """The raw profile row for a slug (spec still a JSON string), or None."""
    row = conn.execute(
        "SELECT slug, label, query_seed, spec, builtin, created_at, updated_at "
        "FROM doc_type_profiles WHERE slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return {
        "slug": row["slug"],
        "label": row["label"],
        "query_seed": row["query_seed"],
        "spec": row["spec"],
        "builtin": bool(row["builtin"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_doc_type_profiles(conn: sqlite3.Connection) -> list[dict]:
    """All profile rows (builtins first, then learned, each alphabetical)."""
    rows = conn.execute(
        "SELECT slug, label, query_seed, spec, builtin, created_at, updated_at "
        "FROM doc_type_profiles ORDER BY builtin DESC, slug"
    ).fetchall()
    return [
        {
            "slug": r["slug"],
            "label": r["label"],
            "query_seed": r["query_seed"],
            "spec": r["spec"],
            "builtin": bool(r["builtin"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def delete_doc_type_profile(conn: sqlite3.Connection, slug: str) -> bool:
    """Delete a profile by slug. Returns False when nothing was deleted (the
    builtin guard lives in search/doc_type_spec.py, which refuses first)."""
    cur = conn.execute("DELETE FROM doc_type_profiles WHERE slug = ?", (slug,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# doc_search_feedback — per-profile accept/reject signal for rank learning.
# ---------------------------------------------------------------------------


def record_doc_search_feedback(
    conn: sqlite3.Connection,
    *,
    profile_slug: str,
    file_path: str,
    label: int,
    context: str | None,
    created_at: str,
) -> int:
    """Insert one feedback row (+1 accepted, -1 rejected). `context` is a
    pre-serialized JSON string (or None). Returns the new row id."""
    cur = conn.execute(
        "INSERT INTO doc_search_feedback "
        "(profile_slug, file_path, label, context, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (profile_slug, file_path, int(label), context, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_doc_search_feedback(conn: sqlite3.Connection, profile_slug: str) -> list[dict]:
    """All feedback rows for one profile, oldest first."""
    rows = conn.execute(
        "SELECT id, profile_slug, file_path, label, context, created_at "
        "FROM doc_search_feedback WHERE profile_slug = ? ORDER BY id",
        (profile_slug,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "profile_slug": r["profile_slug"],
            "file_path": r["file_path"],
            "label": r["label"],
            "context": r["context"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
