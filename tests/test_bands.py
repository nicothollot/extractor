"""D4 unit tests: confidence components, conflict handling, vocab gating,
parse-failure flags, derived computation with cross-check, slot overflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pv_extractor.config import ExtractionConfig, ValidationConfig
from pv_extractor.extract.bands import ALL_EXTRACTORS
from pv_extractor.extract.bands.base import ExtractionContext, FieldSpec, SpecBandExtractor, spec
from pv_extractor.extract.bands.comps import TradingCompsExtractor
from pv_extractor.extract.confidence import hit_confidence
from pv_extractor.extract.derived import apply_derived
from pv_extractor.models import FieldHit, PageContent, SchemaField, TableData


@pytest.fixture(scope="module")
def schema_fields(project_root: Path) -> list[SchemaField]:
    doc = json.loads((project_root / "schema" / "master_schema.json").read_text(encoding="utf-8"))
    return [SchemaField.model_validate(field) for field in doc["fields"]]


def _fields(schema_fields, band: str) -> list[SchemaField]:
    return [field for field in schema_fields if field.band == band]


def _page(number: int, text: str = "", tables: list[TableData] | None = None, ocr: bool = False) -> PageContent:
    page = PageContent(page_number=number, text=text, tables=tables or [], text_char_count=len(text))
    if ocr:
        page.ocr_engine = "rapidocr"
        page.ocr_mean_confidence = 0.9
    return page


def _ctx() -> ExtractionContext:
    return ExtractionContext(cfg=ExtractionConfig())


# --------------------------------------------------------------------------
# confidence model
# --------------------------------------------------------------------------


def test_confidence_components_multiply() -> None:
    cfg = ExtractionConfig().confidence
    conf, components = hit_confidence(
        cfg, label_quality=1.0, parse_clean=True, page=_page(1), from_table=True, has_conflicts=False
    )
    assert conf == 1.0
    conf, components = hit_confidence(
        cfg, label_quality=1.0, parse_clean=True, page=_page(1), from_table=False, has_conflicts=False
    )
    assert conf == pytest.approx(0.85)  # prose factor
    conf, components = hit_confidence(
        cfg, label_quality=1.0, parse_clean=True, page=_page(1, ocr=True), from_table=True, has_conflicts=False
    )
    assert conf == pytest.approx(0.7 * 0.9)  # OCR page: 0.7 x mean word confidence
    assert components["page_class"] == pytest.approx(0.63)
    conf, _ = hit_confidence(
        cfg, label_quality=1.0, parse_clean=False, page=_page(1), from_table=True, has_conflicts=True
    )
    assert conf == pytest.approx(0.85 * 0.6)  # lenient parse x ambiguity


# --------------------------------------------------------------------------
# spec extraction behaviors
# --------------------------------------------------------------------------


def test_conflicting_candidates_kept_with_penalty(schema_fields) -> None:
    extractor = SpecBandExtractor(
        "HEADLINE FINANCIALS", [spec("Net Debt ($M)", "Net Debt")]
    )
    pages = [
        _page(1, "Net Debt: $120.0M"),
        _page(2, "Net Debt: $135.0M"),
    ]
    ctx = _ctx()
    hits = extractor.extract(pages, _fields(schema_fields, "HEADLINE FINANCIALS"), ctx)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.value == 120.0  # earlier page wins the tie
    assert hit.confidence == pytest.approx(0.85 * 0.6)
    assert len(hit.conflicts) == 1 and hit.conflicts[0].value == 135.0
    assert hit.confidence_components["ambiguity"] == 0.6


def test_vocab_below_threshold_left_empty_and_flagged(schema_fields) -> None:
    extractor = SpecBandExtractor("METHODOLOGY ROUTING", [spec("Primary Methodology")])
    pages = [_page(1, "Primary Methodology: proprietary scorecard approach")]
    ctx = _ctx()
    hits = extractor.extract(pages, _fields(schema_fields, "METHODOLOGY ROUTING"), ctx)
    assert hits == []
    assert any(flag.category == "vocab" and "left empty" in flag.description for flag in ctx.flags)


def test_numeric_parse_failure_is_flagged_never_silent(schema_fields) -> None:
    extractor = SpecBandExtractor("HEADLINE FINANCIALS", [spec("Net Debt ($M)", "Net Debt")])
    pages = [_page(1, "Net Debt: not disclosed")]
    ctx = _ctx()
    hits = extractor.extract(pages, _fields(schema_fields, "HEADLINE FINANCIALS"), ctx)
    assert hits == []
    assert any(flag.category == "parse" and "Net Debt" in flag.description for flag in ctx.flags)


def test_local_currency_field_keeps_currency(schema_fields) -> None:
    extractor = SpecBandExtractor(
        "METHODOLOGY: YIELD / CREDIT", [spec("Yield Par Value ($M, local)", "Par Value")]
    )
    pages = [_page(1, "Par Value: £75.0mm")]
    hits = extractor.extract(pages, _fields(schema_fields, "METHODOLOGY: YIELD / CREDIT"), _ctx())
    assert hits[0].value == 75.0
    assert hits[0].unit == "GBP_millions"
    assert hits[0].raw_text == "£75.0mm"


def test_table_beats_prose_on_confidence(schema_fields) -> None:
    table = TableData(
        page_number=2,
        rows=[["Metric", "Value"], ["Net Debt", "$118.0M"]],
        source="pymupdf",
    )
    extractor = SpecBandExtractor("HEADLINE FINANCIALS", [spec("Net Debt ($M)", "Net Debt")])
    pages = [_page(1, "Net Debt: $118.0M"), _page(2, tables=[table])]
    hits = extractor.extract(pages, _fields(schema_fields, "HEADLINE FINANCIALS"), _ctx())
    hit = hits[0]
    assert hit.value == 118.0
    assert hit.page == 2 and hit.confidence == 1.0  # same value, table wins, no ambiguity
    assert hit.conflicts == []


# --------------------------------------------------------------------------
# derived fields
# --------------------------------------------------------------------------


def _hit(schema_fields, header: str, value, confidence: float = 0.9, method: str = "deterministic") -> FieldHit:
    field = next(f for f in schema_fields if f.header == header)
    return FieldHit(
        field=header, col_index=field.col_index, band=field.band,
        raw_text=str(value), value=value, method=method, confidence=confidence,
    )


def test_derived_computed_from_inputs_never_extracted(schema_fields) -> None:
    by_header = {f.header: f for f in schema_fields}
    ctx = _ctx()
    hits = [
        _hit(schema_fields, "EBITDA ($M)", 64.1, 0.9),
        _hit(schema_fields, "Revenue ($M)", 410.0, 0.8),
        _hit(schema_fields, "EBITDA Margin %", 99.0, 0.95),  # extracted, wrong
    ]
    out = apply_derived(hits, by_header, ValidationConfig(), ctx)
    margin = next(h for h in out if h.field == "EBITDA Margin %")
    assert margin.method == "computed"
    assert margin.value == pytest.approx(15.6341, abs=1e-3)
    assert margin.confidence == 0.8  # min of input confidences
    assert margin.conflicts and margin.conflicts[0].value == 99.0
    assert any(f.category == "computed_crosscheck" for f in ctx.flags)


def test_derived_extracted_stands_when_inputs_missing(schema_fields) -> None:
    by_header = {f.header: f for f in schema_fields}
    ctx = _ctx()
    hits = [_hit(schema_fields, "EBITDA Margin %", 15.6, 0.9)]
    out = apply_derived(hits, by_header, ValidationConfig(), ctx)
    margin = next(h for h in out if h.field == "EBITDA Margin %")
    assert margin.method == "deterministic" and margin.value == 15.6
    assert ctx.flags == []


def test_bridge_reconciles_computed(schema_fields) -> None:
    by_header = {f.header: f for f in schema_fields}
    hits = [
        _hit(schema_fields, "NAV Change Abs ($M)", 10.0),
        _hit(schema_fields, "Δ Operating Performance ($M)", 6.0),
        _hit(schema_fields, "Δ Multiple / Exit Assumption ($M)", 4.2),
    ]
    out = apply_derived(hits, by_header, ValidationConfig(), _ctx())
    reconciles = next(h for h in out if h.field == "Bridge Reconciles Y/N")
    assert reconciles.value is True and reconciles.method == "computed"

    hits[2] = _hit(schema_fields, "Δ Multiple / Exit Assumption ($M)", -8.0)
    out = apply_derived(hits, by_header, ValidationConfig(), _ctx())
    reconciles = next(h for h in out if h.field == "Bridge Reconciles Y/N")
    assert reconciles.value is False


# --------------------------------------------------------------------------
# positional slots
# --------------------------------------------------------------------------


def test_slot_overflow_flagged(schema_fields) -> None:
    rows = [["Company", "EV/LTM EBITDA"]] + [[f"Comp {chr(65 + i)}", f"{8 + i * 0.1:.1f}x"] for i in range(18)]
    table = TableData(page_number=1, rows=rows, source="pymupdf")
    ctx = _ctx()
    hits = TradingCompsExtractor().extract(
        [_page(1, tables=[table])], _fields(schema_fields, "TRADING COMPS (POSITIONAL SLOTS)"), ctx
    )
    names = [h for h in hits if h.field.endswith("Name")]
    assert len(names) == 15  # 15 slots filled
    assert names[0].value == "Comp A"  # sorted by name
    overflow = [f for f in ctx.flags if f.category == "slots"]
    assert len(overflow) == 1 and "18 rows found, 15 slots" in overflow[0].description


def test_every_extractor_band_exists_in_schema(schema_fields) -> None:
    bands = {f.band for f in schema_fields}
    for extractor in ALL_EXTRACTORS:
        assert extractor.band in bands, extractor.band


def test_every_spec_header_exists_in_schema(schema_fields) -> None:
    headers = {f.header for f in schema_fields}
    for extractor in ALL_EXTRACTORS:
        for field_spec in getattr(extractor, "specs", []):
            assert isinstance(field_spec, FieldSpec)
            assert field_spec.header in headers, field_spec.header
