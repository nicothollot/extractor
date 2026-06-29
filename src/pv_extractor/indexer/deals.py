"""Smart deal-folder discovery: deals are found, not assumed.

The legacy rule (deal = first folder segment under the client) fails on real
client trees: deal folders sit under strategy groups
(``Ares Management\\Direct Lending Investments\\Elevate\\2025 Q1``), under
project codenames (``Fund Opinion\\Project Cobalt\\Symplr\\9.30.2025``), or
BELOW the period folder (``Auldbrass\\Investments\\12.31.22\\LinkSquares``);
some client folders contain no deals at all.

Discovery builds the client's folder tree from the index, classifies every
segment (PERIOD / STRUCTURAL / ADMIN / NEUTRAL) and walks down from the
client. A NEUTRAL folder is a *container* (recurse) when its period children
hold recurring neutral subfolders (deal-below-period layout), when two or
more of its neutral children carry period evidence of their own (a strategy
group), or when it is a bare pass-through wrapper; otherwise it is emitted as
a deal. Recursion stops at STRUCTURAL/ADMIN folders (their contents belong to
the enclosing deal, or to no deal) and at emitted deals. Deals found under
period folders are merged across sibling periods by normalized name.

Every emitted deal carries an additive confidence score (weights in
config.deal_discovery) plus the full evidence breakdown; results persist to
the ``deal_folders`` table and ``files.deal`` is rewritten so the locator,
run scopes, and the GUI all see the discovered deals. With
deal_discovery.enabled=false nothing here runs and files keep the legacy
``rel[1]`` assignment from derive.py.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from pv_extractor.config import Config, DealDiscoveryConfig
from pv_extractor.indexer import db
from pv_extractor.indexer.periods import parse_date_folder
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DealEvidence, DealFolder, FileRecord
from pv_extractor.normalize import normalize_text, relative_segments

logger = logging.getLogger(__name__)

_BARE_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_DECOR_RE = re.compile(r"^(?:\(\d+\)\s*|\d+\.\s+|\+)+")
# Leading sequence prefixes only: "(16) ", "1. " (the dot needs trailing space).
_SEQ_PREFIX_RE = re.compile(r"^(?:\(\d+\)\s*|\d+\.\s+)+")
# A parenthesized group that contains at least one digit — almost always an
# embedded date/sequence tag: 'PBC (12.31.2023)', 'Lender Call (11.20.24)'.
_PAREN_DATE_RE = re.compile(r"\([^)]*\d[^)]*\)")
# A free-standing date/period token anywhere in a folder name, on token
# boundaries so a company id like '100154' or 'C-2' is never mistaken for one.
_DATE_TOKEN_RE = re.compile(
    r"""(?<![A-Za-z0-9])(?:
        \d{1,4}[./\-]\d{1,2}[./\-]\d{2,4}          # 12.31.2023 / 2020.10.31 / 1.31.25
      | \d{1,2}[./\-]\d{4}                            # 03.2026
      | (?:19|20)\d{2}                                # 2025
      | [1-4][qQ]\d{0,4} | [qQ][1-4]\d{0,4}           # 1q / q1 / q12025
      | (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[-' ]?\d{2,4}
    )(?![A-Za-z0-9])""",
    re.IGNORECASE | re.VERBOSE,
)

_PERIOD = "period"
_STRUCTURAL = "structural"
_ADMIN = "admin"
_NEUTRAL = "neutral"


def _clean_name(raw: str) -> str:
    """Strip embedded date/period tokens, leading sequence prefixes and tidy
    punctuation, leaving the investment-name part of a folder name. Casing is
    preserved: 'PBC (12.31.2023)' -> 'PBC', '(24) BDC Monitor - Spring 2026'
    -> 'BDC Monitor - Spring', '12.31.2023' -> ''. The empty string means the
    folder name is purely a date/period (a PERIOD folder)."""
    work = raw.strip().lstrip("+").strip()
    work = _SEQ_PREFIX_RE.sub("", work)
    work = _PAREN_DATE_RE.sub(" ", work)
    work = _DATE_TOKEN_RE.sub(" ", work)
    work = re.sub(r"[._/]+", " ", work)
    return re.sub(r"\s+", " ", work).strip(" -–—,.")


def _name_and_period(raw: str) -> tuple[str, list[str], str | None]:
    """(cleaned_display_name, normalized_name_tokens, period_key). period_key is
    an iso date or 'year:YYYY' when the folder name carries date evidence (even
    when a real name precedes it, as in 'PBC (12.31.2023)'); None otherwise.

    The name is only date-stripped when a date is actually present, so a folder
    with no date keeps its exact on-disk name ('T.D. Williamson', '+Digital
    Edge') — date-cleaning must never mangle a plain investment name."""
    period = parse_date_folder(raw)
    pk = period.isoformat() if period is not None else None
    if pk is None:
        core = _DECOR_RE.sub("", raw.strip()).strip()
        if _BARE_YEAR_RE.match(core):
            pk = f"year:{core}"
    has_date = pk is not None or bool(_PAREN_DATE_RE.search(raw)) or bool(_DATE_TOKEN_RE.search(raw))
    cleaned = _clean_name(raw) if has_date else raw.strip()
    return cleaned, normalize_text(cleaned).split(), pk


@dataclass
class _Node:
    """One folder in a client's subtree."""

    name: str
    segments: tuple[str, ...]  # path segments from pv_root, client included
    role: str = _NEUTRAL
    period_key: str | None = None  # iso date or 'year:YYYY' for PERIOD nodes
    embedded_period: str | None = None  # date carried in a NON-period name ('PBC (12.31.2023)')
    children: dict[str, "_Node"] = field(default_factory=dict)  # lowercase name -> node
    files_direct: int = 0
    memo_files_direct: int = 0
    memo_stems_direct: list[str] = field(default_factory=list)  # raw stems of direct memo-keyword files
    # filled by _aggregate (postorder):
    subtree_files: int = 0
    subtree_memo_files: int = 0
    subtree_period_keys: set[str] = field(default_factory=set)


def _period_key(name: str) -> str | None:
    """PERIOD detection: everything parse_date_folder accepts, plus bare-year
    folders ('2017', '(4) 2025') that wrap the real date folders on the share.
    NOTE: returns non-None for names with an EMBEDDED date too ('PBC
    (12.31.2023)'); used only to exclude any date-bearing segment from a deal's
    container key. Use `_classify` for role/name decisions."""
    parsed = parse_date_folder(name)
    if parsed is not None:
        return parsed.isoformat()
    stripped = _DECOR_RE.sub("", name.strip()).strip()
    if _BARE_YEAR_RE.match(stripped):
        return f"year:{stripped}"
    return None


def _classify(name: str, cfg: DealDiscoveryConfig) -> tuple[str, str | None, str | None, str]:
    """(role, period_key, embedded_period, display_name).

    The folder name is first split into its investment-name part and any date
    it carries. When the name is PURELY a date ('12.31.2023', '2025 Q1', '(4)
    2025') the residual name is empty and the folder is PERIOD. Otherwise the
    residual name decides the role (ADMIN > STRUCTURAL > NEUTRAL) and any date
    rides along as `embedded_period` so a 'PBC (12.31.2023)' folder is the deal
    'PBC' observed at 2023-12-31 — NOT three separate PBC deals. STRUCTURAL =
    every residual token structural/glue/numeric, or a short From/To
    correspondence folder. The display name is the date-stripped, original-cased
    investment name ('PBC', 'BDC Monitor - Spring')."""
    cleaned, tokens, pk = _name_and_period(name)
    if not tokens:
        # No investment-name part: a pure date folder, or a punctuation-only one.
        return (_PERIOD, pk, None, name) if pk is not None else (_STRUCTURAL, None, None, name)
    display = cleaned or name
    admin = set(cfg.admin_tokens)
    if name.strip().startswith("_") and (set(tokens) & admin or len(tokens) == 1):
        return _ADMIN, None, pk, display
    if set(tokens) & admin and len(tokens) <= 2:
        return _ADMIN, None, pk, display
    structural = set(cfg.structural_tokens) | set(cfg.glue_tokens)
    if all(tok in structural or tok.isdigit() for tok in tokens):
        return _STRUCTURAL, None, pk, display
    if tokens[0] in cfg.correspondence_prefixes and len(tokens) <= 3:
        return _STRUCTURAL, None, pk, display
    return _NEUTRAL, None, pk, display


def _is_generic_deal_name(name: str, cfg: DealDiscoveryConfig) -> bool:
    """True when a folder's (date-stripped) name is ENTIRELY generic — every
    token structural/glue/grouping/admin/stopword — so it is a document bucket,
    not an investment: 'Research', 'Reports', 'Prior Period', 'General'. Such
    leaf folders are never emitted as deals (gated by
    `exclude_generic_deal_names`); the legitimate investments they may contain
    are still surfaced via the shared-bucket branch."""
    tokens = normalize_text(_clean_name(name)).split()
    if not tokens:
        return True
    stop = (
        set(cfg.structural_tokens) | set(cfg.glue_tokens) | set(cfg.grouping_tokens)
        | set(cfg.admin_tokens) | set(cfg.deal_name_stopwords)
    )
    return all(tok in stop or tok.isdigit() for tok in tokens)


def _build_tree(client: str, records: list[FileRecord], config: Config) -> _Node | None:
    """Folder tree for one client from its indexed files (folders only exist
    in the index insofar as they contain files — empty dirs are invisible)."""
    cfg = config.deal_discovery
    root: _Node | None = None
    for rec in records:
        rel = relative_segments(rec.folder_path, config.pv_root)
        if not rel or rel[0] != client:
            continue
        if root is None:
            root = _Node(name=client, segments=(client,))
        node = root
        for seg in rel[1:]:
            child = node.children.get(seg.lower())
            if child is None:
                role, key, embedded, display = _classify(seg, cfg)
                child = _Node(
                    name=display,
                    segments=node.segments + (seg,),  # raw segment: folder_paths must match disk
                    role=role,
                    period_key=key,
                    embedded_period=embedded,
                )
                node.children[seg.lower()] = child
            node = child
        node.files_direct += 1
        if rec.contains_memo_keyword:
            node.memo_files_direct += 1
            stem = rec.file_name.rsplit(".", 1)[0] if "." in rec.file_name else rec.file_name
            node.memo_stems_direct.append(stem)
    if root is not None:
        _aggregate(root)
    return root


def _aggregate(node: _Node) -> None:
    node.subtree_files = node.files_direct
    node.subtree_memo_files = node.memo_files_direct
    node.subtree_period_keys = set()
    if node.period_key is not None:
        node.subtree_period_keys.add(node.period_key)
    if node.embedded_period is not None:
        node.subtree_period_keys.add(node.embedded_period)
    for child in node.children.values():
        _aggregate(child)
        node.subtree_files += child.subtree_files
        node.subtree_memo_files += child.subtree_memo_files
        node.subtree_period_keys |= child.subtree_period_keys


@dataclass
class _Candidate:
    node: _Node
    period_ancestor: str | None  # nearest PERIOD ancestor's key (deal-below-period layout)
    container_depth: int  # NEUTRAL containers traversed between client and deal
    admin_container: bool = False  # surfaced by recursing into an ADMIN container
    shared_bucket: bool = False  # one cluster carved from a shared mixed-investment folder
    cluster_name: str | None = None  # representative display name for a shared-bucket cluster
    name_filter: tuple[str, ...] = ()  # cluster's representative tokens (file -> cluster matching)


def _neutral_grandchild_recurrence(node: _Node) -> bool:
    """True when this node's period children hold neutral subfolders that
    recur (same normalized name) under >= 2 distinct periods — the signature
    of the deal-below-period layout, where this node is a pass-through."""
    name_periods: dict[str, set[str]] = defaultdict(set)
    for child in node.children.values():
        if child.role != _PERIOD:
            continue
        for grand in child.children.values():
            if grand.role == _NEUTRAL and grand.subtree_files > 0:
                name_periods[normalize_text(grand.name)].add(child.period_key or child.name)
    return any(len(periods) >= 2 for periods in name_periods.values())


def _is_grouping_name(name: str, cfg: DealDiscoveryConfig) -> bool:
    """True when the folder name is a pure grouping/strategy token (every
    normalized token is a grouping token, or all glue/structural), so the
    folder is a wrapper, not a company — e.g. 'Investments', 'Direct
    Lending'."""
    tokens = normalize_text(name).split()
    if not tokens:
        return False
    grouping = set(cfg.grouping_tokens)
    glue = set(cfg.glue_tokens) | set(cfg.structural_tokens)
    return all(tok in grouping or tok in glue for tok in tokens)


def _is_container(node: _Node, cfg: DealDiscoveryConfig) -> bool:
    """Container = recurse for deals inside; otherwise the node IS a deal.
    (The client root never reaches here — _walk starts inside it.)"""
    period_children = [c for c in node.children.values() if c.role == _PERIOD]
    neutral_children = [c for c in node.children.values() if c.role == _NEUTRAL]
    deal_like = [c for c in neutral_children if c.subtree_period_keys and c.subtree_files > 0]
    if period_children:
        # Date folders directly below: this is a deal UNLESS the dates hold
        # recurring neutral subfolders (deal-below-period layout).
        return _neutral_grandchild_recurrence(node)
    if len(deal_like) >= 2:
        return True  # strategy group: several period-bearing deals inside
    if deal_like and node.files_direct == 0:
        return True  # wrapper / project codename around a single period-bearing deal
    if (
        len(neutral_children) == 1
        and node.files_direct == 0
        and not any(c.role == _STRUCTURAL for c in node.children.values())
    ):
        # Bare wrapper: nothing but one neutral subfolder ('PE\\Project
        # Antares') — the more specific name below is the deal.
        return True
    # Pure grouping name with neutral deal-like children: recurse even when the
    # children are mixed with stray files / structural siblings ('Direct
    # Lending Investments' holding several deals). Conservative: requires the
    # name to be a grouping token AND at least one period-bearing neutral child.
    if deal_like and _is_grouping_name(node.name, cfg):
        return True
    return False


def _admin_holds_real_deal(node: _Node) -> bool:
    """True when an ADMIN node hides a genuine NEUTRAL deal folder: somewhere
    in its subtree sits a NEUTRAL node whose OWN subtree carries period
    evidence or memo-keyword files. Admin branches that hold only HL
    report/structural files under date folders (the Ares ``_Admin`` cases) have
    no such neutral node, so they stay dead-ends and never become deals."""

    def visit(n: _Node) -> bool:
        for child in n.children.values():
            if child.role == _NEUTRAL and child.subtree_files > 0 and (
                child.subtree_period_keys or child.subtree_memo_files > 0
            ):
                return True
            if visit(child):
                return True
        return False

    return visit(node)


_DOCTYPE_STRIP_RE = re.compile(
    r"\b(valuation|memo|memorandum|ic|investment|committee|portfolio|review|"
    r"summary|report|deck|presentation|draft|final|vf|copy|update|q[1-4]|"
    r"fy\d{2,4}|version|v\d+)\b",
    re.IGNORECASE,
)


def _asset_key(stem: str) -> str:
    """Reduce a memo file stem to its investment-name tokens: drop doc-type
    words, version/period decorations and digits, leaving the company name
    (normalized). 'Acme Valuation Memo Q1 2026 vf' -> 'acme'."""
    no_doc = _DOCTYPE_STRIP_RE.sub(" ", stem)
    norm = normalize_text(no_doc)
    tokens = [t for t in norm.split() if not t.isdigit() and len(t) > 1]
    return " ".join(tokens).strip()


def _cluster_memo_stems(
    node: _Node, cfg: DealDiscoveryConfig
) -> list[tuple[str, tuple[str, ...]]]:
    """Cluster the node's DIRECT memo-keyword file stems by normalized
    investment name (rapidfuzz >= cluster_ratio_threshold). Returns one
    (representative_display_name, name_filter_tokens) per cluster, ordered by
    cluster size then name. Used only for shared-bucket detection."""
    from rapidfuzz import fuzz

    # Map each distinct asset key to a representative raw stem (first seen).
    key_to_rep: dict[str, str] = {}
    for stem in node.memo_stems_direct:
        key = _asset_key(stem)
        if not key:
            continue
        key_to_rep.setdefault(key, stem)

    # Greedy single-link clustering of the asset keys.
    keys = list(key_to_rep)
    clusters: list[list[str]] = []
    for key in keys:
        placed = False
        for cluster in clusters:
            if any(
                fuzz.token_set_ratio(key, other) >= cfg.cluster_ratio_threshold
                for other in cluster
            ):
                cluster.append(key)
                placed = True
                break
        if not placed:
            clusters.append([key])

    out: list[tuple[str, tuple[str, ...]]] = []
    for cluster in clusters:
        rep_stem = key_to_rep[cluster[0]]
        name_filter = tuple(sorted({tok for k in cluster for tok in k.split()}))
        # Display name: the title-cased asset key of the representative.
        display = _asset_key(rep_stem).title() or rep_stem
        out.append((display, name_filter))
    out.sort(key=lambda item: item[0].lower())
    return out


def _is_shared_bucket(node: _Node, cfg: DealDiscoveryConfig) -> list[tuple[str, tuple[str, ...]]] | None:
    """A NEUTRAL leaf-ish folder is a SHARED BUCKET when it DIRECTLY holds
    memo-keyword files for >= shared_bucket_min_clusters DIFFERENT investments
    and has no per-deal neutral subfolders of its own. Returns the cluster list
    when it qualifies, else None. Gated by shared_bucket_enabled."""
    if not cfg.shared_bucket_enabled:
        return None
    if node.memo_files_direct < cfg.shared_bucket_min_clusters:
        return None
    # No per-deal substructure: the memos sit RIGHT HERE, not in subfolders.
    if any(c.role == _NEUTRAL and c.subtree_files > 0 for c in node.children.values()):
        return None
    clusters = _cluster_memo_stems(node, cfg)
    if len(clusters) < cfg.shared_bucket_min_clusters:
        return None
    return clusters


def _walk(
    node: _Node,
    *,
    period_ancestor: str | None,
    container_depth: int,
    out: list[_Candidate],
    cfg: DealDiscoveryConfig,
    admin_container: bool = False,
) -> None:
    """Recurse through a container node, emitting deal candidates."""
    for child in node.children.values():
        if child.role == _PERIOD:
            _walk(
                child,
                period_ancestor=child.period_key,
                container_depth=container_depth,
                out=out,
                cfg=cfg,
                admin_container=admin_container,
            )
        elif child.role == _NEUTRAL and child.subtree_files > 0:
            if _is_container(child, cfg):
                _walk(
                    child,
                    period_ancestor=period_ancestor,
                    container_depth=container_depth + 1,
                    out=out,
                    cfg=cfg,
                    admin_container=admin_container,
                )
            else:
                clusters = _is_shared_bucket(child, cfg)
                if clusters is not None:
                    # Shared mixed-investment bucket: emit ONE synthetic deal
                    # per cluster, all pointing at this one folder.
                    for display, name_filter in clusters:
                        out.append(
                            _Candidate(
                                node=child,
                                period_ancestor=period_ancestor,
                                container_depth=container_depth,
                                admin_container=admin_container,
                                shared_bucket=True,
                                cluster_name=display,
                                name_filter=name_filter,
                            )
                        )
                elif cfg.exclude_generic_deal_names and _is_generic_deal_name(child.name, cfg):
                    # Generic document bucket ('Research (2020.10.31)', 'Reports')
                    # — not an investment. Skip; its files fall to deal=NULL.
                    continue
                else:
                    out.append(
                        _Candidate(
                            node=child,
                            period_ancestor=period_ancestor,
                            container_depth=container_depth,
                            admin_container=admin_container,
                        )
                    )
        elif child.role == _ADMIN and _admin_holds_real_deal(child):
            # ADMIN node is normally a dead-end, but here it wraps a genuine
            # neutral+period/memo-bearing deal — recurse INTO it. The admin
            # node itself is never emitted as a deal (we recurse its children).
            _walk(
                child,
                period_ancestor=period_ancestor,
                container_depth=container_depth,
                out=out,
                cfg=cfg,
                admin_container=True,
            )
        # STRUCTURAL / plain ADMIN children: stop — their contents belong to
        # the enclosing deal (or to no deal at all).


def _direct_period_keys(node: _Node) -> set[str]:
    return {c.period_key for c in node.children.values() if c.role == _PERIOD and c.period_key}


def _structural_below(node: _Node) -> int:
    """STRUCTURAL folders among the node's children and its period children's
    children — the Client/Analysis layout signal wherever the dates sit."""
    count = sum(1 for c in node.children.values() if c.role == _STRUCTURAL)
    for c in node.children.values():
        if c.role == _PERIOD:
            count += sum(1 for g in c.children.values() if g.role == _STRUCTURAL)
    return count


def _score(
    name: str,
    *,
    periods: int,
    structural: int,
    memo_files: int,
    total_files: int,
    container_depth: int,
    flat_default: bool,
    cfg: DealDiscoveryConfig,
    admin_container: bool = False,
    shared_bucket: bool = False,
) -> tuple[float, dict[str, float]]:
    w = cfg.weights
    components: dict[str, float] = {}
    if periods >= 1:
        components["period_evidence"] = w.period_evidence
    if periods >= 2:
        components["multi_period_bonus"] = w.multi_period_bonus
    if structural >= 1:
        components["structural_children"] = w.structural_children
    if memo_files >= 1:
        components["memo_keyword_files"] = w.memo_keyword_files
    if total_files >= 1:
        components["any_files"] = w.any_files
    if flat_default and periods == 0:
        components["flat_default_bonus"] = w.flat_default_bonus
    tokens = set(normalize_text(name).split())
    if tokens & set(cfg.grouping_tokens):
        components["grouping_name_penalty"] = w.grouping_name_penalty
    if container_depth > 0:
        components["container_depth_penalty"] = w.container_depth_penalty * container_depth
    if admin_container:
        components["admin_container"] = w.admin_container
    if shared_bucket:
        components["shared_bucket"] = w.shared_bucket
    confidence = max(0.0, min(1.0, sum(components.values())))
    return confidence, components


def discover_deals(conn: sqlite3.Connection, config: Config, client: str) -> list[DealFolder]:
    """Heuristic discovery for one client. Pure read; persistence and
    files.deal assignment happen in refresh_deals."""
    cfg = config.deal_discovery
    records = db.files_for_client(conn, client)
    root = _build_tree(client, records, config)
    if root is None:
        return []

    candidates: list[_Candidate] = []
    _walk(root, period_ancestor=None, container_depth=0, out=candidates, cfg=cfg)

    # Merge candidates that are the same deal observed in several places: the
    # same NAME under the same non-period container prefix. This unions both
    # the deal-below-period layout (same name across sibling PERIOD branches)
    # AND the same-container sibling case (one deal whose memos sit in two
    # sibling NEUTRAL folders). Shared-bucket clusters key on the cluster name
    # so distinct investments in one folder stay distinct.
    grouped: dict[tuple[str, tuple[str, ...]], list[_Candidate]] = defaultdict(list)
    for cand in candidates:
        container = tuple(
            seg.lower() for seg in cand.node.segments[:-1] if _period_key(seg) is None
        )
        group_name = normalize_text(cand.cluster_name) if cand.shared_bucket else normalize_text(cand.node.name)
        grouped[(group_name, container)].append(cand)

    deals: list[DealFolder] = []
    for group in grouped.values():
        shared_bucket = any(c.shared_bucket for c in group)
        admin_container = any(c.admin_container for c in group)
        if shared_bucket:
            # One synthetic deal for this investment cluster; display name is
            # the cluster name, path is the single shared folder.
            name = next(c.cluster_name for c in group if c.cluster_name)
        else:
            names = Counter(c.node.name for c in group)
            name = names.most_common(1)[0][0]
        name_filter = sorted({tok for c in group for tok in c.name_filter})
        direct_periods: set[str] = set()
        ancestor_periods: set[str] = set()
        for c in group:
            direct_periods |= _direct_period_keys(c.node)
            if c.node.embedded_period:  # 'PBC (12.31.2023)' — date carried in the deal's own name
                direct_periods.add(c.node.embedded_period)
            if c.period_ancestor:
                ancestor_periods.add(c.period_ancestor)
        periods = len(direct_periods) + len(ancestor_periods)
        structural = sum(_structural_below(c.node) for c in group)
        memo_files = sum(c.node.subtree_memo_files for c in group)
        total_files = sum(c.node.subtree_files for c in group)
        container_depth = min(c.container_depth for c in group)
        flat_default = any(len(c.node.segments) == 2 for c in group)  # directly under the client
        confidence, components = _score(
            name,
            periods=periods,
            structural=structural,
            memo_files=memo_files,
            total_files=total_files,
            container_depth=container_depth,
            flat_default=flat_default,
            admin_container=admin_container,
            shared_bucket=shared_bucket,
            cfg=cfg,
        )
        if confidence < cfg.min_confidence:
            continue
        deals.append(
            DealFolder(
                client=client,
                name=name,
                folder_paths=sorted({"\\".join(c.node.segments) for c in group}),
                confidence=round(confidence, 4),
                method="heuristic",
                evidence=DealEvidence(
                    period_children=len(direct_periods),
                    period_recurrence=len(ancestor_periods),
                    structural_children=structural,
                    memo_keyword_files=memo_files,
                    total_files=total_files,
                    container_depth=container_depth,
                    admin_container=admin_container,
                    shared_bucket=shared_bucket,
                    name_filter=name_filter,
                    components={k: round(v, 4) for k, v in components.items()},
                ),
            )
        )

    deals.sort(key=lambda d: (-d.confidence, d.name.lower()))
    _ensure_unique_names(deals)
    return deals


def _ensure_unique_names(deals: list[DealFolder]) -> None:
    """Guarantee every deal under one client has a UNIQUE name, in place. The
    deal_folders table is keyed on (client, deal), so a duplicate would crash
    persistence with a UNIQUE-constraint IntegrityError and abort the scan.

    First pass: deals whose normalized name collides are disambiguated with
    their parent folder name (the readable, intended form). Second pass: a hard
    guarantee — any name still colliding (same parent, a 3-way clash, or a
    suffixed name that now matches another) gets a numeric suffix. This never
    drops a deal and never lets two deals share a name. Deals are processed in
    their pre-sorted order so the highest-confidence deal keeps the bare name."""
    by_name = Counter(normalize_text(d.name) for d in deals)
    for deal in deals:
        if by_name[normalize_text(deal.name)] > 1:
            segments = deal.folder_paths[0].split("\\") if deal.folder_paths else []
            if len(segments) >= 2:
                deal.name = f"{deal.name} ({segments[-2]})"
    seen: set[str] = set()
    for deal in deals:
        base = deal.name
        key = normalize_text(base)
        suffix = 2
        while key in seen:
            deal.name = f"{base} ({suffix})"
            key = normalize_text(deal.name)
            suffix += 1
        seen.add(key)


def needs_llm_assist(deals: list[DealFolder], cfg: DealDiscoveryConfig) -> bool:
    """Auto-assist trigger: nothing found, or nothing convincing."""
    if not cfg.llm.enabled:
        return False
    return not deals or max(d.confidence for d in deals) < cfg.llm.trigger_confidence


def merge_llm_deals(
    heuristic: list[DealFolder], llm: list[DealFolder], cfg: DealDiscoveryConfig
) -> list[DealFolder]:
    """The deterministic pass stays primary: LLM entries corroborate existing
    deals (confidence bump) or fill gaps; they never remove a heuristic deal."""
    w = cfg.weights
    by_path = {p.lower().replace("/", "\\"): d for d in heuristic for p in d.folder_paths}
    merged = list(heuristic)
    for cand in llm:
        existing = None
        for p in cand.folder_paths:
            existing = by_path.get(p.lower().replace("/", "\\"))
            if existing:
                break
        if existing is not None:
            existing.evidence.llm_corroborated = True
            bumped = max(existing.confidence, cand.confidence)
            existing.confidence = round(
                min(1.0, bumped + w.llm_corroboration_bonus), 4
            )
            existing.evidence.components["llm_corroboration_bonus"] = w.llm_corroboration_bonus
        else:
            merged.append(cand)
    merged.sort(key=lambda d: (-d.confidence, d.name.lower()))
    return merged


def _best_cluster_deal(
    stem: str, bucket_deals: list[DealFolder], cfg: DealDiscoveryConfig
) -> str | None:
    """Assign one file in a shared bucket to the cluster-deal whose name_filter
    best matches the file's asset key (rapidfuzz token_set_ratio >=
    shared_bucket_name_match_threshold). No cluster clears the floor -> None
    (nothing silent: an unmatched file gets deal=NULL, never a wrong guess)."""
    from rapidfuzz import fuzz

    asset = _asset_key(stem)
    if not asset:
        return None
    best_name: str | None = None
    best_score = -1.0
    for deal in bucket_deals:
        filt = " ".join(deal.evidence.name_filter)
        if not filt:
            continue
        score = fuzz.token_set_ratio(asset, filt)
        if score >= cfg.shared_bucket_name_match_threshold and score > best_score:
            best_score = score
            best_name = deal.name
    return best_name


def assign_file_deals(
    conn: sqlite3.Connection, config: Config, client: str, deals: list[DealFolder]
) -> int:
    """Rewrite files.deal for one client from the discovered deal folders:
    deepest matching deal folder wins; files under no deal folder get NULL.
    Shared-bucket folders (several cluster-deals sharing one folder path) are
    resolved per-file by stem -> cluster name-filter matching, not by folder.
    Returns the number of rows changed."""
    cfg = config.deal_discovery
    # Shared-bucket deals grouped by their (single) folder path; these folders
    # are resolved file-by-file, so they are excluded from the prefix map.
    bucket_by_path: dict[tuple[str, ...], list[DealFolder]] = defaultdict(list)
    for deal in deals:
        if deal.evidence.shared_bucket:
            for path in deal.folder_paths:
                segs = tuple(s.lower() for s in path.replace("/", "\\").split("\\") if s)
                bucket_by_path[segs].append(deal)
    # Deepest-first so a file under a bucket matches the most specific bucket.
    # A bucket has no per-deal neutral subfolders, so ANY descendant (e.g. a
    # memo in bucket\Client\) belongs to one of its clusters or to none —
    # route those through the cluster matcher too, never silently to NULL.
    bucket_paths_deepest_first = sorted(bucket_by_path, key=lambda p: -len(p))

    prefix_to_deal: list[tuple[tuple[str, ...], str]] = []
    for deal in deals:
        if deal.evidence.shared_bucket:
            continue
        for path in deal.folder_paths:
            segs = tuple(s.lower() for s in path.replace("/", "\\").split("\\") if s)
            prefix_to_deal.append((segs, deal.name))
    prefix_to_deal.sort(key=lambda item: -len(item[0]))  # deepest first

    changes: dict[int, str | None] = {}
    folder_cache: dict[str, str | None] = {}
    for rec in db.files_for_client(conn, client):
        rel = relative_segments(rec.folder_path, config.pv_root)
        segs = tuple(s.lower() for s in (rel or []))
        bucket_match = next(
            (bp for bp in bucket_paths_deepest_first if segs[: len(bp)] == bp), None
        )
        if bucket_match is not None:
            # Shared bucket (file in the bucket folder or a structural subfolder
            # of it): match this file's stem to a cluster.
            stem = rec.file_name.rsplit(".", 1)[0] if "." in rec.file_name else rec.file_name
            new_deal = _best_cluster_deal(stem, bucket_by_path[bucket_match], cfg)
        else:
            key = rec.folder_path.lower()
            if key not in folder_cache:
                assigned = None
                for prefix, deal_name in prefix_to_deal:
                    if segs[: len(prefix)] == prefix:
                        assigned = deal_name
                        break
                folder_cache[key] = assigned
            new_deal = folder_cache[key]
        if rec.deal != new_deal and rec.row_id is not None:
            changes[rec.row_id] = new_deal
    return db.update_file_deals(conn, changes)


def refresh_deals(
    conn: sqlite3.Connection,
    config: Config,
    clients: list[str] | None = None,
    *,
    use_llm: bool | None = None,
    llm_model: str | None = None,
    llm_effort: str | None = None,
    apply_learning: bool = True,
) -> dict[str, list[DealFolder]]:
    """Discover, persist and apply deal folders for the given clients (all
    indexed clients when None). `use_llm` forces (True) or suppresses (False)
    the Claude Code assist; None defers to config.deal_discovery.llm.enabled
    plus the low-confidence trigger. `apply_learning` replays recorded analyst
    corrections + client-scoped layout priors (the default); set False for a
    raw re-discovery that ignores the learning layer. Returns client ->
    discovered deals."""
    if not config.deal_discovery.enabled:
        return {}
    results: dict[str, list[DealFolder]] = {}
    for client in clients if clients is not None else db.distinct_clients(conn):
        # Per-client isolation: a discovery/persistence failure for ONE client
        # (e.g. a bad folder) is logged and skipped, never aborting the whole
        # scan's deal-discovery pass.
        try:
            deals = discover_deals(conn, config, client)
            llm_wanted = use_llm if use_llm is not None else needs_llm_assist(
                deals, config.deal_discovery
            )
            llm_succeeded = False
            llm_discovery_meta: tuple[str, str] | None = None  # (model, effort)
            if llm_wanted:
                from pv_extractor.llm.deal_discovery import llm_discover_deals  # heavy import, optional path

                llm_deals, error = llm_discover_deals(
                    conn, config, client, model=llm_model, effort=llm_effort
                )
                if error is None:
                    deals = merge_llm_deals(deals, llm_deals, config.deal_discovery)
                    llm_succeeded = True
                    # The actual model/effort used rides on the LLM deals' method
                    # (claude-code:<model>:<effort>); fall back to the requested /
                    # configured defaults when the pass grounded nothing.
                    model = llm_model or config.deal_discovery.llm.model
                    effort = llm_effort or config.deal_discovery.llm.effort
                    for d in llm_deals:
                        if d.method.startswith("claude-code:"):
                            parts = d.method.split(":")
                            model = parts[1] if len(parts) > 1 else model
                            effort = parts[2] if len(parts) > 2 else effort
                            break
                    llm_discovery_meta = (model, effort)
                else:
                    log_event(logger, "deal discovery llm assist failed", client=client, error=error)
            # Per-client learning (Phase A3): replay recorded analyst corrections
            # (hard edits) + reusable client-scoped layout priors. No-op when
            # learning is disabled or no corrections exist, so legacy behavior is
            # preserved exactly.
            applied_feedback: list[str] = []
            if apply_learning:
                from pv_extractor.indexer.deal_learning import apply_feedback

                deals, applied_feedback = apply_feedback(deals, conn, config, client)
            # Hard guarantee of (client, deal) uniqueness right before persistence
            # — discovery uniquifies its own output, but the learning layer can
            # add/rename/merge deals, so re-assert it here (UNIQUE-constraint safe).
            _ensure_unique_names(deals)
            db.replace_deal_folders(conn, client, deals)
            # Stamp the LLM-assist event (even a corroboration-only pass that
            # left every row 'heuristic') so a later run can warn / offer reuse.
            if llm_succeeded and llm_discovery_meta is not None:
                db.record_llm_discovery(
                    conn, client,
                    model=llm_discovery_meta[0], effort=llm_discovery_meta[1],
                    deals=len(deals),
                )
            changed = assign_file_deals(conn, config, client, deals)
            low = sum(1 for d in deals if d.confidence < config.deal_discovery.review_confidence)
            log_event(
                logger, "deal discovery", client=client, deals=len(deals),
                low_confidence=low, files_reassigned=changed,
                llm_assist=bool(llm_wanted), corrections_applied=len(applied_feedback),
            )
            results[client] = deals
        except Exception as exc:  # noqa: BLE001 — one client never aborts the batch
            logger.exception("deal discovery failed for client %s", client)
            log_event(logger, "deal discovery failed", client=client, error=f"{type(exc).__name__}: {exc}")
    return results
