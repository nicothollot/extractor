"""Band specs (D4): QoQ BRIDGE ATTRIBUTION — the quarter-over-quarter NAV
bridge. The Δ rows usually live in a bridge table; 'Bridge Reconciles Y/N'
is computed in derived.py, never extracted when its inputs are present."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

BRIDGE = SpecBandExtractor(
    "QoQ BRIDGE ATTRIBUTION",
    [
        spec("Prior Qtr NAV ($M)", "Prior Quarter NAV", "Beginning NAV", "Prior NAV"),
        spec("NAV Change Abs ($M)", "NAV Change", "Change in NAV", "Total Change"),
        spec("NAV Change %", "NAV Change Percent"),
        spec(
            "Δ Operating Performance ($M)", "Operating Performance", "Delta Operating Performance",
            "EBITDA Performance",
        ),
        spec(
            "Δ Multiple / Exit Assumption ($M)", "Multiple / Exit Assumption", "Multiple Change Impact",
            "Delta Multiple", "Multiple Re-rating",
        ),
        spec(
            "Δ Discount Rate / WACC ($M)", "Discount Rate / WACC", "Delta Discount Rate",
            "WACC Change Impact", "Discount Rate Impact",
        ),
        spec("Δ Capital Activity ($M)", "Capital Activity", "Delta Capital Activity", "Net Capital Activity"),
        spec("Δ FX ($M)", "FX", "Delta FX", "FX Translation"),
        spec("Δ Time / Pull-to-Par ($M)", "Time / Pull-to-Par", "Pull-to-Par", "Time Value Accretion"),
        spec("Δ Methodology Change ($M)", "Methodology Change", "Delta Methodology"),
        spec("Δ Other ($M)", "Other", "Delta Other", "Residual"),
        spec("Disclosed Bridge in Memo Y/N", "Disclosed Bridge", "Bridge Disclosed"),
    ],
)

EXTRACTORS = [BRIDGE]
