"""Band specs (D4): NARRATIVE — free-text capture (label-line values kept
verbatim; no parsing beyond the label match)."""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor, spec

NARRATIVE = SpecBandExtractor(
    "NARRATIVE",
    [
        spec("Valuation Tone", "Tone"),
        spec("Management Overlay Rationale", "Overlay Rationale"),
        spec("Key Value Drivers", "Value Drivers"),
        spec("Key Risks", "Risks", "Key Risk Factors"),
        spec("Material Changes QoQ", "Material Changes", "Quarter-over-Quarter Changes"),
    ],
)

EXTRACTORS = [NARRATIVE]
