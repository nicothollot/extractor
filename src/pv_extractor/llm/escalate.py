"""Escalation executor (D4 + D5): consumes each memo's EscalationPlan,
assembles the page payload, runs hidden local provider sessions through the
worker queue, quote-grounds the answers and merges them into the row.

Cost principles enforced here:
  * LOCAL FIRST — only fields the deterministic pass scored below threshold
    (or left empty while required) ever reach the provider CLI (the plan was
    built that way in run.py).
  * BOUNDED TASKS — escalated fields are packed into provider-neutral
    AssistanceTask objects with hard field/page/prompt/output caps; a failed
    sibling task does not discard successful merges.
  * SESSION WORKER QUEUE — a small thread pool (config.llm.workers, default
    1-2) launches hidden provider processes; stdout/exit/usage/session ids are
    captured per attempt.
  * HARD BUDGET — projected spend is reserved before any call; once the cap
    would be passed the memo is marked LLM_DEFERRED and the run finishes
    cleanly.
  * RESULT CACHE — responses are cached per provider/model/effort, schema and
    prompt version, selected page hashes, and requested sparse field keys.

Merge policy: an LLM value NEVER overwrites a deterministic value
with confidence >= threshold. It fills empty fields, or replaces a below-
threshold deterministic value (the old value is preserved as a conflict
entry). Every merge, overwrite and rejection lands in the plan's merge_log
inside the audit record; ungrounded quotes discard the value and raise
UNGROUNDED_LLM_VALUE. Fields that survive every tier are flagged
NOT_EXTRACTABLE (required) / LLM_UNCONFIRMED (low-confidence) with
reviewer_attention — values are never invented.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dataclass_field, replace
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.extract.targeting import _page_score, build_band_lexicons
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.llm import cache as llm_cache
from pv_extractor.llm.arbitration import Candidate, arbitrate_field
from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.codex_cli_client import CodexCliClient
from pv_extractor.llm.costs import (
    LEDGER_FILENAME,
    BudgetExceeded,
    BudgetTracker,
    CostLedger,
    estimate_usage,
)
from pv_extractor.llm.model_registry import ModelRegistry, ModelSelection
from pv_extractor.llm.payload import (
    MemoPayload,
    PayloadError,
    assemble_deal_payload,
    assemble_payload,
)
from pv_extractor.llm.grounding import GroundingResult, ground_quote
from pv_extractor.llm.planner import AssistanceTask, plan_assistance_tasks
from pv_extractor.llm.response_validation import StructuredResponseError
from pv_extractor.llm.schema_builder import (
    build_legacy_call_prompt,
    build_legacy_response_schema,
    build_legacy_static_prompt,
    build_call_prompt,
    build_response_schema,
    build_static_prompt,
    decode_structured_response,
    response_key_map,
    schema_json_bytes,
    sparse_field_keys,
    sorted_fields,
)
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    AssetExtraction,
    ConflictingCandidate,
    EscalationField,
    FieldHit,
    FlagSeverity,
    LlmAttempt,
    MemoResult,
    ReviewFlag,
    SchemaField,
)
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)

_NUMERIC_DTYPES = {"number", "percent", "basis_points", "multiple_x", "years", "integer"}


@dataclass
class LlmSettings:
    """Resolved llm runtime settings (config defaults + CLI overrides)."""

    enabled: bool
    mode: str  # auto | per_deal | single_model | legacy manual
    manual_model: str
    manual_effort: str
    allow_fable: bool
    budget_usd: float
    workers: int
    force: bool = False
    # force_assist: escalate EVERY empty extractable field (not just low-conf /
    # required ones) so the LLM does the extraction instead of trusting the
    # deterministic engine. Implies a deterministic-cache bypass in run() so the
    # broad plan is actually rebuilt. enabled must still be True.
    force_assist: bool = False


def resolve_settings(
    config: Config,
    *,
    no_llm: bool = False,
    mode: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    budget: float | None = None,
    force: bool = False,
    force_assist: bool = False,
    allow_fable: bool | None = None,
) -> LlmSettings:
    """--llm-model without --llm-mode implies single_model (the user forced one
    model); everything else falls back to config.llm. force_assist turns the
    LLM into the primary extractor for this run (see LlmSettings)."""
    llm = config.llm
    resolved_mode = mode or ("single_model" if model else llm.routing_mode)
    if resolved_mode == "manual":
        resolved_mode = "single_model"
    return LlmSettings(
        enabled=llm.enabled and not no_llm,
        mode=resolved_mode,
        manual_model=model or llm.single_model_model or llm.manual_model,
        manual_effort=effort or llm.single_model_effort or llm.manual_effort,
        allow_fable=llm.allow_fable if allow_fable is None else allow_fable,
        budget_usd=budget if budget is not None else llm.budget_usd,
        workers=max(1, llm.workers),
        force=force,
        force_assist=force_assist,
    )


@dataclass
class LlmRunSummary:
    enabled: bool = False
    executed: bool = False
    memos_escalated: int = 0
    memos_deferred: int = 0
    memos_failed: int = 0
    attempts: int = 0
    cache_hits: int = 0
    total_cost_usd: float = 0.0
    any_actual_costs: bool = False
    session_labels: list[str] = dataclass_field(default_factory=list)
    ledger_path: Path | None = None
    detail: str = ""
    diagnostics: dict[str, object] = dataclass_field(default_factory=dict)


# ---------------------------------------------------------------------------
# quote grounding (rule 5)
# ---------------------------------------------------------------------------


def quote_grounding(
    quote: str, page: int | None, payload: MemoPayload, fuzzy_threshold: int
) -> str:
    """Backward-compatible status wrapper around structured grounding."""
    return ground_quote(quote, page, payload, fuzzy_threshold).status


# ---------------------------------------------------------------------------
# value coercion
# ---------------------------------------------------------------------------


def _coerce_value(value, schema_field: SchemaField):
    """(ok, coerced). Light typing only — full range/vocab validation already
    ran in Phase 2 and the merge must never invent or repair values."""
    if schema_field.dtype in _NUMERIC_DTYPES:
        if isinstance(value, bool):
            return False, None
        if isinstance(value, (int, float)):
            number = value
        elif isinstance(value, str):
            text = value.replace(",", "").replace("%", "").replace("x", "").strip()
            if text.startswith("(") and text.endswith(")"):
                text = "-" + text[1:-1]
            try:
                number = float(text)
            except ValueError:
                return False, None
        else:
            return False, None
        if schema_field.dtype == "integer":
            return True, int(number)
        return True, float(number)
    if schema_field.dtype == "boolean":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in ("yes", "no", "true", "false"):
            return True, value.strip().lower() in ("yes", "true")
        return False, None
    if schema_field.dtype == "date":
        if isinstance(value, str) and len(value.strip()) >= 8:
            return True, value.strip()
        return False, None
    if isinstance(value, (str, int, float, bool)):
        return True, value
    return False, None


def _match_vocab(value, vocab: list[str]):
    """(ok, canonical). Exact case-insensitive only — fuzzy vocab repair is
    the deterministic layer's job; an off-vocabulary LLM answer is rejected."""
    text = str(value).strip().lower()
    for allowed in vocab:
        if text == allowed.strip().lower():
            return True, allowed
    return False, None


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def _qualifying_assets(
    memo: MemoResult, header: str, threshold: float
) -> list[tuple[AssetExtraction, FieldHit | None]]:
    """Assets where an LLM value may land: the field is missing,
    empty, or a below-threshold deterministic hit. Confident deterministic /
    computed / metadata values are untouchable."""
    out: list[tuple[AssetExtraction, FieldHit | None]] = []
    for asset in memo.assets:
        existing = next((h for h in asset.hits if h.field == header), None)
        if existing is None or existing.value is None:
            out.append((asset, existing))
        elif existing.method == "deterministic" and existing.confidence < threshold:
            out.append((asset, existing))
    return out


