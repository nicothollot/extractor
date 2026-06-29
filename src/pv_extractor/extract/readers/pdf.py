"""PdfReader (D1): pymupdf text/metrics/classification, pymupdf table finder
with a pdfplumber fallback for the targeted pages.

Page classification drives OCR routing here and vision routing in Phase 3:

  TEXT         normal text layer
  SCANNED      no/negligible text layer and the page is image-covered
  IMAGE_TABLE  text layer present, but a large wide image block that looks
               tabular (a pasted spreadsheet/exhibit) — detected from image
               block geometry, since a table inside an image is invisible to
               both table finders
  MIXED        text layer plus significant image coverage that does not look
               tabular (photos, charts)

Hard cases are explicit: encrypted -> ACCESS_ERROR, zero-byte/unparseable ->
CORRUPT_FILE. Pages are iterated lazily and only their extracted text is
retained, so a 200+ page document never lives in memory as page objects.
Rotation is handled by pymupdf itself (get_text applies /Rotate).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import fitz  # pymupdf

from pv_extractor.evidence import pymupdf_words_to_evidence_words
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    DocFlag,
    DocumentContent,
    PageClass,
    PageContent,
    TableData,
)

from .base import DocumentReader

logger = logging.getLogger(__name__)


class PdfReader(DocumentReader):
    name = "pdf"

    # ------------------------------------------------------------------
    # pass 1
    # ------------------------------------------------------------------

    def summarize(self, path: str | Path, max_pages: int | None = None) -> DocumentContent:
        try:
            data = self._read_bytes(path)
        except OSError as exc:
            return self._empty(path, DocFlag.ACCESS_ERROR, f"{type(exc).__name__}: {exc}")
        if not data:
            return self._empty(path, DocFlag.CORRUPT_FILE, "zero-byte file")
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:  # pymupdf raises various internal types
            return self._empty(path, DocFlag.CORRUPT_FILE, f"{type(exc).__name__}: {exc}")

        try:
            if doc.needs_pass:
                return self._empty(path, DocFlag.ACCESS_ERROR, "encrypted PDF (password required)")
            content = DocumentContent(file_path=str(path), reader=self.name, page_count=doc.page_count)
            for page in doc:  # lazy: one page at a time, nothing retained
                if max_pages is not None and page.number >= max_pages:
                    break
                content.pages.append(self._summarize_page(page))
            return content
        finally:
            doc.close()

    def _summarize_page(self, page: fitz.Page) -> PageContent:
        text = page.get_text("text")
        char_count = len(text.strip())
        page_area = max(float(page.rect.get_area()), 1.0)

        image_area = 0.0
        tabular_image = False
        for info in page.get_image_info():
            rect = fitz.Rect(info["bbox"]) & page.rect  # clip to the page
            if rect.is_empty:
                continue
            image_area += rect.get_area()
            width_ratio = rect.width / max(page.rect.width, 1.0)
            aspect = rect.width / max(rect.height, 1.0)
            if (
                rect.get_area() / page_area >= self.classification.image_table_min_area_ratio
                and width_ratio >= self.classification.image_table_min_width_ratio
                and aspect >= self.classification.image_table_min_aspect
            ):
                tabular_image = True
        image_ratio = min(image_area / page_area, 1.0)

        has_text_layer = char_count >= self.classification.min_text_chars
        if not has_text_layer and image_ratio >= self.classification.image_area_threshold:
            page_class = PageClass.SCANNED
        elif has_text_layer and tabular_image:
            page_class = PageClass.IMAGE_TABLE
        elif has_text_layer and image_ratio >= self.classification.image_area_threshold:
            page_class = PageClass.MIXED
        else:
            page_class = PageClass.TEXT

        words = pymupdf_words_to_evidence_words(page.get_text("words"))
        return PageContent(
            page_number=page.number + 1,
            text=text,
            text_char_count=char_count,
            image_area_ratio=round(image_ratio, 4),
            has_text_layer=has_text_layer,
            rotation=page.rotation,
            page_class=page_class,
            words=words,
        )

    # ------------------------------------------------------------------
    # pass 2
    # ------------------------------------------------------------------

    def extract_tables(self, path: str | Path, page_numbers: list[int]) -> dict[int, list[TableData]]:
        try:
            data = self._read_bytes(path)
        except OSError:
            return {}
        if not data:
            return {}
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            return {}

        out: dict[int, list[TableData]] = {}
        plumber_pages: list[int] = []
        try:
            if doc.needs_pass:
                return {}
            for number in sorted(set(page_numbers)):
                if not 1 <= number <= doc.page_count:
                    continue
                tables = self._pymupdf_tables(doc, number)
                if tables:
                    out[number] = tables
                else:
                    plumber_pages.append(number)
        finally:
            doc.close()

        if plumber_pages:
            for number, tables in self._pdfplumber_tables(data, plumber_pages).items():
                if tables:
                    out[number] = tables
        return out

    def _pymupdf_tables(self, doc: fitz.Document, number: int) -> list[TableData]:
        page = doc[number - 1]
        tables: list[TableData] = []
        try:
            finder = page.find_tables()
        except Exception as exc:
            log_event(logger, "pymupdf table finder failed", page=number, error=str(exc))
            return []
        for tab in finder.tables:
            rows = [[cell if cell not in ("", None) else None for cell in row] for row in tab.extract()]
            if not any(any(cell for cell in row) for row in rows):
                continue
            tables.append(
                TableData(page_number=number, rows=rows, bbox=tuple(tab.bbox), source="pymupdf")
            )
        return tables

    def _pdfplumber_tables(self, data: bytes, page_numbers: list[int]) -> dict[int, list[TableData]]:
        """Fallback when pymupdf finds nothing — pdfplumber's line-based
        detection catches some table styles pymupdf misses."""
        try:
            import pdfplumber
        except ImportError:
            return {}
        out: dict[int, list[TableData]] = {}
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for number in page_numbers:
                    if not 1 <= number <= len(pdf.pages):
                        continue
                    page = pdf.pages[number - 1]
                    tables = []
                    for grid in page.extract_tables() or []:
                        rows = [
                            [cell if cell not in ("", None) else None for cell in row] for row in grid
                        ]
                        if any(any(cell for cell in row) for row in rows):
                            tables.append(TableData(page_number=number, rows=rows, source="pdfplumber"))
                    if tables:
                        out[number] = tables
        except Exception as exc:
            log_event(logger, "pdfplumber fallback failed", error=str(exc))
        return out
