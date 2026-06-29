"""File-based LLM output (the model WRITES answers.json instead of returning it
through the --json-schema StructuredOutput tool). Covers the pure read/validate/
classify helpers everywhere, plus an end-to-end argv/permission/repair contract
against a fake `claude` executable (POSIX-only, like the rest of test_llm_client).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.claude_code_client import (
    ANSWER_FILENAME,
    _is_answer_file_error,
    _normalize_v5_answer,
    _read_answer_file,
    build_answer_file_instruction,
)
from pv_extractor.llm.schema_builder import build_response_schema

_FIELDS = load_schema_fields()[:6]
_SCHEMA_DOC = build_response_schema(_FIELDS)
_FIELD_IDS = list(_SCHEMA_DOC["properties"]["not_found"]["items"]["enum"])


def _all_not_found() -> dict:
    """A minimal valid v5 answer: every requested field marked not_found."""
    return {
        "schema_version": 5,
        "scope": {"type": "deal", "id": "D01"},
        "values": [],
        "not_found": list(_FIELD_IDS),
        "conflicts": [],
        "warnings": [],
    }


# --- pure helpers (run on every platform) ----------------------------------

def test_missing_answer_file_is_an_answer_file_error(tmp_path):
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert structured is None
    assert _is_answer_file_error(error) and "was not created" in error


def test_empty_answer_file_reports_empty(tmp_path):
    (tmp_path / ANSWER_FILENAME).write_text("   \n", encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert structured is None and _is_answer_file_error(error) and "is empty" in error


def test_invalid_json_answer_file_reports_not_valid_json(tmp_path):
    (tmp_path / ANSWER_FILENAME).write_text("definitely not json", encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert structured is None and _is_answer_file_error(error) and "not valid JSON" in error


def test_valid_answer_file_round_trips(tmp_path):
    (tmp_path / ANSWER_FILENAME).write_text(json.dumps(_all_not_found()), encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert error is None
    assert structured is not None and structured["schema_version"] == 5
    assert set(structured["not_found"]) == set(_FIELD_IDS)


def test_missing_optional_keys_are_normalized_not_rejected(tmp_path):
    # The model returns only values + not_found; scope/conflicts/warnings absent.
    sparse = {"values": [], "not_found": list(_FIELD_IDS)}
    (tmp_path / ANSWER_FILENAME).write_text(json.dumps(sparse), encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert error is None
    assert structured["scope"] and structured["conflicts"] == [] and structured["warnings"] == []


def test_fenced_json_is_accepted(tmp_path):
    body = "```json\n" + json.dumps(_all_not_found()) + "\n```"
    (tmp_path / ANSWER_FILENAME).write_text(body, encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert error is None and structured is not None


def test_unknown_field_id_fails_schema_validation(tmp_path):
    answer = _all_not_found()
    answer["not_found"] = ["NOT_A_REAL_FIELD_ID"]
    (tmp_path / ANSWER_FILENAME).write_text(json.dumps(answer), encoding="utf-8")
    structured, error = _read_answer_file(tmp_path, _SCHEMA_DOC)
    assert structured is None
    assert _is_answer_file_error(error) and "failed schema validation" in error


def test_normalize_is_non_destructive():
    obj = {"schema_version": 5, "values": [{"field_id": "X"}], "warnings": ["w"]}
    out = _normalize_v5_answer(obj)
    assert out["values"] == [{"field_id": "X"}]  # untouched
    assert out["warnings"] == ["w"]  # untouched
    assert out["not_found"] == [] and out["conflicts"] == []  # filled


def test_build_instruction_initial_vs_repair():
    initial = build_answer_file_instruction()
    assert ANSWER_FILENAME in initial and "Write tool" in initial
    repair = build_answer_file_instruction(resume=True, reason="answer file was empty")
    assert "Overwrite" in repair and "answer file was empty" in repair


def test_transport_error_is_not_an_answer_file_error():
    assert not _is_answer_file_error("exit 3: Usage limit reached")
    assert not _is_answer_file_error("timed out after 600s")
    assert _is_answer_file_error("answer file answers.json is empty")


def test_write_readable_extraction_one_row_per_field(tmp_path):
    """The human-readable companion dump has one row per field (keyed by header),
    pretty-printed, found values before not_found, with a summary count."""
    from pv_extractor.config import Config
    from pv_extractor.llm.escalate import _write_readable_extraction

    config = Config(pv_root=str(tmp_path / "not_pv"), output_dir=tmp_path / "out",
                    db_path=tmp_path / "out" / "pv.db")
    fields = _FIELDS
    h0, h1 = fields[0].header, fields[1].header
    decoded = {
        h0: {"value": "United States", "unit": None, "document_id": "D01", "page": 1,
             "model_confidence": 0.91, "verbatim_quote": "Country: United States", "not_found": False},
        h1: {"value": None, "not_found": True, "verbatim_quote": ""},
    }
    _write_readable_extraction(tmp_path, "w1", decoded, fields, config)

    out = json.loads((tmp_path / "extracted_w1.json").read_text(encoding="utf-8"))
    assert out["summary"] == {"fields_total": 2, "fields_with_value": 1, "fields_not_found": 1}
    assert [r["field"] for r in out["fields"]] == [h0, h1]  # found first, then not_found
    assert out["fields"][0]["value"] == "United States" and out["fields"][0]["page"] == 1
    assert out["fields"][1]["not_found"] is True
    # pretty-printed = multi-line (one field object readable per block)
    assert (tmp_path / "extracted_w1.json").read_text(encoding="utf-8").count("\n") > 5


# --- end-to-end against a fake `claude` (POSIX-only) ------------------------

_posix_only = pytest.mark.skipif(
    os.name == "nt", reason="fake-CLI shebang scripts are POSIX-only"
)

_FAKE_CLAUDE_FILE = f"""\
#!{sys.executable}
import json, os, sys

