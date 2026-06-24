"""Core API routes: health, first-run setup, doctor, index metadata for the
wizard, the model menu (with editable pricing) and the editable config
surface. Pipeline logic is never reimplemented here — these routes call the
same functions the CLI commands call."""

from __future__ import annotations

import importlib.metadata
import os
from datetime import date as date_type
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from pv_extractor.api.schemas import (
    ConfigUpdate,
    DealFeedbackRequest,
    DealRefreshRequest,
    IntentResolveRequest,
    PricingUpdate,
    ProfileSaveRequest,
    RawConfigUpdate,
    ScanRequest,
    SearchFeedbackRequest,
    SearchPreviewRequest,
)
from pv_extractor.api.yaml_edit import (
    YamlEditError,
    replace_config_yaml,
    update_config_yaml,
    update_model_pricing,
)
from pv_extractor.config import Config, load_config
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import period_label
from pv_extractor.io_guard import open_read
from pv_extractor.models import DocType, DocTypeSpec

router = APIRouter(prefix="/api")

# Settings the GUI may edit by dotted path. Anything not covered here is
# still reachable through the raw config.yaml editor (PUT /config/raw).
_EDITABLE_PREFIXES = (
    "pv_root",
    "output_dir",
    "db_path",
    "claude_code.",
    "codex_cli.",
    "first_run.",
    "gui.",
    "llm.",
    "extraction.confidence_threshold",
    "deal_discovery.display_min_confidence",
    "selection.min_confidence",
)


def _config(request: Request) -> Config:
    return request.app.state.config


def _manager(request: Request):
    return request.app.state.jobs


_NO_INDEX_HINT = (
    "no file index yet — build it from Settings > 'Locations & file index' (Scan), "
    "or run `pv-extractor scan` / `pv-extractor ingest-xlsx`"
)


