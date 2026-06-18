"""Filesystem scanner (Loader 2) with incremental refresh.

Walks `root` with an iterative os.scandir stack (no os.walk, one lstat per
entry). Symlinks and NTFS reparse points (junctions) are never followed;
unreadable directories land in scan_errors without killing the run; OneDrive
cloud-only placeholders are indexed but flagged. A preloaded snapshot of the
index under `root` makes rescans incremental: unchanged files (same size and
mtime to the second) are skipped, vanished paths are deleted.

`root` may be any directory, but derive_record refuses paths outside
config.pv_root — callers scan under pv_root (tests point pv_root at a tmp dir).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from pv_extractor.config import Config
from pv_extractor.indexer.db import (
    add_scan_error,
    all_meta,
    delete_paths,
    file_meta_under,
    insert_records,
    set_meta,
)
from pv_extractor.indexer.derive import derive_record
from pv_extractor.io_guard import is_under_pv_root
from pv_extractor.logging_setup import log_event
from pv_extractor.models import FileRecord, ScanError
from pv_extractor.normalize import strip_extended_prefix, to_extended_path

logger = logging.getLogger(__name__)

# index_meta key prefix for per-root "last completed scan started at" timestamps
# (the quick-rescan baseline). Keyed by the scan root string as passed in.
_LAST_SCAN_PREFIX = "last_scan:"

# Windows file-attribute bits (st_file_attributes); defined locally so the
# module also works on POSIX where the stat module lacks them.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_PLACEHOLDER_ATTRIBUTES = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


class ScanStats(BaseModel):
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0
    stopped_early: bool = False  # paused via should_stop; everything walked so far is committed


def stat_is_cloud_placeholder(st: os.stat_result) -> bool:
    """True for OneDrive/Files-On-Demand cloud-only placeholders (present in
    the namespace but not hydrated locally). Always False on POSIX."""
    if not hasattr(st, "st_file_attributes"):
        return False
    return bool(st.st_file_attributes & _CLOUD_PLACEHOLDER_ATTRIBUTES)


def _is_reparse_point(st: os.stat_result) -> bool:
    if not hasattr(st, "st_file_attributes"):
        return False
    return bool(st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _record_error(conn: sqlite3.Connection, stats: ScanStats, path: str, exc: OSError) -> None:
    add_scan_error(
        conn,
        ScanError(path=path, error_type=type(exc).__name__, message=str(exc), seen_at=datetime.now()),
    )
    stats.errors += 1


def _parent_dir(path: str) -> str:
    """Parent directory of a path, separator-agnostic (the index may hold
    either Windows or POSIX paths). Empty string if there is no separator."""
    idx = max(path.rfind("/"), path.rfind("\\"))
    return path[:idx] if idx > 0 else ""


def _index_dir_shape(
    known: dict[str, tuple[int | None, str | None]], root_clean: str
) -> tuple[dict[str, list[str]], set[str]]:
    """From the indexed file paths derive (files grouped by their parent
    directory, set of directories that held at least one subdirectory last
    scan). A directory is a quick-rescan-skippable LEAF iff it has files but is
    NOT in the returned set — listing such a folder again can only re-confirm
    what we already have (a new/removed entry would have bumped its mtime)."""
    files_by_dir: dict[str, list[str]] = {}
    for path in known:
        files_by_dir.setdefault(_parent_dir(path), []).append(path)
    has_subdir: set[str] = set()
    root_len = len(root_clean)
    for d in list(files_by_dir):
        cur = d
        while True:
            parent = _parent_dir(cur)
            if not parent or parent == cur or len(parent) < root_len or parent in has_subdir:
                break  # reached the root, or ancestors already recorded
            has_subdir.add(parent)
            cur = parent
    return files_by_dir, has_subdir


def _quick_baseline(conn: sqlite3.Connection, root: str, margin_seconds: int) -> str | None:
    """ISO mtime cutoff for the quick rescan: leaf folders untouched before
    this point are taken as unchanged. The baseline is the start time of the
    most recent COMPLETED scan that covered `root` (the root itself or any
    ancestor it was scanned under), minus a clock-skew margin. None when no
    such scan is on record (the first quick rescan then behaves like a full
    one and records a baseline for next time)."""
    best: str | None = None
    for key, value in all_meta(conn).items():
        if not key.startswith(_LAST_SCAN_PREFIX):
            continue
        base_root = key[len(_LAST_SCAN_PREFIX) :]
        if is_under_pv_root(root, base_root) and (best is None or value > best):
            best = value
    if best is None:
        return None
    cutoff = datetime.fromisoformat(best) - timedelta(seconds=max(0, margin_seconds))
    return cutoff.isoformat(timespec="seconds")


def scan_tree(
    conn: sqlite3.Connection,
    root: str | Path,
    config: Config,
    progress: Callable[[ScanStats, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    quick: bool = False,
) -> ScanStats:
    """Scan `root` into the index, incrementally against existing rows.

    `progress`, when given, is called with (running stats, current directory)
    at most ~twice a second — the GUI turns these into live job events.

    `should_stop`, when given, is checked between directories: returning True
    pauses the scan — the pending batch is committed, stats.stopped_early is
    set, and the vanished-paths deletion is SKIPPED (an incomplete walk
    cannot prove a file is gone). Because rescans are incremental (unchanged
    files skip on size+mtime), re-running the same scan later fast-forwards
    through everything already indexed and continues from there.

    `quick` enables the opt-in mtime-prune: a LEAF folder (one that held no
    subfolders last scan) whose directory mtime predates the previous scan is
    taken as unchanged and its listing is skipped entirely — its indexed files
    are kept as-is. This skips the SMB-expensive scandir round-trip on the
    folders where documents actually live. It stays correct for new uploads
    (adding a file or folder bumps its parent's mtime, so a changed folder is
    always re-listed, and every non-leaf folder is still walked so deep
    additions are never missed). The one blind spot: a file overwritten IN
    PLACE under the same name does not bump its folder's mtime, so a quick
    rescan misses it until the next full rescan."""
    stats = ScanStats()
    last_progress = time.monotonic()
    started_iso = datetime.now().isoformat(timespec="seconds")
    scan_root = str(root)
    root_clean = strip_extended_prefix(scan_root)
    if sys.platform == "win32":
        scan_root = to_extended_path(scan_root)  # >260-char safety; no-op elsewhere
    known = file_meta_under(conn, str(root))
    pending: list[FileRecord] = []
    batch_size = config.indexer.batch_size

    # Quick rescan precompute: cutoff timestamp + the indexed folder shape so we
    # can recognise unchanged leaves. Skipped only when a baseline exists.
    cutoff_iso: str | None = None
    files_by_dir: dict[str, list[str]] = {}
    has_subdir: set[str] = set()
    if quick:
        cutoff_iso = _quick_baseline(conn, str(root), config.indexer.quick_rescan_margin_seconds)
        if cutoff_iso is not None:
            files_by_dir, has_subdir = _index_dir_shape(known, root_clean)

    stack: list[str] = [scan_root]
    while stack:
        if should_stop is not None and should_stop():
            stats.stopped_early = True
            break
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError as exc:  # PermissionError included; keep walking
            _record_error(conn, stats, strip_extended_prefix(current), exc)
            continue
        if progress is not None and (now_mono := time.monotonic()) - last_progress >= 0.5:
            last_progress = now_mono
            progress(stats, strip_extended_prefix(current))
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if _is_reparse_point(st):
                        continue  # junction/mount point: never follow
                    if entry.is_dir(follow_symlinks=False):
                        if cutoff_iso is not None:
                            child_clean = strip_extended_prefix(entry.path)
                            child_mtime = (
                                datetime.fromtimestamp(st.st_mtime).replace(microsecond=0).isoformat()
                            )
                            if (
                                child_clean in files_by_dir
                                and child_clean not in has_subdir
                                and child_mtime < cutoff_iso
                            ):
                                # Unchanged leaf: keep its indexed files (mark
                                # them seen so they are not treated as vanished)
                                # and skip the listing round-trip entirely.
                                skipped = files_by_dir.get(child_clean, ())
                                for fp in skipped:
                                    known.pop(fp, None)
                                stats.files_seen += len(skipped)
                                stats.unchanged += len(skipped)
                                continue
                        stack.append(entry.path)
                        continue
                except OSError as exc:
                    _record_error(conn, stats, strip_extended_prefix(entry.path), exc)
                    continue
                stats.files_seen += 1
                # stay pausable inside huge directories too (rescan re-walks
                # the partial directory cheaply: unchanged files just skip)
                if should_stop is not None and stats.files_seen % 256 == 0 and should_stop():
                    stats.stopped_early = True
                    break
                # also tick inside huge directories, not just between them
                if (
                    progress is not None
                    and stats.files_seen % 256 == 0
                    and (now_mono := time.monotonic()) - last_progress >= 0.5
                ):
                    last_progress = now_mono
                    progress(stats, strip_extended_prefix(current))
                path = strip_extended_prefix(entry.path)
                mtime = datetime.fromtimestamp(st.st_mtime).replace(microsecond=0)
                if path in known:
                    if known.pop(path) == (st.st_size, mtime.isoformat()):
                        stats.unchanged += 1
                        continue
                    stats.updated += 1
                else:
                    stats.added += 1
                pending.append(
                    derive_record(
                        path,
                        size_bytes=st.st_size,
                        modified_time=mtime,
                        config=config,
                        is_cloud_placeholder=stat_is_cloud_placeholder(st),
                    )
                )
                if len(pending) >= batch_size:
                    insert_records(conn, pending, batch_size)
                    pending.clear()
    if pending:
        insert_records(conn, pending, batch_size)
    if known and not stats.stopped_early:  # indexed under root but no longer on disk
        delete_paths(conn, known)
        stats.removed = len(known)
    if not stats.stopped_early:
        # Record this scan's START as the quick-rescan baseline for `root`. A
        # paused scan never records one — it did not reach the whole tree, so a
        # later quick rescan must not assume the unwalked folders are unchanged.
        set_meta(conn, f"{_LAST_SCAN_PREFIX}{root}", started_iso)
    log_event(logger, "scan_tree complete", root=str(root), quick=quick, **stats.model_dump())
    return stats
