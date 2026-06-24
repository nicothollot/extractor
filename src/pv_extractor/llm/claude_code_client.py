"""Thin subprocess wrapper around the local ``claude`` binary (D1).

Design rules:
  * Local Claude Code sessions only. The user authenticates once through
    Claude Code (``claude auth login``); every call here reuses that local
    auth. No ``anthropic`` import, no ANTHROPIC_API_KEY — any ANTHROPIC_*
    variable is STRIPPED from the child environment so a stray key can never
    silently take over billing/identity.
  * Plain ``subprocess.run`` for ``claude -p`` print-mode calls (works on
    Windows without ConPTY — print mode is non-interactive by construction).
    Named/background sessions (``--bg``, ``claude agents``, ``--resume``) are
    intentionally NOT used for extraction: one bounded call per memo per tier
    is cheaper and simpler to audit.
  * Redaction: INFO-level logs carry job ids, models, efforts, exit codes,
    durations and token counts — never prompts, page payload, memo contents
    or client names. stderr is logged truncated at DEBUG only.
  * Failures (non-zero exit, timeout, malformed JSON) are returned in the
    ClaudeCodeResult, never raised — the escalation queue decides what a
    failure means (retry tier, NOT_EXTRACTABLE, ...).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from pv_extractor.config import Config
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.logging_setup import log_event
from pv_extractor.models import LlmUsage
from pv_extractor.llm.provider import LlmCliResult, LlmProviderCapabilities
from pv_extractor.llm.response_validation import StructuredResponseError, validate_structured_response

logger = logging.getLogger(__name__)

LOGIN_REMEDIATION = (
    "Claude Code is not authenticated. Run `claude auth login` once in a "
    "terminal (or start `claude` interactively and follow the login prompt); "
    "the PV Extractor reuses that local session — it never asks for an API key."
)

_STDERR_DEBUG_CHARS = 400
_INTERIM_CHARS = 800
# A large single call (e.g. force-assist over ~200 fields) streams nothing until
# the model emits its final tool call, so without this the activity view looks
# dead for minutes. A timer emits an elapsed-time note so the operator always
# knows the call is alive (and roughly how long it has run).
_HEARTBEAT_SECONDS = 15
# Coalesce streamed token deltas: emit at most one thinking/output update this
# often (raw deltas arrive dozens/second — emitting each would flood the UI).
_STREAM_EMIT_SECONDS = 1.0
_THINKING_TAIL_CHARS = 600  # how much of the running thinking text to surface


class ClaudeCodeResult(LlmCliResult):
    """Outcome of one non-interactive extraction call."""

    provider: str = "claude"


def _sanitized_env() -> dict[str, str]:
    """Child environment: everything except ANTHROPIC_* (no API keys — the
    CLI must resolve its own `claude auth login` credentials)."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTHROPIC_")}


def _emit_provider_event(
    event_sink: Callable[[dict[str, object]], None] | None,
    **payload: object,
) -> None:
    if event_sink is None:
        return
    try:
        event_sink(payload)
    except Exception:  # noqa: BLE001 - UI observers must never affect extraction
        logger.debug("provider event sink failed", exc_info=True)


class ClaudeSource(BaseModel):
    """One way to reach the ``claude`` binary, for the Settings picker. The
    GUI persists a choice by writing ``claude_code.command`` +
    ``claude_code.command_args`` (a ClaudeCodeClient is built from those)."""

    id: str                                   # "native" | "wsl"
    label: str
    command: str
    command_args: list[str] = Field(default_factory=list)
    available: bool = False
    version: str | None = None
    detail: str = ""                          # resolved path, or why it's unavailable


# Standard install locations to probe when `claude` is not on the process
# PATH (the GUI is often launched without a login shell, so ~/.local/bin and
# friends are missing even though `claude` works in an interactive terminal).
_NATIVE_FALLBACK_PATHS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "~/bin/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
)