def _index_ready(conn) -> bool:
    """open_db creates an empty database file as a side effect, so a bare
    file-exists check is not enough — the files table must actually exist."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'files'"
    ).fetchone()
    return row is not None


def _open_index(config: Config):
    if not Path(config.db_path).exists():
        raise HTTPException(409, detail=_NO_INDEX_HINT)
    conn = db.open_db(config.db_path, config.pv_root)
    if not _index_ready(conn):
        conn.close()
        raise HTTPException(409, detail=_NO_INDEX_HINT)
    return conn


# ---------------------------------------------------------------------------
# health / setup / doctor
# ---------------------------------------------------------------------------


@router.get("/health")
def health(request: Request) -> dict:
    from pv_extractor.system.version import build_info

    config = _config(request)
    active = _manager(request).active_pipeline_job()
    build = build_info()
    return {
        "ok": True,
        "version": build["version"],
        "build": build,  # commit / committed_at / branch / dirty / python / label
        "active_job": active.model_dump() if active else None,
        "llm_provider": config.llm.provider,
        "auto_update_on_start": config.llm.provider == "claude" and config.claude_code.auto_update_on_start,
    }


@router.get("/setup/status")
def setup_status(request: Request, include_claude: bool = True) -> dict:
    from pv_extractor.system.setup_check import collect_setup_status

    status = collect_setup_status(_config(request), include_claude=include_claude)
    return {"items": [i.model_dump() for i in status.items],
            "missing_packages": status.missing_packages,
            "can_auto_install": status.can_auto_install,
            "install_command": status.install_command,
            "all_ok": status.all_ok}


@router.post("/setup/install")
def setup_install(request: Request) -> dict:
    from pv_extractor.system.setup_check import collect_setup_status, install_missing

    config = _config(request)
    status = collect_setup_status(config, include_claude=False)
    if not status.missing_packages:
        return {"job": None, "detail": "nothing to install"}
    if not config.first_run.install_missing_deps:
        raise HTTPException(409, detail=(
            "first_run.install_missing_deps is false — run manually: "
            + (status.install_command or "")
        ))
    packages = list(status.missing_packages)
    job = _manager(request).start_utility(
        "install_deps", {"packages": packages},
        lambda: dict(zip(("ok", "output"), install_missing(config, packages))),
    )
    return {"job": job.model_dump()}


@router.post("/setup/claude-update")
def claude_update(request: Request) -> dict:
    """Run `claude update` as a short non-blocking job (the startup status
    card polls the job). Never requires ANTHROPIC_API_KEY."""
    from pv_extractor.llm.claude_code_client import ClaudeCodeClient

    config = _config(request)
    client = ClaudeCodeClient(config)
    if client.binary_path() is None:
        raise HTTPException(409, detail=f"claude CLI ({config.claude_code.command!r}) not found on PATH")

    def _do_update() -> dict:
        client.update()
        return {"version": client.version()}

    job = _manager(request).start_utility("claude_update", {}, _do_update)
    return {"job": job.model_dump()}


@router.get("/claude/sources")
def claude_sources(request: Request) -> dict:
    """Detect the reachable `claude` installs (this machine's PATH + bridged
    WSL) so Settings can offer a one-click source picker. Selecting a source
    just persists its `command` + `command_args` through PUT /config. Probing
    spawns short `--version` subprocesses (ANTHROPIC_* stripped); it never
    sends memo data."""
    import shutil
    import sys

    from pv_extractor.llm.claude_code_client import detect_claude_sources

    config = _config(request)
    cur_cmd = config.claude_code.command
    cur_args = list(config.claude_code.command_args)
    sources = []
    for src in detect_claude_sources(cur_cmd):
        payload = src.model_dump()
        payload["selected"] = src.command == cur_cmd and src.command_args == cur_args
        sources.append(payload)
    return {
        "platform": "windows" if os.name == "nt" else "posix",
        "current": {"command": cur_cmd, "command_args": cur_args},
        "sources": sources,
        # Diagnostics: what THIS GUI process actually sees (the server may be
        # Windows Python bridging to WSL, or Python running inside WSL itself).
        "diagnostics": {
            "python": sys.executable,
            "sys_platform": sys.platform,
            "which_claude": shutil.which("claude"),
            "which_wsl": shutil.which("wsl"),
        },
    }


@router.get("/doctor")
def doctor(request: Request) -> dict:
    from pv_extractor.system.doctor import collect_doctor_checks

    checks = collect_doctor_checks(_config(request))
    return {"checks": [c.model_dump() for c in checks], "all_ok": all(c.ok for c in checks)}


# ---------------------------------------------------------------------------
# index metadata (wizard step a)
# ---------------------------------------------------------------------------


@router.get("/fs/list")
def fs_list(path: str = "", files: bool = False) -> dict:
    """Subdirectories of a path for the folder picker (read-only — nothing
    but os.scandir). Empty path = the platform's roots: Windows drive
    letters, or '/' elsewhere. With files=true also returns the path's files
    (the 'Add a missed file' picker in New Run > Confirm documents)."""
    home = str(Path.home())
    if not path:
        if hasattr(os, "listdrives"):  # Windows, Python >= 3.12
            roots = [{"name": d, "path": d} for d in os.listdrives()]
        else:
            roots = [{"name": "/", "path": "/"}]
        return {"path": "", "parent": None, "dirs": roots, "files": [], "home": home}
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(404, detail=f"not a folder: {path}")
    entries: list[dict] = []
    file_entries: list[dict] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        entries.append({"name": entry.name, "path": str(Path(entry.path))})
                    elif files and entry.is_file(follow_symlinks=False):
                        file_entries.append({"name": entry.name, "path": str(Path(entry.path))})
                except OSError:
                    continue  # unreadable entry — skip, never fail the listing
    except OSError as exc:
        raise HTTPException(409, detail=f"cannot list {path}: {exc}") from exc
    entries.sort(key=lambda d: str(d["name"]).lower())
    file_entries.sort(key=lambda d: str(d["name"]).lower())
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": entries, "files": file_entries, "home": home}


@router.get("/index/status")
def index_status(request: Request) -> dict:
    """Index health for the Settings screen: is the database built, how much
    is in it, and is pv_root even reachable from this machine."""
    config = _config(request)
    db_path = Path(config.db_path)
    out: dict = {
        "db_path": str(db_path),
        "pv_root": config.pv_root,
        "pv_root_exists": Path(config.pv_root).exists(),
        "ready": False,
        "files": 0,
        "clients": 0,
        "db_error": None,
        "relocation": getattr(request.app.state, "db_relocation", None),
    }
    if db_path.exists():
        try:
            conn = db.open_db_readonly(db_path)
            try:
                if _index_ready(conn):
                    out["ready"] = True
                    out["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    out["clients"] = conn.execute(
                        "SELECT COUNT(DISTINCT client) FROM files WHERE client IS NOT NULL"
                    ).fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — surface a clear reason instead of a 500
            out["db_error"] = f"could not read index at {config.db_path}: {exc}"
    return out


def _inspect_index_db(path: Path) -> dict:
    """Read-only peek at a candidate index DB for the Settings picker:
    file/client counts when readable, else why not (locked / wrong schema /
    cross-filesystem I/O). Never raises."""
    import sqlite3
    from datetime import datetime, timezone

    info: dict = {
        "path": str(path), "files": None, "clients": None,
        "readable": False, "detail": "",
        "size_bytes": 0, "modified": None,
    }
    try:
        stat = path.stat()
        info["size_bytes"] = stat.st_size
        info["modified"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    except OSError:
        pass
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            info["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            info["clients"] = conn.execute(
                "SELECT COUNT(DISTINCT client) FROM files WHERE client IS NOT NULL"
            ).fetchone()[0]
            info["readable"] = True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — a bad/locked candidate degrades, never fails the scan
        info["detail"] = str(exc)
    return info


@router.get("/index/discover")
def index_discover(request: Request) -> dict:
    """Find existing index databases so the analyst can adopt one instead of
    building from scratch (the 'autodetect an existing DB' flow). Scans the
    configured db_path's folder, output_dir, and the checkout's ./output for
    `*.db` files; each is peeked read-only for its file/client counts. The
    browse-for-file picker handles anything outside these spots."""
    config = _config(request)
    config_path = Path(request.app.state.config_path)
    current = Path(config.db_path)

    candidate_dirs: list[Path] = []
    for raw in (current.parent, Path(config.output_dir), config_path.parent / "output"):
        try:
            resolved = raw.resolve()
        except OSError:
            continue
        if resolved not in candidate_dirs:
            candidate_dirs.append(resolved)

    found: dict[str, dict] = {}
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.db")):
            key = str(f.resolve())
            if key in found:
                continue
            entry = _inspect_index_db(f)
            entry["is_current"] = key == str(current.resolve()) if current else False
            found[key] = entry

    return {
        "current_db_path": str(current),
        "current_exists": current.exists(),
        "scanned_dirs": [str(d) for d in candidate_dirs],
        "found": list(found.values()),
    }


def _files_under(conn, prefix: str) -> int:
    """Count indexed files under a folder via a range scan on the UNIQUE
    file_path index — a LIKE would full-scan the table per folder, which is
    minutes (not milliseconds) on a real share index. The upper bound is the
    prefix + U+10FFFF (max code point, never in real file names)."""
    sep = "\\" if "\\" in prefix else "/"
    if not prefix.endswith(sep):
        prefix += sep
    return conn.execute(
        "SELECT COUNT(*) FROM files WHERE file_path >= ? AND file_path < ?",
        (prefix, prefix + "\U0010ffff"),
    ).fetchone()[0]


@router.get("/index/clients-status")
def index_clients_status(request: Request) -> dict:
    """Top-level folders of pv_root (the client folders) plus how many files
    the index currently holds under each — drives selective scanning, so a
    huge share never needs a full up-front walk."""
    config = _config(request)
    root = Path(config.pv_root)
    if not root.is_dir():
        raise HTTPException(409, detail=(
            f"pv_root is not reachable from this machine: {config.pv_root}"
        ))
    folders: list[dict] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append({"name": entry.name, "path": str(Path(entry.path)), "files": 0})
                except OSError:
                    continue
    except OSError as exc:
        raise HTTPException(409, detail=f"cannot list pv_root: {exc}") from exc
    folders.sort(key=lambda f: str(f["name"]).lower())
    db_path = Path(config.db_path)
    db_error: str | None = None
    if db_path.exists():
        try:
            conn = db.open_db_readonly(db_path)
            try:
                if _index_ready(conn):
                    for folder in folders:
                        folder["files"] = _files_under(conn, str(folder["path"]))
                # Per-client last-scan timestamps (recorded by the scan job) for
                # the "last scan N ago" hint — None when never scanned here.
                meta = db.all_meta(conn)
                for folder in folders:
                    folder["last_scan"] = meta.get(f"last_scan:{folder['name']}")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — a bad/unreachable DB degrades counts, never 500s the list
            db_error = f"could not read index at {config.db_path}: {exc}"
    return {"pv_root": str(root), "folders": folders, "db_error": db_error}


@router.post("/index/scan")
def index_scan(request: Request, body: ScanRequest | None = None) -> dict:
    """Build/refresh the file index as a background job — the GUI counterpart
    of `pv-extractor scan` (same init_schema + scan_tree calls).

    Selective by design: `clients` scans only those top-level folders (the
    recommended path on the real share); `root` scans one subtree; neither =
    full pv_root walk. Emits throttled `scan_progress` events with running
    counters, the directory being walked, and the previous indexed count under
    the same root (the GUI's ETA baseline for rescans)."""
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.scan_tree import scan_tree

    from pv_extractor.indexer.deals import refresh_deals
    from pv_extractor.normalize import relative_segments

    config = _config(request)
    quick = bool(body.quick) if body else False
    # Smart Scan (deterministic heuristics) vs LLM-Assisted Scan (the local
    # claude -p deal-discovery pass). use_llm=False is the deterministic path;
    # True opts the end-of-scan refresh into the assist with the chosen
    # model/effort. None model/effort defer to config.deal_discovery.llm.
    use_llm = bool(body.use_llm) if body else False
    llm_model = body.llm_model if body else None
    llm_effort = body.llm_effort if body else None
    roots: list[str] = []
    if body and body.clients:
        for name in body.clients:
            p = Path(config.pv_root) / name
            if not p.is_dir():
                raise HTTPException(409, detail=f"no such client folder under pv_root: {name!r}")
            roots.append(str(p))
    else:
        root = (body.root if body else None) or config.pv_root
        if not Path(root).exists():
            raise HTTPException(409, detail=(
                f"scan root {root!r} does not exist or is not reachable from this machine"
            ))
        roots.append(str(root))

    def _scan(emit, should_stop) -> dict:
        import time
        from datetime import datetime, timezone

        conn = open_db(config.db_path, config.pv_root)
        agg = {"files_seen": 0, "added": 0, "updated": 0, "unchanged": 0, "removed": 0, "errors": 0}
        stopped_early = False
        started = time.monotonic()
        try:
            init_schema(conn)
            for i, scan_root in enumerate(roots):
                if should_stop():
                    stopped_early = True
                    break
                prev_total = _files_under(conn, scan_root)
                base = {
                    "root": scan_root, "root_index": i + 1, "roots_total": len(roots),
                    "prev_total": prev_total,
                }
                emit("scan_progress", {
                    **base, **agg, "dir": scan_root,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                })

                def _on_progress(stats, current_dir, _base=base, _before=dict(agg)):
                    emit("scan_progress", {
                        **_base,
                        "files_seen": _before["files_seen"] + stats.files_seen,
                        "added": _before["added"] + stats.added,
                        "updated": _before["updated"] + stats.updated,
                        "unchanged": _before["unchanged"] + stats.unchanged,
                        "removed": _before["removed"],
                        "errors": _before["errors"] + stats.errors,
                        "dir": current_dir,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    })

                stats = scan_tree(
                    conn, scan_root, config, progress=_on_progress, should_stop=should_stop, quick=quick
                )
                for key in agg:
                    agg[key] += getattr(stats, key)
                stopped_early = stopped_early or stats.stopped_early

            # Smart deal discovery over the affected clients: a pv_root walk
            # refreshes every client, a subtree scan only its owning client.
            # A PAUSED scan still refreshes — the analyst works with what is
            # indexed so far and the next scan continues incrementally.
            affected: list[str] | None = []
            for scan_root in roots:
                rel = relative_segments(scan_root, config.pv_root)
                if not rel:
                    affected = None  # pv_root itself was scanned
                    break
                if affected is not None and rel[0] not in affected:
                    affected.append(rel[0])
            emit("scan_progress", {
                **agg,
                "dir": "discovering deal folders (LLM-assisted)…" if use_llm else "discovering deal folders…",
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })
            discovered = refresh_deals(
                conn, config, affected,
                use_llm=True if use_llm else None,
                llm_model=llm_model,
                llm_effort=llm_effort,
            )
            agg["deals_discovered"] = sum(len(d) for d in discovered.values())
            agg["deal_clients"] = len(discovered)
            # Record per-client last-scan time (drives the "last scan N ago"
            # hint in Settings). A full pv_root walk stamps every client.
            scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            scanned_clients = affected if affected is not None else db.distinct_clients(conn)
            for scanned_client in scanned_clients:
                db.set_meta(conn, f"last_scan:{scanned_client}", scanned_at)
        finally:
            conn.close()
        return {"roots": roots, "quick": quick, **agg, "stopped_early": stopped_early,
                "elapsed_seconds": round(time.monotonic() - started, 1)}

    job = _manager(request).start_utility(
        "scan",
        {"roots": roots, "quick": quick, "deal_discovery": "llm" if use_llm else "smart"},
        _scan,
    )
    return {"job": job.model_dump()}


@router.get("/index/clients")
def clients(request: Request) -> dict:
    conn = _open_index(_config(request))
    try:
        return {"clients": db.distinct_clients(conn)}
    finally:
        conn.close()


def _deal_folder_payload(config: Config, folder) -> dict:
    ev = folder.evidence
    return {
        "name": folder.name,
        "confidence": folder.confidence,
        "method": folder.method,
        "low_confidence": folder.confidence < config.deal_discovery.review_confidence,
        "folder_paths": folder.folder_paths,
        "periods": ev.period_children + ev.period_recurrence,
        "file_count": ev.total_files,
        "memo_file_count": ev.memo_keyword_files,
        "llm_corroborated": ev.llm_corroborated,
    }


def _display_floor(config: Config, override: float | None) -> float:
    """The confidence floor for SHOWING discovered deals (storage keeps all)."""
    floor = config.deal_discovery.display_min_confidence if override is None else override
    return max(0.0, min(1.0, floor))


@router.get("/index/deals")
def deals(request: Request, client: str, min_confidence: float | None = None) -> dict:
    """Deal names for the wizard dropdown plus the discovered deal-folder
    detail (confidence, evidence, paths) when discovery has run. Discovered
    folders below the display floor (config.deal_discovery.display_min_confidence,
    or the ?min_confidence override) are hidden — nothing is dropped from
    storage, so lowering the floor in Settings reveals them again."""
    config = _config(request)
    conn = _open_index(config)
    floor = _display_floor(config, min_confidence)
    try:
        folders = db.deal_folders_for_client(conn, client)
        kept = [f for f in folders if f.confidence >= floor]
        # Names: keep those with a kept folder, plus any name with no discovered
        # folder at all (legacy / un-scored deals are never hidden by the floor).
        scored_names = {f.name for f in folders}
        kept_names = {f.name for f in kept}
        names = [
            d for d in db.deals_for_client(conn, client)
            if d not in scored_names or d in kept_names
        ]
        return {
            "deals": names,
            "deal_folders": [_deal_folder_payload(config, f) for f in kept],
            "display_min_confidence": floor,
            "hidden_below_floor": len(folders) - len(kept),
            "last_llm_discovery": db.last_llm_discovery(conn, client),
        }
    finally:
        conn.close()


@router.post("/index/deals/refresh")
def deals_refresh(request: Request, body: DealRefreshRequest) -> dict:
    """Re-run smart deal discovery for one client as a background job —
    optionally with the Claude Code assist pass (hidden local `claude -p`
    call over the folder inventory; never the SDK, never an API key)."""
    from pv_extractor.indexer.deals import refresh_deals
    from pv_extractor.indexer.db import init_schema, open_db

    config = _config(request)

    def _refresh(emit) -> dict:
        conn = open_db(config.db_path, config.pv_root)
        try:
            init_schema(conn)
            results = refresh_deals(
                conn, config, [body.client],
                use_llm=body.llm or None,
                llm_model=body.llm_model, llm_effort=body.llm_effort,
                apply_learning=body.apply_learning,
            )
        finally:
            conn.close()
        found = results.get(body.client, [])
        floor = _display_floor(config, None)
        kept = [f for f in found if f.confidence >= floor]
        return {
            "client": body.client,
            "llm": body.llm,
            "apply_learning": body.apply_learning,
            "deals": [_deal_folder_payload(config, f) for f in kept],
            "display_min_confidence": floor,
            "hidden_below_floor": len(found) - len(kept),
        }

    job = _manager(request).start_utility(
        "deal_discovery",
        {"client": body.client, "llm": body.llm, "apply_learning": body.apply_learning},
        _refresh,
    )
    return {"job": job.model_dump()}


@router.post("/index/deals/feedback")
def deals_feedback(request: Request, body: DealFeedbackRequest) -> dict:
    """Record one analyst correction (add/remove/merge/split/rename) to deal
    discovery for a client, then re-run discovery for that client (applying the
    learning layer) as a background job. The result carries the refreshed deal
    folders plus the now-active client-scoped layout priors."""
    from pv_extractor.indexer import deal_learning
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.deals import refresh_deals

    config = _config(request)

    def _feedback(emit) -> dict:
        conn = open_db(config.db_path, config.pv_root)
        try:
            init_schema(conn)
            try:
                deal_learning.record_correction(
                    conn, client=body.client, deal=body.deal, action=body.action,
                    folder_path=body.folder_path, payload=body.payload,
                )
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc)) from exc
            results = refresh_deals(conn, config, [body.client])
            found = results.get(body.client, [])
            priors = deal_learning.derive_layout_priors(conn, config, body.client)
        finally:
            conn.close()
        return {
            "client": body.client,
            "deals": [_deal_folder_payload(config, f) for f in found],
            "learned": priors,
        }

    job = _manager(request).start_utility(
        "deal_feedback",
        {"client": body.client, "deal": body.deal, "action": body.action},
        _feedback,
    )
    return {"job": job.model_dump()}


