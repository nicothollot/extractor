"""Text/path normalization shared by the indexer and the locator.

The conventions match the existing PV index export exactly (verified against
reference/strip_from_index.xlsx): lowercase, every non-alphanumeric character
becomes a space, runs of spaces collapse, and a filename keeps its extension
as a trailing token without the dot, e.g.

    "HL - Angeles NDA 28Sep22 v1 to HL (HL 9-30-22) (002).doc"
        -> "hl angeles nda 28sep22 v1 to hl hl 9 30 22 002 doc"
"""

from __future__ import annotations

import re

from pv_extractor.models import VersionSignal

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_EXTENDED_PREFIXES = ("\\\\?\\UNC\\", "\\\\?\\")

# Version decorations on a filename stem (D5e). Order matters: final-style
# beats vN beats " (00N)" copies.
_FINAL_RE = re.compile(r"(?:^|[ _\-.(])(vf|final)(?:[ _\-.)]|$)", re.IGNORECASE)
_VNUM_RE = re.compile(r"(?:^|[ _\-.(])v(\d{1,3})(?:[ _\-.)]|$)", re.IGNORECASE)
_COPY_RE = re.compile(r"\((\d{3})\)\s*$")
_DO_NOT_USE_RE = re.compile(r"do\s*not\s*use|superseded|\bold\b", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """Lowercase, non-alphanumerics to spaces, collapse runs, strip ends."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def strip_extended_prefix(path: str) -> str:
    r"""Remove Windows long-path prefixes: '\\?\UNC\srv\sh' -> '\\srv\sh',
    '\\?\C:\x' -> 'C:\x'."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path


def to_extended_path(path: str) -> str:
    r"""Add the '\\?\' long-path prefix for Windows paths (>260 chars).
    UNC paths become '\\?\UNC\server\share\...'. No-op for already-prefixed
    or relative paths."""
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    if re.match(r"^[A-Za-z]:\\", path):
        return "\\\\?\\" + path
    return path


def split_path_segments(path: str) -> list[str]:
    """Split a Windows or POSIX path into non-empty segments."""
    cleaned = strip_extended_prefix(path).replace("/", "\\")
    return [seg for seg in cleaned.split("\\") if seg]


def normalize_path(path: str) -> str:
    """Normalize a full path to the export convention: every segment
    normalized and joined with single spaces (UNC host/share included)."""
    return normalize_text(" ".join(split_path_segments(path)))


def relative_segments(path: str, root: str) -> list[str] | None:
    """Segments of `path` below `root` (case-insensitive segment compare),
    or None if `path` is not under `root`."""
    p = [s.lower() for s in split_path_segments(path)]
    r = [s.lower() for s in split_path_segments(root)]
    if len(p) <= len(r) or p[: len(r)] != r:
        return None
    return split_path_segments(path)[len(r):]


def file_stem(file_name: str) -> str:
    """Filename without its final extension."""
    dot = file_name.rfind(".")
    return file_name if dot <= 0 else file_name[:dot]


def parse_version_signal(file_name: str) -> VersionSignal:
    """Extract the version decoration from a filename stem (D5e ranking:
    vf/final=3 > vN=2 > ' (00N)' copy=1 > undecorated=0)."""
    stem = file_stem(file_name)
    m = _FINAL_RE.search(stem)
    if m:
        return VersionSignal(rank=3, raw=m.group(1))
    m = _VNUM_RE.search(stem)
    if m:
        return VersionSignal(rank=2, version_number=int(m.group(1)), raw=f"v{m.group(1)}")
    m = _COPY_RE.search(stem)
    if m:
        return VersionSignal(rank=1, copy_number=int(m.group(1)), raw=f"({m.group(1)})")
    return VersionSignal(rank=0)


def family_stem(file_name: str) -> str:
    """Normalized stem with version decorations removed, used to group
    near-duplicate version families before fuzzy comparison."""
    stem = file_stem(file_name)
    stem = _COPY_RE.sub(" ", stem)
    stem = _FINAL_RE.sub(" ", stem)
    stem = _VNUM_RE.sub(" ", stem)
    return normalize_text(stem)


def has_do_not_use_marker(file_name: str) -> bool:
    """True for filenames carrying 'DO NOT USE' / 'superseded' / 'old'."""
    return bool(_DO_NOT_USE_RE.search(file_name))
