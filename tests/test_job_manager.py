from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pv_extractor.api.jobs import JobConflict, JobManager
from pv_extractor.api.schemas import LlmRunOptions, RunRequest
from pv_extractor.config import LlmConfig, load_config
from pv_extractor.run import CoverageEntry, RunReport

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _manager_config(tmp_path: Path, fixture_pv_root: Path):
    config = load_config(PROJECT_ROOT / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = tmp_path / "output"
    config.db_path = tmp_path / "output" / "pv_index.db"
    config.llm = LlmConfig(models_path=config.llm.models_path)
    return config


def _wait_job(manager: JobManager, job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = manager.get(job_id)
        if info is not None and info.status not in ("queued", "running", "cancelling"):
            return info
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def _request(period: str = "Q1 2026") -> RunRequest:
    return RunRequest(
        scope="all",
        period=period,
        dry_run=True,
        llm=LlmRunOptions(enabled=False),
    )


def test_pipeline_reservation_is_atomic_and_preflight_is_idempotent(
    tmp_path, fixture_pv_root, monkeypatch
) -> None:
    config = _manager_config(tmp_path, fixture_pv_root)
    manager = JobManager(config)
    release = threading.Event()

    def fake_pipeline(*args, **kwargs) -> RunReport:
        release.wait(5)
        return RunReport(
            run_id="RUN_FAKE",
            run_dir=None,
            workbook_path=None,
            dry_run=bool(kwargs.get("dry_run")),
            coverage=[CoverageEntry(client="Fixture", deal="One", status="FOUND", detail="ok")],
        )

    monkeypatch.setattr("pv_extractor.api.jobs.run_pipeline", fake_pipeline)

    first = manager.start_run(_request("Q1 2026"))
    same = manager.start_run(_request("Q1 2026"))
    assert same.id == first.id

    with pytest.raises(JobConflict) as conflict:
        manager.start_run(_request("Q2 2026"))
    assert conflict.value.detail()["active_job"]["id"] == first.id

    release.set()
    done = _wait_job(manager, first.id)
    assert done.status == "completed"

    repeated_completed = manager.start_run(_request("Q1 2026"))
    assert repeated_completed.id == first.id
    assert repeated_completed.status == "completed"
    manager._pipeline_pool.shutdown(wait=True, cancel_futures=True)
    manager._utility_pool.shutdown(wait=True, cancel_futures=True)


def test_stale_pipeline_jobs_are_interrupted_and_do_not_block(tmp_path, fixture_pv_root) -> None:
    config = _manager_config(tmp_path, fixture_pv_root)
    manager = JobManager(config)
    stale = manager._create("run", {"dry_run": True})
    manager._update(stale.id, status="running")

    restarted = JobManager(config)
    info = restarted.get(stale.id)
    assert info is not None
    assert info.status == "interrupted"
    assert "server restarted" in (info.error or "")
    assert restarted.active_pipeline_job() is None
    manager._pipeline_pool.shutdown(wait=True, cancel_futures=True)
    manager._utility_pool.shutdown(wait=True, cancel_futures=True)
    restarted._pipeline_pool.shutdown(wait=True, cancel_futures=True)
    restarted._utility_pool.shutdown(wait=True, cancel_futures=True)


def test_failed_job_persists_safe_diagnostics(tmp_path, fixture_pv_root, monkeypatch) -> None:
    config = _manager_config(tmp_path, fixture_pv_root)
    manager = JobManager(config)

    def failing_pipeline(*args, **kwargs) -> RunReport:
        control = kwargs["control"]
        control.emit("stage", client="Fixture", deal="Broken", stage="extract", status="started")
        raise RuntimeError("Codex CLI executable not found: codex")

    monkeypatch.setattr("pv_extractor.api.jobs.run_pipeline", failing_pipeline)

    job = manager.start_run(_request("Q1 2026"))
    failed = _wait_job(manager, job.id)
    assert failed.status == "failed"
    assert "Codex CLI executable not found" in (failed.error or "")
    assert "Traceback" not in (failed.error or "")
    assert failed.diagnostics is not None
    assert failed.diagnostics["exception_type"] == "RuntimeError"
    assert failed.diagnostics["stage"] == "pipeline"
    assert failed.diagnostics["context"]["stage"] == "extract"
    events = manager.events_since(job.id)
    done = [e for e in events if e.type == "done"][-1]
    assert done.payload["diagnostics"]["context"]["deal"] == "Broken"
    manager._pipeline_pool.shutdown(wait=True, cancel_futures=True)
    manager._utility_pool.shutdown(wait=True, cancel_futures=True)
