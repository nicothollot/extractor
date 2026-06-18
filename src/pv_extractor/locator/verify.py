"""Peek-verifier (D3) — fills the Phase-1 stub.

verify_candidate opens the candidate READ-ONLY (all reads go through the D1
readers, which only use io_guard.open_read) and inspects the first
config.peek_verify.pages pages/sections/slides/sheets:

  * doc class — CLIENT_VALUATION_DOC (memo/IC/portfolio-review vocabulary)
    vs HL_WORK_PRODUCT (Houlihan Lokey letterhead/disclaimer language,
    unless the document is merely ADDRESSED to HL) vs OTHER. Pure
    keyword/regex heuristics, all tunable in config.peek_verify.
  * the as-of date stated INSIDE the document ("as of March 31, 2026"...)
  * asset names (label lines like 'Portfolio Company: X', cleaned title
    lines, docx section headings)

verify_and_rerank cross-checks those findings against the locate query —
a wrong quarter inside the file or a foreign asset name REJECTS (demotes)
the candidate and the remaining ranking is re-resolved. Unreadable content
(scanned pages before OCR, corrupt files) yields UNVERIFIED, never a
rejection: filename evidence then stands on its own.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rapidfuzz import fuzz

from pv_extractor.config import Config
from pv_extractor.extract.patterns import parse_date_text, snippet
from pv_extractor.extract.readers import reader_for_extension
from pv_extractor.locator.aliases import expansions_for, load_aliases
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    DocClass,
    DocFlag,
    LocateQuery,
    LocateResult,
    ResolutionStatus,
    VerifyResult,
    VerifyStatus,
)
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)

_ASOF_MARKERS = re.compile(r"\b(?:valuation\s+(?:date|as\s+of)|as\s+of|as\s+at|dated)\b[:\s]*", re.IGNORECASE)
_DOC_TYPE_TITLE_NOISE = re.compile(
    r"\b(?:valuation|memorandum|memo|investment\s+committee|ic|portfolio|quarterly|review|"
    r"summary|write[\s-]?up|report|q[1-4]|fy)\b|\d",
    re.IGNORECASE,
)


def _count_hits(normalized_text: str, keywords: list[str]) -> tuple[int, list[str]]:
    padded = f" {normalized_text} "
    matched = [kw for kw in keywords if f" {normalize_text(kw)} " in padded]
    return len(matched), matched


def _extract_asof(lines: list[str]) -> tuple[object, str] | None:
    """First as-of/valuation-date statement -> (date, evidence line)."""
    for line in lines:
        m = _ASOF_MARKERS.search(line)
        if m is None:
            continue
        parsed = parse_date_text(line[m.end():]) or parse_date_text(line)
        if parsed is not None:
            return parsed[0], line.strip()
    return None


def _title_asset_candidate(lines: list[str]) -> str | None:
    """Strip doc-type words/dates from the first non-empty line; what is
    left ('Accell Valuation Memorandum ...' -> 'Accell') is an asset name."""
    for line in lines[:3]:
        if not line.strip():
            continue
        cleaned = _DOC_TYPE_TITLE_NOISE.sub(" ", line)
        cleaned = re.sub(r"[^\w&.\- ']+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—.")
        if 2 <= len(cleaned) <= 60:
            return cleaned
        return None
    return None


def _label_line_assets(lines: list[str], labels: list[str]) -> list[str]:
    out: list[str] = []
    wanted = {normalize_text(label) for label in labels}
    for line in lines:
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        if normalize_text(label) in wanted:
            value = value.strip()
            if 2 <= len(value) <= 80:
                out.append(value)
    return out


def verify_candidate(
    path: str | Path,
    config: Config,
    query: LocateQuery | None = None,
    expected_names: list[str] | None = None,
) -> VerifyResult:
    """Peek the first pages of `path` and classify/cross-check it (D3).

    With `query`/`expected_names` the stated as-of date and asset names are
    cross-checked; a clear mismatch returns REJECTED. Unreadable content
    returns UNVERIFIED (content evidence is simply unavailable)."""
    cfg = config.peek_verify
    extension = Path(path).suffix.lower()
    reader = reader_for_extension(extension, config.extraction.page_classification)
    if reader is None:
        return VerifyResult(
            status=VerifyStatus.UNVERIFIED,
            reason=f"no reader for extension {extension!r}; content not inspected",
        )

    content = reader.summarize(path, max_pages=cfg.pages)
    if content.flags:
        return VerifyResult(
            status=VerifyStatus.UNVERIFIED,
            reason=f"content unavailable ({', '.join(flag.value for flag in content.flags)}): "
            f"{content.error_detail or ''}".strip(),
        )

    text = "\n".join(page.text for page in content.pages)
    lines = [line for line in text.splitlines() if line.strip()]
    norm = normalize_text(text)
    if not norm:
        return VerifyResult(
            status=VerifyStatus.UNVERIFIED,
            reason="no extractable text in the peeked pages (scanned/empty); content not inspected",
        )

    evidence: list[str] = []
    hl_hits, hl_matched = _count_hits(norm, cfg.hl_work_product_markers)
    exception_hits, _ = _count_hits(norm, cfg.hl_addressee_exceptions)
    client_hits, client_matched = _count_hits(norm, cfg.client_doc_keywords)

    asof = _extract_asof(lines)
    asof_date = asof[0] if asof else None
    if asof:
        evidence.append(snippet(asof[1]))

    asset_names: list[str] = []
    for name in _label_line_assets(lines, cfg.asset_name_labels):
        if name not in asset_names:
            asset_names.append(name)
    for page in content.pages:  # docx headings / pptx titles
        if page.unit_name and page.unit_label in ("section", "slide") and page.unit_name not in asset_names:
            asset_names.append(page.unit_name)
    title = _title_asset_candidate(lines)
    if title and title not in asset_names:
        asset_names.append(title)

    restrict = query.restrict_to_client_sourced if query is not None else True
    if hl_hits and not exception_hits and restrict:
        evidence.extend(hl_matched[:3])
        return VerifyResult(
            status=VerifyStatus.REJECTED,
            reason=f"HL work-product language found: {', '.join(hl_matched[:3])}",
            doc_class=DocClass.HL_WORK_PRODUCT,
            asof_date=asof_date,
            asset_names=asset_names,
            confidence=min(1.0, 0.5 + 0.25 * hl_hits),
            evidence_snippets=evidence,
        )

    confidence = round(
        min(
            1.0,
            0.4 * min(client_hits, 4) / 4.0
            + (0.3 if asof_date else 0.0)
            + (0.3 if asset_names else 0.0),
        ),
        4,
    )
    if client_hits == 0 or confidence < cfg.min_confidence:
        return VerifyResult(
            status=VerifyStatus.UNVERIFIED,
            reason=(
                f"only {client_hits} valuation-vocabulary hits "
                f"(confidence {confidence:.2f} < {cfg.min_confidence}); not a recognizable client valuation doc"
            ),
            doc_class=DocClass.OTHER,
            asof_date=asof_date,
            asset_names=asset_names,
            confidence=confidence,
            evidence_snippets=evidence,
        )
    evidence.extend(client_matched[:4])

    # --- cross-checks against the locate query ---
    if query is not None and query.as_of_date is not None and asof_date is not None:
        if asof_date != query.as_of_date:
            return VerifyResult(
                status=VerifyStatus.REJECTED,
                reason=(
                    f"as-of date inside the file ({asof_date.isoformat()}) does not match the "
                    f"target period ({query.as_of_date.isoformat()})"
                ),
                doc_class=DocClass.CLIENT_VALUATION_DOC,
                asof_date=asof_date,
                asset_names=asset_names,
                confidence=confidence,
                evidence_snippets=evidence,
            )
    if expected_names and asset_names:
        threshold = config.locator.fuzzy_match_threshold
        best = max(
            fuzz.token_set_ratio(normalize_text(name), normalize_text(expected))
            for name in asset_names
            for expected in expected_names
        )
        if best < threshold:
            return VerifyResult(
                status=VerifyStatus.REJECTED,
                reason=(
                    f"asset names inside the file {asset_names!r} do not match the requested "
                    f"deal (best token_set_ratio {best:.0f} < {threshold})"
                ),
                doc_class=DocClass.CLIENT_VALUATION_DOC,
                asof_date=asof_date,
                asset_names=asset_names,
                confidence=confidence,
                evidence_snippets=evidence,
            )

    return VerifyResult(
        status=VerifyStatus.VERIFIED,
        reason=f"{client_hits} valuation-vocabulary hits"
        + (f"; as-of {asof_date.isoformat()} confirmed" if asof_date else "")
        + (f"; asset {asset_names[0]!r}" if asset_names else ""),
        doc_class=DocClass.CLIENT_VALUATION_DOC,
        asof_date=asof_date,
        asset_names=asset_names,
        confidence=confidence,
        evidence_snippets=evidence,
    )


def verify_and_rerank(
    result: LocateResult, config: Config
) -> tuple[LocateResult, dict[str, VerifyResult]]:
    """Content-verify the ranked candidates and re-resolve (D3).

    REJECTED candidates are demoted below everything else; the best surviving
    candidate wins. An AMBIGUOUS result whose survivors collapse to a single
    (or single VERIFIED) candidate upgrades to FOUND; a result whose
    candidates ALL reject becomes NOT_FOUND. UNVERIFIED never demotes."""
    if not result.candidates:
        return result, {}

    aliases = load_aliases(config.aliases_path_resolved())
    expected = expansions_for(result.query.deal, aliases.deals)
    verdicts: dict[str, VerifyResult] = {}
    for cand in result.candidates:
        verdict = verify_candidate(
            cand.record.file_path, config, query=result.query, expected_names=expected
        )
        verdicts[cand.record.file_path] = verdict
        log_event(
            logger, "peek verify", file_path=cand.record.file_path,
            status=verdict.status.value, doc_class=verdict.doc_class.value,
            confidence=verdict.confidence, reason=verdict.reason,
        )

    def status_of(cand) -> VerifyStatus:
        return verdicts[cand.record.file_path].status

    # An explicit analyst override (the "Use this one" pick in Confirm documents)
    # runs the chosen file even when content verification would reject it (e.g. HL
    # work product). The pick is STILL verified — the verdict rides along so the
    # run can flag it — but the human's choice wins instead of being dropped.
    if result.from_override and result.winner is not None:
        wv = verdicts.get(result.winner.record.file_path)
        evidence = result.evidence
        if wv is not None and wv.status is VerifyStatus.REJECTED:
            evidence = (
                f"manual override: {result.winner.record.file_name!r} runs despite content "
                f"verification ({wv.reason}) — explicit analyst pick"
            )
        return (
            LocateResult(
                status=ResolutionStatus.FOUND, query=result.query,
                candidates=result.candidates, winner=result.winner,
                evidence=evidence, from_override=True,
            ),
            verdicts,
        )

    survivors = [c for c in result.candidates if status_of(c) is not VerifyStatus.REJECTED]
    rejected = [c for c in result.candidates if status_of(c) is VerifyStatus.REJECTED]
    reranked = survivors + rejected

    if not survivors:
        reasons = "; ".join(
            f"{c.record.file_name!r}: {verdicts[c.record.file_path].reason}" for c in rejected
        )
        return (
            LocateResult(
                status=ResolutionStatus.NOT_FOUND,
                query=result.query,
                candidates=reranked,
                evidence=f"all {len(rejected)} candidate(s) rejected by content verification — {reasons}",
            ),
            verdicts,
        )

    verified = [c for c in survivors if status_of(c) is VerifyStatus.VERIFIED]
    winner = None
    evidence = result.evidence
    status = result.status

    if result.status is ResolutionStatus.FOUND:
        if result.winner is not None and status_of(result.winner) is VerifyStatus.REJECTED:
            winner = verified[0] if verified else survivors[0]
            evidence = (
                f"filename winner {result.winner.record.file_name!r} rejected by content "
                f"verification ({verdicts[result.winner.record.file_path].reason}); promoted "
                f"{winner.record.file_name!r}"
            )
        else:
            winner = result.winner
    elif result.status is ResolutionStatus.AMBIGUOUS:
        if len(survivors) == 1:
            winner, status = survivors[0], ResolutionStatus.FOUND
            evidence = (
                f"content verification disambiguated: only {winner.record.file_name!r} survived "
                f"({len(rejected)} candidate(s) rejected)"
            )
        elif len(verified) == 1:
            winner, status = verified[0], ResolutionStatus.FOUND
            evidence = (
                f"content verification disambiguated: {winner.record.file_name!r} is the only "
                f"VERIFIED candidate among {len(survivors)} survivors"
            )

    return (
        LocateResult(status=status, query=result.query, candidates=reranked, winner=winner, evidence=evidence),
        verdicts,
    )
