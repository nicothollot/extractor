"""Deterministic extraction profiles for known document formats.

A profile maps a CUSTOM reference workbook's field set to deterministic
extractors that read a specific document layout, so a custom run still gets
deterministic values (the LLM then only fills the gaps) instead of being purely
LLM-first. Profiles are selected by header-set signature, or forced via
config.extraction.profile.
"""

from __future__ import annotations

from pv_extractor.extract.bands.base import SpecBandExtractor
from pv_extractor.extract.profiles import hl_gedp
from pv_extractor.models import SchemaField

# Registry: profile name -> (signature matcher, extractor factory, dtype overrides).
_PROFILES = {
    "hl_gedp": (hl_gedp.matches_signature, hl_gedp.build_extractors, hl_gedp.DTYPE_OVERRIDES),
}


def detect_profile(fields: list[SchemaField], *, forced: str | None = None) -> str | None:
    """The profile for this field set. `forced` (config.extraction.profile) wins
    when it names a known profile; otherwise auto-detect by header signature.
    Returns None when nothing matches (the run stays LLM-first)."""
    if forced:
        return forced if forced in _PROFILES else None
    headers = {f.header for f in fields}
    for name, (matcher, _factory, _overrides) in _PROFILES.items():
        if matcher(headers):
            return name
    return None


def extractors_for_profile(profile: str | None) -> list[SpecBandExtractor]:
    entry = _PROFILES.get(profile or "")
    return entry[1]() if entry else []


def dtype_overrides_for_profile(profile: str | None) -> dict[str, tuple[str, str | None]]:
    entry = _PROFILES.get(profile or "")
    return dict(entry[2]) if entry else {}
