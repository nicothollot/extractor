"""Review queue: one row per flag / low-confidence cell across a run.

Rows are built from the per-memo audit JSONs (the provenance record Phase 2
wrote for exactly this purpose). Actions go through the writer's Phase-4
entry points against the run's own workbook COPY — never the template,
never anything under pv_root — and every action is appended to the memo's
audit JSON under "review_actions" so the queue state survives restarts and
the trail stays reviewable.

Item ids are stable across reloads:
    <row_memo_id>::flag::<n>        (n = position in the asset's flag list)
    <row_memo_id>::cell::<header>   (low-confidence extracted cell)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from pv_extractor.api import runs_service
from pv_extractor.config import Config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.models import SchemaField
from pv_extractor.write.workbook import WorkbookWriter

_writer_lock = threading.Lock()


class ReviewItem(BaseModel):
    id: str
    kind: str  # flag | low_confidence
    run_id: str
    memo_id: str
    row_memo_id: str
    client: str = ""
    deal: str = ""
    asset_name: str | None = None
    qa_status: str = ""
    source_filename: str = ""
    field: str | None = None
    band: str | None = None
    value: bool | int | float | str | None = None
    raw_text: str = ""
    unit: str | None = None
    method: str | None = None
    confidence: float | None = None
    evidence: str = ""
    page: int | None = None
    bbox: list[float] | None = None
    has_page_image: bool = False
    reader: str = ""  # "pdf" when the source supports full-document paging
    source_page_count: int = 0  # total pages, for the full-document viewer
    category: str = ""
    description: str = ""
    severity: str = ""
    reviewer_attention: bool = False
    # The hard-fail reasons for this asset (why qa_status is qa_fail), attached
    # to EVERY item of a failed memo so the reviewer always sees why it failed.
    qa_fail_reasons: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    resolved: bool = False
    resolution: dict | None = None


def _method_chip(method: str | None, page_classes: dict, page: int | None) -> str | None:
    """deterministic | computed | metadata | ocr | llm:<model>:<effort> —
    'ocr' when the deterministic hit came off an OCR'd page."""
    if method is None:
        return None
    if method.startswith("claude-code:"):
        return "llm:" + method.removeprefix("claude-code:")
    if method == "deterministic" and page is not None:
        if page_classes.get(str(page)) in ("SCANNED", "IMAGE_TABLE"):
            return "ocr"
    return method


def _hit_by_field(asset: dict) -> dict[str, dict]:
    return {hit["field"]: hit for hit in asset.get("hits", [])}


def build_queue(run_dir: Path, config: Config) -> list[ReviewItem]:
    threshold = config.extraction.confidence_threshold
    items: list[ReviewItem] = []
    for audit in runs_service.load_audits(run_dir):
        actions = {a.get("item_id"): a for a in audit.get("review_actions", [])}
        page_classes = audit.get("page_classes", {})
        renderable = audit.get("reader") == "pdf"
        base = {
            "run_id": audit.get("run_id", run_dir.name),
            "memo_id": audit["memo_id"],
            "client": audit.get("client", ""),
            "deal": audit.get("deal", ""),
            "source_filename": audit.get("file_name", ""),
            "reader": audit.get("reader", ""),
            "source_page_count": int(audit.get("page_count") or 0),
        }
        for asset in audit.get("assets", []):
            hits = _hit_by_field(asset)
            row_id = asset["row_memo_id"]
            # Why this memo failed QA (hard-fail flags) — surfaced on every item.
            qa_fail_reasons = [
                f.get("description", "")
                for f in asset.get("flags", [])
                if f.get("severity") == "hard_fail"
            ]
            flagged_fields: set[str] = set()
            for n, flag in enumerate(asset.get("flags", [])):
                item_id = f"{row_id}::flag::{n}"
                hit = hits.get(flag.get("field") or "")
                if flag.get("field"):
                    flagged_fields.add(flag["field"])
                action = actions.get(item_id)
                items.append(
                    ReviewItem(
                        id=item_id, kind="flag", row_memo_id=row_id,
                        asset_name=asset.get("asset_name"), qa_status=asset.get("qa_status", ""),
                        field=flag.get("field"),
                        band=hit.get("band") if hit else None,
                        value=hit.get("value") if hit else None,
                        raw_text=hit.get("raw_text", "") if hit else "",
                        unit=hit.get("unit") if hit else None,
                        method=_method_chip(hit.get("method") if hit else None, page_classes,
                                            hit.get("page") if hit else None),
                        confidence=hit.get("confidence") if hit else None,
                        evidence=hit.get("evidence", "") if hit else "",
                        page=hit.get("page") if hit else None,
                        bbox=hit.get("bbox") if hit else None,
                        has_page_image=bool(renderable and hit and hit.get("page")),
                        category=flag.get("category", ""),
                        description=flag.get("description", ""),
                        severity=flag.get("severity", ""),
                        reviewer_attention=bool(flag.get("reviewer_attention")),
                        qa_fail_reasons=qa_fail_reasons,
                        conflicts=(hit.get("conflicts") or []) if hit else [],
                        resolved=action is not None,
                        resolution=action,
                        **base,
                    )
                )
            for hit in asset.get("hits", []):
                if hit.get("method") in ("metadata", "computed"):
                    continue
                confidence = hit.get("confidence") or 0.0
                if confidence >= threshold or hit.get("value") is None:
                    continue
                if hit["field"] in flagged_fields:
                    continue  # the flag row already carries this cell
                item_id = f"{row_id}::cell::{hit['field']}"
                action = actions.get(item_id)
                items.append(
                    ReviewItem(
                        id=item_id, kind="low_confidence", row_memo_id=row_id,
                        asset_name=asset.get("asset_name"), qa_status=asset.get("qa_status", ""),
                        field=hit["field"], band=hit.get("band"),
                        value=hit.get("value"), raw_text=hit.get("raw_text", ""),
                        unit=hit.get("unit"),
                        method=_method_chip(hit.get("method"), page_classes, hit.get("page")),
                        confidence=confidence,
                        evidence=hit.get("evidence", ""), page=hit.get("page"),
                        bbox=hit.get("bbox"),
                        has_page_image=bool(renderable and hit.get("page")),
                        category="low_confidence",
                        description=f"confidence {confidence:.2f} below threshold {threshold:.2f}",
                        severity="warning",
                        qa_fail_reasons=qa_fail_reasons,
                        conflicts=hit.get("conflicts") or [],
                        resolved=action is not None,
                        resolution=action,
                        **base,
                    )
                )
    return items


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