@router.get("/index/deals/learned")
def deals_learned(request: Request, client: str) -> dict:
    """The deal-admin panel: the client's last-cached layout priors plus every
    recorded correction (read-only)."""
    from pv_extractor.indexer import deal_learning

    config = _config(request)
    conn = _open_index(config)
    try:
        return {
            "client": client,
            "priors": deal_learning.cached_layout_priors(conn, client),
            "corrections": deal_learning.list_corrections(conn, client),
        }
    finally:
        conn.close()


@router.delete("/index/deals/feedback/{feedback_id}")
def deals_feedback_delete(request: Request, feedback_id: int) -> dict:
    """Remove one recorded correction by id."""
    from pv_extractor.indexer import deal_learning

    config = _config(request)
    conn = _open_index(config)
    try:
        return {"deleted": deal_learning.delete_correction(conn, feedback_id)}
    finally:
        conn.close()


@router.get("/index/search/clients")
def search_clients(request: Request, q: str, limit: int = 8) -> dict:
    """Manual mode: fuzzy-rank indexed client folders against typed input
    (aliases.yaml expansions included)."""
    from rapidfuzz import fuzz

    from pv_extractor.locator.aliases import expansions_for, load_aliases
    from pv_extractor.normalize import normalize_text

    config = _config(request)
    aliases = load_aliases(config.aliases_path_resolved())
    needle = normalize_text(q)
    conn = _open_index(config)
    try:
        known = db.distinct_clients(conn)
    finally:
        conn.close()
    scored = []
    for name in known:
        ratio = max(
            (fuzz.token_set_ratio(needle, normalize_text(exp))
             for exp in expansions_for(name, aliases.clients)),
            default=0.0,
        )
        if ratio > 0:
            scored.append({"client": name, "score": round(ratio, 1)})
    scored.sort(key=lambda e: (-e["score"], e["client"].lower()))
    return {"matches": scored[:limit]}


