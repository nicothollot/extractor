"""Claude Code CLI startup self-checks (read-only).

At startup the tool optionally probes the local ``claude`` CLI so operators
learn immediately whether it is installed, what version it is, and whether
it is authenticated. This module never imports ``anthropic``, never needs
ANTHROPIC_API_KEY, and never sends memo data anywhere — the only subprocess
calls are ``claude --version`` / ``claude auth status`` (and ``claude
update`` when configured). Every run appends one JSON line to
``<output_dir>/logs/startup_checks.jsonl``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from pv_extractor.config import Config
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.logging_setup import log_event

logger = logging.getLogger(__name__)

_INSTALL_REMEDIATION = (
    "Claude Code CLI not found on PATH. Install it with "
    "`npm install -g @anthropic-ai/claude-code` (requires Node.js from "
    "https://nodejs.org) or see https://claude.com/claude-code. "
    "If a different executable name/path is used, set claude_code.command in "
    "config.yaml; set claude_code.allow_cli_usage: false to silence this check."
)


class StartupCheckResult(BaseModel):
    """Outcome of one startup check."""

    check: str
    ok: bool
    detail: str


class StartupSnapshot(BaseModel):
    """Everything learned about the Claude Code CLI during one startup."""

    checked_at: datetime
    claude_path: str | None
    version: str | None
    auth_status: str | None
    updated: bool
    results: list[StartupCheckResult]


def _run_check(check: str, argv: list[str], timeout: int) -> tuple[StartupCheckResult, str | None]:
    """Run one read-only CLI subcommand. Failures (non-zero exit, timeout,
    launch error) are recorded in the result, never raised. Returns the
    result plus the stripped stdout on success (None otherwise)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return StartupCheckResult(check=check, ok=False, detail=f"timed out after {timeout}s"), None
    except OSError as exc:
        return StartupCheckResult(check=check, ok=False, detail=f"failed to launch: {exc}"), None
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0:
        return StartupCheckResult(check=check, ok=False, detail=f"exit {proc.returncode}: {output}"), None
    return StartupCheckResult(check=check, ok=True, detail=output), output


def _append_snapshot(snapshot: StartupSnapshot, config: Config) -> Path:
    """Append the snapshot as one JSON line to logs/startup_checks.jsonl."""
    path = Path(config.output_dir) / "logs" / "startup_checks.jsonl"
    line = json.dumps(snapshot.model_dump(), ensure_ascii=False, default=str)
    with guarded_open_write(path, config.pv_root, mode="a") as fh:
        fh.write(line + "\n")
    return path


def run_startup_checks(config: Config) -> StartupSnapshot:
    """Probe the Claude Code CLI per config.claude_code and persist the
    snapshot. Never raises on CLI absence/failure/timeout; never runs any
    subprocess when allow_cli_usage is false."""
    cc = config.claude_code
    checked_at = datetime.now(timezone.utc)

    if not cc.allow_cli_usage:
        snapshot = StartupSnapshot(
            checked_at=checked_at,
            claude_path=None,
            version=None,
            auth_status=None,
            updated=False,
            results=[StartupCheckResult(check="claude_cli", ok=True, detail="disabled by config")],
        )
        path = _append_snapshot(snapshot, config)
        log_event(logger, "claude_code checks skipped", reason="disabled by config", snapshot_path=str(path))
        return snapshot

    claude_path = shutil.which(cc.command)
    if claude_path is None:
        snapshot = StartupSnapshot(
            checked_at=checked_at,
            claude_path=None,
            version=None,
            auth_status=None,
            updated=False,
            results=[StartupCheckResult(check="claude_cli", ok=False, detail=_INSTALL_REMEDIATION)],
        )
        path = _append_snapshot(snapshot, config)
        log_event(logger, "claude_code CLI not found", command=cc.command, snapshot_path=str(path))
        return snapshot

    results = [StartupCheckResult(check="claude_cli", ok=True, detail=f"found at {claude_path}")]
    timeout = cc.default_timeout_seconds
    base = [cc.command, *cc.command_args]  # command_args = e.g. the Windows -> WSL bridge

    version_result, version = _run_check("version", [*base, "--version"], timeout)
    results.append(version_result)

    auth_result, auth_status = _run_check("auth_status", [*base, "auth", "status"], timeout)
    if not auth_result.ok:
        from pv_extractor.llm.claude_code_client import LOGIN_REMEDIATION

        auth_result.detail = f"{auth_result.detail} — {LOGIN_REMEDIATION}"
    results.append(auth_result)

    updated = False
    if cc.auto_update_on_start:
        update_result, _ = _run_check("update", [*base, "update"], timeout)
        results.append(update_result)
        updated = update_result.ok

    snapshot = StartupSnapshot(
        checked_at=checked_at,
        claude_path=claude_path,
        version=version,
        auth_status=auth_status,
        updated=updated,
        results=results,
    )
    path = _append_snapshot(snapshot, config)
    log_event(
        logger,
        "claude_code startup checks complete",
        claude_path=claude_path,
        version=version,
        auth_ok=auth_result.ok,
        updated=updated,
        snapshot_path=str(path),
    )
    return snapshot
