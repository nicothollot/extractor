"""Candidate-page targeting (D2): band extractors never see every page.

Each band gets an anchor lexicon assembled from three sources:

  1. curated seed anchors per band (_BASE_ANCHORS) — terms taken from the
     band names and the row-3 descriptions of the master schema (e.g.
     HEADLINE FINANCIALS <- "enterprise value", "net debt"; DCF <- "wacc",
     "discount rate", "terminal"),
  2. anchors auto-mined from the schema's row-2 field headers (unit
     decorations stripped: 'Implied EV ($M)' -> 'implied ev'), and
  3. config extraction.band_anchor_overrides (user-extensible).

Pages are scored per band by token-bounded anchor occurrences weighted by
anchor specificity (token count). A band extractor receives its top-K pages
(config top_k_pages_per_band) plus pages 1..summary_pages — memos lead with
summary tables. The resulting page->band map is persisted in the audit
record; Phase 3 reuses it to send ONLY those pages to the LLM.
"""

from __future__ import annotations

import re

from pv_extractor.config import ExtractionConfig
from pv_extractor.models import PageContent, SchemaField
from pv_extractor.normalize import normalize_text

# Curated seeds keyed by EXACT schema band name; sourced from the band names
# and row-3 descriptions (see module docstring). Tunables beyond these belong
# in config extraction.band_anchor_overrides, not here.
_BASE_ANCHORS: dict[str, list[str]] = {
    "FUND": ["fund manager", "fund name", "vintage", "portfolio company", "operating name"],
    "CLASSIFICATION": ["sector", "sub sector", "jurisdiction", "country", "investment type", "asset type"],
    "KPI": ["kpi", "key performance indicator"],
    "STATUS": ["ownership", "development stage", "revenue profile", "operating", "construction"],
    "ASC 820 GOVERNANCE": [
        "asc 820", "fair value hierarchy", "level 3", "dlom", "dloc", "calibrated", "overlay",
    ],
    "METHODOLOGY ROUTING": [
        "methodology", "valuation approach", "valuation methodology", "method weight", "weighting",
    ],
    "HEADLINE FINANCIALS": [
        "enterprise value", "equity value", "net debt", "valuation summary", "concluded value",
        "implied ev", "fx rate",
    ],
    "RETURNS": ["gross irr", "net irr", "moic", "dpi", "invested capital", "realized value", "unrealized value"],
    "OPERATING FINANCIALS": [
        "revenue", "ebitda", "ebitda margin", "capex", "distributions", "capacity utilization", "dscr",
    ],
    "CALIBRATION (ENTRY-PRICE ANCHOR)": [
        "entry multiple", "cost basis", "underwrite", "underwriting", "calibration", "investment close",
        "at entry",
    ],
    "QoQ BRIDGE ATTRIBUTION": [
        "bridge", "quarter over quarter", "qoq", "nav change", "attribution", "prior quarter nav",
        "value bridge",
    ],
    "METHODOLOGY: DCF": [
        "wacc", "discount rate", "terminal", "dcf", "discounted cash flow", "gordon growth",
        "exit multiple", "projection period", "beta", "risk free rate", "equity risk premium",
        "cost of equity",
    ],
    "METHODOLOGY: MULTIPLE": [
        "multiple selected", "selected multiple", "ev ebitda", "ev ltm ebitda", "comp set",
        "trading multiple", "size discount", "premium discount",
    ],
    "METHODOLOGY: CAP RATE": ["cap rate", "noi", "net operating income", "stabilized"],
    "METHODOLOGY: YIELD / CREDIT": [
        "yield", "coupon", "ytm", "yield to maturity", "spread", "pik", "par value", "accrued",
        "oid", "gilt", "treasury", "credit rating",
    ],
    "METHODOLOGY: WATERFALL / STRUCTURED EQUITY": [
        "waterfall", "hurdle", "catch up", "liquidation preference", "attach", "detach",
        "drag along", "as converted", "structured equity", "min return", "minimum return",
    ],
    "TRADING COMPS (POSITIONAL SLOTS)": [
        "comparable", "comparable companies", "trading comps", "comp set", "ticker", "peer",
        "public comparables",
    ],
    "TRANSACTION COMPS (POSITIONAL SLOTS)": [
        "precedent transactions", "transaction comps", "acquirer", "target", "announced",
        "precedent", "m a transactions",
    ],
    "CAPITAL STRUCTURE (POSITIONAL SLOTS)": [
        "facility", "tranche", "maturity", "notional", "drawn", "capital structure",
        "revolver", "term loan", "debt summary",
    ],
    "NARRATIVE": ["key risks", "value drivers", "outlook", "overlay", "material changes"],
    "METHODOLOGY NORMALIZED (SUPPLEMENTAL)": ["methodology", "valuation approach", "cross check"],
    "RECOVERED FIELDS (SUPPLEMENTAL)": ["dcf", "exit year", "maturity", "structure", "seniority"],
}

