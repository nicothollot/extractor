"""Cost ledger + token estimation (rule 9).

When a local LLM provider reports token usage / cost the ledger records ACTUAL
numbers. When only a matching configured price table is available, tokens are
estimated from the prompt text and page images and clearly labeled ESTIMATED.
If pricing is unavailable for the provider/model, cost is recorded as zero with
``cost_source=unavailable`` rather than invented. The ledger is a JSONL file
under the run directory — `pv-extractor costs --run <id>` renders it — and a
thread-safe budget tracker enforces the hard per-run cap.
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
    """Raised by reserve() when a job is abandoned at the budget cap.

    Two situations raise it: (a) NON-interactive runs (CLI — no pause callback)
    hit the cap and defer, exactly as before; (b) an interactive (GUI) run that
    is PAUSED at the cap and the user then CANCELS — the parked reserve() wakes
    and raises so the memo defers and the run ends cleanly."""


class BudgetTracker:
    """Thread-safe projected-spend tracker for one run.

    Interactive (GUI) runs PAUSE at the cap: reserve() blocks on a condition
    until the user raises the cap, removes it, or cancels — parked provider
    agents then resume. Non-interactive runs (CLI, no on_pause callback) keep
    the original hard-cap behavior: reserve() raises BudgetExceeded immediately
    so the memo defers (LLM_DEFERRED). budget_usd=None means no cap (unlimited).
    """

    def __init__(
        self,
        budget_usd: float | None,
        *,
        on_pause=None,
        cancel_event: "threading.Event | None" = None,
    ) -> None:
        self.budget_usd = budget_usd
        self._committed = 0.0
        self._cond = threading.Condition()
        self._on_pause = on_pause  # called once when the first thread parks
        self._cancel_event = cancel_event
        self._paused = False
        self._cancelled = False

    @property
    def committed_usd(self) -> float:
        with self._cond:
            return round(self._committed, 6)

    @property
    def interactive(self) -> bool:
        return self._on_pause is not None

    def _cancel_requested_locked(self) -> bool:
        return self._cancelled or (self._cancel_event is not None and self._cancel_event.is_set())

    def _fits_locked(self, estimate_usd: float) -> bool:
        return self.budget_usd is None or self._committed + estimate_usd <= self.budget_usd

    def reserve(self, estimate_usd: float) -> None:
        """Commit a job's projected cost. If it would pass the cap: a
        non-interactive run raises BudgetExceeded (defer); an interactive run
        parks until raise/remove/cancel. A cancel (own flag or the run's
        cancel_event) raises BudgetExceeded so the memo defers."""
        with self._cond:
            while True:
                if self._cancel_requested_locked():
                    raise BudgetExceeded("run cancelled at budget pause")
                if self._fits_locked(estimate_usd):
                    self._committed += estimate_usd
                    return
                if not self.interactive:
                    raise BudgetExceeded(
                        f"projected spend ${self._committed + estimate_usd:.4f} exceeds "
                        f"budget ${self.budget_usd:.2f}"
                    )
                # Interactive: pause and wait for the user. Only the FIRST thread
                # to park fires the pause callback (the latch); the rest wait.
                if not self._paused:
                    self._paused = True
                    if self._on_pause is not None:
                        self._on_pause(round(self._committed, 6), self.budget_usd)
                self._cond.wait()

    def raise_budget(self, new_total_usd: float) -> None:
        """Lift the cap to a new total and wake parked agents."""
        with self._cond:
            self.budget_usd = float(new_total_usd)
            self._paused = False
            self._cond.notify_all()

    def remove_cap(self) -> None:
        """Remove the cap entirely (unlimited) and wake parked agents."""
        with self._cond:
            self.budget_usd = None
            self._paused = False
            self._cond.notify_all()

    def cancel(self) -> None:
        """Wake parked agents so their reserve() raises BudgetExceeded."""
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    def settle(self, estimate_usd: float, actual_usd: float) -> None:
        """Replace a reservation with the settled (actual or final-estimate)
        cost once the call finished."""
        with self._cond:
            self._committed += actual_usd - estimate_usd


class CostLedger:
    """Append-only JSONL ledger: one line per local LLM attempt."""

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
    """Totals for CLI display: spend split by actual/estimated/unavailable."""
    total = sum(e.get("cost_usd", 0.0) for e in entries)
    actual = sum(e.get("cost_usd", 0.0) for e in entries if e.get("cost_source") == "actual")
    estimated = sum(e.get("cost_usd", 0.0) for e in entries if e.get("cost_source") == "estimated")
    return {
        "attempts": len(entries),
        "cache_hits": sum(1 for e in entries if e.get("from_cache")),
        "memos": len({e.get("memo_id") for e in entries}),
        "total_usd": round(total, 4),
        "actual_usd": round(actual, 4),
        "estimated_usd": round(estimated, 4),
        "unavailable_attempts": sum(1 for e in entries if e.get("cost_source") == "unavailable"),
        "input_tokens": sum((e.get("usage") or {}).get("input_tokens", 0) for e in entries),
        "output_tokens": sum((e.get("usage") or {}).get("output_tokens", 0) for e in entries),
    }
