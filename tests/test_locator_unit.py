"""Unit tests for the D5 locator: alias resolution, scoring components,
version-family grouping, and the status cascade against an on-disk temp
SQLite index (synthetic UNC paths under the default pv_root, never opened)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest
from rapidfuzz import fuzz

from pv_extractor.config import Config, parse_period_style
from pv_extractor.indexer import db
from pv_extractor.indexer.derive import derive_record
from pv_extractor.locator.aliases import AliasTable, load_aliases, resolve_name
from pv_extractor.locator.families import group_into_families
from pv_extractor.indexer.periods import resolve_target_period
from pv_extractor.locator.locate import locate
from pv_extractor.locator.scoring import ScoreContext, score_candidate
from pv_extractor.locator.verify import verify_candidate
from pv_extractor.models import (
    CandidateFile,
    DocType,
    FileRecord,
    LocateQuery,
    ResolutionStatus,
    ScanError,
    VerifyStatus,
)

PV = "\\\\hlhz\\dfs\\nyfva\\PV"
TARGET = date(2025, 1, 31)
IN_WINDOW = datetime(2025, 2, 10, 9, 0, 0)  # within [target, target+75d]
OUT_OF_WINDOW = datetime(2024, 6, 1, 9, 0, 0)


@pytest.fixture()
def config(project_root: Path) -> Config:
    cfg = Config()
    cfg.locator.aliases_path = str(project_root / "aliases.yaml")
    return cfg


@pytest.fixture()
def alias_table(config: Config) -> AliasTable:
    return load_aliases(config.locator.aliases_path)


@pytest.fixture()
def conn(tmp_path: Path, config: Config) -> Iterator[sqlite3.Connection]:
    connection = db.open_db(tmp_path / "pv_index.db", config.pv_root)
    db.init_schema(connection)
    yield connection
    connection.close()


def make_record(
    config: Config,
    rel_path: str,
    *,
    size: int | None = 250_000,
    mtime: datetime | None = IN_WINDOW,
) -> FileRecord:
    return derive_record(PV + "\\" + rel_path, size_bytes=size, modified_time=mtime, config=config)


def make_ctx(
    config: Config,
    *,
    deal: str = "Accell",
    expansions: list[str] | None = None,
    doc_type: DocType = DocType.valuation_memo,
) -> ScoreContext:
    return ScoreContext(
        resolved_client="Angelo Gordon",
        resolved_deal=deal,
        deal_expansions=expansions if expansions is not None else [deal],
        client_method="exact",
        client_ratio=100.0,
        deal_method="exact",
        deal_ratio=100.0,
        target_as_of=TARGET,
        doc_type=doc_type,
        cfg=config.locator,
    )


def scored(config: Config, rel_path: str, ctx: ScoreContext | None = None, **rec_kwargs) -> CandidateFile:
    record = make_record(config, rel_path, **rec_kwargs)
    context = ctx if ctx is not None else make_ctx(config)
    return CandidateFile(record=record, breakdown=score_candidate(record, context))


# ---------------------------------------------------------------- aliases


def test_load_aliases_missing_file_gives_empty_table(tmp_path: Path) -> None:
    table = load_aliases(tmp_path / "does_not_exist.yaml")
    assert table.clients == {} and table.deals == {}


def test_load_aliases_reads_repo_yaml(alias_table: AliasTable) -> None:
    assert "AG" in alias_table.clients["Angelo Gordon"]
    assert "DE" in alias_table.deals["Digital Edge"]


def test_resolve_exact_folder_name(alias_table: AliasTable) -> None:
    resolved, method, ratio = resolve_name(
        "Angelo Gordon", ["Angelo Gordon", "Angeles Investments"], alias_table.clients, 80
    )
    assert (resolved, method, ratio) == ("Angelo Gordon", "exact", 100.0)


def test_resolve_exact_via_alias(alias_table: AliasTable) -> None:
    resolved, method, _ = resolve_name(
        "ag", ["Angelo Gordon", "Angeles Investments"], alias_table.clients, 80
    )
    assert (resolved, method) == ("Angelo Gordon", "exact")


def test_resolve_normalized_decorated_folder(alias_table: AliasTable) -> None:
    # Folder '+Digital Edge' is linked to canonical 'Digital Edge' by
    # normalize_text equality; the query matches via normalization.
    resolved, method, _ = resolve_name("Digital Edge", ["+Digital Edge"], alias_table.deals, 80)
    assert (resolved, method) == ("+Digital Edge", "normalized")


def test_resolve_alias_of_decorated_folder(alias_table: AliasTable) -> None:
    # 'DE' is an alias of the canonical linked to '+Digital Edge', so it
    # lands in the folder's expansion set (casefold-equal -> exact).
    resolved, method, _ = resolve_name("DE", ["+Digital Edge"], alias_table.deals, 80)
    assert (resolved, method) == ("+Digital Edge", "exact")


def test_resolve_normalized_punctuation_variant() -> None:
    # 'T D Williamson' normalize_text-equals 'T.D. Williamson'.
    resolved, method, _ = resolve_name("T D Williamson", ["T.D. Williamson"], {}, 80)
    assert (resolved, method) == ("T.D. Williamson", "normalized")


def test_resolve_fuzzy(alias_table: AliasTable) -> None:
    # Without aliases, 'TD Williamson' is neither casefold- nor
    # normalize-equal to 'T.D. Williamson' ('td' vs 't d') -> fuzzy.
    resolved, method, ratio = resolve_name("TD Williamson", ["T.D. Williamson"], {}, 80)
    assert (resolved, method) == ("T.D. Williamson", "fuzzy")
    assert 80 <= ratio < 100


def test_resolve_below_threshold_is_none() -> None:
    resolved, method, ratio = resolve_name("Zebra Holdings", ["T.D. Williamson"], {}, 80)
    assert (resolved, method) == (None, "none")
    assert ratio < 80


# ------------------------------------------------------- period resolution


def test_resolve_target_period_forms() -> None:
    quarterly = parse_period_style("quarterly_calendar")
    monthly = parse_period_style("monthly")
    fiscal_june = parse_period_style("fiscal(6)")
    assert resolve_target_period("2025-01-31", monthly) == date(2025, 1, 31)
    assert resolve_target_period("1.31.25", monthly) == date(2025, 1, 31)
    assert resolve_target_period("Q4 2025", quarterly) == date(2025, 12, 31)
    assert resolve_target_period("2025 Q4", quarterly) == date(2025, 12, 31)
    assert resolve_target_period("Q1 2026", fiscal_june) == date(2025, 9, 30)
    assert resolve_target_period("Q4 2026", fiscal_june) == date(2026, 6, 30)
    assert resolve_target_period("January 2025", monthly) == date(2025, 1, 31)
    assert resolve_target_period("whenever", quarterly) is None


# ------------------------------------------------------ scoring components


def test_client_deal_exact(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.client_deal_score == config.locator.weights.client_deal_exact
    assert bd.client_deal_method == "exact"


def test_client_deal_normalized(config: Config) -> None:
    ctx = make_ctx(config, deal="Digital Edge", expansions=["Digital Edge", "DE"])
    bd = scored(config, "Angelo Gordon\\+Digital Edge\\1.31.2025\\Client\\DE Valuation Memo.pdf", ctx).breakdown
    assert bd.client_deal_score == config.locator.weights.client_deal_normalized
    assert bd.client_deal_method == "normalized"


def test_client_deal_fuzzy_is_scaled(config: Config) -> None:
    ctx = make_ctx(config, deal="Summit Ridge Energy", expansions=["Summit Ridge Energy", "SRE"])
    bd = scored(
        config, "Angelo Gordon\\Summit Rige Energy\\1.31.2025\\Client\\Valuation Memo.pdf", ctx
    ).breakdown
    ratio = fuzz.token_set_ratio("summit rige energy", "summit ridge energy")
    threshold = config.locator.fuzzy_match_threshold
    expected = config.locator.weights.client_deal_fuzzy_max * (ratio - threshold) / (100 - threshold)
    assert bd.client_deal_method == "fuzzy"
    assert bd.client_deal_score == pytest.approx(expected)
    assert 0 < bd.client_deal_score < config.locator.weights.client_deal_fuzzy_max


def test_client_deal_below_threshold_scores_zero(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Completely Different\\1.31.2025\\Client\\Memo.pdf").breakdown
    assert bd.client_deal_score == 0.0
    assert bd.client_deal_method == "none"


def test_period_folder_exact(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.period_score == config.locator.weights.period_folder_exact
    assert bd.period_method == "folder"


def test_period_folder_mismatch(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\12.31.2024\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.period_score == config.locator.weights.period_folder_mismatch
    assert bd.period_method == "folder_mismatch"


def test_period_folder_same_quarter_is_a_match(config: Config) -> None:
    # TARGET is 2025-01-31 (Q1 2025); a 3.31.2025 folder is a DIFFERENT date but
    # the SAME quarter, so with tolerate_same_period it scores just below exact.
    bd = scored(config, "Angelo Gordon\\Accell\\3.31.2025\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.period_score == config.locator.weights.period_folder_same_period
    assert bd.period_method == "folder_same_period"
    assert bd.period_score < config.locator.weights.period_folder_exact


def test_period_same_quarter_off_when_flag_disabled(config: Config) -> None:
    config.locator.tolerate_same_period = False
    bd = scored(config, "Angelo Gordon\\Accell\\3.31.2025\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.period_score == config.locator.weights.period_folder_mismatch
    assert bd.period_method == "folder_mismatch"


def test_period_in_filename(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\Client\\Accell Valuation Memo 1.31.25.pdf").breakdown
    assert bd.period_score == config.locator.weights.period_in_filename
    assert bd.period_method == "filename"


def test_period_mtime_window(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\Client\\Accell Valuation Memo.pdf", mtime=IN_WINDOW).breakdown
    assert bd.period_score == config.locator.weights.period_mtime_window
    assert bd.period_method == "mtime"


def test_period_mtime_before_as_of_does_not_count(config: Config) -> None:
    # Memos are written AFTER quarter end: a pre-period mtime is no signal.
    bd = scored(
        config, "Angelo Gordon\\Accell\\Client\\Accell Valuation Memo.pdf", mtime=OUT_OF_WINDOW
    ).breakdown
    assert bd.period_score == 0.0
    assert bd.period_method == "none"


def test_doctype_keyword_hits_once_and_records_matches(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf").breakdown
    assert bd.doctype_score == config.locator.weights.doctype_keyword
    assert "valuation memo" in bd.matched_keywords


def test_doctype_wrong_type_scores_zero(config: Config) -> None:
    ctx = make_ctx(config, doc_type=DocType.ic_memo)
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf", ctx).breakdown
    assert bd.doctype_score == 0.0
    assert bd.matched_keywords == []


def test_doctype_any_uses_union_of_keywords(config: Config) -> None:
    ctx = make_ctx(config, doc_type=DocType.any_client_valuation_doc)
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Portfolio Review.pdf", ctx).breakdown
    assert bd.doctype_score == config.locator.weights.doctype_keyword
    assert "portfolio review" in bd.matched_keywords


def test_negative_keyword(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell NDA 1.31.25.pdf").breakdown
    assert bd.negative_score == config.locator.weights.negative_keyword
    assert bd.matched_negative_keywords == ["nda"]


def test_source_class_gate(config: Config) -> None:
    client = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Memo.pdf").breakdown
    report = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Report\\Memo.pdf").breakdown
    analysis = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Analysis\\Memo.pdf").breakdown
    other = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Memo.pdf").breakdown
    assert client.source_class_score == config.locator.weights.source_class_client_bonus
    assert report.source_class_score == config.locator.weights.source_class_report_penalty
    assert analysis.source_class_score == config.locator.weights.source_class_report_penalty
    assert other.source_class_score == 0.0


def test_extension_prior(config: Config) -> None:
    pdf = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Memo.pdf").breakdown
    unknown = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Memo.zzz").breakdown
    assert pdf.extension_score == config.locator.weights.extension_prior[".pdf"]
    assert unknown.extension_score == 0.0


def test_version_score_uses_rank_step(config: Config) -> None:
    vf = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo vf.pdf").breakdown
    assert vf.version_score == 3 * config.locator.weights.version_rank_step


def test_do_not_use_penalty(config: Config) -> None:
    bd = scored(
        config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo DO NOT USE.pdf"
    ).breakdown
    assert bd.do_not_use_penalty == config.locator.weights.do_not_use_penalty


def test_zero_byte_penalty(config: Config) -> None:
    bd = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Memo.pdf", size=0).breakdown
    assert bd.zero_byte_penalty == config.locator.weights.zero_byte_penalty


def test_archive_multiplier_on_positive_total(config: Config) -> None:
    cand = scored(
        config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Archive\\Accell Valuation Memo vf.pdf"
    )
    bd = cand.breakdown
    assert cand.record.is_archive
    assert bd.raw_total > 0
    assert bd.archive_multiplier == config.locator.weights.archive_score_multiplier
    assert bd.final_score == pytest.approx(bd.raw_total * config.locator.weights.archive_score_multiplier)


def test_archive_multiplier_never_amplifies_negative_total(config: Config) -> None:
    # deal exact (+30), nda (-25), zero byte (-10), .txt prior 0 -> raw -5.
    cand = scored(
        config, "Angelo Gordon\\Accell\\Archive\\Accell NDA.txt", size=0, mtime=OUT_OF_WINDOW
    )
    bd = cand.breakdown
    assert cand.record.is_archive
    assert bd.raw_total < 0
    assert bd.archive_multiplier == 1.0
    assert bd.final_score == pytest.approx(bd.raw_total)


# ------------------------------------------------------------ families


def test_family_versions_group_with_vf_head(config: Config) -> None:
    base = "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo {}.pdf"
    cands = [scored(config, base.format(v)) for v in ("v1", "v2", "vf")]
    families = group_into_families(cands, config.locator.family_ratio_threshold)
    assert len(families) == 1
    names = [cand.record.file_name for cand in families[0]]
    assert names == [base.format(v).rsplit("\\", 1)[1] for v in ("vf", "v2", "v1")]
    assert [cand.family_rank for cand in families[0]] == [0, 1, 2]
    assert all(cand.family_key == "accell valuation memo" for cand in families[0])


def test_family_copies_later_copy_heads(config: Config) -> None:
    folder = "Angelo Gordon\\Accell\\1.31.2025\\Client\\"
    older = scored(config, folder + "Accell Valuation Memo (002).pdf", mtime=datetime(2025, 2, 5, 9, 0))
    newer = scored(config, folder + "Accell Valuation Memo (003).pdf", mtime=datetime(2025, 2, 9, 9, 0))
    families = group_into_families([older, newer], config.locator.family_ratio_threshold)
    assert len(families) == 1
    assert families[0][0] is newer


def test_family_extension_prior_breaks_tie(config: Config) -> None:
    folder = "Angelo Gordon\\Accell\\1.31.2025\\Client\\"
    pdf = scored(config, folder + "Accell Valuation Memo.pdf")
    xlsm = scored(config, folder + "Accell Valuation Memo.xlsm")
    families = group_into_families([xlsm, pdf], config.locator.family_ratio_threshold)
    assert len(families) == 1
    assert families[0][0] is pdf  # higher final_score via extension prior


def test_family_client_copy_beats_archive_copy(config: Config) -> None:
    client_copy = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf")
    archive_copy = scored(
        config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Archive\\Accell Valuation Memo.pdf"
    )
    families = group_into_families([archive_copy, client_copy], config.locator.family_ratio_threshold)
    assert len(families) == 1
    assert families[0][0] is client_copy
    assert client_copy.breakdown.final_score > archive_copy.breakdown.final_score


def test_family_distinct_stems_stay_separate(config: Config) -> None:
    memo = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf")
    deck = scored(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Board Deck Q4.pdf")
    families = group_into_families([memo, deck], config.locator.family_ratio_threshold)
    assert len(families) == 2


# ---------------------------------------------------------- status cascade


def _ingest(conn: sqlite3.Connection, records: list[FileRecord]) -> None:
    db.insert_records(conn, records, batch_size=100)


def _query(deal: str = "Accell", period: str = "2025-01-31", doc_type: DocType = DocType.valuation_memo) -> LocateQuery:
    return LocateQuery(client="AG", deal=deal, period=period, doc_type=doc_type)


def test_locate_found_vf_beats_own_v2_without_ambiguity(conn, config: Config) -> None:
    folder = "Angelo Gordon\\Accell\\1.31.2025\\Client\\"
    _ingest(conn, [
        make_record(config, folder + "Accell Valuation Memo 1.31.25 vf.pdf"),
        make_record(config, folder + "Accell Valuation Memo 1.31.25 v2.pdf"),
    ])
    result = locate(conn, config, _query())
    assert result.status is ResolutionStatus.FOUND
    assert result.winner is not None
    assert result.winner.record.file_name == "Accell Valuation Memo 1.31.25 vf.pdf"
    assert result.query.as_of_date == TARGET
    assert "min_accept_score" in result.evidence
    # vf and v2 collapse into one family: a single eligible head, no gap issue.
    assert len(result.candidates) == 1


def test_locate_negative_keyword_candidates_are_excluded(conn, config: Config) -> None:
    folder = "Angelo Gordon\\Accell\\1.31.2025\\Client\\"
    _ingest(conn, [
        make_record(config, folder + "Accell Valuation Memo 1.31.25.pdf"),
        make_record(config, folder + "Accell NDA Agreement.pdf"),
        make_record(config, folder + "Accell Engagement Letter.pdf"),
    ])
    result = locate(conn, config, _query())
    assert result.status is ResolutionStatus.FOUND
    # NDA/engagement heads score above floor_score but can never satisfy a
    # document request: only the memo remains eligible.
    assert len(result.candidates) == 1
    assert result.winner.record.file_name == "Accell Valuation Memo 1.31.25.pdf"


def test_locate_ambiguous_when_gap_too_small(conn, config: Config) -> None:
    folder = "Angelo Gordon\\Accell\\1.31.2025\\Client\\"
    _ingest(conn, [
        make_record(config, folder + "Accell Valuation Memo 1.31.25.pdf"),
        make_record(config, folder + "Accell Investment Committee Memo 1.31.25.pdf"),
    ])
    result = locate(conn, config, _query(doc_type=DocType.any_client_valuation_doc))
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.winner is None
    assert len(result.candidates) == 2
    assert "min_gap" in result.evidence


def test_locate_not_yet_uploaded_lists_existing_periods(conn, config: Config) -> None:
    _ingest(conn, [
        make_record(config, "Angelo Gordon\\Accell\\12.31.2024\\Client\\Accell NDA.pdf"),
    ])
    result = locate(conn, config, _query())
    assert result.status is ResolutionStatus.NOT_YET_UPLOADED
    assert "2024-12-31" in result.evidence
    assert "2025-01-31" in result.evidence


def test_locate_not_found_when_no_date_folders(conn, config: Config) -> None:
    _ingest(conn, [
        make_record(
            config,
            "Angelo Gordon\\Accell\\Correspondence\\Accell wire instructions.pdf",
            mtime=OUT_OF_WINDOW,
        ),
    ])
    result = locate(conn, config, _query())
    assert result.status is ResolutionStatus.NOT_FOUND
    assert "floor_score" in result.evidence


def test_locate_access_error_wins_over_not_yet_uploaded(conn, config: Config) -> None:
    _ingest(conn, [
        make_record(config, "Angelo Gordon\\Accell\\12.31.2024\\Client\\Accell NDA.pdf"),
    ])
    locked = PV + "\\Angelo Gordon\\Accell\\1.31.2025\\Client\\locked.pdf"
    db.add_scan_error(conn, ScanError(
        path=locked, error_type="PermissionError", message="access denied",
        seen_at=datetime(2025, 2, 1, 8, 0, 0),
    ))
    result = locate(conn, config, _query())
    assert result.status is ResolutionStatus.ACCESS_ERROR
    assert "locked.pdf" in result.evidence


def test_locate_unresolved_client_is_not_found(conn, config: Config) -> None:
    result = locate(conn, config, LocateQuery(client="Nobody Capital", deal="X", period="2025-01-31"))
    assert result.status is ResolutionStatus.NOT_FOUND
    assert "Nobody Capital" in result.evidence


def test_locate_unresolved_deal_is_not_found(conn, config: Config) -> None:
    _ingest(conn, [
        make_record(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf"),
    ])
    result = locate(conn, config, _query(deal="Zebra Crossing Partners"))
    assert result.status is ResolutionStatus.NOT_FOUND
    assert "Zebra Crossing Partners" in result.evidence


def test_locate_unparseable_period_raises(conn, config: Config) -> None:
    _ingest(conn, [
        make_record(config, "Angelo Gordon\\Accell\\1.31.2025\\Client\\Accell Valuation Memo.pdf"),
    ])
    with pytest.raises(ValueError, match="period"):
        locate(conn, config, _query(period="whenever"))


# ---------------------------------------------------------------- verify


def test_verify_candidate_unreadable_path_is_unverified(default_config) -> None:
    """Content that cannot be inspected must yield UNVERIFIED, never a
    rejection (Phase-2 verifier; full behavior tests live in test_verify.py)."""
    result = verify_candidate(
        PV + "\\Angelo Gordon\\Accell\\1.31.2025\\Client\\Memo.pdf", default_config
    )
    assert result.status is VerifyStatus.UNVERIFIED