def _merge_field(
    memo: MemoResult,
    schema_field: SchemaField,
    result: dict,
    selection: ModelSelection,
    payload: MemoPayload,
    config: Config,
) -> str:
    """Apply one field's LLM answer. Returns the disposition recorded in the
    merge log: merged | not_found | rejected:<reason>."""
    plan = memo.escalation
    assert plan is not None
    header = schema_field.header
    method = f"llm:{selection.entry.provider}:{selection.entry.alias}:{selection.effort}"

    if result.get("conflict_candidates"):
        candidates: list[Candidate] = []
        for raw_candidate in result.get("conflict_candidates") or []:
            if not isinstance(raw_candidate, dict):
                continue
            candidate_quote = str(
                raw_candidate.get("verbatim_quote")
                or raw_candidate.get("quote")
                or raw_candidate.get("evidence_quote")
                or ""
            )
            candidate_page = (
                raw_candidate.get("page") if isinstance(raw_candidate.get("page"), int) else None
            )
            candidate_grounding = ground_quote(
                candidate_quote, candidate_page, payload, config.llm.quote_match_threshold
            )
            candidates.append(
                Candidate.from_result(
                    header,
                    {**raw_candidate, "quote": candidate_quote},
                    provider=selection.entry.provider,
                    model=selection.entry.alias,
                    effort=selection.effort,
                    prompt_version=config.llm.planner.prompt_version,
                    grounding_status=candidate_grounding.status,
                    source="primary",
                )
            )
        decision = arbitrate_field(
            header,
            candidates,
            config=config.llm.candidate_arbitration,
            schema_field=schema_field,
            target_as_of=memo.as_of_date,
            document_ids=None,
        )
        plan.diagnostics.setdefault("confidence_decisions", {})[header] = {
            "selected_candidate_id": decision.selected_candidate_id,
            "raw_model_confidence": decision.raw_model_confidence,
            "calibrated_confidence": decision.calibrated_confidence,
            "agreement_bonus": decision.agreement_bonus,
            "final_confidence": decision.final_confidence,
            "runner_up_confidence": decision.runner_up_confidence,
            "decision": decision.decision,
            "decision_reason": decision.decision_reason,
        }
        if decision.selected is None:
            _flag_first_asset(
                memo,
                ReviewFlag(
                    category="llm",
                    description=f"UNRESOLVED_LLM_CONFLICT: {header} — {decision.decision_reason}",
                    severity=FlagSeverity.warning,
                    reviewer_attention=True,
                    field=header,
                ),
            )
            plan.merge_log.append(f"{header}: unresolved conflict — {decision.decision_reason}")
            return "rejected:unresolved_conflict"
        selected = decision.selected
        return _merge_field(
            memo,
            schema_field,
            {
                "value": selected.value,
                "unit": selected.unit,
                "page": selected.page,
                "document_id": selected.document_id,
                "as_of_date": selected.as_of_date,
                "verbatim_quote": selected.quote,
                "quote": selected.quote,
                "evidence_kind": selected.evidence_kind,
                "confidence": decision.final_confidence,
                "model_confidence": selected.raw_model_confidence,
                "raw_model_confidence": selected.raw_model_confidence,
            },
            selection,
            payload,
            config,
        )

    if result.get("not_found") is True or result.get("value") is None:
        return "not_found"

    ok, value = _coerce_value(result.get("value"), schema_field)
    if not ok:
        _flag_first_asset(
            memo,
            ReviewFlag(
                category="llm",
                description=f"{header}: LLM value {result.get('value')!r} failed "
                f"{schema_field.dtype} typing — discarded",
                severity=FlagSeverity.warning, field=header,
            ),
        )
        return "rejected:type_mismatch"

    if schema_field.controlled_vocab:
        ok, value = _match_vocab(value, schema_field.controlled_vocab)
        if not ok:
            _flag_first_asset(
                memo,
                ReviewFlag(
                    category="llm",
                    description=f"{header}: LLM value outside controlled vocabulary — discarded",
                    severity=FlagSeverity.warning, field=header,
                ),
            )
            return "rejected:vocab"

    quote = str(result.get("verbatim_quote") or result.get("evidence_quote") or "")
    page = result.get("page") if isinstance(result.get("page"), int) else None
    grounding = ground_quote(quote, page, payload, config.llm.quote_match_threshold)
    if grounding.status == "ungrounded" and not config.llm.surface_ungrounded_values:
        _flag_first_asset(
            memo,
            ReviewFlag(
                category="llm",
                description=f"UNGROUNDED_LLM_VALUE: {header} — quote not found on cited "
                f"page {page}; value discarded (score {grounding.score:.2f})",
                severity=FlagSeverity.warning, reviewer_attention=True, field=header,
            ),
        )
        return "rejected:ungrounded"
    # surface_ungrounded_values (default): fall through and WRITE the value as a
    # low-confidence, flagged hit so the analyst can review it (the flag is added
    # below, after the hit is built).

    targets = _qualifying_assets(memo, header, plan.confidence_threshold)
    if not targets:
        return "rejected:protected"  # deterministic value is confident — never overwrite

    asset, existing = targets[0]
    raw_confidence = result.get(
        "model_confidence",
        result.get("raw_model_confidence", result.get("confidence")),
    )
    if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        confidence = max(0.0, min(1.0, float(raw_confidence)))
    else:
        confidence = config.llm.confidence_scores.get(
            str(raw_confidence),
            config.llm.confidence_scores.get(str(result.get("confidence_label")), 0.35),
        )
    decision = arbitrate_field(
        header,
        [
            Candidate.from_result(
                header,
                {
                    **result,
                    "value": value,
                    "model_confidence": confidence,
                    "quote": quote,
                    "document_id": result.get("document_id") or "D01",
                },
                provider=selection.entry.provider,
                model=selection.entry.alias,
                effort=selection.effort,
                prompt_version=config.llm.planner.prompt_version,
                grounding_status=grounding.status,
                source="primary",
            )
        ],
        config=config.llm.candidate_arbitration,
        schema_field=schema_field,
        target_as_of=memo.as_of_date,
        document_ids=None,
    )
    plan.diagnostics.setdefault("confidence_decisions", {})[header] = {
        "selected_candidate_id": decision.selected_candidate_id,
        "raw_model_confidence": decision.raw_model_confidence,
        "calibrated_confidence": decision.calibrated_confidence,
        "agreement_bonus": decision.agreement_bonus,
        "final_confidence": decision.final_confidence,
        "runner_up_confidence": decision.runner_up_confidence,
        "decision": decision.decision,
        "decision_reason": decision.decision_reason,
    }
    if decision.selected is None:
        _flag_first_asset(
            memo,
            ReviewFlag(
                category="llm",
                description=f"LLM_UNRESOLVED: {header} — {decision.decision_reason}",
                severity=FlagSeverity.warning,
                reviewer_attention=True,
                field=header,
            ),
        )
        plan.merge_log.append(f"{header}: {decision.decision} — {decision.decision_reason}")
        return "rejected:arbitration"
    confidence = decision.final_confidence
    if grounding.status == "ungrounded":  # shown for review, not machine-verified -> low confidence
        confidence = min(confidence, config.llm.ungrounded_confidence_cap)
        if existing is not None and existing.value is not None:
            # An unverified value must NEVER overwrite an existing (even
            # below-threshold) deterministic value — keep the real one and show
            # the LLM's reading as a conflict + flag for the analyst to compare.
            existing.conflicts = [
                *existing.conflicts,
                ConflictingCandidate(
                    raw_text=str(result.get("value")), value=value, page=page,
                    confidence=confidence, evidence=quote[: config.extraction.max_evidence_chars],
                    evidence_ref=grounding.evidence_ref,
                ),
            ]
            asset.flags.append(
                ReviewFlag(
                    category="llm",
                    description=f"UNGROUNDED_LLM_VALUE: {header} — LLM read {value} but the "
                    f"quote was not matched on page {page} (score {grounding.score:.2f}); kept deterministic value "
                    f"{existing.value}, review both",
                    severity=FlagSeverity.warning, reviewer_attention=True, field=header,
                )
            )
            plan.merge_log.append(
                f"{header}: ungrounded LLM value {value} kept as conflict; deterministic retained"
            )
            return "merged"
    evidence_ref = (
        grounding.evidence_ref.model_copy(
            update={
                "provider": selection.entry.provider,
                "extraction_method": method,
            }
        )
        if grounding.evidence_ref is not None
        else None
    )
    hit = FieldHit(
        field=header, col_index=schema_field.col_index, band=schema_field.band,
        raw_text=str(result.get("value")), value=value,
        unit=result.get("unit") or schema_field.unit, page=page,
        bbox=grounding.bbox,
        method=method, confidence=confidence,
        evidence=quote[: config.extraction.max_evidence_chars],
        evidence_ref=evidence_ref,
        confidence_components={
            "llm_self_reported": raw_confidence if isinstance(raw_confidence, (int, float)) else confidence,
            "raw_model_confidence": decision.raw_model_confidence,
            "calibrated_confidence": decision.calibrated_confidence,
            "agreement_bonus": decision.agreement_bonus,
            "final_confidence": decision.final_confidence,
            "runner_up_confidence": decision.runner_up_confidence,
            "grounding_score": round(grounding.score, 4),
            "grounded": 1.0 if grounding.status == "grounded" else 0.0,
        },
    )
    if existing is not None:
        if existing.value is not None:
            hit.conflicts = [
                *existing.conflicts,
                ConflictingCandidate(
                    raw_text=existing.raw_text, value=existing.value, page=existing.page,
                    confidence=existing.confidence, evidence=existing.evidence,
                    evidence_ref=existing.evidence_ref,
                ),
            ]
            plan.merge_log.append(
                f"{header}: overwrote below-threshold deterministic value "
                f"(conf {existing.confidence:.2f}) with {method} (conf {confidence:.2f}) "
                f"[{asset.row_memo_id}]"
            )
            asset.flags.append(
                ReviewFlag(
                    category="llm",
                    description=f"{header}: low-confidence deterministic value replaced by "
                    f"{method}; prior value kept as conflict",
                    severity=FlagSeverity.info, field=header,
                )
            )
        else:
            plan.merge_log.append(f"{header}: filled empty field via {method} [{asset.row_memo_id}]")
        asset.hits[asset.hits.index(existing)] = hit
    else:
        plan.merge_log.append(f"{header}: filled missing field via {method} [{asset.row_memo_id}]")
        asset.hits.append(hit)

    if grounding.status == "ungrounded":
        asset.flags.append(
            ReviewFlag(
                category="llm",
                description=f"UNGROUNDED_LLM_VALUE: {header}={value} — quote not found on cited "
                f"page {page} (score {grounding.score:.2f}; likely OCR/scan mismatch); value shown for review, NOT "
                f"machine-verified",
                severity=FlagSeverity.warning, reviewer_attention=True, field=header,
            )
        )
    elif grounding.status == "unverifiable":
        asset.flags.append(
            ReviewFlag(
                category="llm",
                description=f"{header}: quote could not be machine-verified "
                f"(no local text for page {page}) — review against the document",
                severity=FlagSeverity.warning, reviewer_attention=True, field=header,
            )
        )
    if len(targets) > 1:
        asset.flags.append(
            ReviewFlag(
                category="llm",
                description=f"{header}: multi-asset memo — LLM value applied to "
                f"{asset.row_memo_id} only; review remaining assets manually",
                severity=FlagSeverity.warning, reviewer_attention=True, field=header,
            )
        )
    plan.merged_fields.append(header)
    return "merged"


