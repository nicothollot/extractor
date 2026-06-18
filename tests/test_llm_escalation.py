"""D4/D5/D6 escalation tests: payload assembly, quote grounding, merge
policy, tier ladder, budget cap, response cache, and the full pipeline on
the scanned fixture memo. Claude Code is ALWAYS the fake client here."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fixtures.docgen import make_text_pdf
from fixtures.fake_claude import FakeClaudeCodeClient, field_result

from pv_extractor.config import Config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.costs import LEDGER_FILENAME, read_ledger, summarize_ledger
from pv_extractor.llm.escalate import (
    LlmSettings,
    process_memos,
    quote_grounding,
    resolve_settings,
)
from pv_extractor.llm.payload import MemoPayload, PayloadPage, assemble_payload, select_pages
from pv_extractor.models import (
    AssetExtraction,
    EscalationField,
    EscalationPlan,
    FieldHit,
    MemoResult,
    PageClass,
    QaStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FIELDS = load_schema_fields()
BY_HEADER = {f.header: f for f in SCHEMA_FIELDS}

PAGE1 = [
    "Valuation Memo",
    "Fund Name: Test Fund I",
    "Gross IRR: 12.5%",
    "MOIC: 1.4x",
]


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path) -> Config:
    config = Config(
        pv_root=str(tmp_path / "not_pv"),
        output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
    )
    config.llm.models_path = str(PROJECT_ROOT / "config" / "models.yaml")
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    return config


def escalation_field(header: str, reason: str, pages: list[int] | None = None) -> EscalationField:
    schema_field = BY_HEADER[header]
    return EscalationField(
        field=header, col_index=schema_field.col_index, band=schema_field.band,
        reason=reason, candidate_pages=pages or [1],
    )


def det_hit(header: str, value, confidence: float) -> FieldHit:
    schema_field = BY_HEADER[header]
    return FieldHit(
        field=header, col_index=schema_field.col_index, band=schema_field.band,
        raw_text=str(value), value=value, method="deterministic",
        confidence=confidence, evidence=f"{header}: {value}", page=1,
    )


def make_memo(tmp_path: Path, *, hits: list[FieldHit], plan_fields: list[EscalationField]) -> MemoResult:
    pdf = tmp_path / "docs" / "memo.pdf"
    if not pdf.exists():
        make_text_pdf(pdf, [PAGE1])
    memo = MemoResult(
        memo_id="MEMO_20260611_120000_001", run_id="RUN_TEST", client="TestClient",
        deal="TestDeal", file_path=str(pdf), file_name="memo.pdf",
    )
    memo.assets.append(
        AssetExtraction(
            asset_name="TestDeal", row_memo_id=memo.memo_id, hits=hits,
            qa_status=QaStatus.qa_pass_with_flags,
        )
    )
    memo.escalation = EscalationPlan(
        memo_id=memo.memo_id, confidence_threshold=0.75, fields=plan_fields,
    )
    return memo


def settings(**overrides) -> LlmSettings:
    base = dict(
        enabled=True, mode="auto", manual_model="sonnet", manual_effort="low",
        allow_fable=False, budget_usd=25.0, workers=1, force=False,
    )
    base.update(overrides)
    return LlmSettings(**base)


def run_escalation(config, memos, client, *, llm_settings=None):
    return process_memos(
        memos, config, llm_settings or settings(), SCHEMA_FIELDS,
        run_id="RUN_TEST", run_dir=Path(config.output_dir) / "RUN_TEST", client=client,
    )


def flags_of(memo: MemoResult) -> list[str]:
    return [flag.description for flag in memo.assets[0].flags]


# ---------------------------------------------------------------------------
# page selection + payload (rules 1 and 8)
# ---------------------------------------------------------------------------


def test_select_pages_targets_candidates_plus_summary():
    fields = [
        escalation_field("Gross IRR %", "below_confidence", pages=[7, 9]),
        escalation_field("Fund Name", "required_empty", pages=[9, 40]),
    ]
    assert select_pages(fields, page_count=30, summary_pages=3, max_pages=20) == [1, 2, 3, 7, 9]


def test_select_pages_caps_at_max_keeping_summary_first():
    fields = [escalation_field("Gross IRR %", "below_confidence", pages=list(range(4, 30)))]
    selected = select_pages(fields, page_count=30, summary_pages=3, max_pages=6)
    assert selected == [1, 2, 3, 4, 5, 6]


def test_select_pages_short_memo_is_whole_memo():
    fields = [escalation_field("Gross IRR %", "below_confidence", pages=[1])]
    assert select_pages(fields, page_count=2, summary_pages=3, max_pages=20) == [1, 2]


def test_assemble_payload_text_pages_inline_no_images(tmp_path):
    config = make_config(tmp_path)
    pdf = tmp_path / "docs" / "memo.pdf"
    make_text_pdf(pdf, [PAGE1], tables_by_page={1: [[["Metric", "Value"], ["Gross IRR %", "12.5%"]]]})
    payload = assemble_payload(
        file_path=str(pdf), fields=[escalation_field("Gross IRR %", "below_confidence")],
        config=config, payload_dir=tmp_path / "output" / "payload",
    )
    assert [p.kind for p in payload.pages] == ["text"]
    assert payload.image_count == 0 and payload.ocr_hostile is False
    assert "Fund Name: Test Fund I" in payload.dynamic_prompt
    assert "| Metric | Value |" in payload.dynamic_prompt  # tables ride as pipe tables
    assert (tmp_path / "output" / "payload" / "manifest.json").exists()
    assert (tmp_path / "output" / "payload" / "pages" / "page_001.txt").exists()
    assert payload.payload_hash


# ---------------------------------------------------------------------------
# quote grounding (rule 5)
# ---------------------------------------------------------------------------


def _payload_with(page_texts: dict[int, str], image_pages: set[int] = frozenset()) -> MemoPayload:
    payload = MemoPayload(directory=Path("."))
    payload.page_texts = page_texts
    payload.pages = [
        PayloadPage(
            number=n, page_class=PageClass.SCANNED if n in image_pages else PageClass.TEXT,
            kind="image" if n in image_pages else "text", rel_path="x", sha256="",
        )
        for n in page_texts
    ]
    return payload


def test_quote_grounding_exact_and_normalized():
    payload = _payload_with({1: "Fund  Name:\nTest Fund I"})
    assert quote_grounding("fund name: test fund i", 1, payload, 85) == "grounded"
    assert quote_grounding("Test Fund I", 1, payload, 85) == "grounded"


def test_quote_grounding_rejects_fabrications_and_wrong_pages():
    payload = _payload_with({1: "Gross IRR: 12.5%"})
    assert quote_grounding("Gross IRR: 99.9%", 1, payload, 85) == "ungrounded"
    assert quote_grounding("Gross IRR: 12.5%", 2, payload, 85) == "ungrounded"
    assert quote_grounding("", 1, payload, 85) == "ungrounded"


def test_quote_grounding_is_fuzzy_on_image_pages_only():
    noisy = "Gros5 IRR : 12.5 %"  # OCR noise
    payload = _payload_with({1: noisy}, image_pages={1})
    assert quote_grounding("Gross IRR: 12.5%", 1, payload, 85) == "grounded"
    payload = _payload_with({1: noisy})
    assert quote_grounding("Gross IRR: 12.5%", 1, payload, 85) == "ungrounded"


def test_quote_grounding_unverifiable_without_local_text():
    payload = _payload_with({4: ""}, image_pages={4})
    assert quote_grounding("anything", 4, payload, 85) == "unverifiable"


# ---------------------------------------------------------------------------
# merge policy + tier ladder
# ---------------------------------------------------------------------------


def test_fill_empty_field_and_never_overwrite_confident_value(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(
        tmp_path,
        hits=[det_hit("Gross IRR %", 11.0, confidence=0.95)],  # confident: untouchable
        plan_fields=[
            escalation_field("Fund Name", "required_empty"),
            escalation_field("Gross IRR %", "below_confidence"),
        ],
    )
    fake = FakeClaudeCodeClient({
        "Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I"),
        "Gross IRR %": field_result(12.5, unit="percent", page=1, quote="Gross IRR: 12.5%"),
    })
    summary = run_escalation(config, [memo], fake)

    fund = next(h for h in memo.assets[0].hits if h.field == "Fund Name")
    assert fund.value == "Test Fund I"
    assert fund.method == "claude-code:sonnet:medium"  # text memo -> AUTO tier 0
    assert fund.evidence == "Fund Name: Test Fund I" and fund.page == 1
    assert fund.confidence == pytest.approx(0.85)

    irr = next(h for h in memo.assets[0].hits if h.field == "Gross IRR %")
    assert irr.value == 11.0 and irr.method == "deterministic"  # never overwritten

    plan = memo.escalation
    assert plan.merged_fields == ["Fund Name"]
    assert any("rejected:protected" in line for line in plan.merge_log)
    assert plan.status == "llm_partial"
    assert any(d.startswith("LLM_UNCONFIRMED: Gross IRR") for d in flags_of(memo))
    # Band-batched: FUND ("Fund Name") merges on the sonnet pass (one call), while
    # RETURNS ("Gross IRR %", protected by a confident det value) runs the full
    # sonnet->opus ladder and never merges. Order is relevance-driven, so compare
    # the multiset of tiers.
    tiers = sorted((a.model_alias, a.effort) for a in plan.attempts)
    assert tiers == [("opus", "high"), ("sonnet", "medium"), ("sonnet", "medium")]
    assert summary.attempts == 3 and summary.total_cost_usd > 0
    ledger = read_ledger(Path(config.output_dir) / "RUN_TEST" / "llm" / LEDGER_FILENAME)
    assert len(ledger) == 3 and all(e["cost_source"] == "estimated" for e in ledger)


def test_overwrite_below_threshold_keeps_old_value_as_conflict(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(
        tmp_path,
        hits=[det_hit("Gross IRR %", 11.0, confidence=0.50)],
        plan_fields=[escalation_field("Gross IRR %", "below_confidence")],
    )
    fake = FakeClaudeCodeClient(
        {"Gross IRR %": field_result(12.5, unit="percent", page=1, quote="Gross IRR: 12.5%")}
    )
    run_escalation(config, [memo], fake)

    irr = next(h for h in memo.assets[0].hits if h.field == "Gross IRR %")
    assert irr.value == 12.5 and irr.method == "claude-code:sonnet:medium"
    assert irr.conflicts and irr.conflicts[-1].value == 11.0  # audit keeps the loser
    plan = memo.escalation
    assert plan.status == "llm_completed"
    assert any("overwrote below-threshold" in line for line in plan.merge_log)
    assert len(plan.attempts) == 1  # resolved at tier 0, no retry


def test_fabricated_quote_is_discarded_with_ungrounded_flag(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(tmp_path, hits=[], plan_fields=[escalation_field("Fund Name", "required_empty")])
    fake = FakeClaudeCodeClient(
        {"Fund Name": field_result("Fake Fund", page=1, quote="Fund Name: Fake Fund LP")}
    )
    run_escalation(config, [memo], fake)

    assert not any(h.field == "Fund Name" for h in memo.assets[0].hits)  # value discarded
    assert any(d.startswith("UNGROUNDED_LLM_VALUE: Fund Name") for d in flags_of(memo))
    plan = memo.escalation
    assert "Fund Name" in plan.not_extractable
    not_extractable = [d for d in flags_of(memo) if d.startswith("NOT_EXTRACTABLE: Fund Name")]
    assert not_extractable and memo.assets[0].flags[
        flags_of(memo).index(not_extractable[0])
    ].reviewer_attention


def test_malformed_json_falls_through_to_retry_tier(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(tmp_path, hits=[], plan_fields=[escalation_field("Fund Name", "required_empty")])
    fake = FakeClaudeCodeClient(
        {"Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I")},
        behaviors=["malformed", "ok"],
    )
    run_escalation(config, [memo], fake)

    plan = memo.escalation
    assert plan.attempts[0].error and "non-JSON" in plan.attempts[0].error
    assert plan.attempts[1].fields_merged == 1
    fund = next(h for h in memo.assets[0].hits if h.field == "Fund Name")
    assert fund.method == "claude-code:opus:high"  # merged by the retry tier
    assert plan.status == "llm_completed"


def test_not_found_heavy_response_never_invents_values(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(
        tmp_path,
        hits=[det_hit("Gross IRR %", 11.0, confidence=0.50)],
        plan_fields=[
            escalation_field("Fund Name", "required_empty"),
            escalation_field("Gross IRR %", "below_confidence"),
        ],
    )
    fake = FakeClaudeCodeClient({})  # everything not_found
    run_escalation(config, [memo], fake)

    plan = memo.escalation
    assert plan.merged_fields == []
    # Band-batched: Fund Name and Gross IRR % are in different bands, so each is
    # its own one-field call across the sonnet->opus ladder (4 attempts total).
    assert all(a.fields_not_found == 1 for a in plan.attempts)
    assert sum(a.fields_not_found for a in plan.attempts) == 4
    assert plan.not_extractable == ["Fund Name"]
    assert any(d.startswith("NOT_EXTRACTABLE: Fund Name") for d in flags_of(memo))
    assert any(d.startswith("LLM_UNCONFIRMED: Gross IRR") for d in flags_of(memo))
    irr = next(h for h in memo.assets[0].hits if h.field == "Gross IRR %")
    assert irr.value == 11.0  # untouched


def test_budget_cap_defers_without_any_call(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(tmp_path, hits=[], plan_fields=[escalation_field("Fund Name", "required_empty")])
    fake = FakeClaudeCodeClient(
        {"Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I")}
    )
    summary = run_escalation(config, [memo], fake, llm_settings=settings(budget_usd=0.0))

    assert fake.calls == []  # never submitted
    plan = memo.escalation
    assert plan.status == "llm_deferred_budget" and plan.attempts == []
    assert any(d.startswith("LLM_DEFERRED") for d in flags_of(memo))
    assert summary.memos_deferred == 1


def test_response_cache_prevents_repaying_and_force_bypasses(tmp_path):
    config = make_config(tmp_path)
    values = {"Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I")}
    plan_fields = [escalation_field("Fund Name", "required_empty")]

    fake1 = FakeClaudeCodeClient(values)
    memo1 = make_memo(tmp_path, hits=[], plan_fields=list(plan_fields))
    run_escalation(config, [memo1], fake1)
    assert len(fake1.calls) == 1 and memo1.escalation.attempts[0].from_cache is False

    fake2 = FakeClaudeCodeClient(values)
    memo2 = make_memo(tmp_path, hits=[], plan_fields=list(plan_fields))
    run_escalation(config, [memo2], fake2)
    assert fake2.calls == []  # rule 10: unchanged memo never re-runs
    attempt = memo2.escalation.attempts[0]
    assert attempt.from_cache is True and attempt.cost_usd == 0.0
    fund = next(h for h in memo2.assets[0].hits if h.field == "Fund Name")
    assert fund.value == "Test Fund I"  # merged from the cached response

    fake3 = FakeClaudeCodeClient(values)
    memo3 = make_memo(tmp_path, hits=[], plan_fields=list(plan_fields))
    run_escalation(config, [memo3], fake3, llm_settings=settings(force=True))
    assert len(fake3.calls) == 1  # --force-llm bypasses the cache


def test_auth_failure_fails_plans_with_login_instruction(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(tmp_path, hits=[], plan_fields=[escalation_field("Fund Name", "required_empty")])
    fake = FakeClaudeCodeClient(auth_ok=False)
    summary = run_escalation(config, [memo], fake)

    assert fake.calls == []
    assert memo.escalation.status == "llm_failed"
    assert "claude auth login" in summary.detail
    assert any("claude auth login" in d for d in flags_of(memo))


def test_manual_mode_forces_one_model_for_everything(tmp_path):
    config = make_config(tmp_path)
    memo = make_memo(
        tmp_path,
        hits=[],
        plan_fields=[
            escalation_field("Fund Name", "required_empty"),
            escalation_field("Gross IRR %", "below_confidence"),
        ],
    )
    fake = FakeClaudeCodeClient(
        {"Fund Name": field_result("Test Fund I", page=1, quote="Fund Name: Test Fund I")}
    )
    run_escalation(
        config, [memo], fake,
        llm_settings=settings(mode="manual", manual_model="sonnet", manual_effort="low"),
    )
    plan = memo.escalation
    # Manual mode forces the one model+effort for every band call and never
    # escalates to the opus retry tier. Band-batched: FUND merges, RETURNS is
    # not_found — one sonnet/low call each, no opus anywhere.
    assert [(a.model_alias, a.effort) for a in plan.attempts] == [("sonnet", "low"), ("sonnet", "low")]
    assert all(a.model_alias == "sonnet" for a in plan.attempts)
    assert len(fake.calls) == 2


def test_resolve_settings_cli_semantics(default_config):
    s = resolve_settings(default_config)
    assert s.enabled and s.mode == "auto" and s.budget_usd == 25.0
    assert resolve_settings(default_config, no_llm=True).enabled is False
    s = resolve_settings(default_config, model="haiku", effort="low")
    assert s.mode == "manual" and s.manual_model == "haiku"  # --llm-model implies manual
    assert resolve_settings(default_config, budget=5.0).budget_usd == 5.0


# ---------------------------------------------------------------------------
# full pipeline on the scanned fixture memo (D6)
# ---------------------------------------------------------------------------


def test_full_pipeline_scanned_memo_escalates_merges_and_ledgers(phase2_env):
    """Deterministic OCR scores below threshold -> EscalationPlan -> fake
    Claude Code -> merged row + audit record + cost ledger entries."""
    from openpyxl import load_workbook

    from pv_extractor.run import run
    from pv_extractor.write import RUN_LOG_COLUMNS

    config = phase2_env
    fake = FakeClaudeCodeClient({
        "Gross IRR %": field_result(12.5, unit="percent", page=1, quote="Gross IRR: 12.5%"),
        # fabricated quote: must be rejected by quote-grounding
        "MOIC": field_result(9.9, unit="x", page=1, quote="Quarterly dividend of $4.21 per share"),
    })
    report = run(
        config, scope="deal", client="Angeles Investments", deal="Andover Storage",
        period="2026-03-31", force=True, now=datetime(2026, 6, 11, 13, 0, 0),
        llm_settings=resolve_settings(config), llm_client=fake,
    )

    assert report.coverage[0].status == "FOUND"
    memo = report.memos[0]
    plan = memo.escalation
    escalated = {f.field for f in plan.fields}
    assert {"Gross IRR %", "MOIC"} <= escalated  # OCR-page hits sit below threshold

    # the scanned memo is OCR-hostile: AUTO routes straight to opus/high
    assert fake.calls and fake.calls[0]["model"] == "opus" and fake.calls[0]["effort"] == "high"

    # merged row: the grounded LLM value replaced the low-confidence OCR hit
    irr = next(h for h in memo.assets[0].hits if h.field == "Gross IRR %")
    assert irr.value == 12.5
    assert irr.method == "claude-code:opus:high"
    assert irr.evidence == "Gross IRR: 12.5%"
    assert "Gross IRR %" in plan.merged_fields

    # fabricated quote: value discarded, UNGROUNDED flag raised
    moic = next(h for h in memo.assets[0].hits if h.field == "MOIC")
    assert moic.method != "claude-code:opus:high" or moic.value != 9.9
    assert any(
        f.description.startswith("UNGROUNDED_LLM_VALUE: MOIC") and f.reviewer_attention
        for f in memo.assets[0].flags
    )

    # attempts + run summary + ledger
    assert plan.status in ("llm_partial", "llm_completed")
    assert len(plan.attempts) >= 1
    assert report.llm is not None and report.llm.executed
    assert report.llm.session_labels and report.llm.total_cost_usd > 0
    ledger_entries = read_ledger(report.run_dir / "llm" / LEDGER_FILENAME)
    assert len(ledger_entries) == len(plan.attempts)
    summary = summarize_ledger(ledger_entries)
    assert summary["memos"] == 1 and summary["total_usd"] > 0

    # audit record carries the attempts and the merge log
    audit = json.loads((report.run_dir / "audit" / f"{memo.memo_id}.json").read_text("utf-8"))
    assert audit["escalation"]["status"] == plan.status
    assert audit["escalation"]["attempts"][0]["job_id"].startswith(f"pv-{report.run_id}-")
    assert audit["escalation"]["merge_log"]

    # workbook: merged value landed in the Index row; sessions in the Run Log
    workbook = load_workbook(report.workbook_path, read_only=True)
    index = workbook["Index"]
    irr_col = BY_HEADER["Gross IRR %"].col_index
    row = next(
        r for r in range(4, index.max_row + 1)
        if index.cell(row=r, column=1).value == memo.memo_id
    )
    assert index.cell(row=row, column=irr_col).value == 12.5
    run_log = workbook["Run Log"]
    sessions_col = RUN_LOG_COLUMNS.index("Batch Sessions") + 1
    sessions_cell = run_log.cell(row=run_log.max_row, column=sessions_col).value
    assert sessions_cell and f"pv-{report.run_id}-{memo.memo_id}-g0t0" in sessions_cell
    workbook.close()


def test_full_pipeline_no_llm_settings_is_pure_phase2(phase2_env):
    from pv_extractor.run import run

    report = run(
        phase2_env, scope="deal", client="Angeles Investments", deal="Andover Storage",
        period="2026-03-31", force=True, now=datetime(2026, 6, 11, 13, 30, 0),
    )
    memo = report.memos[0]
    assert report.llm is None
    assert memo.escalation.attempts == []
    assert memo.escalation.status == "llm_fallback_disabled"
    assert not any(h.method.startswith("claude-code:") for h in memo.assets[0].hits)
