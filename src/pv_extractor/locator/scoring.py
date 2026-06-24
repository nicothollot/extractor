"""Deterministic candidate scoring with fully populated breakdowns (D5b-d).

Every component weight comes from config.locator.weights; nothing is
hardcoded. score_candidate never raises on odd records — missing signals
simply score 0 with method 'none' — and the ScoreBreakdown it returns is
always complete so ranking decisions are transparent and loggable.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from pydantic import BaseModel
from rapidfuzz import fuzz

from pv_extractor.config import LocatorConfig
from pv_extractor.indexer.periods import filename_contains_period, period_label
from pv_extractor.models import (
    DocType,
    DocTypeSpec,
    FileRecord,
    PeriodStyle,
    PeriodStyleKind,
    ScoreBreakdown,
    SourceClass,
)
from pv_extractor.normalize import has_do_not_use_marker, normalize_text


class ScoreContext(BaseModel):
    """Everything score_candidate needs beyond the record itself."""

    resolved_client: str
    resolved_deal: str
    deal_expansions: list[str]
    client_method: str
    client_ratio: float
    deal_method: str
    deal_ratio: float
    target_as_of: date
    # The client's reporting cadence, used (with cfg.tolerate_same_period) to
    # treat a same-quarter/same-month folder date as a period match. Defaults
    # to calendar-quarterly so existing callers/tests need no change.
    period_style: PeriodStyle = PeriodStyle(kind=PeriodStyleKind.quarterly_calendar)
    doc_type: DocType
    cfg: LocatorConfig
    # When a DocTypeSpec is supplied it REPLACES the static
    # doc_type_keywords(ctx.doc_type, ctx.cfg) lookup for the doc-type /
    # negative components (see _doctype_spec_component). None => behavior is
    # byte-for-byte identical to the legacy builtin-enum path.
    doc_type_spec: DocTypeSpec | None = None
    # When False, the report/analysis (HL work product) source-class penalty is
    # OFF — those files still rank on their other components but are not pushed
    # down for being HL-sourced. Default True = CLAUDE.md rule 2 behavior.
    restrict_to_client_sourced: bool = True


def doc_type_keywords(doc_type: DocType, cfg: LocatorConfig) -> list[str]:
    """Keyword list for a doc type; any_client_valuation_doc = union of all."""
    if doc_type is DocType.any_client_valuation_doc:
        return [kw for kws in cfg.doc_type_keywords.values() for kw in kws]
    return cfg.doc_type_keywords.get(doc_type.value, [])


def _keyword_hits(normalized_file_name: str, keywords: list[str]) -> list[str]:
    """Token-bounded keyword matches against an export-normalized filename."""
    padded = f" {normalized_file_name} "
    return [kw for kw in keywords if f" {normalize_text(kw)} " in padded]


def _folder_keyword_hits(normalized_folder_path: str, keywords: list[str]) -> list[str]:
    """Token-bounded keyword matches against the export-normalized folder path
    (same matching convention as _keyword_hits, applied to the folder rather
    than the file name)."""
    padded = f" {normalized_folder_path} "
    return [kw for kw in keywords if f" {normalize_text(kw)} " in padded]


def _regex_hits(text: str, patterns: list[str]) -> list[str]:
    """re.search each raw pattern against the export-normalized file name.
    A pattern that fails to compile is skipped (never raises); the pattern
    string itself is recorded as the matched term so the breakdown stays
    inspectable. Matching is case-insensitive to mirror the normalized text."""
    hits: list[str] = []
    for pat in patterns:
        try:
            if re.search(pat, text, re.IGNORECASE):
                hits.append(pat)
        except re.error:
            continue
    return hits


def _doctype_spec_component(
    rec: FileRecord, spec: DocTypeSpec, cfg: LocatorConfig
) -> tuple[float, list[str], float, list[str]]:
    """Score the doc-type / negative signal from a DocTypeSpec instead of the
    static config keyword lists. Returns
    (doctype_score, matched_keywords, negative_score, matched_negative).

    Positive (doctype) signal — folded into matched_keywords:
      * filename_include token/phrase hits against rec.normalized_file_name
        (reusing _keyword_hits, same normalization convention);
      * filename_regex raw-pattern hits, re.search (case-insensitive) against
        the normalized file name — the pattern string is the recorded term;
      * folder_include token hits against rec.normalized_folder_path, recorded
        with a 'folder:' prefix so a folder-only match still populates
        matched_keywords (and therefore still satisfies the _is_eligible
        doc-type gate for an arbitrary profile).
    doctype_score fires the FULL weights.doctype_keyword when ANY positive
    term hits (matches the legacy boolean-presence model — keyword COUNT never
    scaled the score), then weight_overrides['doctype_keyword'], when present,
    REPLACES weights.doctype_keyword as the magnitude.

    Negative signal — folded into matched_negative_keywords:
      * filename_exclude token hits (rec.normalized_file_name);
      * folder_exclude token hits (rec.normalized_folder_path, 'folder:'
        prefixed).
    negative_score fires weights.negative_keyword when ANY negative term hits;
    weight_overrides['negative_keyword'], when present, REPLACES it.

    weight_overrides therefore override the per-component WEIGHT magnitudes
    (not added on top); any unrelated keys in weight_overrides are ignored
    here — they are reserved for future components and stay inspectable on the
    spec itself.
    """
    weights = cfg.weights

    matched = _keyword_hits(rec.normalized_file_name, spec.filename_include)
    matched += _regex_hits(rec.normalized_file_name, spec.filename_regex)
    matched += [f"folder:{kw}" for kw in _folder_keyword_hits(rec.normalized_folder_path, spec.folder_include)]

    matched_negative = _keyword_hits(rec.normalized_file_name, spec.filename_exclude)
    matched_negative += [
        f"folder:{kw}" for kw in _folder_keyword_hits(rec.normalized_folder_path, spec.folder_exclude)
    ]

    doctype_weight = spec.weight_overrides.get("doctype_keyword", weights.doctype_keyword)
    negative_weight = spec.weight_overrides.get("negative_keyword", weights.negative_keyword)

    doctype_score = doctype_weight if matched else 0.0
    negative_score = negative_weight if matched_negative else 0.0
    return doctype_score, matched, negative_score, matched_negative


def _client_deal_component(rec: FileRecord, ctx: ScoreContext) -> tuple[float, str]:
    weights = ctx.cfg.weights
    if rec.deal is None:
        return 0.0, "none"
    if rec.deal == ctx.resolved_deal:
        return weights.client_deal_exact, "exact"
    if normalize_text(rec.deal) == normalize_text(ctx.resolved_deal):
        return weights.client_deal_normalized, "normalized"
    threshold = ctx.cfg.fuzzy_match_threshold
    deal_norm = normalize_text(rec.deal)
    ratio = max(
        (fuzz.token_set_ratio(deal_norm, normalize_text(exp)) for exp in ctx.deal_expansions),
        default=0.0,
    )
    if ratio >= threshold:
        scaled = weights.client_deal_fuzzy_max * (ratio - threshold) / (100 - threshold)
        return scaled, "fuzzy"
    return 0.0, "none"


def _period_component(rec: FileRecord, ctx: ScoreContext) -> tuple[float, str]:
    weights = ctx.cfg.weights
    if rec.as_of_date is not None:
        if rec.as_of_date == ctx.target_as_of:
            return weights.period_folder_exact, "folder"
        # Same reporting period (e.g. a 2.28 month-end folder for a Q1 target):
        # a real period match, scored just below an exact hit so the exact-date
        # document still wins when one exists.
        if ctx.cfg.tolerate_same_period and period_label(
            rec.as_of_date, ctx.period_style
        ) == period_label(ctx.target_as_of, ctx.period_style):
            return weights.period_folder_same_period, "folder_same_period"
        return weights.period_folder_mismatch, "folder_mismatch"
    if filename_contains_period(rec.normalized_file_name, ctx.target_as_of):
        return weights.period_in_filename, "filename"
    # Weak tie-breaker only: memos are written AFTER quarter end, so the
    # window opens at the as-of date and extends forward.
    if rec.modified_time is not None:
        window_end = ctx.target_as_of + timedelta(days=ctx.cfg.mtime_window_days)
        if ctx.target_as_of <= rec.modified_time.date() <= window_end:
            return weights.period_mtime_window, "mtime"
    return 0.0, "none"


def score_candidate(rec: FileRecord, ctx: ScoreContext) -> ScoreBreakdown:
    """Score one candidate; the breakdown is always fully populated."""
    weights = ctx.cfg.weights

    client_deal_score, client_deal_method = _client_deal_component(rec, ctx)
    period_score, period_method = _period_component(rec, ctx)

    if ctx.doc_type_spec is not None:
        # Spec-driven path REPLACES the static doc_type_keywords lookup for the
        # doc-type + negative components (folder context folded in via 'folder:'
        # prefixed terms). All other components are untouched.
        doctype_score, matched_keywords, negative_score, matched_negative = _doctype_spec_component(
            rec, ctx.doc_type_spec, ctx.cfg
        )
    else:
        # Legacy builtin-enum path — byte-for-byte unchanged.
        matched_keywords = _keyword_hits(rec.normalized_file_name, doc_type_keywords(ctx.doc_type, ctx.cfg))
        doctype_score = weights.doctype_keyword if matched_keywords else 0.0
        matched_negative = _keyword_hits(rec.normalized_file_name, ctx.cfg.negative_keywords)
        negative_score = weights.negative_keyword if matched_negative else 0.0

    if rec.source_class is SourceClass.client:
        source_class_score = weights.source_class_client_bonus
    elif rec.source_class in (SourceClass.report, SourceClass.analysis) and ctx.restrict_to_client_sourced:
        # HL work product is NOT a valid extraction source (CLAUDE.md rule 2),
        # unless the run opted out of client-source restriction.
        source_class_score = weights.source_class_report_penalty
    else:
        source_class_score = 0.0

    extension_score = weights.extension_prior.get(rec.extension, 0.0)
    version_rank = rec.version_signal.rank if rec.version_signal else 0
    version_score = version_rank * weights.version_rank_step
    do_not_use = weights.do_not_use_penalty if has_do_not_use_marker(rec.file_name) else 0.0
    zero_byte = weights.zero_byte_penalty if rec.is_zero_byte else 0.0

    raw_total = (
        client_deal_score
        + period_score
        + doctype_score
        + negative_score
        + source_class_score
        + extension_score
        + version_score
        + do_not_use
        + zero_byte
    )
    archive_multiplier = (
        weights.archive_score_multiplier if (rec.is_archive and raw_total > 0) else 1.0
    )
    return ScoreBreakdown(
        client_deal_score=client_deal_score,
        client_deal_method=client_deal_method,
        period_score=period_score,
        period_method=period_method,
        doctype_score=doctype_score,
        matched_keywords=matched_keywords,
        negative_score=negative_score,
        matched_negative_keywords=matched_negative,
        source_class_score=source_class_score,
        extension_score=extension_score,
        version_score=version_score,
        do_not_use_penalty=do_not_use,
        zero_byte_penalty=zero_byte,
        raw_total=raw_total,
        archive_multiplier=archive_multiplier,
        final_score=raw_total * archive_multiplier,
    )
