"""D1 reader-layer tests: per-type reading, page classification, and every
hard case the spec names (encrypted, corrupt/zero-byte, rotated, >200 pages,
legacy .doc, scanned, image-table)."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from fixtures.docgen import (
    add_image_table_page,
    make_docx,
    make_encrypted_pdf,
    make_pptx,
    make_scanned_pdf,
    make_text_pdf,
    make_xlsx,
)
from pv_extractor.extract.readers import OcrReader, reader_for_extension
from pv_extractor.extract.readers.pdf import PdfReader
from pv_extractor.models import DocFlag, PageClass

PARA = "This valuation memorandum presents the fair value of the investment as of March 31, 2026."


# --------------------------------------------------------------------------
# PdfReader basics
# --------------------------------------------------------------------------


def test_pdf_text_pages_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "memo.pdf"
    table = [
        ["Metric", "Low", "Mid", "High"],
        ["Discount Rate", "8.0%", "8.5%", "9.0%"],
        ["Terminal Growth", "1.5%", "2.0%", "2.5%"],
    ]
    make_text_pdf(path, [[PARA] * 6, ["DCF Assumptions"]], tables_by_page={2: [table]})

    reader = PdfReader()
    content = reader.summarize(path)
    assert content.flags == [] and content.page_count == 2
    assert content.pages[0].page_class is PageClass.TEXT
    assert content.pages[0].has_text_layer
    assert "valuation memorandum" in content.pages[0].text

    tables_by_page = reader.extract_tables(path, [2])
    assert 2 in tables_by_page
    rows = tables_by_page[2][0].rows
    assert rows[0][0] == "Metric" and rows[1][2] == "8.5%"


def test_pdf_rotated_page_text_still_extracts(tmp_path: Path) -> None:
    path = tmp_path / "rotated.pdf"
    make_text_pdf(path, [[PARA] * 5], rotations={1: 90})
    content = PdfReader().summarize(path)
    assert content.pages[0].rotation == 90
    assert "valuation memorandum" in content.pages[0].text
    assert content.pages[0].page_class is PageClass.TEXT


def test_pdf_encrypted_is_access_error(tmp_path: Path) -> None:
    path = tmp_path / "locked.pdf"
    make_encrypted_pdf(path)
    content = PdfReader().summarize(path)
    assert DocFlag.ACCESS_ERROR in content.flags
    assert content.pages == []
    assert PdfReader().extract_tables(path, [1]) == {}


def test_pdf_zero_byte_and_corrupt_are_corrupt_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"this is not a pdf at all")
    assert DocFlag.CORRUPT_FILE in PdfReader().summarize(empty).flags
    assert DocFlag.CORRUPT_FILE in PdfReader().summarize(garbage).flags


@pytest.mark.perf
def test_pdf_large_document_streams(tmp_path: Path) -> None:
    path = tmp_path / "big.pdf"
    make_text_pdf(path, [[f"Page {i} " + PARA] for i in range(1, 251)])
    content = PdfReader().summarize(path)
    assert content.page_count == 250 and len(content.pages) == 250
    assert all(p.page_class is PageClass.TEXT for p in content.pages)


# --------------------------------------------------------------------------
# Page classification: SCANNED / IMAGE_TABLE / MIXED
# --------------------------------------------------------------------------


def test_scanned_pdf_classified_scanned(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    make_scanned_pdf(path, [[PARA] * 8])
    content = PdfReader().summarize(path)
    page = content.pages[0]
    assert page.page_class is PageClass.SCANNED
    assert not page.has_text_layer
    assert page.image_area_ratio > 0.9


def test_image_table_page_classified(tmp_path: Path) -> None:
    path = tmp_path / "imgtable.pdf"
    doc = fitz.open()
    add_image_table_page(
        doc,
        [PARA] * 6,
        [["Comp", "EV/EBITDA"], ["Acme Infra", "9.1x"], ["GridCo", "8.7x"]],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()

    page = PdfReader().summarize(path).pages[0]
    assert page.page_class is PageClass.IMAGE_TABLE
    assert page.has_text_layer


# --------------------------------------------------------------------------
# OCR (RapidOCR default engine; skipped when unavailable)
# --------------------------------------------------------------------------


def test_ocr_scanned_page(tmp_path: Path) -> None:
    ocr = OcrReader()
    if not ocr.available():
        pytest.skip(f"no OCR engine: {ocr.unavailable_reason}")
    path = tmp_path / "scan.pdf"
    make_scanned_pdf(path, [["Enterprise Value: $545.0M", "Net Debt: $120.0M"]])
    results = ocr.ocr_pdf_pages(path, [1])
    assert 1 in results
    result = results[1]
    assert result.engine == "rapidocr"
    assert result.mean_confidence > 0.5
    assert "545" in result.text and "Enterprise Value" in result.text


# --------------------------------------------------------------------------
# Docx / legacy doc / pptx / xlsx
# --------------------------------------------------------------------------


def test_docx_sections_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "review.docx"
    make_docx(
        path,
        [
            ("Asset One", ["Fair value increased modestly."], [[["Metric", "Value"], ["NAV", "$100.0M"]]]),
            ("Asset Two", ["Fair value declined."], []),
        ],
    )
    reader = reader_for_extension(".docx")
    content = reader.summarize(path)
    assert content.reader == "docx" and content.page_count == 2
    assert content.pages[0].unit_name == "Asset One"
    assert content.pages[0].tables[0].rows[1][1] == "$100.0M"
    assert "declined" in content.pages[1].text


def test_legacy_doc_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0 legacy binary")
    content = reader_for_extension(".doc").summarize(path)
    assert DocFlag.UNSUPPORTED_FORMAT in content.flags
    assert content.pages == []


def test_pptx_slides_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    make_pptx(
        path,
        [("Q1 2026 Portfolio Review", ["Total NAV $1.2bn"], [[["Asset", "NAV"], ["GridCo", "$300"]]])],
    )
    content = reader_for_extension(".pptx").summarize(path)
    assert content.page_count == 1
    assert content.pages[0].unit_label == "slide"
    assert "Portfolio Review" in content.pages[0].text
    assert content.pages[0].tables[0].rows[1][0] == "GridCo"


def test_xlsx_sheets_grid(tmp_path: Path) -> None:
    path = tmp_path / "model.xlsx"
    make_xlsx(
        path,
        {
            "Summary": [["Metric", "Value"], ["Enterprise Value", 545.0], ["Net Debt", 120]],
            "Empty": [],
        },
    )
    content = reader_for_extension(".xlsx").summarize(path)
    assert content.page_count == 2
    summary = content.pages[0]
    assert summary.unit_name == "Summary"
    assert summary.tables[0].rows[1] == ["Enterprise Value", "545"]
    assert content.pages[1].tables == []


def test_unknown_extension_has_no_reader() -> None:
    assert reader_for_extension(".txt") is None
