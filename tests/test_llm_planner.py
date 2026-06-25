from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.docgen import make_text_pdf
from fixtures.fake_claude import FakeClaudeCodeClient, field_result

from pv_extractor.config import Config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.cache import task_cache_key
from pv_extractor.llm.escalate import LlmSettings, process_memos
from pv_extractor.llm.model_registry import ModelRegistry
from pv_extractor.llm.payload import MemoPayload, PayloadPage
from pv_extractor.llm.planner import plan_assistance_tasks
from pv_extractor.llm.schema_builder import decode_structured_response, sparse_response_key_map
from pv_extractor.models import (
    AssetExtraction,
    EscalationField,
    EscalationPlan,
    FieldHit,
    FlagSeverity,
    MemoResult,
    PageClass,
    QaStatus,
    ReviewFlag,
)
from pv_extractor.run import _build_escalation, _prepare_rescue_wave

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FIELDS = load_schema_fields()
BY_HEADER = {f.header: f for f in SCHEMA_FIELDS}


def make_config(tmp_path: Path) -> Config:
    config = Config(
        pv_root=str(tmp_path / "not_pv"),
        output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
    )
    config.llm.models_path = str(PROJECT_ROOT / "config" / "models.yaml")
    # Pin the embedded page-payload path; direct_document_read (production
    # default) is validated separately and changes the call prompt/inputs.
    config.llm.direct_document_read = False
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    return config


def settings(**overrides) -> LlmSettings:
    base = dict(
        enabled=True, mode="auto", manual_model="sonnet", manual_effort="low",
        allow_fable=False, budget_usd=25.0, workers=1, force=False,
    )
    base.update(overrides)
    return LlmSettings(**base)


def esc(header: str, reason: str = "required_empty", pages: list[int] | None = None) -> EscalationField:
    field = BY_HEADER[header]
    return EscalationField(
        field=header, col_index=field.col_index, band=field.band,
        reason=reason, candidate_pages=pages or [1],
    )


def memo_with_plan(tmp_path: Path, plan_fields: list[EscalationField]) -> MemoResult:
    pdf = tmp_path / "docs" / "memo.pdf"
    if not pdf.exists():
        make_text_pdf(pdf, [["Valuation Memo", "Fund Name: Test Fund I", "MOIC: 1.4x"]])
    memo = MemoResult(
        memo_id="MEMO_X", run_id="RUN_X", client="Client", deal="Deal",
        file_path=str(pdf), file_name="memo.pdf", page_count=20,
        page_band_map={field.band: [1, 2, 3, 10, 15] for field in SCHEMA_FIELDS},
    )
    memo.assets.append(
        AssetExtraction(asset_name="Deal", row_memo_id="MEMO_X", hits=[],
                        qa_status=QaStatus.qa_pass_with_flags)
    )
    memo.escalation = EscalationPlan(
        memo_id=memo.memo_id, confidence_threshold=0.75,
        fields=plan_fields, page_band_map=memo.page_band_map,
    )
    return memo


def fake_payload(tmp_path: Path, pages: int = 20) -> MemoPayload:
    payload = MemoPayload(directory=tmp_path)
    for page in range(1, pages + 1):
        text = (
            f"--- page {page} (TEXT) ---\n"
            f"Valuation methodology enterprise value equity value table page {page}\n"
            f"Gross IRR {10 + page / 10:.1f}% MOIC {1 + page / 10:.1f}x\n"
        )
        payload.pages.append(PayloadPage(
            number=page, page_class=PageClass.TEXT, kind="text",
            rel_path=f"page_{page}.txt", sha256=f"sha-{page}",
        ))
        payload.page_texts[page] = text
        payload.page_blocks[page] = text
    payload.dynamic_prompt = "\n\n".join(payload.page_blocks.values())
    payload.payload_hash = "payload"
    return payload


def test_planner_emits_one_natural_primary_task(tmp_path):
    config = make_config(tmp_path)
    fields = [
        esc(field.header, "force_llm_assist", pages=[1, 2, 3, 7, 9, 12, 18])
        for field in SCHEMA_FIELDS
        if field.header not in {"\U0001f511 Memo ID", "Run ID"} and field.slot_group is None
    ][:60]
    memo = memo_with_plan(tmp_path, fields)
    registry = ModelRegistry.load(config.llm.models_path)
    planned = plan_assistance_tasks(
        memo=memo, plan=memo.escalation, payload=fake_payload(tmp_path),
        schema_by_header=BY_HEADER, config=config, settings=settings(),
        registry=registry, provider="claude",
    )
    assert len(planned.tasks) == 1
    assert len(planned.tasks[0].schema_fields) == len(fields)
    assert planned.tasks[0].record.selected_pages == list(range(1, 21))
    assert planned.diagnostics["execution_shape"] == "natural_request_unit"


def test_normal_mode_catalog_includes_protected_deterministic_field(tmp_path):
    config = make_config(tmp_path)
    value_field = BY_HEADER["Implied EV ($M)"]
    asset = AssetExtraction(
        asset_name="Deal", row_memo_id="MEMO_X",
        hits=[
            FieldHit(field=value_field.header, col_index=value_field.col_index, band=value_field.band,
                     value=100.0, method="deterministic", confidence=0.95)
        ],
        qa_status=QaStatus.qa_pass,
    )
    plan = _build_escalation("MEMO_X", [asset], BY_HEADER, {}, 0.75)
    by_field = {field.field: field for field in plan.fields}
    assert value_field.header in by_field
    assert by_field[value_field.header].reason == "primary_catalog"