def _flag_first_asset(memo: MemoResult, flag: ReviewFlag) -> None:
    if memo.assets:
        memo.assets[0].flags.append(flag)
    else:
        memo.memo_flags.append(flag)


def _flag_task_failure(
    memo: MemoResult,
    task: AssistanceTask,
    error: str,
    *,
    timeout: bool = False,
) -> None:
    code = "LLM_TASK_TIMEOUT" if timeout else "LLM_TASK_FAILED"
    fields = task.record.requested_fields
    shown = ", ".join(fields[:8])
    if len(fields) > 8:
        shown += f", +{len(fields) - 8} more"
    _flag_first_asset(
        memo,
        ReviewFlag(
            category="llm",
            description=(
                f"{code}: task {task.record.task_id} wave {task.record.wave} "
                f"({len(fields)} field(s): {shown}) — {error}"
            ),
            severity=FlagSeverity.warning,
            reviewer_attention=True,
            field=fields[0] if len(fields) == 1 else None,
        ),
    )


# ---------------------------------------------------------------------------
# band batching: focused, relevance-ordered calls (one band's pages + schema)
# ---------------------------------------------------------------------------


@dataclass
class _FieldGroup:
    """One unit of LLM work: a band's escalated fields, scored for how strongly
    the document shows evidence for that band, with the pages that band needs."""

    label: str
    fields: list[EscalationField]
    pages: list[int]
    relevance: float
    has_evidence: bool  # page-anchor evidence OR an image page (can't text-score)
    ocr_hostile: bool
    priority: bool  # full tier ladder vs a single cheap sweep pass
    must_try: bool  # contains a required-empty / below-confidence field

    def sort_key(self) -> tuple:
        # Required/low-confidence bands first, then by descending evidence, then
        # by label for determinism.
        return (0 if self.must_try else 1, -self.relevance, self.label)


_ALWAYS_TRY_REASONS = frozenset({"below_confidence", "required_empty"})


def _band_relevance(
    band: str,
    pages: list[int],
    payload: MemoPayload,
    lexicons: dict[str, list[str]],
) -> float:
    """Sum the band's page-anchor score over its candidate pages' local text
    (reuses the Phase-2 targeting scorer). Image pages contribute no text but
    are handled separately as evidence."""
    anchors = lexicons.get(band)
    if not anchors:
        return 0.0
    total = 0.0
    for number in pages:
        text = payload.page_texts.get(number)
        if text:
            total += _page_score(f" {normalize_text(text)} ", anchors)
    return total


def _chunk(fields: list[EscalationField], size: int) -> list[list[EscalationField]]:
    if size <= 0 or len(fields) <= size:
        return [fields]
    return [fields[i : i + size] for i in range(0, len(fields), size)]


