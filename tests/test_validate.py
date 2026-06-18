"""D5 validation tests: schema checks, cross-field rules, QoQ thresholds,
hard failures and QA status assembly."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pv_extractor.models import FieldHit, FlagSeverity, QaStatus, SchemaField, VerifyResult
from pv_extractor.validate import load_rules, validate_asset
from pv_extractor.validate.qoq import qoq_checks

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


def _hit(schema_by_header, header: str, value, confidence: float = 0.9) -> FieldHit:
    field = schema_by_header[header]
    return FieldHit(
        field=header, col_index=field.col_index, band=field.band,
        raw_text=str(value), value=value, confidence=confidence,
    )


def _validate(schema_by_header, ruleset, routing_table, default_config, hits, prior_row=None, verify=None):
    return validate_asset(
        hits=hits, extraction_flags=[], schema_by_header=schema_by_header,
        config=default_config, ruleset=ruleset, routing_table=routing_table,
        as_of_date=AS_OF, verify=verify, prior_row=prior_row,
    )


def _clean_hits(schema_by_header) -> list[FieldHit]:
    return [
        _hit(schema_by_header, "Implied EV ($M)", 545.0),
        _hit(schema_by_header, "Net Debt ($M)", 120.0),
        _hit(schema_by_header, "Implied Equity Value 100% ($M)", 425.0),
        _hit(schema_by_header, "Fund Share Equity Value ($M)", 106.3),
        _hit(schema_by_header, "Primary Methodology", "Multiple-Market"),
        _hit(schema_by_header, "Primary Method Weight %", 100.0),
        _hit(schema_by_header, "Mult Selected (x)", 8.5),
    ]


def test_clean_row_is_qa_pass(schema_by_header, ruleset, routing_table, default_config) -> None:
    result = _validate(schema_by_header, ruleset, routing_table, default_config, _clean_hits(schema_by_header))
    assert result.qa_status is QaStatus.qa_pass
    assert [f for f in result.flags] == []
    qa = {h.field: h.value for h in result.hits if h.band == "QA"}
    assert qa == {"QA Status": "qa_pass", "Extraction Flags Count": 0, "Reviewer Attention": "N"}
    # no prior row -> New to Portfolio
    new_flag = next(h for h in result.hits if h.field == "New to Portfolio")
    assert new_flag.value is True


def test_percent_range_and_vocab_checks(schema_by_header, ruleset, routing_table, default_config) -> None:
    hits = _clean_hits(schema_by_header) + [
        _hit(schema_by_header, "Gross IRR %", 350.0),  # outside [-100, 200]
        _hit(schema_by_header, "DLOM %", 75.0),  # rules.yaml override caps at 60
        _hit(schema_by_header, "Investment Close Date", "1987-01-01"),  # before 2000
    ]
    result = _validate(schema_by_header, ruleset, routing_table, default_config, hits)
    descriptions = "\n".join(f.description for f in result.flags)
    assert "Gross IRR %" in descriptions and "outside" in descriptions
    assert "DLOM %" in descriptions
    assert "1987-01-01" in descriptions
    assert result.qa_status is QaStatus.qa_pass_with_flags


def test_weights_sum_rule(schema_by_header, ruleset, routing_table, default_config) -> None:
    hits = _clean_hits(schema_by_header)
    hits[5] = _hit(schema_by_header, "Primary Method Weight %", 70.0)
    hits.append(_hit(schema_by_header, "Secondary Method Weight %", 20.0))
    result = _validate(schema_by_header, ruleset, routing_table, default_config, hits)
    assert any("method weights sum to 90" in f.description for f in result.flags)


def test_equity_bridge_rule(schema_by_header, ruleset, routing_table, default_config) -> None:
    hits = _clean_hits(schema_by_header)
    hits[2] = _hit(schema_by_header, "Implied Equity Value 100% ($M)", 300.0)  # EV-ND=425
    result = _validate(schema_by_header, ruleset, routing_table, default_config, hits)
    assert any("equity_bridge" in f.description for f in result.flags)


def test_routing_consistency_rule(schema_by_header, ruleset, routing_table, default_config) -> None:
    hits = _clean_hits(schema_by_header) + [
        _hit(schema_by_header, "DCF Discount Rate Mid %", 8.5),  # DCF band populated, not routed
    ]
    result = _validate(schema_by_header, ruleset, routing_table, default_config, hits)
    assert any(
        "METHODOLOGY: DCF" in f.description and "not routed" in f.description for f in result.flags
    )


def test_no_valuation_value_is_hard_fail(schema_by_header, ruleset, routing_table, default_config) -> None:
    hits = [_hit(schema_by_header, "Revenue ($M)", 410.0)]
    result = _validate(schema_by_header, ruleset, routing_table, default_config, hits)
    assert result.qa_status is QaStatus.qa_fail
    assert any(f.severity is FlagSeverity.hard_fail and "no valuation value" in f.description for f in result.flags)


def test_asof_mismatch_is_hard_fail(schema_by_header, ruleset, routing_table, default_config) -> None:
    verify = VerifyResult(asof_date=date(2025, 12, 31))
    result = _validate(
        schema_by_header, ruleset, routing_table, default_config, _clean_hits(schema_by_header), verify=verify
    )
    assert result.qa_status is QaStatus.qa_fail
    assert any("does not match the target period" in f.description for f in result.flags)


# --------------------------------------------------------------------------
# QoQ continuity
# --------------------------------------------------------------------------


def test_qoq_threshold_flags(schema_by_header, default_config) -> None:
    hits = [
        _hit(schema_by_header, "DCF Discount Rate Mid %", 13.5),
        _hit(schema_by_header, "Mult Selected (x)", 9.2),
        _hit(schema_by_header, "Fund Share Equity Value ($M)", 120.0),
    ]
    prior = {
        "DCF Discount Rate Mid %": 13.0,  # +50bps == threshold, NOT > -> no flag
        "Mult Selected (x)": 8.5,  # +0.7x > 0.5x -> flag
        "Fund Share Equity Value ($M)": 100.0,  # +20% > 5% -> flag
    }
    threshold_hits, flags = qoq_checks(hits, prior, schema_by_header, default_config.validation)
    values = {h.field: h.value for h in threshold_hits}
    assert values["New to Portfolio"] is False
    assert values["WACC >50bps QoQ"] is False  # exactly at threshold is not a breach
    assert values["Multiple >0.5x QoQ"] is True
    assert values["NAV >5% QoQ"] is True
    assert sum(1 for f in flags if f.category == "qoq_threshold") == 2

    prior["DCF Discount Rate Mid %"] = 12.9  # +60bps -> flag
    threshold_hits, flags = qoq_checks(hits, prior, schema_by_header, default_config.validation)
    values = {h.field: h.value for h in threshold_hits}
    assert values["WACC >50bps QoQ"] is True
    assert any("wacc_gt_50bps_qoq" in f.description for f in flags)
