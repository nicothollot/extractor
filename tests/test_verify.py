"""D3 peek-verifier tests: classification, in-file as-of/asset extraction,
cross-checks against the locate query, and candidate re-ranking."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fixtures.docgen import make_scanned_pdf, make_text_pdf
from pv_extractor.locator.verify import verify_and_rerank, verify_candidate
from pv_extractor.models import (
    CandidateFile,
    DocClass,
    FileRecord,
    LocateQuery,
    LocateResult,
    ResolutionStatus,
    ScoreBreakdown,
    VerifyStatus,
)

CLIENT_MEMO_LINES = [
    "Accell Valuation Memorandum",
    "Prepared by Angelo Gordon Portfolio Valuation Group",
    "Valuation as of January 31, 2025",
    "Portfolio Company: Accell Group",
    "This valuation memo presents the fair value of the investment.",
    "The concluded enterprise value reflects a market multiple approach.",
]

HL_REPORT_LINES = [
    "Accell Group — Valuation Report",
    "Houlihan Lokey Financial Advisors",
    "This report is confidential and was prepared exclusively for internal use.",
    "Fair value conclusion as of January 31, 2025.",
]


def _pdf(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    make_text_pdf(path, [lines])
    return path


# --------------------------------------------------------------------------
# verify_candidate
# --------------------------------------------------------------------------


def test_client_doc_verified_with_asof_and_asset(tmp_path: Path, default_config) -> None:
    path = _pdf(tmp_path, "memo.pdf", CLIENT_MEMO_LINES)
    result = verify_candidate(path, default_config)
    assert result.status is VerifyStatus.VERIFIED
    assert result.doc_class is DocClass.CLIENT_VALUATION_DOC
    assert result.asof_date == date(2025, 1, 31)
    assert any("Accell" in name for name in result.asset_names)
    assert result.confidence >= 0.5
    assert result.evidence_snippets


def test_hl_work_product_rejected(tmp_path: Path, default_config) -> None:
    path = _pdf(tmp_path, "report.pdf", HL_REPORT_LINES)
    result = verify_candidate(path, default_config)
    assert result.status is VerifyStatus.REJECTED
    assert result.doc_class is DocClass.HL_WORK_PRODUCT
    assert "houlihan lokey" in result.reason


def test_hl_work_product_not_rejected_when_unrestricted(tmp_path: Path, default_config) -> None:
    """restrict_to_client_sourced=False drops the HL-work guard: the file is not
    REJECTED (it can still rank), so 'client, HL, anything' runs are possible."""
    path = _pdf(tmp_path, "report.pdf", HL_REPORT_LINES)
    query = LocateQuery(
        client="Client", deal="Deal", period="2025-01-31", restrict_to_client_sourced=False
    )
    result = verify_candidate(path, default_config, query=query)
    assert result.status is not VerifyStatus.REJECTED
    # The strict default still rejects it.
    strict = LocateQuery(client="Client", deal="Deal", period="2025-01-31")
    assert verify_candidate(path, default_config, query=strict).status is VerifyStatus.REJECTED


def test_hl_work_product_source_modes(tmp_path: Path, default_config) -> None:
    """The three-valued source_mode controls the HL-work REJECT: 'client'
    rejects, 'any' and 'hl' allow HL work product (the bool maps True->client /
    False->any so legacy callers are unchanged)."""
    path = _pdf(tmp_path, "report.pdf", HL_REPORT_LINES)
    base = dict(client="Client", deal="Deal", period="2025-01-31")
    assert verify_candidate(path, default_config, query=LocateQuery(**base, source_mode="client")).status is VerifyStatus.REJECTED
    assert verify_candidate(path, default_config, query=LocateQuery(**base, source_mode="any")).status is not VerifyStatus.REJECTED
    assert verify_candidate(path, default_config, query=LocateQuery(**base, source_mode="hl")).status is not VerifyStatus.REJECTED
    # legacy bool maps consistently
    assert LocateQuery(**base, restrict_to_client_sourced=False).source_mode == "any"
    assert LocateQuery(**base, source_mode="hl").restrict_to_client_sourced is False


def test_addressed_to_hl_is_not_work_product(tmp_path: Path, default_config) -> None:
    lines = CLIENT_MEMO_LINES + ["Prepared for Houlihan Lokey at the request of the manager."]
    path = _pdf(tmp_path, "memo.pdf", lines)
    result = verify_candidate(path, default_config)
    assert result.status is VerifyStatus.VERIFIED
    assert result.doc_class is DocClass.CLIENT_VALUATION_DOC


def test_wrong_period_inside_file_rejected(tmp_path: Path, default_config) -> None:
    path = _pdf(tmp_path, "memo.pdf", CLIENT_MEMO_LINES)
    query = LocateQuery(
        client="Angelo Gordon", deal="Accell", period="2024-10-31", as_of_date=date(2024, 10, 31)
    )
    result = verify_candidate(path, default_config, query=query)
    assert result.status is VerifyStatus.REJECTED
    assert "2025-01-31" in result.reason and "2024-10-31" in result.reason


def test_foreign_asset_name_rejected(tmp_path: Path, default_config) -> None:
    path = _pdf(tmp_path, "memo.pdf", CLIENT_MEMO_LINES)
    result = verify_candidate(
        path, default_config, expected_names=["Summit Ridge Energy", "SRE"]
    )
    assert result.status is VerifyStatus.REJECTED
    assert "do not match" in result.reason


def test_scanned_memo_is_unverified_not_rejected(tmp_path: Path, default_config) -> None:
    path = tmp_path / "scan.pdf"
    make_scanned_pdf(path, [CLIENT_MEMO_LINES])
    result = verify_candidate(path, default_config)
    assert result.status is VerifyStatus.UNVERIFIED
    assert "scanned" in result.reason or "no extractable text" in result.reason


def test_non_valuation_doc_is_other(tmp_path: Path, default_config) -> None:
    path = _pdf(tmp_path, "nda.pdf", ["Mutual Non-Disclosure Agreement", "between the parties"])
    result = verify_candidate(path, default_config)
    assert result.status is VerifyStatus.UNVERIFIED
    assert result.doc_class is DocClass.OTHER


# --------------------------------------------------------------------------
# verify_and_rerank
# --------------------------------------------------------------------------


def _candidate(path: Path, score: float) -> CandidateFile:
    record = FileRecord(
        file_name=path.name,
        file_path=str(path),
        folder_path=str(path.parent),
        parent_folder=path.parent.name,
        extension=path.suffix.lower(),
        client="Angelo Gordon",
        deal="Accell",
    )
    return CandidateFile(record=record, breakdown=ScoreBreakdown(final_score=score))


def _query() -> LocateQuery:
    return LocateQuery(
        client="Angelo Gordon", deal="Accell", period="2025-01-31", as_of_date=date(2025, 1, 31)
    )


def test_rerank_demotes_rejected_winner(tmp_path: Path, default_config) -> None:
    hl = _pdf(tmp_path, "hl_lookalike.pdf", HL_REPORT_LINES)
    good = _pdf(tmp_path, "client_memo.pdf", CLIENT_MEMO_LINES)
    top, second = _candidate(hl, 90.0), _candidate(good, 75.0)
    found = LocateResult(
        status=ResolutionStatus.FOUND, query=_query(), candidates=[top, second],
        winner=top, evidence="filename ranking",
    )
    reranked, verdicts = verify_and_rerank(found, default_config)
    assert reranked.status is ResolutionStatus.FOUND
    assert reranked.winner is second
    assert "rejected by content verification" in reranked.evidence
    assert verdicts[str(hl)].status is VerifyStatus.REJECTED
    assert verdicts[str(good)].status is VerifyStatus.VERIFIED
    assert reranked.candidates[-1] is top  # rejected sinks to the bottom


def test_rerank_disambiguates_ambiguous_to_found(tmp_path: Path, default_config) -> None:
    wrong_period = _pdf(
        tmp_path, "old_memo.pdf",
        [line.replace("January 31, 2025", "October 31, 2024") for line in CLIENT_MEMO_LINES],
    )
    good = _pdf(tmp_path, "right_memo.pdf", CLIENT_MEMO_LINES)
    ambiguous = LocateResult(
        status=ResolutionStatus.AMBIGUOUS, query=_query(),
        candidates=[_candidate(wrong_period, 60.0), _candidate(good, 58.0)],
        evidence="scores within min_gap",
    )
    reranked, _ = verify_and_rerank(ambiguous, default_config)
    assert reranked.status is ResolutionStatus.FOUND
    assert reranked.winner is not None and reranked.winner.record.file_path == str(good)


def test_rerank_auto_selects_best_on_ambiguous(tmp_path: Path, default_config) -> None:
    """Two acceptable, content-verified survivors and neither collapses to a
    single winner: auto-select the highest-confidence (here higher-scored)
    candidate and resolve to FOUND, instead of leaving the slot blank."""
    a = _pdf(tmp_path, "memo_a.pdf", CLIENT_MEMO_LINES)
    b = _pdf(tmp_path, "memo_b.pdf", CLIENT_MEMO_LINES)
    ambiguous = LocateResult(
        status=ResolutionStatus.AMBIGUOUS, query=_query(),
        candidates=[_candidate(a, 60.0), _candidate(b, 58.0)],
        evidence="scores within min_gap",
    )
    reranked, _ = verify_and_rerank(ambiguous, default_config)
    assert reranked.status is ResolutionStatus.FOUND
    assert reranked.winner is not None and reranked.winner.record.file_path == str(a)
    assert "auto-selected" in reranked.evidence


def test_rerank_scanned_valuation_beats_readable_nonvaluation(tmp_path: Path, default_config) -> None:
    """The real BrightNight bug: the valuation memo is a SCANNED PDF (peek can't
    read it -> UNVERIFIED, confidence 0.0) and a readable DDQ in the same folder
    inspects as not-a-valuation (UNVERIFIED, OTHER, confidence > 0). When NOTHING
    verifies, peek confidence is unreliable, so the higher locator score (the
    scanned valuation memo) must win, not the readable off-type doc."""
    scanned_memo = tmp_path / "Valuation.pdf"
    make_scanned_pdf(scanned_memo, [CLIENT_MEMO_LINES])  # UNVERIFIED, conf 0.0
    ddq = _pdf(tmp_path, "DDQ Responses.docx.pdf",
               ["Diligence Information Request List", "Please provide the following items",
                "Question 1", "Question 2"])  # readable, OTHER, conf > 0
    ambiguous = LocateResult(
        status=ResolutionStatus.AMBIGUOUS, query=_query(),
        candidates=[_candidate(scanned_memo, 85.0), _candidate(ddq, 84.0)],
        evidence="scores within min_gap",
    )
    reranked, verdicts = verify_and_rerank(ambiguous, default_config)
    assert verdicts[str(scanned_memo)].status is VerifyStatus.UNVERIFIED
    assert verdicts[str(ddq)].status is VerifyStatus.UNVERIFIED
    assert reranked.status is ResolutionStatus.FOUND
    assert reranked.winner is not None and reranked.winner.record.file_path == str(scanned_memo)


def test_rerank_keeps_subthreshold_ambiguous_for_human(tmp_path: Path, default_config) -> None:
    """Auto-select never accepts candidates below min_accept_score (archived
    priors / period-only fallbacks): those stay AMBIGUOUS for a human pick."""
    below = default_config.locator.min_accept_score - 10.0
    a = _pdf(tmp_path, "weak_a.pdf", CLIENT_MEMO_LINES)
    b = _pdf(tmp_path, "weak_b.pdf", CLIENT_MEMO_LINES)
    ambiguous = LocateResult(
        status=ResolutionStatus.AMBIGUOUS, query=_query(),
        candidates=[_candidate(a, below), _candidate(b, below - 2.0)],
        evidence="period fallback below accept",
    )
    reranked, _ = verify_and_rerank(ambiguous, default_config)
    assert reranked.status is ResolutionStatus.AMBIGUOUS
    assert reranked.winner is None


def test_rerank_all_rejected_becomes_not_found(tmp_path: Path, default_config) -> None:
    hl = _pdf(tmp_path, "hl1.pdf", HL_REPORT_LINES)
    found = LocateResult(
        status=ResolutionStatus.FOUND, query=_query(), candidates=[_candidate(hl, 88.0)],
        winner=None, evidence="",
    )
    reranked, _ = verify_and_rerank(found, default_config)
    assert reranked.status is ResolutionStatus.NOT_FOUND
    assert "rejected by content verification" in reranked.evidence
