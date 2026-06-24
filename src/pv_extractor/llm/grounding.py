"""Quote grounding shared by deterministic evidence checks and LLM providers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from pv_extractor.evidence import resolve_quote_to_words
from pv_extractor.llm.payload import MemoPayload
from pv_extractor.models import EvidenceMatchMethod, EvidenceRef
from pv_extractor.normalize import normalize_evidence_text

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?|[%$€£x]")


@dataclass(frozen=True)
class GroundingResult:
    status: str  # grounded | ungrounded | unverifiable
    score: float
    matched_text: str
    page: int | None
    reason: str
    bbox: tuple[float, float, float, float] | None = None
    evidence_ref: EvidenceRef | None = None


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_evidence_text(text))


def _window_match(needle: str, haystack: str) -> tuple[float, str]:
    needle_tokens = _tokens(needle)
    hay_tokens = _tokens(haystack)
    if not needle_tokens or not hay_tokens:
        return 0.0, ""
    n = len(needle_tokens)
    best_score = 0.0
    best_text = ""
    min_width = max(1, n - 2)
    max_width = min(len(hay_tokens), n + 3)
    needle_joined = " ".join(needle_tokens)
    for width in range(min_width, max_width + 1):
        for start in range(0, len(hay_tokens) - width + 1):
            window = hay_tokens[start : start + width]
            joined = " ".join(window)
            score = fuzz.ratio(needle_joined, joined) / 100.0
            if score > best_score:
                best_score = score
                best_text = joined
                if score >= 0.999:
                    return best_score, best_text
    return best_score, best_text


def ground_quote(
    quote: str,
    page: int | None,
    payload: MemoPayload,
    fuzzy_threshold: int,
) -> GroundingResult:
    if not quote.strip() or page is None or page not in payload.page_texts:
        return GroundingResult("ungrounded", 0.0, "", page, "missing quote or page")
    page_text = payload.page_texts.get(page) or ""
    normalized_page = normalize_evidence_text(page_text)
    normalized_quote = normalize_evidence_text(quote)
    if not normalized_page:
        return GroundingResult("unverifiable", 0.0, "", page, "no local page text")
    words = payload.page_words.get(page, [])
    match_method = (
        EvidenceMatchMethod.ocr_word_alignment
        if any(word.source == "ocr" for word in words)
        else EvidenceMatchMethod.native_text
    )
    provider = None
    extraction_method = "llm"
    if normalized_quote and normalized_quote in normalized_page:
        resolution = resolve_quote_to_words(
            quote=quote,
            page_number=page,
            words=words,
            provider=provider,
            extraction_method=extraction_method,
            match_method=match_method,
            threshold=(fuzzy_threshold / 100.0) if payload.page_kind(page) == "image" else 0.98,
        )
        ref = resolution.evidence_ref
        if resolution.status != "resolved":
            ref = EvidenceRef(
                display_page=page,
                quote=quote[:500],
                match_method=EvidenceMatchMethod.page_only,
                match_score=1.0,
                provenance="llm_quote_grounding",
                extraction_method=extraction_method,
                no_geometry_reason=resolution.reason or "quote matched page text but no bbox was resolved",
            )
        return GroundingResult(
            "grounded", 1.0, quote[:200], page, "normalized substring",
            bbox=resolution.bbox, evidence_ref=ref,
        )

    score, matched_text = _window_match(quote, page_text)
    kind = payload.page_kind(page)
    threshold = (fuzzy_threshold / 100.0) if kind == "image" else 0.98
    if score >= threshold:
        resolution = resolve_quote_to_words(
            quote=quote,
            page_number=page,
            words=words,
            provider=provider,
            extraction_method=extraction_method,
            match_method=match_method,
            threshold=threshold,
        )
        ref = resolution.evidence_ref
        if resolution.status != "resolved":
            ref = EvidenceRef(
                display_page=page,
                quote=quote[:500],
                raw_text=matched_text[:500],
                match_method=EvidenceMatchMethod.page_only,
                match_score=score,
                provenance="llm_quote_grounding",
                extraction_method=extraction_method,
                no_geometry_reason=resolution.reason or "quote matched page text but no bbox was resolved",
            )
        return GroundingResult(
            "grounded", score, matched_text[:200], page, "token window match",
            bbox=resolution.bbox, evidence_ref=ref,
        )
    ref = EvidenceRef(
        display_page=page,
        quote=quote[:500],
        raw_text=matched_text[:500],
        match_method=EvidenceMatchMethod.page_only,
        match_score=score,
        provenance="llm_quote_grounding",
        extraction_method=extraction_method,
        no_geometry_reason="quote did not match cited page text",
    )
    return GroundingResult(
        "ungrounded", score, matched_text[:200], page, "no token window match",
        evidence_ref=ref,
    )
