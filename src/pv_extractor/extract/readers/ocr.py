"""OcrReader (D1): local OCR for SCANNED / IMAGE_TABLE pages only.

The page is rendered to PNG at config dpi (default 300) with pymupdf and OCR'd
fully locally: RapidOCR (onnxruntime, pip-installable, models on disk) by
default, pytesseract optionally behind config (requires a system tesseract).
NO cloud OCR — nothing leaves the machine. Per page we record which engine ran
and the mean word confidence (0-1), which feeds the FieldHit confidence model
(page_class_ocr x mean confidence).

Engines are imported lazily and cached per process; an unavailable engine is
reported through `available` so the caller can flag OCR_UNAVAILABLE instead of
crashing the run.
"""

from __future__ import annotations

import logging
import re
import statistics
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import fitz

from pv_extractor.config import OcrConfig
from pv_extractor.io_guard import open_read
from pv_extractor.logging_setup import log_event

logger = logging.getLogger(__name__)

_RAPIDOCR_SINGLETON = None  # model load is expensive; one engine per process

# Per-page OCR cache. OCR is the single most expensive step on scanned memos and
# the SAME pages are OCR'd more than once per run — the extraction pass and the
# LLM payload pass both call ocr_pdf_pages over the same file. Memoize by page
# identity (path + mtime + size + dpi + engine) so a page is OCR'd once per
# process. Bounded LRU, thread-safe (jobs run in a shared-process pool).
_OCR_CACHE: "OrderedDict[tuple, OcrPageResult]" = OrderedDict()
_OCR_CACHE_LOCK = threading.Lock()
_OCR_CACHE_MAX = 512


def clear_ocr_cache() -> None:
    with _OCR_CACHE_LOCK:
        _OCR_CACHE.clear()


@dataclass
class OcrPageResult:
    """OCR output for one page."""

    page_number: int
    text: str
    mean_confidence: float  # 0-1 mean word/snippet confidence
    engine: str


