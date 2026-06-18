"""Audit sidecar writer (D6): output_dir/<run_id>/audit/<memo_id>.json.

The per-cell provenance record the Phase-4 GUI renders: every FieldHit with
its evidence snippet, page, confidence components and conflicts, the
verify/locator evidence, the page-class map, the page->band map, validation
flags and the Phase-3 EscalationPlan. Serialization is deterministic
(sorted keys, fixed separators) so golden tests can byte-compare audits
once volatile fields (timings) are excluded.
"""

from __future__ import annotations

import json
from pathlib import Path

from pv_extractor.io_guard import guarded_open_write
from pv_extractor.models import MemoResult


def audit_payload(result: MemoResult) -> dict:
    return result.model_dump(mode="json")


def write_audit(result: MemoResult, run_dir: str | Path, pv_root: str) -> Path:
    audit_dir = Path(run_dir) / "audit"
    path = audit_dir / f"{result.memo_id}.json"
    with guarded_open_write(path, pv_root) as fh:
        json.dump(audit_payload(result), fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return path
