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
          "  -p, --print\\n  --output-format <format>\\n  --json-schema <file>\\n"
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
if mode == "sleep":
    time.sleep(30)

with open(argv[argv.index("--json-schema") + 1]) as fh:
    schema = json.load(fh)
doc = {{}}
for band, band_schema in schema["properties"].items():
    doc[band] = {{header: {{"value": "v", "unit": None, "page": 1,
                            "verbatim_quote": "q", "confidence": "low",
                            "not_found": False}}
                 for header in band_schema["properties"]}}
print(json.dumps({{
    "type": "result", "subtype": "success",
    "result": json.dumps(doc),
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
    assert argv[argv.index("--output-format") + 1] == "json"
    # the schema lives inside cwd and is passed RELATIVE with posix
    # separators, so the same argv works when bridged from Windows into WSL
    expected_schema = schema_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    assert argv[argv.index("--json-schema") + 1] == expected_schema
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"  # probed via --help
    assert argv[argv.index("--allowedTools") + 1] == "Read"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert extraction["cwd"] == str(tmp_path)
    # ANTHROPIC_* never reaches the child (no API-key auth, ever)
    assert all(call["has_anthropic_key"] is False for call in _logged_calls(log))
    # the prompt travels on stdin, never argv
    assert all("page text" not in " ".join(call["argv"]) for call in _logged_calls(log))


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
    assert not result.ok and "non-JSON" in (result.error or "")


def test_nonzero_exit_is_an_error_result(fake_env, monkeypatch):
    client, schema_path, _, tmp_path = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit3")
    result = client.extract_json(
        job_id="j", prompt="x", schema_path=schema_path,
        model="sonnet", effort="low", cwd=tmp_path,
    )
    assert not result.ok and result.exit_code == 3 and "exit 3" in (result.error or "")


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