class OcrReader:
    def __init__(self, cfg: OcrConfig | None = None) -> None:
        self.cfg = cfg or OcrConfig()
        self._engine_error: str | None = None

    # ------------------------------------------------------------------
    # engine availability
    # ------------------------------------------------------------------

    @property
    def engine_name(self) -> str:
        return self.cfg.engine

    def available(self) -> bool:
        if not self.cfg.enabled:
            self._engine_error = "ocr disabled in config"
            return False
        if self.cfg.engine == "rapidocr":
            return self._rapidocr() is not None
        if self.cfg.engine == "tesseract":
            try:
                import pytesseract  # noqa: F401
            except ImportError as exc:
                self._engine_error = f"pytesseract not installed: {exc}"
                return False
            return True
        self._engine_error = f"unknown ocr engine {self.cfg.engine!r}"
        return False

    @property
    def unavailable_reason(self) -> str | None:
        return self._engine_error

    def _rapidocr(self):
        global _RAPIDOCR_SINGLETON
        if _RAPIDOCR_SINGLETON is not None:
            return _RAPIDOCR_SINGLETON
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            self._engine_error = f"rapidocr not installed: {exc}"
            return None
        try:
            _RAPIDOCR_SINGLETON = RapidOCR()
        except Exception as exc:  # missing local models etc.
            self._engine_error = f"rapidocr engine init failed: {exc}"
            return None
        return _RAPIDOCR_SINGLETON

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    def ocr_pdf_pages(self, path: str | Path, page_numbers: list[int]) -> dict[int, OcrPageResult]:
        """Render and OCR the given 1-based pages of a PDF. Pages that fail
        are simply absent from the result (caller flags them)."""
        if not self.available():
            return {}
        wanted = sorted(set(page_numbers))
        # File identity for the cache; on a stat failure fall back to uncached.
        try:
            st = os.stat(path)
            ident: tuple | None = (str(path), st.st_mtime_ns, st.st_size, self.cfg.dpi, self.cfg.engine)
        except OSError:
            ident = None

        out: dict[int, OcrPageResult] = {}
        misses: list[int] = []
        if ident is not None:
            with _OCR_CACHE_LOCK:
                for number in wanted:
                    hit = _OCR_CACHE.get((*ident, number))
                    if hit is not None:
                        _OCR_CACHE.move_to_end((*ident, number))
                        out[number] = hit
                    else:
                        misses.append(number)
        else:
            misses = wanted
        if not misses:
            return out

        with open_read(path) as fh:
            data = fh.read()
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            log_event(logger, "ocr open failed", path=str(path), error=str(exc))
            return out
        try:
            for number in misses:
                if not 1 <= number <= doc.page_count:
                    continue
                page = doc[number - 1]
                png = page.get_pixmap(dpi=self.cfg.dpi).tobytes("png")
                result = self._ocr_png(png, number)
                if result is not None:
                    out[number] = result
                    if ident is not None:
                        with _OCR_CACHE_LOCK:
                            _OCR_CACHE[(*ident, number)] = result
                            _OCR_CACHE.move_to_end((*ident, number))
                            while len(_OCR_CACHE) > _OCR_CACHE_MAX:
                                _OCR_CACHE.popitem(last=False)
                    log_event(
                        logger, "ocr page complete", path=str(path), page=number,
                        engine=result.engine, mean_confidence=result.mean_confidence,
                        chars=len(result.text),
                    )
        finally:
            doc.close()
        return out

    def _ocr_png(self, png: bytes, page_number: int) -> OcrPageResult | None:
        if self.cfg.engine == "rapidocr":
            return self._ocr_rapidocr(png, page_number)
        return self._ocr_tesseract(png, page_number)

    def _ocr_rapidocr(self, png: bytes, page_number: int) -> OcrPageResult | None:
        engine = self._rapidocr()
        if engine is None:
            return None
        try:
            result = engine(png)
        except Exception as exc:
            log_event(logger, "rapidocr failed", page=page_number, error=str(exc))
            return None
        txts = list(result.txts or [])
        scores = list(result.scores or [])
        boxes = list(result.boxes) if result.boxes is not None else []
        text = deglue(_lines_from_boxes(txts, boxes))
        mean_conf = float(statistics.fmean(scores)) if scores else 0.0
        return OcrPageResult(page_number=page_number, text=text, mean_confidence=mean_conf, engine="rapidocr")

    def _ocr_tesseract(self, png: bytes, page_number: int) -> OcrPageResult | None:
        try:
            import io

            import pytesseract
            from PIL import Image
        except ImportError as exc:
            self._engine_error = f"pytesseract not installed: {exc}"
            return None
        if self.cfg.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.cfg.tesseract_cmd
        try:
            image = Image.open(io.BytesIO(png))
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            log_event(logger, "tesseract failed", page=page_number, error=str(exc))
            return None
        lines: dict[tuple[int, int, int], list[str]] = {}
        confs: list[float] = []
        for word, conf, block, par, line in zip(
            data["text"], data["conf"], data["block_num"], data["par_num"], data["line_num"]
        ):
            if not word.strip():
                continue
            lines.setdefault((block, par, line), []).append(word)
            conf_f = float(conf)
            if conf_f >= 0:
                confs.append(conf_f / 100.0)
        text = deglue("\n".join(" ".join(words) for _, words in sorted(lines.items())))
        mean_conf = float(statistics.fmean(confs)) if confs else 0.0
        return OcrPageResult(page_number=page_number, text=text, mean_confidence=mean_conf, engine="tesseract")


# OCR engines glue words together on tight scans ('FundManager:$99.0M').
# Deterministic de-glue: split lowercase->uppercase transitions and
# letter<->currency boundaries. Fuzzy label matching absorbs the residue.
_DEGLUE_RES = (
    (re.compile(r"(?<=[a-z])(?=[A-Z])"), " "),
    (re.compile(r"(?<=[a-zA-Z])(?=[$£€])"), " "),
    (re.compile(r"(?<=%)(?=[A-Za-z])"), " "),
)


def deglue(text: str) -> str:
    for pattern, repl in _DEGLUE_RES:
        text = pattern.sub(repl, text)
    return text


def _lines_from_boxes(txts: list[str], boxes: list) -> str:
    """Reconstruct reading order from OCR snippet boxes: group snippets into
    lines by y-center proximity (half the median box height), then sort each
    line left-to-right. Falls back to engine order without boxes."""
    if not txts:
        return ""
    if not boxes or len(boxes) != len(txts):
        return "\n".join(txts)

    items = []
    heights = []
    for text, box in zip(txts, boxes):
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        items.append(((min(ys) + max(ys)) / 2.0, min(xs), text))
        heights.append(max(ys) - min(ys))
    items.sort(key=lambda item: (item[0], item[1]))
    line_tolerance = max(statistics.median(heights) / 2.0, 1.0)

    lines: list[list[tuple[float, float, str]]] = []
    for item in items:
        if lines and abs(item[0] - lines[-1][-1][0]) <= line_tolerance:
            lines[-1].append(item)
        else:
            lines.append([item])
    return "\n".join(" ".join(text for _, _, text in sorted(line, key=lambda i: i[1])) for line in lines)
