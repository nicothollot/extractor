"""Evidence quote-to-geometry resolution.

All bboxes in this module are PDF points in PyMuPDF page coordinates:
`(x0, y0, x1, y1)`, top-left origin, `page.rect` units. The frontend converts
these to CSS pixels for overlays; render endpoints draw them directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from pv_extractor.models import EvidenceMatchMethod, EvidenceRef, EvidenceWord
from pv_extractor.normalize import normalize_evidence_text

_TOKEN_RE = re.compile(r"[a-z]+|[0-9]+(?:\.[0-9]+)?|[$€£%x]")
_HYPHENATED_BREAK_RE = re.compile(r"(?<=\w)-\s+(?=\w)")


@dataclass(frozen=True)
class EvidenceResolution:
    """Structured result of grounding a quote to a page."""

    status: str  # resolved | page_only | no_match
    score: float
    matched_text: str
    bbox: tuple[float, float, float, float] | None
    word_ids: list[str] = field(default_factory=list)
    reason: str = ""
    evidence_ref: EvidenceRef | None = None


@dataclass(frozen=True)
class _Token:
    text: str
    bbox: tuple[float, float, float, float]
    word_ids: tuple[str, ...]
    original: str


def clamp_bbox(
    bbox: tuple[float, float, float, float] | list[float] | None,
    page_width: float,
    page_height: float,
    *,
    min_size: float = 0.5,
) -> tuple[float, float, float, float] | None:
    """Normalize, sort, and clamp a bbox to a page rectangle."""
    if bbox is None or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in bbox)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    left = max(0.0, min(float(page_width), left))
    right = max(0.0, min(float(page_width), right))
    top = max(0.0, min(float(page_height), top))
    bottom = max(0.0, min(float(page_height), bottom))
    if right - left < min_size or bottom - top < min_size:
        return None
    return (left, top, right, bottom)


def union_bboxes(
    bboxes: list[tuple[float, float, float, float]] | tuple[tuple[float, float, float, float], ...]
) -> tuple[float, float, float, float] | None:
    if not bboxes:
        return None
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def pymupdf_words_to_evidence_words(words: list[tuple], *, source: str = "native_text") -> list[EvidenceWord]:
    """Convert PyMuPDF `page.get_text("words")` tuples to EvidenceWord."""
    out: list[EvidenceWord] = []
    for idx, word in enumerate(words):
        if len(word) < 5 or not str(word[4]).strip():
            continue
        out.append(
            EvidenceWord(
                id=f"w{idx}",
                text=str(word[4]),
                bbox=(float(word[0]), float(word[1]), float(word[2]), float(word[3])),
                source=source,
            )
        )
    return out


def _quote_tokens(text: str) -> list[str]:
    normalized = normalize_evidence_text(_HYPHENATED_BREAK_RE.sub("", text or ""))
    return _TOKEN_RE.findall(normalized)


def _word_tokens(words: list[EvidenceWord]) -> list[_Token]:
    tokens: list[_Token] = []
    prev_raw_hyphen = False
    for word in words:
        raw_norm = normalize_evidence_text(word.text)
        pieces = _TOKEN_RE.findall(raw_norm)
        if prev_raw_hyphen and pieces and tokens:
            prev = tokens[-1]
            if prev.text.isalpha() and pieces[0].isalpha():
                merged_bbox = union_bboxes([prev.bbox, word.bbox]) or prev.bbox
                tokens[-1] = _Token(
                    text=prev.text + pieces[0],
                    bbox=merged_bbox,
                    word_ids=tuple(dict.fromkeys((*prev.word_ids, word.id))),
                    original=f"{prev.original}{pieces[0]}",
                )
                pieces = pieces[1:]
        for piece in pieces:
            tokens.append(_Token(piece, word.bbox, (word.id,), word.text))
        prev_raw_hyphen = raw_norm.endswith("-")
    return tokens


def _page_only_ref(
    *,
    quote: str,
    page_number: int | None,
    source_id: str | None,
    source_file: str | None,
    extraction_method: str | None,
    provider: str | None,
    reason: str,
    score: float = 0.0,
) -> EvidenceRef | None:
    if page_number is None:
        return None
    return EvidenceRef(
        source_id=source_id,
        source_file=source_file,
        display_page=page_number,
        quote=quote[:500],
        match_method=EvidenceMatchMethod.page_only,
        match_score=score,
        provenance="quote_alignment",
        provider=provider,
        extraction_method=extraction_method,
        no_geometry_reason=reason,
    )


def resolve_quote_to_words(
    *,
    quote: str,
    page_number: int | None,
    words: list[EvidenceWord],
    source_id: str | None = None,
    source_file: str | None = None,
    extraction_method: str | None = None,
    provider: str | None = None,
    match_method: EvidenceMatchMethod = EvidenceMatchMethod.native_text,
    threshold: float = 0.98,
) -> EvidenceResolution:
    """Align a quote to the best contiguous/small-gap word window."""
    quote = quote or ""
    q_tokens = _quote_tokens(quote)
    if page_number is None or not quote.strip():
        return EvidenceResolution("no_match", 0.0, "", None, reason="missing quote or page")
    if not q_tokens:
        ref = _page_only_ref(
            quote=quote,
            page_number=page_number,
            source_id=source_id,
            source_file=source_file,
            extraction_method=extraction_method,
            provider=provider,
            reason="quote has no alignable tokens",
        )
        return EvidenceResolution("page_only", 0.0, "", None, reason="quote has no alignable tokens", evidence_ref=ref)
    hay_tokens = _word_tokens(words)
    if not hay_tokens:
        ref = _page_only_ref(
            quote=quote,
            page_number=page_number,
            source_id=source_id,
            source_file=source_file,
            extraction_method=extraction_method,
            provider=provider,
            reason="page has no word-level geometry",
        )
        return EvidenceResolution("page_only", 0.0, "", None, reason="page has no word-level geometry", evidence_ref=ref)

    wanted = " ".join(q_tokens)
    n = len(q_tokens)
    best: tuple[float, int, int] | None = None  # score, start, width
    min_width = max(1, n - 3)
    max_width = min(len(hay_tokens), n + 5)
    for width in range(min_width, max_width + 1):
        for start in range(0, len(hay_tokens) - width + 1):
            joined = " ".join(tok.text for tok in hay_tokens[start : start + width])
            score = fuzz.ratio(wanted, joined) / 100.0
            if best is None or score > best[0] or (
                score == best[0] and abs(width - n) < abs(best[2] - n)
            ):
                best = (score, start, width)
                if score >= 0.999 and width == n:
                    break
        if best is not None and best[0] >= 0.999 and best[2] == n:
            break

    if best is None:
        ref = _page_only_ref(
            quote=quote,
            page_number=page_number,
            source_id=source_id,
            source_file=source_file,
            extraction_method=extraction_method,
            provider=provider,
            reason="no token window match",
        )
        return EvidenceResolution("no_match", 0.0, "", None, reason="no token window match", evidence_ref=ref)

    score, start, width = best
    matched = hay_tokens[start : start + width]
    matched_text = " ".join(tok.original for tok in matched)
    if score < threshold:
        ref = _page_only_ref(
            quote=quote,
            page_number=page_number,
            source_id=source_id,
            source_file=source_file,
            extraction_method=extraction_method,
            provider=provider,
            reason="best token window below threshold",
            score=score,
        )
        return EvidenceResolution(
            "no_match", score, matched_text[:500], None,
            reason="best token window below threshold", evidence_ref=ref,
        )

    bbox = union_bboxes([tok.bbox for tok in matched])
    word_ids = list(dict.fromkeys(word_id for tok in matched for word_id in tok.word_ids))
    ref = EvidenceRef(
        source_id=source_id,
        source_file=source_file,
        display_page=page_number,
        quote=quote[:500],
        raw_text=matched_text[:500],
        bbox=bbox,
        match_method=match_method,
        match_score=score,
        word_ids=word_ids,
        provenance="quote_alignment",
        provider=provider,
        extraction_method=extraction_method,
    )
    return EvidenceResolution("resolved", score, matched_text[:500], bbox, word_ids, "token window match", ref)
