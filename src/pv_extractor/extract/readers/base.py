"""Reader interface (D1).

Reading is two-pass so band extractors never see every page (D2) and large
documents are never fully materialized:

  pass 1  summarize(path)            per-page text, metrics, classification —
                                     cheap, streaming, no table extraction
  pass 2  extract_tables(path, nos)  table grids for the TARGETED pages only

Readers open files exclusively through io_guard.open_read and parse from the
in-memory bytes; nothing ever holds a write handle near the share. Hard
conditions (encrypted, corrupt, unsupported format) come back as DocFlags on
the DocumentContent, never as exceptions — one bad file must not kill a run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pv_extractor.config import PageClassificationConfig
from pv_extractor.io_guard import open_read
from pv_extractor.models import DocFlag, DocumentContent, TableData


class DocumentReader(ABC):
    """One implementation per file family; see readers/__init__.py registry."""

    name: str = ""

    def __init__(self, classification: PageClassificationConfig | None = None) -> None:
        self.classification = classification or PageClassificationConfig()

    @abstractmethod
    def summarize(self, path: str | Path, max_pages: int | None = None) -> DocumentContent:
        """Pass 1: every page's text, metrics and PageClass; tables only when
        the format makes them free (docx/pptx/xlsx). Never raises for bad
        files — flags instead. With max_pages only the leading pages are
        materialized (peek-verification), page_count still reports the full
        document."""

    @abstractmethod
    def extract_tables(self, path: str | Path, page_numbers: list[int]) -> dict[int, list[TableData]]:
        """Pass 2: table grids for the given 1-based page numbers only."""

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _read_bytes(self, path: str | Path) -> bytes | None:
        """Whole-file read through the io_guard; None on access failure."""
        with open_read(path) as fh:
            return fh.read()

    def _empty(self, path: str | Path, flag: DocFlag, detail: str) -> DocumentContent:
        return DocumentContent(
            file_path=str(path), reader=self.name, flags=[flag], error_detail=detail
        )
