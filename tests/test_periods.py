"""D4 period resolver tests: every observed date-folder format, garbage
inputs that must return None, per-style target resolution and labels."""

from __future__ import annotations

from datetime import date

import pytest

from pv_extractor.config import parse_period_style
from pv_extractor.indexer.periods import (
    filename_contains_period,
    parse_date_folder,
    period_label,
    pivot_year,
    resolve_target_period,
)

QUARTERLY = parse_period_style("quarterly_calendar")
MONTHLY = parse_period_style("monthly")
FISCAL_JUNE = parse_period_style("fiscal(6)")

PARSE_CASES: list[tuple[str, date | None]] = [
    # plain m.d.yyyy / m.d.yy
    ("1.31.2025", date(2025, 1, 31)),
    ("11.30.2024", date(2024, 11, 30)),
    ("12.31.2024", date(2024, 12, 31)),
    ("2.28.2025", date(2025, 2, 28)),
    ("9.30.24", date(2024, 9, 30)),
    ("12-31-2025", date(2025, 12, 31)),
    ("3/31/2026", date(2026, 3, 31)),
    # sequence prefixes "(N) " and "N. "
    ("(1) 9.30.24", date(2024, 9, 30)),
    ("(10) 11.30.25", date(2025, 11, 30)),
    ("(2) 3.31.26", date(2026, 3, 31)),
    ("(19) 3.31.26", date(2026, 3, 31)),
    ("(18) 2.28.26", date(2026, 2, 28)),
    ("(1) 8.31.2024", date(2024, 8, 31)),
    ("(1) 12.31.2024", date(2024, 12, 31)),
    ("1. 11.30.25", date(2025, 11, 30)),
    # decorated archive folders
    ("+Prior (8.31.24) Reports", date(2024, 8, 31)),
    ("+Prior (12.31.23) Reports", date(2023, 12, 31)),
    # quarter labels
    ("Q1 2026", date(2026, 3, 31)),
    ("2026 Q1", date(2026, 3, 31)),
    ("Q4 2025", date(2025, 12, 31)),
    ("q2 2024", date(2024, 6, 30)),
    ("Q3-2025", date(2025, 9, 30)),
    # month-year numeric and month-name
    ("03.2026", date(2026, 3, 31)),
    ("11.2025", date(2025, 11, 30)),
    ("Mar-26", date(2026, 3, 31)),
    ("Mar 26", date(2026, 3, 31)),
    ("Mar-2026", date(2026, 3, 31)),
    ("March 2026", date(2026, 3, 31)),
    ("feb-25", date(2025, 2, 28)),
    # fiscal-year folders (calendar default)
    ("FY2025", date(2025, 12, 31)),
    ("FY 2025", date(2025, 12, 31)),
    ("FY25", date(2025, 12, 31)),
    # two-digit pivot and leap years
    ("Mar-99", date(1999, 3, 31)),
    ("12.31.70", date(1970, 12, 31)),
    ("12.31.69", date(2069, 12, 31)),
    ("2.29.24", date(2024, 2, 29)),
    # year-first numeric
    ("2025.12.31", date(2025, 12, 31)),
    # garbage -> None
    ("Analysis", None),
    ("Client", None),
    ("Report", None),
    ("Archive", None),
    ("Global Admin", None),
    ("Andover Storage (HSRE 11)", None),
    ("Andover Storage (MHC 3)", None),
    ("", None),
    ("   ", None),
    ("v2", None),
    ("(1)", None),
    ("+", None),
    ("random text", None),
    ("Q5 2026", None),
    ("13.31.2024", None),
    ("2.30.2025", None),
    ("2.29.25", None),
    ("0.0.00", None),
    ("Deal 2", None),
    ("Phase 24", None),
]


@pytest.mark.parametrize("name,expected", PARSE_CASES, ids=[repr(c[0]) for c in PARSE_CASES])
def test_parse_date_folder(name: str, expected: date | None) -> None:
    assert parse_date_folder(name) == expected


@pytest.mark.parametrize(
    "yy,expected",
    [(0, 2000), (24, 2024), (26, 2026), (69, 2069), (70, 1970), (99, 1999)],
)
def test_pivot_year(yy: int, expected: int) -> None:
    assert pivot_year(yy) == expected


