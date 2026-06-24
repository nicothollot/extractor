"""Temporary structured-extraction provider backed by local ``codex exec``.

This provider never calls a hosted API from Python. It only invokes the
operator-authenticated Codex CLI non-interactively.
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
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.logging_setup import log_event
from pv_extractor.llm.provider import LlmCliResult, LlmProviderCapabilities
from pv_extractor.llm.response_validation import (
    StructuredResponseError,
    parse_json_object,
    validate_structured_response,
)
from pv_extractor.models import LlmUsage

logger = logging.getLogger(__name__)

_CAPTURE_LIMIT = 128_000
_ERROR_CHARS = 400


def _safe_env() -> dict[str, str]:
    # Preserve local CLI auth context, but do not let provider API keys silently
    # change identity/billing for extraction subprocesses.
    blocked = ("ANTHROPIC_", "OPENAI_API_KEY")
    return {k: v for k, v in os.environ.items() if not any(k.upper().startswith(p) for p in blocked)}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _read_bounded(handle, limit: int = _CAPTURE_LIMIT) -> str:
    handle.seek(0)
    data = handle.read(limit + 1)
    if isinstance(data, bytes):
        text = data[:limit].decode("utf-8", errors="replace")
    else:
        text = str(data)[:limit]
    if len(data) > limit:
        text += "\n...[truncated]"
    return text


def _parse_jsonl_metadata(stdout: str) -> tuple[str | None, LlmUsage | None]:
    session_id: str | None = None
    usage: LlmUsage | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("session_id", "sessionId", "thread_id", "threadId"):
            value = event.get(key)
            if value and session_id is None:
                session_id = str(value)
        raw_usage = event.get("usage") or event.get("token_usage") or event.get("tokens")
        if isinstance(raw_usage, dict):
            def _int(*keys: str) -> int:
                for key in keys:
                    value = raw_usage.get(key)
                    if isinstance(value, (int, float)):
                        return int(value)
                return 0

            usage = LlmUsage(
                input_tokens=_int("input_tokens", "input", "prompt_tokens"),
                output_tokens=_int("output_tokens", "output", "completion_tokens"),
                cache_read_input_tokens=_int("cache_read_input_tokens", "cache_read"),
                cache_creation_input_tokens=_int("cache_creation_input_tokens", "cache_creation"),
                source="actual",
            )
    return session_id, usage


class CodexCliClient:
    provider_name = "codex"

    def __init__(self, config: Config) -> None:
        self._command = config.codex_cli.command
        self._command_args = list(config.codex_cli.command_args)
        self._timeout = config.codex_cli.default_timeout_seconds
        self._model = config.codex_cli.model
        self._effort = config.codex_cli.reasoning_effort
        self._debug_raw = config.codex_cli.debug_capture_raw_response
        self._pv_root = config.pv_root
        self._help_text: str | None = None

    def binary_path(self) -> str | None:
        return shutil.which(self._command)

    def _base(self) -> list[str]:
        return [self._command, *self._command_args]

    def _probe(self, tail: list[str], timeout: int | None = None) -> tuple[int | None, str]:
        try:
            proc = subprocess.run(
                [*self._base(), *tail],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self._timeout,
                env=_safe_env(),
            )
        except subprocess.TimeoutExpired:
            return None, f"timed out after {timeout or self._timeout}s"
        except OSError as exc:
            return None, f"failed to launch: {exc}"
        return proc.returncode, (proc.stdout or "").strip() or (proc.stderr or "").strip()

    def version(self) -> str | None:
        code, output = self._probe(["--version"])
        return output if code == 0 else None

    def auth_status(self) -> tuple[bool, str]:
        if self.binary_path() is None:
            return False, f"codex CLI ({self._command!r}) not found on PATH"
        # Codex CLI has varied auth probes; a successful help/version check is
        # the read-only availability signal. Real auth failures surface on exec.
        version = self.version()
        if version:
            return True, version
        code, output = self._probe(["exec", "--help"])
        return code == 0, output or "codex exec --help failed"

    def check_available(self) -> tuple[bool, str]:
        return self.auth_status()

    def _exec_help(self) -> str:
        if self._help_text is None:
            _, output = self._probe(["exec", "--help"])
            self._help_text = output or ""
        return self._help_text

    def supports(self, flag: str) -> bool:
        return flag in self._exec_help()

    def _image_flag(self) -> str | None:
        help_text = self._exec_help()
        for flag in ("--image", "--input-image", "--images"):
            if flag in help_text:
                return flag
        return None

    def capabilities(self) -> LlmProviderCapabilities:
        image_input = self._image_flag() is not None
        return LlmProviderCapabilities(
            structured_output=self.supports("--output-schema"),
            image_input=image_input,
            output_schema_file=self.supports("--output-schema"),
            output_last_message_file=self.supports("--output-last-message"),
            json_telemetry=self.supports("--json"),
        )

    def extract_json(
        self,
        *,
        job_id: str,
        prompt: str,
        schema_path: Path,
        model: str | None,
        effort: str | None,
        cwd: Path,
        allow_read_tool: bool = True,
        timeout: int | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> LlmCliResult:
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return LlmCliResult(provider="codex", job_id=job_id, ok=False, error=f"schema unreadable: {exc}")
        return self.extract_structured(
            job_id=job_id,
            prompt=prompt,
            schema=schema,
            images=[] if allow_read_tool else None,
            timeout=timeout,
            model=model,
            effort=effort,
            cwd=cwd,
            event_sink=event_sink,
        )

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
    ) -> LlmCliResult:
        caps = self.capabilities()
        unsupported_images = bool(images) and not caps.image_input
        if unsupported_images:
            return LlmCliResult(
                provider="codex",
                job_id=job_id,
                ok=False,
                error="codex CLI does not advertise image attachment support; OCR/text payload required",
                safe_error_summary="image input unsupported by this codex CLI",
            )

        with tempfile.TemporaryDirectory(prefix="pv_codex_") as tmp:
            tmp_dir = Path(tmp)
            schema_file = tmp_dir / "schema.json"
            result_file = tmp_dir / "last_message.txt"
            with guarded_open_write(schema_file, self._pv_root) as fh:
                fh.write(json.dumps(schema, ensure_ascii=False))

            exec_argv = [*self._base(), "exec"]
            if model or self._model:
                exec_argv += ["--model", model or self._model or ""]
            if self.supports("--sandbox"):
                exec_argv += ["--sandbox", "read-only"]
            if self.supports("-c") or self.supports("--config"):
                exec_argv += ["-c", f"model_reasoning_effort={json.dumps(effort or self._effort)}"]
            if caps.output_schema_file:
                exec_argv += ["--output-schema", str(schema_file)]
            if caps.output_last_message_file:
                exec_argv += ["--output-last-message", str(result_file)]
            if caps.json_telemetry:
                exec_argv.append("--json")
            image_flag = self._image_flag()
            if images and image_flag:
                for image in images:
                    exec_argv += [image_flag, str(image)]
            exec_argv.append("-")

            safe_argv = [
                arg if arg != str(schema_file) else "<schema file>"
                for arg in exec_argv
            ]
            started = time.perf_counter()
            stdout_tmp = tempfile.TemporaryFile(mode="w+b")
            stderr_tmp = tempfile.TemporaryFile(mode="w+b")
            try:
                popen_kwargs: dict = {
                    "stdin": subprocess.PIPE,
                    "stdout": stdout_tmp,
                    "stderr": stderr_tmp,
                    "cwd": str(cwd),
                    "env": _safe_env(),
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(exec_argv, **popen_kwargs)
                try:
                    proc.communicate(prompt.encode("utf-8"), timeout=timeout or self._timeout)
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
                    return LlmCliResult(
                        provider="codex",
                        job_id=job_id,
                        ok=False,
                        duration_seconds=duration,
                        error=f"timed out after {timeout or self._timeout}s",
                        safe_error_summary="codex exec timed out",
                        argv=safe_argv,
                        command_metadata={"supports": caps.model_dump()},
                    )
            except OSError as exc:
                return LlmCliResult(
                    provider="codex",
                    job_id=job_id,
                    ok=False,
                    error=f"failed to launch: {exc}",
                    safe_error_summary=str(exc),
                    argv=safe_argv,
                )
            finally:
                stdout = _read_bounded(stdout_tmp)
                stderr = _read_bounded(stderr_tmp)
                stdout_tmp.close()
                stderr_tmp.close()

            duration = round(time.perf_counter() - started, 2)
            result = LlmCliResult(
                provider="codex",
                job_id=job_id,
                ok=False,
                exit_code=proc.returncode,
                duration_seconds=duration,
                stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                argv=safe_argv,
                command_metadata={"supports": caps.model_dump()},
            )
            session_id, usage = _parse_jsonl_metadata(stdout if caps.json_telemetry else "")
            result.session_id = session_id
            result.usage = usage

            if proc.returncode != 0:
                detail = _squash(stderr) or _squash(stdout)
                result.error = f"exit {proc.returncode}" + (f": {detail[:_ERROR_CHARS]}" if detail else "")
                result.safe_error_summary = result.error
                self._log(result)
                return result

            final_text = ""
            if caps.output_last_message_file and result_file.exists():
                final_text = result_file.read_text(encoding="utf-8", errors="replace")
            elif not caps.json_telemetry:
                final_text = stdout
            else:
                result.error = "codex exec produced JSON telemetry but no --output-last-message file"
                result.safe_error_summary = result.error
                self._log(result)
                return result

            if self._debug_raw:
                debug_path = cwd / f"{job_id}_codex_last_message.txt"
                with guarded_open_write(debug_path, self._pv_root) as fh:
                    fh.write(final_text[:_CAPTURE_LIMIT])
                result.command_metadata["debug_last_message_path"] = str(debug_path)

            try:
                structured = parse_json_object(final_text)
                validate_structured_response(schema, structured)
            except StructuredResponseError as exc:
                result.error = f"structured output failed schema validation: {exc}"
                result.safe_error_summary = result.error
                self._log(result)
                return result

            result.structured = structured
            result.ok = True
            self._log(result)
            return result

    def _log(self, result: LlmCliResult) -> None:
        log_event(
            logger,
            "codex cli call",
            job_id=result.job_id,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            session_id=result.session_id,
            error=result.safe_error_summary or result.error,
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
        )
