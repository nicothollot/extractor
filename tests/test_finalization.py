"""Post-LLM finalization regressions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pv_extractor.config import Config
from pv_extractor.models import AssetExtraction, FieldHit, MemoResult, QaStatus, SchemaField
from pv_extractor.run import _merge_asset
from pv_extractor.validate import load_rules
from pv_extractor.validate.finalize import finalize_asset_after_assistance

AS_OF = date(2026, 3, 31)


@pytest.fixture(scope="module")
def schema_by_header(project_root: Path) -> dict[str, SchemaField]:
    doc = json.loads((project_root / "schema" / "master_schema.json").read_text(encoding="utf-8"))
    return {f["header"]: SchemaField.model_validate(f) for f in doc["fields"]}


@pytest.fixture(scope="module")
def ruleset(project_root: Path):
    return load_rules(project_root / "rules.yaml")


@pytest.fixture(scope="module")
def routing_table(project_root: Path) -> dict[str, list[str]]:
    return json.loads((project_root / "schema" / "band_routing.json").read_text(encoding="utf-8"))["routing"]


class _NoPriorWriter:
    def find_prior_row(self, _company, _fund, _as_of):
        return None


def _hit(schema_by_header, header: str, value, *, method: str = "deterministic", confidence: float = 0.9) -> FieldHit:
    field = schema_by_header[header]
    return FieldHit(
        field=header,
        col_index=field.col_index,
        band=field.band,
        raw_text=str(value),
        value=value,
        method=method,
        confidence=confidence,
        evidence=str(value),
    )


def _qa(asset: AssetExtraction) -> dict[str, object]:
    return {hit.field: hit.value for hit in asset.hits if hit.band == "QA"}


def _finalize(asset, default_config, schema_by_header, ruleset, routing_table):
    return finalize_asset_after_assistance(
        asset,
        config=default_config,
        schema_by_header=schema_by_header,
        ruleset=ruleset,
        routing_table=routing_table,
        as_of_date=AS_OF,
        verify=None,
        prior_row=None,
        client="Client",
    )


def test_llm_value_removes_stale_no_valuation_failure_and_recomputes_qa(
    default_config: Config, schema_by_header, ruleset, routing_table
) -> None:
    asset = AssetExtraction(
        row_memo_id="MEMO_1",
        hits=[_hit(schema_by_header, "Revenue ($M)", 410.0)],
    )
    _finalize(asset, default_config, schema_by_header, ruleset, routing_table)
    assert asset.qa_status is QaStatus.qa_fail
    assert any(flag.code == "no_valuation_value" for flag in asset.flags)
    assert _qa(asset)["QA Status"] == "qa_fail"
    assert _qa(asset)["Reviewer Attention"] == "Y"

    asset.hits.append(
        _hit(
            schema_by_header,
            "Implied EV ($M)",
            545.0,
            method="llm:fake:sonnet:medium",
            confidence=0.85,
        )
    )
    _finalize(asset, default_config, schema_by_header, ruleset, routing_table)

    assert asset.qa_status is QaStatus.qa_pass
    assert not any(flag.code == "no_valuation_value" for flag in asset.flags)
    assert _qa(asset) == {"QA Status": "qa_pass", "Extraction Flags Count": 0, "Reviewer Attention": "N"}
    assert not [flag.description for flag in asset.flags if flag.severity.value == "hard_fail"]


def test_finalization_is_idempotent_and_flag_count_matches_review_flags(
    default_config: Config, schema_by_header, ruleset, routing_table
) -> None:
    asset = AssetExtraction(
        row_memo_id="MEMO_2",
        hits=[
            _hit(schema_by_header, "Fund Share Equity Value ($M)", 125.0),
            _hit(schema_by_header, "Prior Qtr NAV ($M)", 100.0),
        ],
    )
    _finalize(asset, default_config, schema_by_header, ruleset, routing_table)
    first_hits = [(hit.field, hit.value, hit.method) for hit in asset.hits]
    first_flags = [(flag.origin, flag.code, flag.field, flag.description) for flag in asset.flags]

    _finalize(asset, default_config, schema_by_header, ruleset, routing_table)

    assert [(hit.field, hit.value, hit.method) for hit in asset.hits] == first_hits
    assert [(flag.origin, flag.code, flag.field, flag.description) for flag in asset.flags] == first_flags
    assert sum(1 for hit in asset.hits if hit.field == "NAV Change Abs ($M)") == 1
    assert sum(1 for hit in asset.hits if hit.field == "New to Portfolio") == 1
    assert _qa(asset)["Extraction Flags Count"] == len(asset.flags)


def test_single_and_merged_assets_share_finalization_semantics(
    default_config: Config, schema_by_header, ruleset, routing_table
) -> None:
    single = AssetExtraction(
        row_memo_id="MEMO_SINGLE",
        hits=[
            _hit(schema_by_header, "Revenue ($M)", 410.0),
            _hit(schema_by_header, "Implied EV ($M)", 545.0, method="llm:fake:sonnet:medium"),
        ],
    )
    _finalize(single, default_config, schema_by_header, ruleset, routing_table)

    primary_memo = MemoResult(
        memo_id="MEMO_MERGED",
        run_id="RUN_TEST",
        client="Client",
        deal="Asset",
        file_path="/tmp/a.pdf",
        file_name="a.pdf",
        as_of_date=AS_OF,
    )
    primary_asset = AssetExtraction(
        row_memo_id="MEMO_MERGED",
        hits=[_hit(schema_by_header, "Revenue ($M)", 410.0)],
    )
    primary_memo.assets = [primary_asset]
    assist_memo = MemoResult(
        memo_id="MEMO_MERGED_B",
        run_id="RUN_TEST",
        client="Client",
        deal="Asset",
        file_path="/tmp/b.pdf",
        file_name="b.pdf",
        as_of_date=AS_OF,
    )
    assist_asset = AssetExtraction(
        row_memo_id="MEMO_MERGED_B",
        hits=[_hit(schema_by_header, "Implied EV ($M)", 545.0, method="llm:fake:sonnet:medium")],
    )
    assist_memo.assets = [assist_asset]

    merged = _merge_asset(
        primary_memo,
        primary_asset,
        [(primary_memo, primary_asset), (assist_memo, assist_asset)],
        default_config,
        schema_by_header,
        ruleset,
        routing_table,
        _NoPriorWriter(),
        derived_headers=set(),
    )

    assert single.qa_status is QaStatus.qa_pass
    assert merged.qa_status is QaStatus.qa_pass
    assert not any(flag.code == "no_valuation_value" for flag in merged.flags)
    assert _qa(merged)["Extraction Flags Count"] == len(merged.flags)
    assert _qa(merged)["Reviewer Attention"] == _qa(single)["Reviewer Attention"] == "N"
