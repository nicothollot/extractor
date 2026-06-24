"""Band specs (D4): HEADLINE FINANCIALS, RETURNS, OPERATING FINANCIALS,
CALIBRATION (ENTRY-PRICE ANCHOR) — the valuation-summary scalar fields."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

HEADLINE = SpecBandExtractor(
    "HEADLINE FINANCIALS",
    [
        spec(
            "Implied EV ($M)", "Implied Enterprise Value", "Enterprise Value", "Concluded EV",
            "Total Enterprise Value", "TEV",
        ),
        spec("Net Debt ($M)", "Net Debt", "Less: Net Debt"),
        spec(
            "Implied Equity Value 100% ($M)", "Implied Equity Value", "Equity Value 100%",
            "Equity Value (100%)", "Total Equity Value",
        ),
        spec(
            "Fund Share Equity Value ($M)", "Fund Share Equity Value", "Fund Share of Equity",
            "Concluded Fair Value", "Fair Value (Fund Share)", "NAV",
        ),
        spec("FX Rate (Current)", "FX Rate (Current)", "FX Rate", "Exchange Rate", "Spot Rate"),
        spec("FX Rate (Prior Qtr)", "FX Rate (Prior Qtr)", "Prior Quarter FX Rate", "FX Rate Prior"),
        spec("FX Impact on NAV ($M)", "FX Impact"),
    ],
)

RETURNS = SpecBandExtractor(
    "RETURNS",
    [
        spec("Gross IRR %", "Gross IRR"),
        spec("Net IRR %", "Net IRR"),
        spec("MOIC", "Gross MOIC", "Multiple on Invested Capital"),
        spec("DPI"),
        spec("Fair Value as % of Cost", "FV as % of Cost", "FV / Cost"),
        spec("Unrealized Value ($M)", "Unrealized Value"),
        spec("Realized Value ($M)", "Realized Value", "Realized Proceeds"),
        spec(
            "Total Invested Capital ($M)", "Total Invested Capital", "Invested Capital",
            "Total Cost Basis",
        ),
    ],
)

OPERATING = SpecBandExtractor(
    "OPERATING FINANCIALS",
    [
        spec("Revenue ($M)", "Revenue", "LTM Revenue", "Total Revenue"),
        spec(
            "EBITDA ($M)", "EBITDA", "LTM EBITDA", "Adjusted EBITDA", "Cash EBITDA",
            "Reported EBITDA", "Reported EBITDA (Primary EBITDA Metric)", "Credit Adj. EBITDA",
        ),
        spec("EBITDA Margin %", "EBITDA Margin"),
        spec("EBITDA vs Budget %", "EBITDA vs Budget", "EBITDA vs Plan"),
        spec("Maintenance Capex ($M)", "Maintenance Capex"),
        spec("Growth Capex ($M)", "Growth Capex", "Expansion Capex"),
        spec("Distributions in Period ($M)", "Distributions in Period", "Distributions"),
        spec("Capacity Utilization %", "Capacity Utilization", "Utilization"),
        spec("Net Debt at Asset ($M)", "Net Debt at Asset", "Asset-Level Net Debt"),
        spec("Net Debt/EBITDA", "Net Leverage", "Leverage"),
        spec("DSCR", "Debt Service Coverage Ratio", "Debt Service Coverage"),
    ],
)

CALIBRATION = SpecBandExtractor(
    "CALIBRATION (ENTRY-PRICE ANCHOR)",
    [
        spec("Investment Close Date", "Close Date", "Entry Date", "Investment Date"),
        spec("Initial Cost Basis ($M)", "Initial Cost Basis", "Initial Investment", "Cost Basis"),
        spec("Entry Methodology"),
        spec("Entry Multiple", "Multiple at Entry", "Entry EV/EBITDA"),
        spec("Entry Multiple Metric"),
        spec("Entry EBITDA / NOI ($M)", "Entry EBITDA", "Entry NOI"),
        spec("Entry TEV ($M)", "Entry TEV", "TEV at Entry", "Entry Enterprise Value"),
        spec("Entry Equity Value 100% ($M)", "Entry Equity Value"),
        spec("Underwrite Gross IRR %", "Underwritten Gross IRR", "UW IRR", "Underwrite IRR"),
        spec("Underwrite Target MOIC", "Underwritten MOIC", "Target MOIC", "UW MOIC"),
        spec("Underwrite Holding Period (yrs)", "Underwrite Holding Period", "Target Hold Period"),
        spec("Multiple Drift Since Entry", "Multiple Drift"),
        spec("Last 3P Corroboration Date", "Last Third-Party Corroboration"),
        spec(
            "Calibration Status",
            vocab_aliases={"Calibration Stale": ["stale calibration", "stale"]},
        ),
    ],
)

EXTRACTORS = [HEADLINE, RETURNS, OPERATING, CALIBRATION]
