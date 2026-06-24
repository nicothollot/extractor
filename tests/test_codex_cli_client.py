"""Codex CLI provider tests against a fake executable.

These cover the subprocess contract without touching a real Codex login or any
hosted API.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from pv_extractor.config import CodexCliConfig, Config
from pv_extractor.llm.codex_cli_client import CodexCliClient
from pv_extractor.llm.schema_builder import FIELD_RESULT_SCHEMA

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="fake-CLI shebang scripts are POSIX-only"
)

_FAKE_CODEX = f"""\
#!{sys.executable}
import json, os, subprocess, sys, time

argv = sys.argv[1:]
log = os.environ.get("FAKE_CODEX_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{
            "argv": argv,
            "cwd": os.getcwd(),
            "has_openai_key": "OPENAI_API_KEY" in os.environ,
            "has_anthropic_key": "ANTHROPIC_API_KEY" in os.environ,
        }}) + "\\n")

if "--version" in argv and "exec" not in argv:
    print("codex 9.9.9 (fake)")
    sys.exit(0)

if argv[:2] == ["exec", "--help"]:
    help_text = (
        "Usage: codex exec [OPTIONS] -\\n"
        "  --model <model>\\n"
        "  --sandbox <mode>\\n"
        "  --output-schema <path>\\n"
        "  --output-last-message <path>\\n"
        "  --json\\n"
        "  -c <key=value>\\n"
    )
    if os.environ.get("FAKE_CODEX_IMAGE") == "1":
        help_text += "  --image <path>\\n"
    print(help_text)
    sys.exit(0)

if argv and argv[0] == "exec":
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CODEX_MODE", "ok")
    if mode == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pid_file = os.environ.get("FAKE_CODEX_CHILD_PID")
        if pid_file:
            with open(pid_file, "w", encoding="utf-8") as fh:
                fh.write(str(child.pid))
        time.sleep(60)
    if mode == "nonzero":
        print("simulated codex failure", file=sys.stderr)
        sys.exit(9)

    result_path = None
    if "--output-last-message" in argv:
        result_path = argv[argv.index("--output-last-message") + 1]
    if mode == "invalid_json":
        final = "definitely not json"
    elif mode == "schema_failure":
        final = json.dumps({{"HEADLINE": {{}}}})
    else:
        final = json.dumps({{
            "HEADLINE": {{
                "Fund Name": {{
                    "value": "Acme Fund",
                    "unit": None,
                    "page": 1,
                    "verbatim_quote": "Acme Fund",
                    "confidence": "high",
                    "not_found": False,
                }}
            }}
        }})
    if result_path:
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write(final)
    if "--json" in argv:
        print(json.dumps({{"session_id": "codex-sess-1", "usage": {{"input_tokens": 12, "output_tokens": 5}}}}))
    else:
        print(final)
    sys.exit(0)

print("unexpected argv: " + json.dumps(argv), file=sys.stderr)
sys.exit(2)
"""


@pytest.fixture()
def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(_FAKE_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-never-reach-child")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-never-reach-child")
    config = Config(
        pv_root=str(tmp_path / "not_pv"),
        output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
        codex_cli=CodexCliConfig(
            command=str(binary),
            default_timeout_seconds=30,
            model="gpt-local",
            reasoning_effort="high",
        ),
    )
    return CodexCliClient(config), log, tmp_path


def _schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "HEADLINE": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"Fund Name": FIELD_RESULT_SCHEMA},
                "required": ["Fund Name"],
            }
        },
        "required": ["HEADLINE"],
    }


def _logged_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _extract(client: CodexCliClient, tmp_path: Path, **overrides):
    params = {
        "job_id": "job-1",
        "prompt": "page text",
        "schema": _schema(),
        "images": [],
        "timeout": 5,
        "model": None,
        "effort": "high",
        "cwd": tmp_path,
    }
    params.update(overrides)
    return client.extract_structured(**params)


def test_success_uses_supported_flags_and_env_isolation(fake_codex):
    client, log, tmp_path = fake_codex
    result = _extract(client, tmp_path)
    assert result.ok and result.provider == "codex"
    assert result.exit_code == 0
    assert result.structured["HEADLINE"]["Fund Name"]["value"] == "Acme Fund"
    assert result.session_id == "codex-sess-1"
    assert result.usage is not None and result.usage.input_tokens == 12
    assert result.total_cost_usd is None

    extraction = _logged_calls(log)[-1]
    argv = extraction["argv"]
    assert argv[0] == "exec"
    assert argv[-1] == "-"
    assert "--output-schema" in argv
    assert "--output-last-message" in argv
    assert "--json" in argv
    assert argv[argv.index("--model") + 1] == "gpt-local"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert "page text" not in " ".join(argv)
    assert extraction["has_openai_key"] is False
    assert extraction["has_anthropic_key"] is False


def test_invalid_json_is_error_result(fake_codex, monkeypatch):
    client, _, tmp_path = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", "invalid_json")
    result = _extract(client, tmp_path)
    assert not result.ok
    assert "invalid JSON object" in (result.error or "")


def test_schema_failure_is_error_result(fake_codex, monkeypatch):
    client, _, tmp_path = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", "schema_failure")
    result = _extract(client, tmp_path)
    assert not result.ok
    assert "missing required keys" in (result.error or "")


def test_nonzero_exit_is_error_result(fake_codex, monkeypatch):
    client, _, tmp_path = fake_codex
    monkeypatch.setenv("FAKE_CODEX_MODE", "nonzero")
    result = _extract(client, tmp_path)
    assert not result.ok
    assert result.exit_code == 9
    assert "simulated codex failure" in (result.error or "")


def test_timeout_kills_process_group(fake_codex, monkeypatch):
    client, _, tmp_path = fake_codex
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CODEX_MODE", "timeout")
    monkeypatch.setenv("FAKE_CODEX_CHILD_PID", str(pid_file))
    result = _extract(client, tmp_path, timeout=1)
    assert not result.ok
    assert "timed out" in (result.error or "")

    deadline = time.time() + 5
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("codex timeout did not terminate the child process")


def test_missing_executable_is_error_result(tmp_path):
    client = CodexCliClient(
        Config(
            pv_root=str(tmp_path / "not_pv"),
            output_dir=tmp_path / "output",
            db_path=tmp_path / "output" / "pv.db",
            codex_cli=CodexCliConfig(command=str(tmp_path / "missing-codex")),
        )
    )
    result = _extract(client, tmp_path)
    assert not result.ok
    assert "failed to launch" in (result.error or "")


def test_images_require_advertised_support(fake_codex):
    client, _, tmp_path = fake_codex
    image = tmp_path / "page-1.png"
    image.write_bytes(b"not really a png")
    result = _extract(client, tmp_path, images=[image])
    assert not result.ok
    assert "image attachment support" in (result.error or "")


def test_supported_images_are_attached_selectively(fake_codex, monkeypatch):
    client, log, tmp_path = fake_codex
    monkeypatch.setenv("FAKE_CODEX_IMAGE", "1")
    selected = tmp_path / "page-2.png"
    unselected = tmp_path / "page-3.png"
    selected.write_bytes(b"selected")
    unselected.write_bytes(b"unselected")
    result = _extract(client, tmp_path, images=[selected])
    assert result.ok
    argv = _logged_calls(log)[-1]["argv"]
    assert argv[argv.index("--image") + 1] == str(selected)
    assert str(unselected) not in argv
