"""Per-client learning for deal discovery (Search & Selection Revamp, Phase A3).

When an analyst corrects Deal Finder's output in the GUI — pinning a folder to
a deal, removing a wrong folder, renaming, merging or splitting — the correction
is recorded in the local index DB (``deal_finder_feedback``). On the next
``refresh_deals`` for that client this module replays those corrections in two
layers:

  * a HARD layer (deterministic, always wins): exact edits to the discovered
    ``DealFolder`` list — force-add a pinned folder, drop an excluded folder,
    rename, union (merge) or best-effort split.
  * a PRIOR layer: REUSABLE per-client signal nudges derived from the *shape* of
    the corrections (e.g. repeated add_folder under admin nodes raises this
    client's ``admin_container`` signal; shared-bucket corrections raise
    ``shared_bucket``). The total prior contribution per deal is capped at
    ``config.deal_discovery.learning.prior_bump``.

The headline property of A3 is GENERALIZATION: a correction recorded for deal
*Foo* improves discovery of a DIFFERENT new deal *Bar* under the SAME client on
the next refresh, because ``derive_layout_priors`` produces client-scoped
signals, not deal-scoped pins.

All writes go to the local SQLite index DB only (rule 1: read-only on the
share). The live per-client priors are cached in ``index_meta`` under
``layout_priors:<client>`` so the ``/learned`` endpoint and CLI ``--show-learned``
can read them without recomputation; they are recomputed on every refresh.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DealEvidence, DealFolder
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)

# Correction action vocabulary (matches deal_finder_feedback.action comment).
_ACTIONS = {"add_folder", "remove_folder", "merge", "split", "rename"}

# Signal names the PRIOR layer can nudge. These map onto DealEvidence /
# DealDiscoveryWeights component keys so a learned bump is recorded transparently
# in the deal's evidence.components alongside the heuristic components.
_PRIOR_ADMIN = "admin_container"
_PRIOR_SHARED_BUCKET = "shared_bucket"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_path(path: str) -> str:
    """Canonical comparison form for a folder path: backslash separators,
    lowercased, no trailing separators."""
    return path.replace("/", "\\").strip("\\").lower()


# ---------------------------------------------------------------------------
# (a)/(b) recording + listing corrections
# ---------------------------------------------------------------------------


def record_correction(
    conn: sqlite3.Connection,
    *,
    client: str,
    deal: str,
    action: str,
    folder_path: str | None = None,
    payload: dict | None = None,
) -> None:
    """Insert one analyst correction into ``deal_finder_feedback``.

    ``action`` must be one of {'add_folder','remove_folder','merge','split',
    'rename'}; ``payload`` (new name, merge target, ...) is serialized to JSON.
    """
    if action not in _ACTIONS:
        raise ValueError(f"unknown deal-finder correction action: {action!r}")
    payload_json = json.dumps(payload) if payload is not None else None
    db.record_deal_feedback(
        conn,
        client=client,
        deal=deal,
        action=action,
        folder_path=folder_path,
        payload=payload_json,
        created_at=_now(),
    )
    log_event(
        logger,
        "deal finder correction recorded",
        client=client,
        action=action,
    )


def list_corrections(conn: sqlite3.Connection, client: str) -> list[dict]:
    """All corrections for one client (oldest first), with ``payload`` parsed
    back into a dict (None when absent)."""
    rows = db.list_deal_feedback(conn, client)
    for row in rows:
        raw = row.get("payload")
        row["payload"] = json.loads(raw) if raw else None
    return rows


def delete_correction(conn: sqlite3.Connection, feedback_id: int) -> bool:
    """Remove one correction by id; True when a row was deleted."""
    return db.delete_deal_feedback(conn, feedback_id)


# ---------------------------------------------------------------------------
# (c) reusable per-client layout priors
# ---------------------------------------------------------------------------


def derive_layout_priors(conn: sqlite3.Connection, config: Config, client: str) -> dict[str, float]:
    """Derive REUSABLE per-client signal nudges from this client's correction
    history and cache them in ``index_meta`` under ``layout_priors:<client>``.

    The returned dict is ``{signal_name: nudge}`` (always positive nudges here).
    Each individual nudge and the total are capped at
    ``config.deal_discovery.learning.prior_bump`` so the PRIOR layer can never
    contribute more than the configured cap. Priors are client-scoped (not tied
    to a particular deal), which is what makes a correction on deal *Foo*
    generalize to a new deal *Bar* under the same client.

    A documented manual override may live in
    ``config.deal_discovery.layout_priors[client]``; it is unioned in (and
    likewise capped) so an operator can seed priors without correction history.
    """
    cap = max(0.0, config.deal_discovery.learning.prior_bump)
    corrections = db.list_deal_feedback(conn, client)

    # Count the SHAPES of corrections that imply a reusable client-wide signal.
    admin_signal = 0
    bucket_signal = 0
    for row in corrections:
        action = row["action"]
        payload = json.loads(row["payload"]) if row.get("payload") else {}
        fp = (row.get("folder_path") or "")
        norm_segs = normalize_text(fp.replace("\\", " ").replace("/", " ")).split()
        admin_tokens = set(config.deal_discovery.admin_tokens)
        looks_admin = bool(set(norm_segs) & admin_tokens) or "_" in fp
        if action == "add_folder":
            # Pinning a folder the heuristics missed — under an admin-ish path it
            # is evidence this client buries deals in admin containers.
            if looks_admin or payload.get("admin_container"):
                admin_signal += 1
            if payload.get("shared_bucket"):
                bucket_signal += 1
        elif action == "split":
            # Splitting one discovered folder into several deals is the
            # signature of a shared mixed-investment bucket.
            bucket_signal += 1
            if payload.get("shared_bucket"):
                bucket_signal += 1

    priors: dict[str, float] = {}
    # Each repeated correction adds a fraction of the cap, saturating AT the cap.
    # A single correction already nudges (one analyst signal is enough); more
    # corrections saturate faster but never exceed the cap.
    if admin_signal:
        priors[_PRIOR_ADMIN] = round(min(cap, cap * admin_signal / 2.0), 4)
    if bucket_signal:
        priors[_PRIOR_SHARED_BUCKET] = round(min(cap, cap * bucket_signal / 2.0), 4)

    # Union in any documented manual override (capped identically).
    manual = config.deal_discovery.layout_priors.get(client, {})
    for key, value in manual.items():
        nudge = round(min(cap, float(value)), 4)
        if nudge > 0:
            priors[key] = max(priors.get(key, 0.0), nudge)

    # Cache for the read-only endpoints / CLI (recomputed every refresh).
    db.set_meta(conn, f"layout_priors:{client}", json.dumps(priors))
    return priors


def cached_layout_priors(conn: sqlite3.Connection, client: str) -> dict[str, float]:
    """The last cached priors for the client (read-only; for /learned + CLI).
    Empty dict when none have been computed yet."""
    raw = db.get_meta(conn, f"layout_priors:{client}")
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# (d) apply feedback to a freshly discovered deal list
# ---------------------------------------------------------------------------


def _find_deal(deals: list[DealFolder], name: str) -> DealFolder | None:
    target = normalize_text(name)
    for deal in deals:
        if normalize_text(deal.name) == target:
            return deal
    return None


def _apply_hard_corrections(
    deals: list[DealFolder], conn: sqlite3.Connection, client: str
) -> tuple[list[DealFolder], list[str]]:
    """Deterministic exact edits replayed from the correction history, in order.
    Always wins over heuristics. Returns the edited list + human-readable
    markers."""
    deals = list(deals)
    applied: list[str] = []
    for row in db.list_deal_feedback(conn, client):
        action = row["action"]
        deal_name = row["deal"]
        folder_path = row.get("folder_path")
        payload = json.loads(row["payload"]) if row.get("payload") else {}

        if action == "add_folder" and folder_path:
            target = _find_deal(deals, deal_name)
            if target is None:
                target = DealFolder(
                    client=client,
                    name=deal_name,
                    folder_paths=[],
                    confidence=1.0,
                    method="learned",
                    evidence=DealEvidence(),
                )
                deals.append(target)
            if _norm_path(folder_path) not in {_norm_path(p) for p in target.folder_paths}:
                target.folder_paths = sorted({*target.folder_paths, folder_path})
                applied.append(f"pinned folder {folder_path!r} to deal {target.name!r}")

        elif action == "remove_folder" and folder_path:
            norm = _norm_path(folder_path)
            for deal in deals:
                kept = [p for p in deal.folder_paths if _norm_path(p) != norm]
                if len(kept) != len(deal.folder_paths):
                    deal.folder_paths = kept
                    applied.append(f"removed folder {folder_path!r} from deal {deal.name!r}")
            before = len(deals)
            deals = [d for d in deals if d.folder_paths]
            if len(deals) != before:
                applied.append(f"dropped now-empty deal(s) after removing {folder_path!r}")

        elif action == "rename":
            new_name = payload.get("new_name") or payload.get("to")
            target = _find_deal(deals, deal_name)
            if target is not None and new_name:
                applied.append(f"renamed deal {target.name!r} to {new_name!r}")
                target.name = new_name

        elif action == "merge":
            other_name = payload.get("into") or payload.get("target") or payload.get("with")
            primary = _find_deal(deals, deal_name)
            other = _find_deal(deals, other_name) if other_name else None
            if primary is not None and other is not None and primary is not other:
                primary.folder_paths = sorted(
                    {*primary.folder_paths, *other.folder_paths}
                )
                deals = [d for d in deals if d is not other]
                applied.append(f"merged deal {other.name!r} into {primary.name!r}")

        elif action == "split":
            # Best-effort: carve one named folder out of a deal into its own
            # deal. The new deal's name comes from the payload.
            new_name = payload.get("new_name") or payload.get("name")
            target = _find_deal(deals, deal_name)
            if target is not None and folder_path and new_name:
                norm = _norm_path(folder_path)
                if any(_norm_path(p) == norm for p in target.folder_paths):
                    target.folder_paths = [
                        p for p in target.folder_paths if _norm_path(p) != norm
                    ]
                    deals.append(
                        DealFolder(
                            client=client,
                            name=new_name,
                            folder_paths=[folder_path],
                            confidence=target.confidence,
                            method="learned",
                            evidence=DealEvidence(),
                        )
                    )
                    applied.append(
                        f"split folder {folder_path!r} out of {target.name!r} into {new_name!r}"
                    )
                    deals = [d for d in deals if d.folder_paths]
    return deals, applied


def _apply_priors(
    deals: list[DealFolder], priors: dict[str, float], cap: float, client: str
) -> list[str]:
    """Apply client-scoped prior nudges to each deal's confidence (additive,
    clamped to [0, 1]), capping the TOTAL prior bump per deal at ``cap``.
    Mutates the deals in place; records each nudge in evidence.components.
    Returns human-readable markers."""
    applied: list[str] = []
    for deal in deals:
        budget = cap
        for signal, nudge in priors.items():
            if budget <= 0 or nudge <= 0:
                continue
            grant = round(min(nudge, budget), 4)
            if grant <= 0:
                continue
            deal.confidence = round(max(0.0, min(1.0, deal.confidence + grant)), 4)
            key = f"learned_{signal}_prior"
            deal.evidence.components[key] = round(
                deal.evidence.components.get(key, 0.0) + grant, 4
            )
            budget = round(budget - grant, 4)
            applied.append(f"+{grant:.2f} {signal} prior (learned for {client!r})")
    return applied


def apply_feedback(
    deals: list[DealFolder],
    conn: sqlite3.Connection,
    config: Config,
    client: str,
) -> tuple[list[DealFolder], list[str]]:
    """Replay this client's recorded corrections onto a freshly discovered deal
    list. Run at the END of refresh_deals (after discovery + LLM merge, before
    persistence + assign_file_deals).

    Returns (edited_deals, applied_markers). When
    ``config.deal_discovery.learning.enabled`` is False, this is a NO-OP
    returning ``(deals, [])`` — and with no recorded corrections and no manual
    override the result is identical to the input (priors will be empty), so
    legacy behavior is preserved.
    """
    if not config.deal_discovery.learning.enabled:
        return deals, []

    # HARD layer first (deterministic exact edits).
    deals, applied = _apply_hard_corrections(deals, conn, client)

    # PRIOR layer: reusable client-scoped nudges (this is what generalizes a
    # correction on one deal to a different new deal under the same client).
    priors = derive_layout_priors(conn, config, client)
    cap = max(0.0, config.deal_discovery.learning.prior_bump)
    if priors and cap > 0:
        applied.extend(_apply_priors(deals, priors, cap, client))

    deals.sort(key=lambda d: (-d.confidence, d.name.lower()))
    return deals, applied
