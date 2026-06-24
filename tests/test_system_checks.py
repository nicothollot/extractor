"""Tests for system/claude_code.py startup checks and scripts/bootstrap.py
pure helpers. No real claude CLI, no real venv creation: shutil.which and
subprocess.run are monkeypatched; all output lands under tmp_path."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pv_extractor.config import ClaudeCodeConfig, Config
from pv_extractor.system import claude_code

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, **claude_kwargs) -> Config:
    """Config aimed entirely at tmp_path; never touches the repo's output/."""
    return Config(
        pv_root=str(tmp_path / "fake_pv_root"),
        output_dir=tmp_path / "output",
        db_path=tmp_path / "output" / "pv_index.db",
        claude_code=ClaudeCodeConfig(**claude_kwargs),
    )


def jsonl_lines(config: Config) -> list[dict]:
    path = Path(config.output_dir) / "logs" / "startup_checks.jsonl"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


class FakeProcessRunner:
    """subprocess.run stand-in: records argv, returns canned outputs keyed by
    the argv tail (everything after the executable), optionally times out."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str]] | None = None,
        timeout_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.responses = responses or {}
        self.timeout_keys = timeout_keys
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=False, text=False, timeout=None,
                 encoding=None, errors=None):
        self.calls.append(list(argv))
        key = " ".join(argv[1:])
        if key in self.timeout_keys:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        returncode, stdout = self.responses.get(key, (1, ""))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def forbid(*_args, **_kwargs):
    raise AssertionError("must not be called in this scenario")


# ---------------------------------------------------------------------------
# run_startup_checks
# ---------------------------------------------------------------------------


def test_disabled_by_config_runs_no_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", forbid)
    monkeypatch.setattr(claude_code.subprocess, "run", forbid)
    config = make_config(tmp_path, allow_cli_usage=False)

    snapshot = claude_code.run_startup_checks(config)

    assert len(snapshot.results) == 1
    result = snapshot.results[0]
    assert result.check == "claude_cli"
    assert result.detail == "disabled by config"
    assert snapshot.claude_path is None
    assert snapshot.version is None
    assert snapshot.auth_status is None
    assert snapshot.updated is False
    lines = jsonl_lines(config)
    assert len(lines) == 1
    assert lines[0]["results"][0]["detail"] == "disabled by config"


def test_missing_cli_records_remediation_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(claude_code.subprocess, "run", forbid)
    config = make_config(tmp_path)

    snapshot = claude_code.run_startup_checks(config)

    assert snapshot.claude_path is None
    assert len(snapshot.results) == 1
    result = snapshot.results[0]
    assert result.check == "claude_cli"
    assert result.ok is False
    assert "install" in result.detail.lower()
    assert len(jsonl_lines(config)) == 1


def test_present_records_version_and_auth_without_update(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    runner = FakeProcessRunner(
        {"--version": (0, "2.1.0 (Claude Code)\n"), "auth status": (0, "Authenticated\n")}
    )
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path, auto_update_on_start=False)

    snapshot = claude_code.run_startup_checks(config)

    assert snapshot.claude_path == "/usr/local/bin/claude"
    assert snapshot.version == "2.1.0 (Claude Code)"
    assert snapshot.auth_status == "Authenticated"
    assert snapshot.updated is False
    assert ["claude", "--version"] in runner.calls
    assert ["claude", "auth", "status"] in runner.calls
    assert ["claude", "update"] not in runner.calls
    assert all(r.ok for r in snapshot.results)


def test_auto_update_on_start_invokes_update(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    runner = FakeProcessRunner(
        {
            "--version": (0, "2.1.0"),
            "auth status": (0, "Authenticated"),
            "update": (0, "updated to 2.2.0"),
        }
    )
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path, auto_update_on_start=True)

    snapshot = claude_code.run_startup_checks(config)

    assert ["claude", "update"] in runner.calls
    assert snapshot.updated is True
    update_results = [r for r in snapshot.results if r.check == "update"]
    assert len(update_results) == 1 and update_results[0].ok


def test_timeout_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    runner = FakeProcessRunner(
        {"auth status": (0, "Authenticated")}, timeout_keys=frozenset({"--version"})
    )
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path)

    snapshot = claude_code.run_startup_checks(config)  # must not raise

    version_results = [r for r in snapshot.results if r.check == "version"]
    assert len(version_results) == 1
    assert version_results[0].ok is False
    assert "timed out" in version_results[0].detail
    assert snapshot.version is None
    assert snapshot.auth_status == "Authenticated"  # later checks still ran


def test_nonzero_exit_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    runner = FakeProcessRunner({"--version": (0, "2.1.0"), "auth status": (1, "")})
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path)

    snapshot = claude_code.run_startup_checks(config)

    auth_results = [r for r in snapshot.results if r.check == "auth_status"]
    assert len(auth_results) == 1
    assert auth_results[0].ok is False
    assert "exit 1" in auth_results[0].detail
    assert snapshot.auth_status is None


def test_jsonl_appends_one_valid_object_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    runner = FakeProcessRunner({"--version": (0, "2.1.0"), "auth status": (0, "Authenticated")})
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path)

    claude_code.run_startup_checks(config)
    claude_code.run_startup_checks(config)

    lines = jsonl_lines(config)
    assert len(lines) == 2
    for obj in lines:
        assert isinstance(obj, dict)
        assert {"checked_at", "claude_path", "version", "auth_status", "updated", "results"} <= set(obj)
        assert isinstance(obj["results"], list) and obj["results"]


def test_respects_configured_command_name(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake_which(cmd: str) -> str:
        seen.append(cmd)
        return "/opt/claude-custom"

    monkeypatch.setattr(claude_code.shutil, "which", fake_which)
    runner = FakeProcessRunner({"--version": (0, "2.1.0"), "auth status": (0, "ok")})
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    config = make_config(tmp_path, command="claude-custom")

    claude_code.run_startup_checks(config)

    assert seen == ["claude-custom"]
    assert all(argv[0] == "claude-custom" for argv in runner.calls)


# ---------------------------------------------------------------------------
# bootstrap.py pure helpers (no venv creation, no pip)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bootstrap():
    path = PROJECT_ROOT / "scripts" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("pv_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_install_missing_deps_false(bootstrap, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("first_run:\n  install_missing_deps: false   # do not auto-install\n", encoding="utf-8")
    assert bootstrap.read_install_missing_deps(cfg) is False


def test_read_install_missing_deps_true_and_quoted(bootstrap, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("first_run:\n  install_missing_deps: true\n", encoding="utf-8")
    assert bootstrap.read_install_missing_deps(cfg) is True
    cfg.write_text("first_run:\n  install_missing_deps: 'false'\n", encoding="utf-8")
    assert bootstrap.read_install_missing_deps(cfg) is False


def test_read_install_missing_deps_defaults_to_true(bootstrap, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pv_root: 'x'\n", encoding="utf-8")  # key absent
    assert bootstrap.read_install_missing_deps(cfg) is True
    assert bootstrap.read_install_missing_deps(tmp_path / "missing.yaml") is True


def test_extras_spec(bootstrap):
    assert bootstrap.extras_spec(False) == ".[dev]"
    assert bootstrap.extras_spec(True) == ".[dev,gui]"


def test_required_imports_add_gui_deps_only_for_gui_profile(bootstrap):
    assert "fastapi" not in bootstrap.required_imports(False)
    gui_imports = bootstrap.required_imports(True)
    assert "fastapi" in gui_imports
    assert "uvicorn" in gui_imports
    assert "ruamel.yaml" in gui_imports


def test_venv_python_path_layout(bootstrap, tmp_path):
    py = bootstrap.venv_python_path(tmp_path / ".venv")
    if sys.platform == "win32":
        assert py == tmp_path / ".venv" / "Scripts" / "python.exe"
    else:
        assert py == tmp_path / ".venv" / "bin" / "python"


def test_missing_imports_empty_on_complete_venv(bootstrap):
    """The test venv has every runtime dependency installed, so the probe
    must report nothing missing (this is what lets bootstrap skip pip)."""
    assert bootstrap.missing_imports(Path(sys.executable)) == []


def test_missing_imports_detects_absent_module(bootstrap, monkeypatch):
    """A venv from an older phase (some dependencies absent) must be
    reported incomplete so bootstrap re-installs instead of skipping."""
    monkeypatch.setattr(
        bootstrap, "REQUIRED_IMPORTS", ("pv_extractor", "module_that_does_not_exist_xyz")
    )
    assert bootstrap.missing_imports(Path(sys.executable)) == ["module_that_does_not_exist_xyz"]


def test_required_imports_cover_pyproject_runtime_deps(bootstrap):
    """Every [project] dependency in pyproject.toml must have its import
    name probed — otherwise a future phase reintroduces the stale-venv bug."""
    import tomllib

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dist_to_import = {
        "openpyxl": "openpyxl", "pymupdf": "fitz", "rapidfuzz": "rapidfuzz",
        "pydantic": "pydantic", "typer": "typer", "python-dateutil": "dateutil",
        "rich": "rich", "pyyaml": "yaml", "pdfplumber": "pdfplumber",
        "python-docx": "docx", "python-pptx": "pptx", "rapidocr": "rapidocr",
        "onnxruntime": "onnxruntime",
    }
    for requirement in pyproject["project"]["dependencies"]:
        dist = requirement.split("==")[0].strip().lower()
        assert dist in dist_to_import, f"unknown dependency {dist!r}: extend this map"
        assert dist_to_import[dist] in bootstrap.REQUIRED_IMPORTS, dist


def test_remediation_texts_are_actionable(bootstrap, tmp_path):
    install = bootstrap.install_remediation(tmp_path / ".venv" / "bin" / "python", with_gui=True)
    assert "pip install -e" in install
    assert ".[dev,gui]" in install
    assert "install_missing_deps" in install

    node = bootstrap.node_remediation()
    assert "nodejs.org" in node
    assert "npm" in node

    python = bootstrap.python_remediation()
    assert "3.12" in python
