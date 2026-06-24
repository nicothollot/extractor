"""Local, auditable arbitration for LLM candidates.

The provider's model_confidence is useful only after objective gates pass. This
module deliberately keeps those gates explicit rather than hiding them in a
weighted score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pv_extractor.config import CandidateArbitrationConfig
from pv_extractor.models import SchemaField


def calibrator(
    provider: str,
    model: str,
    effort: str,
    prompt_version: str,
    raw_confidence: float,
) -> float:
    """Calibration hook.

    Initial implementation is identity. Persisting provider/model/effort and
    reviewer outcomes lets this become binned or isotonic calibration later
    without changing the merge API.
    """
    _ = (provider, model, effort, prompt_version)
    return raw_confidence


@dataclass(frozen=True)
class Candidate:
    field: str
    value: Any
    unit: str | None
    document_id: str
    page: int | None
    as_of_date: str | None
    quote: str
    evidence_kind: str
    raw_model_confidence: float
    provider: str = ""
    model: str = ""
    effort: str = ""
    prompt_version: str = ""
    grounding_status: str = "grounded"
    source: str = "primary"
    candidate_id: str = ""

    @classmethod
    def from_result(
        cls,
        field: str,
        result: dict,
        *,
        provider: str = "",
        model: str = "",
        effort: str = "",
        prompt_version: str = "",
        grounding_status: str = "grounded",
        source: str = "primary",
    ) -> "Candidate":
        raw = result.get("model_confidence", result.get("raw_model_confidence", result.get("confidence", 0.0)))
        raw_float = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
        return cls(
            field=field,
            value=result.get("value"),
            unit=result.get("unit"),
            document_id=str(result.get("document_id") or ""),
            page=result.get("page") if isinstance(result.get("page"), int) else None,
            as_of_date=result.get("as_of_date") if isinstance(result.get("as_of_date"), str) else None,
            quote=str(result.get("quote") or result.get("verbatim_quote") or ""),
            evidence_kind=str(result.get("evidence_kind") or ""),
            raw_model_confidence=raw_float,
            provider=provider,
            model=model,
            effort=effort,
            prompt_version=prompt_version,
            grounding_status=grounding_status,
            source=source,
            candidate_id=str(result.get("candidate_id") or ""),
        )


@dataclass
class CandidateDecision:
    field: str
    selected: Candidate | None
    eligible: list[Candidate] = field(default_factory=list)
    ineligible: list[tuple[Candidate, str]] = field(default_factory=list)
    decision: str = "unresolved"
    decision_reason: str = ""
    selected_candidate_id: str | None = None
    raw_model_confidence: float = 0.0
    calibrated_confidence: float = 0.0
    agreement_bonus: float = 0.0
    final_confidence: float = 0.0
    runner_up_confidence: float = 0.0
    margin: float = 0.0


def _same_period(candidate_as_of: str | None, target_as_of: date | str | None) -> bool:
    if target_as_of is None or not candidate_as_of:
        return True
    target = target_as_of.isoformat() if isinstance(target_as_of, date) else str(target_as_of)
    return candidate_as_of[:10] == target[:10]


def _normalized_value(value: Any, unit: str | None) -> tuple[str, str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        key = f"{float(value):.6g}"
    else:
        key = str(value).strip().casefold()
    return key, (unit or "").strip().casefold()


def _type_vocab_ok(candidate: Candidate, schema_field: SchemaField | None) -> tuple[bool, str]:
    if schema_field is None:
        return True, ""
    value = candidate.value
    if schema_field.unit and candidate.unit and candidate.unit != schema_field.unit:
        return False, "unit mismatch"
    if schema_field.controlled_vocab:
        text = str(value).strip().casefold()
        allowed = {item.strip().casefold() for item in schema_field.controlled_vocab}
        if text not in allowed:
            return False, "controlled vocabulary mismatch"
    if schema_field.dtype in {"number", "percent", "basis_points", "multiple_x", "years", "integer"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "type mismatch"
    if schema_field.dtype == "boolean" and not isinstance(value, bool):
        return False, "type mismatch"
    if schema_field.dtype in {"string", "date", "enum"} and value is None:
        return False, "missing value"
    return True, ""


def _eligible(
    candidate: Candidate,
    *,
    schema_field: SchemaField | None,
    config: CandidateArbitrationConfig,
    target_as_of: date | str | None,
    document_ids: set[str] | None,
) -> tuple[bool, str]:
    if (
        not math.isfinite(candidate.raw_model_confidence)
        or candidate.raw_model_confidence < 0.0
        or candidate.raw_model_confidence > 1.0
    ):
        return False, "confidence outside [0,1]"
    if candidate.page is None or candidate.page <= 0:
        return False, "missing cited page"
    if document_ids is not None and candidate.document_id not in document_ids:
        return False, "document outside deal-period"
    if not _same_period(candidate.as_of_date, target_as_of):
        return False, "wrong reporting period"
    if config.require_grounded_evidence and candidate.grounding_status != "grounded":
        return False, "ungrounded evidence"
    ok, reason = _type_vocab_ok(candidate, schema_field)
    if not ok:
        return False, reason
    return True, ""


def arbitrate_field(
    field: str,
    candidates: list[Candidate],
    *,
    config: CandidateArbitrationConfig,
    schema_field: SchemaField | None = None,
    target_as_of: date | str | None = None,
    document_ids: set[str] | None = None,
) -> CandidateDecision:
    if not config.enabled:
        first = candidates[0] if candidates else None
        return CandidateDecision(
            field=field,
            selected=first,
            eligible=list(candidates),
            decision="accepted_first_candidate" if first else "unresolved",
            decision_reason="candidate arbitration disabled",
            selected_candidate_id=first.candidate_id if first else None,
        )

    eligible: list[Candidate] = []
    ineligible: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        ok, reason = _eligible(
            candidate,
            schema_field=schema_field,
            config=config,
            target_as_of=target_as_of,
            document_ids=document_ids,
        )
        if ok:
            eligible.append(candidate)
        else:
            ineligible.append((candidate, reason))

    if not eligible:
        return CandidateDecision(
            field=field,
            selected=None,
            eligible=[],
            ineligible=ineligible,
            decision="unresolved_no_eligible_candidate",
            decision_reason="no candidate passed hard eligibility rules",
        )

    groups: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in eligible:
        groups.setdefault(_normalized_value(candidate.value, candidate.unit), []).append(candidate)

    ranked: list[tuple[float, float, list[Candidate]]] = []
    for group in groups.values():
        scored = [
            (
                calibrator(c.provider, c.model, c.effort, c.prompt_version, c.raw_model_confidence),
                c,
            )
            for c in group
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        base_score, _best = scored[0]
        independent_docs = {candidate.document_id for candidate in group if candidate.document_id}
        bonus = min(
            max(0, len(independent_docs) - 1) * config.agreement_bonus_per_extra_document,
            config.max_agreement_bonus,
        )
        ranked.append((base_score + bonus, bonus, group))

    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, bonus, best_group = ranked[0]
    best_group_sorted = sorted(
        best_group,
        key=lambda c: calibrator(c.provider, c.model, c.effort, c.prompt_version, c.raw_model_confidence),
        reverse=True,
    )
    selected = best_group_sorted[0]
    selected_calibrated = calibrator(
        selected.provider,
        selected.model,
        selected.effort,
        selected.prompt_version,
        selected.raw_model_confidence,
    )
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = best_score - runner_up

    if best_score < config.min_accept_confidence:
        decision = "unresolved_below_min_confidence"
        reason = f"final confidence {best_score:.2f} is below minimum {config.min_accept_confidence:.2f}"
        selected_out = None
    elif margin < config.min_winner_margin:
        decision = "unresolved_close_conflict"
        reason = f"winner margin {margin:.2f} is below minimum {config.min_winner_margin:.2f}"
        selected_out = None
    elif bonus > 0:
        decision = "accepted_agreement_winner"
        reason = (
            f"independent documents support the same normalized value; "
            f"agreement bonus {bonus:.2f}, margin {margin:.2f}"
        )
        selected_out = selected
    else:
        decision = "accepted_confidence_winner"
        reason = f"eligible grounded candidate won by confidence margin {margin:.2f}"
        selected_out = selected

    return CandidateDecision(
        field=field,
        selected=selected_out,
        eligible=eligible,
        ineligible=ineligible,
        decision=decision,
        decision_reason=reason,
        selected_candidate_id=selected.candidate_id or None,
        raw_model_confidence=selected.raw_model_confidence,
        calibrated_confidence=selected_calibrated,
        agreement_bonus=bonus,
        final_confidence=best_score,
        runner_up_confidence=runner_up,
        margin=margin,
    )
