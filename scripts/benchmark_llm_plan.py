#!/usr/bin/env python3
"""Print a content-free LLM benchmark table for a completed run directory.

The table is intentionally metadata-only. It reads diagnostics/cost/audit JSON
when present and never opens source documents or payload text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HEADERS = [
    "Deal",
    "Docs",
    "Model/Effort",
    "Shape",
    "Primary calls",
    "Repair calls",
    "LLM sec",
    "Fields accepted",
    "Unresolved",
    "Grounded %",
    "Cost",
]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ledger(run_dir: Path) -> list[dict]:
    path = run_dir / "llm" / "ledger.jsonl"
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _audit_rows(run_dir: Path) -> list[list[str]]:
    ledger = _ledger(run_dir)
    ledger_by_memo: dict[str, list[dict]] = {}
    for row in ledger:
        ledger_by_memo.setdefault(str(row.get("memo_id") or ""), []).append(row)

    rows: list[list[str]] = []
    for audit_path in sorted(run_dir.glob("audit_*.json")):
        audit = _read_json(audit_path)
        memo_id = str(audit.get("memo_id") or audit_path.stem.removeprefix("audit_"))
        esc = audit.get("escalation") if isinstance(audit.get("escalation"), dict) else {}
        attempts = esc.get("attempts") if isinstance(esc.get("attempts"), list) else []
        plan = esc.get("diagnostics", {}).get("resolved_launch_plan", {}) if isinstance(esc.get("diagnostics"), dict) else {}
        model_effort = ""
        if attempts:
            first = attempts[0]
            model_effort = f"{first.get('model_alias', '')}/{first.get('effort', '')}".strip("/")
        elif isinstance(plan, dict):
            model_effort = f"{plan.get('model', '')}/{plan.get('effort', '')}".strip("/")
        llm_sec = sum(float(a.get("duration_seconds") or 0.0) for a in attempts)
        accepted = len(esc.get("merged_fields") or [])
        unresolved = len(esc.get("not_extractable") or [])
        grounded = sum(int(a.get("fields_grounded") or 0) for a in attempts)
        returned = sum(int(a.get("fields_returned") or 0) for a in attempts)
        grounded_pct = f"{(grounded / returned * 100.0):.0f}%" if returned else ""
        cost = sum(float(row.get("cost_usd") or 0.0) for row in ledger_by_memo.get(memo_id, []))
        rows.append(
            [
                str(audit.get("deal") or memo_id),
                str(plan.get("documents", "")) if isinstance(plan, dict) else "",
                model_effort,
                str(plan.get("execution_shape", "")) if isinstance(plan, dict) else "",
                str(len(attempts)),
                str(plan.get("max_repair_calls", 0)) if isinstance(plan, dict) else "0",
                f"{llm_sec:.2f}",
                str(accepted),
                str(unresolved),
                grounded_pct,
                f"${cost:.4f}",
            ]
        )
    return rows


def _print_table(rows: list[list[str]]) -> None:
    widths = [len(header) for header in HEADERS]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    fmt = " | ".join("{:<" + str(width) + "}" for width in widths)
    print(fmt.format(*HEADERS))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt.format(*row))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Completed output/RUN_* directory")
    args = parser.parse_args()
    rows = _audit_rows(args.run_dir)
    _print_table(rows)


if __name__ == "__main__":
    main()
