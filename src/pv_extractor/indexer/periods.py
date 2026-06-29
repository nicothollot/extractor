"""Period resolver: date-folder parsing, target-period resolution, labels.

Date folders on the share come in many shapes — ``1.31.2025``, ``9.30.24``,
``(16) 12.31.25``, ``1. 11.30.25``, ``+Prior (8.31.24) Reports``,
``12-31-2025``, ``Q1 2026``, ``2026 Q1``, ``03.2026``, ``Mar-26``,
``FY2025`` — all parsed to an as-of date. Two-digit years pivot at 70
(>=70 -> 19xx, else 20xx). Numeric forms are US month-first.

Reporting cadence is per-client (``PeriodStyle``): clients are NOT all
calendar-quarterly (Angelo Gordon folders show 11.30 and 1.31 month-ends).
``resolve_target_period`` maps user input to an as-of date under a style;
``period_label`` maps an as-of date back to a reporting-period label.

``filename_contains_period`` reproduces the export's period-signal
convention exactly (verified row-for-row against
reference/strip_from_index.xlsx): numeric month-day-year tokens, month-name
plus four-digit year, and quarter labels (``q4 2025`` / ``4q25`` ...) for
calendar quarter ends, all matched on token boundaries in normalized text.
Do not extend its label set without re-verifying against the export.
"""

from __future__ import annotations

import calendar
import functools
import re
from datetime import date

from pv_extractor.models import PeriodStyle, PeriodStyleKind

_TWO_DIGIT_YEAR_PIVOT = 70  # >=70 -> 19xx, else 20xx (CLAUDE.md convention)

_SEP = r"[.\-_/ ]"
_MDY_RE = re.compile(rf"(?<!\d)(\d{{1,2}}){_SEP}(\d{{1,2}}){_SEP}(\d{{2,4}})(?!\d)")
_YMD_RE = re.compile(rf"(?<!\d)(\d{{4}}){_SEP}(\d{{1,2}}){_SEP}(\d{{1,2}})(?!\d)")

# Anchored forms applied to the decoration-stripped folder name only.
_SEQ_PREFIX_RE = re.compile(r"^(?:\(\d+\)\s*|\d+\.\s+)+")  # "(16) ", "1. " (dot needs whitespace)
_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})[.\-/](\d{4})$")
_Q_YEAR_RE = re.compile(r"^q([1-4])[ \-.]?(\d{2}|\d{4})$", re.IGNORECASE)
_YEAR_Q_RE = re.compile(r"^(\d{4})[ \-.]?q([1-4])$", re.IGNORECASE)
_FY_RE = re.compile(r"^fy[ \-.]?(\d{2}|\d{4})$", re.IGNORECASE)
_MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTH_NAMES.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTH_NAME_RE = re.compile(r"^([a-z]{3,9})[ \-.]?'?(\d{2}|\d{4})$", re.IGNORECASE)

_QUARTER_END_MONTH_DAY = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
_CAL_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def pivot_year(yy: int) -> int:
    """Two-digit year pivot at 70: 24 -> 2024, 99 -> 1999, 70 -> 1970."""
    return yy + (1900 if yy >= _TWO_DIGIT_YEAR_PIVOT else 2000)


def _expand_year(year: int) -> int:
    return year if year >= 100 else pivot_year(year)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _strip_decorations(folder_name: str) -> str:
    """Remove '(N) ' / 'N. ' sequence prefixes, a leading '+', and
    Prior/Report(s) decoration words: '+Prior (8.31.24) Reports' -> '(8.31.24)'."""
    name = folder_name.strip().lstrip("+").strip()
    name = _SEQ_PREFIX_RE.sub("", name)
    name = re.sub(r"\b(?:prior|reports?)\b", " ", name, flags=re.IGNORECASE)
    return name.strip(" ()").strip()


def parse_date_folder(folder_name: str) -> date | None:
    """Parse a date-folder name to its as-of date, or None for garbage.

    Embedded US month-first numeric forms are tried first ('1.31.2025',
    '(16) 12.31.25', '+Prior (8.31.24) Reports'), then year-first numeric
    forms, then anchored month-year ('03.2026' -> month end), quarter
    ('Q1 2026', '2026 Q1' -> calendar quarter end), month-name ('Mar-26',
    'March 2026' -> month end) and fiscal-year ('FY2025' -> Dec 31, the
    calendar default; per-client fiscal cadence applies only at
    resolve_target_period / period_label level) forms. Invalid calendar
    dates ('2.30.2025', '13.31.2024') are skipped, never guessed.
    """
    if not folder_name or not any(ch.isdigit() for ch in folder_name):
        return None
    for m in _MDY_RE.finditer(folder_name):
        month, day = int(m.group(1)), int(m.group(2))
        year = _expand_year(int(m.group(3)))
        try:
            return date(year, month, day)
        except ValueError:
            continue
    for m in _YMD_RE.finditer(folder_name):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            continue
    stripped = _strip_decorations(folder_name)
    m = _MONTH_YEAR_RE.match(stripped)
    if m and 1 <= int(m.group(1)) <= 12:
        return _month_end(int(m.group(2)), int(m.group(1)))
    m = _Q_YEAR_RE.match(stripped) or _YEAR_Q_RE.match(stripped)
    if m:
        first, second = m.group(1), m.group(2)
        quarter, year = (int(first), int(second)) if len(second) >= len(first) else (int(second), int(first))
        if not 1 <= quarter <= 4:  # groups may swap between the two regexes
            quarter, year = year, quarter
        month, day = _CAL_QUARTER_END[quarter]
        return date(_expand_year(year), month, day)
    m = _FY_RE.match(stripped)
    if m:
        return date(_expand_year(int(m.group(1))), 12, 31)
    m = _MONTH_NAME_RE.match(stripped)
    if m and m.group(1).lower() in _MONTH_NAMES:
        return _month_end(_expand_year(int(m.group(2))), _MONTH_NAMES[m.group(1).lower()])
    return None


