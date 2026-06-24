"""Small-doc collapse: when the LLM payload is at most
`llm.single_call_max_pages` pages, `_build_groups` makes ONE call over the whole
document + all fields regardless of band_batched (cheaper/faster for short client
memos); larger payloads still band-batch."""

from __future__ import annotations

import pytest

from pv_extractor.config import load_config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.escalate import _build_groups
from pv_extractor.llm.payload import MemoPayload
from pv_extractor.models import EscalationField

HEADERS = ["Fund Name", "Gross IRR %", "MOIC", "Primary Methodology", "NAV ($M)"]


@pytest.fixture(scope="module")
def schema_by_header():
    return {f.header: f for f in load_schema_fields()}


def _plan(schema_by_header):
    fields = [schema_by_header[h] for h in HEADERS if h in schema_by_header]
    assert len(fields) >= 3, "expected schema headers changed"
    return [
        EscalationField(
            field=f.header, col_index=f.col_index, band=f.band,
            reason="required_empty", candidate_pages=[1],
        )
        for f in fields
    ]


def _payload(n_pages: int) -> MemoPayload:
    return MemoPayload(
        directory=__import__("pathlib").Path("."),
        page_blocks={n: f"--- page {n} (TEXT) ---\nsome content" for n in range(1, n_pages + 1)},
        ocr_hostile=False,
    )


def test_small_payload_collapses_to_one_call(schema_by_header):
    cfg = load_config()
    cfg.llm.band_batched = True            # band-batching ON ...
    cfg.llm.single_call_max_pages = 8
    groups = _build_groups(_plan(schema_by_header), _payload(3), schema_by_header, cfg)
    assert len(groups) == 1                # ... but a 3-page doc still collapses
    assert groups[0].label == "memo"
    # every escalated field rides the single call
    assert {f.field for f in groups[0].fields} == {
        h for h in HEADERS if h in schema_by_header
    }


def test_collapse_chunks_by_max_fields_per_call(schema_by_header):
    """The collapse must NOT put all fields in one giant schema — the inline
    --json-schema arg would blow the Windows ~32 KB command-line limit
    ([WinError 206]). It chunks by max_fields_per_call, all chunks on the same
    pages."""
    cfg = load_config()
    cfg.llm.band_batched = True
    cfg.llm.single_call_max_pages = 8
    cfg.llm.max_fields_per_call = 2          # force chunking
    plan = _plan(schema_by_header)
    groups = _build_groups(plan, _payload(3), schema_by_header, cfg)
    assert len(groups) > 1                   # split, not one giant call
    assert all(len(g.fields) <= 2 for g in groups)
    # no field dropped or duplicated; all chunks share the same page set
    assert sum(len(g.fields) for g in groups) == len(plan)
    assert all(g.pages == groups[0].pages for g in groups)


def test_collapse_can_be_disabled(schema_by_header):
    cfg = load_config()
    cfg.llm.band_batched = True
    cfg.llm.single_call_max_pages = 0      # disabled -> honor band_batched
    groups = _build_groups(_plan(schema_by_header), _payload(3), schema_by_header, cfg)
    # band path labels groups by band name / sweep, never the collapse "memo"
    assert all(g.label != "memo" for g in groups)
    # field-conservation invariant holds regardless of grouping
    assert {f.field for g in groups for f in g.fields} == {
        h for h in HEADERS if h in schema_by_header
    }


def test_large_payload_band_batches(schema_by_header):
    cfg = load_config()
    cfg.llm.band_batched = True
    cfg.llm.single_call_max_pages = 4
    groups = _build_groups(_plan(schema_by_header), _payload(12), schema_by_header, cfg)
    assert all(g.label != "memo" for g in groups)  # not collapsed (12 > 4)
