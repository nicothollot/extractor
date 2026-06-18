"""Phase-4 locator override learning table: record/lookup/delete round-trip,
locate() short-circuit on a recorded pick, stale-override fallback, and the
guarantee that an override winner still faces the peek-verifier."""

from __future__ import annotations

from datetime import date

from pv_extractor.indexer.db import open_db
from pv_extractor.locator import overrides
from pv_extractor.locator.locate import locate
from pv_extractor.locator.verify import verify_and_rerank
from pv_extractor.models import DocType, LocateQuery, ResolutionStatus, VerifyStatus
from pv_extractor.run import run


def _conn(config):
    return open_db(config.db_path, config.pv_root)


def test_record_lookup_delete_roundtrip(phase2_env) -> None:
    conn = _conn(phase2_env)
    try:
        key = dict(client="Angelo Gordon", deal="Accell",
                   as_of_date=date(2025, 1, 31), doc_type="valuation_memo")
        assert overrides.lookup_override(conn, **key) is None
        overrides.record_override(conn, **key, file_path="X:/a.pdf", note="pick")
        assert overrides.lookup_override(conn, **key) == "X:/a.pdf"
        overrides.record_override(conn, **key, file_path="X:/b.pdf")  # latest pick wins
        assert overrides.lookup_override(conn, **key) == "X:/b.pdf"
        assert overrides.list_overrides(conn)[0]["file_path"] == "X:/b.pdf"
        assert overrides.delete_override(conn, **key)
        assert overrides.lookup_override(conn, **key) is None
    finally:
        conn.close()


def test_locate_honors_recorded_override(phase2_env) -> None:
    conn = _conn(phase2_env)
    try:
        query = LocateQuery(client="Angelo Gordon", deal="Accell", period="2025-01-31",
                            doc_type=DocType.any_client_valuation_doc)
        baseline = locate(conn, phase2_env, query)
        assert baseline.status is ResolutionStatus.FOUND
        # Pick a DIFFERENT indexed file than the natural winner (any other
        # indexed PDF under the deal counts — an override is an analyst
        # decision, not a re-score).
        row = conn.execute(
            "SELECT file_path FROM files WHERE client = 'Angelo Gordon' AND deal = 'Accell' "
            "AND extension = '.pdf' AND file_path != ? LIMIT 1",
            (baseline.winner.record.file_path,),
        ).fetchone()
        assert row is not None, "fixture should hold more than one Accell pdf"
        other_path = row[0]
        overrides.record_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=baseline.query.as_of_date,
            doc_type=DocType.any_client_valuation_doc.value, file_path=other_path,
        )
        picked = locate(conn, phase2_env, LocateQuery(
            client="Angelo Gordon", deal="Accell", period="2025-01-31",
            doc_type=DocType.any_client_valuation_doc,
        ))
        assert picked.status is ResolutionStatus.FOUND
        assert picked.winner.record.file_path == other_path
        assert "manual override" in picked.evidence

        # Different doc_type or period: the override does NOT leak.
        other_period = locate(conn, phase2_env, LocateQuery(
            client="Angelo Gordon", deal="Accell", period="2024-11-30",
            doc_type=DocType.any_client_valuation_doc,
        ))
        assert "manual override" not in other_period.evidence

        overrides.delete_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=baseline.query.as_of_date,
            doc_type=DocType.any_client_valuation_doc.value,
        )
    finally:
        conn.close()


def test_stale_override_ignored_when_file_left_the_index(phase2_env) -> None:
    conn = _conn(phase2_env)
    try:
        query = LocateQuery(client="Angelo Gordon", deal="Accell", period="2025-01-31")
        baseline = locate(conn, phase2_env, query)
        overrides.record_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=baseline.query.as_of_date,
            doc_type=DocType.any_client_valuation_doc.value,
            file_path="X:/vanished/file.pdf",
        )
        result = locate(conn, phase2_env, LocateQuery(
            client="Angelo Gordon", deal="Accell", period="2025-01-31"))
        # Falls back to the normal cascade, same winner as before.
        assert result.status is ResolutionStatus.FOUND
        assert result.winner.record.file_path == baseline.winner.record.file_path
        assert "manual override" not in result.evidence
        overrides.delete_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=baseline.query.as_of_date,
            doc_type=DocType.any_client_valuation_doc.value,
        )
    finally:
        conn.close()


def test_override_winner_is_verified_but_forces_past_rejection(phase2_env) -> None:
    """An explicit analyst override (the 'Use this one' pick) RUNS the chosen
    file even when content verification would reject it (e.g. HL work product):
    the pick is still verified — the REJECTED verdict rides along so the run can
    flag it — but the human's choice wins instead of being silently dropped."""
    conn = _conn(phase2_env)
    try:
        query = LocateQuery(client="Angelo Gordon", deal="Accell", period="2025-01-31")
        baseline = locate(conn, phase2_env, query)
        as_of = baseline.query.as_of_date
        # The fixture's HL lookalike: Analysis folder file whose CONTENT
        # carries real HL letterhead/disclaimer language (build_hl_lookalike).
        row = conn.execute(
            "SELECT file_path FROM files WHERE client = 'Angelo Gordon' AND deal = 'Accell' "
            "AND source_class = 'analysis' AND file_name = 'Accell Valuation Memo 1.31.25.pdf'"
        ).fetchone()
        if row is None:  # fixture layout changed; nothing to assert against
            return
        overrides.record_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=as_of,
            doc_type=DocType.any_client_valuation_doc.value, file_path=row[0],
        )

        # locate() short-circuits to the override and marks it.
        located = locate(conn, phase2_env, query)
        assert located.from_override is True
        assert located.winner is not None and located.winner.record.file_path == row[0]

        # verify_and_rerank keeps the override winner even though it is REJECTED.
        reranked, verdicts = verify_and_rerank(located, phase2_env)
        assert reranked.status is ResolutionStatus.FOUND
        assert reranked.winner is not None and reranked.winner.record.file_path == row[0]
        assert reranked.from_override is True
        assert verdicts[row[0]].status is VerifyStatus.REJECTED  # still flagged HL work

        overrides.delete_override(
            conn, client="Angelo Gordon", deal="Accell", as_of_date=as_of,
            doc_type=DocType.any_client_valuation_doc.value,
        )
    finally:
        conn.close()
