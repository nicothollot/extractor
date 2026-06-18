"""Document locator: the D5 resolution cascade.

Pipeline: resolve client/deal names against the index (+ aliases.yaml),
resolve the requested period to an as-of date using the client's period
style, FTS5-prefilter candidates, score them deterministically, group into
version families, and decide FOUND / AMBIGUOUS / NOT_FOUND /
NOT_YET_UPLOADED / ACCESS_ERROR comparing family heads only. Every
candidate's full score breakdown is logged via log_event.
"""

from __future__ import annotations

import logging
import sqlite3

from pv_extractor.config import Config, LocatorConfig
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import resolve_target_period
from pv_extractor.locator.aliases import expansions_for, load_aliases, resolve_name
from pv_extractor.locator.families import group_into_families
from pv_extractor.locator.overrides import indexed_record_for_path, lookup_override
from pv_extractor.locator.scoring import ScoreContext, score_candidate
from pv_extractor.logging_setup import log_event
from pv_extractor.models import (
    CandidateFile,
    DocTypeSpec,
    LocateQuery,
    LocateResult,
    ResolutionStatus,
    ScoreBreakdown,
)
from pv_extractor.normalize import normalize_text, split_path_segments, strip_extended_prefix

logger = logging.getLogger(__name__)


def _fts_phrases(names: list[str]) -> list[str]:
    """Double-quoted FTS5 phrases: normalize_text strips quotes and every
    other non-alphanumeric character, so the output is always a safe
    space-separated token phrase."""
    phrases: list[str] = []
    for name in names:
        phrase = normalize_text(name)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return [f'"{phrase}"' for phrase in phrases]


def _fts_match_expr(client_expansions: list[str], deal_expansions: list[str]) -> str:
    client_part = " OR ".join(_fts_phrases(client_expansions))
    deal_part = " OR ".join(_fts_phrases(deal_expansions))
    return f"({client_part}) AND ({deal_part})"


def _deal_prefix(
    conn: sqlite3.Connection, config: Config, cands: list[CandidateFile], client: str, deal: str
) -> str:
    """Share path of the deal folder, for scan_errors containment checks.
    Discovered deal folders (which may sit several levels below the client)
    win; otherwise fall back to a candidate row's first-two-segments guess,
    then to the legacy string-join."""
    for folder in db.deal_folders_for_client(conn, client):
        if folder.name == deal and folder.folder_paths:
            return config.pv_root + "\\" + folder.folder_paths[0]
    root_depth = len(split_path_segments(config.pv_root))
    for cand in cands:
        path = strip_extended_prefix(cand.record.file_path)
        segments = split_path_segments(path)
        if len(segments) >= root_depth + 2:
            joined = "\\".join(segments[: root_depth + 2])
            return ("\\\\" + joined) if path.replace("/", "\\").startswith("\\\\") else joined
    return config.pv_root + "\\" + client + "\\" + deal


def _is_eligible(head: CandidateFile, cfg: LocatorConfig) -> bool:
    """Score floor plus two hard gates. A candidate must carry positive
    period evidence (date-folder match, period in the filename, or the
    mtime window) — a mismatched date folder or no period signal at all can
    never satisfy a request for a specific period, which is what lets
    NOT_YET_UPLOADED / NOT_FOUND / ACCESS_ERROR fire for deals full of
    other-period documents. It must also hit at least one doc-type keyword:
    an NDA, an invoice or a bare placeholder is never a requested document,
    whatever its other components add up to (this subsumes the
    negative-keyword-without-doc-type exclusion)."""
    breakdown = head.breakdown
    if breakdown.final_score < cfg.floor_score:
        return False
    if breakdown.period_method not in ("folder", "filename", "mtime"):
        return False
    return bool(breakdown.matched_keywords)


def _has_period_evidence(head: CandidateFile, cfg: LocatorConfig) -> bool:
    """Relaxed eligibility for the period-fallback tier: a real document FOR THE
    TARGET PERIOD that simply does not carry a doc-type keyword (a deal full of
    SPAs / credit agreements rather than an obvious 'valuation memo'). Same
    floor + positive period gate as `_is_eligible`, minus the keyword
    requirement, but a pure-negative file (NDA, invoice, wire) is still
    excluded — it is never a document the analyst wants."""
    breakdown = head.breakdown
    if breakdown.final_score < cfg.floor_score:
        return False
    if breakdown.period_method not in ("folder", "filename", "mtime"):
        return False
    return not breakdown.matched_negative_keywords


