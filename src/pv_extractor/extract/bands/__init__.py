"""Band extractor registry (D4).

Every extractor implements extract(band_pages, schema_fields, ctx) ->
list[FieldHit]. The engine routes methodology bands through
schema/band_routing.json — a DCF memo never runs the cap-rate extractor.
"""

from __future__ import annotations

from pv_extractor.extract.bands import (
    bridge,
    cap_rate,
    cap_structure,
    comps,
    dcf,
    fund,
    headline,
    methodology,
    multiple,
    narrative,
    waterfall,
    yield_credit,
)
from pv_extractor.extract.bands.base import ExtractionContext, SpecBandExtractor

ALL_EXTRACTORS = [
    *fund.EXTRACTORS,
    *methodology.EXTRACTORS,
    *headline.EXTRACTORS,
    *bridge.EXTRACTORS,
    *dcf.EXTRACTORS,
    *multiple.EXTRACTORS,
    *cap_rate.EXTRACTORS,
    *yield_credit.EXTRACTORS,
    *waterfall.EXTRACTORS,
    *narrative.EXTRACTORS,
    *comps.EXTRACTORS,
    *cap_structure.EXTRACTORS,
]

__all__ = ["ALL_EXTRACTORS", "ExtractionContext", "SpecBandExtractor"]
