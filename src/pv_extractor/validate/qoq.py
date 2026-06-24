"""QoQ continuity checks (D5): current row vs the prior-period row of the
SAME asset (matched on Portfolio Company + Fund Name in the output workbook).

Sets the THRESHOLD FLAGS columns the deltas drive:
  * New to Portfolio        no prior-period row exists
  * WACC >50bps QoQ         any rate field (DCF mid / yield YTM / cap rate)
                            moved more than validation.wacc_qoq_threshold_bps
  * Multiple >0.5x QoQ      Mult Selected moved more than the configured x
  * NAV >5% QoQ             fund-share NAV moved more than the configured %
  * Partial Realization     Realized Value increased vs prior
  * Follow-On Investment    Total Invested Capital increased vs prior

Each set flag also raises a ReviewFlag for the Review Flags sheet.
"""

from __future__ import annotations

from pv_extractor.config import ValidationConfig
from pv_extractor.models import FieldHit, FlagSeverity, ReviewFlag, SchemaField

# Rate fields checked for the 'WACC >50bps QoQ' flag — one per methodology
# family (the reference workbook flags yield-rate moves under the same flag).
_RATE_HEADERS = ("DCF Discount Rate Mid %", "Yield All-In YTM %", "Cap Rate Selected %")

_NAV_HEADER = "Fund Share Equity Value ($M)"
_MULT_HEADER = "Mult Selected (x)"
_REALIZED_HEADER = "Realized Value ($M)"
_INVESTED_HEADER = "Total Invested Capital ($M)"


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def qoq_checks(
    hits: list[FieldHit],
    prior_row: dict[str, object] | None,
    schema_by_header: dict[str, SchemaField],
    validation: ValidationConfig,
) -> tuple[list[FieldHit], list[ReviewFlag]]:
    """(THRESHOLD FLAGS hits, review flags) from current-vs-prior deltas."""
    current = {hit.field: hit.value for hit in hits}
    threshold_values: dict[str, bool] = {}
    flags: list[ReviewFlag] = []

    if prior_row is None:
        threshold_values["New to Portfolio"] = True
    else:
        threshold_values["New to Portfolio"] = False

        for header in _RATE_HEADERS:
            cur, prior = _numeric(current.get(header)), _numeric(prior_row.get(header))
            if cur is None or prior is None:
                continue
            delta_bps = (cur - prior) * 100.0
            if abs(delta_bps) > validation.wacc_qoq_threshold_bps:
                threshold_values["WACC >50bps QoQ"] = True
                flags.append(
                    ReviewFlag(
                        category="qoq_threshold",
                        description=(
                            f"{header} moved {delta_bps:+.0f}bps QoQ ({prior:g}% → {cur:g}%) — "
                            f"flagged as wacc_gt_50bps_qoq"
                        ),
                        severity=FlagSeverity.warning,
                        reviewer_attention=True,
                        field=header,
                        origin="validation",
                        code="wacc_gt_50bps_qoq",
                    )
                )
        threshold_values.setdefault("WACC >50bps QoQ", False)

        cur, prior = _numeric(current.get(_MULT_HEADER)), _numeric(prior_row.get(_MULT_HEADER))
        if cur is not None and prior is not None:
            delta = cur - prior
            moved = abs(delta) > validation.multiple_qoq_threshold_x
            threshold_values["Multiple >0.5x QoQ"] = moved
            if moved:
                flags.append(
                    ReviewFlag(
                        category="qoq_threshold",
                        description=(
                            f"{_MULT_HEADER} moved {delta:+.2f}x QoQ ({prior:g}x → {cur:g}x) — "
                            f"flagged as multiple_gt_0_5x_qoq"
                        ),
                        severity=FlagSeverity.warning,
                        reviewer_attention=True,
                        field=_MULT_HEADER,
                        origin="validation",
                        code="multiple_gt_0_5x_qoq",
                    )
                )

        cur, prior = _numeric(current.get(_NAV_HEADER)), _numeric(prior_row.get(_NAV_HEADER))
        if cur is not None and prior not in (None, 0.0):
            change_pct = (cur - prior) / abs(prior) * 100.0
            moved = abs(change_pct) > validation.nav_qoq_threshold_pct
            threshold_values["NAV >5% QoQ"] = moved
            if moved:
                flags.append(
                    ReviewFlag(
                        category="qoq_threshold",
                        description=(
                            f"NAV moved {change_pct:+.1f}% QoQ (${prior:g}M → ${cur:g}M) — "
                            f"flagged as nav_gt_5pct_qoq"
                        ),
                        severity=FlagSeverity.warning,
                        reviewer_attention=True,
                        field=_NAV_HEADER,
                        origin="validation",
                        code="nav_gt_5pct_qoq",
                    )
                )

        cur, prior = _numeric(current.get(_REALIZED_HEADER)), _numeric(prior_row.get(_REALIZED_HEADER))
        if cur is not None and prior is not None and cur > prior:
            threshold_values["Partial Realization"] = True

        cur, prior = _numeric(current.get(_INVESTED_HEADER)), _numeric(prior_row.get(_INVESTED_HEADER))
        if cur is not None and prior is not None and cur > prior:
            threshold_values["Follow-On Investment"] = True

    threshold_hits: list[FieldHit] = []
    for header, value in threshold_values.items():
        field = schema_by_header.get(header)
        if field is None:
            continue
        threshold_hits.append(
            FieldHit(
                field=header,
                col_index=field.col_index,
                band=field.band,
                value=value,
                method="computed",
                confidence=1.0,
                evidence="QoQ continuity check vs prior-period row"
                if prior_row is not None
                else "no prior-period row for this asset",
            )
        )
    return threshold_hits, flags
