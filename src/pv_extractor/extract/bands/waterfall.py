"""Band specs (D4): METHODOLOGY: WATERFALL / STRUCTURED EQUITY + the WF row
of RECOVERED FIELDS — hurdles, attach/detach, tier splits, floor vs
as-converted value paths."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

WATERFALL = SpecBandExtractor(
    "METHODOLOGY: WATERFALL / STRUCTURED EQUITY",
    [
        spec(
            "WF Waterfall Type", "Waterfall Type",
            vocab_aliases={
                "Min Return Floor": ["minimum return floor", "min return"],
                "LP/GP Split": ["lp gp split"],
                "Catch-Up": ["catch up", "catchup"],
                "Drag-Along": ["drag along"],
            },
        ),
        spec("WF Hurdle IRR %", "Hurdle IRR", "IRR Hurdle", "IRR Floor", "Preferred Return"),
        spec("WF Hurdle MOIC", "Hurdle MOIC", "MOIC Hurdle", "MOIC Floor"),
        spec("WF Attach Point (Mult of EBITDA)", "Attach Point", "Attachment Point"),
        spec("WF Detach Point (Mult of EBITDA)", "Detach Point", "Detachment Point"),
        spec("WF LTV at Attach %", "LTV at Attach", "Attach LTV"),
        spec(
            "WF Min Annual Cash Distribution %", "Minimum Annual Cash Distribution",
            "Min Cash Distribution", "Priority Cash Distribution",
        ),
        spec("WF Drag-Along Trigger (yrs)", "Drag-Along Trigger", "Drag Along After"),
        spec("WF Tier 1 Split (Below Hurdle)", "Tier 1 Split", "Below Hurdle Split"),
        spec("WF Tier 2 Split (Between Hurdles)", "Tier 2 Split", "Between Hurdles Split"),
        spec("WF Tier 3 Split (Above Cap)", "Tier 3 Split", "Above Cap Split"),
        spec(
            "WF Min Return Accrual Balance ($M)", "Minimum Return Accrual Balance",
            "Accrual Balance", "Accrued Balance",
        ),
        spec("WF Accrual Rate %", "Accrual Rate"),
        spec("WF Cash Received YTD ($M)", "Cash Received YTD", "Cumulative Cash Received"),
        spec("WF Valuation Approach", "Waterfall Valuation Approach"),
        spec("WF Min Return Floor Value ($M)", "Minimum Return Floor Value", "Floor Value"),
        spec("WF As-Converted Value ($M)", "As-Converted Value", "As Converted Value"),
        spec("WF Selected Value ($M)", "Selected Value", "Concluded Waterfall Value"),
    ],
)

WF_RECOVERED = SpecBandExtractor(
    "RECOVERED FIELDS (SUPPLEMENTAL)",
    requires_band="METHODOLOGY: WATERFALL / STRUCTURED EQUITY",
    specs=[
        spec("WF Structure", "Waterfall Structure", "Structured Equity Structure"),
    ],
)

EXTRACTORS = [WATERFALL, WF_RECOVERED]
