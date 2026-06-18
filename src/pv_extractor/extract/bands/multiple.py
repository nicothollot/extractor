"""Band specs (D4): METHODOLOGY: MULTIPLE — market/transaction multiple
methodology scalars (the comp set itself lives in the TC/TX slot bands)."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

_METRIC_ALIASES = {
    "EV/LTM EBITDA": ["ev ltm ebitda", "ltm ebitda multiple", "ev / ltm ebitda"],
    "EV/NTM EBITDA": ["ev ntm ebitda", "ntm ebitda multiple", "forward ebitda multiple"],
    "EV/Revenue": ["ev revenue", "revenue multiple", "ev sales"],
    "P/E": ["price earnings", "pe ratio"],
    "Exit Multiple": ["terminal exit multiple"],
}

MULTIPLE = SpecBandExtractor(
    "METHODOLOGY: MULTIPLE",
    [
        spec(
            "Mult Selected (x)", "Selected Multiple", "Multiple Selected", "Applied Multiple",
            "Concluded Multiple",
            table_row=["Selected Multiple", "Multiple Selected", "Applied Multiple"],
            table_col=["selected", "value", "mid"],
        ),
        spec("Mult Prior Qtr (x)", "Prior Quarter Multiple", "Prior Qtr Multiple", "Prior Multiple"),
        spec("Mult Change (x)", "Multiple Change"),
        spec("Mult Metric", "Multiple Metric", "Multiple Basis", vocab_aliases=_METRIC_ALIASES),
        spec("Mult Basis Year", "Basis Year", "Multiple Basis Year"),
        spec(
            "Mult EBITDA / Revenue Base ($M)", "EBITDA Base", "Revenue Base", "Applied EBITDA",
            "Base EBITDA",
        ),
        # Comp-set statistics live as aggregate rows of the comps table.
        spec(
            "Mult Comp Set Mean (x)", "Comp Set Mean", "Peer Mean", "Mean Multiple",
            table_row=["Mean", "Average"],
            table_col=["ev ltm ebitda", "ev ebitda", "multiple", "selected"],
        ),
        spec(
            "Mult Comp Set Median (x)", "Comp Set Median", "Peer Median", "Median Multiple",
            table_row=["Median"],
            table_col=["ev ltm ebitda", "ev ebitda", "multiple", "selected"],
        ),
        spec(
            "Mult Comp Set Mean Prior Qtr (x)", "Comp Set Mean Prior Quarter", "Prior Quarter Comp Mean",
            table_row=["Prior Mean", "Mean Prior Qtr", "Prior Quarter Mean"],
            table_col=["ev ltm ebitda", "ev ebitda", "multiple"],
        ),
        spec(
            "Mult Premium/Discount to Mean %", "Premium/Discount to Mean", "Discount to Mean",
            "Premium to Mean", "Size Discount",
        ),
        spec("Mult Premium/Discount Rationale", "Premium/Discount Rationale", "Discount Rationale"),
        spec("Mult Implied EV ($M)", "Multiple Implied EV", "Implied EV (Multiple)"),
        spec("Mult Implied Equity ($M)", "Multiple Implied Equity", "Implied Equity (Multiple)"),
    ],
)

EXTRACTORS = [MULTIPLE]
