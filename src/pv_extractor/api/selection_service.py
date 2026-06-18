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

from datetime import date

from pydantic import BaseModel, Field

from pv_extractor.api.jobs import JobManager
from pv_extractor.api.preflight_service import _pdf_page_count
from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import period_label, resolve_target_period
from pv_extractor.locator.locate import locate
from pv_extractor.locator.overrides import lookup_override
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
    slot_key: str  # "client|deal" — stable id for removals/swaps in the UI
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
    slot = SlotSelection(
        client=client, deal=deal, slot_key=f"{client}|{deal}",
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
    for cand in reranked.candidates:
        verdict = verdicts.get(cand.record.file_path)
        if verdict is None or verdict.asof_date is None:
            continue
        if verdict.asof_date == target:
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


def build_selection(manager: JobManager, job_id: str, config: Config) -> SelectionResponse:
    """The per-slot selection table for a finished dry-run (preflight) job."""
    job = manager.get(job_id)
    if job is None:
        raise ValueError(f"unknown job {job_id!r}")
    if job.kind != "run" or not job.params.get("dry_run"):
        raise ValueError("document selection requires a dry-run (preflight) job")

    from pv_extractor.api import run_slots as _rs

    params = job.params
    scope = str(params.get("scope", ""))
    period = str(params.get("period", ""))
    doc_type = DocType(params.get("doc_type", DocType.any_client_valuation_doc.value))
    doc_types = list(params.get("doc_types") or [])
    periods = list(params.get("periods") or [])
    restrict_to_client_sourced = bool(params.get("restrict_to_client_sourced", True))
    client = params.get("client") or None
    deal = params.get("deal") or None
    exclude = {(s["client"], s["deal"]) for s in params.get("exclude", []) if s.get("client")}
    # Enhanced period check is opt-in per job; default off (config-tunable).
    enhanced_period_check = bool(
        params.get(
            "enhanced_period_check",
            config.multi_search.enhanced_period_check_default,
        )
    )

    # Multi-slot fan-out: the table previews the FIRST doc type / period per deal
    # (one row per pair); the launch runs every (pair × doc type × period).
    eff_doc_types = _rs.effective_doc_types(doc_type, doc_types)
    eff_periods = _rs.effective_periods(period, periods)
    preview_period = eff_periods[0]

    response = SelectionResponse(
        job_id=job_id, scope=scope, period=preview_period, doc_type=eff_doc_types[0],
        doc_types=eff_doc_types, periods=eff_periods,
    )
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        preview_doc_type, preview_spec = _rs.resolve_doc_type(conn, config, eff_doc_types[0])
        pairs = _resolve_pairs(conn, scope, client, deal, exclude)
        response.slot_count = len(pairs) * len(eff_doc_types) * len(eff_periods)
        try:
            target = resolve_target_period(
                preview_period, config.client_period_style(client or "default")
            )
        except Exception:  # noqa: BLE001 — per-client style still resolves below
            target = None
        for pair_client, pair_deal in pairs:
            slot_target = target
            if slot_target is None:
                slot_target = resolve_target_period(
                    preview_period, config.client_period_style(pair_client)
                )
            response.slots.append(
                slot_selection(
                    conn,
                    config,
                    pair_client,
                    pair_deal,
                    preview_period,
                    preview_doc_type,
                    target=slot_target,
                    enhanced_period_check=enhanced_period_check,
                    doc_type_spec=preview_spec,
                    restrict_to_client_sourced=restrict_to_client_sourced,
                )
            )
    finally:
        conn.close()
    response.found = sum(1 for s in response.slots if s.status == ResolutionStatus.FOUND.value)
    return response
