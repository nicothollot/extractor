"""Band specs (D4): METHODOLOGY: CAP RATE — direct-capitalization fields for
real-asset memos. Local vs USD NOI fields keep their currency (millions_local
unit handling in bands/base.parse_value)."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

CAP_RATE = SpecBandExtractor(
    "METHODOLOGY: CAP RATE",
    [
        spec("Cap Rate Selected %", "Selected Cap Rate", "Cap Rate", "Applied Cap Rate"),
        spec("Cap Rate Prior Qtr %", "Prior Quarter Cap Rate", "Prior Cap Rate"),
        spec("Cap Rate Change (bps)", "Cap Rate Change"),
        spec("Cap NOI Basis", "NOI Basis"),
        spec("Cap NOI Base ($M, local)", "NOI Base", "NOI", "Net Operating Income"),
        spec("Cap NOI Base ($M, USD)", "NOI Base USD", "NOI (USD)"),
        spec("Cap NOI Currency", "NOI Currency"),
        spec("Cap Implied Asset Value ($M, local)", "Implied Asset Value", "Capitalized Value"),
        spec("Cap Implied Asset Value ($M, USD)", "Implied Asset Value USD", "Capitalized Value (USD)"),
        spec("Cap Comp Tx Cap Rate Median %", "Comparable Transaction Cap Rate Median", "Comp Cap Rate Median"),
        spec("Cap Premium/Discount to Comp (bps)", "Premium/Discount to Comps"),
        spec("Cap Premium/Discount Rationale"),
        spec("Cap Implied Cap Rate on NTM NOI %", "Implied Cap Rate on NTM NOI", "Implied NTM Cap Rate"),
    ],
)

EXTRACTORS = [CAP_RATE]
