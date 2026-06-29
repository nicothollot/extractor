"""Realistic synthetic valuation memos (D8), modeled on the schema bands.

RICH_BUILDERS maps fixture-relative paths to builders; build_fixture calls
them instead of writing stub content, so Phase-1 filename/mtime semantics
are untouched while Phase-2 golden tests get full documents:

  * Accell           multiples memo (4pp: identity, multiple methodology,
                     ruled comps table with aggregates, QoQ bridge table)
  * Digital Edge     DCF memo (WACC build-up, Low/Mid/High tables, comps)
  * SRE              yield/credit memo + ruled capital-structure table
  * TDW              waterfall / structured-equity memo
  * Andover          SCANNED memo (pages are images; OCR path)
  * Hyperoptic       text memo whose comps table is an embedded image
                     (IMAGE_TABLE detection -> Phase-3 escalation)
  * AIOF II ANRP III joint 2-asset PDF portfolio review ('Asset Review:'
                     markers -> 2 output rows)
  * Accell Analysis  HL work-product lookalike (letterhead language) for
                     the peek-verifier
  * Blue Owl docx    2-asset portfolio review (heading sections)
  * Blue Owl xlsx    valuation summary workbook (label/value grid)

All content is deterministic; numbers are internally consistent so the
derived-field cross-checks pass (or intentionally fail where a golden test
asserts the flag).
"""

from __future__ import annotations

from pathlib import Path

import fitz

from fixtures.docgen import (
    add_image_table_page,
    add_text_page,
    make_docx,
    make_scanned_pdf,
    make_text_pdf,
    make_xlsx,
)

# ---------------------------------------------------------------------------
# Accell — multiples memo (Angelo Gordon, as of 2025-01-31)
# ---------------------------------------------------------------------------

ACCELL_PAGES: list[list[str]] = [
    [
        "Accell Group — Valuation Memorandum",
        "Prepared by the Manager's Portfolio Valuation Group",
        "Valuation as of January 31, 2025",
        "",
        "Fund Manager: Angelo Gordon",
        "Fund Name: AG Europe Private Equity Fund II",
        "Fund Vintage: 2019",
        "Fund Strategy: Value-Add",
        "Portfolio Company: Accell Group",
        "Operating Name: Accell",
        "Asset/Project Name: Accell Bicycle Platform",
        "Country: Netherlands",
        "Region: Europe",
        "Jurisdiction: Netherlands",
        "Sector: Consumer",
        "Sub-Sector: Recreational Products",
        "Asset Type: Bicycle manufacturer",
        "Investment Type: Common Equity",
        "Methodology Type: Equity",
        "Ownership %: 38.5%",
        "Dev Stage: Operating",
        "FV Hierarchy Level: Level 3",
        "",
        "Primary Methodology: Market multiples",
        "Primary Method Weight %: 100%",
        "Methodology Changed QoQ: No",
        "",
        "Valuation Summary",
        "Implied EV: $545.0M",
        "Net Debt: $120.0M",
        "Implied Equity Value 100%: $425.0M",
        "Fund Share Equity Value: $163.6M",
        "FX Rate (Current): 1.0842",
        "FX Rate (Prior Qtr): 1.0915",
    ],
    [
        "Market Multiple Methodology",
        "",
        "Selected Multiple: 8.5x",
        "Prior Quarter Multiple: 8.2x",
        "Multiple Metric: EV/LTM EBITDA",
        "Basis Year: LTM",
        "EBITDA Base: $64.1M",
        "Premium/Discount to Mean: (15.0)%",
        "Premium/Discount Rationale: Size and liquidity discount vs listed peers",
        "Multiple Implied EV: $545.0M",
        "",
        "Operating Performance",
        "Revenue: $410.0M",
        "EBITDA: $64.1M",
        "EBITDA vs Budget: (3.2)%",
        "Maintenance Capex: $18.0M",
        "Growth Capex: $9.5M",
        "Distributions in Period: $0.0M",
        "Net Debt/EBITDA: 1.9x",
        "DSCR: 2.4",
        "",
        "Calibration to Entry",
        "Investment Close Date: June 30, 2021",
        "Initial Cost Basis: $150.0M",
        "Entry Multiple: 7.6x",
        "Entry Multiple Metric: EV/LTM EBITDA",
        "Entry EBITDA / NOI: $58.0M",
        "Entry TEV: $441.0M",
        "Underwrite Gross IRR: 18.0%",
        "Underwrite Target MOIC: 2.2x",
        "Underwrite Holding Period: 5 years",
        "Calibration Status: Calibrated",
    ],
    [
        "Trading Comparables (peer set as of the valuation date)",
        "",
        "Returns Summary",
        "Gross IRR: 16.4%",
        "Net IRR: 13.1%",
        "MOIC: 1.6x",
        "DPI: 0.25",
        "Fair Value as % of Cost: 109.1%",
        "Unrealized Value: $163.6M",
        "Realized Value: $37.5M",
        "Total Invested Capital: $184.5M",
    ],
    [
        "Quarter-over-Quarter NAV Bridge and Governance",
        "",
        "ASC 820 Governance",
        "Level Changed from Prior: No",
        "Calibrated to Tx Price: Yes",
        "Months Since 3P Corroboration: 7",
        "Management Overlay Applied: No",
        "DLOM Applied: No",
        "DLOC Applied: No",
        "",
        "Narrative",
        "Valuation Tone: Balanced",
        "Key Value Drivers: E-bike demand recovery and inventory normalization",
        "Key Risks: Consumer discretionary exposure and FX translation",
        "Material Changes QoQ: Comp set re-rating drove the modest uplift",
    ],
]

