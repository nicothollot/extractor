"""Run browsing, jobs (+ live WebSocket progress), the review queue,
evidence rendering and the locator review endpoints."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from pv_extractor.api import (
    evidence_service,
    multi_search_service,
    preflight_service,
    review_service,
    runs_service,
    selection_service,
)
from pv_extractor.api.jobs import JobConflict, JobManager
from pv_extractor.api.schemas import (
    BulkAcceptRequest,
    LocateRequest,
    MultiSearchRunRequest,
    MultiSearchSelectionRequest,
    OpenFolderRequest,
    OverrideRequest,
    ReviewActionRequest,
    RunRequest,
    SourceDocsRequest,
    VerifyFileRequest,
)
from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.io_guard import is_under_pv_root

router = APIRouter(prefix="/api")


def _config(request: Request) -> Config:
    return request.app.state.config


def _manager(request: Request) -> JobManager:
    return request.app.state.jobs


def _run_dir(config: Config, run_id: str) -> Path:
    if "/" in run_id or "\\" in run_id or not run_id.startswith("RUN_"):
        raise HTTPException(400, detail=f"invalid run id {run_id!r}")
    run_dir = Path(config.output_dir) / run_id
    if not run_dir.is_dir():
        raise HTTPException(404, detail=f"no run directory for {run_id!r}")
    return run_dir


# ---------------------------------------------------------------------------
# runs / output browser
# ---------------------------------------------------------------------------


@router.get("/runs")
def list_runs(request: Request) -> dict:
    return {"runs": runs_service.list_run_summaries(_config(request))}


@router.get("/runs/{run_id}")
def run_detail(run_id: str, request: Request) -> dict:
    run_dir = _run_dir(_config(request), run_id)
    return runs_service.run_summary(run_dir)


@router.get("/runs/{run_id}/flags")
def run_flags(run_id: str, request: Request) -> dict:
    run_dir = _run_dir(_config(request), run_id)
    return {"flags": runs_service.review_flags_mirror(run_dir)}


@router.get("/runs/{run_id}/run-log")
def run_log(run_id: str, request: Request) -> dict:
    run_dir = _run_dir(_config(request), run_id)
    return {"run_log": runs_service.run_log_mirror(run_dir)}


@router.get("/runs/{run_id}/index-rows")
def run_index_rows(run_id: str, request: Request) -> dict:
    """Compact preview of the Index rows this run produced (key columns +
    per-row QA status), read read-only from the run's own workbook copy."""
    run_dir = _run_dir(_config(request), run_id)
    return {"rows": runs_service.index_rows_mirror(run_dir, run_id)}


