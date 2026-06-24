"""Document selection for the New Run wizard's "Confirm documents" step.

After preflight (the dry-run job), the analyst curates the exact files the
locator auto-selected before launch. This service builds that table by
calling the SAME pipeline functions the run uses — locate() then the Phase-2
peek-verifier verify_and_rerank() — for every in-scope (client, deal) slot,
so what the table shows is exactly what the run would pick. Nothing is
re-implemented here and nothing under pv_root is ever opened for writing
(all reads go through the readers' io_guard.open_read).

A learned override already in effect for a slot is surfaced (the same table
locate() consults); swaps/adds are recorded through locator/overrides.py and
picked up by locate() at launch, and removals ride on RunRequest.exclude.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from pydantic import BaseModel, Field

from pv_extractor.api.jobs import JobManager
from pv_extractor.api.preflight_service import _pdf_page_count
from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import period_label, resolve_target_period
from pv_extractor.locator.locate import locate
from pv_extractor.locator.overrides import lookup_extra_docs, lookup_override
from pv_extractor.locator.verify import verify_and_rerank
from pv_extractor.models import (
    DocType,
    DocTypeSpec,
    LocateQuery,
    LocateResult,
    ResolutionStatus,
    VerifyResult,
)
from pv_extractor.run import _resolve_pairs


class CandidateInfo(BaseModel):
    file_name: str
    file_path: str
    last_modified: str | None = None
    score: float = 0.0
    family_rank: int = 0
    verify_status: str = ""
    doc_class: str = ""
    verify_reason: str = ""
    is_selected: bool = False


class SlotSelection(BaseModel):
    client: str
    deal: str
    # "client|deal|period|doc_type" — stable id for removals/swaps. The period
    # + doc_type segments make every slot of a multi-period / multi-doc-type run
    # distinct so the Confirm-documents table can show ALL of them (a 2-period
    # run is two slots per deal, one per tab), not just the first.
    slot_key: str
    period: str = ""  # the slot's requested period label (drives the period tabs)
    doc_type: str = ""  # the slot's requested doc-type (slug or enum value)
    status: str  # ResolutionStatus value or ERROR
    as_of_date: str | None = None
    predicted_period: str = ""
    override_in_effect: bool = False
    detail: str = ""
    # the auto-selected (post-verify) document, when one was resolved
    file_name: str | None = None
    file_path: str | None = None
    last_modified: str | None = None
    page_count: int | None = None
    doc_class: str = ""
    verify_status: str = ""
    confidence: float | None = None  # peek-verify confidence (0..1)
    score: float | None = None  # locator final_score
    # Enhanced-period-check surfacing (Phase C): when the best document's
    # in-file as-of date disagrees with the requested folder/target period,
    # the slot is flagged MISFILED and carries the document's TRUE period.
    misfiled: bool = False
    detected_period: str | None = None  # the in-file period label, when it disagrees
    detected_as_of: str | None = None  # the in-file as-of date (ISO), when it disagrees
    # Multi-doc merge: extra source documents recorded for this slot (their
    # fields are merged into the same row by best confidence at run time).
    extra_docs: list[str] = Field(default_factory=list)
    candidates: list[CandidateInfo] = Field(default_factory=list)


class SelectionResponse(BaseModel):
    job_id: str
    scope: str
    period: str
    doc_type: str
    found: int = 0
    slots: list[SlotSelection] = Field(default_factory=list)
    # Multi-slot fan-out (doc types × periods). When >1, the table previews the
    # first type/period per deal; the launch runs the full slot_count.
    doc_types: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    slot_count: int = 0


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    # FileRecord.modified_time is a datetime
    return getattr(value, "isoformat", lambda: str(value))()


def _candidate_info(cand, verdict: VerifyResult | None, selected_path: str | None) -> CandidateInfo:
    return CandidateInfo(
        file_name=cand.record.file_name,
        file_path=cand.record.file_path,
        last_modified=_iso(cand.record.modified_time),
        score=round(cand.breakdown.final_score, 2),
        family_rank=cand.family_rank,
        verify_status=verdict.status.value if verdict else "",
        doc_class=verdict.doc_class.value if verdict else "",
        verify_reason=verdict.reason if verdict else "",
        is_selected=cand.record.file_path == selected_path,
    )


def _locate_slot(
    conn,
    config: Config,
    client: str,
    deal: str,
    period: str,
    doc_type: DocType,
    *,
    target: date | None,
    doc_type_spec: DocTypeSpec | None,
    restrict_to_client_sourced: bool,
    doc_type_label: str | None = None,
) -> tuple[SlotSelection, LocateResult | None]:
    """Conn-bound phase: the predicted period, the learned-override check and
    locate() (FTS + scoring — cheap, must hold the sqlite connection). Returns
    the partially-filled slot and the LocateResult to peek-verify next (None on
    an unresolvable period — ERROR is final, nothing to verify).

    `doc_type_label` is the requested doc-type STRING (a Smart Search slug or an
    enum value) used in slot_key/slot.doc_type so the UI and removal/refresh
    round-trip on the exact value the analyst chose; it defaults to the resolved
    `doc_type` enum value."""
    label = doc_type_label or doc_type.value
    slot = SlotSelection(
        client=client, deal=deal,
        slot_key=f"{client}|{deal}|{period}|{label}",
        period=period, doc_type=label,
        status=ResolutionStatus.NOT_FOUND.value,
    )
    if target is not None:
        slot.as_of_date = target.isoformat()
        slot.predicted_period = period_label(target, config.client_period_style(client))
        slot.override_in_effect = (
            lookup_override(
                conn, client=client, deal=deal, as_of_date=target, doc_type=doc_type.value
            )
            is not None
        )
        slot.extra_docs = lookup_extra_docs(
            conn, client=client, deal=deal, as_of_date=target, doc_type=doc_type.value
        )
    try:
        located: LocateResult = locate(
            conn,
            config,
            LocateQuery(
                client=client, deal=deal, period=period, doc_type=doc_type,
                restrict_to_client_sourced=restrict_to_client_sourced,
            ),
            doc_type_spec=doc_type_spec,
        )
    except ValueError as exc:  # unresolvable period — surfaced, never silent
        slot.status = "ERROR"
        slot.detail = str(exc)
        return slot, None
    return slot, located


def _verify_slot(
    slot: SlotSelection,
    located: LocateResult | None,
    config: Config,
    *,
    target: date | None,
    enhanced_period_check: bool,
    client: str,
) -> SlotSelection:
    """Connection-free phase: peek-verify + re-rank the located candidates and
    fill the per-document fields. This is the expensive, I/O-bound work (PDF
    reads, OCR) and holds no sqlite connection, so it parallelizes safely. Peek
    reads are memoized in verify.py, so repeats (preflight->confirm, reloads)
    are cheap."""
    if located is None:
        return slot
    reranked, verdicts = verify_and_rerank(located, config)
    slot.status = reranked.status.value
    slot.detail = reranked.evidence[:200]
    selected = reranked.winner
    selected_path = selected.record.file_path if selected else None
    slot.candidates = [
        _candidate_info(c, verdicts.get(c.record.file_path), selected_path)
        for c in reranked.candidates
    ]
    if selected is not None:
        verdict = verdicts.get(selected_path)
        slot.file_name = selected.record.file_name
        slot.file_path = selected.record.file_path
        slot.last_modified = _iso(selected.record.modified_time)
        slot.page_count = _pdf_page_count(selected.record.file_path)
        slot.doc_class = verdict.doc_class.value if verdict else ""
        slot.verify_status = verdict.status.value if verdict else ""
        slot.confidence = round(verdict.confidence, 4) if verdict else None
        slot.score = round(selected.breakdown.final_score, 2)

    if enhanced_period_check:
        _flag_misfiled(slot, reranked, verdicts, target, config, client)
    return slot


def slot_selection(
    conn,
    config: Config,
    client: str,
    deal: str,
    period: str,
    doc_type: DocType,
    *,
    target: date | None = None,
    enhanced_period_check: bool = False,
    doc_type_spec: DocTypeSpec | None = None,
    restrict_to_client_sourced: bool = True,
    doc_type_label: str | None = None,
) -> SlotSelection:
    """Resolve one (client, deal) slot through the SAME pipeline steps a run
    uses — locate() then the Phase-2 peek-verifier verify_and_rerank().

    Reusable by both the single-firm preflight table (build_selection) and the
    later multi-search service (one slot per call, no dry-run job needed).

    `doc_type_spec`: when supplied (a Smart Search profile), it is passed to
    locate(); otherwise locate() falls back to the builtin `doc_type`.

    `enhanced_period_check`: when True, a slot that did NOT resolve a verified
    winner for the target period but whose top-scored candidate carries an
    in-file as-of date that DISAGREES with the target is surfaced as MISFILED,
    carrying the document's true detected period (no new extraction — the
    peek-verifier already populates VerifyResult.asof_date, even on REJECTED).
    When False, behavior is byte-for-byte as before.
    """
    slot, located = _locate_slot(
        conn, config, client, deal, period, doc_type,
        target=target, doc_type_spec=doc_type_spec,
        restrict_to_client_sourced=restrict_to_client_sourced,
        doc_type_label=doc_type_label,
    )
    return _verify_slot(
        slot, located, config,
        target=target, enhanced_period_check=enhanced_period_check, client=client,
    )


def _flag_misfiled(
    slot: SlotSelection,
    reranked: LocateResult,
    verdicts: dict[str, VerifyResult],
    target: date | None,
    config: Config,
    client: str,
) -> None:
    """Surface a misfiled document: no verified winner for the target period,
    but the best candidate's in-file as-of date disagrees with the target.

    Only sets the misfiled fields when an ACTUAL in-file asof_date disagreement
    exists (never fabricated). When the slot already resolved a FOUND winner for
    the target, nothing is flagged.
    """
    if target is None:
        return
    if slot.status == ResolutionStatus.FOUND.value:
        return
    # Candidates are score-ranked (top first); pick the most relevant one whose
    # verdict carries an in-file as-of date that disagrees with the target.
    style = config.client_period_style(client)
    target_label = period_label(target, style)
    for cand in reranked.candidates:
        verdict = verdicts.get(cand.record.file_path)
        if verdict is None or verdict.asof_date is None:
            continue
        if verdict.asof_date == target:
            continue
        # Same reporting period (e.g. a Feb as-of for a Q1 target) is NOT
        # misfiled when same-period tolerance is on — only a genuinely different
        # period (different quarter/month) counts.
        if config.locator.tolerate_same_period and period_label(verdict.asof_date, style) == target_label:
            continue
        slot.misfiled = True
        slot.detected_as_of = verdict.asof_date.isoformat()
        slot.detected_period = period_label(
            verdict.asof_date, config.client_period_style(client)
        )
        slot.detail = (
            f"Misfiled: document filed under {slot.predicted_period or target.isoformat()} "
            f"but its in-file as-of date is {slot.detected_as_of} "
            f"({slot.detected_period}) — true period differs from the requested period."
        )
        return


# Back-compat alias for the original private name (single-firm preflight path).
_slot_for_pair = slot_selection


class _SelectionContext(BaseModel):
    """The preview slot parameters parsed once from a dry-run job's params and
    reused by both the full table build and a single-slot re-resolve."""

    scope: str
    period: str  # preview period
    doc_type: str  # preview doc type (resolved value)
    doc_types: list[str]
    periods: list[str]
    restrict_to_client_sourced: bool
    client: str | None
    deal: str | None
    enhanced_period_check: bool
    slot_count_multiplier: int  # len(doc_types) * len(periods)


def _selection_context(job, config: Config) -> "_SelectionContext":
    """Parse a dry-run job's params into the preview-slot context shared by the
    full table build and a single-slot re-resolve. The doc-type/spec resolution
    needs the live sqlite connection, so callers do that step themselves."""
    if job is None:
        raise ValueError("unknown job")
    if job.kind != "run" or not job.params.get("dry_run"):
        raise ValueError("document selection requires a dry-run (preflight) job")

    from pv_extractor.api import run_slots as _rs

    params = job.params
    doc_type = DocType(params.get("doc_type", DocType.any_client_valuation_doc.value))
    eff_doc_types = _rs.effective_doc_types(doc_type, list(params.get("doc_types") or []))
    eff_periods = _rs.effective_periods(str(params.get("period", "")), list(params.get("periods") or []))
    ctx = _SelectionContext(
        scope=str(params.get("scope", "")),
        period=eff_periods[0],
        doc_type=eff_doc_types[0],
        doc_types=eff_doc_types,
        periods=eff_periods,
        restrict_to_client_sourced=bool(params.get("restrict_to_client_sourced", True)),
        client=params.get("client") or None,
        deal=params.get("deal") or None,
        enhanced_period_check=bool(
            params.get(
                "enhanced_period_check", config.multi_search.enhanced_period_check_default
            )
        ),
        slot_count_multiplier=len(eff_doc_types) * len(eff_periods),
    )
    return ctx


def _slot_target(config: Config, client: str, period: str) -> date | None:
    try:
        return resolve_target_period(period, config.client_period_style(client))
    except Exception:  # noqa: BLE001 — surfaced later as an ERROR slot
        return None


# Bounded fan-out for the verify (PDF-read/OCR) phase. I/O-bound, so a few
# workers cut wall-clock for a large client without thrashing the share.
_VERIFY_WORKERS = 8


def build_selection(manager: JobManager, job_id: str, config: Config) -> SelectionResponse:
    """The per-slot selection table for a finished dry-run (preflight) job.

    Builds EVERY slot of the run — the full `pairs × periods × doc_types`
    product — not just the first period/doc-type. Two phases: locate() each slot
    serially on the single sqlite connection (cheap), then peek-verify the
    located slots in parallel (the expensive, connection-free, memoized I/O).
    The same files were already peeked by the preflight dry-run, so most reads
    hit the cache here. The Confirm-documents UI groups the returned slots by
    `period` (tabs) then `client` (sections)."""
    job = manager.get(job_id)
    ctx = _selection_context(job, config)

    from pv_extractor.api import run_slots as _rs

    response = SelectionResponse(
        job_id=job_id, scope=ctx.scope, period=ctx.period, doc_type=ctx.doc_type,
        doc_types=ctx.doc_types, periods=ctx.periods,
    )
    exclude = {(s["client"], s["deal"]) for s in job.params.get("exclude", []) if s.get("client")}
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        # Resolve each requested doc-type STRING once (slug -> DocType + spec).
        resolved_types = {dt: _rs.resolve_doc_type(conn, config, dt) for dt in ctx.doc_types}
        pairs = _resolve_pairs(conn, ctx.scope, ctx.client, ctx.deal, exclude)
        # Phase 1 (serial, conn-bound): locate every slot across all periods and
        # doc-types — one located_slot per (pair, period, doc_type) combination.
        located_slots: list[tuple[SlotSelection, LocateResult | None, date | None, str]] = []
        for pair_client, pair_deal in pairs:
            for period in ctx.periods:
                slot_target = _slot_target(config, pair_client, period)
                for dt_label in ctx.doc_types:
                    dt_resolved, dt_spec = resolved_types[dt_label]
                    slot, located = _locate_slot(
                        conn, config, pair_client, pair_deal, period, dt_resolved,
                        target=slot_target, doc_type_spec=dt_spec,
                        restrict_to_client_sourced=ctx.restrict_to_client_sourced,
                        doc_type_label=dt_label,
                    )
                    located_slots.append((slot, located, slot_target, pair_client))
        response.slot_count = len(located_slots)
    finally:
        conn.close()

    # Phase 2 (parallel, connection-free): peek-verify the located slots.
    def _verify(entry):
        slot, located, slot_target, pair_client = entry
        return _verify_slot(
            slot, located, config,
            target=slot_target, enhanced_period_check=ctx.enhanced_period_check,
            client=pair_client,
        )

    if len(located_slots) > 1:
        with ThreadPoolExecutor(
            max_workers=min(_VERIFY_WORKERS, len(located_slots)),
            thread_name_prefix="pv-selection-verify",
        ) as pool:
            response.slots = list(pool.map(_verify, located_slots))
    else:
        response.slots = [_verify(e) for e in located_slots]

    response.found = sum(1 for s in response.slots if s.status == ResolutionStatus.FOUND.value)
    return response


def build_single_slot(
    manager: JobManager, job_id: str, config: Config, client: str, deal: str,
    *, period: str | None = None, doc_type: str | None = None,
) -> SlotSelection:
    """Re-resolve ONE (client, deal, period, doc_type) slot for a dry-run job —
    used after a swap/override so the Confirm-documents table refreshes just the
    affected row instead of re-running locate()+peek-verify for every slot in
    scope. `period`/`doc_type` default to the run's first values for back-compat
    with the single-period table."""
    job = manager.get(job_id)
    ctx = _selection_context(job, config)
    slot_period = period or ctx.period
    slot_doc_type = doc_type or ctx.doc_type

    from pv_extractor.api import run_slots as _rs

    conn = db.open_db(config.db_path, config.pv_root)
    try:
        dt_resolved, dt_spec = _rs.resolve_doc_type(conn, config, slot_doc_type)
        slot_target = _slot_target(config, client, slot_period)
        slot, located = _locate_slot(
            conn, config, client, deal, slot_period, dt_resolved,
            target=slot_target, doc_type_spec=dt_spec,
            restrict_to_client_sourced=ctx.restrict_to_client_sourced,
            doc_type_label=slot_doc_type,
        )
    finally:
        conn.close()
    return _verify_slot(
        slot, located, config,
        target=slot_target, enhanced_period_check=ctx.enhanced_period_check, client=client,
    )
