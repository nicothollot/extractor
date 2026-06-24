"""Unit tests for run._build_escalation — specifically the broadening that
fixes the bug where a memo the deterministic engine could not recognize came
back with an EMPTY escalation plan (status not_needed) and so was never sent to
the LLM. See run.py._build_escalation."""

from __future__ import annotations

from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.models import AssetExtraction, FieldHit, QaStatus
from pv_extractor.run import _build_escalation

SCHEMA_FIELDS = load_schema_fields()
BY_HEADER = {f.header: f for f in SCHEMA_FIELDS}

# A canonical valuation-value field — present in the schema, NOT marked required
# (which is exactly why the old plan stayed empty when it was missing).
VALUE_HEADER = "Implied EV ($M)"


def _hit(header: str, value, confidence: float) -> FieldHit:
    field = BY_HEADER[header]
    return FieldHit(
        field=header, col_index=field.col_index, band=field.band,
        value=value, confidence=confidence, method="deterministic",
        page=1, evidence="x", raw_text="x",
    )


def _asset(qa_status: QaStatus, hits: list[FieldHit]) -> AssetExtraction:
    return AssetExtraction(
        asset_name=None, row_memo_id="MEMO_X_001", hits=hits,
        flags=[], qa_status=qa_status,
    )


def test_qa_fail_with_no_value_hits_still_escalates_value_fields():
    """The IBMG case: nothing recognized, no low-conf hits, value field empty
    and not required. The old code produced fields=[]; now QA-fail broadens."""
    asset = _asset(QaStatus.qa_fail, hits=[])
    plan = _build_escalation(
        "MEMO_X_001", [asset], BY_HEADER, {}, 0.75,
        force_assist=False, derived_headers=set(),
    )
    assert plan.fields, "qa_fail asset must produce a non-empty escalation plan"
    headers = {f.field for f in plan.fields}
    assert VALUE_HEADER in headers
    assert all(f.reason in ("required_empty", "qa_fail_rescue") for f in plan.fields)


def test_qa_pass_without_force_uses_primary_catalog_not_broad_rescue():
    """A clean pass still builds the LLM-extractable primary catalog, but does
    not use broad rescue/force reasons."""
    asset = _asset(QaStatus.qa_pass, hits=[_hit(VALUE_HEADER, 100.0, 0.95)])
    plan = _build_escalation(
        "MEMO_X_001", [asset], BY_HEADER, {}, 0.75,
        force_assist=False, derived_headers=set(),
    )
    reasons = {f.reason for f in plan.fields}
    assert "qa_fail_rescue" not in reasons
    assert "force_llm_assist" not in reasons
    assert VALUE_HEADER in {f.field for f in plan.fields}
    assert reasons <= {"primary_catalog", "below_confidence", "required_empty"}


def test_force_assist_escalates_even_on_clean_pass():
    """Force LLM assist: the analyst wants the LLM to extract regardless of QA."""
    asset = _asset(QaStatus.qa_pass, hits=[_hit(VALUE_HEADER, 100.0, 0.95)])
    plan = _build_escalation(
        "MEMO_X_001", [asset], BY_HEADER, {}, 0.75,
        force_assist=True, derived_headers=set(),
    )
    headers = {f.field for f in plan.fields}
    # The populated value field is included in the primary catalog, while other
    # empty extractable fields are tagged force_llm_assist.
    assert plan.fields
    assert VALUE_HEADER in headers
    assert any(f.reason == "force_llm_assist" for f in plan.fields)


def test_broad_escalation_excludes_metadata_qa_and_derived_bands():
    """The BROAD additions (qa_fail_rescue / force_llm_assist) never target the
    non-extractable bands, derived fields, or positional slots. (required_empty
    fields, an orthogonal pre-existing path, may still appear in any band.)"""
    derived = {"EBITDA Margin %"}  # a real derived header
    asset = _asset(QaStatus.qa_fail, hits=[])
    plan = _build_escalation(
        "MEMO_X_001", [asset], BY_HEADER, {}, 0.75,
        force_assist=True, derived_headers=derived,
    )
    broad = [f for f in plan.fields if f.reason in ("qa_fail_rescue", "force_llm_assist")]
    assert broad
    bands = {f.band for f in broad}
    assert "IDENTIFICATION" not in bands
    assert "QA" not in bands
    assert "THRESHOLD FLAGS" not in bands
    assert all(BY_HEADER[f.field].slot_group is None for f in broad)
    assert all(f.field not in derived for f in broad)


def test_low_confidence_hit_escalates_regardless_of_qa():
    asset = _asset(QaStatus.qa_pass_with_flags, hits=[_hit(VALUE_HEADER, 100.0, 0.2)])
    plan = _build_escalation(
        "MEMO_X_001", [asset], BY_HEADER, {}, 0.75,
        force_assist=False, derived_headers=set(),
    )
    headers = {f.field for f in plan.fields}
    assert VALUE_HEADER in headers
    assert any(f.reason == "below_confidence" for f in plan.fields)