def _build_groups(
    plan_fields: list[EscalationField],
    payload: MemoPayload,
    schema_by_header: dict[str, SchemaField],
    config: Config,
) -> list[_FieldGroup]:
    """Split a plan's unique fields into LLM work groups.

    band_batched=True: one priority group per band that has page evidence (or a
    required/low-confidence field), ordered by relevance, plus ONE cheap sweep
    group for the bands the document shows no evidence for. band_batched=False:
    a single group over the whole payload (one-call-per-memo behavior).

    SMALL-DOC COLLAPSE: band-batching (many focused calls, each re-uploading the
    page payload) only pays off on large memos. When the payload is at most
    `llm.single_call_max_pages` pages, we collapse to calls over the whole
    document + all fields regardless of band_batched — far cheaper and faster for
    short client memos, and the model sees the whole doc at once. The field set is
    still chunked by `max_fields_per_call`: the response schema is passed INLINE
    on the `claude` command line, and Windows caps a command line at ~32 KB, so a
    one-shot 200-field schema (~40 KB) fails to launch ([WinError 206]). Each
    chunk shares the same (small) page set, so this stays cheap. Set
    single_call_max_pages=0 to disable the collapse and always honor band_batched."""
    # De-duplicate (the plan repeats a field per asset; the call is per memo).
    unique: dict[str, EscalationField] = {}
    for escalated in plan_fields:
        if escalated.field in schema_by_header:
            unique.setdefault(escalated.field, escalated)
    fields = list(unique.values())
    payload_pages = set(payload.page_blocks)

    max_single = max(0, config.llm.single_call_max_pages)
    small_doc = max_single > 0 and len(payload_pages) <= max_single
    if not config.llm.band_batched or small_doc:
        all_pages = sorted(payload_pages)
        # Chunk by max_fields_per_call so the INLINE --json-schema arg stays under
        # the Windows ~32 KB command-line limit (a 200-field one-shot schema is
        # ~40 KB and fails with [WinError 206]). All chunks share the same pages.
        chunks = _chunk(fields, config.llm.max_fields_per_call) if fields else []
        if small_doc and config.llm.band_batched:
            log_event(
                logger, "single-call collapse (small doc)",
                pages=len(payload_pages), fields=len(fields),
                threshold=max_single, chunks=len(chunks),
            )
        return [
            _FieldGroup(
                label="memo" if len(chunks) == 1 else f"memo {i + 1}/{len(chunks)}",
                fields=chunk, pages=all_pages, relevance=1.0,
                has_evidence=True, ocr_hostile=payload.ocr_hostile,
                priority=True, must_try=True,
            )
            for i, chunk in enumerate(chunks)
        ]

    lexicons = build_band_lexicons(list(schema_by_header.values()), config.extraction)
    summary = sorted(p for p in payload_pages if p <= config.extraction.summary_pages)

    by_band: dict[str, list[EscalationField]] = {}
    for escalated in fields:
        by_band.setdefault(escalated.band, []).append(escalated)

    # One UNCHUNKED unit per band, tagged priority (page evidence / required) or
    # sweep (no evidence). Adaptive packing merges these by page-locality; the
    # legacy path chunks each band into its own call(s).
    priority_units: list[_FieldGroup] = []
    sweep_fields: list[EscalationField] = []
    for band, band_fields in by_band.items():
        band_pages = sorted(
            {p for f in band_fields for p in f.candidate_pages if p in payload_pages}
            | set(summary)
        )
        relevance = _band_relevance(band, band_pages, payload, lexicons)
        has_image = payload.scoped_image_count(band_pages) > 0
        must_try = any(f.reason in _ALWAYS_TRY_REASONS for f in band_fields)
        has_evidence = relevance > config.llm.band_relevance_floor or has_image
        if must_try or has_evidence:
            priority_units.append(
                _FieldGroup(
                    label=band, fields=list(band_fields), pages=band_pages, relevance=relevance,
                    has_evidence=has_evidence, ocr_hostile=has_image,
                    priority=True, must_try=must_try,
                )
            )
        else:
            sweep_fields.extend(band_fields)

    sweep_pages = summary or sorted(payload_pages)[: config.extraction.summary_pages]
    sweep_unit = (
        _FieldGroup(
            label="no-evidence sweep", fields=list(sweep_fields), pages=sweep_pages,
            relevance=0.0, has_evidence=False,
            ocr_hostile=payload.scoped_image_count(sweep_pages) > 0,
            priority=False, must_try=False,
        )
        if sweep_fields else None
    )

    if config.llm.adaptive_batching:
        # Merge bands that target the SAME pages, then greedily combine page-sets
        # whose union stays small — so a small document becomes a few calls, not
        # one per band, and each page-set is read once.
        units = [*priority_units, *([sweep_unit] if sweep_unit else [])]
        return _pack_by_pages(units, config)

    # Legacy: one call per band (chunked), priority-ordered, then the sweep.
    groups: list[_FieldGroup] = []
    for unit in sorted(priority_units, key=lambda g: g.sort_key()):
        for chunk in _chunk(unit.fields, config.llm.max_fields_per_call):
            groups.append(replace(unit, fields=chunk))
    if sweep_unit:
        for chunk in _chunk(sweep_unit.fields, config.llm.max_fields_per_call):
            groups.append(replace(sweep_unit, fields=chunk))
    return groups


def _single_group(
    fields: list[EscalationField], payload: MemoPayload, config: Config
) -> list[_FieldGroup]:
    """One work group over the WHOLE (combined) payload — every page, every
    field. Used by the one-call-per-deal path: the model sees the deal's entire
    document set at once. The field set is only ever split by max_fields_per_call
    (so the inline --json-schema arg stays under the Windows command-line limit);
    every chunk shares the same full page set, so this is at most a couple of
    calls, not the per-band fan-out."""
    all_pages = sorted(payload.page_blocks)
    chunks = _chunk(fields, config.llm.max_fields_per_call) if fields else []
    return [
        _FieldGroup(
            label="deal" if len(chunks) == 1 else f"deal {i + 1}/{len(chunks)}",
            fields=chunk, pages=all_pages, relevance=1.0,
            has_evidence=True, ocr_hostile=payload.ocr_hostile,
            priority=True, must_try=True,
        )
        for i, chunk in enumerate(chunks)
    ]