@router.get("/index/search/deals")
def search_deals(request: Request, client: str, q: str, limit: int = 8) -> dict:
    """Manual mode: fuzzy-rank the client's discovered deal folders against
    typed input; every match carries its relative folder path(s) so the
    analyst can confirm the right folder before launching."""
    from rapidfuzz import fuzz

    from pv_extractor.locator.aliases import expansions_for, load_aliases
    from pv_extractor.normalize import normalize_text

    config = _config(request)
    aliases = load_aliases(config.aliases_path_resolved())
    needle = normalize_text(q)
    conn = _open_index(config)
    try:
        folders = db.deal_folders_for_client(conn, client)
        if not folders:  # discovery not run yet: fall back to files.deal names
            folders = []
            names = db.deals_for_client(conn, client)
    finally:
        conn.close()
    scored = []
    if folders:
        for folder in folders:
            ratio = max(
                (fuzz.token_set_ratio(needle, normalize_text(exp))
                 for exp in expansions_for(folder.name, aliases.deals)),
                default=0.0,
            )
            if ratio > 0:
                scored.append({**_deal_folder_payload(config, folder), "score": round(ratio, 1)})
    else:
        for name in names:
            ratio = fuzz.token_set_ratio(needle, normalize_text(name))
            if ratio > 0:
                scored.append({
                    "name": name, "score": round(ratio, 1), "confidence": None,
                    "method": "legacy", "low_confidence": False, "folder_paths": [],
                    "periods": 0, "file_count": 0, "memo_file_count": 0,
                    "llm_corroborated": False,
                })
    scored.sort(key=lambda e: (-e["score"], e["name"].lower()))
    return {"matches": scored[:limit]}


