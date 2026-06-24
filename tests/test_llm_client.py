"""D1 subprocess wrapper tests against a FAKE `claude` executable written to
tmp_path — the argv/stdin/stdout/exit-code/timeout/env contract is exercised
end-to-end without ever touching the real Claude Code CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pv_extractor.config import ClaudeCodeConfig, Config
from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.schema_builder import FIELD_RESULT_SCHEMA

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="fake-CLI shebang scripts are POSIX-only"
)

_FAKE_CLAUDE = f"""\
#!{sys.executable}
import json, os, sys, time

argv = sys.argv[1:]
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({{
            "argv": argv,
            "has_anthropic_key": "ANTHROPIC_API_KEY" in os.environ,
            "cwd": os.getcwd(),
        }}) + "\\n")

if "--help" in argv:
    print("Usage: claude [options] [prompt]\\n"
          "  -p, --print\\n  --output-format <format>\\n  --json-schema <schema>\\n"
          "  --model <model>\\n  --effort <level>\\n  --allowedTools <tools>\\n"
          "  --exclude-dynamic-system-prompt-sections")
    sys.exit(0)
if argv[:2] == ["auth", "status"]:
    if os.environ.get("FAKE_CLAUDE_AUTH", "ok") == "ok":
        print("Authenticated (fake)")
        sys.exit(0)
    print("Not logged in", file=sys.stderr)
    sys.exit(1)
if "--version" in argv:
    print("9.9.9 (fake)")
    sys.exit(0)

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "malformed":
    print("definitely not json")
    sys.exit(0)
if mode == "exit3":
    sys.exit(3)
if mode == "err_stdout":
    # exit non-zero with the reason ONLY on stdout (empty stderr) — the CLI's
    # print-mode error envelope shape.
    print(json.dumps({{"type": "result", "is_error": True, "result": "Usage limit reached"}}))
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode == "slow_ok":
    time.sleep(1)  # succeed, but slowly enough for a heartbeat to fire

# `claude --json-schema` takes the schema JSON inline (a string), not a path.
schema = json.loads(argv[argv.index("--json-schema") + 1])
doc = {{}}
for band, band_schema in schema["properties"].items():
    doc[band] = {{header: {{"value": "v", "unit": None, "page": 1,
                            "verbatim_quote": "q", "confidence": "low",
                            "not_found": False}}
                 for header in band_schema["properties"]}}
