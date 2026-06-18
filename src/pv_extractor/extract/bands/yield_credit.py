"""Band specs (D4): METHODOLOGY: YIELD / CREDIT + the credit rows of
RECOVERED FIELDS — coupon stack, YTM vs market yield, par/accrued/OID."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

YIELD_CREDIT = SpecBandExtractor(
    "METHODOLOGY: YIELD / CREDIT",
    [
        spec(
            "Yield Coupon Type", "Coupon Type",
            vocab_aliases={
                "Cash+PIK": ["cash plus pik", "cash and pik", "cash pik"],
                "Step-Up": ["step up", "stepup"],
            },
        ),
        spec("Yield Cash Coupon Rate %", "Cash Coupon", "Cash Coupon Rate", "Cash Interest Rate"),
        spec("Yield PIK Coupon Rate %", "PIK Coupon Rate", "PIK Coupon", "PIK Rate", "PIK Interest Rate"),
        spec(
            "Yield All-In YTM %", "All-In YTM", "Yield to Maturity", "YTM", "Discount Rate",
            "All-In Yield",
        ),
        spec("Yield All-In YTM Prior Qtr %", "Prior Quarter YTM", "Prior YTM"),
        spec("Yield YTM Change (bps)", "YTM Change"),
        spec("Yield Comp G-Spread (bps)", "G-Spread", "Comparable G-Spread", "Reference Spread"),
        spec(
            "Yield Reference Govt Yield %", "Reference Government Yield", "UK Gilt Yield",
            "US Treasury Yield", "Government Bond Yield",
        ),
        spec("Yield All-In Market Yield %", "All-In Market Yield", "Market Yield"),
        spec("Yield Gap vs Market (bps)", "Gap vs Market", "Yield Gap"),
        spec("Yield Par Value ($M, local)", "Par Value", "Loan Balance", "Principal Balance", "Face Value"),
        spec("Yield Accrued Interest ($M, local)", "Accrued Interest", "Accrued"),
        spec("Yield OID Unamortized ($M, local)", "Unamortized OID", "OID Unamortized", "OID"),
        spec("Yield Cost + Accrued ($M, local)", "Cost Plus Accrued", "Cost + Accrued"),
        spec("Yield Min MOIC Floor", "Minimum MOIC Floor", "MOIC Floor", "Min MOIC"),
        spec("Yield Calibration Risk Rating", "Calibration Risk Rating", "Synthetic Rating", "Implied Rating"),
        spec("Yield Calibration Spread at Origination (bps)", "Spread at Origination", "Origination Spread"),
        spec("Yield Current Spread (bps)", "Current Spread"),
    ],
)

YIELD_RECOVERED = SpecBandExtractor(
    "RECOVERED FIELDS (SUPPLEMENTAL)",
    requires_band="METHODOLOGY: YIELD / CREDIT",
    specs=[
        spec("Yield Structure", "Structure", "Seniority", "Instrument Structure"),
        spec("Yield Maturity Date", "Maturity Date", "Maturity", "Final Maturity"),
    ],
)

EXTRACTORS = [YIELD_CREDIT, YIELD_RECOVERED]
