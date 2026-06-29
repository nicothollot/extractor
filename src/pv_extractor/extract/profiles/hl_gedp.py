"""Deterministic extraction profile for the HL valuation-memo format, scoped to
the GEDP field set.

GEDP_FIELDS workbooks are CUSTOM references (single header row of ~79 columns).
Without a profile they run LLM-first (the master band extractors only know the
604 master headers). HL valuation memos, however, share a highly consistent
layout — "Summary Valuation Conclusions", "Valuation Trends"/"Valuation Summary",
"Representative Levels at Valuation Date", "Discounted Cash Flow Analysis",
"Reverse Option Pricing Model", "Key Terms" — so this profile maps the GEDP
columns to deterministic recipes and the LLM then only fills what they leave
empty (the normal merge keeps a confident deterministic hit and escalates the
rest).

These memos render to text VERTICALLY — a row label on one line, then its value
cell(s) on the following lines ("Implied MOIC" / "1.09x" / "--" / "1.22x") —
which the master band extractors (table-cell + 'label: value' prose) don't
catch. So this profile uses a line-oriented vertical scanner: find the label
line, then read the value(s) immediately below it. A value that is genuinely
absent is left empty and flagged once — never fabricated (rule 6). Recipes bind
to band "REFERENCE" (the band compile_schema_from_workbook assigns custom cols).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pv_extractor.extract import patterns
from pv_extractor.extract.bands.base import SpecBandExtractor
from pv_extractor.models import FieldHit, FlagSeverity, PageContent, ReviewFlag, SchemaField
from pv_extractor.normalize import normalize_text

# A distinctive subset of GEDP headers — enough to recognize the field set while
# tolerating a few analyst edits (we require MIN_SIGNATURE_HITS of these).
GEDP_SIGNATURE: tuple[str, ...] = (
    "TOT INV VALUE-LOW", "PSV - LOW", "EV - LOW", "MVE - LOW", "GPC-WEIGHT",
    "DCF-WACC", "OPM-VOL (avg)", "OPM-T T LIQ (avg)", "UNREALIZED MOIC-LOW",
    "CSE-WEIGHT", "FD-SHARES", "INDUSTRY CODE",
)
MIN_SIGNATURE_HITS = 10

# dtype/unit overrides for headers compile_schema_from_workbook can mis-infer.
# kind_for_field() reads dtype+unit; these keep the writer/QA honest about type.
DTYPE_OVERRIDES: dict[str, tuple[str, str | None]] = {
    "VALUATION DATE": ("date", None),
    "PORTFOLIO COMPANY": ("string", None),
    "SECURITY CLASS": ("string", None),
    "DCF-WACC": ("percent", "percent"),
    "OPM-WEIGHT": ("percent", "percent"),
    "GPC-WEIGHT": ("percent", "percent"),
    "GT-WEIGHT": ("percent", "percent"),
    "DCF-WEIGHT": ("percent", "percent"),
    "MAA-WEIGHT": ("percent", "percent"),
    "CSE-WEIGHT": ("percent", "percent"),
    "OPM-VOL (avg)": ("percent", "percent"),
    "OPM-T T LIQ (avg)": ("years", "years"),
    "UNREALIZED MOIC-LOW": ("multiple_x", "x"),
    "UNREALIZED MOIC-HIGH": ("multiple_x", "x"),
    "FD-SHARES": ("number", None),
    "CONVERSION RATIO": ("number", None),
}
for _h in (
    "EV - LOW", "EV - HIGH", "MVE - LOW", "MVE - HIGH", "PSV - LOW", "PSV - HIGH",
    "TOT INV VALUE-LOW", "TOT INV VALUE-HIGH", "COST", "GPC-LOW", "GPC-HIGH",
    "GT-LOW", "GT-HIGH", "DCF-LOW", "DCF-HIGH", "MAA-LOW", "MAA-HIGH",
    "CASH", "DEBT", "OTHER NON-OP", "LFY REVENUE", "LFY EBITDA", "LTM REVENUE",
    "LTM EBITDA", "NFY REVENUE", "NFY EBITDA", "NFY+1 REVENUE", "NFY+1 EBITDA",
    "NFY+2 REVENUE", "NFY+2 EBITDA",
):
    DTYPE_OVERRIDES.setdefault(_h, ("number", "USD_millions"))


def matches_signature(headers: set[str]) -> bool:
    """True when enough distinctive GEDP headers are present."""
    norm = {normalize_text(h) for h in headers}
    hits = sum(1 for sig in GEDP_SIGNATURE if normalize_text(sig) in norm)
    return hits >= MIN_SIGNATURE_HITS


# --- vertical-scan recipes ---------------------------------------------------
# kind -> parser returning patterns.ParsedValue | None
_PARSERS = {
    "amount": patterns.parse_amount,
    "percent": patterns.parse_percent,
    "years": patterns.parse_years,
    "multiple": patterns.parse_multiple,
    "number": patterns.parse_number,
}
# Lines that separate value cells in the flattened layout (skipped, not a value).
_SEPARATORS = {"", "nmf", "na", "nm", "n a"}
_FOOTNOTE_RE = re.compile(r"\[\d+\]|\(\d+\)\s*$")
# Page-footer line ("Houlihan Lokey | 6") — never a value cell; its trailing
# page number would otherwise parse as an amount.
_FOOTER_RE = re.compile(r"houlihan\s+lokey", re.IGNORECASE)


@dataclass(frozen=True)
class Recipe:
    """One vertical-scan recipe: find a label line, read the value(s) below."""

    header: str
    anchors: tuple[str, ...]
    kind: str
    pick: str = "first"  # first | low | high
    confidence: float = 0.8
    lookahead: int = 10
    exact: bool = False  # require the label line to EQUAL an anchor (no prefix match)


_RECIPES: tuple[Recipe, ...] = (
    # Summary Valuation Conclusions (p6): "Implied MOIC" / 1.09x / -- / 1.22x.
    Recipe("UNREALIZED MOIC-LOW", ("implied moic", "moic"), "multiple", "low", 0.82),
    Recipe("UNREALIZED MOIC-HIGH", ("implied moic", "moic"), "multiple", "high", 0.82),
    # Goldman's investment value range (p6 "Total Value").
    Recipe("TOT INV VALUE-LOW", ("total value",), "amount", "low", 0.8),
    Recipe("TOT INV VALUE-HIGH", ("total value",), "amount", "high", 0.8),
    # 100%-equity range (p20 "Implied Total Equity Value Range").
    Recipe("MVE - LOW", ("implied total equity value", "implied total equity value range"), "amount", "low", 0.78),
    Recipe("MVE - HIGH", ("implied total equity value", "implied total equity value range"), "amount", "high", 0.78),
    # Enterprise value range (p22 DCF "Enterprise Value" / $896.2 / $1,041.9).
    # Exact match so the "Enterprise Value Calculations" sensitivity-grid header
    # never wins.
    Recipe("EV - LOW", ("enterprise value",), "amount", "low", 0.72, exact=True),
    Recipe("EV - HIGH", ("enterprise value",), "amount", "high", 0.72, exact=True),
    # Reverse OPM / Valuation Trends inputs.
    Recipe("OPM-VOL (avg)", ("equity volatility", "volatility"), "percent", "first", 0.82),
    Recipe("OPM-T T LIQ (avg)", ("time to liquidity",), "years", "first", 0.7),
    # Investor-level DCF discount rate (p15 "Selected Discount Rate").
    Recipe("DCF-WACC", ("selected discount rate", "discount rate", "wacc"), "percent", "first", 0.7),
    # Invested cost (Key Terms p16). The client-specific "<client> Invested
    # Capital" line is preferred over the security-level "Total Invested Capital".
    Recipe("COST", ("goldman sachs invested capital", "total invested capital"), "amount", "first", 0.78),
    Recipe("FD-SHARES", ("fully diluted shares", "fd shares", "shares outstanding"), "number", "first", 0.7),
)

# Per-FY revenue/EBITDA, addressed by the memo's explicit "FY<year> ..." labels
# in Valuation Trends (p15). Offsets are relative to the valuation FY.
_FY_RECIPES: dict[str, tuple[str, int]] = {
    "NFY REVENUE": ("Net Revenue", 0), "NFY EBITDA": ("Adjusted EBITDA", 0),
    "NFY+1 REVENUE": ("Net Revenue", 1), "NFY+1 EBITDA": ("Adjusted EBITDA", 1),
    "NFY+2 REVENUE": ("Net Revenue", 2), "NFY+2 EBITDA": ("Adjusted EBITDA", 2),
}

# Methodology families that may be wholly absent from a given memo.
_METHODOLOGY_FAMILIES = {
    "GPC": ("GPC-LOW", "GPC-HIGH"), "GT": ("GT-LOW", "GT-HIGH"), "MAA": ("MAA-LOW", "MAA-HIGH"),
}

_ASOF_RE = re.compile(r"as of\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
_PORTCO_RE = re.compile(
    r"in\s+([A-Z][A-Za-z0-9.&'\- ]+?),?\s+(?:LLC|L\.?L\.?C\.?|L\.?P\.?|LP|Inc\.?|Ltd\.?|Corp\.?)\b"
)
_SECURITY_RE = re.compile(
    r"(Class\s+[A-Z]\s+[Uu]nits|Class\s+[A-Z]\s+[Pp]referred|Series\s+[A-Z][\w ]*?[Pp]referred|Common\s+[Uu]nits)"
)


def _clean_label(line: str) -> str:
    return normalize_text(_FOOTNOTE_RE.sub("", line))


def _scan(pages: list[PageContent], recipe: Recipe) -> tuple[float, int, str] | None:
    """Find a label line matching an anchor and read the value(s) below.

    For low/high picks ONLY a clean two-cell row is accepted (the
    Summary-Conclusions / Valuation-Summary layout: label -> low -> [--] ->
    high). A multi-column trend row (label followed by 4+ consecutive cells:
    calibration low/high, prior low/high, valuation low/high, % changes …) is
    AMBIGUOUS — we skip it and keep scanning so we land on the clean single-date
    table instead. Returns (value, page_number, evidence) or None."""
    parser = _PARSERS[recipe.kind]
    low_high = recipe.pick in ("low", "high")
    # Anchors are tried in PRIORITY order across all pages — the first anchor
    # that yields a value wins (so e.g. "<client> invested capital" beats the
    # generic "total invested capital" fallback).
    for anchor in (normalize_text(a) for a in recipe.anchors):
        for page in pages:
            lines = [ln.strip() for ln in (page.text or "").splitlines() if ln.strip()]
            for i, line in enumerate(lines):
                norm = _clean_label(line)
                if recipe.exact:
                    if norm != anchor:
                        continue
                elif not (norm == anchor or norm.startswith(anchor + " ")):
                    continue
                values: list[float] = []
                for nxt in lines[i + 1 : i + 1 + recipe.lookahead]:
                    if _FOOTER_RE.search(nxt):  # page footer — skip, never a value
                        continue
                    parsed = parser(nxt)
                    if parsed is not None:
                        values.append(parsed.value)
                        continue
                    if normalize_text(_FOOTNOTE_RE.sub("", nxt)) in _SEPARATORS:
                        continue
                    if values:  # hit the next label after collecting -> stop
                        break
                if low_high and len(values) != 2:
                    continue  # ambiguous multi-column (or incomplete) row — keep scanning
                if not values:
                    continue
                value = values[1] if recipe.pick == "high" else values[0]
                evidence = patterns.snippet(f"{line} -> {value}", 200)
                return value, page.page_number, evidence
    return None


def _regex_first(pages: list[PageContent], rx: re.Pattern, limit: int = 4) -> tuple[str, int] | None:
    for page in pages[:limit]:
        m = rx.search(page.text or "")
        if m:
            return m.group(1).strip().strip('"'), page.page_number
    return None


def _resolve_valuation_year(pages: list[PageContent]) -> tuple[int, str, int] | None:
    """The valuation year + matched date string + page, parsed from a cover-page
    'AS OF <date>' line. None when not found."""
    for page in pages[:4]:
        m = _ASOF_RE.search(page.text or "")
        if m:
            parsed = patterns.parse_date_text(m.group(1))
            if parsed is not None:
                return parsed[0].year, parsed[0].isoformat(), page.page_number
    return None


def _hit(field: SchemaField, value, page: int, evidence: str, confidence: float) -> FieldHit:
    return FieldHit(
        field=field.header, col_index=field.col_index, band=field.band,
        raw_text=evidence, value=value, unit=field.unit, page=page,
        method="deterministic", confidence=confidence, evidence=evidence,
    )


class GedpExtractor(SpecBandExtractor):
    """HL/GEDP deterministic extractor (band REFERENCE): vertical-scan recipes +
    title regexes + valuation-year-relative per-FY revenue/EBITDA, with a single
    'methodology not present' flag for whole families this memo lacks."""

    def __init__(self) -> None:
        super().__init__("REFERENCE", [])

    def extract(self, band_pages: list[PageContent], schema_fields: list[SchemaField], ctx) -> list[FieldHit]:
        by_header = {f.header: f for f in schema_fields}
        pages = sorted(band_pages, key=lambda p: p.page_number)
        hits: list[FieldHit] = []

        # Title / prose regexes.
        if (f := by_header.get("PORTFOLIO COMPANY")) is not None:
            found = _regex_first(pages, _PORTCO_RE)
            if found is not None:
                hits.append(_hit(f, found[0], found[1], f"portfolio company: {found[0]}", 0.8))
        if (f := by_header.get("SECURITY CLASS")) is not None:
            found = _regex_first(pages, _SECURITY_RE)
            if found is not None:
                hits.append(_hit(f, found[0], found[1], f"security: {found[0]}", 0.8))
        val = _resolve_valuation_year(pages)
        if (f := by_header.get("VALUATION DATE")) is not None and val is not None:
            hits.append(_hit(f, val[1], val[2], f"as of {val[1]}", 0.85))

        # Vertical-scan recipes.
        for recipe in _RECIPES:
            f = by_header.get(recipe.header)
            if f is None:
                continue
            scanned = _scan(pages, recipe)
            if scanned is not None:
                hits.append(_hit(f, scanned[0], scanned[1], scanned[2], recipe.confidence))

        # Per-FY revenue/EBITDA via the memo's explicit "FY<year> <metric>" labels.
        if val is not None:
            for header, (metric, offset) in _FY_RECIPES.items():
                f = by_header.get(header)
                if f is None:
                    continue
                anchor = f"FY{val[0] + offset} {metric}"
                fy_recipe = Recipe(header, (anchor,), "amount", "first", 0.8)
                scanned = _scan(pages, fy_recipe)
                if scanned is not None:
                    hits.append(_hit(f, scanned[0], scanned[1], scanned[2], 0.8))

        # Flag whole methodology families present in the schema but unfilled here.
        got = {h.field for h in hits}
        missing = [
            name for name, members in _METHODOLOGY_FAMILIES.items()
            if any(m in by_header for m in members) and not any(m in got for m in members)
        ]
        if missing:
            ctx.flags.append(
                ReviewFlag(
                    category="profile",
                    description=(
                        f"HL/GEDP profile: {', '.join(missing)} methodology field(s) not found "
                        "in this memo — left empty (the LLM pass may still fill them)"
                    ),
                    severity=FlagSeverity.info,
                )
            )
        return hits


def build_extractors() -> list[SpecBandExtractor]:
    return [GedpExtractor()]
