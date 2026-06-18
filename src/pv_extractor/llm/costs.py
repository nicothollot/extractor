"""Cost ledger + token estimation (rule 9).

When Claude Code reports token usage / cost the ledger records ACTUAL
numbers; otherwise tokens are estimated from the prompt text and page images
(heuristics in config.llm) and clearly labeled ESTIMATED. The ledger is a
JSONL file under the run directory — `pv-extractor costs --run <id>` renders
it — and a thread-safe budget tracker enforces the hard per-run cap: once
projected spend would exceed the budget, jobs stop being submitted and the
remaining memos are marked LLM_DEFERRED (the run still finishes cleanly).
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

from pv_extractor.config import LlmConfig
from pv_extractor.io_guard import guarded_open_write, open_read
from pv_extractor.models import LlmAttempt, LlmUsage

LEDGER_FILENAME = "cost_ledger.jsonl"


def estimate_usage(
    *, prompt_chars: int, image_count: int, field_count: int, cfg: LlmConfig,
    image_megapixels: float | None = None,
) -> LlmUsage:
    """ESTIMATED token usage for one extraction call (no cache assumed)."""
    text_tokens = math.ceil(prompt_chars / max(cfg.chars_per_token, 1.0))
    megapixels = image_megapixels if image_megapixels is not None else (
        image_count * (cfg.image_max_long_edge * cfg.image_max_long_edge * 0.77) / 1_000_000.0
    )
    image_tokens = math.ceil(megapixels * cfg.image_tokens_per_megapixel)
    output_tokens = cfg.output_tokens_base + field_count * cfg.output_tokens_per_field
    return LlmUsage(
        input_tokens=text_tokens + image_tokens,
        output_tokens=output_tokens,
        source="estimated",
    )


class BudgetExceeded(Exception):
    """Raised by reserve() when a job would push spend past the cap."""


class BudgetTracker:
    """Thread-safe projected-spend tracker for one run."""

    def __init__(self, budget_usd: float) -> None:
        self.budget_usd = budget_usd
        self._committed = 0.0
        self._lock = threading.Lock()

    @property
    def committed_usd(self) -> float:
        with self._lock:
            return round(self._committed, 6)

    def reserve(self, estimate_usd: float) -> None:
        """Commit a job's projected cost; BudgetExceeded if it would pass
        the cap (the job must then be deferred, never submitted)."""
        with self._lock:
            if self._committed + estimate_usd > self.budget_usd:
                raise BudgetExceeded(
                    f"projected spend ${self._committed + estimate_usd:.4f} exceeds "
                    f"budget ${self.budget_usd:.2f}"
                )
            self._committed += estimate_usd

    def settle(self, estimate_usd: float, actual_usd: float) -> None:
        """Replace a reservation with the settled (actual or final-estimate)
        cost once the call finished."""
        with self._lock:
            self._committed += actual_usd - estimate_usd


class CostLedger:
    """Append-only JSONL ledger: one line per Claude Code attempt."""

    def __init__(self, path: Path, pv_root: str) -> None:
        self.path = path
        self.pv_root = pv_root
        self._lock = threading.Lock()

    def append(self, *, run_id: str, memo_id: str, attempt: LlmAttempt) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id,
            "memo_id": memo_id,
            **attempt.model_dump(mode="json"),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with guarded_open_write(self.path, self.pv_root, mode="a") as fh:
                fh.write(line + "\n")


def read_ledger(path: Path) -> list[dict]:
    with open_read(path) as fh:
        text = fh.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def summarize_ledger(entries: list[dict]) -> dict:
    """Totals for CLI display: spend split by actual vs estimated source."""
    total = sum(e.get("cost_usd", 0.0) for e in entries)
    actual = sum(e.get("cost_usd", 0.0) for e in entries if e.get("cost_source") == "actual")
    return {
        "attempts": len(entries),
        "cache_hits": sum(1 for e in entries if e.get("from_cache")),
        "memos": len({e.get("memo_id") for e in entries}),
        "total_usd": round(total, 4),
        "actual_usd": round(actual, 4),
        "estimated_usd": round(total - actual, 4),
        "input_tokens": sum((e.get("usage") or {}).get("input_tokens", 0) for e in entries),
        "output_tokens": sum((e.get("usage") or {}).get("output_tokens", 0) for e in entries),
    }
