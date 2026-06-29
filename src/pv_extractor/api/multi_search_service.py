"""Multi-Search expansion + selection orchestration (Phase C.1/C.2).

The firm-level batch search fans a per-firm request out into the SAME per-slot
pipeline the single-firm path uses — nothing here re-implements locating,
extraction, deal discovery, or the writer. The two public helpers are pure and
synchronous:

  * expand_slots(conn, config, request) -> list[run.RunSlot]
        Resolve each firm's deal set (explicit, or every discovered deal),
        honoring per-firm LLM discovery assist and recorded add/remove
        corrections (which PERSIST + learn via deal_learning), then build one
        RunSlot per (deal x doc_type). A builtin DocType slug yields a builtin
        slot (doc_type_spec=None); a learned profile slug yields a Smart Search
        slot (doc_type=any_client_valuation_doc, doc_type_spec=<resolved>).
        The slot list is what jobs.start_multi_run feeds to run(slots=...).

  * build_multi_selection(conn, config, request) -> dict
        The firm-grouped 'is it finding the right docs?' preview: per firm, the
        discovered deal-folder summary plus one SlotSelection per slot (reusing
        selection_service.slot_selection, with the firm's enhanced_period_check
        and the resolved DocTypeSpec). No dry-run job is needed.

Firms beyond config.multi_search.max_firms are dropped with a logged warning
(nothing silent). Deal discovery runs ONLY through indexer.deals.refresh_deals
(its existing Claude Code assist path) — no new LLM path is introduced here.
"""

from __future__ import annotations

import logging
import sqlite3

from pv_extractor.api.schemas import MultiSearchFirm, MultiSearchSelectionRequest
from pv_extractor.api.selection_service import slot_selection
from pv_extractor.config import Config
from pv_extractor.indexer import db, deal_learning, deals as deals_module
from pv_extractor.indexer.periods import resolve_target_period
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DocType, DocTypeSpec
from pv_extractor.run import RunSlot
from pv_extractor.search.doc_type_spec import resolve_spec

logger = logging.getLogger(__name__)

_BUILTIN_DOC_TYPES = {dt.value for dt in DocType}


def _capped_firms(config: Config, request: MultiSearchSelectionRequest) -> list[MultiSearchFirm]:
    """Firms within config.multi_search.max_firms; the overflow is dropped with
    a logged warning (never silent)."""
    cap = config.multi_search.max_firms
    if len(request.firms) > cap:
        dropped = [f.client for f in request.firms[cap:]]
        log_event(
            logger,
            "multi-search firms capped",
            requested=len(request.firms),
            cap=cap,
            dropped=dropped,
        )
        return list(request.firms[:cap])
    return list(request.firms)


def _doc_types_for(config: Config, firm: MultiSearchFirm) -> list[str]:
    """The firm's doc-type slugs, defaulting to config when the firm gives none."""
    return list(firm.doc_types) if firm.doc_types else list(config.multi_search.default_doc_types)


def _apply_firm_discovery(
    conn: sqlite3.Connection, config: Config, firm: MultiSearchFirm, *, record_corrections: bool
) -> None:
    """Run the firm's deal discovery exactly as the analyst configured it.

    When ``record_corrections`` is True (the RUN path) any add_folder /
    remove_folder is persisted into deal_finder_feedback (Phase A learning)
    BEFORE re-discovery, but only if an identical correction is not already
    recorded — so launching the same multi-search twice does not pile up
    duplicate rows. When False (the SELECTION PREVIEW) no correction is written:
    the preview is read-only on the learning table and never mutates learned
    state. Either way refresh_deals re-discovers (with the Claude Code assist
    when llm_assist is set), replaying whatever is already persisted, and
    rewrites files.deal / deal_folders for the downstream locate()."""
    if record_corrections:
        existing = deal_learning.list_corrections(conn, firm.client)
        seen = {(c["action"], c.get("folder_path"), c["deal"]) for c in existing}
        for folder_path in firm.added_folders:
            if ("add_folder", folder_path, "") not in seen:
                deal_learning.record_correction(
                    conn, client=firm.client, deal="", action="add_folder", folder_path=folder_path
                )
        for deal_name in firm.removed_deals:
            if ("remove_folder", None, deal_name) not in seen:
                deal_learning.record_correction(
                    conn, client=firm.client, deal=deal_name, action="remove_folder"
                )
    # refresh_deals is a no-op when deal_discovery.enabled is False; in that case
    # the legacy deal-is-rel[1] view still backs db.deals_for_client below.
    deals_module.refresh_deals(
        conn,
        config,
        [firm.client],
        use_llm=True if firm.llm_assist else None,
        llm_model=firm.deal_search_model,
        apply_learning=True,
    )