# stream-json (NDJSON): interim events first, then the final result envelope.
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess-001"}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "StructuredOutput", "input": {{}}}}]}}}}))
print(json.dumps({{
    "type": "result", "subtype": "success",
    "result": json.dumps(doc),
    "structured_output": doc,
    "session_id": "sess-001",
    "usage": {{"input_tokens": 1000, "output_tokens": 100,
               "cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}},
    "total_cost_usd": 0.0123,
}}))
"""


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    """Fake claude binary + argv/env log + a client pointed at both."""
    binary = tmp_path / "claude"
    binary.write_text(_FAKE_CLAUDE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-never-reach-the-child")
    config = Config(
        pv_root=str(tmp_path / "not_pv"), output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
        claude_code=ClaudeCodeConfig(command=str(binary), default_timeout_seconds=30),
    )
    client = ClaudeCodeClient(config)

    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "HEADLINE": {
                "type": "object", "additionalProperties": False,
                "properties": {"Fund Name": FIELD_RESULT_SCHEMA},
                "required": ["Fund Name"],
            }
        },
        "required": ["HEADLINE"],
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    return client, schema_path, log, tmp_path


def _logged_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_extract_json_flags_parsing_and_env_isolation(fake_env):
    client, schema_path, log, tmp_path = fake_env
    result = client.extract_json(
        job_id="pv-RUN-MEMO-t0", prompt="page text", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert result.ok and result.exit_code == 0
    assert result.structured == {
        "HEADLINE": {"Fund Name": {"value": "v", "unit": None, "page": 1,
                                   "verbatim_quote": "q", "confidence": "low",
                                   "not_found": False}}
    }
    assert result.session_id == "sess-001"
    assert result.usage is not None and result.usage.source == "actual"
    assert result.usage.input_tokens == 1000 and result.usage.cache_read_input_tokens == 50
    assert result.total_cost_usd == pytest.approx(0.0123)

    extraction = _logged_calls(log)[-1]
    argv = extraction["argv"]
    assert argv[0] == "-p"
    # stream-json (+ required --verbose) so the call streams interim events
    # rather than going silent until the final envelope.
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # the schema is passed INLINE as a JSON string (not a path): the CLI parses
    # --json-schema as JSON, and inline avoids any cwd/path translation when
    # the call is bridged from Windows into WSL.
    schema_arg = argv[argv.index("--json-schema") + 1]
    assert json.loads(schema_arg) == json.loads(schema_path.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"  # probed via --help
    assert argv[argv.index("--allowedTools") + 1] == "Read"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert extraction["cwd"] == str(tmp_path)
    # ANTHROPIC_* never reaches the child (no API-key auth, ever)
    assert all(call["has_anthropic_key"] is False for call in _logged_calls(log))
    # the prompt travels on stdin, never argv
    assert all("page text" not in " ".join(call["argv"]) for call in _logged_calls(log))


def test_stream_json_emits_interim_events(fake_env):
    """stream-json mode surfaces interim progress: the per-line event_sink
    receives the assistant tool_use note BEFORE the final result lands, so a
    long call is no longer silent. The final envelope still parses out of the
    NDJSON stream."""
    client, schema_path, _, tmp_path = fake_env
    events: list[dict] = []
    result = client.extract_json(
        job_id="pv-RUN-MEMO-t0", prompt="page text", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
        event_sink=events.append,
    )
    assert result.ok and result.structured is not None
    messages = [str(e.get("message", "")) for e in events if e.get("stream") == "stdout"]
    assert any("StructuredOutput" in m for m in messages), messages
    # the final result envelope must NOT leak as an interim progress note
    assert not any('"type": "result"' in m or "'type': 'result'" in m for m in messages)


def test_heartbeat_emits_while_call_runs(fake_env, monkeypatch):
    """A long, output-silent call still reports it is alive: the heartbeat timer
    emits elapsed-time notes independent of any provider output, so the activity
    view never looks hung even when the model streams nothing for minutes."""
    import pv_extractor.llm.claude_code_client as ccc

    monkeypatch.setattr(ccc, "_HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow_ok")
    client, schema_path, _, tmp_path = fake_env
    events: list[dict] = []
    result = client.extract_json(
        job_id="pv-RUN-MEMO-t0", prompt="page text", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path, timeout=30,
        event_sink=events.append,
    )
    assert result.ok
    heartbeats = [e for e in events if e.get("stream") == "heartbeat"]
    assert heartbeats, [e.get("stream") for e in events]
    assert "elapsed" in str(heartbeats[0].get("message", ""))


def test_command_args_are_prepended(fake_env):
    """command_args (the Windows -> WSL bridge: command: wsl,
    command_args: [-e, claude]) land between the command and claude's own
    argv, for probes and extraction calls alike."""
    _client, schema_path, log, tmp_path = fake_env
    config = Config(
        pv_root=str(tmp_path / "not_pv"), output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
        claude_code=ClaudeCodeConfig(
            command=str(tmp_path / "claude"), command_args=["--fake-bridge-arg"],
            default_timeout_seconds=30,
        ),
    )
    bridged = ClaudeCodeClient(config)
    assert bridged.version() == "9.9.9 (fake)"
    result = bridged.extract_json(
        job_id="pv-RUN-MEMO-t0", prompt="page text", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert result.ok
    calls = _logged_calls(log)
    assert calls and all(call["argv"][0] == "--fake-bridge-arg" for call in calls)


def test_auth_status_ok_and_failure_remediation(fake_env, monkeypatch):
    client, *_ = fake_env
    ok, detail = client.auth_status()
    assert ok and "Authenticated" in detail
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "fail")
    ok, detail = client.auth_status()
    assert not ok
    assert "claude auth login" in detail  # clear login instruction (D1)


def test_malformed_json_is_an_error_result(fake_env, monkeypatch):
    client, schema_path, _, tmp_path = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "malformed")
    result = client.extract_json(
        job_id="j", prompt="x", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert not result.ok and "no result envelope" in (result.error or "")


def test_nonzero_exit_is_an_error_result(fake_env, monkeypatch):
    client, schema_path, _, tmp_path = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit3")
    result = client.extract_json(
        job_id="j", prompt="x", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert not result.ok and result.exit_code == 3 and "exit 3" in (result.error or "")


def test_nonzero_exit_surfaces_stdout_error(fake_env, monkeypatch):
    """When the CLI exits non-zero with EMPTY stderr but a JSON error envelope
    on stdout, the real reason is surfaced (not a bare 'exit 1')."""
    client, schema_path, _, tmp_path = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "err_stdout")
    result = client.extract_json(
        job_id="j", prompt="x", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert not result.ok and result.exit_code == 1
    assert "Usage limit reached" in (result.error or "")


def test_timeout_is_an_error_result(fake_env, monkeypatch):
    client, schema_path, _, tmp_path = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    result = client.extract_json(
        job_id="j", prompt="x", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path, timeout=1,
    )
    assert not result.ok and "timed out" in (result.error or "")


def test_version_probe(fake_env):
    client, *_ = fake_env
    assert client.version() == "9.9.9 (fake)"
