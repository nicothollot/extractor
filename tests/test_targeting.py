"""D2 targeting tests: lexicon assembly and top-K page selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pv_extractor.config import ExtractionConfig
from pv_extractor.extract.targeting import (
    build_band_lexicons,
    build_page_band_map,
    score_pages_per_band,
)
from pv_extractor.models import PageContent, SchemaField


@pytest.fixture(scope="module")
def schema_fields(project_root: Path) -> list[SchemaField]:
    doc = json.loads((project_root / "schema" / "master_schema.json").read_text(encoding="utf-8"))
    return [SchemaField.model_validate(field) for field in doc["fields"]]


def _page(number: int, text: str) -> PageContent:
    return PageContent(page_number=number, text=text, text_char_count=len(text))


def test_lexicons_seeded_and_mined(schema_fields: list[SchemaField]) -> None:
    lex = build_band_lexicons(schema_fields, ExtractionConfig())
    assert "enterprise value" in lex["HEADLINE FINANCIALS"]  # curated seed
    assert "wacc" in lex["METHODOLOGY: DCF"]
    assert "net debt" in lex["HEADLINE FINANCIALS"]  # mined from 'Net Debt ($M)'
    assert "dcf terminal growth rate" in lex["METHODOLOGY: DCF"]  # mined header
    # slot headers (TC01 Name...) must not pollute lexicons with generics
    assert "name" not in lex["TRADING COMPS (POSITIONAL SLOTS)"]


def test_lexicon_overrides_merge(schema_fields: list[SchemaField]) -> None:
    cfg = ExtractionConfig(band_anchor_overrides={"METHODOLOGY: DCF": ["Regulated Asset Base"]})
    lex = build_band_lexicons(schema_fields, cfg)
    assert "regulated asset base" in lex["METHODOLOGY: DCF"]


def test_page_scoring_and_top_k(schema_fields: list[SchemaField]) -> None:
    cfg = ExtractionConfig(top_k_pages_per_band=2, summary_pages=3)
    pages = [
        _page(1, "Valuation Memorandum cover page"),
        _page(2, "Executive summary: enterprise value of $545M and net debt of $120M"),
        _page(3, "Business update"),
        _page(4, "Discount rate selected via WACC build-up; terminal value uses Gordon Growth"),
        _page(5, "WACC sensitivity: discount rate range 8.0% - 9.0%; terminal growth 2.0%"),
        _page(6, "Comparable companies: trading comps EV/EBITDA peer set"),
        _page(7, "Appendix"),
    ]
    band_map = build_page_band_map(pages, schema_fields, cfg)

    dcf_pages = band_map["METHODOLOGY: DCF"]
    assert set(dcf_pages) >= {4, 5}  # top-K hits
    assert {1, 2, 3} <= set(dcf_pages)  # summary pages always included
    assert 6 not in dcf_pages and 7 not in dcf_pages

    comps_pages = band_map["TRADING COMPS (POSITIONAL SLOTS)"]
    assert 6 in comps_pages and 4 not in comps_pages

    scores = score_pages_per_band(pages, build_band_lexicons(schema_fields, cfg))
    assert scores["METHODOLOGY: DCF"][5] > scores["METHODOLOGY: DCF"][4] or (
        scores["METHODOLOGY: DCF"][4] > 0 and scores["METHODOLOGY: DCF"][5] > 0
    )


def test_short_document_summary_pages_clamped(schema_fields: list[SchemaField]) -> None:
    band_map = build_page_band_map(
        [_page(1, "enterprise value $5M")], schema_fields, ExtractionConfig()
    )
    assert band_map["HEADLINE FINANCIALS"] == [1]
    assert band_map["METHODOLOGY: DCF"] == [1]  # summary page only; no anchor hits