@router.get("/index/search/periods")
def search_periods(request: Request, client: str, deal: str, q: str) -> dict:
    """Manual mode: resolve typed period input ('Q1 2025', '3 31 2025',
    '3.31.25', 'FY2025', 'March 2025'...) to an as-of date under the client's
    period style and rank the deal's indexed periods around it — exact match
    first, then nearest. Each entry carries the date folders it came from."""
    from pv_extractor.indexer.periods import resolve_target_period

    config = _config(request)
    # '3 31 2025' style: spaces between numbers are separators periods.py
    # already understands via the shared separator class.
    target = resolve_target_period(q, config.client_period_style(client))
    conn = _open_index(config)
    try:
        rows = conn.execute(
            "SELECT as_of_date, GROUP_CONCAT(DISTINCT date_folder) AS folders "
            "FROM files WHERE client = ? AND deal = ? AND as_of_date IS NOT NULL "
            "GROUP BY as_of_date ORDER BY as_of_date DESC",
            (client, deal),
        ).fetchall()
    finally:
        conn.close()
    style = config.client_period_style(client)
    entries = []
    for row in rows:
        as_of = date_type.fromisoformat(row["as_of_date"])
        entries.append({
            "as_of_date": as_of.isoformat(),
            "label": period_label(as_of, style),
            "date_folders": sorted(set((row["folders"] or "").split(","))),
            "exact": target is not None and as_of == target,
            "distance_days": abs((as_of - target).days) if target is not None else None,
        })
    if target is not None:
        entries.sort(key=lambda e: (not e["exact"], e["distance_days"]))
    return {
        "resolved_as_of": target.isoformat() if target else None,
        "resolved_label": period_label(target, style) if target else None,
        "parse_error": None if target else (
            f"could not resolve {q!r} to an as-of date — try '2025-03-31', '3.31.25', "
            f"'Q1 2025', 'March 2025' or 'FY2025'"
        ),
        "matches": entries,
    }


