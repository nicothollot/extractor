"""D8 golden extraction tests.

Each realistic fixture memo runs through the FULL pipeline (locate -> verify
-> read -> target -> extract -> validate) and its extracted row is compared
field-by-field against frozen expectations (>= 40 representative fields per
text memo; the values are exactly what tests/fixtures/build_memos.py writes
into the documents). Confidences are asserted within bands per source class
(text page / OCR page / computed), flags and QA statuses exactly, and the
audit sidecar must be byte-stable across runs once timings are excluded.

The OCR memo asserts exact values too — RapidOCR is deterministic for a
given image and model — but its asserted field set is the robust subset
(model-version drift tolerance).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pv_extractor.models import (
    FlagSeverity,
    MemoResult,
    PageClass,
    QaStatus,
    ResolutionStatus,
    VerifyStatus,
)
from pv_extractor.run import RunReport, run

# (client, deal, period) per memo; seq drives a fixed, distinct timestamp.
MEMO_CASES = {
    "accell": ("Angelo Gordon", "Accell", "2025-01-31"),
    "digital_edge": ("Angelo Gordon", "Digital Edge", "Q1 2026"),
    "sre": ("Apollo Global Management", "Summit Ridge Energy", "2026-03-31"),
    "tdw": ("Angelo Gordon", "T.D. Williamson", "2025-12-31"),
    "aiof": ("Apollo Global Management", "AIOF II ANRP III", "Q1 2026"),
    "hyperoptic": ("Apollo Global Management", "Hyperoptic", "Q1 2026"),
    "andover": ("Angeles Investments", "Andover Storage", "2026-03-31"),
    "mountain_peak": ("Blue Owl", "Mountain Peak Holdings", "Q1 2026"),
    "riverbend": ("Blue Owl", "Riverbend Power", "Q1 2026"),
}


@pytest.fixture(scope="module")
def golden(phase2_env) -> dict[str, RunReport]:
    """One full pipeline run per memo, fixed timestamps, cache bypassed."""
    out: dict[str, RunReport] = {}
    for seq, (key, (client, deal, period)) in enumerate(MEMO_CASES.items()):
        out[key] = run(
            phase2_env, scope="deal", client=client, deal=deal, period=period,
            force=True, now=datetime(2026, 6, 11, 12, 0, seq),
        )
    return out


def _memo(golden: dict[str, RunReport], key: str) -> MemoResult:
    report = golden[key]
    assert report.coverage[0].status == "FOUND", report.coverage[0]
    assert len(report.memos) == 1
    return report.memos[0]


def _values(memo: MemoResult, asset_index: int = 0) -> dict[str, tuple[object, str, float]]:
    return {
        hit.field: (hit.value, hit.method, hit.confidence)
        for hit in memo.assets[asset_index].hits
    }


def _assert_expected(memo: MemoResult, expected: list[tuple], asset_index: int = 0) -> None:
    values = _values(memo, asset_index)
    missing, wrong = [], []
    for field, value, method in expected:
        if field not in values:
            missing.append(field)
            continue
        got_value, got_method, _ = values[field]
        if isinstance(value, float):
            ok = isinstance(got_value, (int, float)) and got_value == pytest.approx(value, abs=1e-4)
        else:
            ok = got_value == value
        if not ok or got_method != method:
            wrong.append((field, value, method, got_value, got_method))
    assert not missing, f"missing fields: {missing}"
    assert not wrong, f"wrong values: {wrong}"


def _assert_confidence_bands(
    memo: MemoResult, asset_index: int = 0, *, det_band=(0.80, 1.0), computed_band=(0.80, 1.0)
) -> None:
    for hit in memo.assets[asset_index].hits:
        if hit.method == "deterministic":
            low, high = det_band
        elif hit.method == "computed":
            low, high = computed_band
        else:  # metadata
            low, high = 1.0, 1.0
        assert low <= hit.confidence <= high, (hit.field, hit.method, hit.confidence)


# ===========================================================================
# Accell — multiples memo
# ===========================================================================

ACCELL_EXPECTED = [
    ("Fund Manager", "Angelo Gordon", "deterministic"),
    ("Fund Name", "AG Europe Private Equity Fund II", "deterministic"),
    ("Fund Vintage", 2019, "deterministic"),
    ("Fund Strategy", "Value-Add", "deterministic"),
    ("Portfolio Company", "Accell Group", "deterministic"),
    ("Operating Name", "Accell", "deterministic"),
    ("Asset/Project Name", "Accell Bicycle Platform", "deterministic"),
    ("Country", "Netherlands", "deterministic"),
    ("Region", "Europe", "deterministic"),
    ("Sector", "Consumer", "deterministic"),
    ("Sub-Sector", "Recreational Products", "deterministic"),
    ("Investment Type", "Common Equity", "deterministic"),
    ("Methodology Type", "Equity", "deterministic"),
    ("Dev Stage", "Operating", "deterministic"),
    ("Ownership %", 38.5, "deterministic"),
    ("FV Hierarchy Level", "Level 3", "deterministic"),
    ("Level Changed from Prior", False, "deterministic"),
    ("Calibrated to Tx Price", True, "deterministic"),
    ("Months Since 3P Corroboration", 7.0, "deterministic"),
    ("Management Overlay Applied", False, "deterministic"),
    ("DLOM Applied", False, "deterministic"),
    ("DLOC Applied", False, "deterministic"),
    ("Primary Methodology", "Multiple-Market", "deterministic"),
    ("Primary Method Weight %", 100.0, "deterministic"),
    ("Methodology Changed QoQ", False, "deterministic"),
    ("Implied EV ($M)", 545.0, "deterministic"),
    ("Net Debt ($M)", 120.0, "deterministic"),
    ("Implied Equity Value 100% ($M)", 425.0, "deterministic"),
    ("Fund Share Equity Value ($M)", 163.6, "deterministic"),
    ("FX Rate (Current)", 1.0842, "deterministic"),
    ("FX Rate (Prior Qtr)", 1.0915, "deterministic"),
    ("FX Impact on NAV ($M)", 0.8, "computed"),
    ("Gross IRR %", 16.4, "deterministic"),
    ("Net IRR %", 13.1, "deterministic"),
    ("MOIC", 1.6, "deterministic"),
    ("DPI", 0.25, "deterministic"),
    ("Fair Value as % of Cost", 109.1, "deterministic"),
    ("Unrealized Value ($M)", 163.6, "deterministic"),
    ("Realized Value ($M)", 37.5, "deterministic"),
    ("Total Invested Capital ($M)", 184.5, "deterministic"),
    ("Revenue ($M)", 410.0, "deterministic"),
    ("EBITDA ($M)", 64.1, "deterministic"),
    ("EBITDA Margin %", 15.6341, "computed"),
    ("EBITDA vs Budget %", -3.2, "deterministic"),
    ("Maintenance Capex ($M)", 18.0, "deterministic"),
    ("Growth Capex ($M)", 9.5, "deterministic"),
    ("Net Debt/EBITDA", 1.9, "deterministic"),
    ("DSCR", 2.4, "deterministic"),
    ("Investment Close Date", "2021-06-30", "deterministic"),
    ("Initial Cost Basis ($M)", 150.0, "deterministic"),
    ("Entry Multiple", 7.6, "deterministic"),
    ("Entry Multiple Metric", "EV/LTM EBITDA", "deterministic"),
    ("Entry TEV ($M)", 441.0, "deterministic"),
    ("Underwrite Gross IRR %", 18.0, "deterministic"),
    ("Underwrite Target MOIC", 2.2, "deterministic"),
    ("Underwrite Holding Period (yrs)", 5.0, "deterministic"),
    ("Multiple Drift Since Entry", 0.9, "computed"),
    ("Calibration Status", "Calibrated", "deterministic"),
    ("Prior Qtr NAV ($M)", 158.3, "deterministic"),
    ("NAV Change Abs ($M)", 5.3, "computed"),
    ("NAV Change %", 3.3481, "computed"),
    ("Δ Operating Performance ($M)", 3.1, "deterministic"),
    ("Δ Multiple / Exit Assumption ($M)", 1.4, "deterministic"),
    ("Δ FX ($M)", 0.8, "deterministic"),
    ("Bridge Reconciles Y/N", True, "computed"),
    ("Mult Selected (x)", 8.5, "deterministic"),
    ("Mult Prior Qtr (x)", 8.2, "deterministic"),
    ("Mult Change (x)", 0.3, "computed"),
    ("Mult Metric", "EV/LTM EBITDA", "deterministic"),
    ("Mult Basis Year", "LTM", "deterministic"),
    ("Mult EBITDA / Revenue Base ($M)", 64.1, "deterministic"),
    ("Mult Comp Set Mean (x)", 8.6, "deterministic"),
    ("Mult Comp Set Median (x)", 8.7, "deterministic"),
    ("Mult Premium/Discount to Mean %", -15.0, "deterministic"),
    ("Mult Implied EV ($M)", 545.0, "deterministic"),
    ("TC01 Name", "Dorel Industries", "deterministic"),
    ("TC01 Ticker", "DII.B", "deterministic"),
    ("TC01 Include", False, "deterministic"),
    ("TC01 TEV ($M)", 1150.0, "deterministic"),
    ("TC01 LTM EBITDA ($M)", 148.0, "deterministic"),
    ("TC01 EV/LTM EBITDA", 7.8, "deterministic"),
    ("TC01 EV/NTM EBITDA", 7.4, "deterministic"),
    ("TC02 Name", "Giant Manufacturing", "deterministic"),
    ("TC02 Ticker", "9921", "deterministic"),
    ("TC02 Include", True, "deterministic"),
    ("TC02 TEV ($M)", 4120.0, "deterministic"),
    ("TC02 EV/LTM EBITDA", 8.2, "deterministic"),
    ("TC03 Name", "Shimano Inc", "deterministic"),
    ("TC03 TEV ($M)", 18500.0, "deterministic"),
    ("TC03 EV/LTM EBITDA", 9.1, "deterministic"),
    ("TC04 Name", "Thule Group", "deterministic"),
    ("TC04 EV/LTM EBITDA", 9.2, "deterministic"),
    ("New to Portfolio", True, "computed"),
    ("Valuation Tone", "Balanced", "deterministic"),
    ("Key Value Drivers", "E-bike demand recovery and inventory normalization", "deterministic"),
    ("Key Risks", "Consumer discretionary exposure and FX translation", "deterministic"),
    ("Material Changes QoQ", "Comp set re-rating drove the modest uplift", "deterministic"),
    ("QA Status", "qa_pass", "computed"),
    ("Extraction Flags Count", 0, "computed"),
    ("Reviewer Attention", "N", "computed"),
    ("Methodology Verbatim (Raw Primary)", "Market multiples", "deterministic"),
]


def test_accell_multiples_golden(golden) -> None:
    memo = _memo(golden, "accell")
    assert len(ACCELL_EXPECTED) >= 40
    assert memo.verify is not None and memo.verify.status is VerifyStatus.VERIFIED
    assert memo.verify.asof_date is not None and memo.verify.asof_date.isoformat() == "2025-01-31"
    assert memo.reporting_period == "Jan 2025"  # Angelo Gordon reports monthly
    assert all(cls is PageClass.TEXT for cls in memo.page_classes.values())
    _assert_expected(memo, ACCELL_EXPECTED)
    _assert_confidence_bands(memo)
    assert memo.assets[0].qa_status is QaStatus.qa_pass
    assert memo.assets[0].flags == []
    assert memo.escalation is not None
    assert memo.escalation.fields
    assert {f.reason for f in memo.escalation.fields} <= {"primary_catalog", "below_confidence", "required_empty"}
    # conflicting-candidate audit trail: derived field keeps the extracted loser
    nav_change = next(h for h in memo.assets[0].hits if h.field == "NAV Change Abs ($M)")
    assert nav_change.conflicts and nav_change.conflicts[0].value == 5.3


# ===========================================================================
# Digital Edge — DCF memo
# ===========================================================================

DIGITAL_EDGE_EXPECTED = [
    ("Fund Manager", "Angelo Gordon", "deterministic"),
    ("Fund Name", "AG Asia Realty Fund IV", "deterministic"),
    ("Fund Vintage", 2021, "deterministic"),
    ("Fund Strategy", "Opportunistic", "deterministic"),
    ("Portfolio Company", "Digital Edge", "deterministic"),
    ("Operating Name", "Digital Edge DC", "deterministic"),
    ("Country", "Singapore", "deterministic"),
    ("Sector", "Digital Infrastructure", "deterministic"),
    ("Sub-Sector", "Data Centers", "deterministic"),
    ("Investment Type", "Common Equity", "deterministic"),
    ("Primary KPI Name", "IT Load Capacity", "deterministic"),
    ("Primary KPI Value", 312.0, "deterministic"),
    ("Primary KPI Unit", "MW", "deterministic"),
    ("Ownership %", 62.0, "deterministic"),
    ("Primary Methodology", "DCF", "deterministic"),
    ("Secondary Methodology", "Multiple-Market", "deterministic"),
    ("Primary Method Weight %", 80.0, "deterministic"),
    ("Secondary Method Weight %", 20.0, "deterministic"),
    ("Implied EV ($M)", 2150.0, "deterministic"),
    ("Net Debt ($M)", 640.0, "deterministic"),
    ("Implied Equity Value 100% ($M)", 1510.0, "deterministic"),
    ("Fund Share Equity Value ($M)", 936.2, "deterministic"),
    ("Gross IRR %", 21.5, "deterministic"),
    ("Net IRR %", 17.8, "deterministic"),
    ("MOIC", 1.9, "deterministic"),
    ("Total Invested Capital ($M)", 492.0, "deterministic"),
    ("Revenue ($M)", 310.0, "deterministic"),
    ("EBITDA ($M)", 148.0, "deterministic"),
    ("EBITDA Margin %", 47.7419, "computed"),
    ("Growth Capex ($M)", 260.0, "deterministic"),
    ("Capacity Utilization %", 78.0, "deterministic"),
    ("DCF Discount Rate Type", "WACC (Unlevered)", "deterministic"),
    ("DCF Discount Rate Low %", 8.0, "deterministic"),
    ("DCF Discount Rate Mid %", 8.5, "deterministic"),
    ("DCF Discount Rate High %", 9.0, "deterministic"),
    ("DCF Discount Rate Prior Qtr %", 8.3, "deterministic"),
    ("DCF Discount Rate Change (bps)", 20.0, "computed"),
    ("DCF Risk-Free Rate %", 4.1, "deterministic"),
    ("DCF Equity Risk Premium %", 5.0, "deterministic"),
    ("DCF Country Risk Premium %", 0.6, "deterministic"),
    ("DCF Alpha / Size Premium %", 2.0, "deterministic"),
    ("DCF Unlevered Beta", 0.78, "deterministic"),
    ("DCF Relevered Beta", 0.95, "deterministic"),
    ("DCF Cap Structure (% Debt)", 35.0, "deterministic"),
    ("DCF Cap Structure (% Equity)", 65.0, "deterministic"),
    ("DCF Terminal Method", "Exit Multiple", "deterministic"),
    ("DCF Terminal Exit Multiple", 16.0, "deterministic"),
    ("DCF Terminal Exit Metric", "EV/NTM EBITDA", "deterministic"),
    ("DCF Projection Period (yrs)", 10.0, "deterministic"),
    ("DCF Projection Start Date", "2026-04-01", "deterministic"),
    ("DCF Projection End / Exit Date", "2036-03-31", "deterministic"),
    ("DCF Output Low ($M)", 1980.0, "deterministic"),
    ("DCF Output Mid ($M)", 2150.0, "deterministic"),
    ("DCF Output High ($M)", 2330.0, "deterministic"),
    ("DCF Implied Multiple Low (x)", 14.9, "deterministic"),
    ("DCF Implied Multiple High (x)", 17.5, "deterministic"),
    ("Mult Selected (x)", 16.0, "deterministic"),
    ("Mult Metric", "EV/NTM EBITDA", "deterministic"),
    ("Mult Comp Set Mean (x)", 20.2, "deterministic"),
    ("TC01 Name", "Digital Realty", "deterministic"),
    ("TC01 Beta", 0.71, "deterministic"),
    ("TC02 Name", "Equinix", "deterministic"),
    ("TC02 EV/NTM EBITDA", 20.1, "deterministic"),
    ("TC03 Name", "Keppel DC REIT", "deterministic"),
    ("TC03 Include", False, "deterministic"),
    ("DCF Implied EV (DCF) ($M)", 2150.0, "deterministic"),
    ("DCF Implied Equity Value ($M)", 1510.0, "deterministic"),
    ("DCF Exit Equity Value ($M)", 3420.0, "deterministic"),
    ("DCF Exit Year", 2036, "deterministic"),
    ("QA Status", "qa_pass", "computed"),
]


def test_digital_edge_dcf_golden(golden) -> None:
    memo = _memo(golden, "digital_edge")
    assert len(DIGITAL_EDGE_EXPECTED) >= 40
    _assert_expected(memo, DIGITAL_EDGE_EXPECTED)
    _assert_confidence_bands(memo)
    assert memo.assets[0].flags == []
    # methodology routing gated the bands: no cap-rate/yield/waterfall hits
    bands = {hit.band for hit in memo.assets[0].hits}
    assert "METHODOLOGY: CAP RATE" not in bands
    assert "METHODOLOGY: YIELD / CREDIT" not in bands
    assert "METHODOLOGY: WATERFALL / STRUCTURED EQUITY" not in bands


# ===========================================================================
# Summit Ridge Energy — yield/credit memo + capital structure
# ===========================================================================

SRE_EXPECTED = [
    ("Fund Manager", "Apollo Global Management", "deterministic"),
    ("Fund Name", "AIOF II / ANRP III (joint)", "deterministic"),
    ("Portfolio Company", "Summit Ridge Energy", "deterministic"),
    ("Asset/Project Name", "SRE Community Solar Platform", "deterministic"),
    ("Country", "United States", "deterministic"),
    ("Sector", "Power", "deterministic"),
    ("Sub-Sector", "Generation - Renewable", "deterministic"),
    ("Investment Type", "Structured Equity", "deterministic"),
    ("Methodology Type", "Hybrid", "deterministic"),
    ("Primary KPI Name", "Solar Capacity Online", "deterministic"),
    ("Primary KPI Value", 500.0, "deterministic"),
    ("Primary KPI Unit", "MW (DC)", "deterministic"),
    ("Revenue Profile", "Contracted", "deterministic"),
    ("Calibrated to Tx Price", True, "deterministic"),
    ("Months Since 3P Corroboration", 45.0, "deterministic"),
    ("Primary Methodology", "Yield/Spread", "deterministic"),
    ("Primary Method Weight %", 100.0, "deterministic"),
    ("Net Debt ($M)", -17.0, "deterministic"),  # $(17.0)M parenthesized negative
    ("Fund Share Equity Value ($M)", 248.0, "deterministic"),
    ("Gross IRR %", 24.0, "deterministic"),
    ("MOIC", 1.94, "deterministic"),
    ("DPI", 0.26, "deterministic"),
    ("Fair Value as % of Cost", 166.0, "deterministic"),
    ("Unrealized Value ($M)", 247.0, "deterministic"),
    ("Realized Value ($M)", 45.0, "deterministic"),
    ("Total Invested Capital ($M)", 176.0, "deterministic"),
    ("Yield Coupon Type", "Cash+PIK", "deterministic"),
    ("Yield Cash Coupon Rate %", 6.0, "deterministic"),
    ("Yield PIK Coupon Rate %", 7.5, "deterministic"),
    ("Yield All-In YTM %", 13.5, "deterministic"),
    ("Yield All-In YTM Prior Qtr %", 13.0, "deterministic"),
    ("Yield YTM Change (bps)", 50.0, "computed"),
    ("Yield Comp G-Spread (bps)", 625.0, "deterministic"),
    ("Yield Reference Govt Yield %", 4.3, "deterministic"),
    ("Yield All-In Market Yield %", 10.6, "deterministic"),
    ("Yield Gap vs Market (bps)", 290.0, "computed"),
    ("Yield Par Value ($M, local)", 185.0, "deterministic"),
    ("Yield Accrued Interest ($M, local)", 12.4, "deterministic"),
    ("Yield OID Unamortized ($M, local)", 3.1, "deterministic"),
    ("Yield Cost + Accrued ($M, local)", 194.3, "deterministic"),
    ("Yield Min MOIC Floor", 1.3, "deterministic"),
    ("Yield Calibration Risk Rating", "B+", "deterministic"),
    ("Yield Calibration Spread at Origination (bps)", 700.0, "deterministic"),
    ("Yield Current Spread (bps)", 625.0, "deterministic"),
    # capital structure slots: seniority order Cash, 1L, 2L, Pref, Common
    ("CS01 Facility Name", "Cash & Equivalents", "deterministic"),
    ("CS01 Tranche Rank", "Cash", "deterministic"),
    ("CS01 Notional ($M, local)", 42.0, "deterministic"),
    ("CS02 Facility Name", "Senior Secured Term Loan", "deterministic"),
    ("CS02 Tranche Rank", "1L", "deterministic"),
    ("CS02 Notional ($M, local)", 250.0, "deterministic"),
    ("CS02 Drawn ($M, local)", 230.0, "deterministic"),
    ("CS02 Coupon Rate %", 7.25, "deterministic"),
    ("CS02 Maturity Date", "2028-06-30", "deterministic"),
    ("CS03 Facility Name", "Tax Equity Facility", "deterministic"),
    ("CS03 Tranche Rank", "2L", "deterministic"),
    ("CS03 Maturity Date", "2027-09-30", "deterministic"),
    ("CS04 Facility Name", "HoldCo Structured Preferred", "deterministic"),
    ("CS04 Tranche Rank", "Pref Equity", "deterministic"),
    ("CS04 Coupon Rate %", 13.5, "deterministic"),
    ("CS04 Maturity Date", "2029-06-30", "deterministic"),
    ("CS05 Facility Name", "Common Equity", "deterministic"),
    ("CS05 Tranche Rank", "Common Equity", "deterministic"),
    ("Yield Structure", "HoldCo structured preferred", "deterministic"),
    ("Yield Maturity Date", "2029-06-30", "deterministic"),
    ("QA Status", "qa_pass", "computed"),
]


def test_sre_yield_credit_golden(golden) -> None:
    memo = _memo(golden, "sre")
    assert len(SRE_EXPECTED) >= 40
    _assert_expected(memo, SRE_EXPECTED)
    _assert_confidence_bands(memo)
    assert memo.assets[0].flags == []
    values = _values(memo)
    # QoQ continuity against the prior-period SRE row in the reference template
    assert values["New to Portfolio"][0] is False
    assert values["WACC >50bps QoQ"][0] is False


# ===========================================================================
# T.D. Williamson — waterfall / structured equity memo
# ===========================================================================

TDW_EXPECTED = [
    ("Fund Manager", "Angelo Gordon", "deterministic"),
    ("Fund Name", "AG Energy Transition Partners", "deterministic"),
    ("Portfolio Company", "T.D. Williamson", "deterministic"),
    ("Operating Name", "TDW", "deterministic"),
    ("Investment Type", "Structured Equity", "deterministic"),
    ("Methodology Type", "Hybrid", "deterministic"),
    ("Ownership %", 28.0, "deterministic"),
    ("Primary Methodology", "Waterfall", "deterministic"),
    ("Secondary Methodology", "Multiple-Market", "deterministic"),
    ("Primary Method Weight %", 60.0, "deterministic"),
    ("Secondary Method Weight %", 40.0, "deterministic"),
    ("Implied EV ($M)", 830.0, "deterministic"),
    ("Net Debt ($M)", 190.0, "deterministic"),
    ("Implied Equity Value 100% ($M)", 640.0, "deterministic"),
    ("Fund Share Equity Value ($M)", 215.0, "deterministic"),
    ("Gross IRR %", 14.2, "deterministic"),
    ("Net IRR %", 11.0, "deterministic"),
    ("MOIC", 1.3, "deterministic"),
    ("Total Invested Capital ($M)", 165.0, "deterministic"),
    ("Revenue ($M)", 480.0, "deterministic"),
    ("EBITDA ($M)", 100.0, "deterministic"),
    ("EBITDA Margin %", 20.8333, "computed"),
    ("Initial Cost Basis ($M)", 165.0, "deterministic"),
    ("Entry Multiple", 7.7, "deterministic"),
    ("Underwrite Target MOIC", 1.35, "deterministic"),
    ("Multiple Drift Since Entry", 0.6, "computed"),
    ("Mult Selected (x)", 8.3, "deterministic"),
    ("Mult Prior Qtr (x)", 7.7, "deterministic"),
    ("Mult Change (x)", 0.6, "computed"),
    ("Mult Comp Set Mean (x)", 9.2, "deterministic"),
    ("Mult Comp Set Median (x)", 9.4, "deterministic"),
    ("Mult Premium/Discount to Mean %", -15.0, "deterministic"),
    ("WF Waterfall Type", "Min Return Floor", "deterministic"),
    ("WF Hurdle IRR %", 10.0, "deterministic"),
    ("WF Hurdle MOIC", 1.35, "deterministic"),
    ("WF Attach Point (Mult of EBITDA)", 1.3, "deterministic"),
    ("WF Detach Point (Mult of EBITDA)", 3.1, "deterministic"),
    ("WF LTV at Attach %", 37.0, "deterministic"),
    ("WF Min Annual Cash Distribution %", 7.0, "deterministic"),
    ("WF Drag-Along Trigger (yrs)", 7.0, "deterministic"),
    ("WF Tier 1 Split (Below Hurdle)", "100% to preferred until the floor is met", "deterministic"),
    ("WF Min Return Accrual Balance ($M)", 198.0, "deterministic"),
    ("WF Accrual Rate %", 10.0, "deterministic"),
    ("WF Cash Received YTD ($M)", 31.5, "deterministic"),
    ("WF Valuation Approach", "Greater of Min Return Floor or As-Converted", "deterministic"),
    ("WF Min Return Floor Value ($M)", 198.0, "deterministic"),
    ("WF As-Converted Value ($M)", 215.0, "deterministic"),
    ("WF Selected Value ($M)", 215.0, "deterministic"),
    ("TC01 Name", "ChampionX", "deterministic"),
    ("TC02 Name", "NOV Inc", "deterministic"),
    ("TC03 Name", "Oceaneering", "deterministic"),
    ("TC03 EV/LTM EBITDA", 10.2, "deterministic"),
    ("WF Structure", "Preferred with 1.35x MOIC floor and drag-along", "deterministic"),
    ("QA Status", "qa_pass", "computed"),
]


def test_tdw_waterfall_golden(golden) -> None:
    memo = _memo(golden, "tdw")
    assert len(TDW_EXPECTED) >= 40
    _assert_expected(memo, TDW_EXPECTED)
    _assert_confidence_bands(memo)
    assert memo.assets[0].flags == []


# ===========================================================================
# Joint vehicle / multi-asset documents
# ===========================================================================


def test_aiof_joint_review_two_assets(golden) -> None:
    memo = _memo(golden, "aiof")
    assert len(memo.assets) == 2
    first, second = memo.assets
    assert first.row_memo_id == memo.memo_id
    assert second.row_memo_id == f"{memo.memo_id}-A2"
    assert first.asset_name == "Broadband Partners"
    assert second.asset_name == "GridCo Transmission"
    _assert_expected(memo, [
        ("Portfolio Company", "Broadband Partners", "deterministic"),
        ("Implied EV ($M)", 440.0, "deterministic"),
        ("Fund Share Equity Value ($M)", 120.0, "deterministic"),
        ("Mult Selected (x)", 11.0, "deterministic"),
        ("Primary Methodology", "Multiple-Market", "deterministic"),
    ], asset_index=0)
    _assert_expected(memo, [
        ("Portfolio Company", "GridCo Transmission", "deterministic"),
        ("Implied EV ($M)", 510.0, "deterministic"),
        ("Fund Share Equity Value ($M)", 95.0, "deterministic"),
        ("DCF Discount Rate Mid %", 7.6, "deterministic"),
        ("Primary Methodology", "DCF", "deterministic"),
    ], asset_index=1)
    # no cross-asset bleed: GridCo must not carry Broadband's multiple
    assert "Mult Selected (x)" not in _values(memo, 1)


def test_mountain_peak_docx_two_assets(golden) -> None:
    memo = _memo(golden, "mountain_peak")
    assert memo.reader == "docx"
    assert len(memo.assets) == 2
    _assert_expected(memo, [
        ("Portfolio Company", "Summit Logistics", "deterministic"),
        ("Mult Selected (x)", 9.0, "deterministic"),
        ("Mult EBITDA / Revenue Base ($M)", 20.0, "deterministic"),  # from the docx table
        ("Fund Share Equity Value ($M)", 72.0, "deterministic"),
    ], asset_index=0)
    _assert_expected(memo, [
        ("Portfolio Company", "Pinecrest Storage", "deterministic"),
        ("Cap Rate Selected %", 6.25, "deterministic"),
        ("Cap Implied Asset Value ($M, local)", 128.0, "deterministic"),
        ("Fund Share Equity Value ($M)", 80.0, "deterministic"),
    ], asset_index=1)


def test_riverbend_xlsx_workbook(golden) -> None:
    memo = _memo(golden, "riverbend")
    assert memo.reader == "xlsx"
    _assert_expected(memo, [
        ("Portfolio Company", "Riverbend Power", "deterministic"),
        ("Primary Methodology", "DCF", "deterministic"),
        ("Implied EV ($M)", 1020.0, "deterministic"),
        ("Net Debt ($M)", 310.0, "deterministic"),
        ("Implied Equity Value 100% ($M)", 710.0, "deterministic"),
        ("Fund Share Equity Value ($M)", 355.0, "deterministic"),
        ("DCF Discount Rate Mid %", 9.5, "deterministic"),
        ("DCF Terminal Growth Rate %", 2.0, "deterministic"),
        ("Gross IRR %", 15.0, "deterministic"),
        ("MOIC", 1.5, "deterministic"),
        ("QA Status", "qa_pass", "computed"),
    ])
    # workbook cells are table-sourced: full confidence
    assert _values(memo)["Implied EV ($M)"][2] == 1.0


# ===========================================================================
# Scanned (OCR) and image-table memos
# ===========================================================================


def test_andover_scanned_ocr_golden(golden) -> None:
    memo = _memo(golden, "andover")
    assert memo.page_classes[1] is PageClass.SCANNED
    hits = memo.assets[0].hits
    ocr_hits = [h for h in hits if h.method == "deterministic"]
    assert ocr_hits, "OCR produced no hits"
    # every deterministic hit came off the OCR'd page: confidence band 0.4-0.7
    for hit in ocr_hits:
        assert 0.40 <= hit.confidence <= 0.70, (hit.field, hit.confidence)
        assert hit.confidence_components["page_class"] <= 0.70
    _assert_expected(memo, [
        ("Fund Manager", "Angeles Investments", "deterministic"),
        ("Portfolio Company", "Andover Storage", "deterministic"),
        ("Primary Methodology", "Cap Rate", "deterministic"),
        ("Primary Method Weight %", 100.0, "deterministic"),
        ("Cap Rate Selected %", 5.75, "deterministic"),
        ("Cap Rate Prior Qtr %", 5.5, "deterministic"),
        ("Cap Rate Change (bps)", 25.0, "computed"),
        ("Cap NOI Basis", "NTM", "deterministic"),
        ("Cap NOI Base ($M, local)", 14.2, "deterministic"),
        ("Cap Implied Asset Value ($M, local)", 247.0, "deterministic"),
        ("Net Debt ($M)", 86.0, "deterministic"),
        ("Fund Share Equity Value ($M)", 99.0, "deterministic"),
        ("Gross IRR %", 12.5, "deterministic"),
        ("MOIC", 1.4, "deterministic"),
    ])
    # Phase-3 seam: every below-threshold OCR field is in the escalation plan
    assert memo.escalation is not None
    escalated = {field.field for field in memo.escalation.fields}
    assert {"Cap Rate Selected %", "Fund Share Equity Value ($M)", "MOIC"} <= escalated
    assert any(f.reason == "below_confidence" for f in memo.escalation.fields)
    assert {f.reason for f in memo.escalation.fields} <= {"primary_catalog", "below_confidence", "required_empty"}
    assert memo.escalation.status == "llm_fallback_disabled"
    assert memo.escalation.page_band_map  # Phase 3 reuses the page routing


def test_hyperoptic_image_table_golden(golden) -> None:
    memo = _memo(golden, "hyperoptic")
    assert memo.page_classes[1] is PageClass.TEXT
    assert memo.page_classes[2] is PageClass.IMAGE_TABLE
    flags = memo.assets[0].flags
    image_flags = [f for f in flags if "image-based table" in f.description]
    assert len(image_flags) == 1
    assert image_flags[0].category == "reader"
    assert image_flags[0].severity is FlagSeverity.warning
    assert image_flags[0].reviewer_attention
    assert "Phase-3 vision" in image_flags[0].description
    assert memo.assets[0].qa_status is QaStatus.qa_pass_with_flags
    _assert_expected(memo, [
        ("Portfolio Company", "Hyperoptic", "deterministic"),
        ("Primary Methodology", "Yield/Spread", "deterministic"),
        ("Yield All-In YTM %", 11.0, "deterministic"),
        ("Yield YTM Change (bps)", 60.0, "computed"),
        ("Yield Par Value ($M, local)", 270.0, "deterministic"),
        ("Yield Maturity Date", "2030-12-31", "deterministic"),
        ("QA Status", "qa_pass_with_flags", "computed"),
        ("Extraction Flags Count", 1, "computed"),
        ("Reviewer Attention", "Y", "computed"),
    ])
    # local-currency field keeps its currency (£270.0mm)
    par = next(h for h in memo.assets[0].hits if h.field == "Yield Par Value ($M, local)")
    assert par.unit == "GBP_millions"
    assert par.raw_text == "£270.0mm"


# ===========================================================================
# Audit sidecar: evidence, provenance, byte stability
# ===========================================================================


def test_every_hit_has_reviewer_evidence(golden) -> None:
    """Every extracted cell must be reproducible from its audit evidence
    snippet (<= 200 chars) by a human reviewer."""
    for key in ("accell", "digital_edge", "sre", "tdw"):
        memo = _memo(golden, key)
        for hit in memo.assets[0].hits:
            assert hit.evidence, (key, hit.field)
            assert len(hit.evidence) <= 200, (key, hit.field)
            if hit.method == "deterministic":
                assert hit.page is not None, (key, hit.field)
                assert hit.confidence_components, (key, hit.field)


def test_audit_sidecar_byte_stable(phase2_env) -> None:
    """Two independent extractions with the same timestamp produce identical
    audit JSON once the volatile timings are excluded."""
    fixed = datetime(2026, 6, 11, 13, 0, 0)

    def run_once() -> bytes:
        report = run(
            phase2_env, scope="deal", client="Angelo Gordon", deal="Accell",
            period="2025-01-31", force=True, now=fixed,
        )
        audit_path = report.run_dir / "audit" / f"{report.memos[0].memo_id}.json"
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        payload.pop("timings_ms", None)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")

    assert run_once() == run_once()


def test_dry_run_coverage_only(phase2_env, tmp_path: Path) -> None:
    report = run(
        phase2_env, scope="client", client="Angelo Gordon", period="2025-01-31",
        dry_run=True, now=datetime(2026, 6, 11, 14, 0, 0),
    )
    assert report.dry_run and report.workbook_path is None and report.memos == []
    statuses = {(c.deal): c.status for c in report.coverage}
    assert statuses["Accell"] == ResolutionStatus.FOUND.value
    assert statuses["T.D. Williamson"] == ResolutionStatus.NOT_YET_UPLOADED.value
