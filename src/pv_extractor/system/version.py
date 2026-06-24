"""Build/version identity for the running server.

The GUI surfaces this in Settings so an operator can confirm WHICH build of the
code the running process is executing — the common failure mode is editing code
(or pulling a new commit) and forgetting that the already-running `pv-extractor
gui` process still holds the OLD code until it is restarted.

Everything is best-effort and computed ONCE per process (cached): the running
code cannot change under a live process, so the values describe exactly what was
loaded at startup. Git is probed via subprocess in the repo root; when git is
missing or the tree is not a checkout (e.g. a synced/packaged copy), the git
fields are simply None and only the package version is shown."""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# src/pv_extractor/system/version.py -> repo root is parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # On success return the stripped output even when empty: an empty
    # `git status --porcelain` means a CLEAN tree, which must be distinguishable
    # from "git unavailable" (None). rev-parse/log always yield non-empty.
    return (out.stdout or "").strip()


def _package_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("pv-extractor")
    except Exception:  # noqa: BLE001 — degrade to the source constant
        try:
            from pv_extractor import __version__

            return __version__
        except Exception:  # noqa: BLE001
            return "unknown"


@lru_cache(maxsize=1)
def build_info() -> dict:
    """Identity of the running build: package version + git commit/date/dirty.

    `commit` is the short hash; `dirty` is True when the working tree has
    uncommitted changes (so a hand-edited checkout is never mistaken for a clean
    tagged build). `label` is a one-line human string for compact display."""
    version = _package_version()
    commit = _git("rev-parse", "--short", "HEAD")
    commit_full = _git("rev-parse", "HEAD")
    committed_at = _git("log", "-1", "--format=%cI")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = None
    status = _git("status", "--porcelain")
    if status is not None:
        dirty = bool(status.strip())

    parts = [f"v{version}"]
    if commit:
        parts.append(f"{commit}{'-dirty' if dirty else ''}")
    label = " · ".join(parts)

    return {
        "version": version,
        "commit": commit,
        "commit_full": commit_full,
        "committed_at": committed_at,
        "branch": branch,
        "dirty": dirty,
        "python": sys.version.split()[0],
        "label": label,
    }
