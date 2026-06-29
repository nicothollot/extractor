"""Review-queue evidence images: the source page rendered on demand with
the evidence snippet's region highlighted when a bbox is available.

The source document is read strictly through io_guard.open_read (bytes in
memory; nothing under pv_root is ever opened for writing) and rendered
with pymupdf. Renders are cached under the run directory keyed on
(file path, page, bbox, dpi) so repeat views are instant."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from pv_extractor.api import runs_service
from pv_extractor.config import Config
from pv_extractor.evidence import clamp_bbox
from pv_extractor.io_guard import guarded_open_write, open_read

_HIGHLIGHT_STROKE = (0.85, 0.42, 0.04)  # restrained amber, not a neon marker
_HIGHLIGHT_FILL = (1.0, 0.78, 0.35)


class EvidenceError(RuntimeError):
    pass


def _known_source_files(audit: dict) -> set[str]:
    out = {str(audit.get("file_path") or "")}
    for asset in audit.get("assets", []):
        for hit in asset.get("hits", []):
            if hit.get("source_file"):
                out.add(str(hit["source_file"]))
            ref = hit.get("evidence_ref") or {}
            if isinstance(ref, dict) and ref.get("source_file"):
                out.add(str(ref["source_file"]))
            for conflict in hit.get("conflicts") or []:
                conflict_ref = conflict.get("evidence_ref") if isinstance(conflict, dict) else None
                if isinstance(conflict_ref, dict) and conflict_ref.get("source_file"):
                    out.add(str(conflict_ref["source_file"]))
    return {path for path in out if path}


def _source_file_from_audit(audit: dict, config: Config, source_file: str | None) -> str:
    file_path = source_file or audit.get("file_path")
    if not file_path:
        raise EvidenceError("audit carries no source path")
    if source_file is not None and str(source_file) not in _known_source_files(audit):
        raise EvidenceError("source file is not referenced by this audit")
    if Path(file_path).suffix.lower() != ".pdf":
        raise EvidenceError("page rendering is only available for PDF sources")
    return str(file_path)


def render_page(
    run_dir: Path,
    config: Config,
    memo_id: str,
    page_number: int,
    bbox: tuple[float, float, float, float] | None = None,
    source_file: str | None = None,
) -> Path:
    """Render one source page to a cached PNG; returns the PNG path."""
    audit = runs_service.load_audit(run_dir, memo_id)
    if audit is None:
        raise EvidenceError(f"no audit record for memo {memo_id!r}")
    file_path = _source_file_from_audit(audit, config, source_file)

    dpi = config.gui.evidence_dpi
    key = hashlib.sha1(
        f"{file_path}|{page_number}|{bbox}|{dpi}".encode("utf-8")
    ).hexdigest()[:20]
    png_path = run_dir / "gui" / "evidence" / f"{memo_id}_p{page_number}_{key}.png"
    if png_path.exists():
        return png_path

    with open_read(file_path) as fh:
        data = fh.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — surfaced as an API error
        raise EvidenceError(f"could not open source document: {exc}") from exc
    try:
        if not 1 <= page_number <= doc.page_count:
            raise EvidenceError(f"page {page_number} out of range (1..{doc.page_count})")
        page = doc[page_number - 1]
        if bbox is not None:
            safe_bbox = clamp_bbox(bbox, page.rect.width, page.rect.height)
            if safe_bbox is not None:
                rect = pymupdf.Rect(*safe_bbox)
                shape = page.new_shape()
                shape.draw_rect(rect)
                shape.finish(
                    color=_HIGHLIGHT_STROKE, width=1.5,
                    fill=_HIGHLIGHT_FILL, fill_opacity=0.25,
                )
                shape.commit()
        zoom = dpi / 72.0
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        png_bytes = pixmap.tobytes("png")
    finally:
        doc.close()

    with guarded_open_write(png_path, config.pv_root, mode="wb") as fh:
        fh.write(png_bytes)
    return png_path


def render_file_page(
    config: Config, file_path: str, page_number: int, dpi: int | None = None
) -> Path:
    """Render one page of an ARBITRARY (read-only) PDF to a cached PNG —
    used by the New Run 'Confirm documents' candidate preview, which has no
    run/audit record to key off. The source must be a PDF under pv_root (the
    only place candidates come from); rendering is read-only and cached under
    output_dir/gui/preview keyed on (path, mtime, size, page, dpi). An optional
    higher `dpi` is requested by the GUI for a crisp magnifier lens."""
    from pv_extractor.io_guard import is_under_pv_root

    if not is_under_pv_root(file_path, config.pv_root):
        raise EvidenceError("file is outside pv_root")
    if Path(file_path).suffix.lower() != ".pdf":
        raise EvidenceError("page preview is only available for PDF sources")
    try:
        st = Path(file_path).stat()
        ident = f"{file_path}|{st.st_mtime_ns}|{st.st_size}"
    except OSError as exc:
        raise EvidenceError(f"could not stat source: {exc}") from exc

    # Clamp a caller-supplied dpi to a sane range so the lens can't request a
    # ruinous render.
    dpi = max(72, min(int(dpi), 400)) if dpi else config.gui.evidence_dpi
    key = hashlib.sha1(f"{ident}|{page_number}|{dpi}".encode("utf-8")).hexdigest()[:20]
    png_path = Path(config.output_dir) / "gui" / "preview" / f"p{page_number}_{key}.png"
    if png_path.exists():
        return png_path

    with open_read(file_path) as fh:
        data = fh.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — surfaced as an API error
        raise EvidenceError(f"could not open source document: {exc}") from exc
    try:
        if not 1 <= page_number <= doc.page_count:
            raise EvidenceError(f"page {page_number} out of range (1..{doc.page_count})")
        zoom = dpi / 72.0
        pixmap = doc[page_number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        png_bytes = pixmap.tobytes("png")
    finally:
        doc.close()

    with guarded_open_write(png_path, config.pv_root, mode="wb") as fh:
        fh.write(png_bytes)
    return png_path


def page_words(
    run_dir: Path,
    config: Config,
    memo_id: str,
    page_number: int,
    source_file: str | None = None,
) -> dict:
    """Page geometry + word boxes (PDF points) for the Add-Value highlighter.

    The frontend overlays selectable word spans on the rendered page so the
    reviewer can drag-select text (and the union box becomes the evidence
    region). Scanned/image pages carry no extractable words -> the frontend
    falls back to free box-drawing (the image marker tool)."""
    audit = runs_service.load_audit(run_dir, memo_id)
    if audit is None:
        raise EvidenceError(f"no audit record for memo {memo_id!r}")
    file_path = _source_file_from_audit(audit, config, source_file)

    with open_read(file_path) as fh:
        data = fh.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — surfaced as an API error
        raise EvidenceError(f"could not open source document: {exc}") from exc
    try:
        if not 1 <= page_number <= doc.page_count:
            raise EvidenceError(f"page {page_number} out of range (1..{doc.page_count})")
        page_count = doc.page_count
        page = doc[page_number - 1]
        rect = page.rect
        words = [
            {
                "x0": round(w[0], 2), "y0": round(w[1], 2),
                "x1": round(w[2], 2), "y1": round(w[3], 2), "text": w[4],
            }
            for w in page.get_text("words")
        ]
    finally:
        doc.close()
    return {
        "page": page_number,
        "page_count": page_count,
        "width": round(rect.width, 2),
        "height": round(rect.height, 2),
        "words": words,
    }
