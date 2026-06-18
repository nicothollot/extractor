"""Band specs (D4): FUND, CLASSIFICATION, KPI, STATUS, ASC 820 GOVERNANCE.

Mostly prose label:value fields on the cover/summary pages; controlled
vocabularies for strategy, investment type, methodology type, dev stage and
FV hierarchy level.
"""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

FUND = SpecBandExtractor(
    "FUND",
    [
        spec("Fund Manager", "Manager", "Sponsor", "GP", "General Partner"),
        spec("Fund Name", "Fund", "Vehicle"),
        spec("Fund Vintage", "Vintage", "Vintage Year"),
        spec(
            "Fund Strategy", "Strategy",
            vocab_aliases={
                "Core-Plus": ["core plus"],
                "Value-Add": ["value add", "value added"],
            },
        ),
        spec("Portfolio Company", "Company", "Legal Entity", "Investment"),
        spec("Operating Name", "Trade Name", "dba"),
        spec("Asset/Project Name", "Asset Name", "Project Name", "Asset", "Project"),
    ],
)

CLASSIFICATION = SpecBandExtractor(
    "CLASSIFICATION",
    [
        spec("Country", "Primary Country", "Country of Operations"),
        spec("Region"),
        spec("Jurisdiction", "Tax Jurisdiction"),
        spec("Sector", "Industry"),
        spec("Sub-Sector", "Subsector", "Sub Sector"),
        spec("Asset Type"),
        spec(
            "Investment Type", "Instrument", "Security Type", "Instrument Type",
            vocab_aliases={
                "Common Equity": ["common", "common stock", "ordinary equity"],
                "Preferred Equity": ["preferred", "preferred stock", "prefs"],
                "Structured Equity": ["structured preferred", "structured pref"],
                "HoldCo Loan": ["holdco debt", "holdco note", "holdco loan facility"],
                "OpCo Loan": ["opco debt", "opco note"],
                "Cap Structure (mixed)": ["mixed", "across the capital structure"],
            },
        ),
        spec(
            "Methodology Type",
            vocab_aliases={"Real Asset": ["real assets", "infrastructure"]},
        ),
    ],
)

KPI = SpecBandExtractor(
    "KPI",
    [
        spec("Primary KPI Name", "Primary KPI", "Key Performance Indicator"),
        spec("Primary KPI Value", table_col=["value", "current"]),
        spec("Primary KPI Unit", "KPI Unit"),
        spec("Secondary KPI Name", "Secondary KPI"),
        spec("Secondary KPI Value"),
        spec("Secondary KPI Unit"),
    ],
)

STATUS = SpecBandExtractor(
    "STATUS",
    [
        spec("Revenue Profile"),
        spec(
            "Dev Stage", "Development Stage", "Stage",
            vocab_aliases={"Operating": ["operational", "in operation"]},
        ),
        spec("Ownership %", "Ownership", "Diluted Ownership", "Equity Ownership"),
    ],
)

ASC820 = SpecBandExtractor(
    "ASC 820 GOVERNANCE",
    [
        spec(
            "FV Hierarchy Level", "Fair Value Hierarchy", "ASC 820 Level", "Fair Value Level",
            vocab_aliases={
                "Level 1": ["level i", "1"],
                "Level 2": ["level ii", "2"],
                "Level 3": ["level iii", "3"],
            },
        ),
        spec("Level Changed from Prior", "Level Change"),
        spec("Calibrated to Tx Price", "Calibrated to Transaction Price", "Calibrated"),
        spec("Months Since 3P Corroboration", "Months Since Third-Party Corroboration"),
        spec("Management Overlay Applied", "Management Overlay"),
        spec("DLOM Applied", "DLOM"),
        spec("DLOM %", "Discount for Lack of Marketability"),
        spec("DLOC Applied", "DLOC"),
        spec("DLOC %", "Discount for Lack of Control"),
    ],
)

EXTRACTORS = [FUND, CLASSIFICATION, KPI, STATUS, ASC820]