def _firm_deals(conn: sqlite3.Connection, firm: MultiSearchFirm) -> list[str]:
    """Resolve the firm's deal set: explicit deals as given, else every
    discovered deal folder for the client (falling back to the index's
    deals_for_client when discovery produced none)."""
    if firm.deals:
        return list(firm.deals)
    folders = db.deal_folders_for_client(conn, firm.client)
    if folders:
        return [f.name for f in folders]
    return db.deals_for_client(conn, firm.client)


def _resolve_doc_type(
    conn: sqlite3.Connection, config: Config, slug: str
) -> tuple[DocType, DocTypeSpec | None]:
    """Map a doc-type slug to a (builtin DocType, doc_type_spec) pair for a
    RunSlot: a DocType ENUM value -> (DocType(slug), None); any other slug
    (prewritten-catalog or learned profile) -> (any_client_valuation_doc,
    resolved spec)."""
    if slug in _BUILTIN_DOC_TYPES:  # the four DocType enum values (local set)
        return DocType(slug), None
    spec = resolve_spec(conn, slug, config)
    if spec is None:
        log_event(
            logger,
            "multi-search unknown doc-type slug",
            slug=slug,
        )
        # Unknown slug -> fall back to the broadest builtin so the slot still
        # locates something rather than silently vanishing.
        return DocType.any_client_valuation_doc, None
    return DocType.any_client_valuation_doc, spec


def expand_slots(
    conn: sqlite3.Connection, config: Config, request: MultiSearchSelectionRequest
) -> list[RunSlot]:
    """Fan a multi-search request out into the flat RunSlot list run() consumes.

    One RunSlot per (firm, deal, doc_type). Each slot carries the firm's period
    and firm label (the run lanes events by it), the resolved builtin DocType
    and — for Smart Search profile slugs — the resolved DocTypeSpec."""
    slots: list[RunSlot] = []
    for firm in _capped_firms(config, request):
        _apply_firm_discovery(conn, config, firm, record_corrections=True)
        firm_deals = _firm_deals(conn, firm)
        doc_type_slugs = _doc_types_for(config, firm)
        resolved_types = [_resolve_doc_type(conn, config, slug) for slug in doc_type_slugs]
        for deal in firm_deals:
            for doc_type, spec in resolved_types:
                slots.append(
                    RunSlot(
                        client=firm.client,
                        deal=deal,
                        period=firm.period,
                        doc_type=doc_type,
                        doc_type_spec=spec,
                        firm=firm.client,
                        source_mode=firm.source_mode,
                    )
                )
    return slots


def _deal_folder_preview(config: Config, folder) -> dict:
    """Compact discovered-deal-folder summary for the selection preview (same
    shape routes_core exposes, kept local to avoid a routes->routes import)."""
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


def build_multi_selection(
    conn: sqlite3.Connection, config: Config, request: MultiSearchSelectionRequest
) -> dict:
    """Firm-grouped selection preview. Per firm: the discovered deal-folder
    summary plus one SlotSelection (as dict) per (deal x doc_type) slot,
    resolved through selection_service.slot_selection with the firm's
    enhanced_period_check and resolved DocTypeSpec."""
    firms_out: list[dict] = []
    for firm in _capped_firms(config, request):
        # Preview is read-only on the learning table: corrections are persisted
        # only when the analyst actually launches the run (expand_slots).
        _apply_firm_discovery(conn, config, firm, record_corrections=False)
        firm_deals = _firm_deals(conn, firm)
        doc_type_slugs = _doc_types_for(config, firm)
        resolved_types = [
            (slug, *_resolve_doc_type(conn, config, slug)) for slug in doc_type_slugs
        ]
        # One target per firm (firm-wide period under the client's style).
        try:
            target = resolve_target_period(firm.period, config.client_period_style(firm.client))
        except ValueError:
            target = None

        slots_out: list[dict] = []
        for deal in firm_deals:
            for slug, doc_type, spec in resolved_types:
                selection = slot_selection(
                    conn,
                    config,
                    firm.client,
                    deal,
                    firm.period,
                    doc_type,
                    target=target,
                    enhanced_period_check=firm.enhanced_period_check,
                    doc_type_spec=spec,
                    source_mode=firm.source_mode,
                )
                payload = selection.model_dump()
                payload["doc_type_slug"] = slug
                slots_out.append(payload)

        folders = db.deal_folders_for_client(conn, firm.client)
        firms_out.append(
            {
                "client": firm.client,
                "period": firm.period,
                "doc_types": doc_type_slugs,
                "enhanced_period_check": firm.enhanced_period_check,
                "deal_folders_preview": [_deal_folder_preview(config, f) for f in folders],
                "slots": slots_out,
                "found": sum(1 for s in slots_out if s.get("status") == "FOUND"),
            }
        )
    return {"firms": firms_out}
