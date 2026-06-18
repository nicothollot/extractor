r"""Read-only enforcement for the PV share.

THE RULE: this tool never writes inside pv_root. Every file open in this
codebase goes through one of the two helpers here — `open_read` ('rb' only)
for anything that may live on the share, `guarded_open_write` for outputs.
A unit test greps src/ to ensure no other module calls open() in a write
mode, and `guarded_open_write` refuses any target under pv_root (or under
the well-known production share regardless of configuration).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import IO

from pv_extractor.normalize import split_path_segments, strip_extended_prefix

# Defense in depth: the production share is refused even if config points
# pv_root elsewhere (e.g. at a test fixture).
_HARDCODED_FORBIDDEN = re.compile(r"^\\\\hlhz\\dfs\\nyfva\\pv(\\|$)", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    """Raised on any attempt to open a write handle under pv_root."""


def is_under_pv_root(path: str, pv_root: str) -> bool:
    r"""Case-insensitive containment check, robust to /, \ and \\?\ forms."""
    p = [s.lower() for s in split_path_segments(str(path))]
    r = [s.lower() for s in split_path_segments(str(pv_root))]
    return bool(r) and len(p) >= len(r) and p[: len(r)] == r


def assert_write_allowed(path: str | Path, pv_root: str) -> None:
    """Raise ReadOnlyViolation if `path` is inside pv_root or the production
    PV share."""
    raw = strip_extended_prefix(str(path)).replace("/", "\\")
    if _HARDCODED_FORBIDDEN.match(raw):
        raise ReadOnlyViolation(f"refusing to write inside the PV share: {path}")
    if is_under_pv_root(str(path), pv_root):
        raise ReadOnlyViolation(f"refusing to write under configured pv_root ({pv_root}): {path}")


def open_read(path: str | Path) -> IO[bytes]:
    """The only sanctioned way to open files that may live on the PV share.
    Always binary read-only."""
    return open(path, "rb")  # noqa: io-guard-exempt (read-only by construction)


def guarded_open_write(
    path: str | Path,
    pv_root: str,
    mode: str = "w",
    encoding: str | None = "utf-8",
) -> IO:
    """Open an output file for writing after asserting it is outside pv_root.
    Text modes default to UTF-8 (cp1252 console safety)."""
    if not any(flag in mode for flag in ("w", "a", "x", "+")):
        raise ValueError(f"guarded_open_write is for write modes, got {mode!r}")
    assert_write_allowed(path, pv_root)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if "b" in mode:
        return open(path, mode)  # noqa: io-guard-exempt (guarded above)
    return open(path, mode, encoding=encoding, newline="\n")  # noqa: io-guard-exempt (guarded above)