def _probe_version(argv: list[str], timeout: int = 25) -> tuple[bool, str]:
    """Run ``<argv> --version`` with ANTHROPIC_* stripped; (ok, output)."""
    try:
        proc = subprocess.run(
            [*argv, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, env=_sanitized_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode == 0, out


def _resolve_native_claude() -> str | None:
    """The local `claude` binary: PATH first, then the standard install
    locations (PATH is often missing ~/.local/bin under the GUI process)."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for cand in _NATIVE_FALLBACK_PATHS:
        expanded = Path(cand).expanduser()
        if expanded.is_file():
            return str(expanded)
    if os.name == "nt":  # npm shim for a native Windows install
        for env_key in ("APPDATA", "LOCALAPPDATA", "ProgramFiles"):
            base = os.environ.get(env_key)
            if base:
                for shim in ("npm\\claude.cmd", "npm\\claude.exe"):
                    cand_p = Path(base) / shim
                    if cand_p.is_file():
                        return str(cand_p)
    return None


def _wsl_claude_path(wsl_path: str, timeout: int = 45) -> tuple[str | None, str]:
    """Resolve the ABSOLUTE path of ``claude`` inside the default WSL distro.
    ``wsl -e`` skips the login shell, so PATH from .bashrc/.profile is not set
    — try a login+interactive shell `command -v`, then probe known locations."""
    # 1) Ask a login (+interactive) shell to resolve it the way the user's
    #    terminal would. -lic sources both ~/.profile and ~/.bashrc.
    for shell_args in (["bash", "-lic", "command -v claude"],
                       ["bash", "-lc", "command -v claude"]):
        try:
            proc = subprocess.run(
                [wsl_path, "-e", *shell_args], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout, env=_sanitized_env(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, f"WSL probe failed: {exc}"
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return out.splitlines()[-1].strip(), ""  # last line — skip banner noise
    # 2) Fall back to the standard install locations inside WSL.
    probe = 'for p in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /usr/local/bin/claude; do [ -x "$p" ] && { printf %s "$p"; exit 0; }; done'
    try:
        proc = subprocess.run(
            [wsl_path, "-e", "bash", "-lc", probe], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, env=_sanitized_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"WSL probe failed: {exc}"
    out = (proc.stdout or "").strip()
    if out:
        return out.splitlines()[-1].strip(), ""
    err = (proc.stderr or "").strip()
    return None, err or "claude not found inside WSL (install it in your Linux distro, then `claude auth login`)"


def detect_claude_sources(command: str = "claude") -> list[ClaudeSource]:
    """Probe the reachable ``claude`` installs: this machine's PATH/standard
    locations, and — on Windows or wherever a `wsl` launcher exists — the
    bridged Linux binary. Never raises; each source degrades to
    available=False with a detail string explaining why."""
    is_windows = os.name == "nt"
    native_label = "Windows (native claude)" if is_windows else "This machine (Linux/native claude)"
    sources: list[ClaudeSource] = []

    native_path = _resolve_native_claude()
    if native_path:
        ok, ver = _probe_version([native_path])
        # Use the bare name when it's genuinely on PATH (portable across
        # machines); otherwise pin the absolute path we resolved it to.
        on_path = shutil.which("claude") is not None
        sources.append(ClaudeSource(
            id="native", label=native_label,
            command="claude" if on_path else native_path, command_args=[],
            available=ok, version=ver if ok else None,
            detail=native_path if ok else f"found {native_path} but --version failed: {ver}",
        ))
    else:
        sources.append(ClaudeSource(
            id="native", label=native_label, command="claude", command_args=[],
            available=False,
            detail="not on PATH or in ~/.local/bin, ~/.claude/local, /usr/local/bin, …",
        ))

    wsl_path = shutil.which("wsl")
    if wsl_path or is_windows:
        if not wsl_path:
            sources.append(ClaudeSource(
                id="wsl", label="WSL / Linux (claude inside WSL)",
                command="wsl", command_args=["-e", "claude"], available=False,
                detail="wsl.exe not found — is WSL installed? (`wsl --install`)",
            ))
        else:
            abs_path, why = _wsl_claude_path(wsl_path)
            if abs_path:
                ok, ver = _probe_version([wsl_path, "-e", abs_path])
                sources.append(ClaudeSource(
                    id="wsl", label="WSL / Linux (claude inside WSL)",
                    command="wsl", command_args=["-e", abs_path],
                    available=ok, version=ver if ok else None,
                    detail=abs_path if ok else f"{abs_path} did not respond to --version: {ver}",
                ))
            else:
                sources.append(ClaudeSource(
                    id="wsl", label="WSL / Linux (claude inside WSL)",
                    command="wsl", command_args=["-e", "claude"], available=False, detail=why,
                ))
    return sources


def _parse_usage(envelope: dict) -> LlmUsage | None:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None

    def _int(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return LlmUsage(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        source="actual",
    )


def _extract_structured(envelope: dict) -> dict | None:
    """The schema-conforming document from a print-mode JSON envelope.
    Claude Code variants expose it as `structured_output` or as the `result`
    string; both are accepted, code fences stripped defensively."""
    candidate = envelope.get("structured_output")
    if isinstance(candidate, dict):
        return candidate
    for key in ("result", "response", "text"):
        value = envelope.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _result_envelope(stdout: str) -> dict | None:
    """The final ``{"type":"result", ...}`` envelope from a print-mode call.

    With ``--output-format stream-json`` the CLI emits NDJSON — one JSON object
    per line (system/assistant/user/result events) — so the envelope (the same
    shape ``--output-format json`` produced as a single object: structured_output,
    usage, session_id, total_cost_usd) is the LAST line whose ``type`` is
    ``result``. Falls back to parsing the whole buffer as one object so the older
    single-envelope ``json`` mode (and the test fakes) still parse."""
    text = (stdout or "").strip()
    if not text:
        return None
    last_result: dict | None = None
    any_obj: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            any_obj = obj
            if obj.get("type") == "result":
                last_result = obj
    if last_result is not None:
        return last_result
    # Single-object (non-NDJSON) output: the whole buffer is the envelope.
    try:
        whole = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return any_obj
    return whole if isinstance(whole, dict) else any_obj


def _stream_line_message(line: str) -> str | None:
    """A short human-readable progress note for one stream-json event line, or
    None when the line carries nothing worth surfacing (system init, the final
    result envelope, raw tool payloads). Used to feed the LLM-activity view so a
    long call shows what the model is doing (reading pages, emitting output)
    instead of going silent for minutes."""
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Non-JSON noise on stdout: surface a trimmed snippet, not nothing.
        text = line.strip()
        return text[:_INTERIM_CHARS] if text and not text.startswith("{") else None
    if not isinstance(ev, dict):
        return None
    etype = ev.get("type")
    if etype == "assistant":
        parts: list[str] = []
        for block in ev.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                parts.append(f"using {name}")
            elif block.get("type") == "text":
                snippet = (block.get("text") or "").strip()
                if snippet:
                    parts.append(snippet[:200])
        return "; ".join(parts)[:_INTERIM_CHARS] if parts else None
    if etype == "user":
        # tool_result coming back (e.g. a page image was read)
        return "tool result received"
    if etype == "system":
        # the first line the CLI emits — confirms the prompt was accepted and a
        # session opened, so the operator knows the model received the request
        # even before any output (a big single call streams nothing until then).
        return "session started — model is working"
    if etype == "rate_limit_event":
        # surfaced because rate limiting is a real reason a call appears to hang
        status = ev.get("status") or ev.get("message")
        return f"rate limit: {status}" if isinstance(status, str) and status else "rate limit event"
    # result envelope: handled separately, not a progress note
    return None


def _error_from_stdout(stdout: str) -> str:
    """Best-effort error message when `claude` exits non-zero with empty stderr:
    the CLI's print-mode result envelope often carries the real reason on stdout
    (is_error=true + a `result`/`error` string, or an `api_error_status`). Falls
    back to a raw stdout snippet so the failure is never an undiagnosable
    'exit N'."""
    text = (stdout or "").strip()
    if not text:
        return ""
    env = _result_envelope(text)
    if isinstance(env, dict):
        for key in ("error", "result", "message", "api_error_status", "subtype"):
            value = env.get(key)
            if isinstance(value, str) and value.strip():
                return re.sub(r"\s+", " ", value.strip())
    return re.sub(r"\s+", " ", text)


class ClaudeCodeClient:
    """Launches hidden, non-interactive Claude Code extraction calls."""

    provider_name = "claude"

    def __init__(self, config: Config) -> None:
        self._command = config.claude_code.command
        self._command_args = list(config.claude_code.command_args)
        self._check_timeout = config.claude_code.default_timeout_seconds
        self._call_timeout = config.llm.timeout_seconds
        self._exclude_dynamic = config.llm.exclude_dynamic_system_prompt_sections
        self._stream_partial = getattr(config.llm, "stream_partial_messages", True)
        self._always_think = getattr(config.llm, "always_enable_thinking", True)
        self._pv_root = config.pv_root
        self._help_text: str | None = None

    # ------------------------------------------------------------------
    # read-only probes (auth / version / update / capabilities)
    # ------------------------------------------------------------------

    def binary_path(self) -> str | None:
        return shutil.which(self._command)

    def check_available(self) -> tuple[bool, str]:
        if self.binary_path() is None:
            return False, f"claude CLI ({self._command!r}) not found on PATH"
        return self.auth_status()

    def capabilities(self) -> LlmProviderCapabilities:
        return LlmProviderCapabilities(
            structured_output=self.supports("--json-schema"),
            image_input=True,
            output_schema_file=False,
            output_last_message_file=False,
            json_telemetry=False,
        )

    def _probe(self, argv_tail: list[str], timeout: int | None = None) -> tuple[int | None, str]:
        argv = [self._command, *self._command_args, *argv_tail]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout or self._check_timeout, env=_sanitized_env(),
            )
        except subprocess.TimeoutExpired:
            return None, f"timed out after {timeout or self._check_timeout}s"
        except OSError as exc:
            return None, f"failed to launch: {exc}"
        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode, output

    def version(self) -> str | None:
        code, output = self._probe(["--version"])
        return output if code == 0 else None

    def auth_status(self) -> tuple[bool, str]:
        """(authenticated, detail). On failure the detail carries the exact
        login instruction the operator needs."""
        code, output = self._probe(["auth", "status"])
        if code == 0:
            return True, output
        return False, f"{output or 'auth status failed'} — {LOGIN_REMEDIATION}"

    def update(self) -> tuple[bool, str]:
        code, output = self._probe(["update"], timeout=max(self._check_timeout, 300))
        return code == 0, output

    def supports(self, flag: str) -> bool:
        """Whether `claude --help` advertises a flag (probed once, cached)."""
        if self._help_text is None:
            _, output = self._probe(["--help"])
            self._help_text = output or ""
        return flag in self._help_text

    # ------------------------------------------------------------------
    # extraction
    # ------------------------------------------------------------------

    def extract_json(
        self,
        *,
        job_id: str,
        prompt: str,
        schema_path: Path,
        model: str,
        effort: str,
        cwd: Path,
        allow_read_tool: bool = True,
        timeout: int | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> ClaudeCodeResult:
        """One hidden `claude -p` extraction call. The prompt travels via
        stdin (never argv: no process-list leakage, no length limits); page
        images in `cwd` are exposed read-only through the Read tool."""
        # `claude --json-schema` takes the schema JSON *inline* (a string), NOT
        # a file path — passing a path makes the CLI try to JSON.parse the path
        # and exit 1 ("--json-schema is not valid JSON"). Read the compiled
        # schema and pass its content. Inline also sidesteps the Windows->WSL
        # bridge entirely (no cwd-relative path to translate); argv is a list,
        # so no shell quoting/escaping is involved.
        try:
            schema_arg = schema_path.read_text(encoding="utf-8")
            schema_doc = json.loads(schema_arg)
        except OSError as exc:
            result = ClaudeCodeResult(
                job_id=job_id, ok=False,
                error=f"could not read schema {schema_path}: {exc}",
            )
            self._log(result, model, effort)
            return result
        except json.JSONDecodeError as exc:
            result = ClaudeCodeResult(
                job_id=job_id, ok=False,
                error=f"could not parse schema {schema_path}: {exc}",
            )
            self._log(result, model, effort)
            return result
        tail = ["--model", model]
        if self.supports("--effort"):
            tail += ["--effort", effort]
        if allow_read_tool:
            tail += ["--allowedTools", "Read"]
        if self._exclude_dynamic and self.supports("--exclude-dynamic-system-prompt-sections"):
            tail.append("--exclude-dynamic-system-prompt-sections")
        # stream-json (NDJSON) instead of json: the CLI then emits one event per
        # line AS IT WORKS (assistant turns, tool_use/Read calls, tool results)
        # so the LLM-activity view shows live progress on a long call instead of
        # going silent until the single json envelope lands at the very end. The
        # final {"type":"result"} line is the same envelope json mode produced.
        # --verbose is REQUIRED by the CLI for stream-json with --print.
        stream_args = ["--output-format", "stream-json", "--verbose"]
        if self._stream_partial:
            # token-level deltas: surfaces the model's thinking + answer live
            stream_args.append("--include-partial-messages")
        if self._always_think:
            # force extended thinking on regardless of model (budget scales with
            # --effort); one argv token, so safe across the WSL bridge
            stream_args += ["--settings", '{"alwaysThinkingEnabled":true}']
        # exec_argv carries the inline schema; argv (stored/logged) redacts it to
        # keep the audit pointer flags-only (the schema can be many KB).
        exec_argv = [
            self._command, *self._command_args, "-p",
            *stream_args, "--json-schema", schema_arg, *tail,
        ]
        argv = [
            self._command, *self._command_args, "-p",
            *stream_args, "--json-schema", f"<inline schema:{len(schema_arg)} chars>", *tail,
        ]

        started = time.perf_counter()
        try:
            # text=True alone encodes stdin/decodes stdout with the LOCALE
            # codec — cp1252 on Windows, which cannot encode page-payload
            # characters like ◼ (U+25FC) and raises UnicodeEncodeError before
            # the call even runs. Force UTF-8 on both directions (errors=replace
            # only guards stdout decode; UTF-8 encodes any prompt losslessly).
            popen_kwargs: dict = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                # Line-buffered: default block buffering holds NDJSON lines in an
                # ~8 KB buffer, so the stream-json deltas would only surface in
                # bursts (or at process end) instead of live as the model works.
                "bufsize": 1,
                "cwd": str(cwd),
                "env": _sanitized_env(),
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(exec_argv, **popen_kwargs)
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            # Live-stream state: accumulate token deltas and emit coalesced
            # snapshots (thinking text tail + output char count) so the activity
            # view shows the model actually working without a flood of events.
            progress = {
                "thinking": [], "thinking_emit": 0.0,
                "out_chars": 0, "out_emit": 0.0, "last_delta": 0.0,
                "session_noted": False,
            }

            def _on_stdout_event(text: str) -> None:
                try:
                    ev = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    snippet = text.strip()
                    if snippet and not snippet.startswith("{"):
                        _emit_provider_event(event_sink, stream="stdout", message=snippet[:_INTERIM_CHARS])
                    return
                if not isinstance(ev, dict):
                    return
                if ev.get("type") == "stream_event":
                    se = ev.get("event") or {}
                    if se.get("type") != "content_block_delta":
                        return
                    delta = se.get("delta") or {}
                    dtype = delta.get("type")
                    now = time.perf_counter()
                    progress["last_delta"] = now
                    if dtype == "thinking_delta":
                        progress["thinking"].append(delta.get("thinking") or "")
                        if now - progress["thinking_emit"] >= _STREAM_EMIT_SECONDS:
                            progress["thinking_emit"] = now
                            tail = "".join(progress["thinking"])[-_THINKING_TAIL_CHARS:]
                            _emit_provider_event(event_sink, stream="thinking", message=tail)
                    elif dtype in ("text_delta", "input_json_delta"):
                        progress["out_chars"] += len(delta.get("text") or delta.get("partial_json") or "")
                        if now - progress["out_emit"] >= _STREAM_EMIT_SECONDS:
                            progress["out_emit"] = now
                            _emit_provider_event(
                                event_sink, stream="output",
                                message=f"writing answer… {progress['out_chars']} chars",
                            )
                    return
                # The CLI emits MANY system events with partial messages on;
                # surface the session-start note exactly once.
                if ev.get("type") == "system":
                    if not progress["session_noted"]:
                        progress["session_noted"] = True
                        _emit_provider_event(
                            event_sink, stream="stdout",
                            message="session started — model is working",
                        )
                    return
                # other discrete events (tool use, tool result, rate limit, ...)
                message = _stream_line_message(text)
                if message:
                    _emit_provider_event(event_sink, stream="stdout", message=message)

            def _read_stream(name: str, chunks: list[str]) -> None:
                stream = getattr(proc, name)
                if stream is None:
                    return
                try:
                    for line in stream:
                        chunks.append(line)
                        text = line.rstrip()
                        if not text:
                            continue
                        if name == "stderr":
                            _emit_provider_event(
                                event_sink,
                                stream="stderr",
                                message=text[:_INTERIM_CHARS],
                            )
                        else:
                            _on_stdout_event(text)
                except Exception as exc:  # noqa: BLE001 - capture best effort output
                    _emit_provider_event(
                        event_sink,
                        stream=name,
                        message=f"{name} reader stopped: {exc}",
                    )

            stdout_thread = threading.Thread(
                target=_read_stream, args=("stdout", stdout_chunks), daemon=True
            )
            stderr_thread = threading.Thread(
                target=_read_stream, args=("stderr", stderr_chunks), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()

            # Heartbeat: emit an elapsed-time note every _HEARTBEAT_SECONDS so a
            # long, output-silent call (text-only payload, no Read calls to
            # stream) still shows activity instead of looking hung.
            heartbeat_stop = threading.Event()

            def _heartbeat() -> None:
                if event_sink is None:
                    return  # nothing to report to
                while not heartbeat_stop.wait(_HEARTBEAT_SECONDS):
                    # Stay quiet while the model is actively streaming tokens —
                    # the thinking/output snapshots already show it is alive.
                    if time.perf_counter() - progress["last_delta"] < _HEARTBEAT_SECONDS:
                        continue
                    elapsed = int(time.perf_counter() - started)
                    _emit_provider_event(
                        event_sink,
                        stream="heartbeat",
                        message=f"still working — {elapsed}s elapsed",
                    )

            heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
            heartbeat_thread.start()
            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                proc.wait(timeout=timeout or self._call_timeout)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                    )
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                duration = round(time.perf_counter() - started, 2)
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                result = ClaudeCodeResult(
                    job_id=job_id, ok=False, duration_seconds=duration,
                    error=f"timed out after {timeout or self._call_timeout}s", argv=argv,
                )
                self._log(result, model, effort)
                return result
            except (BrokenPipeError, OSError) as exc:
                _emit_provider_event(
                    event_sink,
                    stream="stdin",
                    message=f"prompt write failed: {exc}",
                )
                try:
                    proc.wait(timeout=timeout or self._call_timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            capture_output=True,
                            text=True,
                        )
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            proc.kill()
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                    duration = round(time.perf_counter() - started, 2)
                    result = ClaudeCodeResult(
                        job_id=job_id, ok=False, duration_seconds=duration,
                        error=f"timed out after {timeout or self._call_timeout}s", argv=argv,
                    )
                    self._log(result, model, effort)
                    return result
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
        except OSError as exc:
            result = ClaudeCodeResult(
                job_id=job_id, ok=False, error=f"failed to launch: {exc}", argv=argv,
            )
            self._log(result, model, effort)
            return result

        duration = round(time.perf_counter() - started, 2)
        stdout = stdout or ""
        stderr = stderr or ""
        result = ClaudeCodeResult(
            job_id=job_id, ok=False, exit_code=proc.returncode, duration_seconds=duration,
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(), argv=argv,
        )
        if proc.returncode != 0:
            # Surface the CLI's own diagnostic — a bare "exit N" is useless.
            # The CLI sometimes writes the error to STDERR and sometimes (with
            # --output-format json) emits a JSON envelope on STDOUT with
            # is_error=true / an error string and STILL exits non-zero, so check
            # both. stderr/the envelope error is the CLI's own diagnostic, not
            # memo content (the memo travels via stdin and only appears in the
            # structured stdout payload). Truncated to keep the audit compact.
            stderr_detail = re.sub(r"\s+", " ", stderr).strip()
            detail = stderr_detail or _error_from_stdout(stdout)
            result.error = f"exit {proc.returncode}" + (f": {detail[:_STDERR_DEBUG_CHARS]}" if detail else "")
            self._log(result, model, effort)
            return result

        envelope = _result_envelope(stdout)
        if envelope is None:
            result.error = "no result envelope in stdout"
            self._log(result, model, effort)
            return result
        if not isinstance(envelope, dict):
            result.error = "unexpected JSON envelope (not an object)"
            self._log(result, model, effort)
            return result

        session_id = envelope.get("session_id")
        result.session_id = str(session_id) if session_id else None
        result.usage = _parse_usage(envelope)
        cost = envelope.get("total_cost_usd", envelope.get("cost_usd"))
        result.total_cost_usd = float(cost) if isinstance(cost, (int, float)) else None

        structured = _extract_structured(envelope)
        if structured is None:
            result.error = "no schema-conforming JSON document in output"
            self._log(result, model, effort)
            return result
        try:
            validate_structured_response(schema_doc, structured)
        except StructuredResponseError as exc:
            result.error = f"structured output failed schema validation: {exc}"
            self._log(result, model, effort)
            return result
        result.structured = structured
        result.ok = True
        self._log(result, model, effort)
        return result

    def extract_structured(
        self,
        *,
        job_id: str,
        prompt: str,
        schema: dict,
        images: list[Path] | None,
        timeout: int | None,
        model: str | None,
        effort: str | None,
        cwd: Path,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> ClaudeCodeResult:
        schema_path = cwd / f"{job_id}_schema.json"
        with guarded_open_write(schema_path, self._pv_root) as fh:
            fh.write(json.dumps(schema, ensure_ascii=False))
        return self.extract_json(
            job_id=job_id,
            prompt=prompt,
            schema_path=schema_path,
            model=model or "",
            effort=effort or "",
            cwd=cwd,
            allow_read_tool=True,
            timeout=timeout,
            event_sink=event_sink,
        )

    def _log(self, result: ClaudeCodeResult, model: str, effort: str) -> None:
        """INFO-safe logging: identifiers and counters only (redaction rule)."""
        log_event(
            logger, "claude code call",
            job_id=result.job_id, model=model, effort=effort, ok=result.ok,
            exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            session_id=result.session_id, error=result.error,
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost_usd=result.total_cost_usd,
        )