def _pages_label(pages: list[int]) -> str:
    if not pages:
        return "pages —"
    runs: list[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = p
    runs.append(f"{start}-{prev}" if start != prev else f"{start}")
    return "pages " + ",".join(runs)


def _merge_into(target: _FieldGroup, other: _FieldGroup, pages: list[int]) -> None:
    target.fields = target.fields + other.fields
    target.pages = pages
    target.relevance = max(target.relevance, other.relevance)
    target.must_try = target.must_try or other.must_try
    target.has_evidence = target.has_evidence or other.has_evidence
    target.ocr_hostile = target.ocr_hostile or other.ocr_hostile
    target.priority = target.priority or other.priority


def _pack_by_pages(units: list[_FieldGroup], config: Config) -> list[_FieldGroup]:
    """Pack per-band units into the fewest page-coherent calls: merge identical
    page-sets, then greedily combine sets whose union stays within
    adaptive_max_pages_per_call and max_fields_per_call. Over-cap groups chunk;
    final order is must-try / most-relevant first."""
    max_fields = config.llm.max_fields_per_call
    max_pages = max(1, config.llm.adaptive_max_pages_per_call)

    # 1) merge identical page-sets
    by_pageset: dict[tuple, _FieldGroup] = {}
    order: list[tuple] = []
    for unit in units:
        key = tuple(unit.pages)
        if key in by_pageset:
            _merge_into(by_pageset[key], unit, list(key))
        else:
            by_pageset[key] = replace(unit, fields=list(unit.fields), pages=list(unit.pages))
            order.append(key)
    merged = [by_pageset[k] for k in order]

    # 2) greedy cross-page-set merge (most-relevant first so it leads its call)
    merged.sort(key=lambda g: g.sort_key())
    packed: list[_FieldGroup] = []
    for group in merged:
        for host in packed:
            union = sorted(set(host.pages) | set(group.pages))
            if len(union) <= max_pages and len(host.fields) + len(group.fields) <= max_fields:
                _merge_into(host, group, union)
                break
        else:
            packed.append(group)

    # 3) chunk any over-cap group; label by the pages the call covers
    out: list[_FieldGroup] = []
    for group in packed:
        chunks = _chunk(group.fields, max_fields)
        for index, chunk in enumerate(chunks):
            suffix = f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            out.append(replace(group, fields=chunk, label=_pages_label(group.pages) + suffix))
    out.sort(key=lambda g: g.sort_key())
    return out


# ---------------------------------------------------------------------------
# one tier = one local provider call (rule 4)
# ---------------------------------------------------------------------------


def _run_tier(
    *,
    memo: MemoResult,
    payload: MemoPayload,
    task: AssistanceTask,
    selection: ModelSelection,
    tier: int,
    slug: str,
    config: Config,
    settings: LlmSettings,
    client,
    registry: ModelRegistry,
    budget: BudgetTracker,
    run_id: str,
    corrective: bool = False,
) -> tuple[LlmAttempt, dict | None]:
    fields = task.schema_fields
    pages = task.record.selected_pages
    job_id = f"pv-{run_id}-{memo.memo_id}-{slug}"
    schema_version = config.llm.planner.sparse_schema_version
    inferable = set(config.llm.planner.inferable_fields)
    static_prompt = build_static_prompt(fields, inferable_fields=inferable)
    schema = build_response_schema(fields)
    schema_path = payload.directory / f"schema_{slug}.json"
    with guarded_open_write(schema_path, config.pv_root, mode="wb") as fh:
        fh.write(schema_json_bytes(schema))
    with guarded_open_write(
        payload.directory / f"prompt_static_{slug}.txt", config.pv_root
    ) as fh:
        fh.write(static_prompt)
    prompt = build_call_prompt(
        fields,
        payload.scoped_prompt(pages),
        inferable_fields=inferable,
        corrective=corrective,
    )

    provider_name = getattr(client, "provider_name", selection.entry.provider)
    attempt = LlmAttempt(
        job_id=job_id, task_id=task.record.task_id, wave=task.record.wave,
        tier=tier, model_alias=selection.entry.alias,
        model_id=selection.entry.id, effort=selection.effort,
        provider=provider_name,
        schema_version=schema_version,
        prompt_version=config.llm.planner.prompt_version,
        selected_pages=list(pages),
        selected_page_count=len(pages),
        selected_image_count=task.record.image_count,
        estimated_prompt_chars=task.record.estimated_prompt_chars,
        estimated_prompt_tokens=task.record.estimated_prompt_tokens,
        estimated_output_tokens=task.record.estimated_output_tokens,
        fields_requested=len(fields),
    )
    image_paths = payload.scoped_image_paths(pages)
    if image_paths and hasattr(client, "capabilities"):
        caps = client.capabilities()
        if not caps.image_input:
            image_pages = payload.scoped_image_pages(pages)
            unsupported_pages = [
                p.number for p in image_pages if not (payload.page_texts.get(p.number) or "").strip()
            ]
            if unsupported_pages:
                attempt.error = (
                    "image input unsupported by provider CLI and no OCR/text is available "
                    f"for page(s): {', '.join(str(p) for p in unsupported_pages)}"
                )
                attempt.retry_status = "non_retryable"
                return attempt, None
            prompt = build_call_prompt(
                fields,
                payload.scoped_prompt_with_ocr_fallback(pages),
                inferable_fields=inferable,
                corrective=corrective,
            )
            image_paths = []

    key = llm_cache.task_cache_key(
        provider=provider_name,
        model_id=selection.entry.id or selection.entry.alias,
        effort=selection.effort,
        schema_version=schema_version,
        prompt_version=config.llm.planner.prompt_version,
        page_hashes=payload.selected_page_hashes(pages),
        field_keys=sparse_field_keys(fields),
    )

    if config.llm.cache_enabled and not settings.force:
        conn = sqlite3.connect(str(config.db_path), timeout=30)
        try:
            llm_cache.init_cache(conn)
            cached = llm_cache.get_cached(conn, key)
        finally:
            conn.close()
        if cached is not None:
            try:
                decode_structured_response(cached["structured"], fields)
            except StructuredResponseError as exc:
                plan_detail = f"cached structured output failed accounting validation: {exc}"
                log_event(logger, "llm cache entry ignored", task_id=task.record.task_id, error=plan_detail)
                cached = None
            if cached is not None:
                attempt.from_cache = True
                attempt.session_id = cached["session_id"]
                attempt.usage = cached["usage"] or attempt.usage
                attempt.cost_usd = 0.0  # no new spend on a cache hit
                attempt.cost_source = "cache"
                return attempt, cached["structured"]

    estimated = estimate_usage(
        prompt_chars=len(prompt), image_count=task.record.image_count,
        field_count=len(fields), cfg=config.llm,
    )
    estimated_cost_maybe = registry.cost_usd(estimated, selection.entry)
    estimated_cost = estimated_cost_maybe if estimated_cost_maybe is not None else 0.0
    budget.reserve(estimated_cost)  # BudgetExceeded propagates: memo deferred, no call made

    result = _call_provider(
        client=client, job_id=job_id, prompt=prompt, schema=schema,
        schema_path=schema_path, image_paths=image_paths,
        selection=selection, payload=payload, config=config,
    )
    if _should_fallback_legacy(result.error) and not result.ok:
        legacy_schema = build_legacy_response_schema(fields)
        legacy_schema_path = payload.directory / f"schema_{slug}_legacy.json"
        with guarded_open_write(legacy_schema_path, config.pv_root, mode="wb") as fh:
            fh.write(schema_json_bytes(legacy_schema))
        legacy_prompt = build_legacy_call_prompt(
            fields,
            payload.scoped_prompt_with_ocr_fallback(pages) if not image_paths else payload.scoped_prompt(pages),
            corrective=corrective,
        )
        result = _call_provider(
            client=client, job_id=f"{job_id}-legacy", prompt=legacy_prompt,
            schema=legacy_schema, schema_path=legacy_schema_path, image_paths=image_paths,
            selection=selection, payload=payload, config=config,
        )
        schema_version = 1
        attempt.schema_version = 1
        attempt.prompt_version = f"{config.llm.planner.prompt_version}:legacy"
    attempt.exit_code = result.exit_code
    attempt.duration_seconds = result.duration_seconds
    attempt.session_id = result.session_id
    attempt.usage = result.usage or estimated
    if result.total_cost_usd is not None:
        attempt.cost_usd, attempt.cost_source = round(result.total_cost_usd, 6), "actual"
    elif result.usage is not None:
        actual_cost = registry.cost_usd(result.usage, selection.entry)
        if actual_cost is None:
            attempt.cost_usd = 0.0
            attempt.cost_source = "unavailable"
        else:
            attempt.cost_usd = actual_cost
            attempt.cost_source = "actual"  # actual tokens, configured prices
    else:
        if estimated_cost_maybe is None:
            attempt.cost_usd, attempt.cost_source = 0.0, "unavailable"
        else:
            attempt.cost_usd, attempt.cost_source = estimated_cost, "estimated"
    budget.settle(estimated_cost, attempt.cost_usd)

    if not result.ok or result.structured is None:
        attempt.error = result.error or "no structured output"
        attempt.retry_status = "retryable" if _is_retryable_error(attempt.error) else "non_retryable"
        return attempt, None
    try:
        decode_structured_response(result.structured, fields)
    except StructuredResponseError as exc:
        attempt.error = f"structured output failed accounting validation: {exc}"
        attempt.retry_status = "retryable"
        return attempt, None

    cache_store_key = key
    if schema_version == 1:
        cache_store_key = llm_cache.task_cache_key(
            provider=provider_name,
            model_id=selection.entry.id or selection.entry.alias,
            effort=selection.effort,
            schema_version=1,
            prompt_version=f"{config.llm.planner.prompt_version}:legacy",
            page_hashes=payload.selected_page_hashes(pages),
            field_keys=sparse_field_keys(fields),
        )

    conn = sqlite3.connect(str(config.db_path), timeout=30)
    try:
        llm_cache.init_cache(conn)
        llm_cache.put_cached(
            conn, cache_store_key, model_id=selection.entry.id, effort=selection.effort,
            structured=result.structured, session_id=result.session_id,
            usage=result.usage, cost_usd=attempt.cost_usd, cost_source=attempt.cost_source,
        )
    finally:
        conn.close()
    return attempt, result.structured


def _call_provider(
    *,
    client,
    job_id: str,
    prompt: str,
    schema: dict,
    schema_path: Path,
    image_paths: list[Path],
    selection: ModelSelection,
    payload: MemoPayload,
    config: Config,
):
    if hasattr(client, "extract_structured"):
        return client.extract_structured(
            job_id=job_id, prompt=prompt, schema=schema, images=image_paths,
            timeout=config.llm.timeout_seconds, model=selection.entry.cli_model_arg() or None,
            effort=selection.effort, cwd=payload.directory,
        )
    return client.extract_json(
        job_id=job_id, prompt=prompt, schema_path=schema_path,
        model=selection.entry.cli_model_arg() or None, effort=selection.effort,
        cwd=payload.directory, allow_read_tool=True, timeout=config.llm.timeout_seconds,
    )


def _should_fallback_legacy(error: str | None) -> bool:
    if not error:
        return False
    text = error.lower()
    return "schema" in text and any(
        needle in text for needle in ("unsupported", "invalid", "failed schema validation", "not accepted")
    )


def _is_retryable_error(error: str | None) -> bool:
    if not error:
        return False
    text = error.lower()
    non_retryable = ("auth", "login", "not authenticated", "not found on path", "unsupported", "api key")
    if any(part in text for part in non_retryable):
        return False
    retryable = ("timed out", "timeout", "non-json", "invalid json", "schema validation", "no structured")
    return any(part in text for part in retryable)


def _flatten_response(structured: dict, fields: list[SchemaField]) -> dict[str, dict]:
    """Sparse v2 or legacy v1 -> {workbook header: field object}."""
    return decode_structured_response(structured, fields)


# ---------------------------------------------------------------------------
# per-memo job
# ---------------------------------------------------------------------------


def _escalate_memo(
    memo: MemoResult,
    *,
    config: Config,
    settings: LlmSettings,
    schema_by_header: dict[str, SchemaField],
    client: ClaudeCodeClient,
    registry: ModelRegistry,
    budget: BudgetTracker,
    ledger: CostLedger,
    run_id: str,
    run_dir: Path,
    deal_files: list[tuple[str, str]] | None = None,
) -> None:
    plan = memo.escalation
    assert plan is not None and plan.fields

    try:
        if deal_files is not None:
            # One-call-per-deal: combine ALL the deal-period's documents into ONE
            # payload (global page index, per-document labels) so the whole deal
            # is extracted in a single call.
            payload = assemble_deal_payload(
                files=deal_files, fields=plan.fields, config=config,
                payload_dir=run_dir / "llm" / memo.memo_id,
            )
        else:
            payload = assemble_payload(
                file_path=memo.file_path, fields=plan.fields, config=config,
                payload_dir=run_dir / "llm" / memo.memo_id,
            )
    except (PayloadError, OSError) as exc:
        plan.status = "llm_failed"
        _flag_first_asset(
            memo,
            ReviewFlag(
                category="llm", description=f"LLM payload assembly failed: {exc}",
                severity=FlagSeverity.warning, reviewer_attention=True,
            ),
        )
        return

    provider_name = getattr(client, "provider_name", config.llm.provider)
    planned = plan_assistance_tasks(
        memo=memo, plan=plan, payload=payload, schema_by_header=schema_by_header,
        config=config, settings=settings, registry=registry, provider=provider_name,
    )
    tasks = planned.tasks
    plan.tasks.extend(task.record for task in tasks)
    prior_planner_ms = float(plan.diagnostics.get("planner_duration_ms", 0.0))
    plan.diagnostics.update(planned.diagnostics)
    plan.diagnostics["planner_duration_ms"] = round(
        prior_planner_ms + float(planned.diagnostics.get("planner_duration_ms", 0.0)), 1
    )
    unresolved: dict[str, EscalationField] = {}
    for task in tasks:
        for escalated in task.escalation_fields:
            unresolved.setdefault(escalated.field, escalated)
    log_event(
        logger, "memo escalation plan", memo_id=memo.memo_id, fields=len(unresolved),
        tasks=len(tasks), task_count_by_wave=planned.diagnostics.get("task_count_by_wave"),
        planner_duration_ms=planned.duration_ms,
    )

    deferred = False
    any_merged = False
    any_ok_attempt = False
    for task_index, task in enumerate(tasks):
        if deferred:
            break
        remaining = {f.field: f for f in task.escalation_fields if f.field in unresolved}
        if not remaining:
            continue
        selection = task.first_selection
        for tier, selection in enumerate([selection]):
            if not remaining:
                break
            slug = f"{task.record.task_id}-t{tier}"
            try:
                attempt, structured = _run_tier(
                    memo=memo, payload=payload, task=task, selection=selection,
                    tier=tier, slug=slug,
                    config=config, settings=settings, client=client,
                    registry=registry, budget=budget, run_id=run_id,
                    corrective=tier > 0,
                )
            except BudgetExceeded as exc:
                deferred = True
                plan.merge_log.append(
                    f"{task.record.task_id} [{slug}] ({selection.entry.alias}): deferred — {exc}"
                )
                _flag_first_asset(
                    memo,
                    ReviewFlag(
                        category="llm",
                        description=f"LLM_DEFERRED: run budget reached before "
                        f"{memo.memo_id} task {task.record.task_id} — fields left for a later run",
                        severity=FlagSeverity.warning, reviewer_attention=True,
                    ),
                )
                break

            if structured is None:
                plan.merge_log.append(
                    f"{task.record.task_id} [{slug}] ({selection.entry.alias}:{selection.effort}): "
                    f"failed — {attempt.error}"
                )
                if attempt.error and "timed out" in attempt.error.lower():
                    _flag_task_failure(memo, task, attempt.error, timeout=True)
                _flag_task_failure(memo, task, attempt.error or "provider call failed")
                plan.attempts.append(attempt)
                ledger.append(run_id=run_id, memo_id=memo.memo_id, attempt=attempt)
                break
            else:
                try:
                    flat = _flatten_response(structured, task.schema_fields)
                except StructuredResponseError as exc:
                    attempt.error = f"structured output failed accounting validation: {exc}"
                    attempt.retry_status = "retryable"
                    plan.merge_log.append(
                        f"{task.record.task_id} [{slug}] ({selection.entry.alias}:{selection.effort}): "
                        f"failed — {attempt.error}"
                    )
                    plan.attempts.append(attempt)
                    ledger.append(run_id=run_id, memo_id=memo.memo_id, attempt=attempt)
                    _flag_task_failure(memo, task, attempt.error)
                    continue
                any_ok_attempt = True
                attempt.fields_returned = sum(1 for h in remaining if h in flat)
                for header in list(remaining):
                    schema_field = schema_by_header.get(header)
                    result = flat.get(header)
                    if schema_field is None or result is None:
                        continue
                    if result.get("not_found") is not True:
                        grounding = ground_quote(
                            str(result.get("verbatim_quote") or ""),
                            result.get("page") if isinstance(result.get("page"), int) else None,
                            payload,
                            config.llm.quote_match_threshold,
                        )
                        if grounding.status == "grounded":
                            attempt.fields_grounded += 1
                        elif grounding.status == "ungrounded":
                            attempt.fields_ungrounded += 1
                    disposition = _merge_field(memo, schema_field, result, selection, payload, config)
                    if disposition == "merged":
                        attempt.fields_merged += 1
                        any_merged = True
                        del remaining[header]
                        unresolved.pop(header, None)
                    elif disposition == "not_found":
                        attempt.fields_not_found += 1
                        # The model looked and the field is absent. Unless the
                        # operator opted into retrying not_found, treat it as
                        # resolved: no expensive-tier re-ask, and no leftover
                        # NOT_EXTRACTABLE flag (a confirmed absence is not a
                        # failure). Failed/rejected fields stay in `remaining`.
                        if not config.llm.retry_not_found:
                            del remaining[header]
                            unresolved.pop(header, None)
                    else:
                        attempt.fields_rejected += 1
                        plan.merge_log.append(f"{header}: {disposition} ({slug})")
            plan.attempts.append(attempt)
            ledger.append(run_id=run_id, memo_id=memo.memo_id, attempt=attempt)
            if structured is None and not _is_retryable_error(attempt.error):
                break

    # Leftovers after every group: surface, never invent (D5).
    if not deferred:
        call_errors = [a.error for a in plan.attempts if a.error]
        if not any_ok_attempt and call_errors:
            # EVERY provider call failed — emit ONE actionable error carrying
            # the CLI's real reason, instead of an identical "no value" flag per
            # unresolved field (hundreds of those bury the actual cause and tell
            # the analyst nothing). The fields are still recorded as
            # not_extractable for the audit/row, just not flagged one-by-one.
            plan.not_extractable.extend(unresolved)
            _flag_first_asset(
                memo,
                ReviewFlag(
                    category="llm",
                    description=f"LLM_PASS_FAILED: every provider call failed "
                    f"({len(plan.attempts)} attempt(s)) — {call_errors[0]}. No fields could "
                    f"be extracted by the LLM; fix this error and re-run.",
                    severity=FlagSeverity.hard_fail, reviewer_attention=True,
                ),
            )
        else:
            for header, escalated in unresolved.items():
                if escalated.reason == "required_empty":
                    plan.not_extractable.append(header)
                    _flag_first_asset(
                        memo,
                        ReviewFlag(
                            category="llm",
                            description=f"NOT_EXTRACTABLE: {header} — deterministic extraction "
                            f"and all LLM passes failed",
                            severity=FlagSeverity.warning, reviewer_attention=True, field=header,
                        ),
                    )
                else:
                    _flag_first_asset(
                        memo,
                        ReviewFlag(
                            category="llm",
                            description=f"LLM_UNCONFIRMED: {header} — low-confidence value "
                            f"could not be improved by the LLM",
                            severity=FlagSeverity.warning, reviewer_attention=True, field=header,
                        ),
                    )

    if deferred:
        plan.status = "llm_deferred_budget"
    elif not any_ok_attempt and plan.attempts:
        plan.status = "llm_failed"
    elif unresolved and any_merged:
        plan.status = "llm_partial"
    elif unresolved:
        plan.status = "llm_partial" if any_ok_attempt else "llm_failed"
    else:
        plan.status = "llm_completed"
    log_event(
        logger, "memo escalation finished", memo_id=memo.memo_id, status=plan.status,
        attempts=len(plan.attempts), merged=len(plan.merged_fields),
        not_extractable=len(plan.not_extractable),
    )


# ---------------------------------------------------------------------------
# run-level entry point (rule 7 worker queue)
# ---------------------------------------------------------------------------


def process_memos(
    memos: list[MemoResult],
    config: Config,
    settings: LlmSettings,
    schema_fields: list[SchemaField],
    *,
    run_id: str,
    run_dir: Path,
    client: ClaudeCodeClient | None = None,
    registry: ModelRegistry | None = None,
) -> LlmRunSummary:
    """Execute the Phase-3 second pass for one run. Mutates the MemoResults
    in place (merged hits, plan attempts/status, flags) and returns the
    run-level summary for the Run Log and the CLI."""
    summary = LlmRunSummary(enabled=settings.enabled)
    if not settings.enabled:
        return summary

    eligible: list[MemoResult] = []
    for memo in memos:
        plan = memo.escalation
        if plan is None:
            continue
        if not plan.fields:
            plan.status = "not_needed"
        elif memo.error is None:
            eligible.append(memo)
    if not eligible:
        summary.executed = True
        summary.detail = "no memos required escalation"
        return summary

    setup = _setup_escalation(config, settings, schema_fields, run_dir, client=client, registry=registry)
    if isinstance(setup, str):
        summary.detail = setup
        _fail_all(eligible, setup)
        return summary
    registry, client, ledger, budget, schema_by_header = setup
    log_event(
        logger, "llm escalation started", run_id=run_id, memos=len(eligible),
        mode=settings.mode, budget_usd=settings.budget_usd, workers=settings.workers,
    )

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        futures = {
            pool.submit(
                _escalate_memo, memo, config=config, settings=settings,
                schema_by_header=schema_by_header, client=client, registry=registry,
                budget=budget, ledger=ledger, run_id=run_id, run_dir=run_dir,
            ): memo
            for memo in eligible
        }
        for future, memo in futures.items():
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 — isolation: one memo never kills the run
                logger.exception("escalation failed for %s", memo.memo_id)
                if memo.escalation is not None:
                    memo.escalation.status = "llm_failed"
                _flag_first_asset(
                    memo,
                    ReviewFlag(
                        category="llm",
                        description=f"LLM escalation error: {type(exc).__name__}: {exc}",
                        severity=FlagSeverity.warning, reviewer_attention=True,
                    ),
                )

    summary.executed = True
    summary.ledger_path = ledger.path
    _accumulate_summary(summary, eligible)
    log_event(
        logger, "llm escalation complete", run_id=run_id,
        memos=summary.memos_escalated, attempts=summary.attempts,
        deferred=summary.memos_deferred, failed=summary.memos_failed,
        cache_hits=summary.cache_hits, total_cost_usd=summary.total_cost_usd,
    )
    return summary


@dataclass
class DealGroup:
    """All documents for one deal-period, escalated in a SINGLE LLM call.

    `primary` is the memo the merged LLM hits land on (the run's multi-doc merge
    later collapses the group's documents to one row by best confidence per
    field, so the primary's fills win). `members` are every memo in the group.
    `files` are the (path, label) pairs combined into the one payload. The caller
    sets the primary's escalation plan to the comprehensive union of the group's
    fields and marks the non-primary members not_needed."""

    primary: MemoResult
    members: list[MemoResult]
    files: list[tuple[str, str]]


def process_deals(
    groups: list[DealGroup],
    config: Config,
    settings: LlmSettings,
    schema_fields: list[SchemaField],
    *,
    run_id: str,
    run_dir: Path,
    client: ClaudeCodeClient | None = None,
    registry: ModelRegistry | None = None,
) -> LlmRunSummary:
    """Combined-deal second pass: one provider call per deal-period over
    the combined payload of all its documents. Mirrors process_memos (shared
    setup/accounting) but the unit of work is a DealGroup, not a single memo."""
    summary = LlmRunSummary(enabled=settings.enabled)
    if not settings.enabled:
        return summary

    eligible: list[DealGroup] = []
    for group in groups:
        plan = group.primary.escalation
        if plan is None:
            continue
        if not plan.fields:
            plan.status = "not_needed"
        elif group.primary.error is None:
            eligible.append(group)
    if not eligible:
        summary.executed = True
        summary.detail = "no deals required escalation"
        return summary

    setup = _setup_escalation(config, settings, schema_fields, run_dir, client=client, registry=registry)
    if isinstance(setup, str):
        summary.detail = setup
        _fail_all([g.primary for g in eligible], setup)
        return summary
    registry, client, ledger, budget, schema_by_header = setup
    log_event(
        logger, "llm deal escalation started", run_id=run_id, deals=len(eligible),
        mode=settings.mode, budget_usd=settings.budget_usd, workers=settings.workers,
    )

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        futures = {
            pool.submit(
                _escalate_memo, group.primary, config=config, settings=settings,
                schema_by_header=schema_by_header, client=client, registry=registry,
                budget=budget, ledger=ledger, run_id=run_id, run_dir=run_dir,
                deal_files=group.files,
            ): group
            for group in eligible
        }
        for future, group in futures.items():
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 — isolation: one deal never kills the run
                logger.exception("deal escalation failed for %s", group.primary.memo_id)
                if group.primary.escalation is not None:
                    group.primary.escalation.status = "llm_failed"
                _flag_first_asset(
                    group.primary,
                    ReviewFlag(
                        category="llm",
                        description=f"LLM escalation error: {type(exc).__name__}: {exc}",
                        severity=FlagSeverity.warning, reviewer_attention=True,
                    ),
                )

    summary.executed = True
    summary.ledger_path = ledger.path
    _accumulate_summary(summary, [g.primary for g in eligible])
    log_event(
        logger, "llm deal escalation complete", run_id=run_id,
        deals=summary.memos_escalated, attempts=summary.attempts,
        deferred=summary.memos_deferred, failed=summary.memos_failed,
        cache_hits=summary.cache_hits, total_cost_usd=summary.total_cost_usd,
    )
    return summary


def _provider_client(config: Config):
    provider = (config.llm.provider or "claude").strip().lower()
    if provider == "codex":
        return CodexCliClient(config)
    if provider == "claude":
        return ClaudeCodeClient(config)
    raise ValueError(f"unknown llm.provider {provider!r} (expected claude|codex)")


def _setup_escalation(
    config: Config,
    settings: LlmSettings,
    schema_fields: list[SchemaField],
    run_dir: Path,
    *,
    client: ClaudeCodeClient | None,
    registry: ModelRegistry | None,
) -> tuple[ModelRegistry, object, CostLedger, BudgetTracker, dict[str, SchemaField]] | str:
    """Shared LLM-pass setup: load model routing, locate/auth the local
    provider CLI, and build the cost ledger / budget tracker / header index.
    Returns the assembled tuple, or an error-detail STRING the caller surfaces
    (and fails the pass with). Used by both process_memos and process_deals."""
    try:
        registry = registry or ModelRegistry.load(config.llm.models_path)
    except (OSError, ValueError) as exc:
        return f"models.yaml unusable: {exc}"
    try:
        client = client or _provider_client(config)
    except ValueError as exc:
        return str(exc)
    if client.binary_path() is None:
        provider = getattr(client, "provider_name", config.llm.provider)
        command = config.codex_cli.command if provider == "codex" else config.claude_code.command
        return (
            f"{provider} CLI ({command!r}) not found on PATH — install/configure the local CLI"
        )
    if hasattr(client, "check_available"):
        auth_ok, auth_detail = client.check_available()
    else:
        auth_ok, auth_detail = client.auth_status()
    if not auth_ok:
        return auth_detail
    if getattr(client, "provider_name", "claude") == "claude" and config.claude_code.auto_update_on_start:
        client.update()
    registry.refresh_cli_status(client)
    ledger = CostLedger(run_dir / "llm" / LEDGER_FILENAME, config.pv_root)
    budget = BudgetTracker(settings.budget_usd)
    schema_by_header = {f.header: f for f in schema_fields}
    return registry, client, ledger, budget, schema_by_header


def _accumulate_summary(summary: LlmRunSummary, memos: list[MemoResult]) -> None:
    """Fold each memo's escalation plan attempts/status into the run summary."""
    diagnostics = {
        "task_count_by_wave": {},
        "requested_fields": 0,
        "found_fields": 0,
        "not_found_fields": 0,
        "grounded_fields": 0,
        "ungrounded_fields": 0,
        "selected_page_count": 0,
        "selected_image_count": 0,
        "estimated_prompt_chars": 0,
        "estimated_output_tokens": 0,
        "provider_duration_seconds": 0.0,
        "timeouts": 0,
        "retries": 0,
        "cache_hits": 0,
        "planner_duration_ms": 0.0,
    }
    for memo in memos:
        plan = memo.escalation
        if plan is None:
            continue
        summary.memos_escalated += 1
        if plan.status == "llm_deferred_budget":
            summary.memos_deferred += 1
        if plan.status == "llm_failed":
            summary.memos_failed += 1
        diagnostics["planner_duration_ms"] = round(
            float(diagnostics["planner_duration_ms"]) + float(plan.diagnostics.get("planner_duration_ms", 0.0)),
            1,
        )
        for task in plan.tasks:
            wave_key = str(task.wave)
            by_wave = diagnostics["task_count_by_wave"]
            by_wave[wave_key] = by_wave.get(wave_key, 0) + 1
            diagnostics["requested_fields"] += len(task.requested_fields)
            diagnostics["selected_page_count"] += len(task.selected_pages)
            diagnostics["selected_image_count"] += task.image_count
            diagnostics["estimated_prompt_chars"] += task.estimated_prompt_chars
            diagnostics["estimated_output_tokens"] += task.estimated_output_tokens
        for attempt in plan.attempts:
            summary.attempts += 1
            summary.cache_hits += 1 if attempt.from_cache else 0
            summary.total_cost_usd += attempt.cost_usd
            summary.any_actual_costs = summary.any_actual_costs or attempt.cost_source == "actual"
            label = attempt.job_id + (f":{attempt.session_id}" if attempt.session_id else "")
            summary.session_labels.append(label)
            diagnostics["found_fields"] += attempt.fields_merged + attempt.fields_rejected
            diagnostics["not_found_fields"] += attempt.fields_not_found
            diagnostics["grounded_fields"] += attempt.fields_grounded
            diagnostics["ungrounded_fields"] += attempt.fields_ungrounded
            diagnostics["provider_duration_seconds"] = round(
                float(diagnostics["provider_duration_seconds"]) + attempt.duration_seconds,
                2,
            )
            diagnostics["timeouts"] += 1 if attempt.error and "timed out" in attempt.error.lower() else 0
            diagnostics["retries"] += 1 if attempt.tier > 0 else 0
            diagnostics["cache_hits"] += 1 if attempt.from_cache else 0
    summary.total_cost_usd = round(summary.total_cost_usd, 4)
    summary.diagnostics = diagnostics


def _fail_all(memos: list[MemoResult], detail: str) -> None:
    for memo in memos:
        if memo.escalation is not None:
            memo.escalation.status = "llm_failed"
        _flag_first_asset(
            memo,
            ReviewFlag(
                category="llm", description=f"LLM fallback unavailable: {detail}",
                severity=FlagSeverity.warning, reviewer_attention=True,
            ),
        )