ACCELL_COMPS_TABLE = [
    ["Company", "Ticker", "TEV ($M)", "LTM EBITDA", "EV/LTM EBITDA", "EV/NTM EBITDA", "Include"],
    ["Giant Manufacturing", "9921", "4,120", "505", "8.2x", "7.7x", "Yes"],
    ["Shimano Inc", "7309", "18,500", "2,030", "9.1x", "8.6x", "Yes"],
    ["Thule Group", "THULE", "3,950", "430", "9.2x", "8.8x", "Yes"],
    ["Dorel Industries", "DII.B", "1,150", "148", "7.8x", "7.4x", "No"],
    ["Mean", "", "", "", "8.6x", "8.1x", ""],
    ["Median", "", "", "", "8.7x", "8.2x", ""],
]

ACCELL_BRIDGE_TABLE = [
    ["Driver", "Impact ($M)"],
    ["Prior Qtr NAV", "158.3"],
    ["Operating Performance", "3.1"],
    ["Multiple / Exit Assumption", "1.4"],
    ["Capital Activity", "0.0"],
    ["FX", "0.8"],
    ["Other", "0.0"],
    ["NAV Change", "5.3"],
]


def build_accell_memo(path: Path) -> None:
    make_text_pdf(
        path,
        ACCELL_PAGES,
        tables_by_page={3: [ACCELL_COMPS_TABLE], 4: [ACCELL_BRIDGE_TABLE]},
    )


def build_hl_lookalike(path: Path) -> None:
    """HL work product inside the Analysis folder — the peek-verifier must
    reject it on letterhead/disclaimer language."""
    make_text_pdf(
        path,
        [
            [
                "Accell Group — Valuation Analysis",
                "Houlihan Lokey Financial Advisors",
                "This report is confidential and was prepared exclusively for internal use",
                "of Houlihan Lokey engagement personnel.",
                "Fair value conclusions as of January 31, 2025.",
                "Enterprise value and valuation summary follow.",
            ]
        ],
    )


# ---------------------------------------------------------------------------
# Digital Edge — DCF memo (Angelo Gordon, as of 2026-03-31)
# ---------------------------------------------------------------------------

