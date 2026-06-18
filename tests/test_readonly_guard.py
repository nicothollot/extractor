"""D1 strict read-only rule: a source grep proving no module outside
io_guard.py opens write handles or touches destructive filesystem APIs,
plus runtime refusal checks for guarded_open_write and db.open_db."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pv_extractor.indexer import db
from pv_extractor.io_guard import ReadOnlyViolation, guarded_open_write, open_read

PROD_SHARE_TARGET = "\\\\hlhz\\dfs\\nyfva\\PV\\x\\y.txt"

# Any open( / .open( call site; \w lookbehind keeps guarded_open_write( out.
_OPEN_CALL_RE = re.compile(r"(?<!\w)open\(")
# A quoted literal that looks like a file mode within the call's argument span.
_MODE_LITERAL_RE = re.compile(r"['\"]([rwabxt+]{1,4})['\"]")
_WRITE_MODE_CHARS = set("wax+")
_FORBIDDEN_API_RE = re.compile(
    r"\.write_text\(|\.write_bytes\(|shutil\.copy|shutil\.move"
    r"|os\.remove|os\.unlink|os\.rename"
)


def _src_files(project_root: Path) -> list[Path]:
    files = sorted((project_root / "src" / "pv_extractor").rglob("*.py"))
    assert files, "src/pv_extractor python files not found"
    return files


# --------------------------------------------------------------------------
# Source grep
# --------------------------------------------------------------------------


def test_no_write_mode_open_outside_io_guard(project_root: Path) -> None:
    """Every open( call carrying a literal mode with w/a/x/+ must live in
    io_guard.py. The mode literal is searched in a window after the call so
    multi-line calls and mode= keyword forms are covered too."""
    offenders: list[tuple[str, str]] = []
    for path in _src_files(project_root):
        text = path.read_text(encoding="utf-8")
        for call in _OPEN_CALL_RE.finditer(text):
            window = text[call.end() : call.end() + 200]
            for literal in _MODE_LITERAL_RE.finditer(window):
                if set(literal.group(1)) & _WRITE_MODE_CHARS:
                    offenders.append((path.name, literal.group(1)))
    assert all(name == "io_guard.py" for name, _ in offenders), offenders


def test_no_destructive_filesystem_apis_in_src(project_root: Path) -> None:
    for path in _src_files(project_root):
        hits = _FORBIDDEN_API_RE.findall(path.read_text(encoding="utf-8"))
        assert hits == [], f"{path}: forbidden write/delete API usage {hits}"


# --------------------------------------------------------------------------
# Runtime refusals
# --------------------------------------------------------------------------


def test_production_share_refused_even_when_pv_root_points_elsewhere(tmp_path: Path) -> None:
    """The hardcoded production guard fires regardless of configuration."""
    with pytest.raises(ReadOnlyViolation):
        guarded_open_write(PROD_SHARE_TARGET, pv_root=str(tmp_path))


def test_configured_pv_root_refused(fixture_pv_root: Path) -> None:
    target = fixture_pv_root / "Angelo Gordon" / "Accell" / "intruder.txt"
    with pytest.raises(ReadOnlyViolation):
        guarded_open_write(str(target), pv_root=str(fixture_pv_root))
    assert not target.exists()


def test_guarded_open_write_allows_off_share_outputs(
    tmp_path: Path, fixture_pv_root: Path
) -> None:
    out = tmp_path / "out" / "export.txt"
    with guarded_open_write(out, pv_root=str(fixture_pv_root)) as fh:
        fh.write("ok")
    assert out.read_text(encoding="utf-8") == "ok"


def test_open_db_refuses_db_path_under_pv_root(fixture_pv_root: Path) -> None:
    with pytest.raises(ReadOnlyViolation):
        db.open_db(fixture_pv_root / "pv_index.db", str(fixture_pv_root))


def test_open_read_is_binary_read_only(fixture_pv_root: Path) -> None:
    target = (
        fixture_pv_root / "Angelo Gordon" / "Accell" / "(5) 1.31.25" / "Client"
        / "Accell Valuation Memo 1.31.25 vf.pdf"
    )
    with open_read(target) as fh:
        assert fh.mode == "rb"
        assert fh.read(5) == b"%PDF-"
