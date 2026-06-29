"""Shared pytest fixtures: project paths, default config, synthetic PV tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = PROJECT_ROOT / "reference"
FIXTURE_PV_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "pv_root"

sys.path.insert(0, str(PROJECT_ROOT / "tests"))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def reference_dir() -> Path:
    return REFERENCE_DIR


@pytest.fixture(scope="session")
def master_workbook_path() -> Path:
    return REFERENCE_DIR / "master_index_v4.xlsx"


@pytest.fixture(scope="session")
def strip_xlsx_path() -> Path:
    return REFERENCE_DIR / "strip_from_index.xlsx"


@pytest.fixture(scope="session")
def default_config():
    from pv_extractor.config import LlmConfig, load_config

    config = load_config(PROJECT_ROOT / "config.yaml")
    # config.yaml is a live file the GUI edits (mode, budget, locations...);
    # tests asserting DEFAULT llm semantics must not depend on the operator's
    # current choices. Keep the resolved models_path, reset the rest.
    config.llm = LlmConfig(models_path=config.llm.models_path)
    return config


@pytest.fixture(scope="session")
def fixture_pv_root() -> Path:
    """Synthetic PV tree, rebuilt once per session (deterministic content
    and mtimes)."""
    from fixtures.build_fixture import build_fixture

    build_fixture(FIXTURE_PV_ROOT)
    return FIXTURE_PV_ROOT


@pytest.fixture(scope="session")
def phase2_env(fixture_pv_root: Path, tmp_path_factory: pytest.TempPathFactory):
    """Phase-2 pipeline environment: the fixture tree scanned into a session
    SQLite index plus a config factory (fresh output dir per request)."""
    from pv_extractor.config import LlmConfig, load_config
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.scan_tree import scan_tree

    base = tmp_path_factory.mktemp("phase2")
    config = load_config(PROJECT_ROOT / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = base / "output"
    config.db_path = base / "output" / "pv_index.db"
    # decouple from the operator's live llm choices (see default_config)
    config.llm = LlmConfig(models_path=config.llm.models_path)

    conn = open_db(config.db_path, config.pv_root)
    init_schema(conn)
    scan_tree(conn, str(fixture_pv_root), config)
    conn.close()
    return config
