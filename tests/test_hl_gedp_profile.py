"""HL/GEDP deterministic extraction profile.

Detection by header signature + the vertical-scan recipes over a synthetic
HL-valuation-memo page set (mirroring the real layout: a row label on one line,
its value cell(s) on the lines below). No client document is committed; the
fixture reproduces the section structure the recipes target.
"""

from __future__ import annotations

from pv_extractor.config import ExtractionConfig
from pv_extractor.extract.bands.base import ExtractionContext
from pv_extractor.extract.profiles import (
    detect_profile,
    dtype_overrides_for_profile,
    extractors_for_profile,
)
from pv_extractor.extract.profiles import hl_gedp
from pv_extractor.models import PageContent, SchemaField

GEDP_HEADERS = [
    "CLIENT", "VALUATION DATE", "PORTFOLIO COMPANY", "INDUSTRY", "SECURITY CLASS",
    "COST", "TOT INV VALUE-LOW", "TOT INV VALUE-HIGH", "PSV - LOW", "PSV - HIGH",
    "EV - LOW", "EV - HIGH", "MVE - LOW", "MVE - HIGH", "NFY REVENUE", "NFY EBITDA",
    "NFY+1 REVENUE", "NFY+1 EBITDA", "NFY+2 REVENUE", "NFY+2 EBITDA",
    "GPC-LOW", "GPC-HIGH", "GPC-WEIGHT", "GT-LOW", "GT-HIGH", "DCF-WACC",
    "OPM-VOL (avg)", "OPM-T T LIQ (avg)", "CSE-WEIGHT", "FD-SHARES",
    "UNREALIZED MOIC-LOW", "UNREALIZED MOIC-HIGH", "INDUSTRY CODE",
]


def _gedp_fields() -> list[SchemaField]:
    fields = [
        SchemaField(col_index=i + 1, band="REFERENCE", header=h, description="", dtype="string")
        for i, h in enumerate(GEDP_HEADERS)
    ]
    for f in fields:
        ov = hl_gedp.DTYPE_OVERRIDES.get(f.header)
        if ov is not None:
            f.dtype, f.unit = ov
    return fields


def _page(n: int, text: str) -> PageContent:
    return PageContent(page_number=n, text=text)


def _memo_pages() -> list[PageContent]:
    return [
        _page(1, "VALUATION OF INVESTMENT IN BRIGHTNIGHT, LLC AS OF MARCH 31, 2026"),
        _page(2, 'the Class C units (the "Class C units") in BrightNight, LLC ("BrightNight")'),
        _page(6, "Summary Valuation Conclusions\nTotal Value\n$467.68\n--\n$521.70\n"
                 "Implied MOIC\n1.09x\n--\n1.22x\nHoulihan Lokey  |  6"),
        _page(15, "Valuation Trends\nVolatility\n50.0%\n50.0%\n"
                  "Time to Liquidity (In Years)\n6.50\n7.00\n5.02\n6.02\n"
                  "Selected Discount Rate\n13.5%\n17.5%\n14.0%\n18.0%\n"
                  "FY2026 Net Revenue\n$86.04\n$86.04\n"
                  "FY2026 Adjusted EBITDA\n$88.92\n$88.92\n"
                  "FY2027 Net Revenue\n$132.42\n"
                  "FY2027 Adjusted EBITDA\n$154.55\n"
                  "FY2028 Net Revenue\n$369.83\n"
                  "FY2028 Adjusted EBITDA\n$393.79"),
        _page(16, "Key Terms\nSecurity:\nInvestment in Class C Units\nIssue Date\n10/7/2024\n"
                  "Total Invested Capital in Class C Units (in $ Mn)\n$440.00\n"
                  "Goldman Sachs Invested Capital (in $ Mn)\n$427.72"),
        _page(20, "Valuation Summary\nImplied Total Equity Value Range\n$837.30\n--\n$983.01"),
        _page(22, "Discounted Cash Flow Analysis (Valuation Date)\n"
                  "Enterprise Value Calculations\n10.0x\n10.5x\n$873.1\n$873.1\n"
                  "Add: Net debt\n$58.89\nEnterprise Value\n$896.2\n$1,041.9\nNote: mid-year"),
    ]


def _run() -> dict[str, object]:
    fields = _gedp_fields()
    ctx = ExtractionContext(cfg=ExtractionConfig())
    [extractor] = extractors_for_profile("hl_gedp")
    hits = extractor.extract(_memo_pages(), fields, ctx)
    return {h.field: h.value for h in hits}


def test_detects_gedp_signature():
    fields = _gedp_fields()
    assert detect_profile(fields) == "hl_gedp"


def test_master_headers_are_not_gedp():
    master = [
        SchemaField(col_index=i + 1, band="HEADLINE FINANCIALS", header=h, description="", dtype="number")
        for i, h in enumerate(["Implied EV ($M)", "Net Debt ($M)", "Gross IRR %", "MOIC"])
    ]
    assert detect_profile(master) is None


def test_forced_profile_wins():
    assert detect_profile([], forced="hl_gedp") == "hl_gedp"
    assert detect_profile([], forced="does_not_exist") is None


def test_dtype_overrides_applied():
    by_header = {f.header: f for f in _gedp_fields()}
    assert by_header["UNREALIZED MOIC-LOW"].dtype == "multiple_x"
    assert by_header["OPM-VOL (avg)"].dtype == "percent"
    assert by_header["OPM-T T LIQ (avg)"].dtype == "years"
    assert by_header["VALUATION DATE"].dtype == "date"


def test_identity_and_scalars():
    values = _run()
    assert values["VALUATION DATE"] == "2026-03-31"
    assert values["PORTFOLIO COMPANY"] == "BrightNight"
    assert values["SECURITY CLASS"] == "Class C units"
    assert values["COST"] == 427.72  # client-specific line beats "Total Invested Capital"


def test_low_high_pairs_pick_clean_two_cell_rows():
    values = _run()
    assert values["UNREALIZED MOIC-LOW"] == 1.09
    assert values["UNREALIZED MOIC-HIGH"] == 1.22
    assert values["TOT INV VALUE-LOW"] == 467.68
    assert values["TOT INV VALUE-HIGH"] == 521.70
    assert values["MVE - LOW"] == 837.30
    assert values["MVE - HIGH"] == 983.01
    # The "Enterprise Value Calculations" grid header must NOT win (exact match).
    assert values["EV - LOW"] == 896.2
    assert values["EV - HIGH"] == 1041.9


def test_per_fiscal_year_revenue_ebitda():
    values = _run()
    assert values["NFY REVENUE"] == 86.04
    assert values["NFY EBITDA"] == 88.92
    assert values["NFY+1 REVENUE"] == 132.42
    assert values["NFY+2 EBITDA"] == 393.79


def test_opm_and_volatility():
    values = _run()
    assert values["OPM-VOL (avg)"] == 50.0


def test_missing_methodology_flag():
    fields = _gedp_fields()
    ctx = ExtractionContext(cfg=ExtractionConfig())
    [extractor] = extractors_for_profile("hl_gedp")
    extractor.extract(_memo_pages(), fields, ctx)
    assert any(f.category == "profile" and "GPC" in f.description for f in ctx.flags)