def locate(
    conn: sqlite3.Connection,
    config: Config,
    query: LocateQuery,
    *,
    doc_type_spec: DocTypeSpec | None = None,
) -> LocateResult:
    """Run the full locator cascade for one query. Raises ValueError when
    the period string cannot be resolved to an as-of date.

    `doc_type_spec`: when supplied, its filename/folder include/exclude lists
    and weight_overrides REPLACE the static config.locator.doc_type_keywords
    lookup for the doc-type + negative scoring components (see
    scoring._doctype_spec_component). None => identical to the legacy
    builtin-enum path keyed off query.doc_type. Resolving query.doc_type_profile
    (a slug) into a DocTypeSpec is the CALLER's job — locate only CONSUMES a
    spec if one is passed (avoids a dependency on the search/ package)."""
    cfg = config.locator
    aliases = load_aliases(config.aliases_path_resolved())

    known_clients = db.distinct_clients(conn)
    client, client_method, client_ratio = resolve_name(
        query.client, known_clients, aliases.clients, cfg.fuzzy_match_threshold
    )
    log_event(
        logger, "locator client resolution", query_client=query.client,
        resolved=client, method=client_method, ratio=client_ratio,
    )
    if client is None:
        return LocateResult(
            status=ResolutionStatus.NOT_FOUND,
            query=query,
            evidence=(
                f"client {query.client!r} did not resolve against {len(known_clients)} indexed "
                f"client folders: best token_set_ratio {client_ratio:.1f} is below "
                f"fuzzy_match_threshold {cfg.fuzzy_match_threshold}"
            ),
        )

    known_deals = db.deals_for_client(conn, client)
    deal, deal_method, deal_ratio = resolve_name(
        query.deal, known_deals, aliases.deals, cfg.fuzzy_match_threshold
    )
    log_event(
        logger, "locator deal resolution", query_deal=query.deal,
        client=client, resolved=deal, method=deal_method, ratio=deal_ratio,
    )
    if deal is None:
        return LocateResult(
            status=ResolutionStatus.NOT_FOUND,
            query=query,
            evidence=(
                f"deal {query.deal!r} did not resolve against {len(known_deals)} indexed deal "
                f"folders under client {client!r}: best token_set_ratio {deal_ratio:.1f} is below "
                f"fuzzy_match_threshold {cfg.fuzzy_match_threshold}"
            ),
        )

    target = resolve_target_period(query.period, config.client_period_style(client))
    if target is None:
        raise ValueError(
            f"could not resolve period {query.period!r} to an as-of date; expected an ISO date "
            f"(2025-01-31), a date-folder form (1.31.25), a quarter (Q1 2026, 4Q25) or a month "
            f"(January 2025)"
        )
    query.as_of_date = target

    # Phase 4: an analyst pick recorded in the GUI locator review wins the
    # cascade outright — but only while the file is still in the index, and
    # the winner still faces the Phase-2 peek-verifier like any other.
    override_path = lookup_override(
        conn, client=client, deal=deal, as_of_date=target, doc_type=query.doc_type.value
    )
    if override_path is not None:
        record = indexed_record_for_path(conn, override_path)
        if record is not None:
            winner = CandidateFile(record=record, breakdown=ScoreBreakdown())
            log_event(
                logger, "locator override applied", client=client, deal=deal,
                as_of_date=target.isoformat(), file_path=override_path,
            )
            return LocateResult(
                status=ResolutionStatus.FOUND,
                query=query,
                candidates=[winner],
                winner=winner,
                evidence=(
                    f"manual override recorded in the locator review for "
                    f"{client}/{deal} as of {target.isoformat()} — scoring cascade skipped"
                ),
                from_override=True,
            )
        log_event(
            logger, "locator override ignored (file no longer indexed)",
            client=client, deal=deal, file_path=override_path,
        )

    client_expansions = expansions_for(client, aliases.clients)
    deal_expansions = expansions_for(deal, aliases.deals)
    match_expr = _fts_match_expr(client_expansions, deal_expansions)
    records = db.fts_candidates(conn, match_expr, cfg.fts_candidate_limit)
    log_event(
        logger, "locator fts prefilter", match_expr=match_expr,
        candidate_count=len(records), limit=cfg.fts_candidate_limit,
    )

    ctx = ScoreContext(
        resolved_client=client,
        resolved_deal=deal,
        deal_expansions=deal_expansions,
        client_method=client_method,
        client_ratio=client_ratio,
        deal_method=deal_method,
        deal_ratio=deal_ratio,
        target_as_of=target,
        doc_type=query.doc_type,
        cfg=cfg,
        doc_type_spec=doc_type_spec,
        restrict_to_client_sourced=query.restrict_to_client_sourced,
    )
    cands = [CandidateFile(record=rec, breakdown=score_candidate(rec, ctx)) for rec in records]
    families = group_into_families(cands, cfg.family_ratio_threshold)
    for cand in cands:
        log_event(
            logger, "locator candidate scored", file_path=cand.record.file_path,
            family_key=cand.family_key, family_rank=cand.family_rank,
            **cand.breakdown.model_dump(),
        )

    heads = [family[0] for family in families]
    eligible = sorted(
        (head for head in heads if _is_eligible(head, cfg)),
        key=lambda cand: -cand.breakdown.final_score,
    )

    if eligible:
        top1 = eligible[0]
        top2 = eligible[1] if len(eligible) > 1 else None
        gap = top1.breakdown.final_score - top2.breakdown.final_score if top2 else None
        if top1.breakdown.final_score >= cfg.min_accept_score and (gap is None or gap >= cfg.min_gap):
            evidence = (
                f"{top1.record.file_name!r} scored {top1.breakdown.final_score:.1f} >= "
                f"min_accept_score {cfg.min_accept_score:.1f}"
            )
            evidence += (
                f" and leads the #2 eligible family head ({top2.breakdown.final_score:.1f}) "
                f"by {gap:.1f} >= min_gap {cfg.min_gap:.1f}"
                if top2 is not None
                else "; no other eligible family head"
            )
            return LocateResult(
                status=ResolutionStatus.FOUND,
                query=query,
                candidates=eligible[: cfg.ambiguous_top_n],
                winner=top1,
                evidence=evidence,
            )
        if top1.breakdown.final_score < cfg.min_accept_score:
            evidence = (
                f"best family head {top1.record.file_name!r} scored "
                f"{top1.breakdown.final_score:.1f} < min_accept_score {cfg.min_accept_score:.1f}"
            )
        else:
            evidence = (
                f"top family head {top1.record.file_name!r} ({top1.breakdown.final_score:.1f}) "
                f"leads #2 ({top2.breakdown.final_score:.1f}) by only {gap:.1f} < "  # type: ignore[union-attr]
                f"min_gap {cfg.min_gap:.1f}"
            )
        return LocateResult(
            status=ResolutionStatus.AMBIGUOUS,
            query=query,
            candidates=eligible[: cfg.ambiguous_top_n],
            evidence=evidence + f"; returning top {min(len(eligible), cfg.ambiguous_top_n)} for human pick",
        )

    # No eligible family heads: explain WHY nothing qualified. An unreadable
    # path under the deal is the most important signal, so it is checked first.
    prefix = _deal_prefix(conn, config, cands, client, deal)
    errors = db.scan_errors_under(conn, prefix)
    if errors:
        error_paths = ", ".join(err.path for err in errors)
        return LocateResult(
            status=ResolutionStatus.ACCESS_ERROR,
            query=query,
            evidence=(
                f"no eligible candidate scored >= floor_score {cfg.floor_score:.1f}, and "
                f"{len(errors)} unreadable path(s) were recorded under {prefix}: {error_paths}"
            ),
        )

    # No doc-type match, but real documents may still exist for the target
    # period (a deal full of legal/diligence files, not an obvious memo). Rather
    # than a bare NOT_YET_UPLOADED with nothing to act on, surface those
    # period-matching candidates as AMBIGUOUS so the analyst can pick/Replace.
    if cfg.surface_period_matches_without_doctype:
        period_matches = sorted(
            (head for head in heads if _has_period_evidence(head, cfg)),
            key=lambda cand: -cand.breakdown.final_score,
        )
        if period_matches:
            shown = min(len(period_matches), cfg.ambiguous_top_n)
            return LocateResult(
                status=ResolutionStatus.AMBIGUOUS,
                query=query,
                candidates=period_matches[:shown],
                evidence=(
                    f"{len(period_matches)} document(s) match the target period "
                    f"{target.isoformat()} for {client}\\{deal} but none carry a "
                    f"{query.doc_type.value!r} keyword; returning top {shown} for human pick"
                ),
            )
    existing_periods = db.as_of_dates_for_deal(conn, client, deal)
    if existing_periods:
        period_list = ", ".join(d.isoformat() for d in existing_periods)
        return LocateResult(
            status=ResolutionStatus.NOT_YET_UPLOADED,
            query=query,
            evidence=(
                f"deal folder {client}\\{deal} exists with other-period date folders "
                f"({period_list}); nothing eligible for the target period {target.isoformat()} — "
                f"client has not delivered yet"
            ),
        )
    return LocateResult(
        status=ResolutionStatus.NOT_FOUND,
        query=query,
        evidence=(
            f"deal folder {client}\\{deal} has no date folders and no candidate scored >= "
            f"floor_score {cfg.floor_score:.1f} for period {target.isoformat()}"
        ),
    )