RESOLVE_CASES: list[tuple[str, object, date | None]] = [
    ("2025-01-31", QUARTERLY, date(2025, 1, 31)),  # ISO always wins
    ("2025-01-31", MONTHLY, date(2025, 1, 31)),
    ("1.31.2025", MONTHLY, date(2025, 1, 31)),
    ("Q1 2026", QUARTERLY, date(2026, 3, 31)),
    ("2026 Q1", QUARTERLY, date(2026, 3, 31)),
    ("Q1 2026", MONTHLY, date(2026, 3, 31)),  # monthly treats quarters as calendar
    ("FY2025", QUARTERLY, date(2025, 12, 31)),
    ("FY2025", MONTHLY, date(2025, 12, 31)),
    # fiscal(6): quarter N of the FY ending June of the stated year
    ("Q1 2026", FISCAL_JUNE, date(2025, 9, 30)),
    ("Q2 2026", FISCAL_JUNE, date(2025, 12, 31)),
    ("Q3 2026", FISCAL_JUNE, date(2026, 3, 31)),
    ("Q4 2026", FISCAL_JUNE, date(2026, 6, 30)),
    ("2026 Q4", FISCAL_JUNE, date(2026, 6, 30)),
    ("FY2025", FISCAL_JUNE, date(2025, 6, 30)),
    ("FY26", FISCAL_JUNE, date(2026, 6, 30)),
    ("Mar-26", QUARTERLY, date(2026, 3, 31)),
    ("03.2026", QUARTERLY, date(2026, 3, 31)),
    ("(1) 9.30.24", QUARTERLY, date(2024, 9, 30)),
    ("garbage", QUARTERLY, None),
    ("", MONTHLY, None),
    ("Q7 2026", FISCAL_JUNE, None),
]


@pytest.mark.parametrize("period,style,expected", RESOLVE_CASES, ids=[f"{c[0]!r}/{i}" for i, c in enumerate(RESOLVE_CASES)])
def test_resolve_target_period(period: str, style, expected: date | None) -> None:
    assert resolve_target_period(period, style) == expected


LABEL_CASES = [
    (date(2026, 3, 31), QUARTERLY, "Q1 2026"),
    (date(2025, 12, 31), QUARTERLY, "Q4 2025"),
    (date(2025, 1, 31), QUARTERLY, "Q1 2025"),
    (date(2025, 1, 31), MONTHLY, "Jan 2025"),
    (date(2024, 11, 30), MONTHLY, "Nov 2024"),
    (date(2025, 9, 30), FISCAL_JUNE, "FY2026 Q1"),
    (date(2025, 12, 31), FISCAL_JUNE, "FY2026 Q2"),
    (date(2026, 3, 31), FISCAL_JUNE, "FY2026 Q3"),
    (date(2026, 6, 30), FISCAL_JUNE, "FY2026 Q4"),
]


@pytest.mark.parametrize("as_of,style,expected", LABEL_CASES, ids=[c[2] for c in LABEL_CASES])
def test_period_label(as_of: date, style, expected: str) -> None:
    assert period_label(as_of, style) == expected


FILENAME_CASES = [
    # (normalized text, target, expected)
    ("03 31 2026 sre valuation memo vf pdf", date(2026, 3, 31), True),
    ("3 31 26 sre valuation memo pdf", date(2026, 3, 31), True),
    ("accell 11 30 2024 report v1 pdf", date(2024, 11, 30), True),
    ("accell valuation memo q1 2026 pdf", date(2026, 3, 31), True),
    ("tdw valuation memo 4q25 pdf", date(2025, 12, 31), True),
    ("hyperoptic march 2026 update pdf", date(2026, 3, 31), True),
    ("accell 11 30 2024 report v1 pdf", date(2025, 1, 31), False),
    ("plain valuation memo pdf", date(2026, 3, 31), False),
    # token boundaries: '103 31 2026' must not match 3/31/2026
    ("doc 103 31 2026 pdf", date(2026, 3, 31), False),
    ("q1 2026 inside folder name", date(2026, 3, 31), True),
]


@pytest.mark.parametrize("text,target,expected", FILENAME_CASES, ids=[f"{c[0][:25]}/{c[2]}" for c in FILENAME_CASES])
def test_filename_contains_period(text: str, target: date, expected: bool) -> None:
    assert filename_contains_period(text, target) == expected