# ---------------------------------------------------------------------------
# Smart Search (Search & Selection Revamp, Phase B): free-text -> DocTypeSpec
# profiles, a live ranking preview through the same locator-style scorer, and
# accept/reject learning. Synchronous like the other /index/search lookups —
# these are fast (no full discovery/scan). The rule engine is self-sufficient:
# the optional Claude Code CLI fallback only assists and never blocks.
# ---------------------------------------------------------------------------


@router.get("/search/profiles")
def search_profiles(request: Request) -> dict:
    """Every Smart Search DocTypeSpec profile — the seeded builtins (so they
    always show) plus learned profiles. Each carries its ``builtin`` flag so the
    GUI can mark builtins as forkable-not-deletable."""
    from pv_extractor.search import doc_type_spec as profiles

    config = _config(request)
    conn = _open_index(config)
    try:
        profiles.seed_builtins(conn, config)
        rows = db.list_doc_type_profiles(conn)
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            spec = DocTypeSpec.model_validate_json(row["spec"])
        except Exception:  # noqa: BLE001 — a corrupt row is skipped, never fatal
            continue
        out.append({**spec.model_dump(), "builtin": row["builtin"]})
    return {"profiles": out}


@router.post("/search/profiles/resolve")
def search_resolve(request: Request, body: IntentResolveRequest) -> dict:
    """Resolve a free-text query into a DocTypeSpec via the rule engine, with an
    optional one-shot Claude Code CLI augmentation. NEVER raises if the CLI is
    unavailable — resolve_intent degrades to the rule spec on any failure.
    ``use_cli`` defaults to config.smart_search.use_cli_fallback."""
    from pv_extractor.search import intent

    config = _config(request)
    conn = _open_index(config)
    try:
        spec, provenance = intent.resolve_intent(
            body.query, config, conn=conn, use_cli=body.use_cli
        )
    finally:
        conn.close()
    return {"spec": spec.model_dump(), "provenance": provenance}


@router.post("/search/profiles")
def search_save_profile(request: Request, body: ProfileSaveRequest) -> dict:
    """Save/update a learned profile. Builtins are forkable but NOT
    overwritable: a save whose slug collides with a seeded builtin is refused
    (HTTP 409) — fork it under a different slug instead."""
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.search import doc_type_spec as profiles

    config = _config(request)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        profiles.seed_builtins(conn, config)
        existing = db.get_doc_type_profile(conn, body.spec.slug)
        if existing is not None and existing["builtin"]:
            raise HTTPException(
                409,
                detail=(
                    f"{body.spec.slug!r} is a builtin profile and cannot be "
                    "overwritten — save under a different slug to fork it"
                ),
            )
        profiles.save_profile(conn, body.spec, query_seed=body.query_seed, builtin=False)
    finally:
        conn.close()
    return {"spec": body.spec.model_dump()}


