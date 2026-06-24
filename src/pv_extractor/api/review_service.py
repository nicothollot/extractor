"""Review queue: one row per flag / low-confidence cell across a run.

Rows are built from the per-memo audit JSONs (the provenance record Phase 2
wrote for exactly this purpose). Actions go through the writer's Phase-4
entry points against the run's own workbook COPY — never the template,
never anything under pv_root — and every action is appended to the memo's
audit JSON under "review_actions" so the queue state survives restarts and
the trail stays reviewable.

Item ids are stable across reloads:
    <row_memo_id>::flag::<hash(row, field, category, issue_code)>
    <row_memo_id>::cell::<hash(row, field)>
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from pv_extractor.api import runs_service
from pv_extractor.config import Config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.models import EvidenceMatchMethod, EvidenceRef, FlagSeverity, SchemaField
from pv_extractor.validate.flags import normalize_flag_text
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
    evidence_ref: EvidenceRef | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    grounding_status: str = "none"  # box | page_only | none
    grounding_reason: str = ""
    issue_code: str = ""
    issue_descriptions: list[str] = Field(default_factory=list)
    reviewer_comment: str | None = None
    page: int | None = None
    bbox: list[float] | None = None
    has_page_image: bool = False
    reader: str = ""  # "pdf" when the source supports full-document paging
    source_page_count: int = 0  # total pages, for the full-document viewer
    category: str = ""
    description: str = ""
    severity: str = ""
    reviewer_attention: bool = False
    # Deprecated migration shim. Memo-level QA reasons now live in MemoIssue
    # records returned beside the queue instead of being copied onto field cards.
    qa_fail_reasons: list[str] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    resolved: bool = False
    resolution: dict | None = None


class MemoIssue(BaseModel):
    id: str
    run_id: str
    memo_id: str
    source_filename: str = ""
    descriptions: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    severity: str = ""
    reviewer_attention: bool = False
    resolved: bool = False
    resolution: dict | None = None


def _digest(parts: list[object], length: int = 16) -> str:
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def _severity_value(severity: str | None) -> int:
    order = {FlagSeverity.info.value: 0, FlagSeverity.warning.value: 1, FlagSeverity.hard_fail.value: 2}
    return order.get(str(severity or ""), 0)


def _max_severity(flags: list[dict]) -> str:
    if not flags:
        return ""
    return max((str(flag.get("severity") or "") for flag in flags), key=_severity_value)


def _unique_text(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        key = normalize_flag_text(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _issue_key(row_id: str, flag: dict) -> tuple[str, str, str, str]:
    field = str(flag.get("field") or "")
    issue_code = str(flag.get("code") or flag.get("category") or normalize_flag_text(flag.get("description")))
    return (row_id, field, str(flag.get("category") or ""), issue_code)


def _evidence_ref_from_hit(audit: dict, hit: dict | None) -> EvidenceRef | None:
    if not hit:
        return None
    raw = hit.get("evidence_ref")
    if raw:
        ref = EvidenceRef.model_validate(raw)
    elif hit.get("page") or hit.get("evidence") or hit.get("bbox"):
        method = EvidenceMatchMethod.page_only
        reason = "legacy hit has no resolved bbox"
        if hit.get("bbox") is not None:
            method = EvidenceMatchMethod.manual_box if hit.get("method") == "manual" else EvidenceMatchMethod.table_cell
            reason = ""
        ref = EvidenceRef(
            source_id=audit.get("memo_id"),
            source_file=hit.get("source_file") or audit.get("file_path"),
            display_page=hit.get("page"),
            quote=hit.get("evidence", ""),
            raw_text=hit.get("raw_text", ""),
            bbox=hit.get("bbox"),
            match_method=method,
            match_score=hit.get("confidence"),
            provenance="legacy_audit_hit",
            extraction_method=hit.get("method"),
            no_geometry_reason=reason or None,
        )
    else:
        return None
    if ref.source_id is None:
        ref.source_id = audit.get("memo_id")
    if ref.source_file is None:
        ref.source_file = hit.get("source_file") or audit.get("file_path")
    return ref


def _distinct_evidence_refs(refs: list[EvidenceRef | None]) -> list[EvidenceRef]:
    out: list[EvidenceRef] = []
    seen: set[tuple] = set()
    for ref in refs:
        if ref is None:
            continue
        key = (ref.source_file, ref.display_page, ref.bbox, normalize_flag_text(ref.quote), ref.match_method.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _grounding(ref: EvidenceRef | None) -> tuple[str, str]:
    if ref is None or ref.display_page is None:
        return "none", ""
    if ref.bbox is not None:
        return "box", ""
    return "page_only", ref.no_geometry_reason or "page evidence available, exact box unavailable"


def _method_chip(method: str | None, page_classes: dict, page: int | None) -> str | None:
    """deterministic | computed | metadata | ocr | llm:<model>:<effort> —
    'ocr' when the deterministic hit came off an OCR'd page."""
    if method is None:
        return None
    if method.startswith("llm:"):
        return method
    if method.startswith("claude-code:"):
        return "llm:" + method.removeprefix("claude-code:")
    if method == "deterministic" and page is not None:
        if page_classes.get(str(page)) in ("SCANNED", "IMAGE_TABLE"):
            return "ocr"
    return method