def test_force_assist_broadens_but_still_one_primary_task(tmp_path):
    config = make_config(tmp_path)
    asset = AssetExtraction(asset_name="Deal", row_memo_id="MEMO_X", hits=[],
                            qa_status=QaStatus.qa_pass)
    plan = _build_escalation("MEMO_X", [asset], BY_HEADER, {}, 0.75, force_assist=True)
    memo = memo_with_plan(tmp_path, plan.fields)
    registry = ModelRegistry.load(config.llm.models_path)
    planned = plan_assistance_tasks(
        memo=memo, plan=memo.escalation, payload=fake_payload(tmp_path),
        schema_by_header=BY_HEADER, config=config, settings=settings(),
        registry=registry, provider="claude",
    )
    assert len(plan.fields) > 5
    assert len(planned.tasks) == 1
    assert len(planned.tasks[0].schema_fields) == len(plan.fields)


def test_sparse_output_requires_exact_accounting():
    fields = [BY_HEADER["Fund Name"], BY_HEADER["MOIC"]]
    key_map = sparse_response_key_map(fields)
    fund_key = next(key for key, header in key_map.items() if header == "Fund Name")
    moic_key = next(key for key, header in key_map.items() if header == "MOIC")
    decoded = decode_structured_response(
        {
            "schema_version": 2,
            "results": [{
                "field_key": fund_key, "value": "Test Fund I", "unit": None,
                "page": 1, "evidence_quote": "Fund Name: Test Fund I",
                "confidence": 0.85, "notes": "",
            }],
            "not_found_field_keys": [moic_key],
            "warnings": [],
        },
        fields,
    )
    assert decoded["Fund Name"]["value"] == "Test Fund I"
    assert decoded["MOIC"]["not_found"] is True
    with pytest.raises(Exception):
        decode_structured_response(
            {"schema_version": 2, "results": [], "not_found_field_keys": [fund_key], "warnings": []},
            fields,
        )


def test_one_document_failure_keeps_sibling_document_result(tmp_path):
    config = make_config(tmp_path)
    memo = memo_with_plan(tmp_path, [esc("Fund Name")])
    sibling = memo_with_plan(tmp_path, [esc("MOIC")])
    sibling.memo_id = "MEMO_Y"
    sibling.assets[0].row_memo_id = "MEMO_Y"
    fake = FakeClaudeCodeClient(
        {
            "Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I"),
            "MOIC": field_result(1.4, unit="x", page=1, quote="MOIC: 1.4x"),
        },
        behaviors=["timeout", "ok"],
    )
    summary = process_memos(
        [memo, sibling], config, settings(), SCHEMA_FIELDS,
        run_id="RUN_X", run_dir=Path(config.output_dir) / "RUN_X", client=fake,
    )
    hits = {hit.field: hit for hit in sibling.assets[0].hits}
    assert "MOIC" in hits and hits["MOIC"].value == 1.4
    assert any(flag.description.startswith("LLM_TASK_TIMEOUT") for flag in memo.assets[0].flags)
    assert summary.diagnostics["timeouts"] == 1


def test_task_cache_key_changes_for_schema_prompt_provider_page_and_fields():
    base = dict(
        provider="claude", model_id="sonnet", effort="medium", schema_version=2,
        prompt_version="p1", page_hashes=["1:text:a"], field_keys=["Fund_Name"],
    )
    key = task_cache_key(**base)
    for field, value in [
        ("provider", "codex"),
        ("schema_version", 1),
        ("prompt_version", "p2"),
        ("page_hashes", ["1:text:b"]),
        ("field_keys", ["MOIC"]),
    ]:
        changed = dict(base)
        changed[field] = value
        assert task_cache_key(**changed) != key


def test_rescue_wave_targets_only_required_missing_and_field_hard_fail(tmp_path):
    config = make_config(tmp_path)
    memo = memo_with_plan(tmp_path, [])
    asset = memo.assets[0]
    asset.flags.append(
        ReviewFlag(
            category="range", description="bad MOIC", severity=FlagSeverity.hard_fail,
            reviewer_attention=True, field="MOIC",
        )
    )
    rescue = _prepare_rescue_wave([(None, memo)], config, BY_HEADER, derived_headers=set())
    assert rescue == [memo]
    fields = {field.field for field in memo.escalation.fields}
    assert "MOIC" in fields
    assert all(field.reason == "finalization_rescue" for field in memo.escalation.fields)


def test_planner_benchmark_reports_bounded_workload(tmp_path):
    config = make_config(tmp_path)
    config.llm.planner.max_fields_per_task = 10
    config.llm.planner.max_pages_per_task = 5
    fields = [
        esc(field.header, "force_llm_assist", pages=[1, 2, 3, 8, 12, 18, 22])
        for field in SCHEMA_FIELDS
        if field.slot_group is None and field.band not in {"IDENTIFICATION", "QA", "THRESHOLD FLAGS"}
    ][:100]
    memo = memo_with_plan(tmp_path, fields)
    registry = ModelRegistry.load(config.llm.models_path)
    planned = plan_assistance_tasks(
        memo=memo, plan=memo.escalation, payload=fake_payload(tmp_path, pages=25),
        schema_by_header=BY_HEADER, config=config, settings=settings(),
        registry=registry, provider="claude",
    )
    max_fields = max(len(task.schema_fields) for task in planned.tasks)
    max_pages = max(len(task.record.selected_pages) for task in planned.tasks)
    simulated_seconds = sum(5 + len(task.schema_fields) * 0.1 for task in planned.tasks)
    assert max_fields == len(fields)
    assert max_pages == 25
    assert planned.tasks
    assert len(planned.tasks) == 1
    assert simulated_seconds < 600