DIGITAL_EDGE_PAGES: list[list[str]] = [
    [
        "Digital Edge Valuation Memorandum",
        "Valuation as of March 31, 2026",
        "",
        "Fund Manager: Angelo Gordon",
        "Fund Name: AG Asia Realty Fund IV",
        "Fund Vintage: 2021",
        "Fund Strategy: Opportunistic",
        "Portfolio Company: Digital Edge",
        "Operating Name: Digital Edge DC",
        "Asset/Project Name: Pan-Asia Data Center Platform",
        "Country: Singapore",
        "Region: Asia-Pacific",
        "Sector: Digital Infrastructure",
        "Sub-Sector: Data Centers",
        "Investment Type: Common Equity",
        "Methodology Type: Equity",
        "Ownership %: 62.0%",
        "Dev Stage: Operating",
        "FV Hierarchy Level: Level 3",
        "Primary KPI Name: IT Load Capacity",
        "Primary KPI Value: 312",
        "Primary KPI Unit: MW",
        "",
        "Primary Methodology: Discounted Cash Flow",
        "Primary Method Weight %: 80%",
        "Secondary Methodology: Market multiples",
        "Secondary Method Weight %: 20%",
        "Methodology Changed QoQ: No",
        "",
        "Valuation Summary",
        "Implied EV: $2,150.0M",
        "Net Debt: $640.0M",
        "Implied Equity Value 100%: $1,510.0M",
        "Fund Share Equity Value: $936.2M",
    ],
    [
        "DCF Assumptions",
        "",
        "Discount Rate Type: WACC",
        "Risk-Free Rate: 4.1%",
        "Equity Risk Premium: 5.0%",
        "Country Risk Premium: 0.6%",
        "Alpha / Size Premium: 2.0%",
        "Unlevered Beta: 0.78",
        "Relevered Beta: 0.95",
        "% Debt: 35.0%",
        "% Equity: 65.0%",
        "Prior Quarter Discount Rate: 8.3%",
        "Terminal Value Method: Exit Multiple",
        "Terminal Exit Multiple: 16.0x",
        "Terminal Exit Metric: EV/NTM EBITDA",
        "Projection Period: 10 years",
        "Projection Start Date: April 1, 2026",
        "Projection End Date: March 31, 2036",
        "",
        "DCF Implied EV: $2,150.0M",
        "DCF Implied Equity Value: $1,510.0M",
        "Exit Equity Value: $3,420.0M",
        "Exit Year: 2036",
    ],
    [
        "Supporting Trading Comparables",
        "",
        "Selected Multiple: 16.0x",
        "Multiple Metric: EV/NTM EBITDA",
    ],
    [
        "Returns and Operating Summary",
        "",
        "Gross IRR: 21.5%",
        "Net IRR: 17.8%",
        "MOIC: 1.9x",
        "DPI: 0.10",
        "Total Invested Capital: $492.0M",
        "",
        "Revenue: $310.0M",
        "EBITDA: $148.0M",
        "Maintenance Capex: $22.0M",
        "Growth Capex: $260.0M",
        "Capacity Utilization: 78.0%",
        "Net Debt at Asset: $640.0M",
        "",
        "Key Value Drivers: Contracted MW growth and yield-on-cost expansion",
        "Key Risks: Hyperscaler concentration and power constraints",
    ],
]

DIGITAL_EDGE_DCF_TABLE = [
    ["Metric", "Low", "Mid", "High"],
    ["Discount Rate", "8.0%", "8.5%", "9.0%"],
    ["DCF Output", "1,980", "2,150", "2,330"],
    ["Implied Multiple", "14.9x", "16.2x", "17.5x"],
]

DIGITAL_EDGE_COMPS_TABLE = [
    ["Company", "Ticker", "EV/LTM EBITDA", "EV/NTM EBITDA", "Beta", "Include"],
    ["Equinix", "EQIX", "22.5x", "20.1x", "0.62", "Yes"],
    ["Digital Realty", "DLR", "19.8x", "18.2x", "0.71", "Yes"],
    ["Keppel DC REIT", "AJBU", "18.4x", "17.0x", "0.55", "No"],
    ["Mean", "", "20.2x", "18.4x", "", ""],
]


def build_digital_edge_memo(path: Path) -> None:
    make_text_pdf(
        path,
        DIGITAL_EDGE_PAGES,
        tables_by_page={2: [DIGITAL_EDGE_DCF_TABLE], 3: [DIGITAL_EDGE_COMPS_TABLE]},
    )


# ---------------------------------------------------------------------------
# Summit Ridge Energy — yield/credit memo + cap structure (as of 2026-03-31)
# ---------------------------------------------------------------------------