def _hit_by_field(asset: dict) -> dict[str, dict]:
    return {hit["field"]: hit for hit in asset.get("hits", [])}


def build_review(run_dir: Path, config: Config) -> tuple[list[ReviewItem], list[MemoIssue]]:
    threshold = config.extraction.confidence_threshold
    items: list[ReviewItem] = []
    memo_issue_groups: dict[tuple[str, str], dict] = {}
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
        memo_flags = [*audit.get("memo_flags", [])]
        for asset in audit.get("assets", []):
            hits = _hit_by_field(asset)
            row_id = asset["row_memo_id"]
            memo_flags.extend(f for f in asset.get("flags", []) if not f.get("field"))
            flagged_fields: set[str] = set()
            grouped_flags: dict[tuple[str, str, str, str], dict] = {}
            for n, flag in enumerate(asset.get("flags", [])):
                if not flag.get("field"):
                    continue
                flagged_fields.add(flag["field"])
                key = _issue_key(row_id, flag)
                group = grouped_flags.setdefault(key, {"flags": [], "legacy_ids": []})
                group["flags"].append(flag)
                group["legacy_ids"].append(f"{row_id}::flag::{n}")
            for key, group in grouped_flags.items():
                flags = group["flags"]
                flag = flags[0]
                field_name = flag.get("field") or ""
                item_id = f"{row_id}::flag::{_digest(list(key))}"
                hit = hits.get(field_name)
                evidence_ref = _evidence_ref_from_hit(audit, hit)
                conflict_refs = [
                    EvidenceRef.model_validate(c["evidence_ref"])
                    for c in (hit.get("conflicts") or []) if isinstance(c, dict) and c.get("evidence_ref")
                ] if hit else []
                evidence_refs = _distinct_evidence_refs([evidence_ref, *conflict_refs])
                grounding_status, grounding_reason = _grounding(evidence_ref)
                action = actions.get(item_id)
                if action is None:
                    action = next((actions.get(legacy) for legacy in group["legacy_ids"] if actions.get(legacy)), None)
                descriptions = _unique_text([f.get("description", "") for f in flags])
                description = "; ".join(descriptions)
                severity = _max_severity(flags)
                reviewer_attention = any(bool(f.get("reviewer_attention")) for f in flags)
                issue_code = str(flag.get("code") or flag.get("category") or "")
                items.append(
                    ReviewItem(
                        id=item_id, kind="flag", row_memo_id=row_id,
                        asset_name=asset.get("asset_name"), qa_status=asset.get("qa_status", ""),
                        field=field_name,
                        band=hit.get("band") if hit else None,
                        value=hit.get("value") if hit else None,
                        raw_text=hit.get("raw_text", "") if hit else "",
                        unit=hit.get("unit") if hit else None,
                        method=_method_chip(hit.get("method") if hit else None, page_classes,
                                            hit.get("page") if hit else None),
                        confidence=hit.get("confidence") if hit else None,
                        evidence=(evidence_ref.quote if evidence_ref else hit.get("evidence", "")) if hit else "",
                        evidence_ref=evidence_ref,
                        evidence_refs=evidence_refs,
                        grounding_status=grounding_status,
                        grounding_reason=grounding_reason,
                        issue_code=issue_code,
                        issue_descriptions=descriptions,
                        reviewer_comment=(action or {}).get("note") if action else None,
                        page=(evidence_ref.display_page if evidence_ref else hit.get("page")) if hit else None,
                        bbox=(list(evidence_ref.bbox) if evidence_ref and evidence_ref.bbox else hit.get("bbox")) if hit else None,
                        has_page_image=bool(renderable and hit and (evidence_ref.display_page if evidence_ref else hit.get("page"))),
                        category=flag.get("category", ""),
                        description=description,
                        severity=severity,
                        reviewer_attention=reviewer_attention,
                        qa_fail_reasons=[],
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
                item_id = f"{row_id}::cell::{_digest([row_id, hit['field']])}"
                action = actions.get(item_id)
                legacy_id = f"{row_id}::cell::{hit['field']}"
                if action is None:
                    action = actions.get(legacy_id)
                evidence_ref = _evidence_ref_from_hit(audit, hit)
                evidence_refs = _distinct_evidence_refs([evidence_ref])
                grounding_status, grounding_reason = _grounding(evidence_ref)
                items.append(
                    ReviewItem(
                        id=item_id, kind="low_confidence", row_memo_id=row_id,
                        asset_name=asset.get("asset_name"), qa_status=asset.get("qa_status", ""),
                        field=hit["field"], band=hit.get("band"),
                        value=hit.get("value"), raw_text=hit.get("raw_text", ""),
                        unit=hit.get("unit"),
                        method=_method_chip(hit.get("method"), page_classes, hit.get("page")),
                        confidence=confidence,
                        evidence=evidence_ref.quote if evidence_ref else hit.get("evidence", ""),
                        evidence_ref=evidence_ref,
                        evidence_refs=evidence_refs,
                        grounding_status=grounding_status,
                        grounding_reason=grounding_reason,
                        issue_code="low_confidence",
                        issue_descriptions=[f"confidence {confidence:.2f} below threshold {threshold:.2f}"],
                        reviewer_comment=(action or {}).get("note") if action else None,
                        page=evidence_ref.display_page if evidence_ref else hit.get("page"),
                        bbox=list(evidence_ref.bbox) if evidence_ref and evidence_ref.bbox else hit.get("bbox"),
                        has_page_image=bool(renderable and (evidence_ref.display_page if evidence_ref else hit.get("page"))),
                        category="low_confidence",
                        description=f"confidence {confidence:.2f} below threshold {threshold:.2f}",
                        severity="warning",
                        qa_fail_reasons=[],
                        conflicts=hit.get("conflicts") or [],
                        resolved=action is not None,
                        resolution=action,
                        **base,
                    )
                )
        for flag in memo_flags:
            issue_code = str(flag.get("code") or flag.get("category") or normalize_flag_text(flag.get("description")))
            key = (audit["memo_id"], issue_code)
            group = memo_issue_groups.setdefault(
                key,
                {
                    "flags": [],
                    "run_id": audit.get("run_id", run_dir.name),
                    "memo_id": audit["memo_id"],
                    "source_filename": audit.get("file_name", ""),
                },
            )
            group["flags"].append(flag)

    memo_issues: list[MemoIssue] = []
    for key, group in memo_issue_groups.items():
        flags = group["flags"]
        descriptions = _unique_text([f.get("description", "") for f in flags])
        item_id = f"{group['memo_id']}::memo::{_digest(list(key))}"
        memo_issues.append(
            MemoIssue(
                id=item_id,
                run_id=group["run_id"],
                memo_id=group["memo_id"],
                source_filename=group["source_filename"],
                descriptions=descriptions,
                categories=sorted({str(f.get("category") or "") for f in flags if f.get("category")}),
                severity=_max_severity(flags),
                reviewer_attention=any(bool(f.get("reviewer_attention")) for f in flags),
            )
        )
    return items, memo_issues


def build_queue(run_dir: Path, config: Config) -> list[ReviewItem]:
    return build_review(run_dir, config)[0]


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
            manual_ref = EvidenceRef(
                source_id=item.memo_id,
                source_file=(item.evidence_ref.source_file if item.evidence_ref else None),
                display_page=page,
                quote=(evidence or "")[: config.extraction.max_evidence_chars],
                raw_text=str(value),
                bbox=tuple(float(v) for v in bbox) if bbox else None,
                match_method=EvidenceMatchMethod.manual_box if bbox else EvidenceMatchMethod.page_only,
                match_score=1.0,
                provenance="manual_review",
                provider="reviewer",
                extraction_method="manual",
                no_geometry_reason=None if bbox else "manual value saved without a bbox",
            )
            record.update({
                "field": header, "old_value": item.value, "new_value": value,
                "page": page, "bbox": bbox, "evidence": evidence,
                "evidence_ref": manual_ref.model_dump(mode="json"),
            })
            manual_hit = {
                "field": header, "col_index": schema_field.col_index,
                "band": schema_field.band, "value": value,
                "unit": schema_field.unit, "page": page, "bbox": bbox,
                "method": "manual", "confidence": 1.0,
                "evidence": (evidence or "")[: config.extraction.max_evidence_chars],
                "evidence_ref": manual_ref.model_dump(mode="json"),
                "raw_text": str(value), "confidence_components": {"manual": 1.0},
                "conflicts": [],
            }
        if item.kind == "flag" and item.description:
            descriptions = item.issue_descriptions or [item.description]
            if action == "unresolvable":
                for description in descriptions:
                    writer.resolve_flag(
                        item.row_memo_id, description, resolved=False,
                        note=f"UNRESOLVABLE: {note or 'marked unresolvable in review'}",
                    )
            else:
                for description in descriptions:
                    writer.resolve_flag(
                        item.row_memo_id, description, resolved=True,
                        note=note or f"{action}ed in GUI review",
                    )
        writer.save()

    _append_audit_action(
        run_dir, config, _base_memo_id(item.row_memo_id), record,
        manual_hit=manual_hit, row_memo_id=item.row_memo_id,
    )
    return record
