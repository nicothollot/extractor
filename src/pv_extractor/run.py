"""Run orchestrator (D7): locate -> verify -> read -> target -> extract ->
validate -> write, per asset/period.

Design points:
  * SQLite work (locate, cache writes) stays on the main thread; document
    I/O (verify peeks, hashing, reading, OCR, extraction) runs in a thread
    pool capped at config.extraction.workers — it is a network share.
    Worker threads that need the cache open their own read-only connection.
  * Results are written in (client, deal) order whatever order the pool
    finishes in, so memo numbering and workbook content are deterministic.
  * Memo-level cache: sha256(file) + schema version + extractor version.
    A cache hit skips re-extraction; rows are still appended when the output
    copy does not already contain the memo (fresh template), and skipped
    when it does (cumulative re-run -> idempotent, no duplicate rows).
  * Graceful per-memo failure isolation: one bad file becomes a qa_fail row
    plus flags; the run continues.
  * --dry-run stops after locate+verify and only returns the coverage table.
  * Phase-3 escalation: per memo, fields below extraction.confidence_threshold
    plus required-but-empty fields become an EscalationPlan. When an asset
    QA-FAILS (e.g. the engine recognized nothing and found no valuation value)
    OR llm_settings.force_assist is set, the plan ALSO broadens to every empty
    LLM-extractable field (excludes IDENTIFICATION/QA/THRESHOLD bands, computed
    fields per rule 7, and positional slots) — so a memo the deterministic
    engine could not parse still gets a real LLM second pass instead of an empty
    plan. force_assist additionally bypasses the deterministic result cache (a
    cached memo carries the OLD narrow plan) and is never written back to it.
    When LLM settings are supplied (CLI default unless --no-llm), the plans are executed through
    hidden local provider sessions (llm/escalate.py) AFTER the deterministic
    result cache is populated and BEFORE rows/audits are written — merged
    values land in the workbook, attempts/costs land in the audit record and
    the run's cost ledger, session ids land in the Run Log "Batch Sessions"
    column. Without settings (library callers, Phase-2 tests) the plans are
    only serialized and the run behaves exactly like Phase 2.
"""

from __future__ import annotations

import logging
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.extract import cache as result_cache
from pv_extractor.extract.derived import derived_specs
from pv_extractor.extract.engine import (
    EngineResult,
    extract_memo,
    load_band_routing,
    load_schema_fields,
)
from pv_extractor.indexer import db
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.indexer.periods import period_label
from pv_extractor.llm.escalate import (
    DealGroup,
    LlmRunSummary,
    LlmSettings,
    process_deals,
    process_memos,
)
from pv_extractor.llm.model_registry import ExtractionPlanMetrics, ModelRegistry
from pv_extractor.locator.locate import locate
from pv_extractor.locator.verify import verify_and_rerank
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    AssetExtraction,
    CandidateFile,
    DocType,
    DocTypeSpec,
    EscalationField,
    EscalationPlan,
    FieldHit,
    FlagSeverity,
    LocateQuery,
    LocateResult,
    MemoResult,
    QaStatus,
    ResolutionStatus,
    ReviewFlag,
    ScoreBreakdown,
    SchemaField,
    VerifyResult,
    VerifyStatus,
)
from pv_extractor.validate import load_rules
from pv_extractor.validate.finalize import finalize_asset_after_assistance
from pv_extractor.write import WorkbookWriter, copy_template, write_audit

logger = logging.getLogger(__name__)


@dataclass
class RunControl:
    """Phase-4 GUI seam: optional progress events and graceful cancellation.

    Both default off — a run without a control behaves exactly like the CLI.
    Events carry identifiers and counters only (client/deal/stage/status/
    file name), never memo content (hard rule 5 applies to the GUI event
    stream too). Cancellation is cooperative: it is checked before each
    memo's verify/extract work starts and between assembly steps, so the
    in-flight memo always finishes and unprocessed memos are marked
    DEFERRED in the coverage table. The LLM phase is skipped entirely when
    cancellation arrives before it starts; once started it completes its
    in-flight calls (the budget tracker bounds its cost)."""

    on_event: Callable[[str, dict], None] | None = None
    cancel_event: threading.Event | None = None

    def emit(self, event: str, **fields: object) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, fields)
        except Exception:  # noqa: BLE001 — an observer must never kill the run
            logger.exception("run observer failed for event %r", event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()


@dataclass(frozen=True)
class RunSlot:
    """One firm/deal/period/doc-type slot for a multi-firm run (Phase C.1a).

    A run() driven by a list of RunSlot spans MANY firms, each slot carrying
    its OWN period and doc_type, instead of the single run-wide period/doc_type
    of the legacy (slots=None) path. The slot is consumed ONLY at the locate
    step (one LocateQuery + locate(..., doc_type_spec=) per slot); everything
    after locate (verify/extract/write/audit/coverage/LLM/cache) is unchanged
    and already keys off each memo's resolved as_of, so per-slot periods work
    transparently. The caller resolves a Smart Search profile slug to a
    DocTypeSpec and passes it as doc_type_spec (run.py never imports
    pv_extractor.search — it only CONSUMES a spec, mirroring locate())."""

    client: str
    deal: str
    period: str
    doc_type: DocType = DocType.any_client_valuation_doc
    doc_type_spec: DocTypeSpec | None = None  # Smart Search profile; None = builtin doc_type
    firm: str | None = None  # event-grouping label; defaults to client
    restrict_to_client_sourced: bool = True  # False = allow HL/non-client sources (rank-only)

    @property
    def group(self) -> str:
        return self.firm or self.client


@dataclass
class CoverageEntry:
    client: str
    deal: str
    status: str  # ResolutionStatus value, ERROR, or DEFERRED (cancelled)
    detail: str = ""


@dataclass
class RunReport:
    run_id: str
    run_dir: Path | None
    workbook_path: Path | None
    dry_run: bool
    coverage: list[CoverageEntry] = field(default_factory=list)
    memos: list[MemoResult] = field(default_factory=list)
    rows_added: int = 0
    flags_added: int = 0
    cache_hits: int = 0
    duration_minutes: float = 0.0
    started_at: str = ""  # ISO wall-clock run start
    finished_at: str = ""  # ISO wall-clock run finish
    llm: LlmRunSummary | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def qa_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in QaStatus}
        for memo in self.memos:
            for asset in memo.assets:
                counts[asset.qa_status.value] += 1
        return counts


@dataclass
class _WorkItem:
    client: str
    deal: str
    locate_result: LocateResult
    verify: VerifyResult | None = None
    engine: EngineResult | None = None
    cached: MemoResult | None = None
    sha256: str = ""
    error: str | None = None
    deferred: bool = False  # cancelled before this memo was processed
    timings_ms: dict[str, float] = field(default_factory=dict)
    group: str | None = None  # multi-firm event-lane label; None = run-wide path (no 'group' field emitted)
    # Multi-doc merge: work items sharing a merge_key are extracted independently
    # then merged into ONE row by best confidence-per-field. merge_primary marks
    # the slot's primary (located) document; the rest are recorded extras.
    merge_key: str | None = None
    merge_primary: bool = False