argv = sys.argv[1:]
if "--help" in argv:
    print("Usage: claude [options]\\n  -p, --print\\n  --output-format <format>\\n"
          "  --json-schema <schema>\\n  --model <model>\\n  --effort <level>\\n"
          "  --allowedTools <tools>\\n  --permission-mode <mode>\\n  --resume <id>\\n"
          "  --exclude-dynamic-system-prompt-sections")
    sys.exit(0)
if argv[:2] == ["auth", "status"]:
    print("Authenticated (fake)"); sys.exit(0)
if "--version" in argv:
    print("9.9.9 (fake)"); sys.exit(0)

prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({{"argv": argv, "is_resume": "--resume" in argv}}) + "\\n")

import re as _re
_m = _re.search(r"answers[\\w.-]*\\.json", prompt)
answer_path = os.path.join(os.getcwd(), _m.group(0) if _m else "answers.json")
good = json.dumps({{"schema_version": 5, "scope": {{"type": "deal", "id": "D01"}},
                   "values": [], "not_found": {json.dumps(_FIELD_IDS)},
                   "conflicts": [], "warnings": []}})

mode = os.environ.get("FAKE_CLAUDE_FILE_MODE", "ok")
if mode == "ok":
    open(answer_path, "w").write(good)
elif mode == "bad_then_good":
    # first call writes broken JSON; the same-session --resume round fixes it
    if "--resume" in argv:
        open(answer_path, "w").write(good)
    else:
        open(answer_path, "w").write("not json at all")
elif mode == "never":
    pass  # never writes the file

# the model's final turn — a result envelope with session_id (no structured_output)
print(json.dumps({{"type": "result", "subtype": "success", "result": "done",
                  "session_id": "sess-file-001",
                  "usage": {{"input_tokens": 100, "output_tokens": 20}},
                  "total_cost_usd": 0.01}}))
"""


@pytest.fixture()
def fake_file_env(tmp_path, monkeypatch):
    from pv_extractor.config import ClaudeCodeConfig, Config
    from pv_extractor.llm.claude_code_client import ClaudeCodeClient

    binary = tmp_path / "claude"
    binary.write_text(_FAKE_CLAUDE_FILE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    config = Config(
        pv_root=str(tmp_path / "not_pv"), output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv.db",
        claude_code=ClaudeCodeConfig(command=str(binary), default_timeout_seconds=30),
    )
    config.llm.answer_file_repair_rounds = 2
    client = ClaudeCodeClient(config)
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(_SCHEMA_DOC), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return client, schema_path, log, work


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@_posix_only
def test_file_output_reads_written_answer_and_sets_permissions(fake_file_env, monkeypatch):
    client, schema_path, log, work = fake_file_env
    monkeypatch.setenv("FAKE_CLAUDE_FILE_MODE", "ok")
    result = client.extract_json_file(
        job_id="pv-RUN-MEMO-t0", prompt="extract these fields", schema_path=schema_path,
        model="sonnet", effort="medium", cwd=work,
    )
    assert result.ok and result.structured is not None
    assert result.structured["schema_version"] == 5
    argv = _calls(log)[0]["argv"]
    assert "--json-schema" not in argv  # no StructuredOutput tool
    assert "Write" in argv and "Read" in argv  # the model can read docs + write the file
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


@_posix_only
def test_file_output_repairs_bad_file_in_same_session(fake_file_env, monkeypatch):
    client, schema_path, log, work = fake_file_env
    monkeypatch.setenv("FAKE_CLAUDE_FILE_MODE", "bad_then_good")
    result = client.extract_json_file(
        job_id="pv-RUN-MEMO-t0", prompt="extract these fields", schema_path=schema_path,
        model="sonnet", effort="medium", cwd=work,
    )
    assert result.ok and result.structured is not None
    calls = _calls(log)
    assert len(calls) == 2  # first call + one same-session repair round
    assert calls[1]["is_resume"] is True


@_posix_only
def test_file_output_gives_up_after_repair_rounds(fake_file_env, monkeypatch):
    client, schema_path, log, work = fake_file_env
    monkeypatch.setenv("FAKE_CLAUDE_FILE_MODE", "never")
    result = client.extract_json_file(
        job_id="pv-RUN-MEMO-t0", prompt="extract these fields", schema_path=schema_path,
        model="sonnet", effort="medium", cwd=work,
    )
    assert not result.ok
    assert _is_answer_file_error(result.error) and "was not created" in result.error
    # one initial call + answer_file_repair_rounds repair attempts
    assert len(_calls(log)) == 1 + client._answer_file_repair_rounds
