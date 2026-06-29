"""Per-client deal-discovery learning (indexer/deal_learning.py).

Groundwork: the learning/profile tables exist after init_schema. Phase A.6:
the HARD layer (add/remove/merge/rename + delete-undo), the GENERALIZATION
property (a correction on deal Foo nudges a different new deal Bar under the
same client via a client-scoped layout prior, additive and capped at
learning.prior_bump), and the learning.enabled=False no-op gate.

In-memory _ingest([...]) lists mirror the test_deal_discovery.py idiom so no
build_fixture.py change is needed (protecting the back-compat pin).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.indexer.db import init_schema, open_db
from pv_extractor.indexer.deal_learning import (
    apply_feedback,
    cached_layout_priors,
    delete_correction,
    derive_layout_priors,
    list_corrections,
    record_correction,
)
from pv_extractor.indexer.deals import discover_deals, refresh_deals
from pv_extractor.indexer.derive import derive_record

PV_ROOT = "\\\\testsrv\\share\\PV"


def _ingest(paths: list[str]) -> tuple[sqlite3.Connection, Config]:
    config = Config(pv_root=PV_ROOT, db_path=Path("/tmp/unused.db"), output_dir=Path("/tmp"))
    config.llm.models_path = str(Path(__file__).parent.parent / "config" / "models.yaml")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    records = [
        derive_record(
            f"{PV_ROOT}\\{rel}",
            size_bytes=1000,
            modified_time=datetime(2026, 1, 15, 12, 0, 0),
            config=config,
        )
        for rel in paths
    ]
    db.insert_records(conn, records, 100)
    return conn, config


def _names(deals) -> set[str]:
    return {d.name for d in deals}


# A small two-deal client: one ordinary deal, one ordinary deal we will correct.
LEARN_FILES = [
    r"LearnCo\Acme\12.31.2025\Client\Acme Valuation Memo.pdf",
    r"LearnCo\Beta\12.31.2025\Client\Beta Valuation Memo.pdf",
]


# ---------------------------------------------------------------------------
# groundwork (kept)
# ---------------------------------------------------------------------------


def test_learning_tables_created(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "learning.db", str(tmp_path / "pv_root"))
    init_schema(conn)
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"deal_finder_feedback", "doc_type_profiles", "doc_search_feedback"} <= names


# ---------------------------------------------------------------------------
# (a) recording / listing / deleting corrections
# ---------------------------------------------------------------------------


def test_record_list_delete_roundtrip() -> None:
    conn, _ = _ingest(LEARN_FILES)
    try:
        record_correction(
            conn, client="LearnCo", deal="Acme", action="rename",
            payload={"new_name": "Acme Corp"},
        )
        rows = list_corrections(conn, "LearnCo")
        assert len(rows) == 1
        row = rows[0]
        assert {"id", "client", "deal", "action", "folder_path", "payload", "created_at"} <= set(row)
        assert row["action"] == "rename"
        assert row["payload"] == {"new_name": "Acme Corp"}  # parsed back to a dict
        assert delete_correction(conn, row["id"]) is True
        assert list_corrections(conn, "LearnCo") == []
        assert delete_correction(conn, row["id"]) is False  # already gone
    finally:
        conn.close()


def test_record_correction_rejects_unknown_action() -> None:
    conn, _ = _ingest(LEARN_FILES)
    try:
        with pytest.raises(ValueError):
            record_correction(conn, client="LearnCo", deal="Acme", action="frobnicate")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (b) HARD layer: exact edits replayed through refresh_deals + undo via delete
# ---------------------------------------------------------------------------


def test_hard_rename_reflected_in_refresh_and_undone_by_delete() -> None:
    conn, config = _ingest(LEARN_FILES)
    try:
        assert "Acme" in _names(discover_deals(conn, config, "LearnCo"))
        record_correction(
            conn, client="LearnCo", deal="Acme", action="rename",
            payload={"new_name": "Acme Corp"},
        )
        names = _names(refresh_deals(conn, config, ["LearnCo"])["LearnCo"])
        assert "Acme Corp" in names and "Acme" not in names
        # undo: delete the correction -> the rename evaporates on the next refresh
        cid = list_corrections(conn, "LearnCo")[0]["id"]
        assert delete_correction(conn, cid) is True
        names = _names(refresh_deals(conn, config, ["LearnCo"])["LearnCo"])
        assert "Acme" in names and "Acme Corp" not in names
    finally:
        conn.close()


def test_hard_add_folder_pins_unknown_deal() -> None:
    conn, config = _ingest(LEARN_FILES)
    try:
        record_correction(
            conn, client="LearnCo", deal="Hidden Co", action="add_folder",
            folder_path=r"LearnCo\_Admin\Hidden Co",
        )
        deals = {d.name: d for d in refresh_deals(conn, config, ["LearnCo"])["LearnCo"]}
        assert "Hidden Co" in deals
        forced = deals["Hidden Co"]
        assert forced.method == "learned"
        assert r"LearnCo\_Admin\Hidden Co" in forced.folder_paths
    finally:
        conn.close()


def test_hard_remove_folder_drops_deal() -> None:
    conn, config = _ingest(LEARN_FILES)
    try:
        record_correction(
            conn, client="LearnCo", deal="Beta", action="remove_folder",
            folder_path=r"LearnCo\Beta",
        )
        names = _names(refresh_deals(conn, config, ["LearnCo"])["LearnCo"])
        assert "Beta" not in names  # its only folder removed -> deal dropped
        assert "Acme" in names
    finally:
        conn.close()


def test_hard_merge_unions_paths() -> None:
    conn, config = _ingest(LEARN_FILES)
    try:
        record_correction(
            conn, client="LearnCo", deal="Acme", action="merge",
            payload={"into": "Beta"},
        )
        deals = {d.name: d for d in refresh_deals(conn, config, ["LearnCo"])["LearnCo"]}
        # deal='Acme' is the primary, payload 'into'='Beta' is the other: the
        # primary (Acme) survives carrying both folders, the other is dropped
        assert "Acme" in deals and "Beta" not in deals
        assert r"LearnCo\Acme" in deals["Acme"].folder_paths
        assert r"LearnCo\Beta" in deals["Acme"].folder_paths
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (c) GENERALIZATION property (the A3 headline): a correction on deal Foo
#     nudges a DIFFERENT deal Bar under the same client through a client-scoped
#     layout prior — additive, capped at learning.prior_bump.
# ---------------------------------------------------------------------------

# A client with exactly one admin-wrapped deal 'Bar'. A correction recorded for
# a DIFFERENT deal 'Foo' under an admin-ish path raises this client's
# admin_container prior, which then lifts Bar's confidence on the next refresh.
GENERALIZE_FILES = [
    r"GenCo\_Admin\Bar\12.31.2025\Client\Bar Valuation Memo.pdf",
]


def test_correction_generalizes_to_a_different_deal() -> None:
    conn, config = _ingest(GENERALIZE_FILES)
    try:
        before = {d.name: d.confidence for d in discover_deals(conn, config, "GenCo")}
        assert "Bar" in before
        baseline = before["Bar"]

        # correction is for a DIFFERENT deal 'Foo', under an admin path
        record_correction(
            conn, client="GenCo", deal="Foo", action="add_folder",
            folder_path=r"GenCo\_Admin\Foo",
        )
        deals = {d.name: d for d in refresh_deals(conn, config, ["GenCo"])["GenCo"]}
        bar = deals["Bar"]
        bump = config.deal_discovery.learning.prior_bump

        # a single correction nudges half the cap (cap * 1/2.0)
        expected = round(min(1.0, baseline + bump / 2.0), 4)
        assert bar.confidence == pytest.approx(expected)
        assert bar.confidence > baseline  # the prior generalized to a different deal
        # the nudge is recorded transparently in the evidence breakdown
        assert bar.evidence.components["learned_admin_container_prior"] == pytest.approx(bump / 2.0)
        # the client-scoped prior is cached for the read-only endpoints
        assert cached_layout_priors(conn, "GenCo") == {"admin_container": pytest.approx(bump / 2.0)}
    finally:
        conn.close()


def test_prior_is_additive_and_capped_at_prior_bump() -> None:
    conn, config = _ingest(GENERALIZE_FILES)
    try:
        baseline = {d.name: d.confidence for d in discover_deals(conn, config, "GenCo")}["Bar"]
        # many corrections of the same shape: the prior saturates AT the cap
        for i in range(6):
            record_correction(
                conn, client="GenCo", deal=f"Foo{i}", action="add_folder",
                folder_path=rf"GenCo\_Admin\Foo{i}",
            )
        priors = derive_layout_priors(conn, config, "GenCo")
        bump = config.deal_discovery.learning.prior_bump
        assert priors["admin_container"] == pytest.approx(bump)  # saturated at the cap
        deals = {d.name: d for d in refresh_deals(conn, config, ["GenCo"])["GenCo"]}
        bar = deals["Bar"]
        # additive and capped: Bar gains at most prior_bump
        assert bar.confidence == pytest.approx(round(min(1.0, baseline + bump), 4))
        assert bar.evidence.components["learned_admin_container_prior"] == pytest.approx(bump)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (d) gate: learning.enabled=False makes apply_feedback a no-op
# ---------------------------------------------------------------------------


def test_learning_disabled_is_a_no_op() -> None:
    conn, config = _ingest(GENERALIZE_FILES)
    try:
        record_correction(
            conn, client="GenCo", deal="Foo", action="add_folder",
            folder_path=r"GenCo\_Admin\Foo",
        )
        config2 = config.model_copy(deep=True)
        config2.deal_discovery.learning.enabled = False
        discovered = discover_deals(conn, config2, "GenCo")
        before = {d.name: d.confidence for d in discovered}
        out, markers = apply_feedback(discovered, conn, config2, "GenCo")
        assert markers == []  # no hard edits, no priors applied
        assert {d.name: d.confidence for d in out} == before  # unchanged
    finally:
        conn.close()
