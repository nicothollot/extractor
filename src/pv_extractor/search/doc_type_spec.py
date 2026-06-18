"""CRUD over the `doc_type_profiles` table + seeded builtins (Phase B).

A `DocTypeSpec` is the learnable description of "what document to find". The
three builtin doc types (valuation_memo / ic_memo / portfolio_review) are
MIGRATED here, idempotently, from `config.locator.doc_type_keywords` with
``builtin=1`` so existing behavior is preserved and now editable from the GUI.
``any_client_valuation_doc`` is the runtime UNION of those three builtins'
filename anchors; it is seeded as a builtin profile too (so it lists/edits like
the rest) and `resolve_spec` rebuilds its union on the fly when the underlying
keyword lists change.

Conn-first, mirroring locator/overrides.py: every function takes the SQLite
connection first; the spec is serialized to JSON for the thin db.py accessors.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DocType, DocTypeSpec

logger = logging.getLogger(__name__)

# Prewritten doc types migrated from config.locator.doc_type_keywords into
# doc_type_profiles (builtin=1). The first three are also DocType enum values
# (so the legacy single-doc-type path still resolves them); the rest exist only
# as profiles (slugs) addressable by Smart Search / the doc-type picker.
# any_client_valuation_doc is the union, handled specially.
_BUILTIN_DOC_TYPES: tuple[str, ...] = (
    DocType.valuation_memo.value,
    DocType.ic_memo.value,
    DocType.portfolio_review.value,
    "quarterly_report",
    "annual_report",
    "houlihan_valuation",
    "investor_presentation",
    "fund_report",
    "capital_account_statement",
    "financial_statements",
    "board_materials",
)

# The 'any client valuation doc' union stays the original three client docs.
_ANY_UNION_DOC_TYPES: tuple[str, ...] = (
    DocType.valuation_memo.value,
    DocType.ic_memo.value,
    DocType.portfolio_review.value,
)

_BUILTIN_LABELS: dict[str, str] = {
    DocType.valuation_memo.value: "Valuation Memo",
    DocType.ic_memo.value: "IC Memo",
    DocType.portfolio_review.value: "Portfolio Review",
    DocType.any_client_valuation_doc.value: "Any Client Valuation Document",
    "quarterly_report": "Quarterly Report",
    "annual_report": "Annual Report",
    "houlihan_valuation": "Houlihan Valuation",
    "investor_presentation": "Investor Presentation",
    "fund_report": "Fund Report",
    "capital_account_statement": "Capital Account Statement",
    "financial_statements": "Financial Statements",
    "board_materials": "Board Materials",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _builtin_spec(slug: str, config: Config) -> DocTypeSpec:
    """The DocTypeSpec a builtin doc type should carry, derived live from
    config.locator.doc_type_keywords (single source of truth for the keywords)."""
    keywords = config.locator.doc_type_keywords
    if slug == DocType.any_client_valuation_doc.value:
        # Union of the three CLIENT-VALUATION builtin lists, de-duped,
        # order-stable (NOT the wider prewritten catalog — 'any' stays focused).
        seen: set[str] = set()
        union: list[str] = []
        for dt in _ANY_UNION_DOC_TYPES:
            for kw in keywords.get(dt, []):
                if kw not in seen:
                    seen.add(kw)
                    union.append(kw)
        includes = union
    else:
        includes = list(keywords.get(slug, []))
    return DocTypeSpec(
        slug=slug,
        label=_BUILTIN_LABELS.get(slug, slug.replace("_", " ").title()),
        filename_include=includes,
        # The locator matches negative_keywords against the FILE NAME, so they
        # belong in filename_exclude (mirrors locator/scoring._keyword_hits).
        filename_exclude=list(config.locator.negative_keywords),
        period_required=True,
    )


def save_profile(
    conn: sqlite3.Connection,
    spec: DocTypeSpec,
    *,
    query_seed: str | None = None,
    builtin: bool = False,
) -> None:
    """Upsert a profile by slug; updated_at is bumped, created_at preserved."""
    now = _now_iso()
    db.upsert_doc_type_profile(
        conn,
        slug=spec.slug,
        label=spec.label,
        spec_json=spec.model_dump_json(),
        query_seed=query_seed,
        builtin=builtin,
        created_at=now,  # ignored on conflict (created_at is preserved there)
        updated_at=now,
    )
    log_event(logger, "doc type profile saved", slug=spec.slug, builtin=builtin)


def get_profile(conn: sqlite3.Connection, slug: str) -> DocTypeSpec | None:
    """The stored DocTypeSpec for a slug (None when absent / spec corrupt)."""
    row = db.get_doc_type_profile(conn, slug)
    if row is None:
        return None
    try:
        return DocTypeSpec.model_validate_json(row["spec"])
    except Exception as exc:  # noqa: BLE001 — a corrupt row is a flag, not a crash
        log_event(logger, "doc type profile spec unreadable", slug=slug, error=str(exc))
        return None


def list_profiles(conn: sqlite3.Connection) -> list[DocTypeSpec]:
    """All stored profiles (builtins + learned). Corrupt rows are skipped
    (logged) rather than aborting the whole listing."""
    specs: list[DocTypeSpec] = []
    for row in db.list_doc_type_profiles(conn):
        try:
            specs.append(DocTypeSpec.model_validate_json(row["spec"]))
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger, "doc type profile spec unreadable",
                slug=row.get("slug"), error=str(exc),
            )
    return specs


def delete_profile(conn: sqlite3.Connection, slug: str) -> bool:
    """Delete a learned profile. REFUSES to delete a builtin (returns False)."""
    row = db.get_doc_type_profile(conn, slug)
    if row is None:
        return False
    if row["builtin"]:
        log_event(logger, "refused delete of builtin doc type profile", slug=slug)
        return False
    deleted = db.delete_doc_type_profile(conn, slug)
    if deleted:
        log_event(logger, "doc type profile deleted", slug=slug)
    return deleted


def seed_builtins(conn: sqlite3.Connection, config: Config) -> None:
    """Idempotently seed the builtin doc types into doc_type_profiles with
    builtin=1, migrated from config.locator.doc_type_keywords. Re-running
    refreshes the spec so a keyword-list edit in config flows through (a
    learned weight_overrides nudge on a builtin survives because it is stored
    on the spec and re-derived empty here only for a fresh builtin — see
    note). Safe to call lazily before the first profile read."""
    for slug in (*_BUILTIN_DOC_TYPES, DocType.any_client_valuation_doc.value):
        existing = get_profile(conn, slug)
        spec = _builtin_spec(slug, config)
        if existing is not None:
            # Preserve any learned per-component nudges accumulated on the
            # builtin; only the keyword/anchor lists are re-synced from config.
            spec.weight_overrides = dict(existing.weight_overrides)
        save_profile(
            conn, spec, query_seed=spec.label, builtin=True,
        )


def resolve_spec(
    conn: sqlite3.Connection, slug_or_doctype: str, config: Config
) -> DocTypeSpec | None:
    """Resolve a profile slug OR a builtin DocType enum value to a DocTypeSpec.

    Builtins are seeded lazily on first resolution so a fresh index resolves
    the builtin doc types without a prior explicit seed call. For builtins the
    spec's keyword anchors are always re-derived live from
    config.locator.doc_type_keywords (so the locator and Smart Search never
    drift), while any learned weight_overrides on the stored row are preserved.
    """
    is_builtin = slug_or_doctype in {dt.value for dt in DocType}
    if is_builtin:
        seed_builtins(conn, config)
        spec = _builtin_spec(slug_or_doctype, config)
        stored = get_profile(conn, slug_or_doctype)
        if stored is not None:
            spec.weight_overrides = dict(stored.weight_overrides)
        return spec
    return get_profile(conn, slug_or_doctype)
