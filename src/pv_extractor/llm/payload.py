"""Payload assembly for one memo's escalation (rules 1 + 8).

PAGES, NOT DOCUMENTS: the payload contains only the pages named by the
escalated fields' candidate pages (from the Phase-2 page->band map) plus
pages 1-3 — unless that selection already covers the whole memo, in which
case the whole (short) memo goes.

PAGE IMAGE ECONOMY: TEXT/MIXED pages travel as extracted text with table
cells serialized as markdown pipe tables; SCANNED and IMAGE_TABLE pages
travel as PNG page images rendered at most `llm.image_max_long_edge` pixels
on the long edge. Scanned pages are also OCR'd locally — that text is NOT
sent (the image is better), it is kept for quote-grounding (rule 5).

Everything is written into a per-memo payload directory under the run dir
(payload files, manifest.json, schema.json, prompt files) through the
io_guard — the share itself is only ever opened read-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # pymupdf

from pv_extractor.config import Config
from pv_extractor.extract.readers import OcrReader, reader_for_extension
from pv_extractor.io_guard import guarded_open_write, open_read
from pv_extractor.logging_setup import log_event
from pv_extractor.models import EscalationField, EvidenceWord, PageClass, PageContent, TableData
from pv_extractor.normalize import normalize_evidence_text

logger = logging.getLogger(__name__)

_IMAGE_CLASSES = (PageClass.SCANNED, PageClass.IMAGE_TABLE)


class PayloadError(RuntimeError):
    """The document could not be re-read for payload assembly."""


@dataclass
class PayloadPage:
    number: int
    page_class: PageClass
    kind: str  # text | image
    rel_path: str
    sha256: str


@dataclass
class MemoPayload:
    directory: Path
    pages: list[PayloadPage] = field(default_factory=list)
    page_texts: dict[int, str] = field(default_factory=dict)  # grounding text per page
    page_words: dict[int, list[EvidenceWord]] = field(default_factory=dict)  # grounding geometry per page
    page_blocks: dict[int, str] = field(default_factory=dict)  # per-page prompt section
    dynamic_prompt: str = ""
    payload_hash: str = ""  # sha256 of the canonical manifest (incl. file hashes)
    image_count: int = 0
    ocr_hostile: bool = False  # any SCANNED/IMAGE_TABLE page in the payload
    # (document_id, relative filename) of the SOURCE documents copied into the
    # payload dir for direct_document_read — the model reads these with its own
    # tool instead of consuming a pre-rendered page payload.
    source_documents: list[tuple[str, str]] = field(default_factory=list)
    # Original source paths/hashes keyed by document_id. These are used for
    # review rendering and cache invalidation; the model only sees the copied
    # relative filenames above.
    source_document_paths: dict[str, str] = field(default_factory=dict)
    source_document_hashes: dict[str, str] = field(default_factory=dict)
    source_document_sizes: dict[str, int] = field(default_factory=dict)
    # Direct-read citations use document-local page numbers, while embedded
    # multi-document prompts use a global page sequence. This map bridges them.
    source_page_map: dict[str, int] = field(default_factory=dict)  # "D01:7" -> global page
    page_sources: dict[int, dict[str, object]] = field(default_factory=dict)

    def read_instruction(self, pages: list[int] | None = None) -> str:
        """Prompt section for direct_document_read: point the model at the real
        source files in its working directory and have it Read them, instead of
        embedding a rasterized/OCR'd page payload. Cite the document_id we assign
        here so values map back to the right document."""
        if not self.source_documents:
            return self.dynamic_prompt  # nothing copied — fall back to the payload
        wanted_pages = sorted(set(pages or []))
        wanted_doc_ids = {
            str(info.get("document_id"))
            for number, info in self.page_sources.items()
            if not wanted_pages or number in wanted_pages
        }
        if not wanted_doc_ids:
            wanted_doc_ids = {doc_id for doc_id, _name in self.source_documents}
        lines = ["== SOURCE DOCUMENTS =="]
        for doc_id, name in self.source_documents:
            if doc_id not in wanted_doc_ids:
                continue
            lines.append(f'- document_id {doc_id}: "{name}"')
        if wanted_pages and self.page_sources:
            lines.append("")
            lines.append("Relevant pages from the bounded extraction plan:")
            for global_page in wanted_pages:
                info = self.page_sources.get(global_page)
                if not info:
                    continue
                doc_id = info.get("document_id")
                doc_page = info.get("document_page")
                label = info.get("label") or doc_id
                lines.append(f"- prompt page {global_page}: document_id {doc_id}, document page {doc_page} ({label})")
        lines.append("")
        lines.append(
            "Each document above is a real file in your current working directory. "
            "Use the Read tool to inspect the relevant pages first, then read "
            "additional pages only when needed to answer a requested field. Cite "
            "the document_id and that document's own 1-based page number for every "
            "value. Read the actual documents; do not guess."
        )
        return "\n".join(lines) + "\n"

    def resolve_citation(self, document_id: str | None, page: int | None) -> tuple[int | None, dict[str, object] | None]:
        """Return (global_page_for_grounding, source_info) for an LLM citation.

        Single-document payloads use the same page number for both concepts.
        Combined deal payloads ask the model to cite document-local pages; this
        maps that citation back to the global page used by page_texts/page_words.
        """
        if page is None:
            return None, None
        if document_id:
            global_page = self.source_page_map.get(f"{document_id}:{page}")
            if global_page is not None:
                return global_page, self.page_sources.get(global_page)
        return page, self.page_sources.get(page)

    def selected_source_hashes(self, pages: list[int]) -> list[str]:
        """Source-document hashes for direct-read cache keys."""
        doc_ids = {
            str(info.get("document_id"))
            for number, info in self.page_sources.items()
            if number in set(pages)
        }
        if not doc_ids:
            doc_ids = set(self.source_document_hashes)
        return [
            f"{doc_id}:{self.source_document_hashes[doc_id]}"
            for doc_id in sorted(doc_ids)
            if doc_id in self.source_document_hashes
        ]

    def source_read_estimate_chars(self, pages: list[int]) -> int:
        """Conservative prompt-size proxy for direct-read budget reservation.

        The provider may read source PDFs outside the prompt text. Use local page
        text plus a small source-byte proxy so unpriced/default models still
        reserve non-zero budget for direct reads.
        """
        doc_ids = {
            str(info.get("document_id"))
            for number, info in self.page_sources.items()
            if number in set(pages)
        }
        page_chars = sum(len(self.page_texts.get(number, "")) for number in set(pages))
        size_proxy = sum(min(self.source_document_sizes.get(doc_id, 0), 200_000) // 4 for doc_id in doc_ids)
        return max(page_chars, size_proxy)

    def page_kind(self, number: int) -> str:
        """'image' | 'text' for a page in this payload (defaults to 'text')."""
        return next((p.kind for p in self.pages if p.number == number), "text")

    def scoped_prompt(self, pages: list[int]) -> str:
        """The dynamic prompt restricted to `pages` (band-scoped LLM calls send
        only the pages relevant to that band — fewer tokens, sharper focus).
        Falls back to the full prompt when no scoped page has a block."""
        blocks = [self.page_blocks[n] for n in sorted(set(pages)) if n in self.page_blocks]
        if not blocks:
            return self.dynamic_prompt
        return "== DOCUMENT PAGES ==\n\n" + "\n\n".join(blocks) + "\n"

    def scoped_image_count(self, pages: list[int]) -> int:
        wanted = set(pages)
        return sum(1 for p in self.pages if p.number in wanted and p.kind == "image")

    def scoped_image_pages(self, pages: list[int]) -> list[PayloadPage]:
        wanted = set(pages)
        return [p for p in self.pages if p.number in wanted and p.kind == "image"]

    def scoped_image_paths(self, pages: list[int]) -> list[Path]:
        return [self.directory / p.rel_path for p in self.scoped_image_pages(pages)]

    def selected_page_hashes(self, pages: list[int]) -> list[str]:
        """Canonical page-scope hashes for LLM response caching.

        Text pages use normalized page text so harmless PDF line wrapping or
        Unicode spacing changes do not invalidate useful cache entries. Image
        pages use the rendered image hash from the manifest.
        """
        page_meta = {p.number: p for p in self.pages}
        hashes: list[str] = []
        for number in sorted(set(pages)):
            meta = page_meta.get(number)
            if meta is None:
                continue
            if meta.kind == "image":
                hashes.append(f"{number}:image:{meta.sha256}")
                continue
            text = normalize_evidence_text(self.page_texts.get(number, ""))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            hashes.append(f"{number}:text:{digest}")
        return hashes

    def scoped_prompt_with_ocr_fallback(self, pages: list[int]) -> str:
        """A scoped prompt where image blocks are replaced by local OCR text.

        This is used only for providers whose CLI cannot attach images. Pages
        with no OCR/text are still represented by the original block; the caller
        is expected to fail explicitly rather than asking the model to infer
        from an unread image.
        """
        blocks: list[str] = []
        image_numbers = {p.number for p in self.scoped_image_pages(pages)}
        for number in sorted(set(pages)):
            block = self.page_blocks.get(number)
            if not block:
                continue
            if number not in image_numbers:
                blocks.append(block)
                continue
            text = (self.page_texts.get(number) or "").strip()
            if not text:
                blocks.append(block)
                continue
            heading = block.split("\n", 1)[0]
            blocks.append(f"{heading} [OCR text fallback]\n{text}")
        if not blocks:
            return self.dynamic_prompt
        return "== DOCUMENT PAGES ==\n\n" + "\n\n".join(blocks) + "\n"


def select_pages(
    fields: list[EscalationField], page_count: int, summary_pages: int, max_pages: int
) -> list[int]:
    """Candidate pages of the escalated fields + pages 1..summary_pages,
    capped at max_pages (summary pages first — they carry the headline
    identity every band needs). A selection covering the memo means the
    whole memo IS the targeted payload."""
    wanted: set[int] = set(range(1, min(summary_pages, page_count) + 1))
    for escalated in fields:
        wanted.update(p for p in escalated.candidate_pages if 1 <= p <= page_count)
    ordered = sorted(wanted)
    if len(ordered) > max_pages:
        summary = [p for p in ordered if p <= summary_pages]
        rest = [p for p in ordered if p > summary_pages]
        ordered = summary + rest[: max_pages - len(summary)]
    return ordered


def _table_to_markdown(table: TableData) -> str:
    rows = [["" if cell is None else str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
            for row in table.rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _page_text_block(page: PageContent) -> str:
    parts = [page.text.rstrip()]
    for table in page.tables:
        markdown = _table_to_markdown(table)
        if markdown:
            parts.append(markdown)
    return "\n\n".join(part for part in parts if part).strip()


def _render_page_png(data: bytes, page_number: int, max_long_edge: int) -> bytes | None:
    """Render one PDF page to PNG, long edge capped at max_long_edge."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None
    try:
        if not 1 <= page_number <= doc.page_count:
            return None
        page = doc[page_number - 1]
        long_edge_points = max(page.rect.width, page.rect.height, 1.0)
        zoom = max_long_edge / long_edge_points
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")
    except Exception as exc:
        log_event(logger, "page render failed", page=page_number, error=str(exc))
        return None
    finally:
        doc.close()


