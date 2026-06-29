"""Reader registry (D1): one DocumentReader per file family."""

from __future__ import annotations

from pv_extractor.config import PageClassificationConfig

from .base import DocumentReader
from .docx import DocReaderUnsupported, DocxReader
from .ocr import OcrPageResult, OcrReader
from .pdf import PdfReader
from .pptx import PptxReader
from .xlsx import XlsxReader

__all__ = [
    "DocumentReader",
    "DocxReader",
    "DocReaderUnsupported",
    "OcrPageResult",
    "OcrReader",
    "PdfReader",
    "PptxReader",
    "XlsxReader",
    "reader_for_extension",
]

_READERS: dict[str, type[DocumentReader]] = {
    ".pdf": PdfReader,
    ".docx": DocxReader,
    ".doc": DocReaderUnsupported,
    ".pptx": PptxReader,
    ".xlsx": XlsxReader,
    ".xlsm": XlsxReader,
}


def reader_for_extension(
    extension: str, classification: PageClassificationConfig | None = None
) -> DocumentReader | None:
    """Reader instance for a lowercase dotted extension, or None when the
    extension has no reader (caller flags UNSUPPORTED_FORMAT)."""
    reader_cls = _READERS.get(extension.lower())
    return reader_cls(classification) if reader_cls is not None else None
