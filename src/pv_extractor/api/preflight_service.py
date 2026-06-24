"""Preflight cost ESTIMATE for the New Run wizard (step d).

After the dry-run job finishes, the wizard asks for an estimate computed
SERVER-SIDE from page counts x the configured model pricing — the same
estimate_usage/cost_usd machinery the Phase-3 cost ledger uses, with two
pre-run heuristics (documented below) because the deterministic pass has
not run yet. Everything here is labeled ESTIMATED; actual costs replace
estimates in the ledger once the run executes."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pymupdf
from pydantic import BaseModel, Field

from pv_extractor.api.jobs import JobManager
from pv_extractor.config import Config
from pv_extractor.io_guard import open_read
from pv_extractor.llm.costs import estimate_usage
from pv_extractor.llm.model_registry import ExtractionPlanMetrics, ModelRegistry

# Pre-run heuristics: payload text volume per page and how many fields a
# typical memo escalates. Both only exist before extraction has run; the
# in-run estimator uses the real prompt/payload sizes instead.
_CHARS_PER_PAGE = 2800
_ASSUMED_ESCALATED_FIELDS = 80
# Under force_llm_assist the LLM is the primary extractor: most empty
# extractable fields escalate, so the per-memo schema/output is much larger.
_ASSUMED_ESCALATED_FIELDS_FORCE_ASSIST = 300


class MemoEstimate(BaseModel):
    client: str
    deal: str
    file_name: str | None = None
    page_count: int | None = None  # None = non-PDF or unreadable (cap assumed)
    payload_pages: int = 0
    first_tier: str = ""
    first_tier_usd: float = 0.0
    ladder_usd: float = 0.0  # worst case: every tier in the ladder runs
    documents: int = 1
    pages: int = 0
    image_pages: int = 0
    fields: int = 0
    estimated_input_tokens: int = 0
    provider: str = ""
    model: str = ""
    effort: str = ""
    execution_shape: str = ""
    expected_primary_calls: int = 0
    repair_policy: str = "never"
    max_repair_calls: int = 0
    reason: str = ""


class PreflightEstimate(BaseModel):
    label: str = "ESTIMATED"
    mode: str = ""
    found: int = 0
    estimated_total_usd: float = 0.0
    estimated_worst_case_usd: float = 0.0
    budget_usd: float = 0.0
    over_budget: bool = False
    memos: list[MemoEstimate] = Field(default_factory=list)
    assumptions: dict = Field(default_factory=dict)


_PAGE_COUNT_CACHE: "dict[tuple, int | None]" = {}
_PAGE_COUNT_LOCK = threading.Lock()


def _pdf_page_count(file_path: str) -> int | None:
    """Total page count of a PDF, memoized by file identity so the selection
    table and preflight don't reopen the same file once per slot per reload."""
    if not file_path.lower().endswith(".pdf"):
        return None
    key = None
    try:
        st = os.stat(file_path)
        key = (file_path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None:
        with _PAGE_COUNT_LOCK:
            if key in _PAGE_COUNT_CACHE:
                return _PAGE_COUNT_CACHE[key]
    try:
        with open_read(file_path) as fh:
            data = fh.read()
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            count: int | None = doc.page_count
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 — preflight estimates degrade, never fail
        return None
    if key is not None:
        with _PAGE_COUNT_LOCK:
            _PAGE_COUNT_CACHE[key] = count
    return count


def estimate_from_dry_run(
    manager: JobManager,
    job_id: str,
    config: Config,
    *,
    mode: str | None = None,
    manual_model: str | None = None,
    manual_effort: str | None = None,
    budget_usd: float | None = None,
    force_assist: bool = False,
    routing_mode: str | None = None,
    repair_policy: str | None = None,
) -> PreflightEstimate:
    """Estimate from the dry-run job's verify events (which carry the
    winner file paths). force_assist reflects the LLM-as-primary-extractor mode:
    many more fields escalate per memo, so the assumed field count is larger."""
    job = manager.get(job_id)
    if job is None:
        raise ValueError(f"unknown job {job_id!r}")
    assumed_fields = (
        _ASSUMED_ESCALATED_FIELDS_FORCE_ASSIST if force_assist else _ASSUMED_ESCALATED_FIELDS
    )

    registry = ModelRegistry.load(config.llm.models_path)
    resolved_mode = routing_mode or mode or ("single_model" if manual_model else config.llm.routing_mode)
    if resolved_mode == "manual":
        resolved_mode = "single_model"
    provider = config.llm.provider

    estimate = PreflightEstimate(
        mode=resolved_mode,
        budget_usd=budget_usd if budget_usd is not None else config.llm.budget_usd,
        assumptions={
            "chars_per_page": _CHARS_PER_PAGE,
            "assumed_escalated_fields": assumed_fields,
            "force_llm_assist": force_assist,
            "max_pages_per_memo": config.llm.max_pages_per_memo,
            "provider": provider,
            "pricing_source": str(config.llm.models_path),
            "repair_policy": repair_policy or config.llm.candidate_arbitration.repair_policy,
        },
    )

    seen: set[tuple[str, str]] = set()
    for event in manager.events_since(job_id, 0, limit=10000):
        if event.type != "stage" or event.payload.get("stage") != "verify":
            continue
        if event.payload.get("status") != "FOUND" or not event.payload.get("file_path"):
            continue
        key = (event.payload.get("client", ""), event.payload.get("deal", ""))
        if key in seen:
            continue
        seen.add(key)
        file_path = event.payload["file_path"]
        page_count = _pdf_page_count(file_path)
        payload_pages = min(
            page_count if page_count is not None else config.llm.max_pages_per_memo,
            config.llm.max_pages_per_memo,
        )
        usage = estimate_usage(
            prompt_chars=payload_pages * _CHARS_PER_PAGE,
            image_count=0,
            field_count=assumed_fields,
            cfg=config.llm,
        )
        metrics = ExtractionPlanMetrics(
            estimated_input_tokens=usage.input_tokens,
            documents=1,
            image_pages=0,
            fields=assumed_fields,
        )
        plan = registry.resolve_extraction_plan(
            routing_mode=resolved_mode,
            metrics=metrics,
            auto=config.llm.auto,
            provider=provider,
            manual_model=manual_model or config.llm.single_model_model,
            manual_effort=manual_effort or config.llm.single_model_effort,
            provider_default_model=config.codex_cli.model,
            provider_default_effort=config.codex_cli.reasoning_effort,
            repair_policy=repair_policy or config.llm.candidate_arbitration.repair_policy,
            max_repair_calls_per_deal=config.llm.candidate_arbitration.max_repair_calls_per_deal,
            allow_fable=config.llm.allow_fable,
        )
        cost = registry.cost_usd(usage, plan.selection.entry) or 0.0
        memo = MemoEstimate(
            client=key[0], deal=key[1],
            file_name=event.payload.get("file_name"),
            page_count=page_count, payload_pages=payload_pages,
            first_tier=f"{plan.model}/{plan.effort}",
            first_tier_usd=round(cost, 4),
            ladder_usd=round(cost, 4),
            pages=page_count or payload_pages,
            fields=assumed_fields,
            estimated_input_tokens=usage.input_tokens,
            provider=plan.provider,
            model=plan.model,
            effort=plan.effort,
            execution_shape=plan.execution_shape,
            expected_primary_calls=plan.expected_primary_calls,
            repair_policy=plan.repair_policy,
            max_repair_calls=plan.max_repair_calls,
            reason=plan.reason,
        )
        estimate.memos.append(memo)

    estimate.found = len(estimate.memos)
    estimate.estimated_total_usd = round(sum(m.first_tier_usd for m in estimate.memos), 4)
    estimate.estimated_worst_case_usd = round(sum(m.ladder_usd for m in estimate.memos), 4)
    estimate.over_budget = estimate.estimated_total_usd > estimate.budget_usd
    return estimate
