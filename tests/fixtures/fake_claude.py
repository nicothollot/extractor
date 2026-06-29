"""Fake Claude Code client (D6): canned schema-valid responses, zero
subprocesses. Drop-in for pv_extractor.llm.claude_code_client.ClaudeCodeClient
via the run()/process_memos `client` parameter — NO test launches the real
CLI by default (the only exception is tests/test_llm_live.py, opt-in)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pv_extractor.llm.claude_code_client import LOGIN_REMEDIATION, ClaudeCodeResult
from pv_extractor.models import LlmUsage

# Field lines in the call prompt look like:
#   - "Gross IRR %" {field_id: Gross_IRR} (...)
# Legacy fallback still uses {json key: ...}.
_PROMPT_KEY_RE = re.compile(r'"(?P<header>[^"]+)"\s*\{(?:field_id|field_key|json key):\s*(?P<key>[^}]+)\}')


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


def schema_response(
    schema: dict, values: dict[str, dict], key_to_header: dict[str, str] | None = None
) -> dict:
    """Fill EVERY band/field the response schema demands: canned objects for
    headers in `values`, not_found for the rest (always schema-valid). The
    schema's property keys are the sanitized JSON keys, so `key_to_header`
    (parsed from the call prompt) maps each schema key back to its workbook
    header before looking it up in the header-keyed `values`."""
    key_to_header = key_to_header or {}
    out: dict[str, dict] = {}
    for band, band_schema in schema["properties"].items():
        out[band] = {
            key: values.get(key_to_header.get(key, key), field_result(not_found=True))
            for key in band_schema["properties"]
        }
    return out


def sparse_v2_response(
    schema: dict, values: dict[str, dict], key_to_header: dict[str, str] | None = None
) -> dict:
    key_to_header = key_to_header or {}
    keys = list(schema["properties"]["not_found_field_keys"]["items"]["enum"])
    results: list[dict] = []
    not_found: list[str] = []
    for key in keys:
        header = key_to_header.get(key, key)
        value = values.get(header)
        if value is None or value.get("not_found"):
            not_found.append(key)
            continue
        confidence = value.get("confidence", "high")
        score = {"high": 0.85, "medium": 0.60, "low": 0.35}.get(str(confidence), confidence)
        results.append(
            {
                "field_key": key,
                "value": value.get("value"),
                "unit": value.get("unit"),
                "page": value.get("page") or 1,
                "evidence_quote": value.get("verbatim_quote") or value.get("evidence_quote") or "",
                "confidence": float(score) if isinstance(score, (int, float)) else 0.35,
                "notes": "",
            }
        )
    return {"schema_version": 2, "results": results, "not_found_field_keys": not_found, "warnings": []}


def sparse_response(
    schema: dict, values: dict[str, dict], key_to_header: dict[str, str] | None = None
) -> dict:
    """Fill sparse v5 responses by field id."""
    key_to_header = key_to_header or {}
    properties = schema.get("properties", {})
    if "not_found_field_keys" in properties:
        return sparse_v2_response(schema, values, key_to_header)
    keys = list(properties["not_found"]["items"]["enum"])
    results: list[dict] = []
    not_found: list[str] = []
    for key in keys:
        header = key_to_header.get(key, key)
        value = values.get(header)
        if value is None or value.get("not_found"):
            not_found.append(key)
            continue
        confidence = value.get("model_confidence", value.get("confidence", "high"))
        score = {"high": 0.85, "medium": 0.60, "low": 0.35}.get(str(confidence), confidence)
        results.append(
            {
                "field_id": key,
                "value": value.get("value"),
                "unit": value.get("unit"),
                "document_id": value.get("document_id") or "D01",
                "page": value.get("page") or 1,
                "as_of_date": value.get("as_of_date"),
                "quote": value.get("verbatim_quote") or value.get("evidence_quote") or value.get("quote") or "",
                "evidence_kind": value.get("evidence_kind") or "explicit_label",
                "model_confidence": float(score) if isinstance(score, (int, float)) else 0.35,
            }
        )
    return {
        "schema_version": 5,
        "scope": {"type": "document", "id": "D01"},
        "values": results,
        "not_found": not_found,
        "conflicts": [],
        "warnings": [],
    }


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
    def _canned_result(
        self, *, job_id: str, prompt: str, schema_path: Path, model: str, effort: str,
        cwd: Path, timeout: int | None, call_kind: str,
    ) -> ClaudeCodeResult:
        """Shared canned outcome for extract_json / extract_json_file. The fake
        replaces the whole client, so it returns the structured result directly
        (no subprocess, no real answers.json file); call_kind is recorded so a
        test can assert the file-based seam was taken."""
        self.calls.append(
            {"job_id": job_id, "model": model, "effort": effort,
             "prompt_chars": len(prompt), "cwd": str(cwd), "schema_path": str(schema_path),
             "call_kind": call_kind}
        )
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior == "malformed":
            error = (
                f"answer file answers.json is not valid JSON: Expecting value: line 1 column 1 (char 0)"
                if call_kind == "file"
                else "non-JSON stdout: Expecting value: line 1 column 1 (char 0)"
            )
            return ClaudeCodeResult(
                job_id=job_id, ok=False, exit_code=0, duration_seconds=0.1, error=error,
            )
        if behavior == "exit":
            return ClaudeCodeResult(
                job_id=job_id, ok=False, exit_code=3, duration_seconds=0.1, error="exit 3",
            )
        if behavior == "timeout":
            return ClaudeCodeResult(
                job_id=job_id, ok=False, exit_code=None, duration_seconds=timeout or 180,
                error=f"timed out after {timeout or 180}s",
            )
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        if self.mode == "intent":
            structured = intent_response(schema, **self.intent_anchors)
        else:
            key_to_header = {
                m.group("key").strip(): m.group("header")
                for m in _PROMPT_KEY_RE.finditer(prompt)
            }
            if schema.get("properties", {}).get("schema_version"):
                structured = sparse_response(schema, self.values, key_to_header)
            else:
                structured = schema_response(schema, self.values, key_to_header)
        return ClaudeCodeResult(
            job_id=job_id, ok=True, exit_code=0, duration_seconds=0.2,
            session_id=f"sess-fake-{len(self.calls):03d}",
            structured=structured,
            usage=self.usage, total_cost_usd=self.total_cost_usd,
        )

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
        return self._canned_result(
            job_id=job_id, prompt=prompt, schema_path=schema_path, model=model,
            effort=effort, cwd=cwd, timeout=timeout, call_kind="json",
        )

    def extract_json_file(
        self,
        *,
        job_id: str,
        prompt: str,
        schema_path: Path,
        model: str,
        effort: str,
        cwd: Path,
        timeout: int | None = None,
        event_sink=None,
    ) -> ClaudeCodeResult:
        """File-based-output seam: the real client has the model WRITE answers.json
        and reads it back; the fake returns the same canned structured payload."""
        return self._canned_result(
            job_id=job_id, prompt=prompt, schema_path=schema_path, model=model,
            effort=effort, cwd=cwd, timeout=timeout, call_kind="file",
        )
