"""Bounded adaptive assistance planner for local LLM extraction providers."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from pv_extractor.config import Config
from pv_extractor.extract.targeting import _page_score, build_band_lexicons
from pv_extractor.llm.model_registry import ModelRegistry, ModelSelection
from pv_extractor.llm.model_registry import ExtractionPlanMetrics
from pv_extractor.llm.payload import MemoPayload
from pv_extractor.llm.schema_builder import (
    build_call_prompt,
    build_response_schema,
    schema_json_bytes,
    sparse_field_keys,
)
from pv_extractor.models import (
    AssistanceTaskRecord,
    EscalationField,
    EscalationPlan,
    MemoResult,
    SchemaField,
)
from pv_extractor.normalize import normalize_text


@dataclass
class AssistanceTask:
    record: AssistanceTaskRecord
    schema_fields: list[SchemaField]
    escalation_fields: list[EscalationField]
    first_selection: ModelSelection


@dataclass
class PlannerOutput:
    tasks: list[AssistanceTask]
    duration_ms: float
    diagnostics: dict[str, object]


@dataclass
class _Draft:
    wave: int
    fields: list[SchemaField]
    escalated: list[EscalationField]
    pages: list[int]
    priority: int
    reason: str


def _field_priority(field: SchemaField, escalated: EscalationField, config: Config) -> int:
    planner = config.llm.planner
    values = [
        planner.reason_priorities.get(escalated.reason, 50),
        planner.band_priorities.get(field.band, 50),
        planner.field_priorities.get(field.header, 50),
    ]
    header_norm = normalize_text(f"{field.header} {field.description}")
    for phrase, priority in planner.field_keyword_priorities.items():
        if f" {normalize_text(phrase)} " in f" {header_norm} ":
            values.append(priority)
    return min(values)


def _wave_for(priority: int, escalated: EscalationField, config: Config) -> int:
    if escalated.reason == "finalization_rescue":
        return 3
    return 1 if priority <= config.llm.planner.wave1_priority_max else 2


def _page_scores(
    field: SchemaField,
    escalated: EscalationField,
    payload: MemoPayload,
    lexicons: dict[str, list[str]],
    config: Config,
    *,
    priority: int,
) -> list[tuple[int, float]]:
    payload_pages = set(payload.page_blocks)
    candidate = {p for p in escalated.candidate_pages if p in payload_pages}
    summary = {
        p for p in payload_pages
        if p <= config.extraction.summary_pages and priority <= config.llm.planner.wave1_priority_max
    }
    anchors = list(lexicons.get(field.band, []))
    anchors.extend(normalize_text(field.header).split())
    anchors.extend(tok for tok in normalize_text(field.description).split() if len(tok) >= 4)
    out: list[tuple[int, float]] = []
    for page in sorted(payload_pages):
        text = payload.page_texts.get(page, "")
        padded = f" {normalize_text(text)} "
        score = 0.0
        if page in candidate:
            score += 10.0
        if page in summary:
            score += 4.0
        if payload.page_kind(page) == "image":
            score += 2.0 if page in candidate else 0.25
        if "|" in payload.page_blocks.get(page, ""):
            score += 0.75
        if anchors and text:
            score += _page_score(padded, anchors)
        if score > 0:
            out.append((page, score))
    if not out:
        # Last resort: one or two summary/candidate pages, never the whole memo.
        fallback = sorted(candidate) or sorted(payload_pages)[: min(2, len(payload_pages))]
        out = [(p, 0.1) for p in fallback]
    out.sort(key=lambda item: (-item[1], item[0]))
    return out


def _estimate(fields: list[SchemaField], pages: list[int], payload: MemoPayload, config: Config) -> dict[str, int]:
    prompt = build_call_prompt(
        fields,
        payload.scoped_prompt(pages),
        inferable_fields=set(config.llm.planner.inferable_fields),
    )
    schema_bytes = schema_json_bytes(build_response_schema(fields))
    prompt_chars = len(prompt) + len(schema_bytes)
    prompt_tokens = int(prompt_chars / max(config.llm.chars_per_token, 1.0))
    output_tokens = (
        config.llm.planner.output_tokens_base
        + len(fields) * config.llm.planner.output_tokens_per_found_field
    )
    return {
        "prompt_chars": prompt_chars,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "output_chars": output_tokens * 4,
    }


def _fits(fields: list[SchemaField], pages: list[int], payload: MemoPayload, config: Config) -> bool:
    planner = config.llm.planner
    if len(fields) > planner.max_fields_per_task:
        return False
    if len(set(pages)) > planner.max_pages_per_task:
        return False
    if payload.scoped_image_count(pages) > planner.max_images_per_task:
        return False
    est = _estimate(fields, pages, payload, config)
    return (
        est["prompt_chars"] <= planner.max_prompt_chars_per_task
        and est["output_tokens"] <= planner.max_output_tokens_per_task
    )


def _trim_pages(
    field: SchemaField,
    pages_scored: list[tuple[int, float]],
    payload: MemoPayload,
    config: Config,
) -> list[int]:
    limit = config.llm.planner.max_pages_per_task
    pages = [p for p, _score in pages_scored[:limit]]
    while pages and not _fits([field], pages, payload, config):
        pages = pages[:-1]
    return sorted(pages or [pages_scored[0][0]])


def _merge_reason(reasons: list[str]) -> str:
    unique = sorted(set(reasons))
    return unique[0] if len(unique) == 1 else ",".join(unique)


def _selection_for(
    *,
    task_has_image: bool,
    config: Config,
    settings,
    registry: ModelRegistry,
    provider: str,
) -> ModelSelection:
    routing_mode = "single_model" if settings.mode == "manual" else getattr(settings, "mode", config.llm.routing_mode)
    plan = registry.resolve_extraction_plan(
        routing_mode=routing_mode,
        metrics=ExtractionPlanMetrics(
            estimated_input_tokens=0,
            documents=1,
            image_pages=1 if task_has_image else 0,
            fields=1,
        ),
        auto=config.llm.auto,
        provider=provider,
        manual_model=settings.manual_model,
        manual_effort=settings.manual_effort,
        allow_fable=settings.allow_fable,
        provider_default_model=config.codex_cli.model,
        provider_default_effort=config.codex_cli.reasoning_effort,
        repair_policy=config.llm.candidate_arbitration.repair_policy,
        max_repair_calls_per_deal=config.llm.candidate_arbitration.max_repair_calls_per_deal,
    )
    return plan.selection


def _record_for(
    *,
    memo: MemoResult,
    draft: _Draft,
    payload: MemoPayload,
    config: Config,
    provider: str,
    selection: ModelSelection,
) -> AssistanceTaskRecord:
    fields = draft.fields
    pages = sorted(set(draft.pages))
    est = _estimate(fields, pages, payload, config)
    field_keys = sparse_field_keys(fields)
    digest = hashlib.sha1(
        (
            memo.memo_id
            + f"|w{draft.wave}|"
            + ",".join(field_keys)
            + "|"
            + ",".join(str(p) for p in pages)
            + f"|{provider}|{selection.entry.id}|{selection.effort}"
        ).encode("utf-8")
    ).hexdigest()[:12]
    return AssistanceTaskRecord(
        task_id=f"w{draft.wave}-{digest}",
        memo_id=memo.memo_id,
        row_memo_ids=[asset.row_memo_id for asset in memo.assets],
        deal_id=f"{memo.client}|{memo.deal}",
        document_ids=[memo.file_name],
        requested_field_keys=field_keys,
        requested_fields=[field.header for field in fields],
        field_priorities={field.header: _field_priority(field, esc, config)
                          for field, esc in zip(fields, draft.escalated)},
        selected_pages=pages,
        selected_page_hashes=payload.selected_page_hashes(pages),
        text_block_count=sum(1 for p in pages if payload.page_kind(p) != "image"),
        image_count=payload.scoped_image_count(pages),
        estimated_prompt_chars=est["prompt_chars"],
        estimated_prompt_tokens=est["prompt_tokens"],
        estimated_output_tokens=est["output_tokens"],
        worst_case_output_chars=est["output_chars"],
        reason=draft.reason,
        wave=draft.wave,
        provider=provider,
        model_alias=selection.entry.alias,
        model_id=selection.entry.id,
        effort=selection.effort,
    )


def _add_unit(drafts: list[_Draft], unit: _Draft, payload: MemoPayload, config: Config) -> None:
    for draft in drafts:
        if draft.wave != unit.wave:
            continue
        # Keep batches page-coherent and mostly band-coherent without forcing one
        # call per band when pages are identical.
        union_pages = sorted(set(draft.pages) | set(unit.pages))
        union_fields = [*draft.fields, *unit.fields]
        if not _fits(union_fields, union_pages, payload, config):
            continue
        draft.fields.extend(unit.fields)
        draft.escalated.extend(unit.escalated)
        draft.pages = union_pages
        draft.priority = min(draft.priority, unit.priority)
        draft.reason = _merge_reason([draft.reason, unit.reason])
        return
    drafts.append(unit)


def plan_assistance_tasks(
    *,
    memo: MemoResult,
    plan: EscalationPlan,
    payload: MemoPayload,
    schema_by_header: dict[str, SchemaField],
    config: Config,
    settings,
    registry: ModelRegistry,
    provider: str,
) -> PlannerOutput:
    started = time.perf_counter()
    unique: dict[str, EscalationField] = {}
    for escalated in plan.fields:
        if escalated.field in schema_by_header:
            unique.setdefault(escalated.field, escalated)
    fields = sorted((schema_by_header[name] for name in unique), key=lambda f: f.col_index)
    escalated_by_header = {field.field: field for field in unique.values()}
    escalated = [escalated_by_header[field.header] for field in fields]
    pages = sorted(payload.page_blocks)
    provider_name = provider
    selection = _selection_for(
        task_has_image=payload.scoped_image_count(pages) > 0,
        config=config,
        settings=settings,
        registry=registry,
        provider=provider_name,
    )
    profile = selection.entry.extraction_profile
    full_est = _estimate(fields, pages, payload, config) if fields else {
        "prompt_chars": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "output_chars": 0,
    }
    context_limit = selection.entry.context_window or 0
    provider_limit = int(context_limit * 0.85) if context_limit else 0
    oversized = bool(
        fields
        and provider_limit
        and full_est["prompt_tokens"] > provider_limit
        and len(fields) > profile.oversized_target_fields
    )
    chunks: list[list[SchemaField]]
    fallback_reason = ""
    batch_count = max(1, int(getattr(config.llm, "field_batch_count", 1) or 1))
    if fields and batch_count > 1:
        # Operator-chosen batching: split the fields into N near-equal contiguous
        # batches (each its own call); ceil sizing means the last batch absorbs
        # the remainder (191 / 4 -> 48, 48, 48, 47).
        size = math.ceil(len(fields) / batch_count)
        chunks = [fields[i : i + size] for i in range(0, len(fields), size)]
        fallback_reason = f"field_batch_count={batch_count}: {len(chunks)} field batches"
    elif oversized:
        target = max(1, profile.oversized_target_fields)
        chunks = [fields[i : i + target] for i in range(0, len(fields), target)]
        fallback_reason = (
            f"oversized fallback: estimated {full_est['prompt_tokens']} input tokens "
            f"exceeds configured single-request limit {provider_limit}"
        )
    else:
        chunks = [fields] if fields else []

    tasks: list[AssistanceTask] = []
    for index, chunk in enumerate(chunks):
        by_header = {e.field: e for e in escalated}
        draft = _Draft(
            wave=1,
            fields=chunk,
            escalated=[by_header[field.header] for field in chunk],
            pages=pages,
            priority=1,
            reason="oversized_fallback" if oversized else "primary_extraction",
        )
        record = _record_for(
            memo=memo,
            draft=draft,
            payload=payload,
            config=config,
            provider=provider_name,
            selection=selection,
        )
        # Multiple chunks (operator batching OR oversized fallback) must get
        # UNIQUE task ids — they drive the per-call slug, prompt/schema filenames
        # and event ids; a shared id would collide on disk and in the UI.
        if len(chunks) > 1:
            record.reason = fallback_reason
            record.task_id = f"{record.task_id}-part{index + 1}-of-{len(chunks)}"
        tasks.append(AssistanceTask(record=record, schema_fields=draft.fields,
                                    escalation_fields=draft.escalated, first_selection=selection))

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    by_wave: dict[str, int] = {}
    for task in tasks:
        by_wave[str(task.record.wave)] = by_wave.get(str(task.record.wave), 0) + 1
    diagnostics = {
        "planner_duration_ms": duration_ms,
        "task_count": len(tasks),
        "task_count_by_wave": by_wave,
        "requested_fields": sum(len(task.schema_fields) for task in tasks),
        "selected_pages": sum(len(task.record.selected_pages) for task in tasks),
        "selected_images": sum(task.record.image_count for task in tasks),
        "estimated_prompt_chars": sum(task.record.estimated_prompt_chars for task in tasks),
        "estimated_output_tokens": sum(task.record.estimated_output_tokens for task in tasks),
        "max_fields_per_task": max((len(task.schema_fields) for task in tasks), default=0),
        "max_pages_per_task": max((len(task.record.selected_pages) for task in tasks), default=0),
        "max_prompt_chars_per_task": max((task.record.estimated_prompt_chars for task in tasks), default=0),
        "execution_shape": "natural_request_unit",
        "fallback_reason": fallback_reason,
    }
    return PlannerOutput(tasks=tasks, duration_ms=duration_ms, diagnostics=diagnostics)
