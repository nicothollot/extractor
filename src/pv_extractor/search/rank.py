"""Rank indexed files against a DocTypeSpec (Phase B).

Transparent, additive scoring that mirrors the locator's style: every file
gets a `components` dict so a ranking decision is inspectable. The lexical core
is a small BM25 over the spec's filename_include terms (no new dependency —
term frequencies come from the export-normalized file name; document length and
the corpus average drive the standard BM25 saturation/length-normalization),
blended with a rapidfuzz phrase-similarity signal so multi-word anchors ("cap
table") still score on near-spellings. Folder context, extension prior and
period evidence layer on additively; negative terms and a missing-but-required
period subtract. Learned doc_search_feedback nudges per-spec weight_overrides
as a transparent linear bump/penalty.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone

from rapidfuzz import fuzz

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import filename_contains_period, parse_date_folder
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DocTypeSpec, FileRecord
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)


def _safe_regex_search(pattern: str, text: str) -> bool:
    """re.search that never raises: an invalid pattern (e.g. one suggested by
    the optional CLI augmentation) is treated as a non-match, not a crash."""
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False


# weight_overrides keys produced by learning are namespaced so they never
# collide with the config rank_weights component keys.
_LEARN_PREFIX = "learn:"
_STOP_TOKENS = frozenset(
    {"the", "of", "and", "for", "report", "memo", "final", "draft", "copy", "v1", "v2", "vf"}
)


# ---------------------------------------------------------------------------
# lexical core (BM25 over filename_include terms + fuzzy phrase blend)
# ---------------------------------------------------------------------------


def _spec_terms(spec: DocTypeSpec) -> list[str]:
    """Normalized single tokens drawn from the spec's filename anchors — the
    BM25 query terms. Multi-word phrases contribute each of their tokens."""
    terms: list[str] = []
    for phrase in spec.filename_include:
        for tok in normalize_text(phrase).split():
            if tok and tok not in terms:
                terms.append(tok)
    return terms


def _bm25_idf(term: str, df: int, n_docs: int) -> float:
    """Standard BM25 IDF with the +0.5 smoothing, floored at 0 so a term in
    every document never goes negative."""
    return max(0.0, math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0))


def _lexical_relevance(
    rec: FileRecord,
    spec: DocTypeSpec,
    terms: list[str],
    *,
    df: dict[str, int],
    n_docs: int,
    avg_len: float,
    k1: float,
    b: float,
    max_bm25: float,
) -> float:
    """0..1 lexical relevance: BM25 over the filename terms (normalized by the
    corpus' best achievable score) blended with a fuzzy phrase-match signal."""
    name = rec.normalized_file_name
    name_tokens = name.split()
    tf = Counter(name_tokens)
    doc_len = max(len(name_tokens), 1)
    score = 0.0
    for term in terms:
        f = tf.get(term, 0)
        if f == 0:
            continue
        idf = _bm25_idf(term, df.get(term, 0), n_docs)
        denom = f + k1 * (1 - b + b * doc_len / max(avg_len, 1.0))
        score += idf * (f * (k1 + 1)) / denom
    bm25_norm = min(1.0, score / max_bm25) if max_bm25 > 0 else 0.0

    # Fuzzy phrase blend: best token_set_ratio of any anchor phrase vs the name
    # so a near-miss spelling or word order still registers.
    fuzzy = 0.0
    for phrase in spec.filename_include:
        ratio = fuzz.token_set_ratio(normalize_text(phrase), name)
        if ratio > fuzzy:
            fuzzy = ratio
    fuzzy_norm = fuzzy / 100.0

    # Regex anchors are a hard filename signal (full credit when they hit).
    # A pattern that fails to compile is skipped, never raised — the CLI
    # augmentation can suggest an invalid pattern, and that must not crash a
    # rank/preview (mirrors locator.scoring._regex_hits).
    regex_hit = any(_safe_regex_search(p, name) for p in spec.filename_regex if p)
    regex_norm = 1.0 if regex_hit else 0.0

    return max(bm25_norm, fuzzy_norm, regex_norm)


def _corpus_stats(conn: sqlite3.Connection, terms: list[str]) -> tuple[dict[str, int], int, float]:
    """Document frequencies (per term, over file NAMES), total doc count and
    average name length — the BM25 corpus statistics, computed once per rank."""
    n_docs = db.count_files(conn)
    if n_docs == 0:
        return {term: 0 for term in terms}, 0, 1.0
    df: dict[str, int] = {term: 0 for term in terms}
    total_len = 0
    for row in conn.execute("SELECT normalized_file_name FROM files"):
        name_tokens = (row[0] or "").split()
        total_len += len(name_tokens)
        present = set(name_tokens)
        for term in terms:
            if term in present:
                df[term] += 1
    avg_len = total_len / n_docs if n_docs else 1.0
    return df, n_docs, avg_len


# ---------------------------------------------------------------------------
# learning: doc_search_feedback -> per-spec weight_overrides
# ---------------------------------------------------------------------------


def effective_weight_overrides(
    conn: sqlite3.Connection, spec: DocTypeSpec, config: Config
) -> dict[str, float]:
    """Fold recorded accept/reject feedback into a transparent token-level
    nudge map (key = ``learn:<token>`` -> signed weight). Accepted files'
    distinctive tokens earn a positive bump, rejected patterns a penalty, each
    scaled by config.smart_search.learning_weight. Returns the spec's own
    weight_overrides MERGED with the freshly computed learn:* nudges."""
    learning_weight = config.smart_search.learning_weight
    token_signal: Counter = Counter()
    for fb in db.list_doc_search_feedback(conn, spec.slug):
        name_row = conn.execute(
            "SELECT normalized_file_name FROM files WHERE file_path = ?",
            (fb["file_path"],),
        ).fetchone()
        name = (name_row[0] if name_row else "") or ""
        for tok in set(name.split()):
            if len(tok) > 1 and tok not in _STOP_TOKENS:
                token_signal[tok] += fb["label"]
    overrides: dict[str, float] = dict(spec.weight_overrides)
    for tok, signal in token_signal.items():
        # A small bounded linear nudge: sign of the accumulated feedback,
        # magnitude saturating so a single token can't dominate the score.
        nudge = learning_weight * max(-3.0, min(3.0, float(signal)))
        if nudge:
            overrides[f"{_LEARN_PREFIX}{tok}"] = round(nudge, 4)
    return overrides


def _learning_bump(rec: FileRecord, overrides: dict[str, float]) -> float:
    """Sum the learn:* nudges whose token appears in this file's name."""
    if not overrides:
        return 0.0
    name_tokens = set(rec.normalized_file_name.split())
    total = 0.0
    for key, weight in overrides.items():
        if key.startswith(_LEARN_PREFIX) and key[len(_LEARN_PREFIX):] in name_tokens:
            total += weight
    return total


# ---------------------------------------------------------------------------
# component scoring
# ---------------------------------------------------------------------------


def _folder_context(rec: FileRecord, spec: DocTypeSpec) -> bool:
    folder = f" {rec.normalized_folder_path} "
    return any(f" {normalize_text(a)} " in folder for a in spec.folder_include)


def _negative_hit(rec: FileRecord, spec: DocTypeSpec) -> bool:
    name = f" {rec.normalized_file_name} "
    folder = f" {rec.normalized_folder_path} "
    if any(f" {normalize_text(t)} " in name for t in spec.filename_exclude):
        return True
    return any(f" {normalize_text(t)} " in folder for t in spec.folder_exclude)


def _period_evidence(rec: FileRecord, target_as_of: date | None) -> bool:
    if target_as_of is None:
        return False
    if rec.as_of_date is not None and rec.as_of_date == target_as_of:
        return True
    if rec.date_folder and parse_date_folder(rec.date_folder) == target_as_of:
        return True
    return filename_contains_period(rec.normalized_file_name, target_as_of)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def rank_files(
    conn: sqlite3.Connection,
    config: Config,
    spec: DocTypeSpec,
    *,
    client: str | None = None,
    deal: str | None = None,
    target_as_of: date | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Score indexed files against `spec`; return the best matches, each a dict
    {file_path, file_name, score, components, client, deal, as_of_date}. Drops
    results below config.smart_search.min_score and caps at top_n (or `limit`)."""
    cfg = config.smart_search
    weights = cfg.rank_weights
    terms = _spec_terms(spec)
    overrides = effective_weight_overrides(conn, spec, config)

    # Candidate pool: scope by client/deal when given (exact column match),
    # else the whole index. Files_fts could prefilter, but the spec anchors are
    # synonyms — scoring every scoped row keeps recall high and stays fast at
    # index scale; the BM25 corpus stats are computed over the full corpus.
    sql = "SELECT * FROM files WHERE 1=1"
    params: list = []
    if client is not None:
        sql += " AND client = ?"
        params.append(client)
    if deal is not None:
        sql += " AND deal = ?"
        params.append(deal)
    rows = conn.execute(sql, params).fetchall()
    candidates = [db.record_from_row(row) for row in rows]

    df, n_docs, avg_len = _corpus_stats(conn, terms)
    # Best achievable BM25 over these terms (each present once in a short name)
    # — the normalizer so lexical relevance lands in 0..1.
    max_bm25 = sum(
        _bm25_idf(t, df.get(t, 0), n_docs) * (cfg.bm25_k1 + 1) / (1 + cfg.bm25_k1 * (1 - cfg.bm25_b))
        for t in terms
    ) or 1.0

    results: list[dict] = []
    for rec in candidates:
        if spec.extensions and rec.extension not in spec.extensions:
            ext_score = 0.0
            ext_match = False
        else:
            ext_match = bool(spec.extensions)
            ext_score = weights.get("extension_prior", 0.0) if ext_match else 0.0

        lexical = _lexical_relevance(
            rec, spec, terms, df=df, n_docs=n_docs, avg_len=avg_len,
            k1=cfg.bm25_k1, b=min(1.0, max(0.0, cfg.bm25_b)), max_bm25=max_bm25,
        )
        filename_score = weights.get("filename_match", 0.0) * lexical
        folder_match = _folder_context(rec, spec)
        folder_score = weights.get("folder_context", 0.0) if folder_match else 0.0

        period_match = _period_evidence(rec, target_as_of)
        if period_match:
            period_score = weights.get("period_evidence", 0.0)
        elif spec.period_required and target_as_of is not None:
            period_score = weights.get("period_missing_penalty", 0.0)
        else:
            period_score = 0.0

        negative = _negative_hit(rec, spec)
        negative_score = weights.get("negative_penalty", 0.0) if negative else 0.0
        learning_score = _learning_bump(rec, overrides)

        total = (
            filename_score
            + folder_score
            + ext_score
            + period_score
            + negative_score
            + learning_score
        )
        if total < cfg.min_score:
            continue
        results.append(
            {
                "file_path": rec.file_path,
                "file_name": rec.file_name,
                "client": rec.client,
                "deal": rec.deal,
                "as_of_date": rec.as_of_date.isoformat() if rec.as_of_date else None,
                "score": round(total, 4),
                "components": {
                    "filename_match": round(filename_score, 4),
                    "lexical_relevance": round(lexical, 4),
                    "folder_context": round(folder_score, 4),
                    "extension_prior": round(ext_score, 4),
                    "period_evidence": round(period_score, 4),
                    "negative_penalty": round(negative_score, 4),
                    "learning": round(learning_score, 4),
                },
            }
        )

    # Deterministic order: score desc, then file_path asc as a stable tiebreak
    # so equal-scoring files never reorder run-to-run.
    results.sort(key=lambda r: (-r["score"], r["file_path"]))
    cap = limit if limit is not None else cfg.top_n
    log_event(
        logger, "smart search ranked",
        slug=spec.slug, candidates=len(candidates), kept=len(results), cap=cap,
    )
    return results[:cap]


def record_search_feedback(
    conn: sqlite3.Connection,
    *,
    profile_slug: str,
    file_path: str,
    label: int,
    context: dict | None,
) -> None:
    """Record one accept (+1) / reject (-1) signal for a profile's ranking.
    The signal is folded into per-spec token nudges by
    effective_weight_overrides on the next rank."""
    if label not in (1, -1):
        raise ValueError(f"label must be +1 or -1, got {label!r}")
    db.record_doc_search_feedback(
        conn,
        profile_slug=profile_slug,
        file_path=file_path,
        label=label,
        context=json.dumps(context) if context is not None else None,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    log_event(
        logger, "smart search feedback recorded",
        profile_slug=profile_slug, label=label,
    )