def _group_kw(item: _WorkItem) -> dict[str, str]:
    """Extra emit kwargs for the multi-firm lane label. Empty for the
    run-wide path (item.group is None) so its emitted event fields stay
    byte-for-byte identical to the legacy behavior."""
    return {"group": item.group} if item.group is not None else {}


def _resolve_pairs(
    conn: sqlite3.Connection,
    scope: str,
    client: str | None,
    deal: str | None,
    exclude: set[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """In-scope (client, deal) pairs, minus any explicitly excluded slots.

    `exclude` is the Phase-4 "Confirm documents" removal set (the analyst
    dropped a slot before launch); it never changes CLI behavior because the
    CLI never passes one."""
    if scope == "deal":
        if not client or not deal:
            raise ValueError("scope=deal requires --client and --deal")
        pairs = [(client, deal)]
    elif scope == "client":
        if not client:
            raise ValueError("scope=client requires --client")
        pairs = [(client, d) for d in db.deals_for_client(conn, client)]
    elif scope == "all":
        pairs = [
            (c, d) for c in db.distinct_clients(conn) for d in db.deals_for_client(conn, c)
        ]
    else:
        raise ValueError(f"unknown scope {scope!r} (expected client|deal|all)")
    if exclude:
        pairs = [pair for pair in pairs if pair not in exclude]
    return pairs


def _verify_and_extract(
    item: _WorkItem, config: Config, schema_fields: list[SchemaField],
    routing: dict[str, list[str]], force: bool, db_path: Path,
    control: RunControl,
) -> _WorkItem:
    """Thread-pool worker: content verification, cache lookup, extraction."""
    if control.cancelled:
        item.deferred = True
        return item
    started = time.perf_counter()
    gkw = _group_kw(item)
    control.emit("stage", client=item.client, deal=item.deal, stage="verify", status="started", **gkw)
    reranked, verdicts = verify_and_rerank(item.locate_result, config)
    item.locate_result = reranked
    item.timings_ms["verify"] = round((time.perf_counter() - started) * 1000, 1)
    control.emit(
        "stage", client=item.client, deal=item.deal, stage="verify",
        status=reranked.status.value,
        file_name=reranked.winner.record.file_name if reranked.winner else None,
        file_path=reranked.winner.record.file_path if reranked.winner else None,
        **gkw,
    )
    if reranked.status is not ResolutionStatus.FOUND or reranked.winner is None:
        return item
    winner_path = reranked.winner.record.file_path
    item.verify = verdicts.get(winner_path)

    item.sha256 = result_cache.file_sha256(winner_path)
    if config.extraction.cache_enabled and not force:
        worker_conn = sqlite3.connect(str(db_path))
        try:
            result_cache.init_cache(worker_conn)
            schema_ver = result_cache.schema_version(_schema_json_path())
            cached = result_cache.get_cached(
                worker_conn, result_cache.cache_key(item.sha256, schema_ver)
            )
        finally:
            worker_conn.close()
        if cached is not None and cached.file_path == winner_path:
            item.cached = cached
            control.emit(
                "stage", client=item.client, deal=item.deal, stage="extract", status="cached",
                **gkw,
            )
            return item

    started = time.perf_counter()
    control.emit("stage", client=item.client, deal=item.deal, stage="read", status="started", **gkw)
    item.engine = extract_memo(winner_path, config, schema_fields, routing)
    item.timings_ms["extract"] = round((time.perf_counter() - started) * 1000, 1)
    control.emit(
        "stage", client=item.client, deal=item.deal, stage="extract", status="done",
        pages=item.engine.page_count if item.engine else None,
        **gkw,
    )
    return item


def _extra_work_items(
    conn, config: Config, client: str, deal: str, located: LocateResult,
    doc_type_value: str, group: str | None,
) -> list[_WorkItem]:
    """Build work items for a slot's EXTRA source documents (Feature: multi-doc
    merge). Each extra doc is run through the normal pipeline like an explicit
    override pick (from_override winner); they share the primary's merge_key so
    the post-LLM merge collapses them into one row. Returns [] for a slot with
    no extras (the common case — zero overhead)."""
    from pv_extractor.locator.overrides import indexed_record_for_path, lookup_extra_docs

    as_of = located.query.as_of_date
    if as_of is None:
        return []
    extras = lookup_extra_docs(conn, client=client, deal=deal, as_of_date=as_of, doc_type=doc_type_value)
    if not extras:
        return []
    primary_path = located.winner.record.file_path if located.winner else None
    merge_key = f"{client}|{deal}|{as_of.isoformat()}|{doc_type_value}"
    items: list[_WorkItem] = []
    for path in extras:
        if path == primary_path:
            continue  # the primary is already the located winner
        record = indexed_record_for_path(conn, path)
        if record is None:
            logger.warning("extra source doc not indexed, skipped: %s", path)
            continue
        winner = CandidateFile(record=record, breakdown=ScoreBreakdown())
        sub = LocateResult(
            status=ResolutionStatus.FOUND, query=located.query,
            candidates=[winner], winner=winner,
            evidence=f"extra source document for {client}/{deal} (multi-doc merge)",
            from_override=True,
        )
        items.append(_WorkItem(
            client=client, deal=deal, locate_result=sub, group=group, merge_key=merge_key,
        ))
    return items


def _schema_json_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schema" / "master_schema.json"


def _metadata_hits(
    schema_by_header: dict[str, SchemaField],
    values: dict[str, object],
    already_populated: set[str],
) -> list[FieldHit]:
    hits = []
    for header, value in values.items():
        if value is None or header in already_populated:
            continue
        schema_field = schema_by_header.get(header)
        if schema_field is None:
            continue
        hits.append(
            FieldHit(
                field=header, col_index=schema_field.col_index, band=schema_field.band,
                value=value, method="metadata", confidence=1.0,
                evidence="run metadata (locator/identification)",
            )
        )
    return hits


def _attach_evidence_source(hits: list[FieldHit], *, source_id: str, source_file: str) -> None:
    for hit in hits:
        if hit.evidence_ref is None:
            continue
        if hit.evidence_ref.source_id is None:
            hit.evidence_ref.source_id = source_id
        if hit.evidence_ref.source_file is None:
            hit.evidence_ref.source_file = source_file


# Bands that are never extracted from the document (run identity, QA verdicts,
# threshold flags) and so are never escalated to the LLM. Derived/computed
# fields (rule 7) are excluded separately, by header, via derived_headers.
_NON_EXTRACTABLE_BANDS = frozenset({"IDENTIFICATION", "QA", "THRESHOLD FLAGS"})


def _is_llm_extractable(field: SchemaField, derived_headers: set[str]) -> bool:
    """A field the LLM could plausibly read off the document: not run identity /
    QA / threshold bands, not a Python-computed field (rule 7), and not a
    positional comp/cap-structure slot (the merge is scalar-by-header, not
    positional — broad escalation of N empty slots only bloats the payload)."""
    if field.band in _NON_EXTRACTABLE_BANDS:
        return False
    if field.header in derived_headers:
        return False
    if field.slot_group is not None:
        return False
    return True


def _build_escalation(
    memo_id: str,
    assets: list[AssetExtraction],
    schema_by_header: dict[str, SchemaField],
    page_band_map: dict[str, list[int]],
    threshold: float,
    *,
    force_assist: bool = False,
    derived_headers: set[str] | None = None,
) -> EscalationPlan:
    """Build the per-memo escalation plan.

    Always escalates below-confidence hits and required-but-empty fields. When
    an asset QA-FAILED (e.g. the deterministic engine recognized nothing and
    found no valuation value) OR force_assist is set, ALSO escalates every empty
    LLM-extractable field for that asset — so a memo the engine could not parse
    still gets a real LLM second pass instead of silently coming back empty.
    force_assist applies this to every asset regardless of QA outcome (the
    analyst chose 'use the LLM to extract')."""
    derived_headers = derived_headers or set()
    fields: list[EscalationField] = []
    seen: set[tuple[str, str]] = set()
    for asset in assets:
        populated = {hit.field for hit in asset.hits if hit.value is not None}
        # Broaden when the analyst forced LLM extraction, or when this asset
        # failed QA — the case the IBMG memo hit, where the engine produced no
        # value hits and nothing was 'required', so the plan was empty.
        broad = force_assist or asset.qa_status is QaStatus.qa_fail
        for hit in asset.hits:
            if hit.method in ("metadata", "computed") or hit.confidence >= threshold:
                continue
            key = (asset.row_memo_id, hit.field)
            if key in seen:
                continue
            seen.add(key)
            fields.append(
                EscalationField(
                    field=hit.field, col_index=hit.col_index, band=hit.band,
                    reason="below_confidence", confidence=hit.confidence,
                    candidate_pages=page_band_map.get(hit.band, []),
                )
            )
        low_confidence_headers = {
            hit.field
            for hit in asset.hits
            if hit.method not in ("metadata", "computed") and hit.confidence < threshold
        }
        for schema_field in schema_by_header.values():
            if not _is_llm_extractable(schema_field, derived_headers):
                continue
            if schema_field.header in low_confidence_headers:
                reason = "below_confidence"
            elif schema_field.required and schema_field.header not in populated:
                reason = "required_empty"
            elif broad:
                reason = "force_llm_assist" if force_assist else "qa_fail_rescue"
            else:
                reason = "primary_catalog"
            key = (asset.row_memo_id, schema_field.header)
            if key in seen:
                continue
            seen.add(key)
            fields.append(
                EscalationField(
                    field=schema_field.header, col_index=schema_field.col_index,
                    band=schema_field.band, reason=reason,
                    candidate_pages=page_band_map.get(schema_field.band, []),
                )
            )
    return EscalationPlan(
        memo_id=memo_id, confidence_threshold=threshold,
        fields=fields, page_band_map=page_band_map,
    )


def run(
    config: Config,
    *,
    scope: str,
    period: str,
    client: str | None = None,
    deal: str | None = None,
    doc_type: DocType = DocType.any_client_valuation_doc,
    template: str | Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
    llm_settings: LlmSettings | None = None,
    llm_client=None,
    control: RunControl | None = None,
    exclude: set[tuple[str, str]] | None = None,
    slots: list[RunSlot] | None = None,
    restrict_to_client_sourced: bool = True,
) -> RunReport:
    """Execute one extraction run. See module docstring for the pipeline.
    llm_settings=None keeps pure Phase-2 behavior (escalation plans are
    serialized but never executed); the CLI builds settings from config.llm
    plus the --llm-* flags. llm_client overrides the local provider subprocess
    client (tests inject fakes — no test launches the real CLI). control
    is the Phase-4 GUI seam (progress events + graceful cancel); None
    keeps exact CLI behavior. exclude drops specific (client, deal) slots from
    the scope (the GUI "Confirm documents" step's removals); None/empty =
    every in-scope pair, exactly like the CLI.

    slots (Phase C.1a, multi-firm): when None — EVERY existing caller (CLI and
    the current GUI start_run) — behavior is byte-for-byte identical to today:
    _resolve_pairs(scope, client, deal, exclude) yields the in-scope pairs and
    the single run-wide period/doc_type drives every locate. When a list of
    RunSlot is provided, it BYPASSES _resolve_pairs and the run-wide
    period/doc_type entirely: one _WorkItem per slot, located via
    LocateQuery(client, deal, period=slot.period, doc_type=slot.doc_type) and
    locate(..., doc_type_spec=slot.doc_type_spec). scope/client/deal/period/
    doc_type/exclude are then ignored for pairing (slots is purely additive —
    the signature stays backward-compatible). One workbook covers the whole
    batch (the existing write phase over the items list). IMPORTANT semantic
    difference vs the run-wide path: there a ValueError from locate (e.g. a bad
    period) propagates and ABORTS the whole run before any processing; in the
    slots path that same ValueError is caught PER SLOT and marks only that slot
    ERROR, so one bad period never kills the rest of the batch. Each slot's
    progress events carry a 'group' field (slot.firm or slot.client) so the GUI
    can lane by firm; the run-wide path emits no 'group' field (byte-for-byte)."""
    started = time.perf_counter()
    now = now or datetime.now()
    run_id = f"RUN_{now:%Y%m%d_%H%M%S}"
    control = control or RunControl()

    conn = db.open_db(config.db_path, config.pv_root)
    try:
        result_cache.init_cache(conn)

        items: list[_WorkItem] = []
        coverage: list[CoverageEntry] = []

        if slots is None:
            # ---- run-wide path (legacy; byte-for-byte unchanged) ----
            pairs = _resolve_pairs(conn, scope, client, deal, exclude)
            log_event(logger, "run started", run_id=run_id, scope=scope, period=period,
                      pairs=len(pairs), dry_run=dry_run)

            # ---- locate (main thread: SQLite) ----
            control.emit("run_started", run_id=run_id, scope=scope, period=period,
                         pairs=len(pairs), dry_run=dry_run)
            for pair_client, pair_deal in pairs:
                query = LocateQuery(
                    client=pair_client, deal=pair_deal, period=period, doc_type=doc_type,
                    restrict_to_client_sourced=restrict_to_client_sourced,
                )
                located = locate(conn, config, query)  # ValueError (bad period) aborts before any processing
                extras = _extra_work_items(conn, config, pair_client, pair_deal, located, doc_type.value, None)
                items.append(_WorkItem(
                    client=pair_client, deal=pair_deal, locate_result=located,
                    merge_key=extras[0].merge_key if extras else None,
                    merge_primary=bool(extras),
                ))
                items.extend(extras)
                control.emit(
                    "stage", client=pair_client, deal=pair_deal, stage="locate",
                    status=located.status.value,
                    file_name=located.winner.record.file_name if located.winner else None,
                )
        else:
            # ---- multi-firm slots path (Phase C.1a) ----
            firms = {slot.group for slot in slots}
            log_event(logger, "run started", run_id=run_id, scope="slots",
                      slots=len(slots), firms=len(firms), dry_run=dry_run)
            control.emit("run_started", run_id=run_id, scope="slots",
                         slots=len(slots), firms=len(firms), dry_run=dry_run)
            for slot in slots:
                query = LocateQuery(client=slot.client, deal=slot.deal,
                                    period=slot.period, doc_type=slot.doc_type,
                                    restrict_to_client_sourced=slot.restrict_to_client_sourced)
                try:
                    # Unlike the run-wide path, a per-slot ValueError (e.g. a
                    # bad period) is contained: only this slot is marked ERROR
                    # so one bad slot never aborts the whole batch.
                    located = locate(conn, config, query, doc_type_spec=slot.doc_type_spec)
                    extras = _extra_work_items(
                        conn, config, slot.client, slot.deal, located, slot.doc_type.value, slot.group
                    )
                    item = _WorkItem(
                        client=slot.client, deal=slot.deal, locate_result=located, group=slot.group,
                        merge_key=extras[0].merge_key if extras else None,
                        merge_primary=bool(extras),
                    )
                    control.emit(
                        "stage", client=slot.client, deal=slot.deal, stage="locate",
                        status=located.status.value,
                        file_name=located.winner.record.file_name if located.winner else None,
                        group=slot.group,
                    )
                except ValueError as exc:
                    located = LocateResult(
                        status=ResolutionStatus.NOT_FOUND, query=query,
                        evidence=f"locate failed: {type(exc).__name__}: {exc}",
                    )
                    item = _WorkItem(client=slot.client, deal=slot.deal,
                                     locate_result=located, group=slot.group,
                                     error=f"{type(exc).__name__}: {exc}")
                    extras = []
                    logger.exception("locate failed for slot %s/%s", slot.client, slot.deal)
                    control.emit(
                        "stage", client=slot.client, deal=slot.deal, stage="locate",
                        status="ERROR", file_name=None, group=slot.group,
                    )
                items.append(item)
                items.extend(extras)

        report = RunReport(run_id=run_id, run_dir=None, workbook_path=None, dry_run=dry_run)
        report.started_at = now.isoformat(timespec="seconds")

        schema_fields = load_schema_fields()
        routing = load_band_routing()
        schema_by_header = {f.header: f for f in schema_fields}
        ruleset = load_rules(config.validation.rules_path)
        # Computed/derived headers (rule 7) are never escalated to the LLM even
        # under force_assist; cache the set once for _build_escalation.
        derived_headers = {spec.header for spec in derived_specs(config.validation)}
        # force_assist makes the LLM the primary extractor: bypass the
        # deterministic result cache so the broad escalation plan is actually
        # rebuilt (a cached memo carries the OLD, narrow plan). Deal-document
        # grouping is only a batching choice; it must not broaden fields and
        # must not bypass deterministic cache.
        llm_on = bool(llm_settings is not None and llm_settings.enabled)
        combine_deal_documents = bool(
            llm_on and (config.llm.combine_deal_documents or config.llm.one_call_per_deal)
        )
        force_assist = bool(llm_settings is not None and llm_settings.force_assist)
        effective_force = force or force_assist

        # ---- verify (+extract unless dry-run) in the worker pool ----
        # AMBIGUOUS results are verified too: content verification is what
        # disambiguates same-score candidates (D3 re-rank).
        candidates = [
            item
            for item in items
            if item.locate_result.status in (ResolutionStatus.FOUND, ResolutionStatus.AMBIGUOUS)
        ]
        with ThreadPoolExecutor(max_workers=max(config.extraction.workers, 1)) as pool:
            if dry_run:
                futures = {
                    pool.submit(_dry_verify, item, config, control): item for item in candidates
                }
            else:
                futures = {
                    pool.submit(
                        _verify_and_extract, item, config, schema_fields, routing, effective_force,
                        config.db_path, control,
                    ): item
                    for item in candidates
                }
            for future, item in futures.items():
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 — isolation: one memo never kills the run
                    item.error = f"{type(exc).__name__}: {exc}"
                    logger.exception("memo processing failed for %s/%s", item.client, item.deal)

        for item in items:
            result = item.locate_result
            detail = (
                result.winner.record.file_name
                if result.winner is not None
                else result.evidence[:160]
            )
            if item.deferred:
                status, detail = "DEFERRED", "run cancelled before this memo was processed"
            elif item.error:
                status, detail = "ERROR", item.error
            else:
                status = result.status.value
            coverage.append(
                CoverageEntry(client=item.client, deal=item.deal, status=status, detail=detail)
            )
        report.coverage = coverage

        if dry_run:
            report.duration_minutes = round((time.perf_counter() - started) / 60.0, 2)
            report.finished_at = datetime.now().isoformat(timespec="seconds")
            log_event(logger, "dry run complete", run_id=run_id,
                      coverage={c.status: sum(1 for x in coverage if x.status == c.status) for c in coverage})
            control.emit("run_complete", run_id=run_id, dry_run=True,
                         coverage={c.status: sum(1 for x in coverage if x.status == c.status) for c in coverage})
            return report

        # ---- write phase (main thread), deterministic (client, deal) order ----
        run_dir = Path(config.output_dir) / run_id
        template_path = Path(template) if template else _default_template()
        workbook_path = run_dir / f"master_index_{run_id}.xlsx"
        copy_template(template_path, workbook_path, config.pv_root)
        writer = WorkbookWriter(workbook_path, schema_fields, config.pv_root)
        report.run_dir = run_dir
        report.workbook_path = workbook_path

        # Assemble every memo first (and populate the deterministic result
        # cache) so the LLM second pass can run over the whole batch before
        # any row/audit is written.
        memo_counter = 0
        schema_ver = result_cache.schema_version(_schema_json_path())
        assembled: list[tuple[_WorkItem, MemoResult]] = []
        for item in sorted(items, key=lambda i: (i.client.lower(), i.deal.lower())):
            if item.deferred or item.locate_result.status is not ResolutionStatus.FOUND or item.locate_result.winner is None:
                continue
            if control.cancelled:
                item.deferred = True
                _mark_coverage_deferred(report, item)
                continue
            try:
                if item.cached is not None:
                    memo = item.cached
                    report.cache_hits += 1
                else:
                    memo_counter += 1
                    memo = _assemble_memo(
                        item, config, schema_by_header, ruleset, routing, writer,
                        run_id=run_id, memo_seq=memo_counter, now=now,
                        force_assist=force_assist, derived_headers=derived_headers,
                    )
                    # Never persist a force_assist memo: its escalation plan is
                    # intentionally broad and would poison a later normal run.
                    if (
                        config.extraction.cache_enabled and not force_assist
                        and item.engine is not None and not item.engine.fatal
                    ):
                        result_cache.put_cached(
                            conn, result_cache.cache_key(item.sha256, schema_ver), schema_ver, memo
                        )
                assembled.append((item, memo))
                control.emit(
                    "stage", client=item.client, deal=item.deal, stage="validate",
                    status="done", memo_id=memo.memo_id,
                    qa={a.row_memo_id: a.qa_status.value for a in memo.assets},
                    escalated_fields=len(memo.escalation.fields) if memo.escalation else 0,
                    **_group_kw(item),
                )
            except Exception as exc:  # noqa: BLE001 — isolation
                logger.exception("assembly failed for %s/%s", item.client, item.deal)
                _mark_coverage_error(report, item, exc)

        # Phase-3 second pass: local CLI LLM assist over the escalation
        # plans (merged hits mutate the memos before rows/audits are written).
        # A cancellation that arrived before this point skips the LLM phase
        # entirely (plans stay serialized in the audits, exactly like --no-llm).
        if llm_settings is not None and llm_settings.enabled and not control.cancelled:
            all_groups = _build_deal_groups(assembled)
            deal_groups, document_memos, routing_diag = _route_llm_groups(all_groups, config, llm_settings)
            control.emit(
                "llm_phase",
                status="started",
                memos=len(document_memos) + len(deal_groups),
                routing_mode=llm_settings.mode,
            )
            report.llm = LlmRunSummary(enabled=True, executed=True, diagnostics=routing_diag)
            if deal_groups:
                deal_summary = process_deals(
                    deal_groups, config, llm_settings, schema_fields,
                    run_id=run_id, run_dir=run_dir, client=llm_client,
                )
                report.llm = _merge_llm_summaries(report.llm, deal_summary)
            if document_memos:
                doc_summary = process_memos(
                    document_memos, config, llm_settings, schema_fields,
                    run_id=run_id, run_dir=run_dir, client=llm_client,
                )
                report.llm = _merge_llm_summaries(report.llm, doc_summary)
            control.emit(
                "llm_phase", status="done", attempts=report.llm.attempts,
                cache_hits=report.llm.cache_hits, deferred=report.llm.memos_deferred,
                total_cost_usd=report.llm.total_cost_usd,
                cost_source="actual+estimated" if report.llm.any_actual_costs else "estimated",
            )

        # Multi-doc merge: collapse each slot's per-document memos into one row
        # (best confidence per field), AFTER the LLM pass so every doc's filled
        # values + confidences are final. No-op when no slot has extra docs.
        assembled = _merge_assembled(assembled, config, schema_by_header, ruleset, routing, writer)
        finalization_ms = 0.0
        final_started = time.perf_counter()
        for _, memo in assembled:
            _finalize_memo_assets(memo, config, schema_by_header, ruleset, routing, writer)
        finalization_ms += (time.perf_counter() - final_started) * 1000

        if (
            llm_settings is not None
            and llm_settings.enabled
            and config.llm.planner.rescue_enabled
            and config.llm.candidate_arbitration.repair_policy == "core_only"
            and not control.cancelled
        ):
            rescue_memos = _prepare_rescue_wave(assembled, config, schema_by_header, derived_headers)
            if rescue_memos:
                control.emit("llm_phase", status="rescue_started", memos=len(rescue_memos))
                rescue_summary = process_memos(
                    rescue_memos, config, llm_settings, schema_fields,
                    run_id=run_id, run_dir=run_dir, client=llm_client,
                )
                report.llm = _merge_llm_summaries(report.llm, rescue_summary)
                final_started = time.perf_counter()
                for _, memo in assembled:
                    _finalize_memo_assets(memo, config, schema_by_header, ruleset, routing, writer)
                finalization_ms += (time.perf_counter() - final_started) * 1000
                control.emit(
                    "llm_phase", status="rescue_done", attempts=rescue_summary.attempts,
                    cache_hits=rescue_summary.cache_hits, total_cost_usd=rescue_summary.total_cost_usd,
                )
        report.diagnostics = _build_run_diagnostics(assembled, report.llm, finalization_ms)

        for item, memo in assembled:
            try:
                report.memos.append(memo)
                report.rows_added += _write_memo(writer, memo)
                report.flags_added += _write_flags(writer, memo)
                write_audit(memo, run_dir, config.pv_root)
                control.emit(
                    "stage", client=item.client, deal=item.deal, stage="write",
                    status="done", memo_id=memo.memo_id,
                    **_group_kw(item),
                )
            except Exception as exc:  # noqa: BLE001 — isolation
                logger.exception("write phase failed for %s/%s", item.client, item.deal)
                _mark_coverage_error(report, item, exc)

        duration_minutes = round((time.perf_counter() - started) / 60.0, 2)
        report.duration_minutes = duration_minutes
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        qa = report.qa_counts()
        llm = report.llm
        if llm is None or not llm.enabled:
            notes = "Deterministic run; LLM fallback disabled"
        elif not llm.executed:
            notes = f"LLM fallback unavailable: {llm.detail}"
        else:
            label = "actual+estimated" if llm.any_actual_costs else "ESTIMATED"
            notes = (
                f"LLM assist ({config.llm.provider}): {llm.memos_escalated} memo(s) escalated, "
                f"{llm.attempts} call(s) ({llm.cache_hits} cached), "
                f"{llm.memos_deferred} deferred, ${llm.total_cost_usd:.4f} ({label})"
            )
        writer.append_run_log(
            {
                "Run ID": run_id,
                "Run Date": now.date().isoformat(),
                "Memos Processed": len(report.memos),
                "Assets Extracted": sum(len(m.assets) for m in report.memos),
                "QA Pass": qa[QaStatus.qa_pass.value],
                "QA Pass with Flags": qa[QaStatus.qa_pass_with_flags.value],
                "QA Fail": qa[QaStatus.qa_fail.value],
                "Records Added to Index": report.rows_added,
                "Total Flags": report.flags_added,
                "Reviewer Attention Items": sum(
                    1
                    for memo in report.memos
                    for asset in memo.assets
                    for flag in asset.flags
                    if flag.reviewer_attention
                ),
                "Run Duration (mins)": duration_minutes,
                # provider session identifiers (job id[:session id]) — not
                # API batch ids; there is no Batch API in this architecture.
                "Batch Sessions": "; ".join(llm.session_labels) if llm and llm.session_labels else None,
                "Notes": notes,
            }
        )
        _write_diagnostics(report, config)
        writer.save()
        log_event(
            logger, "run complete", run_id=run_id, memos=len(report.memos),
            rows_added=report.rows_added, flags=report.flags_added,
            cache_hits=report.cache_hits,
            llm_fallback="enabled" if llm and llm.enabled else "disabled",
            llm_cost_usd=llm.total_cost_usd if llm else None,
        )
        control.emit(
            "run_complete", run_id=run_id, dry_run=False, memos=len(report.memos),
            rows_added=report.rows_added, flags=report.flags_added,
            cache_hits=report.cache_hits, qa=report.qa_counts(),
            cancelled=control.cancelled,
            llm_cost_usd=llm.total_cost_usd if llm else None,
        )
        return report
    finally:
        conn.close()


def _mark_coverage_error(report: RunReport, item: _WorkItem, exc: Exception) -> None:
    coverage_entry = next(
        c for c in report.coverage if c.client == item.client and c.deal == item.deal
    )
    coverage_entry.status = "ERROR"
    coverage_entry.detail = f"{type(exc).__name__}: {exc}"


def _mark_coverage_deferred(report: RunReport, item: _WorkItem) -> None:
    coverage_entry = next(
        c for c in report.coverage if c.client == item.client and c.deal == item.deal
    )
    coverage_entry.status = "DEFERRED"
    coverage_entry.detail = "run cancelled before this memo was processed"


def _dry_verify(item: _WorkItem, config: Config, control: RunControl) -> _WorkItem:
    if control.cancelled:
        item.deferred = True
        return item
    reranked, verdicts = verify_and_rerank(item.locate_result, config)
    item.locate_result = reranked
    if reranked.winner is not None:
        item.verify = verdicts.get(reranked.winner.record.file_path)
    control.emit(
        "stage", client=item.client, deal=item.deal, stage="verify",
        status=reranked.status.value,
        file_name=reranked.winner.record.file_name if reranked.winner else None,
        file_path=reranked.winner.record.file_path if reranked.winner else None,
        **_group_kw(item),
    )
    return item


def _default_template() -> Path:
    return Path(__file__).resolve().parents[2] / "reference" / "master_index_v4.xlsx"


def _prior_row_for_hits(
    writer: WorkbookWriter,
    hits: list[FieldHit],
    as_of: date | None,
) -> dict[str, object] | None:
    if as_of is None:
        return None
    company = next((h.value for h in hits if h.field == "Portfolio Company"), None)
    fund = next((h.value for h in hits if h.field == "Fund Name"), None)
    return writer.find_prior_row(
        str(company) if company else None,
        str(fund) if fund else None,
        as_of,
    )


def _finalize_memo_assets(
    memo: MemoResult,
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset,
    routing: dict[str, list[str]],
    writer: WorkbookWriter,
) -> None:
    for index, asset in enumerate(memo.assets):
        prior_row = _prior_row_for_hits(writer, asset.hits, memo.as_of_date)
        finalize_asset_after_assistance(
            asset,
            config=config,
            schema_by_header=schema_by_header,
            ruleset=ruleset,
            routing_table=routing,
            as_of_date=memo.as_of_date,
            verify=memo.verify,
            prior_row=prior_row,
            client=memo.client,
            extra_flags=memo.memo_flags if index == 0 else None,
        )
        _attach_evidence_source(asset.hits, source_id=memo.memo_id, source_file=memo.file_path)


def _hit_for(asset: AssetExtraction, header: str) -> FieldHit | None:
    return next((hit for hit in asset.hits if hit.field == header), None)


def _rescue_pages(memo: MemoResult, asset: AssetExtraction, field: SchemaField, config: Config) -> list[int]:
    pages: list[int] = []
    hit = _hit_for(asset, field.header)
    if hit is not None and hit.page is not None:
        pages.append(hit.page)
    pages.extend(memo.page_band_map.get(field.band, []))
    limit = max(1, config.llm.planner.rescue_max_pages)
    deduped: list[int] = []
    for page in pages:
        if page and page not in deduped:
            deduped.append(page)
        if len(deduped) >= limit:
            break
    return deduped


def _prepare_rescue_wave(
    assembled: list[tuple[_WorkItem, MemoResult]],
    config: Config,
    schema_by_header: dict[str, SchemaField],
    derived_headers: set[str],
) -> list[MemoResult]:
    """Build a rescue wave only for specific unresolved hard-fail fields.

    This never re-runs a whole memo: required-missing fields and field-scoped
    hard-fail validation flags are the only candidates, capped by
    llm.planner.rescue_max_fields.
    """
    rescue_memos: list[MemoResult] = []
    max_fields = max(1, config.llm.planner.rescue_max_fields)
    for _item, memo in assembled:
        rescue: dict[str, EscalationField] = {}
        for asset in memo.assets:
            populated = {hit.field for hit in asset.hits if hit.value is not None}
            for field in schema_by_header.values():
                if len(rescue) >= max_fields:
                    break
                if not field.required or field.header in populated:
                    continue
                if not _is_llm_extractable(field, derived_headers):
                    continue
                rescue.setdefault(
                    field.header,
                    EscalationField(
                        field=field.header,
                        col_index=field.col_index,
                        band=field.band,
                        reason="finalization_rescue",
                        candidate_pages=_rescue_pages(memo, asset, field, config),
                    ),
                )
            for flag in asset.flags:
                if len(rescue) >= max_fields:
                    break
                if flag.severity != FlagSeverity.hard_fail or not flag.field:
                    continue
                field = schema_by_header.get(flag.field)
                if field is None or not _is_llm_extractable(field, derived_headers):
                    continue
                hit = _hit_for(asset, field.header)
                if hit is not None and hit.method == "deterministic" and hit.confidence >= config.extraction.confidence_threshold:
                    continue
                rescue.setdefault(
                    field.header,
                    EscalationField(
                        field=field.header,
                        col_index=field.col_index,
                        band=field.band,
                        reason="finalization_rescue",
                        confidence=hit.confidence if hit else None,
                        candidate_pages=_rescue_pages(memo, asset, field, config),
                    ),
                )
        if rescue:
            if memo.escalation is None:
                memo.escalation = EscalationPlan(
                    memo_id=memo.memo_id,
                    confidence_threshold=config.extraction.confidence_threshold,
                    page_band_map=memo.page_band_map,
                )
            memo.escalation.fields = list(rescue.values())
            memo.escalation.merge_log.append(
                "rescue wave planned for fields: "
                + ", ".join(field.field for field in memo.escalation.fields)
            )
            rescue_memos.append(memo)
    return rescue_memos


def _merge_counter_dict(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for key, value in b.items():
        out[key] = int(out.get(key, 0)) + int(value)
    return out


def _merge_diagnostics(a: dict[str, object], b: dict[str, object]) -> dict[str, object]:
    out = dict(a)
    for key, value in b.items():
        if key == "task_count_by_wave" and isinstance(value, dict):
            out[key] = _merge_counter_dict(
                out.get(key, {}) if isinstance(out.get(key), dict) else {},
                value,
            )
        elif isinstance(value, (int, float)) and isinstance(out.get(key), (int, float)):
            out[key] = round(float(out[key]) + float(value), 4)
        else:
            out.setdefault(key, value)
    return out


def _merge_llm_summaries(
    primary: LlmRunSummary | None,
    extra: LlmRunSummary,
) -> LlmRunSummary:
    if primary is None:
        return extra
    primary.executed = primary.executed or extra.executed
    primary.memos_escalated += extra.memos_escalated
    primary.memos_deferred += extra.memos_deferred
    primary.memos_failed += extra.memos_failed
    primary.attempts += extra.attempts
    primary.cache_hits += extra.cache_hits
    primary.total_cost_usd = round(primary.total_cost_usd + extra.total_cost_usd, 4)
    primary.any_actual_costs = primary.any_actual_costs or extra.any_actual_costs
    primary.session_labels.extend(extra.session_labels)
    primary.diagnostics = _merge_diagnostics(primary.diagnostics, extra.diagnostics)
    return primary


def _build_run_diagnostics(
    assembled: list[tuple[_WorkItem, MemoResult]],
    llm: LlmRunSummary | None,
    finalization_ms: float,
) -> dict[str, object]:
    deterministic_ms = sum(
        float(memo.timings_ms.get("extract", 0.0))
        for _item, memo in assembled
    )
    return {
        "deterministic_extraction_ms": round(deterministic_ms, 1),
        "finalization_ms": round(finalization_ms, 1),
        "llm": llm.diagnostics if llm else {},
    }


def _write_diagnostics(report: RunReport, config: Config) -> None:
    if report.run_dir is None:
        return
    path = report.run_dir / "diagnostics.json"
    with guarded_open_write(path, config.pv_root) as fh:
        json.dump(
            {
                "run_id": report.run_id,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "diagnostics": report.diagnostics,
            },
            fh,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        fh.write("\n")


_MERGE_EXCLUDED_BANDS = {"QA", "THRESHOLD FLAGS"}


def _merge_asset(
    primary_memo: MemoResult,
    primary_asset: AssetExtraction,
    members: list[tuple[MemoResult, AssetExtraction]],
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset,
    routing: dict[str, list[str]],
    writer: WorkbookWriter,
    derived_headers: set[str],
) -> AssetExtraction:
    """Merge one asset across its source documents: per field, keep the
    highest-confidence non-empty hit (tagged with its source file), recompute
    derived fields from the merged inputs, then re-validate so the row's QA
    reflects the combined data (a field missing in doc A but present in doc B
    now counts)."""
    meta = [h for h in primary_asset.hits if h.method == "metadata"]
    best: dict[str, tuple[FieldHit, str]] = {}
    for memo, asset in members:
        for h in asset.hits:
            if h.method == "metadata" or h.band in _MERGE_EXCLUDED_BANDS or h.field in derived_headers:
                continue
            if h.value is None:
                continue
            current = best.get(h.field)
            if current is None or h.confidence > current[0].confidence:
                best[h.field] = (h, memo.file_name)
    real_hits: list[FieldHit] = []
    for hit, source in best.values():
        merged_hit = hit.model_copy(deep=True)
        merged_hit.source_file = source
        real_hits.append(merged_hit)

    merged_asset = AssetExtraction(
        asset_name=primary_asset.asset_name,
        row_memo_id=primary_asset.row_memo_id,
        hits=[*meta, *real_hits],
        flags=[flag for _memo, asset in members for flag in asset.flags],
        qa_status=primary_asset.qa_status,
    )
    prior_row = _prior_row_for_hits(writer, merged_asset.hits, primary_memo.as_of_date)
    return finalize_asset_after_assistance(
        merged_asset,
        config=config,
        schema_by_header=schema_by_header,
        ruleset=ruleset,
        routing_table=routing,
        as_of_date=primary_memo.as_of_date,
        verify=primary_memo.verify,
        prior_row=prior_row,
        client=primary_memo.client,
    )


def _merge_memo_group(
    primary: MemoResult,
    memos: list[MemoResult],
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset,
    routing: dict[str, list[str]],
    writer: WorkbookWriter,
) -> MemoResult:
    """Collapse a group of per-document memos for one investment into ONE memo
    (one row per asset), merging fields by best confidence. Keeps the primary
    document's identity; records the contributing source documents."""
    derived_headers = {spec.header for spec in derived_specs(config.validation)}
    merged = primary.model_copy(deep=True)
    sources = [m.file_name for m in memos]
    merged.memo_flags.append(
        ReviewFlag(
            category="run",
            description=(
                f"Merged from {len(memos)} source documents (best-confidence per field): "
                + ", ".join(sources)
            ),
            severity=FlagSeverity.info,
        )
    )

    # Group assets across memos. The common case is one asset per document for
    # the SAME investment — but each doc may extract a slightly different name
    # ('Accell . . vf' vs '... v1'), so when every document is single-asset we
    # merge them into ONE bucket rather than splitting on the noisy name. Only
    # genuine multi-asset (joint) memos bucket by normalized name.
    buckets: dict[str, list[tuple[MemoResult, AssetExtraction]]] = {}
    order: list[str] = []
    single_asset = max((len(m.assets) for m in memos), default=0) <= 1
    for memo in memos:
        for asset in memo.assets:
            key = "__single__" if single_asset else ((asset.asset_name or "").strip().lower() or "__single__")
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append((memo, asset))

    merged_assets: list[AssetExtraction] = []
    for key in order:
        bucket = buckets[key]
        primary_asset = next((a for m, a in bucket if m is primary), bucket[0][1])
        merged_assets.append(
            _merge_asset(
                primary, primary_asset, bucket, config, schema_by_header,
                ruleset, routing, writer, derived_headers,
            )
        )
    merged.assets = merged_assets
    return merged


def _build_deal_groups(
    assembled: list[tuple[_WorkItem, MemoResult]],
) -> list[DealGroup]:
    """Group the assembled (item, memo) pairs into one DealGroup per deal-period
    for the one-call-per-deal LLM pass. Grouping mirrors _merge_assembled exactly
    (merge_key, with non-merge items standalone) so the single combined call's
    hits land on the SAME memo the multi-doc merge later keeps as the row.

    The primary is the merge primary (else the largest-page member); its
    documents lead the combined payload. Non-primary members are folded into the
    one call (their content is sent as context) and marked not_needed so they are
    not separately escalated."""
    keyed: dict[str, list[tuple[_WorkItem, MemoResult]]] = {}
    order: list[str] = []
    for item, memo in assembled:
        key = item.merge_key or memo.memo_id
        if key not in keyed:
            keyed[key] = []
            order.append(key)
        keyed[key].append((item, memo))

    groups: list[DealGroup] = []
    for key in order:
        members = keyed[key]
        _, primary_memo = next(
            ((it, m) for it, m in members if it.merge_primary),
            max(members, key=lambda im: im[1].page_count),
        )
        member_memos = [m for _, m in members]
        files = [(primary_memo.file_path, primary_memo.file_name)]
        files += [
            (m.file_path, m.file_name) for m in member_memos if m is not primary_memo
        ]
        groups.append(DealGroup(primary=primary_memo, members=member_memos, files=files))
    return groups


def _group_plan_metrics(group: DealGroup, config: Config) -> ExtractionPlanMetrics:
    pages = sum(max(0, memo.page_count) for memo in group.members)
    image_pages = sum(
        1
        for memo in group.members
        for page_class in memo.page_classes.values()
        if page_class.value in {"SCANNED", "IMAGE_TABLE"}
    )
    fields = len(group.primary.escalation.fields) if group.primary.escalation else 0
    prompt_chars = pages * 2800 + fields * config.llm.output_tokens_per_field * 4
    return ExtractionPlanMetrics(
        estimated_input_tokens=int(prompt_chars / max(config.llm.chars_per_token, 1.0)),
        documents=max(1, len(group.members)),
        image_pages=image_pages,
        fields=fields,
    )


def _route_llm_groups(
    groups: list[DealGroup],
    config: Config,
    settings: LlmSettings,
) -> tuple[list[DealGroup], list[MemoResult], dict[str, object]]:
    registry = ModelRegistry.load(config.llm.models_path)
    deal_groups: list[DealGroup] = []
    document_memos: list[MemoResult] = []
    resolved: list[dict[str, object]] = []
    force_deal = bool(config.llm.combine_deal_documents or config.llm.one_call_per_deal)
    for group in groups:
        metrics = _group_plan_metrics(group, config)
        plan = registry.resolve_extraction_plan(
            routing_mode=settings.mode,
            metrics=metrics,
            auto=config.llm.auto,
            provider=config.llm.provider,
            manual_model=settings.manual_model,
            manual_effort=settings.manual_effort,
            execution_shape="deal" if force_deal else "profile",
            allow_fable=settings.allow_fable,
            provider_default_model=config.codex_cli.model,
            provider_default_effort=config.codex_cli.reasoning_effort,
            repair_policy=config.llm.candidate_arbitration.repair_policy,
            max_repair_calls_per_deal=config.llm.candidate_arbitration.max_repair_calls_per_deal,
        )
        if group.primary.escalation is not None:
            group.primary.escalation.diagnostics["resolved_launch_plan"] = plan.model_dump(
                exclude={"selection"}
            )
        resolved.append({"deal_key": f"{group.primary.client}|{group.primary.deal}|{group.primary.as_of_date}", **plan.model_dump(exclude={"selection"})})
        if plan.execution_shape == "deal" and group.primary.escalation and group.primary.escalation.fields:
            for member in group.members:
                if member is not group.primary and member.escalation is not None:
                    member.escalation.status = "not_needed"
            deal_groups.append(group)
        else:
            document_memos.extend(
                member for member in group.members if member.escalation and member.escalation.fields
            )
    return deal_groups, document_memos, {"resolved_launch_plan": resolved}


def _merge_assembled(
    assembled: list[tuple[_WorkItem, MemoResult]],
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset,
    routing: dict[str, list[str]],
    writer: WorkbookWriter,
) -> list[tuple[_WorkItem, MemoResult]]:
    """Collapse multi-doc merge groups in the assembled list into one entry
    each (emitted at the primary's position). Items without a merge_key, and
    groups that ended up with a single member, pass through unchanged."""
    keyed: dict[str, list[tuple[_WorkItem, MemoResult]]] = {}
    for item, memo in assembled:
        if item.merge_key:
            keyed.setdefault(item.merge_key, []).append((item, memo))

    out: list[tuple[_WorkItem, MemoResult]] = []
    emitted: set[str] = set()
    for item, memo in assembled:
        if not item.merge_key:
            out.append((item, memo))
            continue
        if item.merge_key in emitted:
            continue
        emitted.add(item.merge_key)
        members = keyed[item.merge_key]
        if len(members) == 1:
            out.append(members[0])
            continue
        primary_item, primary_memo = next(
            ((it, m) for it, m in members if it.merge_primary), members[0]
        )
        merged = _merge_memo_group(
            primary_memo, [m for _, m in members], config, schema_by_header, ruleset, routing, writer
        )
        log_event(
            logger, "multi-doc merge", merge_key=item.merge_key,
            documents=len(members), memo_id=merged.memo_id,
        )
        out.append((primary_item, merged))
    return out


def _assemble_memo(
    item: _WorkItem,
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset,
    routing: dict[str, list[str]],
    writer: WorkbookWriter,
    *,
    run_id: str,
    memo_seq: int,
    now: datetime,
    force_assist: bool = False,
    derived_headers: set[str] | None = None,
) -> MemoResult:
    winner = item.locate_result.winner
    assert winner is not None
    record = winner.record
    as_of = item.locate_result.query.as_of_date
    memo_id = f"MEMO_{now:%Y%m%d_%H%M%S}_{memo_seq:03d}"
    reporting = period_label(as_of, config.client_period_style(item.client)) if as_of else ""

    memo = MemoResult(
        memo_id=memo_id, run_id=run_id, client=item.client, deal=item.deal,
        file_path=record.file_path, file_name=record.file_name, file_sha256=item.sha256,
        as_of_date=as_of, reporting_period=reporting,
        locate_status=item.locate_result.status, locate_evidence=item.locate_result.evidence,
        locator_breakdown=winner.breakdown, verify=item.verify,
        timings_ms=item.timings_ms,
    )
    # An explicit analyst override ran a file the peek-verifier would have
    # rejected (HL work product, wrong period/asset). Never silent: flag it so
    # the row is reviewable, but the chosen file was still extracted.
    if item.locate_result.from_override and item.verify is not None and item.verify.status is VerifyStatus.REJECTED:
        memo.memo_flags.append(
            ReviewFlag(
                category="locator",
                description=(
                    f"MANUAL OVERRIDE: ran analyst-selected file despite content verification "
                    f"({item.verify.reason}) — review that this is the intended source"
                ),
                severity=FlagSeverity.warning, reviewer_attention=True,
            )
        )
    engine = item.engine
    if engine is None:
        memo.error = item.error or "extraction did not run"
        memo.memo_flags.append(
            ReviewFlag(category="run", description=memo.error, severity=FlagSeverity.hard_fail,
                       reviewer_attention=True)
        )
    else:
        memo.reader = engine.reader
        memo.page_count = engine.page_count
        memo.page_classes = engine.page_classes
        memo.page_band_map = engine.page_band_map
        memo.memo_flags.extend(engine.memo_flags)

    asset_sources = (
        engine.assets if engine is not None and engine.assets else [(None, [], [])]
    )
    for index, (asset_name, hits, extraction_flags) in enumerate(asset_sources, start=1):
        row_memo_id = memo_id if index == 1 else f"{memo_id}-A{index}"
        populated = {hit.field for hit in hits if hit.value is not None}
        resolved_asset = asset_name or (item.verify.asset_names[0] if item.verify and item.verify.asset_names else None)
        metadata = _metadata_hits(
            schema_by_header,
            {
                "\U0001f511 Memo ID": row_memo_id,
                "Run ID": run_id,
                "Source Filename": record.file_name,
                "Extraction Date": now.date().isoformat(),
                "Valuation Date": as_of.isoformat() if as_of else None,
                "Reporting Period": reporting,
                "Fund Manager": item.client,
                "Portfolio Company": asset_name or resolved_asset or item.deal,
            },
            populated,
        )
        all_hits = [*metadata, *hits]
        _attach_evidence_source(all_hits, source_id=memo_id, source_file=record.file_path)

        memo_level = list(memo.memo_flags) if index == 1 else []
        asset = AssetExtraction(
            asset_name=asset_name or resolved_asset,
            row_memo_id=row_memo_id,
            hits=all_hits,
            flags=list(extraction_flags),
        )
        finalize_asset_after_assistance(
            asset,
            config=config,
            schema_by_header=schema_by_header,
            ruleset=ruleset,
            routing_table=routing,
            as_of_date=as_of,
            verify=item.verify,
            prior_row=_prior_row_for_hits(writer, all_hits, as_of),
            client=item.client,
            extra_flags=memo_level,
        )
        memo.assets.append(asset)

    memo.escalation = _build_escalation(
        memo_id, memo.assets, schema_by_header,
        memo.page_band_map, config.extraction.confidence_threshold,
        force_assist=force_assist, derived_headers=derived_headers,
    )
    if memo.escalation.fields:
        log_event(
            logger, "escalation plan built", memo_id=memo_id,
            fields=len(memo.escalation.fields),
            force_assist=force_assist,
        )
    return memo


def _existing_row_memo_ids(writer: WorkbookWriter) -> set[str]:
    sheet = writer.workbook["Index"]
    ids: set[str] = set()
    for row in range(4, sheet.max_row + 1):  # Memo ID is column 1, data starts row 4
        value = sheet.cell(row=row, column=1).value
        if value:
            ids.add(str(value))
    return ids


def _write_memo(writer: WorkbookWriter, memo: MemoResult) -> int:
    existing = _existing_row_memo_ids(writer)
    rows = 0
    for asset in memo.assets:
        if asset.row_memo_id in existing:
            continue  # cumulative re-run: row already present (idempotent)
        writer.append_index_row(asset.hits)
        rows += 1
    return rows


def _write_flags(writer: WorkbookWriter, memo: MemoResult) -> int:
    added = 0
    for asset in memo.assets:
        values = {hit.field: hit.value for hit in asset.hits}
        added += writer.append_review_flags(
            run_id=memo.run_id,
            memo_id=asset.row_memo_id,
            source_filename=memo.file_name,
            fund_manager=str(values.get("Fund Manager") or memo.client),
            portfolio_company=str(values.get("Portfolio Company") or memo.deal),
            valuation_date=memo.as_of_date,
            qa_status=asset.qa_status,
            flags=asset.flags,
        )
    return added