SRE_PAGES: list[list[str]] = [
    [
        "Summit Ridge Energy Valuation Memorandum",
        "Valuation as of March 31, 2026",
        "",
        "Fund Manager: Apollo Global Management",
        "Fund Name: AIOF II / ANRP III (joint)",
        "Portfolio Company: Summit Ridge Energy",
        "Operating Name: Summit Ridge Energy",
        "Asset/Project Name: SRE Community Solar Platform",
        "Country: United States",
        "Region: North America",
        "Sector: Power",
        "Sub-Sector: Generation - Renewable",
        "Investment Type: Structured Equity",
        "Methodology Type: Hybrid",
        "Revenue Profile: Contracted",
        "Dev Stage: Operating",
        "FV Hierarchy Level: Level 3",
        "Primary KPI Name: Solar Capacity Online",
        "Primary KPI Value: 500",
        "Primary KPI Unit: MW (DC)",
        "",
        "Primary Methodology: Yield",
        "Primary Method Weight %: 100%",
        "Methodology Changed QoQ: No",
        "",
        "Valuation Summary",
        "Fund Share Equity Value: $248.0M",
        "Net Debt: $(17.0)M",
    ],
    [
        "Yield / Credit Analysis",
        "",
        "Coupon Type: Cash+PIK",
        "Cash Coupon Rate: 6.0%",
        "PIK Coupon Rate: 7.5%",
        "All-In YTM: 13.5%",
        "Prior Quarter YTM: 13.0%",
        "Comparable G-Spread: 625 bps",
        "Reference Government Yield: 4.3%",
        "All-In Market Yield: 10.6%",
        "Par Value: $185.0M",
        "Accrued Interest: $12.4M",
        "Unamortized OID: $3.1M",
        "Cost Plus Accrued: $194.3M",
        "Minimum MOIC Floor: 1.3x",
        "Calibration Risk Rating: B+",
        "Spread at Origination: 700 bps",
        "Current Spread: 625 bps",
        "Structure: HoldCo structured preferred",
        "Maturity Date: June 30, 2029",
    ],
    [
        "Capital Structure Summary",
    ],
    [
        "Returns and Governance",
        "",
        "Gross IRR: 24.0%",
        "Net IRR: 19.5%",
        "MOIC: 1.94x",
        "DPI: 0.26",
        "Fair Value as % of Cost: 166.0%",
        "Unrealized Value: $247.0M",
        "Realized Value: $45.0M",
        "Total Invested Capital: $176.0M",
        "",
        "Calibrated to Tx Price: Yes",
        "Months Since 3P Corroboration: 45",
        "Management Overlay Applied: No",
        "",
        "Key Risks: ITC monetization timing and interconnection queues",
        "Material Changes QoQ: Discount rate aligned to the 13.5% PIK rate ahead of a potential 1H 2026 monetization",
    ],
]

SRE_CAP_TABLE = [
    ["Facility", "Seniority", "Currency", "Notional", "Drawn", "Coupon", "Maturity"],
    ["Cash & Equivalents", "Cash", "USD", "42.0", "", "", ""],
    ["Senior Secured Term Loan", "1L", "USD", "250.0", "230.0", "7.25%", "6/30/2028"],
    ["Tax Equity Facility", "2L", "USD", "110.0", "95.0", "9.00%", "9/30/2027"],
    ["HoldCo Structured Preferred", "Pref Equity", "USD", "185.0", "185.0", "13.50%", "6/30/2029"],
    ["Common Equity", "Common Equity", "USD", "95.0", "", "", ""],
]


def build_sre_memo(path: Path) -> None:
    make_text_pdf(path, SRE_PAGES, tables_by_page={3: [SRE_CAP_TABLE]})


# ---------------------------------------------------------------------------
# T.D. Williamson — waterfall / structured equity memo (as of 2025-12-31)
# ---------------------------------------------------------------------------

