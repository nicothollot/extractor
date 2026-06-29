"""Multi-Search backend tests (Phase C.4).

Covers the three new pure/synchronous seams that the firm-level batch search is
built on, WITHOUT re-implementing or launching any pipeline machinery:

  * run() per-slot path (RunSlot fan-out): a run driven by ``slots=[...]`` locates
    each slot with ITS OWN period/doc_type, lanes events by firm ('group'), and
    contains a per-slot locate ValueError as an ERROR coverage entry instead of
    aborting the batch. The slots=None path stays byte-for-byte identical.
  * multi_search_service.expand_slots: a request fans out into one RunSlot per
    (firm x deal x doc_type); explicit deals vs all-discovered; builtin doc_type
    -> doc_type_spec None; learned profile slug -> resolved DocTypeSpec; the
    config.multi_search.max_firms cap drops (logged) overflow firms.
  * selection_service.slot_selection misfiled surfacing under
    enhanced_period_check: an in-file as-of date that disagrees with the target
    flags the slot MISFILED and carries the document's TRUE period — never
    fabricated (off when the check is disabled / dates agree).

The misfiled scenario reuses the REAL rich Accell memo PDF (whose in-file as-of
is 2025-01-31) but registers it in an in-memory index under a deliberately wrong
Nov-2024 period folder, so verify_and_rerank's in-file cross-check rejects it
and exposes the true detected period. No file under pv_root is ever written.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

from pv_extractor.api.multi_search_service import expand_slots
from pv_extractor.api.schemas import MultiSearchFirm, MultiSearchSelectionRequest
from pv_extractor.api.selection_service import slot_selection
from pv_extractor.config import LlmConfig, load_config
from pv_extractor.indexer import db
from pv_extractor.indexer.derive import derive_record
from pv_extractor.indexer.db import init_schema, open_db
from pv_extractor.indexer.periods import period_label
from pv_extractor.indexer.scan_tree import scan_tree
from pv_extractor.models import DocType, DocTypeSpec
from pv_extractor.run import RunControl, RunSlot, run
from pv_extractor.search.doc_type_spec import save_profile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# environment helpers
# ---------------------------------------------------------------------------


def _config(fixture_pv_root: Path, base: Path):
    config = load_config(PROJECT_ROOT / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = base / "output"
    config.db_path = base / "output" / "pv_index.db"
    # decouple from the operator's live llm choices (see conftest.default_config)
    config.llm = LlmConfig(models_path=config.llm.models_path)
    return config


@pytest.fixture(scope="module")
def scanned_env(fixture_pv_root, tmp_path_factory):
    """The fixture tree scanned into a real SQLite index (locate/verify read
    the real PDFs) plus a config pointing at it."""
    base = tmp_path_factory.mktemp("multi")
    config = _config(fixture_pv_root, base)
    conn = open_db(config.db_path, config.pv_root)
    init_schema(conn)
    scan_tree(conn, str(fixture_pv_root), config)
    conn.close()
    return config


# ---------------------------------------------------------------------------
# 1+2. run() per-slot path  (and slots=None byte-for-byte equivalence)
# ---------------------------------------------------------------------------


def test_run_slots_locates_each_slot_with_its_own_period(scanned_env) -> None:
    """A run driven by RunSlots spanning TWO firms with DIFFERENT periods
    resolves the right document per (firm, period); events lane by firm and the
    run_started payload reports scope='slots' + firm/slot counts."""
    config = scanned_env
    events: list[tuple[str, dict]] = []
    control = RunControl(on_event=lambda name, fields: events.append((name, fields)))
    slots = [
        RunSlot(client="Angelo Gordon", deal="Accell", period="2025-01-31"),
        RunSlot(
            client="Apollo Global Management",
            deal="Summit Ridge Energy",
            period="2026-03-31",
        ),
    ]
    report = run(config, scope="all", period="", dry_run=True, control=control, slots=slots)

    by_deal = {c.deal: c for c in report.coverage}
    assert by_deal["Accell"].status == "FOUND"
    assert by_deal["Summit Ridge Energy"].status == "FOUND"
    # the per-period winners are the right files (different firms, different periods)
    assert "1.31.25" in by_deal["Accell"].detail
    assert "03-31-2026" in by_deal["Summit Ridge Energy"].detail

    run_started = next(f for n, f in events if n == "run_started")
    assert run_started["scope"] == "slots"
    assert run_started["slots"] == 2
    assert run_started["firms"] == 2

    # every slots-path stage event carries the firm 'group' lane label
    locate_events = [f for n, f in events if n == "stage" and f.get("stage") == "locate"]
    assert {f["group"] for f in locate_events} == {
        "Angelo Gordon",
        "Apollo Global Management",
    }


def test_run_slots_bad_period_is_per_slot_error_not_abort(scanned_env) -> None:
    """A locate ValueError (unparseable period) in the slots path is CAUGHT per
    slot: that slot reports ERROR while the rest of the batch still resolves.
    (Contrast with the slots=None path, where the same error aborts the run.)"""
    config = scanned_env
    slots = [
        RunSlot(client="Angelo Gordon", deal="Accell", period="not-a-real-period"),
        RunSlot(client="Apollo Global Management", deal="Summit Ridge Energy", period="2026-03-31"),
    ]
    report = run(config, scope="all", period="", dry_run=True, slots=slots)
    by_deal = {c.deal: c for c in report.coverage}
    assert by_deal["Accell"].status == "ERROR"
    assert by_deal["Accell"].detail  # the ValueError message rides on the coverage detail
    # the rest of the batch still ran
    assert by_deal["Summit Ridge Energy"].status == "FOUND"


def test_run_wide_bad_period_still_aborts(scanned_env) -> None:
    """Regression guard: the legacy (slots=None) path keeps propagating a bad
    period ValueError — the slots-path containment must NOT change this."""
    config = scanned_env
    with pytest.raises(ValueError):
        run(
            config,
            scope="deal",
            client="Angelo Gordon",
            deal="Accell",
            period="not-a-real-period",
            dry_run=True,
        )


def test_slots_none_equals_equivalent_single_slot(scanned_env) -> None:
    """SINGLE-FIRM UNCHANGED: a run-wide (slots=None) deal run and the
    equivalent one-slot run produce the same coverage outcome, and the run-wide
    path emits NO 'group' field on any event (byte-for-byte vs legacy)."""
    config = scanned_env

    wide_events: list[tuple[str, dict]] = []
    wide = run(
        config,
        scope="deal",
        client="Angelo Gordon",
        deal="Accell",
        period="2025-01-31",
        dry_run=True,
        control=RunControl(on_event=lambda n, f: wide_events.append((n, f))),
    )
    slotted = run(
        config,
        scope="all",
        period="",
        dry_run=True,
        slots=[RunSlot(client="Angelo Gordon", deal="Accell", period="2025-01-31")],
    )

    assert [(c.client, c.deal, c.status) for c in wide.coverage] == [
        (c.client, c.deal, c.status) for c in slotted.coverage
    ]
    # the run-wide path never emits a 'group' field (legacy event shape)
    assert all("group" not in fields for _, fields in wide_events)
    wide_started = next(f for n, f in wide_events if n == "run_started")
    assert wide_started["scope"] == "deal"
    assert "slots" not in wide_started


# ---------------------------------------------------------------------------
# 3. expand_slots fan-out
# ---------------------------------------------------------------------------


def test_expand_slots_fans_out_deals_and_doc_types(scanned_env) -> None:
    """Two firms (explicit deals + all-discovered), mixed builtin + profile
    doc_types -> a flat (firm x deal x doc_type) RunSlot list; builtin doc_type
    -> doc_type_spec None; learned profile slug -> resolved DocTypeSpec."""
    config = scanned_env
    conn = open_db(config.db_path, config.pv_root)
    try:
        # a learned profile (NOT a builtin DocType enum value)
        save_profile(
            conn,
            DocTypeSpec(
                slug="my-cap-tables",
                label="My Cap Tables",
                filename_include=["cap table", "capitalization"],
            ),
        )
        request = MultiSearchSelectionRequest(
            firms=[
                MultiSearchFirm(
                    client="Angelo Gordon",
                    deals=["Accell"],  # explicit single deal
                    period="2025-01-31",
                    doc_types=["valuation_memo", "my-cap-tables"],  # builtin + profile slug
                ),
                MultiSearchFirm(
                    client="Apollo Global Management",
                    deals=[],  # empty == all discovered
                    period="2026-03-31",
                    doc_types=["any_client_valuation_doc"],
                ),
            ]
        )
        slots = expand_slots(conn, config, request)
    finally:
        conn.close()

    ag = [s for s in slots if s.client == "Angelo Gordon"]
    # explicit deal x two doc_types -> exactly two slots, both on the same deal
    assert len(ag) == 2
    assert {s.deal for s in ag} == {"Accell"}
    builtin_slot = next(s for s in ag if s.doc_type is DocType.valuation_memo)
    assert builtin_slot.doc_type_spec is None  # builtin -> no spec
    profile_slot = next(s for s in ag if s.doc_type_spec is not None)
    assert profile_slot.doc_type is DocType.any_client_valuation_doc  # profile -> broad builtin + spec
    assert profile_slot.doc_type_spec.slug == "my-cap-tables"

    # the all-discovered firm fans out to one slot per discovered deal
    apollo = [s for s in slots if s.client == "Apollo Global Management"]
    assert len(apollo) >= 2
    assert {"Summit Ridge Energy", "Hyperoptic"} <= {s.deal for s in apollo}
    assert all(s.firm == s.client for s in slots)
    assert all(s.period == "2026-03-31" for s in apollo)


def test_expand_slots_unknown_slug_falls_back_to_broad_builtin(scanned_env, caplog) -> None:
    """An unknown doc-type slug logs (never silent) and falls back to
    any_client_valuation_doc with no spec, rather than erroring."""
    config = scanned_env
    conn = open_db(config.db_path, config.pv_root)
    try:
        request = MultiSearchSelectionRequest(
            firms=[
                MultiSearchFirm(
                    client="Angelo Gordon",
                    deals=["Accell"],
                    period="2025-01-31",
                    doc_types=["no-such-slug-xyz"],
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="pv_extractor.api.multi_search_service"):
            slots = expand_slots(conn, config, request)
    finally:
        conn.close()
    assert len(slots) == 1
    assert slots[0].doc_type is DocType.any_client_valuation_doc
    assert slots[0].doc_type_spec is None
    assert any("unknown doc-type slug" in r.getMessage() for r in caplog.records)


def test_expand_slots_caps_firms(scanned_env, caplog) -> None:
    """Firms beyond config.multi_search.max_firms are dropped with a logged
    warning (nothing silent)."""
    config = scanned_env
    original = config.multi_search.max_firms
    config.multi_search.max_firms = 1
    conn = open_db(config.db_path, config.pv_root)
    try:
        request = MultiSearchSelectionRequest(
            firms=[
                MultiSearchFirm(client="Angelo Gordon", deals=["Accell"], period="2025-01-31"),
                MultiSearchFirm(
                    client="Apollo Global Management",
                    deals=["Summit Ridge Energy"],
                    period="2026-03-31",
                ),
            ]
        )
        with caplog.at_level(logging.INFO, logger="pv_extractor.api.multi_search_service"):
            slots = expand_slots(conn, config, request)
    finally:
        conn.close()
        config.multi_search.max_firms = original
    # only the first firm survived the cap
    assert {s.client for s in slots} == {"Angelo Gordon"}
    assert any("firms capped" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# slot_selection misfiled surfacing (enhanced_period_check)
# ---------------------------------------------------------------------------


def _misfiled_env(fixture_pv_root: Path):
    """An in-memory index holding the REAL rich Accell memo PDF (in-file as-of
    2025-01-31) registered under a deliberately WRONG Nov-2024 period folder."""
    base = Path(tempfile.mkdtemp())
    config = _config(fixture_pv_root, base)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)

    real = (
        Path(config.pv_root)
        / "Angelo Gordon"
        / "Accell"
        / "(5) 1.31.25"
        / "Client"
        / "Accell Valuation Memo 1.31.25 vf.pdf"
    )
    rec = derive_record(
        str(real), size_bytes=real.stat().st_size, modified_time=datetime(2025, 2, 14), config=config
    )
    # file it under a wrong Nov-2024 period (the path/content is unchanged on disk).
    misfiled = rec.model_copy(update={"as_of_date": date(2024, 11, 30), "date_folder": "11.30.24"})
    db.insert_records(conn, [misfiled], 100)
    return conn, config, rec.client, rec.deal


def test_slot_selection_flags_misfiled_under_enhanced_check(fixture_pv_root) -> None:
    """enhanced_period_check=True surfaces a misfiled document: no verified
    winner for the requested Nov-2024 period, but the candidate's in-file as-of
    (2025-01-31) disagrees -> MISFILED carrying the TRUE detected period."""
    conn, config, client, deal = _misfiled_env(fixture_pv_root)
    try:
        target = date(2024, 11, 30)
        slot = slot_selection(
            conn,
            config,
            client,
            deal,
            "2024-11-30",
            DocType.any_client_valuation_doc,
            target=target,
            enhanced_period_check=True,
        )
        assert slot.status != "FOUND"  # the in-file cross-check rejected the wrong-period file
        assert slot.misfiled is True
        assert slot.detected_as_of == "2025-01-31"
        expected_period = period_label(date(2025, 1, 31), config.client_period_style(client))
        assert slot.detected_period == expected_period
        assert "Misfiled" in slot.detail and "2025-01-31" in slot.detail
    finally:
        conn.close()


def test_slot_selection_misfiled_off_by_default(fixture_pv_root) -> None:
    """enhanced_period_check=False (default) never touches the misfiled fields,
    even on the same wrong-period document — single-firm output is unchanged."""
    conn, config, client, deal = _misfiled_env(fixture_pv_root)
    try:
        slot = slot_selection(
            conn,
            config,
            client,
            deal,
            "2024-11-30",
            DocType.any_client_valuation_doc,
            target=date(2024, 11, 30),
            enhanced_period_check=False,
        )
        assert slot.misfiled is False
        assert slot.detected_as_of is None
        assert slot.detected_period is None
        assert "Misfiled" not in slot.detail
    finally:
        conn.close()


def test_slot_selection_not_fabricated_when_dates_agree(scanned_env) -> None:
    """Never fabricated: a slot that resolves a verified winner for the target
    period (in-file as-of agrees) stays not-misfiled even with the check on."""
    config = scanned_env
    conn = open_db(config.db_path, config.pv_root)
    try:
        slot = slot_selection(
            conn,
            config,
            "Angelo Gordon",
            "Accell",
            "2025-01-31",
            DocType.any_client_valuation_doc,
            target=date(2025, 1, 31),
            enhanced_period_check=True,
        )
        assert slot.status == "FOUND"
        assert slot.misfiled is False
        assert slot.detected_as_of is None
    finally:
        conn.close()
