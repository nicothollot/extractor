"""End-to-end locator suite (D6): scan the synthetic PV fixture tree into a
real SQLite index, then drive locate() through every resolution status and
the hard ranking cases (version families, archive dups, HL lookalikes,
decorated folders, DO NOT USE files, zero-byte uploads, loose root files)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

from pv_extractor.config import ClientConfig, Config, load_config
from pv_extractor.indexer import db
from pv_extractor.indexer.scan_tree import scan_tree
from pv_extractor.locator.locate import locate
from pv_extractor.locator.scoring import ScoreContext, score_candidate
from pv_extractor.models import (
    DocType,
    LocateQuery,
    LocateResult,
    ResolutionStatus,
    ScanError,
)
from pv_extractor.normalize import family_stem

# Phase 1 tree (28 files) + the two Blue Owl Phase-2 documents (docx/xlsx).
FIXTURE_FILE_COUNT = 30

ACCELL_VF = "Angelo Gordon/Accell/(5) 1.31.25/Client/Accell Valuation Memo 1.31.25 vf.pdf"
ACCELL_LOOKALIKE = "Angelo Gordon/Accell/(5) 1.31.25/Analysis/Accell Valuation Memo 1.31.25.pdf"
ACCELL_NOV24 = "Angelo Gordon/Accell/(3) 11.30.24/Client/Accell Valuation Memo 11.30.24.pdf"
ACCELL_NDA = "Angelo Gordon/Accell/(3) 11.30.24/Client/Accell NDA.pdf"
TDW_CLEAN = "Angelo Gordon/T.D. Williamson/(1) 9.30.24/Client/TDW Valuation Memo 9.30.24.pdf"
TDW_DNU = "Angelo Gordon/T.D. Williamson/(1) 9.30.24/Client/TDW Valuation Memo 9.30.24 OLD DO NOT USE.pdf"
SRE_PRIOR = "Apollo Global Management/Summit Ridge Energy/+Prior (8.31.24) Reports/SRE Valuation Memo 8.31.24.pdf"
HYPEROPTIC_MEMO = "Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic Valuation Memo Q1 2026.pdf"
HYPEROPTIC_WRITEUP = "Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic Valuation Write Up Q1 2026.pdf"
ANDOVER_PDF = "Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Memo 03.2026.pdf"
ANDOVER_ZERO = "Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Summary empty.pdf"


@pytest.fixture(scope="session")
def e2e_config(
    project_root: Path, fixture_pv_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> Config:
    """Repo-default locator config retargeted at the fixture tree. The
    aliases_path stays the repo's aliases.yaml (load_config resolves it)."""
    out = tmp_path_factory.mktemp("e2e_out")
    config = load_config(project_root / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = out
    config.db_path = out / "pv_index.db"
    config.clients = {
        "default": ClientConfig(period_style="quarterly_calendar"),
        "Angelo Gordon": ClientConfig(period_style="monthly"),
    }
    assert Path(config.locator.aliases_path) == project_root / "aliases.yaml"
    return config


@pytest.fixture(scope="session")
def e2e_conn(e2e_config: Config, fixture_pv_root: Path) -> Iterator[sqlite3.Connection]:
    """Index the fixture tree once, then inject one scan_errors row for the
    'Blocked Deal' folder (simulating an unreadable folder hit during scan)."""
    conn = db.open_db(e2e_config.db_path, e2e_config.pv_root)
    db.init_schema(conn)
    stats = scan_tree(conn, fixture_pv_root, e2e_config)
    assert stats.errors == 0
    assert stats.files_seen == FIXTURE_FILE_COUNT
    db.add_scan_error(
        conn,
        ScanError(
            path=str(fixture_pv_root / "Angeles Investments" / "Blocked Deal"),
            error_type="PermissionError",
            message="simulated unreadable folder recorded during scanning",
            seen_at=datetime(2026, 6, 1, 8, 0, 0),
        ),
    )
    yield conn
    conn.close()


def _locate(
    conn: sqlite3.Connection,
    config: Config,
    client: str,
    deal: str,
    period: str,
    doc_type: DocType = DocType.valuation_memo,
) -> LocateResult:
    return locate(
        conn, config, LocateQuery(client=client, deal=deal, period=period, doc_type=doc_type)
    )


def _path(root: Path, rel: str) -> str:
    return str(root.joinpath(*rel.split("/")))


# --------------------------------------------------------------------------
# Exact status (and exact winner path for FOUND) per query
# --------------------------------------------------------------------------

CASES = [
    pytest.param("Angelo Gordon", "Accell", "2025-01-31", DocType.valuation_memo,
                 ResolutionStatus.FOUND, ACCELL_VF, id="01-accell-vf-wins"),
    pytest.param("Angelo Gordon", "Accell", "2024-11-30", DocType.valuation_memo,
                 ResolutionStatus.FOUND, ACCELL_NOV24, id="05-seq-prefixed-monthly-folder"),
    pytest.param("Angelo Gordon", "Accell", "2025-11-30", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Angelo Gordon/Accell/1. 11.30.25/Client/Accell Valuation Memo 11.30.25.pdf",
                 id="06-dot-prefixed-folder"),
    pytest.param("Angelo Gordon", "TDW", "2024-09-30", DocType.valuation_memo,
                 ResolutionStatus.FOUND, TDW_CLEAN, id="07-alias-tdw"),
    pytest.param("Angelo Gordon", "T.D. Williamson", "2025-12-31", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Angelo Gordon/T.D. Williamson/(2) 12-31-2025/Client/TDW Valuation Memo 12-31-2025 (003).pdf",
                 id="08-later-copy-wins"),
    pytest.param("Angelo Gordon", "Digital Edge", "Q1 2026", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Angelo Gordon/+Digital Edge/Q1 2026/Client/Digital Edge Valuation Memo Q1 2026.pdf",
                 id="10a-plus-decorated-deal-folder"),
    pytest.param("Angelo Gordon", "DE", "Q1 2026", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Angelo Gordon/+Digital Edge/Q1 2026/Client/Digital Edge Valuation Memo Q1 2026.pdf",
                 id="10b-codename-de"),
    pytest.param("Apollo Global Management", "SRE", "2026-03-31", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Apollo Global Management/Summit Ridge Energy/03-31-2026 SRE Valuation Memo_vf.pdf",
                 id="11-loose-root-file-period-from-filename"),
    pytest.param("Apollo Global Management", "Summit Ridge Energy", "2025-12-31", DocType.valuation_memo,
                 ResolutionStatus.FOUND,
                 "Apollo Global Management/Summit Ridge Energy/FY2025/Client/SRE Valuation Memo FY2025.pdf",
                 id="12-fy-folder"),
    pytest.param("Apollo Global Management", "Summit Ridge Energy", "2024-08-31", DocType.valuation_memo,
                 ResolutionStatus.AMBIGUOUS, None, id="13-prior-reports-needs-human"),
    pytest.param("Apollo Global Management", "AIOF II / ANRP III", "Q1 2026", DocType.portfolio_review,
                 ResolutionStatus.FOUND,
                 "Apollo Global Management/AIOF II ANRP III/Q1 2026/Client/AIOF II ANRP III Portfolio Review Q1 2026.pdf",
                 id="14-joint-vehicle"),
    pytest.param("Apollo Global Management", "Hyperoptic", "Q1 2026", DocType.ic_memo,
                 ResolutionStatus.FOUND,
                 "Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic IC Memo Mar-26.pdf",
                 id="15-ic-memo-doc-type"),
    pytest.param("Apollo Global Management", "Hyperoptic", "Q1 2026", DocType.valuation_memo,
                 ResolutionStatus.AMBIGUOUS, None, id="16-memo-vs-write-up"),
    pytest.param("Angeles Investments", "Andover Storage", "2026-03-31", DocType.valuation_memo,
                 ResolutionStatus.FOUND, ANDOVER_PDF, id="17-pdf-beats-xlsm"),
    pytest.param("Angeles Investments", "Andover Storage", "2024-12-31", DocType.valuation_memo,
                 ResolutionStatus.NOT_YET_UPLOADED, None, id="18-period-folder-has-only-nda"),
    pytest.param("Angeles Investments", "Carlsbad Desal", "2026-03-31", DocType.valuation_memo,
                 ResolutionStatus.NOT_FOUND, None, id="19-no-date-folders"),
    pytest.param("Angeles Investments", "Zebra Holdings", "2026-03-31", DocType.valuation_memo,
                 ResolutionStatus.NOT_FOUND, None, id="20-unknown-deal"),
    pytest.param("Angeles Investments", "Blocked Deal", "2024-09-30", DocType.valuation_memo,
                 ResolutionStatus.ACCESS_ERROR, None, id="21-injected-scan-error"),
    pytest.param("Angelo Gordon", "Accell", "2024-11-30", DocType.any_client_valuation_doc,
                 ResolutionStatus.FOUND, ACCELL_NOV24, id="22-any-doc-type-memo-not-nda"),
]


@pytest.mark.parametrize(("client", "deal", "period", "doc_type", "status", "winner_rel"), CASES)
def test_locate_status_and_winner(
    e2e_conn: sqlite3.Connection,
    e2e_config: Config,
    fixture_pv_root: Path,
    client: str,
    deal: str,
    period: str,
    doc_type: DocType,
    status: ResolutionStatus,
    winner_rel: str | None,
) -> None:
    result = _locate(e2e_conn, e2e_config, client, deal, period, doc_type)
    assert result.status is status, result.evidence
    if winner_rel is not None:
        assert result.winner is not None
        assert result.winner.record.file_path == _path(fixture_pv_root, winner_rel)
    else:
        assert result.winner is None


# --------------------------------------------------------------------------
# Case-specific invariants
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def accell_q1_result(e2e_conn: sqlite3.Connection, e2e_config: Config) -> LocateResult:
    """Case-1 query, shared by the breakdown/family/lookalike assertions."""
    return _locate(e2e_conn, e2e_config, "Angelo Gordon", "Accell", "2025-01-31")


def test_case1_winner_breakdown_invariants(accell_q1_result: LocateResult) -> None:
    assert accell_q1_result.winner is not None
    breakdown = accell_q1_result.winner.breakdown
    assert breakdown.client_deal_method == "exact"
    assert breakdown.period_method == "folder"
    assert breakdown.matched_keywords and breakdown.doctype_score > 0  # doctype hit recorded
    assert breakdown.source_class_score > 0
    assert breakdown.archive_multiplier == 1.0  # not archived
    assert breakdown.final_score == breakdown.raw_total


def test_case2_winner_from_client_not_archive(accell_q1_result: LocateResult) -> None:
    assert accell_q1_result.winner is not None
    path = accell_q1_result.winner.record.file_path.replace("\\", "/")
    assert "/Client/" in path
    assert "/Archive/" not in path


def test_case3_analysis_lookalike_is_demoted(
    accell_q1_result: LocateResult,
    e2e_conn: sqlite3.Connection,
    e2e_config: Config,
    fixture_pv_root: Path,
) -> None:
    lookalike_path = _path(fixture_pv_root, ACCELL_LOOKALIKE)
    assert accell_q1_result.winner is not None
    assert accell_q1_result.winner.record.file_path != lookalike_path
    assert all(c.record.file_path != lookalike_path for c in accell_q1_result.candidates)

    # Score the lookalike under the same query: HL work product carries the
    # report/analysis penalty, which is what keeps it from ever winning.
    row = e2e_conn.execute(
        "SELECT * FROM files WHERE file_path = ?", (lookalike_path,)
    ).fetchone()
    assert row is not None
    ctx = ScoreContext(
        resolved_client="Angelo Gordon",
        resolved_deal="Accell",
        deal_expansions=["Accell"],
        client_method="exact",
        client_ratio=100.0,
        deal_method="exact",
        deal_ratio=100.0,
        target_as_of=date(2025, 1, 31),
        doc_type=DocType.valuation_memo,
        cfg=e2e_config.locator,
    )
    breakdown = score_candidate(db.record_from_row(row), ctx)
    assert breakdown.source_class_score < 0
    assert breakdown.final_score < accell_q1_result.winner.breakdown.final_score


def test_case4_version_family_collapses(accell_q1_result: LocateResult) -> None:
    head_names = [c.record.file_name for c in accell_q1_result.candidates]
    assert "Accell Valuation Memo 1.31.25 v1.pdf" not in head_names
    assert "Accell Valuation Memo 1.31.25 v2.pdf" not in head_names
    # ... because they belong to the winner's family, not their own.
    assert accell_q1_result.winner is not None
    assert accell_q1_result.winner.family_rank == 0
    assert accell_q1_result.winner.family_key == family_stem("Accell Valuation Memo 1.31.25 v1.pdf")
    assert accell_q1_result.winner.family_key == family_stem("Accell Valuation Memo 1.31.25 v2.pdf")


def test_case9_do_not_use_never_wins(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    result = _locate(e2e_conn, e2e_config, "Angelo Gordon", "TDW", "2024-09-30")
    assert result.winner is not None
    assert result.winner.record.file_path == _path(fixture_pv_root, TDW_CLEAN)
    dnu = [c for c in result.candidates if c.record.file_path == _path(fixture_pv_root, TDW_DNU)]
    assert dnu, "the DO NOT USE file should surface as a demoted candidate, not vanish"
    assert dnu[0].breakdown.do_not_use_penalty < 0
    assert dnu[0].breakdown.final_score < result.winner.breakdown.final_score


def test_case13_prior_reports_top_candidate_below_accept(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    result = _locate(
        e2e_conn, e2e_config, "Apollo Global Management", "Summit Ridge Energy", "2024-08-31"
    )
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.candidates, "the +Prior file must surface for the human to confirm"
    top = result.candidates[0]
    assert top.record.file_path == _path(fixture_pv_root, SRE_PRIOR)
    # Archive multiplier + report-class penalty keep it below min_accept_score.
    assert top.breakdown.source_class_score < 0
    assert top.breakdown.archive_multiplier == e2e_config.locator.weights.archive_score_multiplier
    assert top.breakdown.final_score < e2e_config.locator.min_accept_score


def test_case16_memo_and_write_up_both_among_candidates(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    result = _locate(e2e_conn, e2e_config, "Apollo Global Management", "Hyperoptic", "Q1 2026")
    assert result.status is ResolutionStatus.AMBIGUOUS
    paths = {c.record.file_path for c in result.candidates}
    assert _path(fixture_pv_root, HYPEROPTIC_MEMO) in paths
    assert _path(fixture_pv_root, HYPEROPTIC_WRITEUP) in paths


def test_case21_access_error_evidence_names_blocked_folder(
    e2e_conn: sqlite3.Connection, e2e_config: Config
) -> None:
    result = _locate(e2e_conn, e2e_config, "Angeles Investments", "Blocked Deal", "2024-09-30")
    assert result.status is ResolutionStatus.ACCESS_ERROR
    assert "Blocked Deal" in result.evidence


def test_period_fallback_surfaces_docs_without_doctype(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    """Andover Storage has real documents for 2026-03-31 but none is a
    'portfolio review'. Rather than a bare NOT_YET_UPLOADED with nothing to act
    on, the locator surfaces the period-matching documents as AMBIGUOUS so the
    analyst can pick/Replace — but the NDA-class negative is still excluded."""
    result = _locate(
        e2e_conn, e2e_config, "Angeles Investments", "Andover Storage", "2026-03-31",
        DocType.portfolio_review,
    )
    assert result.status is ResolutionStatus.AMBIGUOUS, result.evidence
    assert result.candidates, "period-matching documents must be offered for human pick"
    assert "portfolio_review" in result.evidence
    # the real valuation memo for this period is among the offered candidates
    assert any(
        c.record.file_path == _path(fixture_pv_root, ANDOVER_PDF) for c in result.candidates
    )


def test_period_fallback_can_be_disabled(
    e2e_conn: sqlite3.Connection, e2e_config: Config
) -> None:
    """With the fallback off, a doc-type miss reverts to the strict status."""
    import copy

    cfg = copy.deepcopy(e2e_config)
    cfg.locator.surface_period_matches_without_doctype = False
    result = locate(
        e2e_conn, cfg,
        LocateQuery(
            client="Angeles Investments", deal="Andover Storage",
            period="2026-03-31", doc_type=DocType.portfolio_review,
        ),
    )
    assert result.status in (ResolutionStatus.NOT_YET_UPLOADED, ResolutionStatus.NOT_FOUND)


def test_case22_nda_is_never_a_candidate(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    result = _locate(
        e2e_conn, e2e_config, "Angelo Gordon", "Accell", "2024-11-30",
        DocType.any_client_valuation_doc,
    )
    assert result.winner is not None
    assert result.winner.record.file_path == _path(fixture_pv_root, ACCELL_NOV24)
    nda_path = _path(fixture_pv_root, ACCELL_NDA)
    assert all(c.record.file_path != nda_path for c in result.candidates)


def test_case23_zero_byte_candidate_is_flagged(
    e2e_conn: sqlite3.Connection, e2e_config: Config, fixture_pv_root: Path
) -> None:
    result = _locate(e2e_conn, e2e_config, "Angeles Investments", "Andover Storage", "2026-03-31")
    zero = [c for c in result.candidates if c.record.file_path == _path(fixture_pv_root, ANDOVER_ZERO)]
    assert zero, "the zero-byte upload should surface as its own (penalized) family head"
    assert zero[0].record.is_zero_byte
    assert zero[0].breakdown.zero_byte_penalty < 0
    # Its own family: distinct stem from the winning memo.
    assert result.winner is not None
    assert zero[0].family_key != result.winner.family_key
    assert zero[0].family_key == family_stem("Andover Storage Valuation Summary empty.pdf")
