"""Per-memo extraction engine (D4): read -> classify -> OCR -> target ->
extract -> derive, with multi-asset segmentation.

Flow for one memo file:

  1. reader.summarize() — page text, metrics, classification (D1). Reader
     flags (ACCESS_ERROR / CORRUPT_FILE / UNSUPPORTED_FORMAT) end the memo.
  2. OCR routing — SCANNED pages are OCR'd locally and their text replaced
     (ocr_engine + mean confidence recorded; they feed the confidence
     model). IMAGE_TABLE pages keep their text layer and raise a flag: an
     image-borne table is NOT extracted deterministically — OCR'd table
     geometry is unreliable — it is escalated for Phase-3 vision instead.
  3. targeting (D2) — page->band map; only the union of targeted pages gets
     pass-2 table extraction.
  4. methodology routing — the ROUTING band extracts first; the
     schema/band_routing.json table then decides which METHODOLOGY:/comps
     bands run at all (no methodology resolved => all of them run).
  5. asset segmentation — a multi-asset document (docx asset sections, or
     PDF pages starting an 'Asset Review: <name>' block) yields one
     extraction scope per asset; band pages intersect the asset's scope
     (plus the cover page). Single-asset documents use every page.
  6. band extraction + derived computation per asset (D4).

The engine is orchestration-free: no memo ids, no workbook, no cache —
run.py owns those.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.extract.bands import ALL_EXTRACTORS, ExtractionContext
from pv_extractor.extract.derived import apply_derived
from pv_extractor.extract.readers import OcrReader, reader_for_extension
from pv_extractor.extract.targeting import build_page_band_map
from pv_extractor.io_guard import open_read
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    DocFlag,
    DocumentContent,
    FieldHit,
    FlagSeverity,
    PageClass,
    PageContent,
    ReviewFlag,
    SchemaField,
)

logger = logging.getLogger(__name__)

_ASSET_MARKER_RE = re.compile(
    r"^\s*(?:asset|portfolio company|investment)\s+review[:\s—-]+(.{2,60})\s*$",
    re.IGNORECASE | re.MULTILINE,
)



class EngineResult:
    """Raw extraction product for one memo (pre-validation)."""

    def __init__(self) -> None:
        self.reader: str = ""
        self.page_count: int = 0
        self.page_classes: dict[int, PageClass] = {}
        self.page_band_map: dict[str, list[int]] = {}
        self.memo_flags: list[ReviewFlag] = []
        self.assets: list[tuple[str | None, list[FieldHit], list[ReviewFlag]]] = []
        self.fatal: bool = False  # nothing extractable (reader hard flag)


def load_schema_fields(project_dir: Path | None = None) -> list[SchemaField]:
    """Compiled schema fields (single source of truth, CLAUDE.md rule 3)."""
    schema_path = (project_dir or Path(__file__).resolve().parents[3]) / "schema" / "master_schema.json"
    with open_read(schema_path) as fh:
        doc = json.load(fh)
    return [SchemaField.model_validate(field) for field in doc["fields"]]


def load_band_routing(project_dir: Path | None = None) -> dict[str, list[str]]:
    routing_path = (project_dir or Path(__file__).resolve().parents[3]) / "schema" / "band_routing.json"
    with open_read(routing_path) as fh:
        return json.load(fh)["routing"]


def _flag(content: DocumentContent) -> ReviewFlag:
    flag = content.flags[0]
    severity = FlagSeverity.hard_fail if flag in (DocFlag.ACCESS_ERROR, DocFlag.CORRUPT_FILE) else FlagSeverity.warning
    return ReviewFlag(
        category="reader",
        description=f"{flag.value}: {content.error_detail or content.file_path}",
        severity=severity,
        reviewer_attention=True,
    )


def _run_ocr(path: str | Path, content: DocumentContent, config: Config, result: EngineResult) -> None:
    scanned = [page.page_number for page in content.pages if page.page_class is PageClass.SCANNED]
    image_tables = [page.page_number for page in content.pages if page.page_class is PageClass.IMAGE_TABLE]
    for number in image_tables:
        result.memo_flags.append(
            ReviewFlag(
                category="reader",
                description=(
                    f"image-based table detected on page {number}; values not extracted "
                    f"deterministically — escalate to Phase-3 vision"
                ),
                severity=FlagSeverity.warning,
                reviewer_attention=True,
            )
        )
    if not scanned or content.reader != "pdf":
        return
    ocr = OcrReader(config.extraction.ocr)
    if not ocr.available():
        result.memo_flags.append(
            ReviewFlag(
                category="reader",
                description=(
                    f"{DocFlag.OCR_UNAVAILABLE.value}: {len(scanned)} scanned page(s) but no OCR "
                    f"engine ({ocr.unavailable_reason})"
                ),
                severity=FlagSeverity.warning,
                reviewer_attention=True,
            )
        )
        return
    results = ocr.ocr_pdf_pages(path, scanned)
    by_number = {page.page_number: page for page in content.pages}
    for number in scanned:
        page = by_number[number]
        ocr_result = results.get(number)
        if ocr_result is None:
            result.memo_flags.append(
                ReviewFlag(
                    category="reader",
                    description=f"OCR failed on scanned page {number}",
                    severity=FlagSeverity.warning,
                    reviewer_attention=True,
                )
            )
            continue
        page.text = ocr_result.text
        page.text_char_count = len(ocr_result.text.strip())
        page.ocr_engine = ocr_result.engine
        page.ocr_mean_confidence = round(ocr_result.mean_confidence, 4)
        page.words = ocr_result.words


def _asset_scopes(content: DocumentContent) -> list[tuple[str | None, set[int]]]:
    """(asset name, page numbers) per asset; single unnamed scope of every
    page when no multi-asset structure is recognizable."""
    scopes: list[tuple[str, set[int]]] = []
    if content.reader in ("docx", "pptx"):
        named = [page for page in content.pages if page.unit_name]
        if len(named) >= 2:
            current: tuple[str, set[int]] | None = None
            for page in content.pages:
                if page.unit_name:
                    current = (page.unit_name, set())
                    scopes.append(current)
                if current is not None:
                    current[1].add(page.page_number)
    else:
        current = None
        for page in content.pages:
            marker = _ASSET_MARKER_RE.search(page.text)
            if marker is not None:
                current = (marker.group(1).strip(), set())
                scopes.append(current)
            if current is not None:
                current[1].add(page.page_number)
    if len(scopes) >= 2:
        return list(scopes)
    all_pages = {page.page_number for page in content.pages}
    return [(None, all_pages)]


def _allowed_methodology_bands(
    routing_hits: list[FieldHit], routing_table: dict[str, list[str]]
) -> set[str] | None:
    """Bands the resolved methodologies route to; None = no gate (run all)."""
    methodologies = [
        str(hit.value)
        for hit in routing_hits
        if hit.field in ("Primary Methodology", "Secondary Methodology") and hit.value
    ]
    if not methodologies:
        return None
    allowed: set[str] = set()
    for methodology in methodologies:
        allowed.update(routing_table.get(methodology, []))
    return allowed


def extract_memo(
    path: str | Path,
    config: Config,
    schema_fields: list[SchemaField],
    routing_table: dict[str, list[str]] | None = None,
) -> EngineResult:
    """Run the full deterministic extraction pipeline on one document."""
    result = EngineResult()
    extension = Path(path).suffix.lower()
    reader = reader_for_extension(extension, config.extraction.page_classification)
    if reader is None:
        result.memo_flags.append(
            ReviewFlag(
                category="reader",
                description=f"{DocFlag.UNSUPPORTED_FORMAT.value}: no reader for {extension!r}",
                severity=FlagSeverity.warning,
                reviewer_attention=True,
            )
        )
        result.fatal = True
        return result

    content = reader.summarize(path)
    result.reader = content.reader
    result.page_count = content.page_count
    if content.flags:
        result.memo_flags.append(_flag(content))
        result.fatal = True
        return result

    _run_ocr(path, content, config, result)
    result.page_classes = {page.page_number: page.page_class for page in content.pages}

    # D2: targeting, then pass-2 tables for the targeted pages only.
    result.page_band_map = build_page_band_map(content.pages, schema_fields, config.extraction)
    targeted = sorted({number for numbers in result.page_band_map.values() for number in numbers})
    tables = reader.extract_tables(path, targeted)
    for page in content.pages:
        if page.page_number in tables and not page.tables:
            page.tables = tables[page.page_number]
    log_event(
        logger, "extraction targeting", path=str(path), pages=result.page_count,
        targeted_pages=targeted, bands=len(result.page_band_map),
    )

    routing = routing_table if routing_table is not None else load_band_routing()
    # Bands gated by methodology routing: only methodology-exclusive bands.
    # band_routing.json also lists universal bands (RETURNS, CALIBRATION under
    # 'Cost') as data locations — those always run.
    routed_universe = {
        band
        for bands in routing.values()
        for band in bands
        if band.startswith("METHODOLOGY:") or "COMPS" in band
    }
    fields_by_band: dict[str, list[SchemaField]] = {}
    for field in schema_fields:
        fields_by_band.setdefault(field.band, []).append(field)
    schema_by_header = {field.header: field for field in schema_fields}
    pages_by_number = {page.page_number: page for page in content.pages}

    for asset_name, scope in _asset_scopes(content):
        ctx = ExtractionContext(cfg=config.extraction)
        scope_with_cover = scope | ({1} if pages_by_number else set())

        def band_pages(band: str) -> list[PageContent]:
            numbers = [n for n in result.page_band_map.get(band, []) if n in scope_with_cover]
            return [pages_by_number[n] for n in numbers if n in pages_by_number]

        hits: list[FieldHit] = []
        routing_extractor = next(e for e in ALL_EXTRACTORS if e.band == "METHODOLOGY ROUTING")
        routing_hits = routing_extractor.extract(
            band_pages("METHODOLOGY ROUTING"), fields_by_band.get("METHODOLOGY ROUTING", []), ctx
        )
        hits.extend(routing_hits)
        allowed = _allowed_methodology_bands(routing_hits, routing)

        for extractor in ALL_EXTRACTORS:
            if extractor is routing_extractor:
                continue
            if allowed is not None and extractor.band in routed_universe and extractor.band not in allowed:
                continue
            requires = getattr(extractor, "requires_band", None)
            if allowed is not None and requires is not None and requires not in allowed:
                continue
            hits.extend(extractor.extract(band_pages(extractor.band), fields_by_band.get(extractor.band, []), ctx))

        hits = apply_derived(hits, schema_by_header, config.validation, ctx)
        result.assets.append((asset_name, hits, ctx.flags))
        log_event(
            logger, "asset extracted", path=str(path), asset=asset_name,
            hits=len(hits), flags=len(ctx.flags),
        )
    return result
