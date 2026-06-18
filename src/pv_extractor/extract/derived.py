"""Derived/computed fields (D4).

THE RULE: a derived field is NEVER extracted when its inputs are present —
it is computed in Python from the extracted inputs, and an extracted value
that disagrees with the computation becomes a cross-check flag (the
extracted candidate is preserved on the hit's conflict list). Only when the
inputs are missing does an extracted value stand on its own.

Computed hits carry method="computed", evidence showing the arithmetic with
each input's page, and confidence = min(input confidences).

Specs run in declaration order, so later computations (NAV Change %) can
consume earlier computed results (NAV Change Abs).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pv_extractor.config import ValidationConfig
from pv_extractor.extract.bands.base import ExtractionContext
from pv_extractor.models import (
    ConflictingCandidate,
    FieldHit,
    FlagSeverity,
    ReviewFlag,
    SchemaField,
)

# Bridge attribution deltas (QoQ BRIDGE ATTRIBUTION band).
_BRIDGE_DELTAS = (
    "Δ Operating Performance ($M)",
    "Δ Multiple / Exit Assumption ($M)",
    "Δ Discount Rate / WACC ($M)",
    "Δ Capital Activity ($M)",
    "Δ FX ($M)",
    "Δ Time / Pull-to-Par ($M)",
    "Δ Methodology Change ($M)",
    "Δ Other ($M)",
)


@dataclass
class DerivedSpec:
    header: str
    inputs: tuple[str, ...]
    compute: Callable[..., float | bool | None]  # receives input values in order
    formula: str  # human-readable, for the evidence snippet
    optional_inputs: tuple[str, ...] = ()  # missing ones arrive as None


def _sub(a: float, b: float) -> float:
    return round(a - b, 6)


def _bps_change(current: float, prior: float) -> float:
    return round((current - prior) * 100.0, 4)


def derived_specs(validation: ValidationConfig) -> list[DerivedSpec]:
    def bridge_reconciles(nav_change: float, *deltas: float | None) -> bool | None:
        present = [delta for delta in deltas if delta is not None]
        if not present:
            return None
        gap = abs(sum(present) - nav_change)
        tolerance = max(abs(nav_change) * validation.bridge_tolerance_ratio, 0.5)
        return gap <= tolerance

    return [
        DerivedSpec(
            "EBITDA Margin %", ("EBITDA ($M)", "Revenue ($M)"),
            lambda ebitda, revenue: round(ebitda / revenue * 100.0, 4) if revenue else None,
            "EBITDA / Revenue x 100",
        ),
        DerivedSpec(
            "Mult Change (x)", ("Mult Selected (x)", "Mult Prior Qtr (x)"),
            _sub, "Mult Selected - Mult Prior Qtr",
        ),
        DerivedSpec(
            "Multiple Drift Since Entry", ("Mult Selected (x)", "Entry Multiple"),
            _sub, "Mult Selected - Entry Multiple",
        ),
        DerivedSpec(
            "NAV Change Abs ($M)", ("Fund Share Equity Value ($M)", "Prior Qtr NAV ($M)"),
            _sub, "Fund Share Equity Value - Prior Qtr NAV",
        ),
        DerivedSpec(
            "NAV Change %", ("NAV Change Abs ($M)", "Prior Qtr NAV ($M)"),
            lambda change, prior: round(change / prior * 100.0, 4) if prior else None,
            "NAV Change Abs / Prior Qtr NAV x 100",
        ),
        DerivedSpec(
            "FX Impact on NAV ($M)", ("Δ FX ($M)",),
            lambda fx: fx, "= Δ FX (QoQ bridge)",
        ),
        DerivedSpec(
            "DCF Discount Rate Change (bps)",
            ("DCF Discount Rate Mid %", "DCF Discount Rate Prior Qtr %"),
            _bps_change, "(Mid - Prior Qtr) x 100",
        ),
        DerivedSpec(
            "Yield YTM Change (bps)", ("Yield All-In YTM %", "Yield All-In YTM Prior Qtr %"),
            _bps_change, "(YTM - Prior Qtr YTM) x 100",
        ),
        DerivedSpec(
            "Cap Rate Change (bps)", ("Cap Rate Selected %", "Cap Rate Prior Qtr %"),
            _bps_change, "(Selected - Prior Qtr) x 100",
        ),
        DerivedSpec(
            "Yield Gap vs Market (bps)", ("Yield All-In YTM %", "Yield All-In Market Yield %"),
            _bps_change, "(YTM - Market Yield) x 100",
        ),
        DerivedSpec(
            "Bridge Reconciles Y/N", ("NAV Change Abs ($M)",),
            bridge_reconciles, "sum(Δs) ≈ NAV Change Abs",
            optional_inputs=_BRIDGE_DELTAS,
        ),
    ]


def _numeric(hit: FieldHit | None) -> float | None:
    if hit is None or not isinstance(hit.value, (int, float)) or isinstance(hit.value, bool):
        return None
    return float(hit.value)


def apply_derived(
    hits: list[FieldHit],
    schema_by_header: dict[str, SchemaField],
    validation: ValidationConfig,
    ctx: ExtractionContext,
) -> list[FieldHit]:
    """Run every DerivedSpec over the hit list; returns the updated list."""
    by_header: dict[str, FieldHit] = {hit.field: hit for hit in hits}

    for spec in derived_specs(validation):
        field = schema_by_header.get(spec.header)
        if field is None:
            continue
        input_hits = [by_header.get(name) for name in spec.inputs]
        input_values = [_numeric(hit) for hit in input_hits]
        optional_hits = [by_header.get(name) for name in spec.optional_inputs]
        optional_values = [_numeric(hit) for hit in optional_hits]

        if any(value is None for value in input_values):
            continue  # inputs missing: an extracted value (if any) stands
        computed = spec.compute(*input_values, *optional_values)
        if computed is None:
            continue

        used_hits = [hit for hit in (*input_hits, *optional_hits) if hit is not None]
        confidence = round(min(hit.confidence for hit in used_hits), 4)
        evidence = f"{spec.formula} = " + ", ".join(
            f"{hit.field}={hit.value} (p{hit.page})" for hit in used_hits if hit.field in spec.inputs
        )

        existing = by_header.get(spec.header)
        conflicts: list[ConflictingCandidate] = []
        if existing is not None and existing.method == "deterministic":
            if _mismatch(existing.value, computed, validation):
                ctx.flags.append(
                    ReviewFlag(
                        category="computed_crosscheck",
                        description=(
                            f"{spec.header}: extracted value {existing.value!r} disagrees with "
                            f"computed {computed!r} ({spec.formula}); computed value kept"
                        ),
                        severity=FlagSeverity.warning,
                        reviewer_attention=True,
                        field=spec.header,
                    )
                )
            conflicts = [
                ConflictingCandidate(
                    raw_text=existing.raw_text, value=existing.value, page=existing.page,
                    confidence=existing.confidence, evidence=existing.evidence,
                ),
                *existing.conflicts,
            ]

        computed_hit = FieldHit(
            field=spec.header,
            col_index=field.col_index,
            band=field.band,
            raw_text="",
            value=computed,
            unit=field.unit,
            method="computed",
            confidence=confidence,
            evidence=evidence[:200],
            confidence_components={"computed_from_inputs": confidence},
            conflicts=conflicts,
        )
        by_header[spec.header] = computed_hit
        # A parse warning on a field that ended up computed is moot noise
        # (e.g. the bridge table's abs row teasing the % field's labels).
        ctx.flags[:] = [
            flag for flag in ctx.flags
            if not (flag.category == "parse" and flag.field == spec.header)
        ]

    # preserve schema column order
    return sorted(by_header.values(), key=lambda hit: hit.col_index)


def _mismatch(extracted, computed, validation: ValidationConfig) -> bool:
    if isinstance(computed, bool) or isinstance(extracted, bool):
        return bool(extracted) != bool(computed)
    if not isinstance(extracted, (int, float)):
        return True
    scale = max(abs(float(computed)), 1.0)
    return abs(float(extracted) - float(computed)) > validation.computed_crosscheck_tolerance * scale