def _fiscal_quarter_end(quarter: int, fy: int, fy_end_month: int) -> date:
    """End date of fiscal quarter N for the fiscal year ending (fy, fy_end_month)."""
    month = (fy_end_month - 3 * (4 - quarter) - 1) % 12 + 1
    year = fy if month <= fy_end_month else fy - 1
    return _month_end(year, month)


def resolve_target_period(period: str, style: PeriodStyle) -> date | None:
    """User-supplied target period -> as-of date under the client's cadence.

    Accepts ISO ('2025-01-31'), everything parse_date_folder accepts, and
    quarter/FY labels interpreted per style: quarterly_calendar/monthly ->
    calendar quarter ends; fiscal(M) -> quarter N of the fiscal year ENDING
    in month M of the stated year (fiscal(6): 'Q1 2026' -> 2025-09-30,
    'FY2025' -> 2025-06-30). Returns None when unparseable.
    """
    text = period.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if style.kind == PeriodStyleKind.fiscal and style.fiscal_year_end_month:
        fy_end = style.fiscal_year_end_month
        m = _Q_YEAR_RE.match(text) or _YEAR_Q_RE.match(text)
        if m:
            first, second = m.group(1), m.group(2)
            quarter, year = (int(first), int(second)) if len(second) >= len(first) else (int(second), int(first))
            if not 1 <= quarter <= 4:
                quarter, year = year, quarter
            return _fiscal_quarter_end(quarter, _expand_year(year), fy_end)
        m = _FY_RE.match(text)
        if m:
            return _month_end(_expand_year(int(m.group(1))), fy_end)
    return parse_date_folder(text)


def period_label(as_of: date, style: PeriodStyle) -> str:
    """As-of date -> reporting-period label under the client's cadence:
    quarterly_calendar -> 'Q1 2026'; monthly -> 'Jan 2025'; fiscal(6) ->
    'FY2026 Q1' (fiscal year named for the calendar year it ends in)."""
    if style.kind == PeriodStyleKind.monthly:
        return f"{calendar.month_abbr[as_of.month]} {as_of.year}"
    if style.kind == PeriodStyleKind.fiscal and style.fiscal_year_end_month:
        fy_end = style.fiscal_year_end_month
        months_after_fy_start = (as_of.month - fy_end - 1) % 12
        quarter = months_after_fy_start // 3 + 1
        fy = as_of.year + (1 if as_of.month > fy_end else 0)
        return f"FY{fy} Q{quarter}"
    return f"Q{(as_of.month - 1) // 3 + 1} {as_of.year}"


def _advance_month_end(d: date, months: int) -> date:
    """The month-end `months` months after the month of `d`."""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    return _month_end(year, month0 + 1)


def expand_period_range(
    start: str, end: str, style: PeriodStyle, *, max_periods: int = 60
) -> list[str]:
    """Expand an inclusive period range into the ordered list of period labels
    between `start` and `end` under the client's cadence (e.g. 'Q1 2024' ..
    'Q4 2025' -> ['Q1 2024', .., 'Q4 2025']; monthly clients step by month).

    Both ends are parsed with resolve_target_period; a bad end raises
    ValueError (never silent). Endpoints out of order are swapped. Capped at
    max_periods so a typo can't fan out forever."""
    s = resolve_target_period(start, style)
    if s is None:
        raise ValueError(f"could not parse start period {start!r}")
    e = resolve_target_period(end, style)
    if e is None:
        raise ValueError(f"could not parse end period {end!r}")
    if s > e:
        s, e = e, s
    step = 1 if style.kind == PeriodStyleKind.monthly else 3
    out: list[str] = []
    seen: set[str] = set()
    cur = s
    while cur <= e and len(out) < max_periods:
        label = period_label(cur, style)
        if label not in seen:
            seen.add(label)
            out.append(label)
        cur = _advance_month_end(cur, step)
    return out


@functools.cache
def _period_labels(target: date) -> tuple[str, ...]:
    """Normalized-text labels signalling `target`, pre-padded with spaces
    for token-boundary substring matching. Matches the export's convention;
    do not extend without re-verifying against strip_from_index.xlsx."""
    yyyy = str(target.year)
    yy = f"{target.year % 100:02d}"
    labels: set[str] = set()
    for month in {str(target.month), f"{target.month:02d}"}:
        for day in {str(target.day), f"{target.day:02d}"}:
            for year in (yyyy, yy):
                labels.add(f"{month} {day} {year}")
    labels.add(f"{calendar.month_name[target.month].lower()} {yyyy}")
    labels.add(f"{calendar.month_abbr[target.month].lower()} {yyyy}")
    quarter = _QUARTER_END_MONTH_DAY.get((target.month, target.day))
    if quarter is not None:
        for year in (yyyy, yy):
            labels.update(
                {f"q{quarter} {year}", f"{quarter}q {year}", f"{quarter}q{year}", f"q{quarter}{year}"}
            )
    return tuple(f" {label} " for label in sorted(labels))


def filename_contains_period(normalized_text: str, target: date) -> bool:
    """True when normalized text (export convention: lowercase, spaces only)
    contains a token-bounded label for the target period."""
    padded = f" {normalized_text} "
    return any(label in padded for label in _period_labels(target))
