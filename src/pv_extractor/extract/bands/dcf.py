"""Band specs (D4): METHODOLOGY: DCF + the DCF rows of RECOVERED FIELDS.

DCF assumptions arrive both as label:value prose and as Low/Mid/High range
tables — the Low/Mid/High fields carry explicit table_col hints."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

DCF = SpecBandExtractor(
    "METHODOLOGY: DCF",
    [
        spec(
            "DCF Discount Rate Type", "Discount Rate Type",
            vocab_aliases={
                "WACC (Unlevered)": ["wacc", "unlevered wacc", "weighted average cost of capital"],
                "Cost of Equity (Levered FCFE)": ["cost of equity", "levered fcfe", "fcfe"],
            },
        ),
        spec("DCF Discount Rate Low %", "Discount Rate Low", table_row=["Discount Rate", "WACC"], table_col=["low"]),
        spec(
            "DCF Discount Rate Mid %", "Discount Rate", "WACC", "Selected Discount Rate",
            "Discount Rate Mid", "Discount Rate Selected",
            table_row=["Discount Rate", "WACC"], table_col=["mid", "selected", "value"],
        ),
        spec("DCF Discount Rate High %", "Discount Rate High", table_row=["Discount Rate", "WACC"], table_col=["high"]),
        spec(
            "DCF Discount Rate Prior Qtr %", "Prior Quarter Discount Rate", "Prior Qtr WACC",
            "Prior Discount Rate",
            table_row=["Discount Rate", "WACC"], table_col=["prior qtr", "prior quarter", "prior"],
        ),
        spec("DCF Discount Rate Change (bps)", "Discount Rate Change", "WACC Change"),
        spec("DCF Risk-Free Rate %", "Risk-Free Rate", "Risk Free Rate"),
        spec("DCF Equity Risk Premium %", "Equity Risk Premium", "ERP"),
        spec("DCF Country Risk Premium %", "Country Risk Premium", "CRP"),
        spec("DCF Alpha / Size Premium %", "Alpha / Size Premium", "Size Premium", "Alpha"),
        spec("DCF Unlevered Beta", "Unlevered Beta", "Asset Beta"),
        spec("DCF Relevered Beta", "Relevered Beta", "Levered Beta"),
        spec("DCF Cap Structure (% Debt)", "% Debt", "Debt Weighting", "Capital Structure Debt"),
        spec("DCF Cap Structure (% Equity)", "% Equity", "Equity Weighting", "Capital Structure Equity"),
        spec(
            "DCF Terminal Method", "Terminal Value Method", "Terminal Method",
            vocab_aliases={
                "Exit Multiple": ["terminal exit multiple", "exit multiple method"],
                "Gordon Growth": ["perpetuity growth", "gordon growth model", "terminal growth"],
            },
        ),
        spec("DCF Terminal Exit Multiple", "Terminal Exit Multiple", "Exit Multiple", "Terminal Multiple"),
        spec("DCF Terminal Exit Metric", "Terminal Exit Metric", "Exit Multiple Metric"),
        spec("DCF Terminal Growth Rate %", "Terminal Growth Rate", "Perpetuity Growth Rate", "Terminal Growth"),
        spec("DCF Projection Period (yrs)", "Projection Period", "Forecast Period", "Explicit Period"),
        spec("DCF Projection Start Date", "Projection Start Date", "Forecast Start"),
        spec("DCF Projection End / Exit Date", "Projection End Date", "Exit Date", "Forecast End"),
        spec("DCF Output Low ($M)", "DCF Value Low", table_row=["DCF Output", "DCF Value", "Enterprise Value"], table_col=["low"]),
        spec("DCF Output Mid ($M)", "DCF Value Mid", "DCF Value Selected", table_row=["DCF Output", "DCF Value", "Enterprise Value"], table_col=["mid", "selected"]),
        spec("DCF Output High ($M)", "DCF Value High", table_row=["DCF Output", "DCF Value", "Enterprise Value"], table_col=["high"]),
        spec("DCF Implied Multiple Low (x)", "Implied Multiple Low", table_row=["Implied Multiple", "Implied EV/EBITDA"], table_col=["low"]),
        spec("DCF Implied Multiple High (x)", "Implied Multiple High", table_row=["Implied Multiple", "Implied EV/EBITDA"], table_col=["high"]),
        spec("DCF RAB Value ($M)", "RAB Value", "Regulated Asset Base"),
        spec("DCF Regulatory Allowed WACC %", "Regulatory Allowed WACC", "Allowed WACC"),
        spec("DCF Premium/Discount to RAB %", "Premium/Discount to RAB", "Premium to RAB"),
    ],
)

DCF_RECOVERED = SpecBandExtractor(
    "RECOVERED FIELDS (SUPPLEMENTAL)",
    requires_band="METHODOLOGY: DCF",
    specs=[
        spec("DCF Implied EV (DCF) ($M)", "DCF Implied EV", "DCF Implied Enterprise Value"),
        spec("DCF Implied Equity Value ($M)", "DCF Implied Equity Value", "DCF Equity Value"),
        spec("DCF Exit Equity Value ($M)", "Exit Equity Value", "Modeled Exit Equity"),
        spec("DCF Exit Year", "Exit Year", "Modeled Exit Year"),
    ],
)

EXTRACTORS = [DCF, DCF_RECOVERED]
