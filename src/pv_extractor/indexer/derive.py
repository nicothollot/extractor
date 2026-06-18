"""Re-derive every export-mirror and derived column from file_path alone.

THE RULE (CLAUDE.md rule 4) lives here: derived columns in the PV index
export are never trusted — 349 of 999 rows in the reference export carry a
corrupt '#NAME?' parent_folder (folder names starting with '+'). Only
file_path, size_bytes and modified_time come from outside; everything else
is recomputed with normalize.py helpers and indexer.periods.
"""

from __future__ import annotations

from datetime import date, datetime

from pv_extractor.config import Config
from pv_extractor.indexer.periods import filename_contains_period, parse_date_folder
from pv_extractor.models import FileRecord, SourceClass
from pv_extractor.normalize import (
    has_do_not_use_marker,
    normalize_path,
    normalize_text,
    parse_version_signal,
    relative_segments,
    split_path_segments,
    strip_extended_prefix,
)

# The two export period-signal columns are pinned to these as-of dates.
_Q4_2025 = date(2025, 12, 31)
_Q1_2026 = date(2026, 3, 31)

_ARCHIVE_TOKENS = frozenset({"archive", "archived", "old", "prior", "superseded"})

# Walking file -> client, the first folder whose normalized tokens hit this
# mapping classifies the file; earlier entries win within a single folder.
_SOURCE_CLASS_TOKENS: tuple[tuple[SourceClass, frozenset[str]], ...] = (
    (SourceClass.client, frozenset({"client"})),
    (SourceClass.report, frozenset({"report", "reports"})),
    (SourceClass.analysis, frozenset({"analysis"})),
    (SourceClass.research, frozenset({"research"})),
    (SourceClass.support, frozenset({"support"})),
    (SourceClass.archive, _ARCHIVE_TOKENS),
    (SourceClass.admin, frozenset({"admin"})),
)


def _classify_folder(tokens: frozenset[str] | set[str]) -> SourceClass | None:
    for source_class, wanted in _SOURCE_CLASS_TOKENS:
        if tokens & wanted:
            return source_class
    return None


def _contains_keyword(normalized_file_name: str, keywords: list[str]) -> bool:
    padded = f" {normalized_file_name} "
    return any(f" {normalize_text(keyword)} " in padded for keyword in keywords)


def derive_record(
    file_path: str,
    *,
    size_bytes: int | None,
    modified_time: datetime | None,
    config: Config,
    is_cloud_placeholder: bool = False,
) -> FileRecord:
    """Build a FileRecord for a file under config.pv_root.

    Raises ValueError for paths outside pv_root. Stored paths keep their
    original separators but lose any '\\\\?\\' long-path prefix.
    """
    path = strip_extended_prefix(str(file_path)).rstrip("\\/")
    rel = relative_segments(path, config.pv_root)
    if rel is None:
        raise ValueError(f"file is not under pv_root ({config.pv_root}): {file_path}")

    file_name = rel[-1]
    folder_path = path[: max(path.rfind("\\"), path.rfind("/"))]
    parent_folder = split_path_segments(folder_path)[-1]
    dot = file_name.rfind(".")
    extension = file_name[dot:].lower() if dot > 0 else ""
    depth = len(rel) - 1  # folder segments below pv_root, file excluded

    client = rel[0] if depth >= 1 else None
    deal = rel[1] if depth >= 2 else None
    folders_below_client = rel[1:-1]

    date_folder: str | None = None
    as_of_date: date | None = None
    source_class = SourceClass.other
    classified = False
    for folder in reversed(folders_below_client):  # nearest the file first
        if as_of_date is None:
            parsed = parse_date_folder(folder)
            if parsed is not None:
                date_folder, as_of_date = folder, parsed
        if not classified:
            found = _classify_folder(set(normalize_text(folder).split()))
            if found is not None:
                source_class, classified = found, True

    is_archive = has_do_not_use_marker(file_name) or any(
        set(normalize_text(folder).split()) & _ARCHIVE_TOKENS for folder in folders_below_client
    )

    normalized_file_name = normalize_text(file_name)
    normalized_full_path = normalize_path(path)
    doc_keywords = [kw for kws in config.locator.doc_type_keywords.values() for kw in kws]

    return FileRecord(
        file_name=file_name,
        file_path=path,
        folder_path=folder_path,
        parent_folder=parent_folder,
        extension=extension,
        size_bytes=size_bytes,
        modified_time=modified_time,
        depth_from_pv_root=depth,
        normalized_file_name=normalized_file_name,
        normalized_folder_path=normalize_path(folder_path),
        normalized_full_path=normalized_full_path,
        contains_memo_keyword=_contains_keyword(normalized_file_name, doc_keywords),
        contains_q4_2025_signal=filename_contains_period(normalized_full_path, _Q4_2025),
        contains_q1_2026_signal=filename_contains_period(normalized_full_path, _Q1_2026),
        archive_or_old_flag=is_archive,
        client=client,
        deal=deal,
        date_folder=date_folder,
        as_of_date=as_of_date,
        source_class=source_class,
        is_archive=is_archive,
        version_signal=parse_version_signal(file_name),
        is_cloud_placeholder=is_cloud_placeholder,
        is_zero_byte=size_bytes == 0,
    )