TDW_PAGES: list[list[str]] = [
    [
        "T.D. Williamson Valuation Memorandum",
        "Valuation as of December 31, 2025",
        "",
        "Fund Manager: Angelo Gordon",
        "Fund Name: AG Energy Transition Partners",
        "Portfolio Company: T.D. Williamson",
        "Operating Name: TDW",
        "Country: United States",
        "Sector: Energy Services",
        "Investment Type: Structured Equity",
        "Methodology Type: Hybrid",
        "Ownership %: 28.0%",
        "FV Hierarchy Level: Level 3",
        "",
        "Primary Methodology: Waterfall",
        "Primary Method Weight %: 60%",
        "Secondary Methodology: Market multiples",
        "Secondary Method Weight %: 40%",
        "Methodology Changed QoQ: No",
        "",
        "Valuation Summary",
        "Implied EV: $830.0M",
        "Net Debt: $190.0M",
        "Implied Equity Value 100%: $640.0M",
        "Fund Share Equity Value: $215.0M",
    ],
    [
        "Waterfall / Structured Equity Analysis",
        "",
        "Waterfall Type: Min Return Floor",
        "Hurdle IRR: 10.0%",
        "Hurdle MOIC: 1.35x",
        "Attach Point: 1.3x",
        "Detach Point: 3.1x",
        "LTV at Attach: 37.0%",
        "Minimum Annual Cash Distribution: 7.0%",
        "Drag-Along Trigger: 7 years",
        "Tier 1 Split: 100% to preferred until the floor is met",
        "Tier 2 Split: 80/20 to common above the hurdle",
        "Tier 3 Split: 50/50 shared upside above the cap",
        "Minimum Return Accrual Balance: $198.0M",
        "Accrual Rate: 10.0%",
        "Cash Received YTD: $31.5M",
        "Waterfall Valuation Approach: Greater of Min Return Floor or As-Converted",
        "Floor Value: $198.0M",
        "As-Converted Value: $215.0M",
        "Selected Value: $215.0M",
        "Waterfall Structure: Preferred with 1.35x MOIC floor and drag-along",
    ],
    [
        "Market Multiple Cross-Check",
        "",
        "Selected Multiple: 8.3x",
        "Prior Quarter Multiple: 7.7x",
        "Multiple Metric: EV/LTM EBITDA",
        "EBITDA Base: $100.0M",
        "Premium/Discount to Mean: (15.0)%",
        "",
        "Calibration to Entry",
        "Entry Multiple: 7.7x",
        "Entry Multiple Metric: EV/LTM EBITDA",
        "Initial Cost Basis: $165.0M",
        "Underwrite Target MOIC: 1.35x",
    ],
    [
        "Returns and Operating Summary",
        "",
        "Gross IRR: 14.2%",
        "Net IRR: 11.0%",
        "MOIC: 1.3x",
        "DPI: 0.19",
        "Total Invested Capital: $165.0M",
        "",
        "Revenue: $480.0M",
        "EBITDA: $100.0M",
        "",
        "Key Risks: Pipeline integrity capex cycles",
        "Key Value Drivers: Energy transition services backlog",
        "Material Changes QoQ: Multiple re-rated +0.6x on peer recovery",
    ],
]

TDW_COMPS_TABLE = [
    ["Company", "EV/LTM EBITDA", "Include"],
    ["ChampionX", "9.4x", "Yes"],
    ["NOV Inc", "8.1x", "Yes"],
    ["Oceaneering", "10.2x", "Yes"],
    ["Mean", "9.2x", ""],
    ["Median", "9.4x", ""],
]


def build_tdw_memo(path: Path) -> None:
    make_text_pdf(path, TDW_PAGES, tables_by_page={3: [TDW_COMPS_TABLE]})


# ---------------------------------------------------------------------------
# Andover Storage — SCANNED memo (no text layer; OCR) (as of 2026-03-31)
# ---------------------------------------------------------------------------

ANDOVER_LINES = [
    "Andover Storage Valuation Memorandum",
    "Valuation as of March 31, 2026",
    "Fund Manager: Angeles Investments",
    "Portfolio Company: Andover Storage",
    "Primary Methodology: Cap Rate",
    "Primary Method Weight %: 100%",
    "Cap Rate Selected: 5.75%",
    "Prior Quarter Cap Rate: 5.50%",
    "NOI Basis: NTM",
    "NOI Base: $14.2M",
    "Implied Asset Value: $247.0M",
    "Net Debt: $86.0M",
    "Fund Share Equity Value: $99.0M",
    "Gross IRR: 12.5%",
    "MOIC: 1.4x",
]


def build_andover_scanned_memo(path: Path) -> None:
    make_scanned_pdf(path, [ANDOVER_LINES])