@router.delete("/search/profiles/{slug}")
def search_delete_profile(request: Request, slug: str) -> dict:
    """Delete a learned profile. Builtins are not deletable -> deleted false."""
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.search import doc_type_spec as profiles

    config = _config(request)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        deleted = profiles.delete_profile(conn, slug)
    finally:
        conn.close()
    return {"deleted": deleted}


@router.post("/search/preview")
def search_preview(request: Request, body: SearchPreviewRequest) -> dict:
    """Live ranking preview: score the indexed files against an inline
    DocTypeSpec OR a profile/doc-type slug, optionally scoped to a client/deal
    and a target period, through the same rank.rank_files scorer the locator
    uses. Returns the ranked file dicts."""
    from pv_extractor.indexer.periods import resolve_target_period
    from pv_extractor.search import doc_type_spec as profiles
    from pv_extractor.search import rank

    config = _config(request)
    conn = _open_index(config)
    try:
        if isinstance(body.spec_or_slug, DocTypeSpec):
            spec = body.spec_or_slug
        else:
            spec = profiles.resolve_spec(conn, body.spec_or_slug, config)
            if spec is None:
                raise HTTPException(404, detail=f"no profile/doc-type for {body.spec_or_slug!r}")
        target_as_of = None
        if body.period:
            target_as_of = resolve_target_period(
                body.period, config.client_period_style(body.client or "default")
            )
        results = rank.rank_files(
            conn, config, spec,
            client=body.client, deal=body.deal, target_as_of=target_as_of,
        )
    finally:
        conn.close()
    return {"results": results}