def _write_payload_file(path: Path, data: bytes, pv_root: str) -> str:
    with guarded_open_write(path, pv_root, mode="wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


# Filenames safe to drop in the call's working dir (and to type into a Read tool
# arg across the WSL bridge): keep the original stem readable, strip the rest.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _copy_source_document(file_path: str, payload_dir: Path, doc_id: str, pv_root: str) -> tuple[str, str, int]:
    """Copy the source document (read-only) into the payload dir so the model can
    Read it directly. Returns (relative filename, sha256, byte length)."""
    src = Path(file_path)
    safe = _SAFE_NAME_RE.sub("_", src.name) or f"{doc_id}{src.suffix.lower()}"
    rel = f"{doc_id}_{safe}"
    with open_read(file_path) as fh:
        data = fh.read()
    digest = _write_payload_file(payload_dir / rel, data, pv_root)
    return rel, digest, len(data)


def assemble_payload(
    *,
    file_path: str,
    fields: list[EscalationField],
    config: Config,
    payload_dir: Path,
) -> MemoPayload:
    """Re-read the memo (read-only) and materialize the page payload."""
    extension = Path(file_path).suffix.lower()
    reader = reader_for_extension(extension, config.extraction.page_classification)
    if reader is None:
        raise PayloadError(f"no reader for {extension!r}")
    content = reader.summarize(file_path)
    if content.flags:
        raise PayloadError(f"document unreadable: {[f.value for f in content.flags]}")

    selected = select_pages(
        fields, content.page_count,
        config.extraction.summary_pages, config.llm.max_pages_per_memo,
    )
    pages_by_number = {page.page_number: page for page in content.pages}
    selected = [n for n in selected if n in pages_by_number]

    # Tables for the selected text pages (pass 2, targeted pages only).
    tables = reader.extract_tables(file_path, selected)
    for number in selected:
        page = pages_by_number[number]
        if number in tables and not page.tables:
            page.tables = tables[number]

    # Local OCR on selected SCANNED pages — grounding text only, never sent.
    scanned = [
        n for n in selected
        if pages_by_number[n].page_class is PageClass.SCANNED and content.reader == "pdf"
    ]
    ocr_conf: dict[int, float] = {}
    if scanned and config.extraction.ocr.enabled:
        ocr = OcrReader(config.extraction.ocr)
        if ocr.available():
            for number, result in ocr.ocr_pdf_pages(file_path, scanned).items():
                pages_by_number[number].text = result.text
                pages_by_number[number].ocr_engine = result.engine
                pages_by_number[number].words = result.words
                ocr_conf[number] = result.mean_confidence

    pdf_bytes: bytes | None = None
    if content.reader == "pdf":
        with open_read(file_path) as fh:
            pdf_bytes = fh.read()

    payload = MemoPayload(directory=payload_dir)
    if config.llm.direct_document_read:
        rel, digest, size = _copy_source_document(file_path, payload_dir, "D01", config.pv_root)
        payload.source_documents.append(("D01", rel))
        payload.source_document_paths["D01"] = file_path
        payload.source_document_hashes["D01"] = digest
        payload.source_document_sizes["D01"] = size
    prompt_sections: list[str] = ["== DOCUMENT PAGES =="]
    block_start = len(prompt_sections)
    for number in selected:
        page = pages_by_number[number]
        payload.source_page_map[f"D01:{number}"] = number
        payload.page_sources[number] = {
            "document_id": "D01",
            "document_page": number,
            "source_file": file_path,
            "label": Path(file_path).name,
        }
        # A SCANNED page that OCR'd cleanly is sent as TEXT, not an image: the
        # Read-tool/vision path is slow (huge output, re-done per call) and the
        # OCR text is reliable at high confidence. IMAGE_TABLE always stays an
        # image (OCR mangles table structure).
        high_ocr_text = (
            page.page_class is PageClass.SCANNED
            and config.llm.prefer_ocr_text_over_image
            and ocr_conf.get(number, 0.0) >= config.llm.ocr_text_min_confidence
            and bool((page.text or "").strip())
        )
        as_image = page.page_class in _IMAGE_CLASSES and pdf_bytes is not None and not high_ocr_text
        png = (
            _render_page_png(pdf_bytes, number, config.llm.image_max_long_edge)
            if as_image
            else None
        )
        if png is not None:
            rel = f"pages/page_{number:03d}.png"
            digest = _write_payload_file(payload_dir / rel, png, config.pv_root)
            payload.pages.append(
                PayloadPage(number=number, page_class=page.page_class, kind="image",
                            rel_path=rel, sha256=digest)
            )
            payload.image_count += 1
            payload.page_texts[number] = page.text  # OCR text (possibly empty)
            payload.page_words[number] = page.words
            prompt_sections.append(
                f"--- page {number} ({page.page_class.value}, image) ---\n"
                f'This page is an image. View it with the Read tool: "{rel}"'
            )
        else:
            text_block = _page_text_block(page)
            rel = f"pages/page_{number:03d}.txt"
            digest = _write_payload_file(
                payload_dir / rel, text_block.encode("utf-8"), config.pv_root
            )
            payload.pages.append(
                PayloadPage(number=number, page_class=page.page_class, kind="text",
                            rel_path=rel, sha256=digest)
            )
            payload.page_texts[number] = text_block
            payload.page_words[number] = page.words
            prompt_sections.append(f"--- page {number} ({page.page_class.value}) ---\n{text_block}")

    # OCR-hostile only if a page is ACTUALLY sent as an image (a scanned page
    # downgraded to high-confidence OCR text is text, not vision — AUTO can stay
    # on sonnet rather than escalating to opus for it).
    payload.ocr_hostile = any(p.kind == "image" for p in payload.pages)
    # Per-page sections (everything after the "== DOCUMENT PAGES ==" header)
    # line up with `selected` in order — keep them so band-scoped calls can
    # compose a prompt from only the pages a band needs.
    for offset, number in enumerate(selected):
        payload.page_blocks[number] = prompt_sections[block_start + offset]
    payload.dynamic_prompt = "\n\n".join(prompt_sections) + "\n"

    manifest = {
        "pages": [
            {"number": p.number, "page_class": p.page_class.value, "kind": p.kind,
             "file": p.rel_path, "sha256": p.sha256}
            for p in payload.pages
        ],
        "source_documents": [
            {
                "document_id": doc_id,
                "file": rel,
                "sha256": payload.source_document_hashes.get(doc_id),
                "source_file": payload.source_document_paths.get(doc_id),
            }
            for doc_id, rel in payload.source_documents
        ],
        "page_sources": payload.page_sources,
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    with guarded_open_write(payload_dir / "manifest.json", config.pv_root, mode="wb") as fh:
        fh.write(manifest_bytes)
    payload.payload_hash = hashlib.sha256(manifest_bytes).hexdigest()

    log_event(
        logger, "llm payload assembled",
        pages=len(payload.pages), images=payload.image_count,
        page_count=content.page_count, ocr_hostile=payload.ocr_hostile,
    )
    return payload


def assemble_deal_payload(
    *,
    files: list[tuple[str, str]],
    fields: list[EscalationField],
    config: Config,
    payload_dir: Path,
) -> MemoPayload:
    """Re-read EVERY document of one deal-period (read-only) into ONE payload.

    This backs the one-call-per-deal LLM pass: a deal's whole document set is
    extracted in a single local provider call. All documents' pages are concatenated
    under a single GLOBAL page index (1..N) so quote-grounding still keys off
    `page` -> page_texts[page]; each block is labelled with its source document
    and that document's own page number so the model (and a reviewer) can tell
    the documents apart. A document that fits the per-document cap is sent WHOLE
    ("parse the whole document"); an oversized one falls back to candidate-page
    selection. Cleanly-OCR'd scanned pages travel as TEXT (fast); IMAGE_TABLE and
    low-confidence scanned pages stay images. The total page count is bounded by
    `llm.max_pages_per_deal` so a deal with many large documents stays feasible.
    """
    payload = MemoPayload(directory=payload_dir)
    prompt_sections: list[str] = ["== DOCUMENT PAGES =="]
    block_offsets: list[tuple[int, int]] = []  # (global page no, prompt_sections index)
    manifest_docs: list[dict] = []
    total_cap = max(1, config.llm.max_pages_per_deal)
    per_doc_cap = max(1, config.llm.max_pages_per_memo)
    summary_pages = config.extraction.summary_pages
    global_no = 0
    readable = 0

    for doc_index, (file_path, label) in enumerate(files):
        if global_no >= total_cap:
            break
        doc_id = f"D{doc_index + 1:02d}"
        extension = Path(file_path).suffix.lower()
        reader = reader_for_extension(extension, config.extraction.page_classification)
        if reader is None:
            log_event(logger, "deal payload: skipped doc (no reader)", doc=doc_index)
            continue
        try:
            content = reader.summarize(file_path)
        except OSError as exc:
            log_event(logger, "deal payload: skipped doc (read error)", doc=doc_index, error=str(exc))
            continue
        if content.flags:
            log_event(
                logger, "deal payload: skipped doc (unreadable)",
                doc=doc_index, flags=[f.value for f in content.flags],
            )
            continue
        readable += 1

        if config.llm.direct_document_read:
            src_rel, digest, size = _copy_source_document(file_path, payload_dir, doc_id, config.pv_root)
            payload.source_documents.append((doc_id, src_rel))
            payload.source_document_paths[doc_id] = file_path
            payload.source_document_hashes[doc_id] = digest
            payload.source_document_sizes[doc_id] = size

        doc_cap = min(per_doc_cap, total_cap - global_no)
        if content.page_count <= doc_cap:
            selected = list(range(1, content.page_count + 1))  # whole document
        else:
            selected = select_pages(fields, content.page_count, summary_pages, doc_cap)
        pages_by_number = {page.page_number: page for page in content.pages}
        selected = [n for n in selected if n in pages_by_number]
        if not selected:
            continue

        tables = reader.extract_tables(file_path, selected)
        for number in selected:
            page = pages_by_number[number]
            if number in tables and not page.tables:
                page.tables = tables[number]

        scanned = [
            n for n in selected
            if pages_by_number[n].page_class is PageClass.SCANNED and content.reader == "pdf"
        ]
        ocr_conf: dict[int, float] = {}
        if scanned and config.extraction.ocr.enabled:
            ocr = OcrReader(config.extraction.ocr)
            if ocr.available():
                for number, result in ocr.ocr_pdf_pages(file_path, scanned).items():
                    pages_by_number[number].text = result.text
                    pages_by_number[number].ocr_engine = result.engine
                    pages_by_number[number].words = result.words
                    ocr_conf[number] = result.mean_confidence

        pdf_bytes: bytes | None = None
        if content.reader == "pdf":
            with open_read(file_path) as fh:
                pdf_bytes = fh.read()

        manifest_docs.append({"doc": doc_index, "label": label, "pages": selected})
        for number in selected:
            if global_no >= total_cap:
                break
            page = pages_by_number[number]
            global_no += 1
            payload.source_page_map[f"{doc_id}:{number}"] = global_no
            payload.page_sources[global_no] = {
                "document_id": doc_id,
                "document_page": number,
                "source_file": file_path,
                "label": label,
            }
            high_ocr_text = (
                page.page_class is PageClass.SCANNED
                and config.llm.prefer_ocr_text_over_image
                and ocr_conf.get(number, 0.0) >= config.llm.ocr_text_min_confidence
                and bool((page.text or "").strip())
            )
            as_image = page.page_class in _IMAGE_CLASSES and pdf_bytes is not None and not high_ocr_text
            png = (
                _render_page_png(pdf_bytes, number, config.llm.image_max_long_edge)
                if as_image
                else None
            )
            if png is not None:
                rel = f"pages/doc{doc_index:02d}_page_{number:03d}.png"
                digest = _write_payload_file(payload_dir / rel, png, config.pv_root)
                payload.pages.append(
                    PayloadPage(number=global_no, page_class=page.page_class, kind="image",
                                rel_path=rel, sha256=digest)
                )
                payload.image_count += 1
                payload.page_texts[global_no] = page.text
                payload.page_words[global_no] = page.words
                block_offsets.append((global_no, len(prompt_sections)))
                prompt_sections.append(
                    f"--- page {global_no} | {label}, document page {number} "
                    f"({page.page_class.value}, image) ---\n"
                    f'This page is an image. View it with the Read tool: "{rel}"'
                )
            else:
                text_block = _page_text_block(page)
                rel = f"pages/doc{doc_index:02d}_page_{number:03d}.txt"
                digest = _write_payload_file(
                    payload_dir / rel, text_block.encode("utf-8"), config.pv_root
                )
                payload.pages.append(
                    PayloadPage(number=global_no, page_class=page.page_class, kind="text",
                                rel_path=rel, sha256=digest)
                )
                payload.page_texts[global_no] = text_block
                payload.page_words[global_no] = page.words
                block_offsets.append((global_no, len(prompt_sections)))
                prompt_sections.append(
                    f"--- page {global_no} | {label}, document page {number} "
                    f"({page.page_class.value}) ---\n{text_block}"
                )

    if global_no == 0:
        raise PayloadError(f"no readable pages across {len(files)} document(s) for the deal")

    payload.ocr_hostile = any(p.kind == "image" for p in payload.pages)
    for global_idx, section_offset in block_offsets:
        payload.page_blocks[global_idx] = prompt_sections[section_offset]
    payload.dynamic_prompt = "\n\n".join(prompt_sections) + "\n"

    manifest = {
        "pages": [
            {"number": p.number, "page_class": p.page_class.value, "kind": p.kind,
             "file": p.rel_path, "sha256": p.sha256}
            for p in payload.pages
        ],
        "documents": manifest_docs,
        "source_documents": [
            {
                "document_id": doc_id,
                "file": rel,
                "sha256": payload.source_document_hashes.get(doc_id),
                "source_file": payload.source_document_paths.get(doc_id),
            }
            for doc_id, rel in payload.source_documents
        ],
        "page_sources": payload.page_sources,
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    with guarded_open_write(payload_dir / "manifest.json", config.pv_root, mode="wb") as fh:
        fh.write(manifest_bytes)
    payload.payload_hash = hashlib.sha256(manifest_bytes).hexdigest()

    log_event(
        logger, "llm deal payload assembled",
        documents=readable, pages=len(payload.pages), images=payload.image_count,
        ocr_hostile=payload.ocr_hostile,
    )
    return payload
