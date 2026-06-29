"""Review-flag provenance helpers.

Generated validation/QA flags are intentionally removable before a final
post-LLM validation pass; source/reader/extraction/LLM/run flags must survive.
"""

from __future__ import annotations

import re

from pv_extractor.models import FlagSeverity, ReviewFlag

_WS_RE = re.compile(r"\s+")
_SEVERITY_RANK = {
    FlagSeverity.info: 0,
    FlagSeverity.warning: 1,
    FlagSeverity.hard_fail: 2,
}

GENERATED_FLAG_ORIGINS = {"validation", "qa"}
GENERATED_FLAG_CATEGORIES = {
    "range",
    "cross_field",
    "qoq_threshold",
    "computed_crosscheck",
    "qa",
}


def normalize_flag_text(text: str | None) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def is_generated_flag(flag: ReviewFlag) -> bool:
    origin = (flag.origin or "").strip().lower()
    return origin in GENERATED_FLAG_ORIGINS or flag.category in GENERATED_FLAG_CATEGORIES


def flag_stable_key(flag: ReviewFlag) -> tuple[str, str, str, str, str]:
    """Stable duplicate key: provenance + code + field + normalized message.

    `category` stays in the key so old flags without codes remain separated
    when different subsystems happened to produce the same text.
    """
    return (
        (flag.origin or "").strip().lower(),
        (flag.code or "").strip().lower(),
        (flag.category or "").strip().lower(),
        (flag.field or "").strip().lower(),
        normalize_flag_text(flag.description),
    )


def _usefulness(flag: ReviewFlag) -> tuple[int, int, int, int]:
    severity = _SEVERITY_RANK.get(flag.severity, 0)
    attention = 1 if flag.reviewer_attention else 0
    linked_field = 1 if flag.field else 0
    detail = len(flag.description or "")
    return (severity, attention, linked_field, detail)


def deduplicate_review_flags(flags: list[ReviewFlag]) -> list[ReviewFlag]:
    """Deduplicate while keeping first-seen order and the most useful record."""
    keyed: dict[tuple[str, str, str, str, str], tuple[int, ReviewFlag]] = {}
    order: list[tuple[str, str, str, str, str]] = []
    for index, flag in enumerate(flags):
        key = flag_stable_key(flag)
        if key not in keyed:
            keyed[key] = (index, flag)
            order.append(key)
            continue
        old_index, old_flag = keyed[key]
        if _usefulness(flag) > _usefulness(old_flag):
            keyed[key] = (old_index, flag)
    order.sort(key=lambda key: keyed[key][0])
    return [keyed[key][1] for key in order]