@router.post("/search/feedback")
def search_feedback(request: Request, body: SearchFeedbackRequest) -> dict:
    """Record one accept (+1) / reject (-1) signal for a profile's ranking and
    return the profile's updated effective weight_overrides (the live learning
    state)."""
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.search import doc_type_spec as profiles
    from pv_extractor.search import rank

    config = _config(request)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        try:
            rank.record_search_feedback(
                conn,
                profile_slug=body.profile_slug,
                file_path=body.file_path,
                label=body.label,
                context=body.context,
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        spec = profiles.get_profile(conn, body.profile_slug)
        if spec is None:
            # No stored profile (e.g. an inline-spec preview); the feedback is
            # still recorded by slug. Surface only the freshly learned nudges.
            spec = DocTypeSpec(slug=body.profile_slug, label=body.profile_slug)
        effective = rank.effective_weight_overrides(conn, spec, config)
    finally:
        conn.close()
    return {"ok": True, "effective_weights": effective}


@router.get("/index/periods")
def periods(request: Request, client: str | None = None, deal: str | None = None) -> dict:
    """Reporting periods that exist in the index, newest first, DEDUPED to the
    client-style label (one 'Q1 2026', never one per underlying date folder).

    Many distinct date folders can map to the same reporting period — e.g. for a
    calendar-quarterly client, deals filed under 1.31, 2.28 and 3.31 are all
    'Q1 2026'. We collapse them so the picker shows the quarter ONCE. The submit
    value is the label itself ('period'); with locator.tolerate_same_period a run
    on that label finds every deal in the quarter regardless of its month-end.
    `as_of_date` is the representative (latest) underlying date, for display."""
    config = _config(request)
    conn = _open_index(config)
    try:
        if client and deal:
            dates = db.as_of_dates_for_deal(conn, client, deal)
        elif client:
            rows = conn.execute(
                "SELECT DISTINCT as_of_date FROM files WHERE client = ? AND as_of_date IS NOT NULL",
                (client,),
            ).fetchall()
            dates = sorted({date_type.fromisoformat(r[0]) for r in rows})
        else:
            rows = conn.execute(
                "SELECT DISTINCT as_of_date FROM files WHERE as_of_date IS NOT NULL"
            ).fetchall()
            dates = sorted({date_type.fromisoformat(r[0]) for r in rows})
    finally:
        conn.close()
    style = config.client_period_style(client or "default")
    # Group by reporting-period label; keep the latest underlying date as the
    # representative. dates is ascending, so the last write per label is newest.
    by_label: dict[str, date_type] = {}
    for d in dates:
        by_label[period_label(d, style)] = d
    # Newest period first (by the representative date).
    ordered = sorted(by_label.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "periods": [
            {"period": label, "label": label, "as_of_date": rep.isoformat()}
            for label, rep in ordered
        ]
    }


@router.get("/index/periods/expand")
def periods_expand(request: Request, start: str, end: str, client: str | None = None) -> dict:
    """Expand an inclusive period range (start..end) into the ordered periods
    under the client's cadence, for the multi-period range picker. Each carries
    its label and resolved as-of date (ISO) so it round-trips under any style."""
    from pv_extractor.indexer.periods import expand_period_range, resolve_target_period

    config = _config(request)
    style = config.client_period_style(client or "default")
    try:
        labels = expand_period_range(start, end, style)
    except ValueError as exc:
        return {"periods": [], "error": str(exc)}
    out = []
    for label in labels:
        as_of = resolve_target_period(label, style)
        # 'period' (the submit value) is the label, so a range round-trips as
        # quarter labels and matches every deal in each quarter (see /index/periods).
        out.append({"period": label, "label": label, "as_of_date": as_of.isoformat() if as_of else label})
    return {"periods": out, "error": None}


@router.get("/index/doc-types")
def doc_types() -> dict:
    return {"doc_types": [d.value for d in DocType]}


@router.get("/templates")
def templates(request: Request) -> dict:
    """Workbook choices for wizard step b: the reference template plus
    previous run outputs (newest first, for cumulative runs)."""
    from pv_extractor.api import runs_service

    config = _config(request)
    default = Path(__file__).resolve().parents[3] / "reference" / "master_index_v4.xlsx"
    outputs = []
    for run_dir in runs_service.run_dirs(config):
        wb = runs_service.workbook_path(run_dir)
        if wb is not None:
            outputs.append({"run_id": run_dir.name, "path": str(wb)})
    return {"default_template": str(default), "previous_outputs": outputs}


# ---------------------------------------------------------------------------
# model menu + pricing
# ---------------------------------------------------------------------------


@router.get("/models")
def models(request: Request) -> dict:
    from pv_extractor.llm.model_registry import ModelEntry, ModelRegistry

    config = _config(request)
    try:
        registry = ModelRegistry.load(config.llm.models_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, detail=f"models.yaml unusable: {exc}") from exc
    provider = config.llm.provider
    provider_models = registry.entries_for_provider(provider)
    if provider != "claude" and not provider_models:
        provider_models = [
            ModelEntry(
                provider=provider,
                alias="provider-default",
                id="",
                display_name=f"{provider} CLI default",
                context_window=0,
                default_effort=config.codex_cli.reasoning_effort,
                pricing_per_mtok=None,
            )
        ]
    return {
        "last_reviewed": registry.menu.last_reviewed,
        "models_path": str(config.llm.models_path),
        "provider": provider,
        "models": [e.model_dump() for e in provider_models],
        "all_models": [e.model_dump() for e in registry.entries],
        "llm": {
            "enabled": config.llm.enabled,
            "provider": provider,
            "routing_mode": config.llm.routing_mode,
            "mode": config.llm.mode,
            "single_model_provider": config.llm.single_model_provider,
            "single_model_model": config.llm.single_model_model,
            "single_model_effort": config.llm.single_model_effort,
            "manual_model": config.llm.manual_model,
            "manual_effort": config.llm.manual_effort,
            "allow_fable": config.llm.allow_fable,
            "budget_usd": config.llm.budget_usd,
            "auto": config.llm.auto.model_dump(),
        },
    }


@router.put("/models/{alias}/pricing")
def update_pricing(alias: str, body: PricingUpdate, request: Request) -> dict:
    config = _config(request)
    try:
        update_model_pricing(
            Path(config.llm.models_path), config.pv_root, alias,
            body.model_dump(exclude={"last_reviewed"}), last_reviewed=body.last_reviewed,
        )
    except YamlEditError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return models(request)


# ---------------------------------------------------------------------------
# editable config surface
# ---------------------------------------------------------------------------


@router.get("/config")
def get_config(request: Request) -> dict:
    config = _config(request)
    return {
        "config_path": str(request.app.state.config_path),
        "pv_root": config.pv_root,
        "output_dir": str(config.output_dir),
        "db_path": str(config.db_path),
        "claude_code": config.claude_code.model_dump(),
        "codex_cli": config.codex_cli.model_dump(),
        "first_run": config.first_run.model_dump(),
        "gui": config.gui.model_dump(),
        "llm": config.llm.model_dump(exclude={"confidence_scores"}),
        "extraction": {"confidence_threshold": config.extraction.confidence_threshold},
        "deal_discovery": {"display_min_confidence": config.deal_discovery.display_min_confidence},
        "selection": {"min_confidence": config.selection.min_confidence},
    }


@router.put("/config")
def put_config(body: ConfigUpdate, request: Request) -> dict:
    config_path = Path(request.app.state.config_path)
    config = _config(request)
    for dotted in body.values:
        if not dotted.startswith(_EDITABLE_PREFIXES):
            raise HTTPException(400, detail=f"{dotted!r} is not GUI-editable (whitelist: {_EDITABLE_PREFIXES})")
    try:
        update_config_yaml(config_path, config.pv_root, body.values)
    except (YamlEditError, ValueError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    # Reload so subsequent requests (and new jobs) see the change.
    fresh = load_config(config_path)
    request.app.state.config = fresh
    request.app.state.jobs.config = fresh
    return get_config(request)


@router.get("/config/raw")
def get_config_raw(request: Request) -> dict:
    """The full config.yaml text (comments included) for the advanced editor."""
    config_path = Path(request.app.state.config_path)
    with open_read(config_path) as fh:
        text = fh.read().decode("utf-8")
    return {"config_path": str(config_path), "text": text}


@router.put("/config/raw")
def put_config_raw(body: RawConfigUpdate, request: Request) -> dict:
    """Replace config.yaml wholesale. The text is validated through the typed
    loader first — an invalid edit never lands on disk."""
    config_path = Path(request.app.state.config_path)
    config = _config(request)
    try:
        replace_config_yaml(config_path, config.pv_root, body.text)
    except (YamlEditError, ValueError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    fresh = load_config(config_path)
    request.app.state.config = fresh
    request.app.state.jobs.config = fresh
    return get_config_raw(request)