class ReviewError(RuntimeError):
    pass


def _schema_by_header() -> dict[str, SchemaField]:
    return {f.header: f for f in load_schema_fields()}


def _append_audit_action(
    run_dir: Path,
    config: Config,
    memo_id: str,
    action: dict,
    *,
    manual_hit: dict | None = None,
    row_memo_id: str | None = None,
) -> None:
    audit = runs_service.load_audit(run_dir, memo_id)
    if audit is None:
        raise ReviewError(f"no audit record for memo {memo_id!r}")
    audit.setdefault("review_actions", []).append(action)
    # add_value: record the analyst's value as a provenance-backed hit so the
    # cell reads back with its page/bbox/quote (and the evidence viewer can
    # re-highlight the region the reviewer marked).
    if manual_hit is not None and row_memo_id is not None:
        for asset in audit.get("assets", []):
            if asset.get("row_memo_id") == row_memo_id:
                kept = [h for h in asset.get("hits", []) if h.get("field") != manual_hit["field"]]
                kept.append(manual_hit)
                asset["hits"] = kept
                break
    path = run_dir / runs_service.AUDIT_DIR / f"{memo_id}.json"
    with guarded_open_write(path, config.pv_root) as fh:
        json.dump(audit, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def _base_memo_id(row_memo_id: str) -> str:
    """MEMO_..._001-A2 -> MEMO_..._001 (audit files are per memo)."""
    return row_memo_id.split("-A")[0]


def apply_action(
    run_dir: Path,
    config: Config,
    item: ReviewItem,
    *,
    action: str,
    note: str | None = None,
    value: bool | int | float | str | None = None,
    field: str | None = None,
    page: int | None = None,
    bbox: list[float] | None = None,
    evidence: str | None = None,
) -> dict:
    """accept | edit | unresolvable | add_value. Edits and added values write
    the run's workbook copy via the writer seam; every action lands in the
    memo's audit JSON. add_value also records the value as a manual hit with
    the page/bbox/quote the reviewer marked on the document."""
    if action not in ("accept", "edit", "unresolvable", "add_value"):
        raise ReviewError(f"unknown action {action!r}")
    wb_path = runs_service.workbook_path(run_dir)
    if wb_path is None:
        raise ReviewError(f"no output workbook in {run_dir}")

    record: dict = {
        "item_id": item.id,
        "row_memo_id": item.row_memo_id,
        "field": item.field,
        "action": action,
        "note": note,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manual_hit: dict | None = None

    with _writer_lock:
        writer = WorkbookWriter(wb_path, load_schema_fields(), config.pv_root)
        if action == "edit":
            if item.field is None:
                raise ReviewError("cannot edit an item with no linked field")
            schema_field = _schema_by_header().get(item.field)
            if schema_field is None:
                raise ReviewError(f"unknown schema field {item.field!r}")
            writer.update_cell(item.row_memo_id, schema_field.col_index, value)
            record["old_value"] = item.value
            record["new_value"] = value
        elif action == "add_value":
            header = item.field or field
            if header is None:
                raise ReviewError("add_value requires a target field")
            schema_field = _schema_by_header().get(header)
            if schema_field is None:
                raise ReviewError(f"unknown schema field {header!r}")
            writer.update_cell(item.row_memo_id, schema_field.col_index, value)
            record.update({
                "field": header, "old_value": item.value, "new_value": value,
                "page": page, "bbox": bbox, "evidence": evidence,
            })
            manual_hit = {
                "field": header, "col_index": schema_field.col_index,
                "band": schema_field.band, "value": value,
                "unit": schema_field.unit, "page": page, "bbox": bbox,
                "method": "manual", "confidence": 1.0,
                "evidence": (evidence or "")[: config.extraction.max_evidence_chars],
                "raw_text": str(value), "confidence_components": {"manual": 1.0},
                "conflicts": [],
            }
        if item.kind == "flag" and item.description:
            if action == "unresolvable":
                writer.resolve_flag(
                    item.row_memo_id, item.description, resolved=False,
                    note=f"UNRESOLVABLE: {note or 'marked unresolvable in review'}",
                )
            else:
                writer.resolve_flag(
                    item.row_memo_id, item.description, resolved=True,
                    note=note or f"{action}ed in GUI review",
                )
        writer.save()

    _append_audit_action(
        run_dir, config, _base_memo_id(item.row_memo_id), record,
        manual_hit=manual_hit, row_memo_id=item.row_memo_id,
    )
    return record
