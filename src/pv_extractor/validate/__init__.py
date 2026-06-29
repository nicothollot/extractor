"""Validation & QA assembly (D5): runs on the assembled row before writing.

validate_asset chains the layers —

  1. schema checks: type / controlled vocab / range (checks.py)
  2. table-driven cross-field rules from rules.yaml (rules.py)
  3. QoQ continuity vs the prior-period row -> THRESHOLD FLAGS (qoq.py)
  4. hard failures: no valuation value found, in-file as-of mismatch
  5. QA verdict: qa_fail (any hard failure) / qa_pass_with_flags / qa_pass,
     plus the QA band cells (QA Status, Extraction Flags Count, Reviewer
     Attention) appended as computed hits
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pv_extractor.config import Config
from pv_extractor.models import (
    FieldHit,
    FlagSeverity,
    QaStatus,
    ReviewFlag,
    SchemaField,
    VerifyResult,
)
from pv_extractor.indexer.periods import period_label
from pv_extractor.validate.checks import check_hits
from pv_extractor.validate.flags import deduplicate_review_flags
from pv_extractor.validate.qoq import qoq_checks
from pv_extractor.validate.rules import RuleSet, load_rules, run_rules

__all__ = ["ValidatedAsset", "load_rules", "RuleSet", "validate_asset"]

# A memo with NONE of these populated has no valuation value -> qa_fail.
_VALUATION_VALUE_HEADERS = (
    "Fund Share Equity Value ($M)",
    "Implied Equity Value 100% ($M)",
    "Implied EV ($M)",
    "WF Selected Value ($M)",
    "Cap Implied Asset Value ($M, USD)",
    "Cap Implied Asset Value ($M, local)",
    "Yield Cost + Accrued ($M, local)",
)


def _same_reporting_period(
    asof_date: date, target: date, config: Config, client: str | None
) -> bool:
    """True when the document's in-file as-of and the target fall in the SAME
    reporting period under the client's cadence (same quarter for quarterly
    clients, same month for monthly). The period selector chooses the quarter/
    year, not an exact day — a genuine Q2 document dated Apr/May must satisfy a
    Q2 (Jun-30) target. Mirrors the peek-verifier's same-period tolerance and
    is likewise gated by locator.tolerate_same_period (False = strict exact
    date)."""
    if not config.locator.tolerate_same_period:
        return False
    style = config.client_period_style(client or "default")
    return period_label(asof_date, style) == period_label(target, style)


@dataclass
class ValidatedAsset:
    hits: list[FieldHit]  # including THRESHOLD FLAGS + QA band cells
    flags: list[ReviewFlag]
    qa_status: QaStatus


def validate_asset(
    *,
    hits: list[FieldHit],
    extraction_flags: list[ReviewFlag],
    schema_by_header: dict[str, SchemaField],
    config: Config,
    ruleset: RuleSet,
    routing_table: dict[str, list[str]],
    as_of_date: date | None,
    verify: VerifyResult | None,
    prior_row: dict[str, object] | None,
    client: str | None = None,
) -> ValidatedAsset:
    """Return a validated copy of `hits`; input hit/flag lists are not mutated."""
    validation = config.validation
    flags: list[ReviewFlag] = list(extraction_flags)

    flags.extend(check_hits(hits, schema_by_header, validation, as_of_date, ruleset.ranges))
    flags.extend(run_rules(hits, ruleset, routing_table))

    threshold_hits, qoq_flags = qoq_checks(hits, prior_row, schema_by_header, validation)
    flags.extend(qoq_flags)
    all_hits = [*hits, *threshold_hits]

    # --- hard failures ---
    populated = {hit.field for hit in all_hits if hit.value is not None}
    # This hard-fail only applies to the MASTER schema, whose value headers are
    # below. A CUSTOM reference workbook (e.g. GEDP: "EV - LOW", "MVE - LOW", …)
    # does not contain these headers at all, so the check is meaningless there —
    # skip it rather than fail every custom run. Only enforce when at least one
    # of the headers is actually part of this run's schema.
    valuation_headers = set(_VALUATION_VALUE_HEADERS) & set(schema_by_header)
    if valuation_headers and not (populated & valuation_headers):
        flags.append(
            ReviewFlag(
                category="qa",
                description="no valuation value found (none of the headline/methodology value fields populated)",
                severity=FlagSeverity.hard_fail,
                reviewer_attention=True,
                origin="qa",
                code="no_valuation_value",
            )
        )
    if (
        verify is not None
        and verify.asof_date is not None
        and as_of_date is not None
        and verify.asof_date != as_of_date
        and not _same_reporting_period(verify.asof_date, as_of_date, config, client)
    ):
        flags.append(
            ReviewFlag(
                category="qa",
                description=(
                    f"as-of date inside the document ({verify.asof_date.isoformat()}) does not match "
                    f"the target period ({as_of_date.isoformat()})"
                ),
                severity=FlagSeverity.hard_fail,
                reviewer_attention=True,
                origin="qa",
                code="asof_mismatch",
            )
        )

    flags = deduplicate_review_flags(flags)

    if any(flag.severity is FlagSeverity.hard_fail for flag in flags):
        qa_status = QaStatus.qa_fail
    elif flags:
        qa_status = QaStatus.qa_pass_with_flags
    else:
        qa_status = QaStatus.qa_pass

    for header, value in (
        ("QA Status", qa_status.value),
        ("Extraction Flags Count", len(flags)),
        ("Reviewer Attention", "Y" if any(f.reviewer_attention for f in flags) else "N"),
    ):
        field = schema_by_header.get(header)
        if field is not None:
            all_hits.append(
                FieldHit(
                    field=header, col_index=field.col_index, band=field.band,
                    value=value, method="computed", confidence=1.0,
                    evidence="QA assembly",
                )
            )

    all_hits.sort(key=lambda hit: hit.col_index)
    return ValidatedAsset(hits=all_hits, flags=flags, qa_status=qa_status)
