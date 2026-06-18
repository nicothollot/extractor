"""Shared pattern toolkit for deterministic field extraction (D4).

Primitives shared by every band extractor: numeric/value parsing (currency
amounts with parenthesized negatives and scale words, percents vs bps,
multiples, dates, booleans), label:value line scanning over prose, and
fuzzy table-cell lookup by (row label, column header).

Parsing is conservative: a parse either succeeds with an explicit
`ParsedValue` (carrying whether repairs were needed — that feeds the
confidence model) or returns None. Nothing is ever guessed from ambiguous
text; a failed parse upstream becomes a review flag, never a silent None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from pv_extractor.indexer.periods import parse_date_folder
from pv_extractor.models import TableData
from pv_extractor.normalize import normalize_text

# ---------------------------------------------------------------------------
# Parsed value
# ---------------------------------------------------------------------------


@dataclass
class ParsedValue:
    """One successfully parsed scalar with parse provenance."""

    value: float | bool | str
    raw: str  # the exact substring consumed
    currency: str | None = None  # USD | GBP | EUR | ... when a symbol/code was present
    scale: str | None = None  # millions | billions | thousands | None (no scale word)
    clean: bool = True  # False when the value needed repair (confidence: parse_lenient)
    notes: list[str] = field(default_factory=list)


_CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "£": "GBP",
    "GBP": "GBP",
    "€": "EUR",
    "EUR": "EUR",
    "C$": "CAD",
    "CAD": "CAD",
    "A$": "AUD",
    "AUD": "AUD",
}

_SCALE_WORDS = {
    "m": "millions",
    "mm": "millions",
    "million": "millions",
    "millions": "millions",
    "bn": "billions",
    "b": "billions",
    "billion": "billions",
    "billions": "billions",
    "k": "thousands",
    "thousand": "thousands",
    "thousands": "thousands",
}

_SCALE_FACTOR_TO_MILLIONS = {"millions": 1.0, "billions": 1000.0, "thousands": 0.001, None: None}

# A number: optional sign, thousands groups or plain digits, optional decimals.
_NUM = r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?"
_CUR = r"(?:US\$|C\$|A\$|\$|£|€|USD|GBP|EUR|CAD|AUD)"
_SCALE = r"(?:mm|m|bn|b|k|million(?:s)?|billion(?:s)?|thousand(?:s)?)"

# Full amount: optional currency, optionally parenthesized number, optional
# scale word. The guard after the number refuses digit continuations (no
# partial-number matches), percents, multiples ('8.3x') and bps quantities —
# those belong to the dedicated parsers below.
_AMOUNT_RE = re.compile(
    rf"(?P<cur>{_CUR})?\s*"
    rf"(?:\((?P<pnum>{_NUM})\)|(?P<num>{_NUM}))"
    rf"(?!\d|[.,]\d|\s*%|\s*x\b|\s*bps?\b)"
    rf"(?:\s*(?P<scale>{_SCALE})\b)?",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(
    rf"(?:\((?P<pnum>{_NUM})\)|(?P<num>{_NUM}))\s*(?:%|percent\b|per\s*cent\b)", re.IGNORECASE
)
_BPS_RE = re.compile(
    rf"(?:\((?P<pnum>{_NUM})\)|(?P<num>{_NUM}))\s*(?:bps|bp|basis\s+points?)\b", re.IGNORECASE
)
_MULTIPLE_RE = re.compile(rf"(?:\((?P<pnum>{_NUM})\)|(?P<num>{_NUM}))\s*x\b", re.IGNORECASE)
_YEARS_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?:-?\s*(?:years?|yrs?|yr))\b", re.IGNORECASE
)
# Same partial-match guard as amounts: never let '5.50%' backtrack to '5'.
_BARE_NUMBER_RE = re.compile(rf"(?:\((?P<pnum>{_NUM})\)|(?P<num>{_NUM}))(?!\d|[.,]\d|\s*%)")

_TRUE_WORDS = frozenset({"y", "yes", "true", "1"})
_FALSE_WORDS = frozenset({"n", "no", "false", "0"})

_MONTH_TOKEN = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)

# Candidate substrings handed to periods.parse_date_folder, tried in priority
# order so a numeric date is never partially eaten by a looser word pattern.
_DATE_CANDIDATE_RES = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),  # ISO
    re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"),  # US numeric m-d-y
    # Mar-26 / Mar'26 (2-digit year needs a tight separator) / March 2026.
    re.compile(rf"(?:{_MONTH_TOKEN})\.?(?:[-.]\d{{2}}\b|\s?'\d{{2}}|[ \-.]?\d{{4}})", re.IGNORECASE),
    re.compile(r"(?:fy|q[1-4])[ \-.]?\d{2,4}|\d{4}[ \-.]?q[1-4]", re.IGNORECASE),  # FY2025, Q1 2026
    re.compile(r"\d{1,2}[./-]\d{4}"),  # 03.2026 month-year
)
_MDY_TEXT_RE = re.compile(
    rf"(?P<month>{_MONTH_TOKEN})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})", re.IGNORECASE
)
_DMY_TEXT_RE = re.compile(
    rf"(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_TOKEN})\s+(?P<year>\d{{4}})", re.IGNORECASE
)
_MONTHS = {
    name: i % 12 + 1
    for i, name in enumerate(
        "january february march april may june july august september october november december".split()
    )
}

# Financial-basis tags ("LTM EBITDA", "FY+1", "2025E", "CY+2", "NTM").
_BASIS_RE = re.compile(
    r"\b(LTM|NTM|TTM|(?:FY|CY)\s*\+\s*\d|FY\s*\d{2,4}|CY\s*\d{2,4}|\d{4}\s*[AEP]\b|Stabilized|Run[- ]Rate)\b",
    re.IGNORECASE,
)

_FOOTNOTE_RE = re.compile(r"\(\d\)$|\[\d\]$|[*†‡]+$")


def _to_float(num_text: str) -> float:
    return float(num_text.replace(",", ""))


def _month_number(token: str) -> int | None:
    token = token.lower().rstrip(".")
    for name, number in _MONTHS.items():
        if name.startswith(token[:3]) and (len(token) <= 4 or name == token):
            return number
    return None


# ---------------------------------------------------------------------------
# Scalar parsers
# ---------------------------------------------------------------------------


def parse_amount(text: str) -> ParsedValue | None:
    """First currency amount in `text`: '$1,234.5M', '(123.4)', '£75mm',
    '€1.2bn', '12.5 million'. Parenthesized numbers are negatives."""
    m = _AMOUNT_RE.search(text)
    if m is None:
        return None
    raw_num = m.group("pnum") or m.group("num")
    value = _to_float(raw_num)
    if m.group("pnum") is not None:
        value = -abs(value)
    scale_token = (m.group("scale") or "").lower() or None
    cur_token = m.group("cur")
    # A bare 'm'/'b' immediately after a number with no currency is still a
    # scale word ('75mm'); but a trailing standalone letter that started a
    # word was excluded by the (?![\w%]) guard already.
    return ParsedValue(
        value=value,
        raw=m.group(0).strip(),
        currency=_CURRENCY_SYMBOLS.get((cur_token or "").upper(), None) if cur_token else None,
        scale=_SCALE_WORDS.get(scale_token) if scale_token else None,
    )


def parse_percent(text: str) -> ParsedValue | None:
    """First percentage in `text`, in percent units ('8.5%' -> 8.5).
    Accepts bps and converts (50bps -> 0.5, marked clean: exact arithmetic)."""
    m = _PERCENT_RE.search(text)
    if m is not None:
        raw_num = m.group("pnum") or m.group("num")
        value = _to_float(raw_num)
        if m.group("pnum") is not None:
            value = -abs(value)
        return ParsedValue(value=value, raw=m.group(0).strip())
    m = _BPS_RE.search(text)
    if m is not None:
        raw_num = m.group("pnum") or m.group("num")
        value = _to_float(raw_num)
        if m.group("pnum") is not None:
            value = -abs(value)
        return ParsedValue(value=value / 100.0, raw=m.group(0).strip(), notes=["converted_from_bps"])
    return None


def parse_bps(text: str) -> ParsedValue | None:
    """First basis-point quantity ('+50bps' -> 50.0). Accepts percents and
    converts ('0.5%' -> 50.0)."""
    m = _BPS_RE.search(text)
    if m is not None:
        raw_num = m.group("pnum") or m.group("num")
        value = _to_float(raw_num)
        if m.group("pnum") is not None:
            value = -abs(value)
        return ParsedValue(value=value, raw=m.group(0).strip())
    m = _PERCENT_RE.search(text)
    if m is not None:
        raw_num = m.group("pnum") or m.group("num")
        value = _to_float(raw_num)
        if m.group("pnum") is not None:
            value = -abs(value)
        return ParsedValue(value=value * 100.0, raw=m.group(0).strip(), notes=["converted_from_percent"])
    return None


def parse_multiple(text: str) -> ParsedValue | None:
    """First multiple in `text`: '8.3x' -> 8.3, '(0.6)x' -> -0.6."""
    m = _MULTIPLE_RE.search(text)
    if m is None:
        return None
    raw_num = m.group("pnum") or m.group("num")
    value = _to_float(raw_num)
    if m.group("pnum") is not None:
        value = -abs(value)
    return ParsedValue(value=value, raw=m.group(0).strip())


def parse_years(text: str) -> ParsedValue | None:
    """'5 years' / '5-yr' / '7yr' -> 5.0 / 5.0 / 7.0; falls back to a bare
    number (lenient) since projection periods are often written '5.0'."""
    m = _YEARS_RE.search(text)
    if m is not None:
        return ParsedValue(value=_to_float(m.group("num")), raw=m.group(0).strip())
    m = _BARE_NUMBER_RE.search(text)
    if m is not None:
        value = _to_float(m.group("pnum") or m.group("num"))
        if 0 < value <= 50:
            return ParsedValue(value=value, raw=m.group(0).strip(), clean=False, notes=["bare_number_as_years"])
    return None


def parse_number(text: str) -> ParsedValue | None:
    """First bare number; parenthesized = negative. For unitless schema
    fields (FX rates, betas, DSCR, DPI)."""
    m = _BARE_NUMBER_RE.search(text)
    if m is None:
        return None
    raw_num = m.group("pnum") or m.group("num")
    value = _to_float(raw_num)
    if m.group("pnum") is not None:
        value = -abs(value)
    return ParsedValue(value=value, raw=m.group(0).strip())


def parse_boolean(text: str) -> ParsedValue | None:
    """Y/N / Yes/No / True/False, token-exact on the trimmed text."""
    token = normalize_text(text)
    if token in _TRUE_WORDS:
        return ParsedValue(value=True, raw=text.strip())
    if token in _FALSE_WORDS:
        return ParsedValue(value=False, raw=text.strip())
    return None


def parse_date_text(text: str) -> tuple[date, str] | None:
    """First parseable date in `text` -> (date, matched_raw). Textual forms
    ('March 31, 2026', '31 March 2026') first, then everything
    indexer.periods.parse_date_folder accepts ('12-31-2025', 'Q1 2026',
    'Mar-26', 'FY2025'). Invalid calendar dates are skipped, never guessed."""
    m = _MDY_TEXT_RE.search(text)
    if m is not None:
        month = _month_number(m.group("month"))
        if month is not None:
            try:
                return date(int(m.group("year")), month, int(m.group("day"))), m.group(0)
            except ValueError:
                pass
    m = _DMY_TEXT_RE.search(text)
    if m is not None:
        month = _month_number(m.group("month"))
        if month is not None:
            try:
                return date(int(m.group("year")), month, int(m.group("day"))), m.group(0)
            except ValueError:
                pass
    for candidate_re in _DATE_CANDIDATE_RES:
        for candidate in candidate_re.finditer(text):
            parsed = parse_date_folder(candidate.group(0))
            if parsed is not None:
                return parsed, candidate.group(0)
    return None


def find_basis_tag(text: str) -> str | None:
    """First financial-basis tag in `text` ('LTM', 'NTM', 'FY+1', '2025E'),
    normalized to upper case without internal spaces."""
    m = _BASIS_RE.search(text)
    if m is None:
        return None
    return re.sub(r"\s+", "", m.group(1).upper())


# ---------------------------------------------------------------------------
# Unit normalization (raw preserved by the caller in FieldHit.raw_text)
# ---------------------------------------------------------------------------


def normalize_amount_to_millions(parsed: ParsedValue) -> tuple[float, bool]:
    """Normalize a parsed amount to millions. Returns (value, clean).

    With an explicit scale word the conversion is exact. Without one, the
    figure is assumed to already be in millions (memo tables are almost
    always '$M' — the schema header says so), EXCEPT clearly raw-unit
    magnitudes (>= 100,000) which are converted and marked lenient."""
    factor = _SCALE_FACTOR_TO_MILLIONS[parsed.scale]
    if factor is not None:
        return parsed.value * factor, parsed.clean
    value = float(parsed.value)
    if abs(value) >= 100_000:
        return value / 1_000_000.0, False
    return value, parsed.clean


# ---------------------------------------------------------------------------
# Label:value scanning over prose
# ---------------------------------------------------------------------------

# 'Label: value', 'Label - value', or 'Label    value' (>=2 spaces or dot
# leaders). The label part is matched fuzzily by the caller.
_LINE_SPLIT_RE = re.compile(r"^\s*(?P<label>[^:]{2,80}?)\s*[:–—-]\s+(?P<value>\S.*)$")
_COLUMNAR_SPLIT_RE = re.compile(r"^\s*(?P<label>\S.{1,78}?)(?:\s{2,}|[.·]{3,}\s*)(?P<value>\S.*)$")


@dataclass
class LabeledLine:
    """One prose line split into (label, value) by separator heuristics."""

    label: str
    value: str
    line: str
    line_index: int


def split_labeled_lines(text: str) -> list[LabeledLine]:
    """Split page text into label/value candidates, one per line. A line can
    contribute at most one candidate; colon/dash split wins over columnar."""
    out: list[LabeledLine] = []
    for idx, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        m = _LINE_SPLIT_RE.match(line) or _COLUMNAR_SPLIT_RE.match(line)
        if m is None:
            continue
        label = _FOOTNOTE_RE.sub("", m.group("label").strip())
        out.append(LabeledLine(label=label, value=m.group("value").strip(), line=line, line_index=idx))
    return out


# Financial-label qualifier tokens: two labels that differ on one of these
# mean DIFFERENT fields ('Primary' vs 'Tertiary Methodology', 'EV/LTM' vs
# 'EV/NTM EBITDA', 'Gross' vs 'Net IRR') — fuzzy similarity must never bridge
# them, however high the ratio.
LABEL_DISCRIMINATORS = frozenset({
    "primary", "secondary", "tertiary", "prior", "current", "ltm", "ntm", "ttm",
    "low", "mid", "high", "gross", "net", "entry", "exit", "local", "usd",
    "min", "max", "1", "2", "3", "100", "dcf", "fy", "cy", "qoq",
    "ev", "equity", "implied", "applied", "selected", "fx", "cap",
    "realized", "unrealized", "levered", "unlevered", "relevered",
})


def discriminator_conflict(norm_a: str, norm_b: str) -> bool:
    """True when the two normalized labels disagree on a qualifier token."""
    return bool((set(norm_a.split()) ^ set(norm_b.split())) & LABEL_DISCRIMINATORS)


def label_match_quality(label: str, wanted_labels: list[str], fuzzy_threshold: int = 85) -> float:
    """Best match quality of `label` against the accepted spellings:
    1.0 exact-normalized, else token_sort_ratio/100 when >= threshold AND no
    discriminator-token conflict, else 0. (token_SET_ratio is deliberately
    avoided: it scores any token subset 100, so 'Fund' would perfectly match
    a 'Fund Manager' line.)"""
    norm = normalize_text(label)
    if not norm:
        return 0.0
    best = 0.0
    for wanted in wanted_labels:
        wanted_norm = normalize_text(wanted)
        if norm == wanted_norm:
            return 1.0
        if discriminator_conflict(norm, wanted_norm):
            continue
        ratio = fuzz.token_sort_ratio(norm, wanted_norm)
        if ratio >= fuzzy_threshold:
            best = max(best, ratio / 100.0)
    return best


# ---------------------------------------------------------------------------
# Table-cell lookup
# ---------------------------------------------------------------------------


@dataclass
class TableCellHit:
    """One table cell located by fuzzy (row label, column header) lookup."""

    text: str
    row_index: int
    col_index: int
    row_label: str
    col_header: str
    quality: float  # min(row match, column match) in 0..1


def _header_row_index(table: TableData) -> int:
    """First row with at least two non-empty cells is treated as the header."""
    for idx, row in enumerate(table.rows):
        if sum(1 for cell in row if cell and str(cell).strip()) >= 2:
            return idx
    return 0


def find_table_cell(
    table: TableData,
    row_label: str | list[str],
    col_header: str | list[str],
    fuzzy_threshold: int = 85,
) -> TableCellHit | None:
    """Locate a cell by fuzzy row-label and column-header match.

    Row labels are matched against the first non-empty cell of each row;
    column headers against the header row. Returns the best (row, column)
    pair by min(row_quality, col_quality), or None when either side misses.
    """
    row_labels = [row_label] if isinstance(row_label, str) else list(row_label)
    col_headers = [col_header] if isinstance(col_header, str) else list(col_header)
    if not table.rows:
        return None

    header_idx = _header_row_index(table)
    header = table.rows[header_idx]

    best_col, best_col_q, best_col_text = -1, 0.0, ""
    for c_idx, cell in enumerate(header):
        if not cell or not str(cell).strip():
            continue
        quality = label_match_quality(str(cell), col_headers, fuzzy_threshold)
        if quality > best_col_q:
            best_col, best_col_q, best_col_text = c_idx, quality, str(cell)
    if best_col < 0:
        return None

    best: TableCellHit | None = None
    for r_idx, row in enumerate(table.rows):
        if r_idx == header_idx:
            continue
        label_cell = next((cell for cell in row if cell and str(cell).strip()), None)
        if label_cell is None:
            continue
        row_q = label_match_quality(str(label_cell), row_labels, fuzzy_threshold)
        if row_q <= 0.0:
            continue
        if best_col >= len(row) or row[best_col] is None or not str(row[best_col]).strip():
            continue
        quality = min(row_q, best_col_q)
        if best is None or quality > best.quality:
            best = TableCellHit(
                text=str(row[best_col]).strip(),
                row_index=r_idx,
                col_index=best_col,
                row_label=str(label_cell).strip(),
                col_header=best_col_text.strip(),
                quality=quality,
            )
    return best


def find_table_row(
    table: TableData, row_label: str | list[str], fuzzy_threshold: int = 85
) -> tuple[int, float] | None:
    """Index and quality of the best row whose first non-empty cell matches."""
    row_labels = [row_label] if isinstance(row_label, str) else list(row_label)
    best: tuple[int, float] | None = None
    for r_idx, row in enumerate(table.rows):
        label_cell = next((cell for cell in row if cell and str(cell).strip()), None)
        if label_cell is None:
            continue
        quality = label_match_quality(str(label_cell), row_labels, fuzzy_threshold)
        if quality > 0.0 and (best is None or quality > best[1]):
            best = (r_idx, quality)
    return best


def snippet(text: str, max_chars: int = 200) -> str:
    """Verbatim evidence snippet, single-spaced, hard-capped at max_chars."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= max_chars else flat[: max_chars - 1] + "…"