# Unit decorations stripped from headers before mining them as anchors.
_HEADER_NOISE_RE = re.compile(
    r"\(\$m(?:,\s*(?:local|usd))?\)|\(x\)|\(bps\)|\(yrs?\)|\(mult of ebitda\)|y/n|%|\(current\)|\(prior qtr\)",
    re.IGNORECASE,
)
# Slot prefixes (TC01/TX12/CS05) and the key glyph never help page scoring.
_SLOT_PREFIX_RE = re.compile(r"^(?:tc|tx|cs)\d{2}\s+", re.IGNORECASE)

# Generic single tokens that would light up on every page of any memo.
_STOP_ANCHORS = frozenset({
    "name", "date", "value", "type", "notes", "status", "include", "currency", "unit",
    "number", "rationale", "metric", "basis", "low", "mid", "high", "change", "other",
})


def _mined_header_anchor(field: SchemaField) -> str | None:
    header = _SLOT_PREFIX_RE.sub("", field.header)
    header = header.replace("\U0001f511", " ")  # key glyph on 'Memo ID'
    header = _HEADER_NOISE_RE.sub(" ", header)
    anchor = normalize_text(header)
    if not anchor or anchor in _STOP_ANCHORS:
        return None
    if len(anchor) < 4 and " " not in anchor:
        return None
    return anchor


def build_band_lexicons(
    schema_fields: list[SchemaField], cfg: ExtractionConfig
) -> dict[str, list[str]]:
    """Anchor lexicon per band: curated seeds + mined headers + config extras,
    normalized and deduplicated, sorted for determinism."""
    lexicons: dict[str, set[str]] = {}
    for band, seeds in _BASE_ANCHORS.items():
        lexicons.setdefault(band, set()).update(normalize_text(seed) for seed in seeds)
    for field in schema_fields:
        anchor = _mined_header_anchor(field)
        if anchor is not None:
            lexicons.setdefault(field.band, set()).add(anchor)
    for band, extras in cfg.band_anchor_overrides.items():
        lexicons.setdefault(band, set()).update(normalize_text(extra) for extra in extras)
    return {band: sorted(anchor for anchor in anchors if anchor) for band, anchors in lexicons.items()}


def _page_score(padded_text: str, anchors: list[str]) -> float:
    """Sum over anchors of min(occurrences, 3) x token-count weight."""
    score = 0.0
    for anchor in anchors:
        count = padded_text.count(f" {anchor} ")
        if count:
            score += min(count, 3) * len(anchor.split())
    return score


def score_pages_per_band(
    pages: list[PageContent], lexicons: dict[str, list[str]]
) -> dict[str, dict[int, float]]:
    """band -> {page_number: score} for pages scoring > 0."""
    padded: list[tuple[int, str]] = [
        (page.page_number, f" {normalize_text(page.text)} ") for page in pages
    ]
    out: dict[str, dict[int, float]] = {}
    for band, anchors in lexicons.items():
        scores = {
            number: score for number, text in padded if (score := _page_score(text, anchors)) > 0
        }
        if scores:
            out[band] = scores
    return out


def build_page_band_map(
    pages: list[PageContent],
    schema_fields: list[SchemaField],
    cfg: ExtractionConfig,
) -> dict[str, list[int]]:
    """The D2 product: band -> sorted page numbers each extractor receives
    (top-K by anchor score, plus pages 1..summary_pages). Persisted in the
    audit record and reused by Phase 3 for LLM page routing."""
    lexicons = build_band_lexicons(schema_fields, cfg)
    band_scores = score_pages_per_band(pages, lexicons)
    page_count = max((page.page_number for page in pages), default=0)
    summary = list(range(1, min(cfg.summary_pages, page_count) + 1))

    out: dict[str, list[int]] = {}
    for band in lexicons:
        scores = band_scores.get(band, {})
        top = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        top_k = cfg.top_k_pages_per_band_overrides.get(band, cfg.top_k_pages_per_band)
        selected = {number for number, _ in top[:top_k]}
        selected.update(summary)
        out[band] = sorted(selected)
    return out
