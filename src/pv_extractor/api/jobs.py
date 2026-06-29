"""Background job manager for the GUI.

Every long operation is a job with an id, persisted in
output_dir/gui/jobs.sqlite (jobs + events tables) so the app is
refresh-safe: reopening the browser reattaches to a running job by
replaying its event log and subscribing to the live stream.

Pipeline jobs (kind="run") execute pv_extractor.run.run — the exact
function the CLI calls — on a dedicated single-slot executor, so at most
one pipeline run is active at a time (the PV share and the local CPU are
both contended resources). Progress arrives through two channels:

  * the RunControl seam (structured stage events per memo lane), and
  * a logging bridge that forwards pv_extractor.* JSONL log records as
    "log" events (the GUI log tail) and turns escalation milestones into
    "cost_tick" events by re-reading the run's cost ledger.

Events carry identifiers and counters only — never memo content, client
document text, or page payload (hard rule: INFO-level redaction applies to
the GUI event stream too; client/deal names are workbook metadata, not
document content).

Cancellation is graceful by construction: it sets the RunControl cancel
event, the in-flight memo finishes, the rest are marked DEFERRED, and the
run still writes its workbook/audits for everything completed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pv_extractor.api.schemas import (
    JobEvent,
    JobInfo,
    LlmRunOptions,
    MultiSearchRunRequest,
    RunRequest,
)
from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.io_guard import assert_write_allowed, guarded_open_write
from pv_extractor.llm.costs import LEDGER_FILENAME, read_ledger, summarize_ledger
from pv_extractor.run import RunControl, RunReport, run as run_pipeline

logger = logging.getLogger(__name__)

_PIPELINE_KINDS = ("multi_run", "run")
_ACTIVE_STATUSES = ("queued", "running", "cancelling")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _JobLogBridge(logging.Handler):
    """Forwards pv_extractor log records emitted during a pipeline job to
    the job's event stream (log tail + cost ticks). Attached for the
    duration of one job only; pipeline jobs are serialized so records
    cannot belong to another run."""

    def __init__(self, manager: "JobManager", job_id: str, run_dir: Path | None) -> None:
        super().__init__(level=logging.INFO)
        self.manager = manager
        self.job_id = job_id
        self.run_dir = run_dir

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            fields = getattr(record, "extra_fields", None)
            payload = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if isinstance(fields, dict):
                payload.update({k: v for k, v in fields.items() if _json_safe(v)})
            self.manager.emit_event(self.job_id, "log", payload)
            if (
                record.name == "pv_extractor.llm.escalate"
                and record.getMessage() in ("memo escalation finished", "llm escalation complete")
                and self.run_dir is not None
            ):
                ledger_path = self.run_dir / "llm" / LEDGER_FILENAME
                if ledger_path.exists():
                    summary = summarize_ledger(read_ledger(ledger_path))
                    self.manager.emit_event(self.job_id, "cost_tick", summary)
        except Exception:  # noqa: BLE001 — the bridge must never break the run
            pass


def _json_safe(value: object) -> bool:
    try:
        json.dumps(value, default=str)
        return True
    except (TypeError, ValueError):
        return False


def _safe_text(value: object, limit: int = 800) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _stat_token(path: str | Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        p = Path(path)
        st = p.stat()
        return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    except OSError:
        return {"path": str(path), "missing": True}


def _pipeline_fingerprint(kind: str, params: dict, config: Config) -> str:
    payload = {
        "kind": kind,
        "params": params,
        "inputs": {
            "db": _stat_token(config.db_path),
            "aliases": _stat_token(config.aliases_path_resolved()),
            "rules": _stat_token(config.validation.rules_path),
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_event_context(event_type: str, fields: dict) -> dict[str, object]:
    allowed = {
        "run_id", "stage", "status", "client", "deal", "group", "file_name",
        "memo_id", "memos", "attempts", "cache_hits", "deferred",
        "total_cost_usd", "cost_source",
    }
    context: dict[str, object] = {"event": event_type}
    for key in allowed:
        value = fields.get(key)
        if value is not None and _json_safe(value):
            context[key] = value
    return context


def _effective_llm_config(base: Config, llm: LlmRunOptions) -> tuple[Config, dict[str, object]]:
    """Job-local config/options for backward-compatible LLM launch."""
    config = base.model_copy(deep=True)
    single = llm.single_model or {}
    if isinstance(single, dict):
        provider = single.get("provider")
        if provider:
            config.llm.provider = str(provider)
            config.llm.single_model_provider = str(provider)
        if single.get("model"):
            config.llm.single_model_model = str(single["model"])
        if single.get("effort"):
            config.llm.single_model_effort = str(single["effort"])
    if llm.repair_policy:
        config.llm.candidate_arbitration.repair_policy = llm.repair_policy
    mode = llm.routing_mode or llm.mode
    model = llm.model or (single.get("model") if isinstance(single, dict) else None)
    effort = llm.effort or (single.get("effort") if isinstance(single, dict) else None)
    return config, {"mode": mode, "model": model, "effort": effort}


def _exception_diagnostic(exc: Exception, *, stage: str, context: dict[str, object] | None = None) -> dict:
    summary = f"{type(exc).__name__}: {_safe_text(exc)}"
    return {
        "summary": summary,
        "exception_type": type(exc).__name__,
        "stage": stage,
        "context": context or {},
    }


class JobManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db_path = Path(config.output_dir) / "gui" / "jobs.sqlite"
        assert_write_allowed(self.db_path, config.pv_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db_lock = threading.RLock()
        self._init_schema()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners: dict[str, set[asyncio.Queue]] = {}
        self._listeners_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        # Live BudgetTracker per active run (set the moment the LLM pass builds
        # it) so a paused run's budget can be resolved by the API. Symmetric to
        # _cancel_events; popped in the run's finally.
        self._budget_trackers: dict[str, object] = {}
        # One slot: at most one pipeline run at a time (share + CPU contention).
        self._pipeline_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pv-gui-run")
        self._utility_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pv-gui-util")
        self._mark_stale_jobs()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._db_lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    run_id TEXT,
                    params_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    diagnostic_json TEXT,
                    fingerprint TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, seq)
                );
                """
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "diagnostic_json" not in cols:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN diagnostic_json TEXT")
            if "fingerprint" not in cols:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN fingerprint TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs (fingerprint)")
            self._conn.commit()

    def _mark_stale_jobs(self) -> None:
        """Jobs left 'running' by a previous process can no longer finish."""
        diagnostic = {
            "summary": "Job interrupted because the GUI server restarted before it finished.",
            "exception_type": "Interrupted",
            "stage": "startup_recovery",
            "context": {},
        }
        with self._db_lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ?, "
                "error = COALESCE(error, ?), diagnostic_json = COALESCE(diagnostic_json, ?) "
                "WHERE status IN ('queued', 'running', 'cancelling')",
                (_now(), diagnostic["summary"], json.dumps(diagnostic, ensure_ascii=False)),
            )
            self._conn.commit()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _update(self, job_id: str, **cols: object) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._db_lock:
            self._conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*cols.values(), job_id))
            self._conn.commit()

    def _job_from_row_locked(self, row) -> JobInfo:
        seq_row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM job_events WHERE job_id = ?", (row[0],)
        ).fetchone()
        return JobInfo(
            id=row[0], kind=row[1], status=row[2], created_at=row[3], started_at=row[4],
            finished_at=row[5], run_id=row[6], params=json.loads(row[7]),
            result=json.loads(row[8]) if row[8] else None, error=row[9],
            diagnostics=json.loads(row[10]) if row[10] else None,
            last_seq=seq_row[0],
        )

    def get(self, job_id: str) -> JobInfo | None:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT id, kind, status, created_at, started_at, finished_at, run_id, "
                "params_json, result_json, error, diagnostic_json FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return self._job_from_row_locked(row)

    def list_jobs(self, *, kind: str | None = None, limit: int = 50) -> list[JobInfo]:
        sql = (
            "SELECT id FROM jobs"
            + (" WHERE kind = ?" if kind else "")
            + " ORDER BY created_at DESC LIMIT ?"
        )
        args = (kind, limit) if kind else (limit,)
        with self._db_lock:
            ids = [r[0] for r in self._conn.execute(sql, args).fetchall()]
        return [info for job_id in ids if (info := self.get(job_id)) is not None]

    def events_since(self, job_id: str, since: int = 0, limit: int = 2000) -> list[JobEvent]:
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT seq, ts, type, payload_json FROM job_events "
                "WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (job_id, since, limit),
            ).fetchall()
        return [JobEvent(seq=r[0], ts=r[1], type=r[2], payload=json.loads(r[3])) for r in rows]

    # ------------------------------------------------------------------
    # event stream
    # ------------------------------------------------------------------

    def emit_event(self, job_id: str, event_type: str, payload: dict) -> None:
        """Persist + broadcast one event. Callable from any thread."""
        entry = json.dumps(payload, ensure_ascii=False, default=str)
        with self._db_lock:
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (job_id,)
            ).fetchone()
            seq = seq_row[0]
            self._conn.execute(
                "INSERT INTO job_events (job_id, seq, ts, type, payload_json) VALUES (?, ?, ?, ?, ?)",
                (job_id, seq, _now(), event_type, entry),
            )
            self._conn.commit()
        event = JobEvent(seq=seq, ts=_now(), type=event_type, payload=json.loads(entry))
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._listeners_lock:
            queues = list(self._listeners.get(job_id, ()))
        for queue in queues:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._listeners_lock:
            self._listeners.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        with self._listeners_lock:
            self._listeners.get(job_id, set()).discard(queue)

    # ------------------------------------------------------------------
    # job lifecycle
    # ------------------------------------------------------------------

    def active_pipeline_job(self) -> JobInfo | None:
        with self._db_lock:
            row = self._conn.execute(
                f"SELECT id, kind, status, created_at, started_at, finished_at, run_id, "
                f"params_json, result_json, error, diagnostic_json FROM jobs "
                f"WHERE kind IN ({', '.join('?' for _ in _PIPELINE_KINDS)}) "
                "AND status IN ('queued', 'running', 'cancelling') "
                "ORDER BY created_at DESC LIMIT 1",
                _PIPELINE_KINDS,
            ).fetchone()
            return self._job_from_row_locked(row) if row else None

    def _create(self, kind: str, params: dict, *, fingerprint: str | None = None) -> JobInfo:
        job_id = f"JOB_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, status, created_at, params_json, fingerprint) "
                "VALUES (?, ?, 'queued', ?, ?, ?)",
                (job_id, kind, _now(), json.dumps(params, ensure_ascii=False, default=str), fingerprint),
            )
            self._conn.commit()
        info = self.get(job_id)
        assert info is not None
        return info

    def _reserve_pipeline(
        self,
        kind: str,
        params: dict,
        *,
        fingerprint: str | None = None,
        reuse_completed: bool = False,
    ) -> tuple[JobInfo, bool]:
        """Atomically reserve the single pipeline slot.

        Returns (job, created). For idempotent dry-run preflights, an identical
        active or completed job is returned with created=False instead of
        enqueueing hidden duplicate work.
        """
        job_id = f"JOB_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        params_json = json.dumps(params, ensure_ascii=False, default=str)
        select_cols = (
            "id, kind, status, created_at, started_at, finished_at, run_id, "
            "params_json, result_json, error, diagnostic_json, fingerprint"
        )
        with self._db_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active = self._conn.execute(
                    f"SELECT {select_cols} FROM jobs "
                    f"WHERE kind IN ({', '.join('?' for _ in _PIPELINE_KINDS)}) "
                    "AND status IN ('queued', 'running', 'cancelling') "
                    "ORDER BY created_at DESC LIMIT 1",
                    _PIPELINE_KINDS,
                ).fetchone()
                if active is not None:
                    active_job = self._job_from_row_locked(active)
                    if fingerprint and active[11] == fingerprint:
                        self._conn.commit()
                        return active_job, False
                    self._conn.rollback()
                    raise JobConflict(active_job)

                if fingerprint and reuse_completed:
                    previous = self._conn.execute(
                        f"SELECT {select_cols} FROM jobs "
                        "WHERE kind = ? AND fingerprint = ? AND status = 'completed' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (kind, fingerprint),
                    ).fetchone()
                    if previous is not None:
                        job = self._job_from_row_locked(previous)
                        self._conn.commit()
                        return job, False

                self._conn.execute(
                    "INSERT INTO jobs (id, kind, status, created_at, params_json, fingerprint) "
                    "VALUES (?, ?, 'queued', ?, ?, ?)",
                    (job_id, kind, _now(), params_json, fingerprint),
                )
                row = self._conn.execute(f"SELECT {select_cols} FROM jobs WHERE id = ?", (job_id,)).fetchone()
                assert row is not None
                job = self._job_from_row_locked(row)
                self._conn.commit()
                return job, True
            except JobConflict:
                raise
            except Exception:
                self._conn.rollback()
                raise

    def start_run(self, request: RunRequest) -> JobInfo:
        """Queue one pipeline run; refuses while another is active."""
        params = request.model_dump(mode="json")
        fingerprint = _pipeline_fingerprint("run", params, self.config) if request.dry_run else None
        job, created = self._reserve_pipeline(
            "run", params, fingerprint=fingerprint, reuse_completed=request.dry_run
        )
        if not created:
            return job
        cancel = threading.Event()
        self._cancel_events[job.id] = cancel
        self._pipeline_pool.submit(self._execute_run, job.id, request, cancel)
        return job

    def start_multi_run(self, request: MultiSearchRunRequest) -> JobInfo:
        """Queue one firm-level batch run (Phase C); refuses while another
        pipeline run is active (same single-slot guard as start_run). The whole
        batch is ONE pipeline run / ONE workbook, with events laned by firm."""
        params = request.model_dump(mode="json")
        fingerprint = _pipeline_fingerprint("multi_run", params, self.config) if request.dry_run else None
        job, created = self._reserve_pipeline(
            "multi_run", params, fingerprint=fingerprint, reuse_completed=request.dry_run
        )
        if not created:
            return job
        cancel = threading.Event()
        self._cancel_events[job.id] = cancel
        self._pipeline_pool.submit(self._execute_multi_run, job.id, request, cancel)
        return job

    def start_utility(self, kind: str, params: dict, fn) -> JobInfo:
        """Non-pipeline job (claude update, dependency install, index scan).
        fn() -> dict result; an fn that accepts one argument instead receives
        emit(event_type, payload) for live progress events; an fn that accepts
        two also receives should_stop() — a cooperative pause flag set by
        cancel() (the scan job uses it to commit what it has and stop)."""
        job = self._create(kind, params)
        cancel = threading.Event()
        self._cancel_events[job.id] = cancel
        self._utility_pool.submit(self._execute_utility, job.id, fn, cancel)
        return job

    def cancel(self, job_id: str) -> JobInfo | None:
        job = self.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return job
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
            # Wake any agents parked at a budget pause so their reserve() raises
            # and the run unwinds (the cancel_event also makes new reserves raise).
            tracker = self._budget_trackers.get(job_id)
            if tracker is not None:
                try:
                    tracker.cancel()
                except Exception:  # noqa: BLE001 — cancel must never raise
                    logger.exception("force-cancel: budget tracker cancel failed for %s", job_id)
            # FORCE cancel: kill every running provider subprocess (and its WSL/
            # CLI children) immediately — don't wait for in-flight agents to
            # finish. The cooperative flag above still stops new work.
            if job.kind in ("run", "multi_run"):
                try:
                    from pv_extractor.llm.claude_code_client import abort_all_calls

                    abort_all_calls()
                except Exception:  # noqa: BLE001 — cancel must never raise
                    logger.exception("force-cancel: abort_all_calls failed for %s", job_id)
            self._update(job_id, status="cancelling")
            self.emit_event(job_id, "cancel_requested", {})
        else:
            self._update(job_id, status="cancelled", finished_at=_now())
        return self.get(job_id)

    def resolve_budget(
        self, job_id: str, action: str, amount_usd: float | None = None
    ) -> JobInfo | None:
        """Resolve a paused run's budget (GUI). raise -> lift the cap and resume;
        remove -> uncap and resume; cancel -> force-cancel the run."""
        job = self.get(job_id)
        if job is None:
            return None
        if action == "cancel":
            return self.cancel(job_id)
        tracker = self._budget_trackers.get(job_id)
        if tracker is None:
            # No active LLM pass / not paused — nothing to resolve.
            return job
        if action == "raise":
            if amount_usd is None:
                raise ValueError("raise requires amount_usd")
            tracker.raise_budget(float(amount_usd))
            self.emit_event(job_id, "budget_updated", {
                "budget_usd": float(amount_usd), "committed_usd": tracker.committed_usd,
            })
        elif action == "remove":
            tracker.remove_cap()
            self.emit_event(job_id, "budget_updated", {
                "budget_usd": None, "committed_usd": tracker.committed_usd,
            })
        else:
            raise ValueError(f"unknown budget action {action!r}")
        self.emit_event(job_id, "budget_resumed", {"committed_usd": tracker.committed_usd})
        return job

    def _budget_control(self, job_id: str):
        """Build (on_pause, budget_sink) callbacks for a run. on_pause emits a
        budget_paused event once the LLM budget is first reached; budget_sink
        registers the live tracker so resolve_budget()/cancel() can reach it."""

        def on_pause(committed_usd: float, budget_usd: float | None) -> None:
            self.emit_event(job_id, "budget_paused", {
                "run_id": self.get(job_id).run_id if self.get(job_id) else None,
                "committed_usd": committed_usd,
                "budget_usd": budget_usd,
            })

        def budget_sink(tracker) -> None:
            self._budget_trackers[job_id] = tracker

        return on_pause, budget_sink

    # ------------------------------------------------------------------
    # execution (worker threads)
    # ------------------------------------------------------------------

    def _execute_utility(self, job_id: str, fn, cancel: threading.Event) -> None:
        self._update(job_id, status="running", started_at=_now())
        try:
            emit = lambda event_type, payload: self.emit_event(job_id, event_type, payload)  # noqa: E731
            n_params = len(inspect.signature(fn).parameters)
            if n_params >= 2:
                result = fn(emit, cancel.is_set)
            elif n_params == 1:
                result = fn(emit)
            else:
                result = fn()
            # A cooperative pause still returns a (partial) result worth keeping.
            status = "cancelled" if cancel.is_set() else "completed"
            self._update(
                job_id, status=status, finished_at=_now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {"status": status})
        except Exception as exc:  # noqa: BLE001 — job isolation
            logger.exception("utility job %s failed", job_id)
            diagnostic = _exception_diagnostic(exc, stage="utility", context={"job_id": job_id})
            self._update(
                job_id, status="failed", finished_at=_now(), error=diagnostic["summary"],
                diagnostic_json=json.dumps(diagnostic, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {
                "status": "failed", "error": diagnostic["summary"], "diagnostics": diagnostic,
            })
        finally:
            self._cancel_events.pop(job_id, None)
            self._budget_trackers.pop(job_id, None)

    def _execute_run(self, job_id: str, request: RunRequest, cancel: threading.Event) -> None:
        from pv_extractor.llm.claude_code_client import reset_abort
        from pv_extractor.llm.escalate import resolve_settings

        reset_abort()  # clear any prior force-cancel before a new run
        self._update(job_id, status="running", started_at=_now())
        now = datetime.now()
        run_id = f"RUN_{now:%Y%m%d_%H%M%S}"
        run_dir = None if request.dry_run else Path(self.config.output_dir) / run_id
        self._update(job_id, run_id=run_id)

        last_context: dict[str, object] = {"job_id": job_id}

        def on_event(event: str, fields: dict) -> None:
            nonlocal last_context
            last_context = _safe_event_context(event, dict(fields))
            self.emit_event(job_id, event, dict(fields))

        pause_cb, budget_sink = self._budget_control(job_id)
        control = RunControl(
            on_event=on_event, cancel_event=cancel,
            budget_pause_cb=pause_cb, budget_sink=budget_sink,
        )
        bridge = _JobLogBridge(self, job_id, run_dir)
        pv_logger = logging.getLogger("pv_extractor")
        pv_logger.addHandler(bridge)
        try:
            llm = request.llm
            run_config, llm_args = _effective_llm_config(self.config, llm)
            settings = resolve_settings(
                run_config,
                no_llm=not llm.enabled,
                mode=llm_args["mode"], model=llm_args["model"], effort=llm_args["effort"],
                budget=llm.budget_usd, force=llm.force_llm,
                force_assist=llm.force_llm_assist, allow_fable=llm.allow_fable,
            )
            exclude = {(slot.client, slot.deal) for slot in request.exclude}
            # Multiple doc types and/or periods -> fan out into per-slot work
            # (one workbook). A single doc type + period stays on the legacy path.
            from pv_extractor.api import run_slots as _rs

            # The wizard has two period inputs — a single `period` and a
            # multi-`periods` selector — and the launch is valid when EITHER is
            # set. When the analyst fills only `periods` (even with one entry),
            # `request.period` is "", so the legacy single-period path below would
            # call locate(period="") and raise "could not resolve period ''".
            # Collapse to the effective single period so `periods=[X], period=""`
            # behaves exactly like `period=X`.
            eff_periods = _rs.effective_periods(request.period, request.periods)
            effective_period = eff_periods[0] if eff_periods else request.period
            req_mode = request.source_mode or ("client" if request.restrict_to_client_sourced else "any")

            if request.direct_files or request.direct_file:
                report = run_pipeline(
                    run_config, scope="deal", period=request.period,
                    doc_type=request.doc_type,
                    template=request.template, dry_run=request.dry_run, force=request.force,
                    now=now, llm_settings=settings, control=control,
                    direct_files=request.direct_files or None,
                    direct_file=request.direct_file,
                    direct_client=request.direct_client, direct_deal=request.direct_deal,
                    field_edits=request.field_edits,
                )
            elif _rs.needs_expansion(request.doc_type, request.doc_types, request.period, request.periods):
                conn = db.open_db(run_config.db_path, run_config.pv_root)
                try:
                    slots = _rs.build_run_slots(
                        conn, run_config,
                        scope=request.scope, client=request.client, deal=request.deal,
                        exclude=exclude, doc_type=request.doc_type, doc_types=request.doc_types,
                        period=request.period, periods=request.periods,
                        source_mode=req_mode,
                    )
                finally:
                    conn.close()
                report = run_pipeline(
                    run_config, scope=request.scope, period=effective_period,
                    template=request.template, dry_run=request.dry_run, force=request.force,
                    now=now, llm_settings=settings, control=control, slots=slots,
                    field_edits=request.field_edits,
                )
            else:
                report = run_pipeline(
                    run_config,
                    scope=request.scope, period=effective_period,
                    client=request.client, deal=request.deal, doc_type=request.doc_type,
                    source_mode=req_mode,
                    template=request.template, dry_run=request.dry_run, force=request.force,
                    now=now, llm_settings=settings, control=control, exclude=exclude,
                    field_edits=request.field_edits,
                )
            result = _report_summary(report, request)
            if not request.dry_run and report.run_dir is not None:
                _write_run_summary(report, request, run_config, cancelled=cancel.is_set())
            status = "cancelled" if cancel.is_set() else "completed"
            self._update(
                job_id, status=status, finished_at=_now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {"status": status})
        except Exception as exc:  # noqa: BLE001 — job isolation
            logger.exception("run job %s failed", job_id)
            diagnostic = _exception_diagnostic(exc, stage="pipeline", context=last_context)
            self._update(
                job_id, status="failed", finished_at=_now(), error=diagnostic["summary"],
                diagnostic_json=json.dumps(diagnostic, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {
                "status": "failed", "error": diagnostic["summary"], "diagnostics": diagnostic,
            })
        finally:
            pv_logger.removeHandler(bridge)
            self._cancel_events.pop(job_id, None)
            self._budget_trackers.pop(job_id, None)

    def _execute_multi_run(
        self, job_id: str, request: MultiSearchRunRequest, cancel: threading.Event
    ) -> None:
        """Firm-level batch run (Phase C): expand the request into RunSlot,
        drive ONE pipeline run over them (slots path), reusing the exact
        event/cancel/cost-bridge machinery as the single-firm run."""
        from pv_extractor.api import multi_search_service
        from pv_extractor.llm.claude_code_client import reset_abort
        from pv_extractor.llm.escalate import resolve_settings

        reset_abort()  # clear any prior force-cancel before a new run
        self._update(job_id, status="running", started_at=_now())
        now = datetime.now()
        run_id = f"RUN_{now:%Y%m%d_%H%M%S}"
        run_dir = None if request.dry_run else Path(self.config.output_dir) / run_id
        self._update(job_id, run_id=run_id)

        last_context: dict[str, object] = {"job_id": job_id}

        def on_event(event: str, fields: dict) -> None:
            nonlocal last_context
            last_context = _safe_event_context(event, dict(fields))
            self.emit_event(job_id, event, dict(fields))

        pause_cb, budget_sink = self._budget_control(job_id)
        control = RunControl(
            on_event=on_event, cancel_event=cancel,
            budget_pause_cb=pause_cb, budget_sink=budget_sink,
        )
        bridge = _JobLogBridge(self, job_id, run_dir)
        pv_logger = logging.getLogger("pv_extractor")
        pv_logger.addHandler(bridge)
        try:
            llm = request.llm
            run_config, llm_args = _effective_llm_config(self.config, llm)
            conn = db.open_db(run_config.db_path, run_config.pv_root)
            try:
                slots = multi_search_service.expand_slots(conn, run_config, request)
            finally:
                conn.close()
            settings = resolve_settings(
                run_config,
                no_llm=not llm.enabled,
                mode=llm_args["mode"], model=llm_args["model"], effort=llm_args["effort"],
                budget=llm.budget_usd, force=llm.force_llm,
                force_assist=llm.force_llm_assist, allow_fable=llm.allow_fable,
            )
            # The slots path ignores scope/period/client/deal/doc_type for
            # pairing (it pairs off `slots`); pass benign placeholders.
            report = run_pipeline(
                run_config,
                scope="all", period="",
                template=request.template, dry_run=request.dry_run, force=request.force,
                now=now, llm_settings=settings, control=control, slots=slots,
                field_edits=getattr(request, "field_edits", None),
            )
            result = _multi_report_summary(report, request, len(slots))
            if not request.dry_run and report.run_dir is not None:
                _write_multi_run_summary(report, request, run_config, len(slots), cancelled=cancel.is_set())
            status = "cancelled" if cancel.is_set() else "completed"
            self._update(
                job_id, status=status, finished_at=_now(),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {"status": status})
        except Exception as exc:  # noqa: BLE001 — job isolation
            logger.exception("multi-run job %s failed", job_id)
            diagnostic = _exception_diagnostic(exc, stage="pipeline", context=last_context)
            self._update(
                job_id, status="failed", finished_at=_now(), error=diagnostic["summary"],
                diagnostic_json=json.dumps(diagnostic, ensure_ascii=False, default=str),
            )
            self.emit_event(job_id, "done", {
                "status": "failed", "error": diagnostic["summary"], "diagnostics": diagnostic,
            })
        finally:
            pv_logger.removeHandler(bridge)
            self._cancel_events.pop(job_id, None)
            self._budget_trackers.pop(job_id, None)


class JobConflict(RuntimeError):
    """A pipeline run is already active (single-slot executor)."""

    def __init__(self, active_job: JobInfo) -> None:
        self.active_job = active_job
        super().__init__("a pipeline run is already active")

    def detail(self) -> dict:
        return {
            "code": "active_pipeline_job",
            "message": str(self),
            "active_job": {
                "id": self.active_job.id,
                "kind": self.active_job.kind,
                "status": self.active_job.status,
                "created_at": self.active_job.created_at,
            },
        }


# ---------------------------------------------------------------------------
# run summaries
# ---------------------------------------------------------------------------


def _report_summary(report: RunReport, request: RunRequest) -> dict:
    coverage_counts: dict[str, int] = {}
    for entry in report.coverage:
        coverage_counts[entry.status] = coverage_counts.get(entry.status, 0) + 1
    llm = report.llm
    # Human-readable digest pieces for the Output browser list/preview: the
    # fund managers and deals that actually produced rows, plus the portfolio
    # companies named on the extracted assets (all from the run's own memos —
    # no re-reading of the share).
    clients = sorted({m.client for m in report.memos if m.client})
    deals = sorted({m.deal for m in report.memos if m.deal})
    companies = sorted({
        str(a.asset_name) for m in report.memos for a in m.assets if a.asset_name
    })
    return {
        "run_id": report.run_id,
        "dry_run": report.dry_run,
        "scope": request.scope,
        "client": request.client,
        "deal": request.deal,
        "period": request.period,
        "doc_type": request.doc_type.value,
        "coverage": [
            {"client": c.client, "deal": c.deal, "status": c.status, "detail": c.detail}
            for c in report.coverage
        ],
        "coverage_counts": coverage_counts,
        "clients": clients,
        "deals": deals,
        "companies": companies,
        "source_files": len([m for m in report.memos if m.file_path]),
        "sources": [
            {"file_name": m.file_name, "file_path": m.file_path, "client": m.client, "deal": m.deal}
            for m in report.memos if m.file_path
        ],
        "memos": len(report.memos),
        "assets": sum(len(m.assets) for m in report.memos),
        "rows_added": report.rows_added,
        "flags_added": report.flags_added,
        "cache_hits": report.cache_hits,
        "qa_counts": report.qa_counts(),
        "duration_minutes": report.duration_minutes,
        "started_at": report.started_at or None,
        "finished_at": report.finished_at or None,
        "workbook_path": str(report.workbook_path) if report.workbook_path else None,
        "llm": {
            "enabled": llm.enabled if llm else False,
            "executed": llm.executed if llm else False,
            "memos_escalated": llm.memos_escalated if llm else 0,
            "memos_deferred": llm.memos_deferred if llm else 0,
            "attempts": llm.attempts if llm else 0,
            "cache_hits": llm.cache_hits if llm else 0,
            "total_cost_usd": llm.total_cost_usd if llm else 0.0,
            "cost_source": (
                ("actual+estimated" if llm.any_actual_costs else "estimated") if llm and llm.executed else None
            ),
            "detail": llm.detail if llm else "",
        },
        "diagnostics": report.diagnostics,
    }


def _write_run_summary(
    report: RunReport, request: RunRequest, config: Config, *, cancelled: bool
) -> None:
    """Persist the dashboard summary next to the run's audits."""
    assert report.run_dir is not None
    summary = _report_summary(report, request)
    summary["cancelled"] = cancelled
    summary["created_at"] = _now()
    summary["source"] = "gui"
    path = Path(report.run_dir) / "run_summary.json"
    with guarded_open_write(path, config.pv_root) as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def _multi_report_summary(
    report: RunReport, request: MultiSearchRunRequest, slot_count: int
) -> dict:
    """Dashboard digest for a firm-level batch run. Reuses the single-run digest
    (a multi-firm run is still ONE workbook / ONE coverage list) and overlays the
    multi-search shape: scope='multi', the requested firms, and slot/firm counts."""
    proxy = RunRequest(
        scope="multi",
        period="",
        template=request.template,
        dry_run=request.dry_run,
        force=request.force,
        llm=request.llm,
    )
    summary = _report_summary(report, proxy)
    summary["multi_search"] = {
        "firms": [f.model_dump() for f in request.firms],
        "firm_count": len(request.firms),
        "slot_count": slot_count,
    }
    return summary


def _write_multi_run_summary(
    report: RunReport, request: MultiSearchRunRequest, config: Config,
    slot_count: int, *, cancelled: bool,
) -> None:
    """Persist the dashboard summary for a firm-level batch run."""
    assert report.run_dir is not None
    summary = _multi_report_summary(report, request, slot_count)
    summary["cancelled"] = cancelled
    summary["created_at"] = _now()
    summary["source"] = "gui"
    path = Path(report.run_dir) / "run_summary.json"
    with guarded_open_write(path, config.pv_root) as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
