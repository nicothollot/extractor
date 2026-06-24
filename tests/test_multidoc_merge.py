"""Feature: multi-document merge — several source files for one investment
collapse into ONE row, each field taking the highest-confidence value across
documents (tagged with the document it came from)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pv_extractor.config import load_config
from pv_extractor.extract.engine import load_band_routing, load_schema_fields
from pv_extractor.models import AssetExtraction, FieldHit, MemoResult
from pv_extractor.run import _merge_memo_group
from pv_extractor.validate import load_rules
from pv_extractor.write import WorkbookWriter, copy_template

REFERENCE_TEMPLATE = Path(__file__).resolve().parent.parent / "reference" / "master_index_v4.xlsx"


def _writer(tmp_path: Path, schema_fields):
    wb = tmp_path / "out.xlsx"
    copy_template(REFERENCE_TEMPLATE, wb, pv_root=str(tmp_path / "fake_pv"))
    return WorkbookWriter(wb, schema_fields, pv_root=str(tmp_path / "fake_pv"))


def test_merge_picks_highest_confidence_per_field(tmp_path: Path) -> None:
    config = load_config()
    schema_fields = load_schema_fields()
    sbh = {f.header: f for f in schema_fields}
    routing = load_band_routing()
    ruleset = load_rules(config.validation.rules_path)
    writer = _writer(tmp_path, schema_fields)

    def hit(header: str, value, conf: float, method: str = "deterministic") -> FieldHit:
        f = sbh[header]
        return FieldHit(field=header, col_index=f.col_index, band=f.band,
                        value=value, confidence=conf, method=method, evidence="e")

    def meta(header: str, value) -> FieldHit:
        f = sbh[header]
        return FieldHit(field=header, col_index=f.col_index, band=f.band,
                        value=value, confidence=1.0, method="metadata", evidence="meta")

    # doc A (primary): a low-confidence EV, nothing else of value
    primary = MemoResult(
        memo_id="MEMO_X_001", run_id="RUN_X", client="C", deal="D",
        file_path="/pv/A.pdf", file_name="A.pdf", as_of_date=date(2026, 6, 30),
        assets=[AssetExtraction(
            asset_name="Acme", row_memo_id="MEMO_X_001",
            hits=[
                meta("\U0001f511 Memo ID", "MEMO_X_001"),
                meta("Portfolio Company", "Acme"),
                hit("Implied EV ($M)", 100.0, 0.30),
            ],
        )],
    )
    # doc B (extra): a high-confidence EV (should win) + Equity Value only it has
    extra = MemoResult(
        memo_id="MEMO_X_002", run_id="RUN_X", client="C", deal="D",
        file_path="/pv/B.pdf", file_name="B.pdf", as_of_date=date(2026, 6, 30),
        assets=[AssetExtraction(
            asset_name="Acme", row_memo_id="MEMO_X_002",
            hits=[
                hit("Implied EV ($M)", 200.0, 0.80),
                hit("Implied Equity Value 100% ($M)", 1444.0, 0.90),
            ],
        )],
    )

    merged = _merge_memo_group(primary, [primary, extra], config, sbh, ruleset, routing, writer)

    assert len(merged.assets) == 1
    by_field = {h.field: h for h in merged.assets[0].hits}
    # EV: B's 0.80 beats A's 0.30
    assert by_field["Implied EV ($M)"].value == 200.0
    assert by_field["Implied EV ($M)"].source_file == "B.pdf"
    # Equity value only B had it
    assert by_field["Implied Equity Value 100% ($M)"].value == 1444.0
    assert by_field["Implied Equity Value 100% ($M)"].source_file == "B.pdf"
    # identity stays the primary's
    assert by_field["\U0001f511 Memo ID"].value == "MEMO_X_001"
    # a valuation value is present -> the merged row is not a qa_fail for that
    reasons = [f.description for f in merged.assets[0].flags]
    assert not any("no valuation value found" in r for r in reasons)
    # provenance: the merge is recorded, never silent
    assert any("Merged from 2 source documents" in f.description for f in merged.memo_flags)


def test_single_document_group_is_unchanged(tmp_path: Path) -> None:
    """A group of one (no extras) passes through _merge_assembled untouched."""
    from pv_extractor.run import _WorkItem, _merge_assembled

    config = load_config()
    schema_fields = load_schema_fields()
    sbh = {f.header: f for f in schema_fields}
    routing = load_band_routing()
    ruleset = load_rules(config.validation.rules_path)
    writer = _writer(tmp_path, schema_fields)

    memo = MemoResult(
        memo_id="MEMO_Y_001", run_id="RUN_Y", client="C", deal="D",
        file_path="/pv/A.pdf", file_name="A.pdf",
        assets=[AssetExtraction(asset_name="Acme", row_memo_id="MEMO_Y_001", hits=[])],
    )
    item = _WorkItem(client="C", deal="D", locate_result=None)  # merge_key None
    out = _merge_assembled([(item, memo)], config, sbh, ruleset, routing, writer)
    assert out == [(item, memo)]
