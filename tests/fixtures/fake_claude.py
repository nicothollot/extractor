"""Fake Claude Code client (D6): canned schema-valid responses, zero
subprocesses. Drop-in for pv_extractor.llm.claude_code_client.ClaudeCodeClient
via the run()/process_memos `client` parameter — NO test launches the real
CLI by default (the only exception is tests/test_llm_live.py, opt-in)."""

from __future__ import annotations

import json
from pathlib import Path

from pv_extractor.llm.claude_code_client import LOGIN_REMEDIATION, ClaudeCodeResult
from pv_extractor.models import LlmUsage


def field_result(
    value=None,
    *,
    unit: str | None = None,
    page: int | None = None,
    quote: str = "",
    confidence: str = "high",
    not_found: bool = False,
) -> dict:
    """One response field object in the rule-5 provenance shape."""
    return {
        "value": value,
        "unit": unit,
        "page": page,
        "verbatim_quote": quote,
        "confidence": confidence,
        "not_found": not_found,
    }


def schema_response(schema: dict, values: dict[str, dict]) -> dict:
    """Fill EVERY band/field the response schema demands: canned objects for
    headers in `values`, not_found for the rest (always schema-valid)."""
    out: dict[str, dict] = {}
    for band, band_schema in schema["properties"].items():
        out[band] = {
            header: values.get(header, field_result(not_found=True))
            for header in band_schema["properties"]
        }
    return out


def intent_response(
    schema: dict,
    *,
    filename_include: list[str] | None = None,
    filename_regex: list[str] | None = None,
    filename_exclude: list[str] | None = None,
    folder_include: list[str] | None = None,
    folder_exclude: list[str] | None = None,
    extensions: list[str] | None = None,
) -> dict:
    """Fill EVERY field the Smart Search intent schema demands (a flat object
    of string-array fields), defaulting missing fields to []. Always
    schema-valid against search.intent._INTENT_SCHEMA."""
    supplied = {
        "filename_include": filename_include,
        "filename_regex": filename_regex,
        "filename_exclude": filename_exclude,
        "folder_include": folder_include,
        "folder_exclude": folder_exclude,
        "extensions": extensions,
    }
    return {field: list(supplied.get(field) or []) for field in schema["properties"]}


class FakeClaudeCodeClient:
    """Programmable stand-in. `values` maps schema headers to field_result()
    objects; `behaviors` scripts per-call outcomes in order ("ok",
    "malformed", "exit") and defaults to "ok" once exhausted.

    When `mode='intent'` the structured output is a flat Smart Search
    DocTypeSpec-anchor object (search.intent._INTENT_SCHEMA) built from
    `intent_anchors` via intent_response(); the default extraction mode fills
    the band-grouped schema via schema_response()."""

    def __init__(
        self,
        values: dict[str, dict] | None = None,
        *,
        behaviors: list[str] | None = None,
        auth_ok: bool = True,
        usage: LlmUsage | None = None,
        total_cost_usd: float | None = None,
        mode: str = "extraction",
        intent_anchors: dict[str, list[str]] | None = None,
        binary: str | None = "/fake/claude",
    ) -> None:
        self.values = values or {}
        self.behaviors = list(behaviors or [])
        self.auth_ok = auth_ok
        self.usage = usage
        self.total_cost_usd = total_cost_usd
        self.mode = mode
        self.intent_anchors = intent_anchors or {}
        self._binary = binary
        self.calls: list[dict] = []

    # --- probe surface -------------------------------------------------
    def binary_path(self) -> str | None:
        return self._binary

    def version(self) -> str | None:
        return "9.9.9 (fake)"

    def auth_status(self) -> tuple[bool, str]:
        if self.auth_ok:
            return True, "Authenticated (fake)"
        return False, f"Not logged in — {LOGIN_REMEDIATION}"

    def update(self) -> tuple[bool, str]:
        return True, "fake update"

    def supports(self, flag: str) -> bool:
        return True

    # --- extraction ----------------------------------------------------
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
    ) -> ClaudeCodeResult:
        self.calls.append(
            {"job_id": job_id, "model": model, "effort": effort,
             "prompt_chars": len(prompt), "cwd": str(cwd), "schema_path": str(schema_path)}
        )
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior == "malformed":
            return ClaudeCodeResult(
                job_id=job_id, ok=False, exit_code=0, duration_seconds=0.1,
                error="non-JSON stdout: Expecting value: line 1 column 1 (char 0)",
            )
        if behavior == "exit":
            return ClaudeCodeResult(
                job_id=job_id, ok=False, exit_code=3, duration_seconds=0.1, error="exit 3",
            )
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        if self.mode == "intent":
            structured = intent_response(schema, **self.intent_anchors)
        else:
            structured = schema_response(schema, self.values)
        return ClaudeCodeResult(
            job_id=job_id, ok=True, exit_code=0, duration_seconds=0.2,
            session_id=f"sess-fake-{len(self.calls):03d}",
            structured=structured,
            usage=self.usage, total_cost_usd=self.total_cost_usd,
        )