# ---------------------------------------------------------------------------
# Hyperoptic — text memo with an IMAGE comps table (as of 2026-03-31)
# ---------------------------------------------------------------------------

HYPEROPTIC_PAGE1 = [
    "Hyperoptic Valuation Memorandum",
    "Valuation as of March 31, 2026",
    "",
    "Fund Manager: Apollo Global Management",
    "Fund Name: Apollo European Infrastructure Fund",
    "Portfolio Company: Hyperoptic",
    "Country: United Kingdom",
    "Investment Type: HoldCo Loan",
    "Methodology Type: Credit",
    "FV Hierarchy Level: Level 3",
    "",
    "Primary Methodology: Yield",
    "Primary Method Weight %: 100%",
    "",
    "Valuation Summary",
    "Fund Share Equity Value: $310.0M",
    "Par Value: £270.0mm",
    "All-In YTM: 11.0%",
    "Prior Quarter YTM: 10.4%",
    "Reference Government Yield: 4.4%",
    "All-In Market Yield: 9.1%",
    "Coupon Type: PIK",
    "PIK Coupon Rate: 9.5%",
    "Maturity Date: December 31, 2030",
]

HYPEROPTIC_PAGE2_TEXT = [
    "Benchmark Yield Comparables",
    "The comparable lending spreads below are shown for reference only;",
    "the concluded yield reflects equity-return pricing of the holdco loan.",
    "The table was provided by the manager as a pasted exhibit image.",
    "Fair value sensitivity to the yield assumption is approximately",
    "plus or minus $9M per 50bps.",
]

HYPEROPTIC_IMAGE_TABLE = [
    ["Issuer", "Spread (bps)", "Yield"],
    ["UK FTTP Issuer A", "612", "10.4%"],
    ["UK FTTP Issuer B", "655", "10.9%"],
    ["EU AltNet Issuer C", "640", "10.7%"],
]


def build_hyperoptic_memo(path: Path) -> None:
    doc = fitz.open()
    add_text_page(doc, HYPEROPTIC_PAGE1)
    add_image_table_page(doc, HYPEROPTIC_PAGE2_TEXT, HYPEROPTIC_IMAGE_TABLE)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


# ---------------------------------------------------------------------------
# AIOF II ANRP III — joint 2-asset PDF portfolio review (as of 2026-03-31)
# ---------------------------------------------------------------------------

AIOF_PAGES: list[list[str]] = [
    [
        "AIOF II ANRP III Portfolio Review Q1 2026",
        "Valuation as of March 31, 2026",
        "Fund Manager: Apollo Global Management",
        "Fund Name: AIOF II / ANRP III (joint)",
        "Fund Strategy: Opportunistic",
        "This quarterly review covers the joint vehicle's two portfolio assets.",
    ],
    [
        "Asset Review: Broadband Partners",
        "",
        "Portfolio Company: Broadband Partners",
        "Sector: Telecommunications",
        "Investment Type: Common Equity",
        "Primary Methodology: Market multiples",
        "Primary Method Weight %: 100%",
        "Selected Multiple: 11.0x",
        "Multiple Metric: EV/LTM EBITDA",
        "EBITDA Base: $40.0M",
        "Implied EV: $440.0M",
        "Net Debt: $140.0M",
        "Implied Equity Value 100%: $300.0M",
        "Fund Share Equity Value: $120.0M",
        "Gross IRR: 13.5%",
        "MOIC: 1.5x",
    ],
    [
        "Asset Review: GridCo Transmission",
        "",
        "Portfolio Company: GridCo Transmission",
        "Sector: Power",
        "Investment Type: Common Equity",
        "Primary Methodology: Discounted Cash Flow",
        "Primary Method Weight %: 100%",
        "Discount Rate: 7.6%",
        "Terminal Growth Rate: 2.0%",
        "Implied EV: $510.0M",
        "Net Debt: $215.0M",
        "Implied Equity Value 100%: $295.0M",
        "Fund Share Equity Value: $95.0M",
        "Gross IRR: 10.8%",
        "MOIC: 1.3x",
    ],
]


def build_aiof_review(path: Path) -> None:
    make_text_pdf(path, AIOF_PAGES)


# ---------------------------------------------------------------------------
# Blue Owl — docx portfolio review (2 asset sections) + xlsx workbook
# ---------------------------------------------------------------------------


