"""Band specs (D4): METHODOLOGY ROUTING + METHODOLOGY NORMALIZED
(SUPPLEMENTAL).

The Primary/Secondary Methodology vocab drives band routing
(schema/band_routing.json); free-text methodology statements map onto the
controlled vocabulary via the alias table below (exact/alias/fuzzy >= 90,
else empty + flag).
"""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

_METHODOLOGY_ALIASES = {
    "DCF": ["discounted cash flow", "dcf analysis", "income approach", "discounted cash flows"],
    "Multiple-Market": [
        "market multiple", "market multiples", "trading multiples", "trading comps",
        "comparable companies", "public comparables", "market approach", "ev ebitda multiple",
    ],
    "Multiple-Transaction": [
        "transaction multiple", "transaction multiples", "precedent transactions",
        "transaction comps", "m&a comparables",
    ],
    "Cap Rate": ["capitalization rate", "cap rate approach", "direct capitalization"],
    "Yield/Spread": ["yield", "yield analysis", "spread", "yield to maturity", "market yield"],
    "Waterfall": ["distribution waterfall", "waterfall analysis", "opm waterfall"],
    "Cost+Accrued": ["cost plus accrued", "cost plus accrued interest", "amortized cost"],
    "Recent Tx Price": ["recent transaction price", "recent round", "calibration to entry"],
    "Cost": ["at cost", "cost basis"],
}

ROUTING = SpecBandExtractor(
    "METHODOLOGY ROUTING",
    [
        spec(
            "Primary Methodology", "Primary Valuation Methodology", "Primary Approach",
            "Valuation Methodology",
            vocab_aliases=_METHODOLOGY_ALIASES,
        ),
        spec(
            "Secondary Methodology", "Secondary Valuation Methodology", "Secondary Approach",
            "Cross-Check Methodology",
            vocab_aliases=_METHODOLOGY_ALIASES,
        ),
        spec("Primary Method Weight %", "Primary Weight", "Primary Methodology Weight"),
        spec("Secondary Method Weight %", "Secondary Weight", "Secondary Methodology Weight"),
        spec("Methodology Changed QoQ", "Methodology Change", "Methodology Changed"),
    ],
)

NORMALIZED = SpecBandExtractor(
    "METHODOLOGY NORMALIZED (SUPPLEMENTAL)",
    [
        spec("Tertiary Methodology"),
        spec("Tertiary Method Weight %", "Tertiary Weight"),
        spec(
            "Methodology Blend Type", "Blend Type",
            vocab_aliases={
                "Weighted-Hybrid": ["weighted hybrid", "weighted average", "blended"],
                "Sum-of-Parts": ["sum of the parts", "sotp"],
                "Primary-with-Cross-Check": ["primary with cross check", "cross check"],
            },
        ),
        spec("Methodology Considered Not Used", "Considered But Not Used"),
        # Verbatim = the RAW primary methodology text (kind=text keeps it
        # unmapped, unlike the vocab-routed Primary Methodology above).
        spec(
            "Methodology Verbatim (Raw Primary)", "Methodology Verbatim",
            "Primary Methodology", "Valuation Methodology",
        ),
    ],
)

EXTRACTORS = [ROUTING, NORMALIZED]
