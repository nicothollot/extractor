from __future__ import annotations

from datetime import date

import pytest

from pv_extractor.config import CandidateArbitrationConfig
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.arbitration import Candidate, arbitrate_field

FIELDS = {field.header: field for field in load_schema_fields()}
CFG = CandidateArbitrationConfig()


def cand(
    field: str,
    value,
    confidence: float,
    *,
    doc: str = "D01",
    as_of: str = "2026-03-31",
    grounded: bool = True,
):
    return Candidate(
        field=field,
        value=value,
        unit=FIELDS[field].unit,
        document_id=doc,
        page=1,
        as_of_date=as_of,
        quote=f"{field}: {value}",
        evidence_kind="explicit_label",
        raw_model_confidence=confidence,
        provider="claude",
        model="sonnet",
        effort="medium",
        prompt_version="assist-sparse-v5-2026-06-24",
        grounding_status="grounded" if grounded else "ungrounded",
    )


def test_eligible_085_beats_030_without_repair():
    decision = arbitrate_field(
        "Implied EV ($M)",
        [
            cand("Implied EV ($M)", 100.0, 0.85, doc="D01"),
            cand("Implied EV ($M)", 92.0, 0.30, doc="D02"),
        ],
        config=CFG,
        schema_field=FIELDS["Implied EV ($M)"],
        target_as_of=date(2026, 3, 31),
    )
    assert decision.selected and decision.selected.value == 100.0
    assert decision.decision == "accepted_confidence_winner"
    assert decision.margin == pytest.approx(0.55)


def test_wrong_period_095_loses_to_current_period_072():
    decision = arbitrate_field(
        "Implied EV ($M)",
        [
            cand("Implied EV ($M)", 100.0, 0.95, as_of="2025-12-31"),
            cand("Implied EV ($M)", 92.0, 0.72, as_of="2026-03-31"),
        ],
        config=CFG,
        schema_field=FIELDS["Implied EV ($M)"],
        target_as_of=date(2026, 3, 31),
    )
    assert decision.selected and decision.selected.value == 92.0
    assert decision.final_confidence == pytest.approx(0.72)


def test_ungrounded_099_is_ineligible():
    decision = arbitrate_field(
        "Implied EV ($M)",
        [cand("Implied EV ($M)", 100.0, 0.99, grounded=False)],
        config=CFG,
        schema_field=FIELDS["Implied EV ($M)"],
        target_as_of=date(2026, 3, 31),
    )
    assert decision.selected is None
    assert decision.decision == "unresolved_no_eligible_candidate"


def test_close_085_vs_082_is_unresolved():
    decision = arbitrate_field(
        "Implied EV ($M)",
        [
            cand("Implied EV ($M)", 100.0, 0.85, doc="D01"),
            cand("Implied EV ($M)", 92.0, 0.82, doc="D02"),
        ],
        config=CFG,
        schema_field=FIELDS["Implied EV ($M)"],
        target_as_of=date(2026, 3, 31),
    )
    assert decision.selected is None
    assert decision.decision == "unresolved_close_conflict"


def test_same_value_gets_capped_agreement_bonus():
    decision = arbitrate_field(
        "Implied EV ($M)",
        [
            cand("Implied EV ($M)", 100.0, 0.76, doc="D01"),
            cand("Implied EV ($M)", 100.0, 0.71, doc="D02"),
        ],
        config=CFG,
        schema_field=FIELDS["Implied EV ($M)"],
        target_as_of=date(2026, 3, 31),
    )
    assert decision.selected and decision.selected.value == 100.0
    assert decision.decision == "accepted_agreement_winner"
    assert decision.agreement_bonus == pytest.approx(0.05)
    assert decision.final_confidence == pytest.approx(0.81)
