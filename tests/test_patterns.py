"""Unit tests for the shared pattern toolkit (D4 foundation)."""

from __future__ import annotations

from datetime import date

from pv_extractor.extract.patterns import (
    find_basis_tag,
    find_table_cell,
    label_match_quality,
    normalize_amount_to_millions,
    parse_amount,
    parse_boolean,
    parse_bps,
    parse_date_text,
    parse_multiple,
    parse_number,
    parse_percent,
    parse_years,
    snippet,
    split_labeled_lines,
)
from pv_extractor.models import TableData

# --------------------------------------------------------------------------
# Amounts
# --------------------------------------------------------------------------


def test_amount_currency_and_thousands() -> None:
    p = parse_amount("Enterprise Value: $1,234.5")
    assert p is not None and p.value == 1234.5 and p.currency == "USD" and p.scale is None


def test_amount_parenthesized_negative() -> None:
    p = parse_amount("FX Impact (12.3)")
    assert p is not None and p.value == -12.3


def test_amount_scale_words() -> None:
    assert parse_amount("£75mm").scale == "millions"
    assert parse_amount("£75mm").currency == "GBP"
    assert parse_amount("€1.2bn").scale == "billions"
    assert parse_amount("12.5 million").scale == "millions"
    assert parse_amount("$850k").scale == "thousands"


def test_amount_normalization_to_millions() -> None:
    value, clean = normalize_amount_to_millions(parse_amount("€1.2bn"))
    assert value == 1200.0 and clean
    value, clean = normalize_amount_to_millions(parse_amount("$425.0"))
    assert value == 425.0 and clean
    value, clean = normalize_amount_to_millions(parse_amount("$850,000,000"))
    assert value == 850.0 and not clean  # raw units repaired -> lenient


def test_amount_does_not_eat_percent() -> None:
    # '%' values must not be consumed as bare amounts (guarded lookahead).
    assert parse_amount("8.5%") is None


# --------------------------------------------------------------------------
# Percent / bps / multiple / years / number / boolean
# --------------------------------------------------------------------------


def test_percent_and_bps_directions() -> None:
    assert parse_percent("WACC of 8.5%").value == 8.5
    assert parse_percent("(2.5)%").value == -2.5
    assert parse_percent("up 50bps").value == 0.5  # bps -> percent
    assert parse_bps("up 50bps").value == 50.0
    assert parse_bps("+0.5%").value == 50.0  # percent -> bps


def test_multiple() -> None:
    assert parse_multiple("8.3x EV/LTM EBITDA").value == 8.3
    assert parse_multiple("no multiple here") is None


def test_years() -> None:
    assert parse_years("5 years").value == 5.0
    assert parse_years("7-yr drag").value == 7.0
    lenient = parse_years("Projection Period: 5.0")
    assert lenient.value == 5.0 and not lenient.clean


def test_number_and_boolean() -> None:
    assert parse_number("1.3245").value == 1.3245
    assert parse_number("(0.6)").value == -0.6
    assert parse_boolean(" Yes ").value is True
    assert parse_boolean("N").value is False
    assert parse_boolean("maybe") is None


# --------------------------------------------------------------------------
# Dates and basis tags
# --------------------------------------------------------------------------


def test_dates_textual_and_numeric() -> None:
    assert parse_date_text("as of March 31, 2026")[0] == date(2026, 3, 31)
    assert parse_date_text("dated 31 March 2026")[0] == date(2026, 3, 31)
    assert parse_date_text("valuation date 12-31-2025")[0] == date(2025, 12, 31)
    assert parse_date_text("for Q1 2026")[0] == date(2026, 3, 31)
    assert parse_date_text("February 30, 2026 is invalid") is None


def test_basis_tags() -> None:
    assert find_basis_tag("EV/LTM EBITDA") == "LTM"
    assert find_basis_tag("FY+1 estimate") == "FY+1"
    assert find_basis_tag("2025E EBITDA") == "2025E"
    assert find_basis_tag("nothing") is None


# --------------------------------------------------------------------------
# Label:value lines
# --------------------------------------------------------------------------


def test_split_labeled_lines_colon_and_columnar() -> None:
    text = "Net Debt: $120.0M\nImplied EV          $545.0M\nKey Risks - customer churn\nplain sentence here\n"
    lines = split_labeled_lines(text)
    pairs = {ln.label: ln.value for ln in lines}
    assert pairs["Net Debt"] == "$120.0M"
    assert pairs["Implied EV"] == "$545.0M"
    assert pairs["Key Risks"] == "customer churn"
    assert "plain sentence here" not in pairs


def test_label_match_quality() -> None:
    assert label_match_quality("Net Debt", ["Net Debt"]) == 1.0
    assert label_match_quality("Net Debt ($M)", ["Net Debt"]) >= 0.85
    assert label_match_quality("Gross Margin", ["Net Debt"]) == 0.0


# --------------------------------------------------------------------------
# Table-cell lookup
# --------------------------------------------------------------------------


def _table() -> TableData:
    return TableData(
        page_number=4,
        rows=[
            ["Metric", "Low", "Mid", "High"],
            ["Discount Rate", "8.0%", "8.5%", "9.0%"],
            ["Terminal Growth", "1.5%", "2.0%", "2.5%"],
            [None, None, None, None],
        ],
        source="pymupdf",
    )


def test_find_table_cell_exact_and_fuzzy() -> None:
    hit = find_table_cell(_table(), "Discount Rate", "Mid")
    assert hit is not None and hit.text == "8.5%" and hit.quality == 1.0
    fuzzy = find_table_cell(_table(), "Terminal Growth Rate %", ["Mid", "Selected"])
    assert fuzzy is not None and fuzzy.text == "2.0%" and 0.0 < fuzzy.quality <= 1.0


def test_find_table_cell_misses_cleanly() -> None:
    assert find_table_cell(_table(), "WACC Build", "Mid") is None
    assert find_table_cell(_table(), "Discount Rate", "Selected") is None


def test_snippet_caps_length() -> None:
    s = snippet("word " * 100, max_chars=50)
    assert len(s) <= 50 and s.endswith("…")
