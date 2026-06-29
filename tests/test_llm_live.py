"""Optional LIVE Claude Code test (D6). Skipped unless BOTH hold:

    PV_LIVE_CLAUDE_CODE_TESTS=1   (explicit opt-in)
    `claude auth status` passes   (a real logged-in local Claude Code)

One tiny single-field haiku/low call end-to-end — the only test in the
suite allowed to launch the real CLI."""

from __future__ import annotations

import json
import os

import pytest

from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.schema_builder import FIELD_RESULT_SCHEMA

pytestmark = pytest.mark.live


@pytest.mark.live
def test_live_tiny_single_field_extraction(tmp_path, default_config):
    if os.environ.get("PV_LIVE_CLAUDE_CODE_TESTS") != "1":
        pytest.skip("set PV_LIVE_CLAUDE_CODE_TESTS=1 to run the live Claude Code test")
    client = ClaudeCodeClient(default_config)
    if client.binary_path() is None:
        pytest.skip("claude CLI not installed")
    auth_ok, detail = client.auth_status()
    if not auth_ok:
        pytest.skip(f"claude not authenticated: {detail}")

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "TEST": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"Color": FIELD_RESULT_SCHEMA},
                "required": ["Color"],
            }
        },
        "required": ["TEST"],
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
    prompt = (
        "Extract the field below from the supplied page. Respond with JSON "
        "conforming to the provided schema; verbatim_quote must be copied "
        "exactly from the page.\n\n"
        '[TEST]\n- "Color" (type=string) — the stated color of the grass\n\n'
        "== DOCUMENT PAGES ==\n--- page 1 (TEXT) ---\nThe grass is green.\n"
    )
    result = client.extract_json(
        job_id="pv-live-smoke-t0", prompt=prompt, schema_path=schema_path,
        model="haiku", effort="low", cwd=tmp_path, allow_read_tool=False, timeout=300,
    )
    assert result.ok, result.error
    field = result.structured["TEST"]["Color"]
    assert field["not_found"] is False
    assert "green" in str(field["value"]).lower()
    assert "green" in field["verbatim_quote"].lower()