@router.get("/runs/{run_id}/workbook")
def download_workbook(run_id: str, request: Request) -> FileResponse:
    run_dir = _run_dir(_config(request), run_id)
    path = runs_service.workbook_path(run_dir)
    if path is None:
        raise HTTPException(404, detail=f"no workbook in {run_id}")
    return FileResponse(
        path, filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/runs/{run_id}/audits.zip")
def download_audits(run_id: str, request: Request) -> FileResponse:
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    path = runs_service.build_audit_zip(run_dir, config)
    return FileResponse(path, filename=f"{run_id}_audits.zip", media_type="application/zip")


@router.get("/runs/{run_id}/audit/{memo_id}")
def audit_json(run_id: str, memo_id: str, request: Request) -> dict:
    run_dir = _run_dir(_config(request), run_id)
    audit = runs_service.load_audit(run_dir, memo_id)
    if audit is None:
        raise HTTPException(404, detail=f"no audit for {memo_id!r}")
    return audit


@router.get("/runs/{run_id}/costs")
def run_costs(run_id: str, request: Request) -> dict:
    run_dir = _run_dir(_config(request), run_id)
    return {
        "entries": runs_service.ledger_entries(run_dir),
        "summary": runs_service.ledger_summary(run_dir),
    }


@router.get("/costs/history")
def costs_history(request: Request) -> dict:
    return {"points": runs_service.cost_history(_config(request))}


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/review")
def review_queue(run_id: str, request: Request) -> dict:
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    items = review_service.build_queue(run_dir, config)
    return {"items": [i.model_dump() for i in items],
            "confidence_threshold": config.extraction.confidence_threshold}


def _find_item(run_dir: Path, config: Config, item_id: str) -> review_service.ReviewItem:
    for item in review_service.build_queue(run_dir, config):
        if item.id == item_id:
            return item
    raise HTTPException(404, detail=f"unknown review item {item_id!r}")


@router.post("/runs/{run_id}/review/{item_id}/action")
def review_action(run_id: str, item_id: str, body: ReviewActionRequest, request: Request) -> dict:
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    item = _find_item(run_dir, config, item_id)
    if item.resolved:
        raise HTTPException(409, detail=f"item {item_id!r} is already resolved")
    try:
        record = review_service.apply_action(
            run_dir, config, item, action=body.action, note=body.note, value=body.value,
            field=body.field, page=body.page, bbox=body.bbox, evidence=body.evidence,
        )
    except review_service.ReviewError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return {"applied": record}


@router.post("/runs/{run_id}/review/bulk-accept")
def review_bulk_accept(run_id: str, body: BulkAcceptRequest, request: Request) -> dict:
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    applied = []
    default_note = (
        f"bulk-accepted category {body.category!r}" if body.category is not None
        else "bulk-accepted all pending items"
    )
    for item in review_service.build_queue(run_dir, config):
        if item.resolved or (body.category is not None and item.category != body.category):
            continue
        try:
            applied.append(
                review_service.apply_action(
                    run_dir, config, item, action="accept",
                    note=body.note or default_note,
                )
            )
        except review_service.ReviewError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
    return {"applied": len(applied)}


@router.get("/runs/{run_id}/evidence/{memo_id}")
def evidence_image(
    run_id: str, memo_id: str, request: Request,
    page: int, l: float | None = None, t: float | None = None,
    r: float | None = None, b: float | None = None,
) -> FileResponse:
    """The source page as PNG, with the evidence bbox highlighted when all
    four coordinates are given (PDF points, from the audit's FieldHit)."""
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    bbox = (l, t, r, b) if None not in (l, t, r, b) else None
    try:
        path = evidence_service.render_page(run_dir, config, memo_id, page, bbox)
    except evidence_service.EvidenceError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/png")


@router.get("/runs/{run_id}/page-words/{memo_id}")
def page_words(run_id: str, memo_id: str, request: Request, page: int) -> dict:
    """Page geometry + selectable word boxes for the Add-Value highlighter."""
    config = _config(request)
    run_dir = _run_dir(config, run_id)
    try:
        return evidence_service.page_words(run_dir, config, memo_id, page)
    except evidence_service.EvidenceError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@router.post("/jobs/run")
def start_run(body: RunRequest, request: Request) -> dict:
    if body.scope in ("deal",) and (not body.client or not body.deal):
        raise HTTPException(400, detail="scope=deal requires client and deal")
    if body.scope == "client" and not body.client:
        raise HTTPException(400, detail="scope=client requires client")
    try:
        job = _manager(request).start_run(body)
    except JobConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"job": job.model_dump()}


