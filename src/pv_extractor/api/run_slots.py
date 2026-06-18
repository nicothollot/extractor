"""Expand a single New-Run request that names MULTIPLE document types and/or
MULTIPLE periods into the flat RunSlot list run() consumes.

A run with exactly one doc type and one period stays on the legacy run-wide
path (slots=None, byte-for-byte unchanged). With more than one of either, we
build the cartesian product (in-scope pair × doc type × period) of RunSlots —
the same per-slot machinery the multi-firm search uses, but with no firm
grouping (firm=None). One workbook is written for the whole batch.

Kept in its own module so both jobs.py (launch) and selection_service.py
(the Confirm-documents preview) can share it without an import cycle.
"""

from __future__ import annotations

import sqlite3

from pv_extractor.config import Config
from pv_extractor.models import DocType, DocTypeSpec
from pv_extractor.run import RunSlot, _resolve_pairs
from pv_extractor.search.doc_type_spec import resolve_spec

_ENUM_DOC_TYPES = {dt.value for dt in DocType}


def doc_type_value(doc_type) -> str:
    return doc_type.value if isinstance(doc_type, DocType) else str(doc_type)


def effective_doc_types(doc_type, doc_types: list[str] | None) -> list[str]:
    """The doc-type slugs to run: the explicit list (de-duped, order-stable) when
    given, else the single doc_type."""
    if doc_types:
        seen: set[str] = set()
        out: list[str] = []
        for slug in doc_types:
            if slug and slug not in seen:
                seen.add(slug)
                out.append(slug)
        if out:
            return out
    return [doc_type_value(doc_type)]


def effective_periods(period: str, periods: list[str] | None) -> list[str]:
    """The periods to run: the explicit list (de-duped, order-stable) when given,
    else the single period."""
    if periods:
        seen: set[str] = set()
        out: list[str] = []
        for p in periods:
            p = (p or "").strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        if out:
            return out
    return [period]


def needs_expansion(doc_type, doc_types: list[str] | None, period: str, periods: list[str] | None) -> bool:
    """True when the run must fan out into slots: more than one doc type or
    period, OR a single doc type that is a profile slug / free-text (which the
    legacy DocType-enum path cannot represent)."""
    eff_types = effective_doc_types(doc_type, doc_types)
    if len(eff_types) > 1 or len(effective_periods(period, periods)) > 1:
        return True
    return any(slug not in _ENUM_DOC_TYPES for slug in eff_types)


def resolve_doc_type(
    conn: sqlite3.Connection, config: Config, slug: str
) -> tuple[DocType, DocTypeSpec | None]:
    """A DocType enum value -> (DocType(slug), None); a known profile slug ->
    (any_client_valuation_doc, stored spec); anything else (a free-text "Other"
    entry) -> a spec built live from the Smart Search rule engine so it still
    ranks matches."""
    if slug in _ENUM_DOC_TYPES:
        return DocType(slug), None
    spec = resolve_spec(conn, slug, config)
    if spec is not None:
        return DocType.any_client_valuation_doc, spec
    # Free-text doc type: rule-only intent resolution (never raises, no CLI).
    from pv_extractor.search.intent import resolve_intent

    free_spec, _ = resolve_intent(slug, config, conn=conn, use_cli=False)
    return DocType.any_client_valuation_doc, free_spec


def build_run_slots(
    conn: sqlite3.Connection,
    config: Config,
    *,
    scope: str,
    client: str | None,
    deal: str | None,
    exclude: set[tuple[str, str]],
    doc_type,
    doc_types: list[str] | None,
    period: str,
    periods: list[str] | None,
    restrict_to_client_sourced: bool = True,
) -> list[RunSlot]:
    """One RunSlot per (in-scope pair × doc type × period). firm=None so the run
    emits a flat (un-laned) progress stream and writes one workbook."""
    pairs = _resolve_pairs(conn, scope, client or None, deal or None, exclude)
    resolved = [resolve_doc_type(conn, config, slug) for slug in effective_doc_types(doc_type, doc_types)]
    plist = effective_periods(period, periods)
    slots: list[RunSlot] = []
    for pair_client, pair_deal in pairs:
        for slot_period in plist:
            for slot_doc_type, spec in resolved:
                slots.append(
                    RunSlot(
                        client=pair_client,
                        deal=pair_deal,
                        period=slot_period,
                        doc_type=slot_doc_type,
                        doc_type_spec=spec,
                        firm=None,
                        restrict_to_client_sourced=restrict_to_client_sourced,
                    )
                )
    return slots
