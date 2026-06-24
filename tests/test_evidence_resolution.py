from __future__ import annotations

import json
from pathlib import Path

from pv_extractor.api import review_service, runs_service
from pv_extractor.evidence import resolve_quote_to_words
from pv_extractor.models import EvidenceMatchMethod, EvidenceWord


def _word(text: str, idx: int, y: float = 10.0) -> EvidenceWord:
    x0 = 10.0 + idx * 35.0
    return EvidenceWord(id=f"w{idx}", text=text, bbox=(x0, y, x0 + 28.0, y + 10.0))


def test_resolve_quote_normal_text() -> None:
    words = [_word("Enterprise", 0), _word("Value", 1), _word("$545.0M", 2)]
    result = resolve_quote_to_words(quote="Enterprise Value $545.0M", page_number=1, words=words)
    assert result.status == "resolved"
    assert result.evidence_ref is not None
    assert result.evidence_ref.match_method is EvidenceMatchMethod.native_text
    assert result.bbox == (10.0, 10.0, 108.0, 20.0)


def test_resolve_quote_wrapped_text() -> None:
    words = [_word("Gross", 0), _word("IRR", 1), _word("was", 0, 28.0), _word("12.5%", 1, 28.0)]
    result = resolve_quote_to_words(quote="Gross IRR was 12.5%", page_number=2, words=words)
    assert result.status == "resolved"
    assert result.bbox == (10.0, 10.0, 73.0, 38.0)


def test_resolve_quote_hyphenation() -> None:
    words = [_word("invest-", 0), _word("ment", 1), _word("period", 2)]
    result = resolve_quote_to_words(quote="investment period", page_number=1, words=words)
    assert result.status == "resolved"
    assert result.word_ids == ["w0", "w1", "w2"]


def test_resolve_quote_financial_punctuation() -> None:
    words = [_word("$", 0), _word("1,234.5", 1), _word("(12.5", 2), _word("%)", 3)]
    result = resolve_quote_to_words(quote="$ 1,234.5 (12.5 %)", page_number=1, words=words)
    assert result.status == "resolved"
    assert result.score >= 0.98


def test_resolve_quote_duplicate_phrase_uses_first_window() -> None:
    words = [
        _word("Gross", 0, 10.0), _word("IRR", 1, 10.0), _word("12.5%", 2, 10.0),
        _word("Gross", 0, 100.0), _word("IRR", 1, 100.0), _word("12.5%", 2, 100.0),
    ]
    result = resolve_quote_to_words(quote="Gross IRR 12.5%", page_number=1, words=words)
    assert result.status == "resolved"
    assert result.bbox is not None and result.bbox[1] == 10.0


def test_resolve_quote_no_match_keeps_page_only_reference() -> None:
    result = resolve_quote_to_words(quote="Net Debt $120M", page_number=3, words=[_word("Revenue", 0)])
    assert result.status == "no_match"
    assert result.bbox is None
    assert result.evidence_ref is not None
    assert result.evidence_ref.match_method is EvidenceMatchMethod.page_only
    assert result.evidence_ref.no_geometry_reason


def _write_audit(run_dir: Path, audit: dict) -> None:
    audit_dir = run_dir / runs_service.AUDIT_DIR
    audit_dir.mkdir(parents=True)
    (audit_dir / f"{audit['memo_id']}.json").write_text(json.dumps(audit), encoding="utf-8")


def test_review_queue_stable_ids_dedupes_and_separates_memo_issues(tmp_path: Path, project_root: Path) -> None:
    from pv_extractor.config import load_config

    config = load_config(project_root / "config.example.yaml")
    audit = {
        "run_id": "RUN_TEST",
        "memo_id": "MEMO_001",
        "file_name": "memo.pdf",
        "file_path": "/pv/memo.pdf",
        "reader": "pdf",
        "page_count": 2,
        "page_classes": {"1": "TEXT"},
        "memo_flags": [
            {
                "category": "qa",
                "description": "no valuation value found",
                "severity": "hard_fail",
                "reviewer_attention": True,
                "code": "no_valuation_value",
                "origin": "qa",
            }
        ],
        "assets": [
            {
                "row_memo_id": "MEMO_001",
                "qa_status": "qa_fail",
                "hits": [
                    {
                        "field": "Gross IRR",
                        "col_index": 10,
                        "band": "RETURNS",
                        "raw_text": "12.5%",
                        "value": 12.5,
                        "page": 1,
                        "bbox": [10, 20, 80, 40],
                        "method": "deterministic",
                        "confidence": 0.4,
                        "evidence": "Gross IRR 12.5%",
                        "conflicts": [],
                    }
                ],
                "flags": [
                    {
                        "category": "range",
                        "description": "Gross IRR: outside range",
                        "severity": "warning",
                        "field": "Gross IRR",
                        "code": "percent_range",
                    },
                    {
                        "category": "range",
                        "description": "Gross IRR: outside range",
                        "severity": "warning",
                        "field": "Gross IRR",
                        "code": "percent_range",
                    },
                ],
            }
        ],
    }
    _write_audit(tmp_path, audit)
    items, memo_issues = review_service.build_review(tmp_path, config)
    assert len(items) == 1
    first_id = items[0].id
    assert "::flag::" in first_id and not first_id.endswith("::0")
    assert items[0].issue_descriptions == ["Gross IRR: outside range"]
    assert items[0].qa_fail_reasons == []
    assert len(memo_issues) == 1
    assert memo_issues[0].descriptions == ["no valuation value found"]

    audit["assets"][0]["flags"].reverse()
    _write_audit(tmp_path / "rerun", audit)
    rerun_items, _ = review_service.build_review(tmp_path / "rerun", config)
    assert rerun_items[0].id == first_id