@router.get("/jobs")
def list_jobs(request: Request, kind: str | None = None) -> dict:
    return {"jobs": [j.model_dump() for j in _manager(request).list_jobs(kind=kind)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    return job.model_dump()


@router.get("/jobs/{job_id}/events")
def job_events(job_id: str, request: Request, since: int = 0) -> dict:
    manager = _manager(request)
    if manager.get(job_id) is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    return {"events": [e.model_dump() for e in manager.events_since(job_id, since)]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    job = _manager(request).cancel(job_id)
    if job is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    return job.model_dump()


@router.get("/jobs/{job_id}/preflight")
def job_preflight(
    job_id: str, request: Request,
    mode: str | None = None, model: str | None = None,
    effort: str | None = None, budget: float | None = None,
    force_assist: bool = False,
) -> dict:
    """Server-side cost ESTIMATE from the dry-run job's coverage (wizard
    step d). The Run button stays disabled until this was viewed."""
    config = _config(request)
    manager = _manager(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    if job.kind != "run" or not job.params.get("dry_run"):
        raise HTTPException(400, detail="preflight estimates require a dry-run job")
    try:
        estimate = preflight_service.estimate_from_dry_run(
            manager, job_id, config,
            mode=mode, manual_model=model, manual_effort=effort, budget_usd=budget,
            force_assist=force_assist,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return estimate.model_dump()


@router.get("/jobs/{job_id}/selection")
def job_selection(job_id: str, request: Request) -> dict:
    """Per-slot document selection for the wizard's 'Confirm documents' step:
    the auto-selected file, its alternatives and any learned override in
    effect — built from the SAME locate()+peek-verifier the run uses."""
    config = _config(request)
    manager = _manager(request)
    if manager.get(job_id) is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    try:
        selection = selection_service.build_selection(manager, job_id, config)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return selection.model_dump()


@router.get("/jobs/{job_id}/selection/slot")
def job_selection_slot(
    job_id: str, client: str, deal: str, request: Request,
    period: str | None = None, doc_type: str | None = None,
) -> dict:
    """Re-resolve ONE (client, deal, period, doc_type) slot for the
    Confirm-documents table. Used after a swap/override so the table refreshes
    just the affected row — no re-running locate()+peek-verify for every slot in
    scope. `period`/`doc_type` default to the run's first values."""
    config = _config(request)
    manager = _manager(request)
    if manager.get(job_id) is None:
        raise HTTPException(404, detail=f"unknown job {job_id!r}")
    try:
        slot = selection_service.build_single_slot(
            manager, job_id, config, client, deal, period=period, doc_type=doc_type
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return slot.model_dump()


@router.websocket("/ws/jobs/{job_id}")
async def job_events_ws(websocket: WebSocket, job_id: str) -> None:
    """Replay persisted events from ?since=<seq>, then stream live ones.
    Refresh-safe: reconnecting picks up exactly where the page left off."""
    manager: JobManager = websocket.app.state.jobs
    if manager.get(job_id) is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    since = int(websocket.query_params.get("since", 0))
    queue = manager.subscribe(job_id)
    try:
        for event in manager.events_since(job_id, since):
            await websocket.send_json(event.model_dump())
            since = event.seq
        while True:
            event = await queue.get()
            if event.seq <= since:
                continue
            await websocket.send_json(event.model_dump())
            job = manager.get(job_id)
            if event.type == "done" and job is not None and job.status not in ("queued", "running", "cancelling"):
                break
        await websocket.close()
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(job_id, queue)


# ---------------------------------------------------------------------------
# multi-search (Phase C)
# ---------------------------------------------------------------------------


@router.post("/multi-search/selection")
def multi_search_selection(body: MultiSearchSelectionRequest, request: Request) -> dict:
    """Firm-grouped 'is it finding the right docs?' preview (synchronous). Runs
    each firm's deal discovery as configured (llm_assist), then resolves every
    (deal x doc_type) slot through the SAME locate()+peek-verifier the run uses.
    READ-ONLY on the learning table: added_folders / removed_deals are NOT
    recorded here; they persist (deduped) ONLY when the run is launched
    (expand_slots). enhanced_period_check surfaces misfiled documents per firm."""
    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        return multi_search_service.build_multi_selection(conn, config, body)
    finally:
        conn.close()


@router.post("/multi-search/run")
def multi_search_run(body: MultiSearchRunRequest, request: Request) -> dict:
    """Launch the firm-level batch as ONE pipeline run (one workbook for the
    whole batch); events are laned by firm. Refused while another pipeline run
    is active (single-slot guard)."""
    try:
        job = _manager(request).start_multi_run(body)
    except JobConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"job": job.model_dump()}


# ---------------------------------------------------------------------------
# locator review
# ---------------------------------------------------------------------------


@router.post("/locator/locate")
def locator_locate(body: LocateRequest, request: Request) -> dict:
    """Fresh candidate table (full score breakdowns) — the same locate()
    the pipeline calls."""
    from pv_extractor.locator.locate import locate

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        from pv_extractor.models import LocateQuery

        try:
            result = locate(conn, config, LocateQuery(
                client=body.client, deal=body.deal, period=body.period, doc_type=body.doc_type,
            ))
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
    finally:
        conn.close()
    return result.model_dump(mode="json")


def _resolve_override_key(conn, config: Config, body: OverrideRequest) -> tuple[str, str, object]:
    """Resolve (client, deal, as-of) exactly the way locate() does, so the
    recorded override key matches the lookup at run time."""
    from pv_extractor.indexer.periods import resolve_target_period
    from pv_extractor.locator.aliases import load_aliases, resolve_name

    aliases = load_aliases(config.aliases_path_resolved())
    client, _, _ = resolve_name(
        body.client, db.distinct_clients(conn), aliases.clients, config.locator.fuzzy_match_threshold
    )
    if client is None:
        raise HTTPException(400, detail=f"client {body.client!r} does not resolve against the index")
    deal, _, _ = resolve_name(
        body.deal, db.deals_for_client(conn, client), aliases.deals, config.locator.fuzzy_match_threshold
    )
    if deal is None:
        raise HTTPException(400, detail=f"deal {body.deal!r} does not resolve under client {client!r}")
    if not (body.period or "").strip():
        raise HTTPException(
            400,
            detail="no period was supplied for this document — pick a period (e.g. Q1 2026) "
            "before swapping or adding a file.",
        )
    target = resolve_target_period(body.period, config.client_period_style(client))
    if target is None:
        raise HTTPException(
            400,
            detail=f"period {body.period!r} does not resolve to an as-of date "
            "(try a quarter like 'Q1 2026', a month like 'January 2026', or an ISO date '2026-03-31').",
        )
    return client, deal, target


@router.post("/locator/override")
def record_locator_override(body: OverrideRequest, request: Request) -> dict:
    """'Pick this one': records the analyst's choice so the same pick is
    automatic next run (consumed by locate(); still peek-verified)."""
    from pv_extractor.locator import overrides

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        client, deal, target = _resolve_override_key(conn, config, body)
        if overrides.indexed_record_for_path(conn, body.file_path) is None:
            raise HTTPException(400, detail=f"{body.file_path!r} is not in the file index")
        overrides.record_override(
            conn, client=client, deal=deal, as_of_date=target,
            doc_type=body.doc_type.value, file_path=body.file_path, note=body.note,
        )
        return {"recorded": {
            "client": client, "deal": deal, "as_of_date": target.isoformat(),
            "doc_type": body.doc_type.value, "file_path": body.file_path,
        }}
    finally:
        conn.close()


@router.post("/locator/source-docs")
def record_source_docs(body: SourceDocsRequest, request: Request) -> dict:
    """Record the SET of documents for one investment (multi-doc merge):
    file_paths[0] becomes the primary (override = the row's identity); the rest
    are extra sources merged into the same row by best confidence per field.
    A 0/1-element list clears the extras. Every path must be indexed."""
    from pv_extractor.locator import overrides

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        client, deal, target = _resolve_override_key(conn, config, body)
        for path in body.file_paths:
            if overrides.indexed_record_for_path(conn, path) is None:
                raise HTTPException(400, detail=f"{path!r} is not in the file index")
        primary = body.file_paths[0] if body.file_paths else None
        extras = body.file_paths[1:]
        if primary is not None:
            overrides.record_override(
                conn, client=client, deal=deal, as_of_date=target,
                doc_type=body.doc_type.value, file_path=primary,
                note="primary of a multi-document selection",
            )
        overrides.set_extra_docs(
            conn, client=client, deal=deal, as_of_date=target,
            doc_type=body.doc_type.value, file_paths=extras,
        )
        return {"recorded": {
            "client": client, "deal": deal, "as_of_date": target.isoformat(),
            "doc_type": body.doc_type.value, "primary": primary, "extra_docs": extras,
        }}
    finally:
        conn.close()


@router.post("/locator/verify-file")
def verify_locator_file(body: VerifyFileRequest, request: Request) -> dict:
    """Peek-verify an analyst-chosen file against a (client, deal, period)
    slot before it is recorded as an override (the 'Add a missed file' /
    swap-to-arbitrary preview). Surfaces the verdict so the UI can warn when
    a pick would not survive the run-time peek-verifier, plus whether the
    file is even in the index (overrides require an indexed target)."""
    from pv_extractor.locator import overrides
    from pv_extractor.locator.aliases import expansions_for, load_aliases
    from pv_extractor.locator.verify import verify_candidate

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        client, deal, target = _resolve_override_key(conn, config, body)
        indexed = overrides.indexed_record_for_path(conn, body.file_path) is not None
        aliases = load_aliases(config.aliases_path_resolved())
        from pv_extractor.models import LocateQuery

        verdict = verify_candidate(
            body.file_path, config,
            query=LocateQuery(
                client=client, deal=deal, period=body.period,
                doc_type=body.doc_type, as_of_date=target,
            ),
            expected_names=expansions_for(deal, aliases.deals),
        )
    finally:
        conn.close()
    return {
        "client": client, "deal": deal, "as_of_date": target.isoformat(),
        "file_path": body.file_path, "indexed": indexed,
        "status": verdict.status.value, "doc_class": verdict.doc_class.value,
        "reason": verdict.reason,
        "asof_date": verdict.asof_date.isoformat() if verdict.asof_date else None,
        "asset_names": verdict.asset_names,
        "confidence": round(verdict.confidence, 4),
        "would_pass": verdict.status.value != "REJECTED",
    }


@router.get("/locator/overrides")
def list_locator_overrides(request: Request) -> dict:
    from pv_extractor.locator import overrides

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        return {"overrides": overrides.list_overrides(conn)}
    finally:
        conn.close()


@router.delete("/locator/overrides")
def delete_locator_override(
    request: Request, client: str, deal: str, as_of_date: str, doc_type: str
) -> dict:
    from datetime import date

    from pv_extractor.locator import overrides

    config = _config(request)
    conn = db.open_db(config.db_path, config.pv_root)
    try:
        removed = overrides.delete_override(
            conn, client=client, deal=deal,
            as_of_date=date.fromisoformat(as_of_date), doc_type=doc_type,
        )
    finally:
        conn.close()
    return {"removed": removed}


@router.post("/locator/open-folder")
def open_containing_folder(body: OpenFolderRequest, request: Request) -> dict:
    """Open the containing folder in the OS file manager (read-only action;
    the path must live under pv_root or output_dir)."""
    config = _config(request)
    target = Path(body.path)
    folder = target.parent if target.suffix else target
    allowed = is_under_pv_root(str(folder), config.pv_root) or is_under_pv_root(
        str(folder), str(config.output_dir)
    )
    if not allowed:
        raise HTTPException(400, detail="path is outside pv_root and output_dir")
    try:
        if platform.system() == "Windows":
            os.startfile(str(folder))  # noqa: S606 — explicit analyst action
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        raise HTTPException(500, detail=f"could not open folder: {exc}") from exc
    return {"opened": str(folder)}


@router.post("/locator/open-file")
def open_document(body: OpenFolderRequest, request: Request) -> dict:
    """Open a document in its OS default application for inspection (explicit
    analyst action; the file must live under pv_root). Read-only — opening a
    file never writes the share."""
    config = _config(request)
    target = str(Path(body.path))
    if not is_under_pv_root(target, config.pv_root):
        raise HTTPException(400, detail="path is outside pv_root")
    if not Path(target).is_file():
        raise HTTPException(404, detail="file not found")
    try:
        if platform.system() == "Windows":
            os.startfile(target)  # noqa: S606 — explicit analyst action
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError as exc:
        raise HTTPException(500, detail=f"could not open file: {exc}") from exc
    return {"opened": target}


@router.get("/locator/preview")
def candidate_preview(request: Request, file_path: str, page: int = 1) -> FileResponse:
    """Render a candidate document page (default page 1) to PNG so the analyst
    can eyeball it in Confirm documents before picking. PDF + pv_root only."""
    config = _config(request)
    try:
        path = evidence_service.render_file_page(config, file_path, page)
    except evidence_service.EvidenceError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/png")