def build_mountain_peak_docx(path: Path) -> None:
    make_docx(
        path,
        [
            (
                None,
                [
                    "Mountain Peak Holdings Portfolio Review Q1 2026",
                    "Valuation as of March 31, 2026",
                    "Fund Manager: Blue Owl",
                    "Fund Name: Blue Owl Infrastructure Fund I",
                ],
                [],
            ),
            (
                "Summit Logistics",
                [
                    "Portfolio Company: Summit Logistics",
                    "Investment Type: Common Equity",
                    "Primary Methodology: Market multiples",
                    "Primary Method Weight %: 100%",
                    "Selected Multiple: 9.0x",
                    "Multiple Metric: EV/LTM EBITDA",
                    "Implied EV: $180.0M",
                    "Net Debt: $60.0M",
                    "Implied Equity Value 100%: $120.0M",
                    "Fund Share Equity Value: $72.0M",
                ],
                [[["Metric", "Value"], ["EBITDA Base", "$20.0M"], ["Revenue", "$95.0M"]]],
            ),
            (
                "Pinecrest Storage",
                [
                    "Portfolio Company: Pinecrest Storage",
                    "Investment Type: Common Equity",
                    "Primary Methodology: Cap Rate",
                    "Primary Method Weight %: 100%",
                    "Cap Rate Selected: 6.25%",
                    "NOI Base: $8.0M",
                    "Implied Asset Value: $128.0M",
                    "Net Debt: $48.0M",
                    "Fund Share Equity Value: $80.0M",
                ],
                [],
            ),
        ],
    )


def build_riverbend_xlsx(path: Path) -> None:
    make_xlsx(
        path,
        {
            "Summary": [
                ["Riverbend Power Valuation Summary Q1 2026", None],
                ["Valuation as of", "March 31, 2026"],
                ["Fund Manager", "Blue Owl"],
                ["Fund Name", "Blue Owl Infrastructure Fund I"],
                ["Portfolio Company", "Riverbend Power"],
                ["Investment Type", "Common Equity"],
                ["Primary Methodology", "DCF"],
                ["Primary Method Weight %", "100%"],
                ["Discount Rate", "9.5%"],
                ["Terminal Growth Rate", "2.0%"],
                ["Implied EV", "$1,020.0M"],
                ["Net Debt", "$310.0M"],
                ["Implied Equity Value 100%", "$710.0M"],
                ["Fund Share Equity Value", "$355.0M"],
                ["Gross IRR", "15.0%"],
                ["MOIC", "1.5x"],
            ],
        },
    )


# ---------------------------------------------------------------------------
# registry consumed by build_fixture
# ---------------------------------------------------------------------------

RICH_BUILDERS: dict[str, callable] = {
    "Angelo Gordon/Accell/(5) 1.31.25/Client/Accell Valuation Memo 1.31.25 vf.pdf": build_accell_memo,
    "Angelo Gordon/Accell/(5) 1.31.25/Analysis/Accell Valuation Memo 1.31.25.pdf": build_hl_lookalike,
    "Angelo Gordon/+Digital Edge/Q1 2026/Client/Digital Edge Valuation Memo Q1 2026.pdf": build_digital_edge_memo,
    "Apollo Global Management/Summit Ridge Energy/03-31-2026 SRE Valuation Memo_vf.pdf": build_sre_memo,
    "Angelo Gordon/T.D. Williamson/(2) 12-31-2025/Client/TDW Valuation Memo 12-31-2025 (003).pdf": build_tdw_memo,
    "Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Memo 03.2026.pdf": build_andover_scanned_memo,
    "Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic Valuation Memo Q1 2026.pdf": build_hyperoptic_memo,
    "Apollo Global Management/AIOF II ANRP III/Q1 2026/Client/AIOF II ANRP III Portfolio Review Q1 2026.pdf": build_aiof_review,
    "Blue Owl/Mountain Peak Holdings/Q1 2026/Client/Mountain Peak Portfolio Review Q1 2026.docx": build_mountain_peak_docx,
    "Blue Owl/Riverbend Power/Q1 2026/Client/Riverbend Power Valuation Summary Q1 2026.xlsx": build_riverbend_xlsx,
}
