"""Self-contained extraction smoke test — run this to prove (or disprove) that
the LLM extraction works ON THIS MACHINE, end to end, with YOUR config and a
real local `claude` call. It needs no network share and no real memo: it builds
a tiny synthetic SCANNED valuation page (EBITDA / Net Debt / EV), forces the
full escalate-everything LLM path, and prints what came back.

    .venv/bin/python scripts/diagnose_extraction.py            # WSL/Linux
    .venv\\Scripts\\python scripts\\diagnose_extraction.py      # Windows

What to read:
  * LLM_VERSION must be the version you expect (stale code = wrong number).
  * "VALUES EXTRACTED" with the right numbers => the engine works here.
  * "EVERY CALL FAILED: <error>" => the local `claude` call is the problem
    (auth, the wsl bridge, a CLI flag) — the printed error is the real cause.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import fitz

from pv_extractor.config import load_config
from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm import LLM_VERSION
from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.escalate import LlmSettings, process_memos
from pv_extractor.models import (
    AssetExtraction,
    EscalationField,
    EscalationPlan,
    MemoResult,
    QaStatus,
)

LINES = [
    "BrightNight Power — Valuation Memo as of 2026-06-30",
    "Enterprise Value: $300.0M",
    "EBITDA: $45.2M",
    "Net Debt: $120.0M",
    "Gross IRR: 22.5%   Net IRR: 18.1%   MOIC: 1.8x",
    "Primary methodology: DCF.",
]
TRUTH = {"EBITDA ($M)": 45.2, "Net Debt ($M)": 120.0, "Gross IRR %": 22.5, "MOIC": 1.8}


def _scanned_pdf(path: Path) -> None:
    """Render text to an image and place it as a full-page picture: no text
    layer, so the deterministic engine gets nothing and the LLM vision/OCR path
    is exercised (exactly like the real scanned valuation memos)."""
    doc = fitz.open()
    page = doc.new_page(width=900, height=700)
    y = 60
    for line in LINES:
        page.insert_text((50, y), line, fontsize=15)
        y += 46
    pix = page.get_pixmap(dpi=200)
    out = fitz.open()
    p2 = out.new_page(width=900, height=700)
    p2.insert_image(fitz.Rect(0, 0, 900, 700), pixmap=pix)
    out.save(path)


def main() -> int:
    print(f"\npv_extractor LLM_VERSION = {LLM_VERSION}")
    print("(if this is not the version you just pulled, you are running STALE code)\n")

    config = load_config()
    fields = load_schema_fields()
    by_header = {f.header: f for f in fields}
    skip = {
        b for b in {f.band for f in fields}
        if any(k in b.upper() for k in ("IDENTIFICATION", "QA", "THRESHOLD", "POSITIONAL",
                                        "TRADING COMPS", "TRANSACTION COMPS", "CAP STRUCTURE"))
    }
    escalatable = [f for f in fields if f.band not in skip]

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pdf = tmp / "BrightNight_Valuation.pdf"
        _scanned_pdf(pdf)

        config.output_dir = tmp / "out"
        (tmp / "out").mkdir(parents=True, exist_ok=True)
        memo = MemoResult(
            memo_id="MEMO_DIAG_001", run_id="RUN_DIAG", client="Diagnostic",
            deal="BrightNight", file_path=str(pdf), file_name=pdf.name,
        )
        memo.assets.append(AssetExtraction(
            asset_name="BrightNight", row_memo_id=memo.memo_id, hits=[],
            qa_status=QaStatus.qa_fail,
        ))
        memo.escalation = EscalationPlan(
            memo_id=memo.memo_id, confidence_threshold=0.75,
            fields=[EscalationField(field=f.header, col_index=f.col_index, band=f.band,
                                    reason="qa_fail_rescue", candidate_pages=[1])
                    for f in escalatable],
        )
        settings = LlmSettings(
            enabled=True, mode="manual", manual_model="sonnet", manual_effort="low",
            allow_fable=False, budget_usd=10.0, workers=1, force=True,
        )
        print(f"Running {len(escalatable)} escalatable fields through one sonnet/low call ...\n")
        process_memos([memo], config, settings, fields, run_id="RUN_DIAG",
                      run_dir=tmp / "out" / "RUN_DIAG", client=ClaudeCodeClient(config))

    plan = memo.escalation
    errors = [a.error for a in plan.attempts if a.error]
    ok_attempts = [a for a in plan.attempts if not a.error]
    print(f"status={plan.status}  attempts={len(plan.attempts)}  "
          f"ok={len(ok_attempts)}  failed={len(errors)}  merged={len(plan.merged_fields)}")

    if errors and not ok_attempts:
        print(f"\n*** EVERY CALL FAILED: {errors[0]}")
        print("The local `claude` call is the problem (not the extraction logic). "
              "Fix that error and re-run.\n")
        return 1

    hits = {h.field: h for h in memo.assets[0].hits}
    print("\nExtracted values:")
    good = 0
    for header, truth in TRUTH.items():
        hit = hits.get(header)
        if hit is not None:
            good += 1
            print(f"  OK  {header} = {hit.value}  (expected {truth})  conf={hit.confidence:.2f}")
        else:
            print(f"  --  {header} = <none>  (expected {truth})")
    print(f"\n{'VALUES EXTRACTED — the engine works on this machine.' if good else 'NO VALUES — see flags/errors above.'}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
