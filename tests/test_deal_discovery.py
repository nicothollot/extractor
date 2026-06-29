"""Smart deal-folder discovery tests (indexer/deals.py + llm/deal_discovery.py).

Synthetic trees reproduce the layouts observed in the real Ares index sample:
strategy groups above deals, project codenames, deals BELOW period folders
(recurring across periods), _Admin/structural branches that are never deals,
correspondence-only clients, and incomplete clients with no deals at all.
The fixture-compatibility test pins the legacy flat layout: every fixture
client must keep its exact rel[1] deal names so the rest of the suite (and
existing aliases/overrides) keep working. The LLM assist tests use a fake
DealFolder list / a stubbed client — never the real Claude Code CLI.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.indexer.deals import (
    _ensure_unique_names,
    discover_deals,
    merge_llm_deals,
    needs_llm_assist,
    refresh_deals,
)
from pv_extractor.indexer.derive import derive_record
from pv_extractor.models import DealEvidence, DealFolder

PV_ROOT = "\\\\testsrv\\share\\PV"

# One relative path per file, Ares-index shapes condensed.
ARES_LIKE_FILES = [
    # strategy group -> deal -> period -> structural
    r"Ares Management\Direct Lending Investments\Elevate\2025 Q1\Client\Legal\POA.pdf",
    r"Ares Management\Direct Lending Investments\Elevate\2025 Q1\Client\Diligence\DL questions.pdf",
    r"Ares Management\Direct Lending Investments\PDI\2024.3.31\Client\Term Sheet.pdf",
    r"Ares Management\Direct Lending Investments\Cornerstone\(4) 2025\2025.12.31\Client\Revolver.pdf",
    r"Ares Management\Direct Lending Investments\United Talent Agency\(3) 1.31.2025\Client\Closing Set\SPA.pdf",
    # group _Admin branch: 100+ HL report files in the real tree — never a deal
    r"Ares Management\Direct Lending Investments\_Admin\Reports\Client Deliverable\2024\9.30.24\Final Report.pdf",
    r"Ares Management\Direct Lending Investments\_Admin\Info From Client\Old\7.31.20\Questions.pdf",
    # codename wrapper -> deal -> period
    r"Ares Management\Fund Opinion\Project Cobalt\Symplr\9.30.2025\Client\Legal\HealthcareSource.pdf",
    # another group, deal + admin sibling
    r"Ares Management\Special Situations\CPP\(1) 3.31.24\Client\Final Docs\doc.pdf",
    r"Ares Management\Special Situations\CPP\(1) 3.31.24\Analysis\Reference.pdf",
    r"Ares Management\Special Situations\_Admin\EL range of value\Ares ASOF\v1.pdf",
    # deal BELOW the period folders, recurring across three periods
    r"Auldbrass Partners\Investments\12.31.22\LinkSquares\Client\LinkSquares Valuation Memo.pdf",
    r"Auldbrass Partners\Investments\12.31.23\Linksquares\overview.pdf",
    r"Auldbrass Partners\Investments\12.31.24\Linksquares\Linksquares Valuation Memo.pdf",
    # deal below period directly under the client
    r"Axon\12.31.2021\AxonPrime Founder Shares\memo.pdf",
    r"Axon\12.31.2022\AxonPrime Founder Shares\memo v2.pdf",
    # nested period chain: year folder -> date folder -> deal
    r"Anchorage\2017\2017.6.30\PHS\Info from Client\Charterhouse Class B shares\share cert.pdf",
    # correspondence-only client: no deals
    r"Ares Monthly\2025\January2025\From Ares\upload.xlsx",
    r"Ares Monthly\2025\January2025\To Ares\queries.xlsx",
    r"Ares Monthly\Admin\Ares\notes.docx",
    # incomplete client: one stray file in the client root
    r"Ares Infrastructure\placeholder.pdf",
    # same deal name under two different containers -> disambiguated
    r"Claira\PDF Parser\Ares\sample.pdf",
    r"Claira\Prompt\Ares\prompt.txt",
]


def _ingest(paths: list[str], tmp_db: str = ":memory:") -> tuple[sqlite3.Connection, Config]:
    config = Config(pv_root=PV_ROOT, db_path=Path("/tmp/unused.db"), output_dir=Path("/tmp"))
    config.llm.models_path = str(Path(__file__).parent.parent / "config" / "models.yaml")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    records = [
        derive_record(
            f"{PV_ROOT}\\{rel}",
            size_bytes=1000,
            modified_time=datetime(2026, 1, 15, 12, 0, 0),
            config=config,
        )
        for rel in paths
    ]
    db.insert_records(conn, records, 100)
    return conn, config


@pytest.fixture(scope="module")
def ares_like():
    conn, config = _ingest(ARES_LIKE_FILES)
    yield conn, config
    conn.close()


def _names(deals: list[DealFolder]) -> set[str]:
    return {d.name for d in deals}


def test_groups_are_not_deals(ares_like) -> None:
    conn, config = ares_like
    deals = discover_deals(conn, config, "Ares Management")
    assert _names(deals) == {
        "Elevate", "PDI", "Cornerstone", "United Talent Agency", "Symplr", "CPP",
    }


def test_admin_branches_never_become_deals(ares_like) -> None:
    conn, config = ares_like
    for deal in discover_deals(conn, config, "Ares Management"):
        for path in deal.folder_paths:
            assert "_Admin" not in path


def test_codename_wrapper_resolves_to_inner_deal(ares_like) -> None:
    conn, config = ares_like
    deals = {d.name: d for d in discover_deals(conn, config, "Ares Management")}
    assert deals["Symplr"].folder_paths == [
        r"Ares Management\Fund Opinion\Project Cobalt\Symplr"
    ]
    assert "Project Cobalt" not in deals


def test_deal_below_period_merges_across_periods(ares_like) -> None:
    conn, config = ares_like
    deals = discover_deals(conn, config, "Auldbrass Partners")
    assert len(deals) == 1
    deal = deals[0]
    assert deal.name.lower() == "linksquares"
    assert len(deal.folder_paths) == 3
    assert deal.evidence.period_recurrence == 3
    assert deal.confidence >= 0.75  # periods + recurrence bonus + memo files


def test_deal_below_period_directly_under_client(ares_like) -> None:
    conn, config = ares_like
    deals = discover_deals(conn, config, "Axon")
    assert _names(deals) == {"AxonPrime Founder Shares"}
    assert deals[0].evidence.period_recurrence == 2


def test_nested_year_then_date_chain(ares_like) -> None:
    conn, config = ares_like
    deals = discover_deals(conn, config, "Anchorage")
    assert _names(deals) == {"PHS"}


def test_correspondence_and_incomplete_clients_have_no_deals(ares_like) -> None:
    conn, config = ares_like
    assert discover_deals(conn, config, "Ares Monthly") == []
    assert discover_deals(conn, config, "Ares Infrastructure") == []


def test_same_name_under_two_containers_disambiguates(ares_like) -> None:
    conn, config = ares_like
    names = _names(discover_deals(conn, config, "Claira"))
    assert names == {"Ares (PDF Parser)", "Ares (Prompt)"}


# ---------------------------------------------------------------------------
# Phase A.6 hard real-world layouts (in-memory _ingest lists, like ARES_LIKE)
# ---------------------------------------------------------------------------
#
# These layouts are added as in-memory ingest lists rather than as
# build_fixture.py files so that the back-compat pin in
# test_fixture_layout_keeps_legacy_deal_names (which asserts the WHOLE fixture
# tree yields exactly its rel[1] names with zero NULL-deal files) is not
# perturbed. Each layout exercises one of the new discovery branches.


# 1. ADMIN-wrapped deal: an _Admin node that hides a genuine period-bearing
#    neutral deal surfaces that inner deal (admin_container evidence), while the
#    report-only / structural-only / evidence-less admin branches stay dead-ends.
ADMIN_WRAPPED_FILES = [
    # genuine deal buried under _Admin -> must surface as 'Misplaced Co'
    r"Fund Alpha\_Admin\Misplaced Co\12.31.2025\Client\Misplaced Co Valuation Memo.pdf",
    # admin branch holding only HL reports under date folders, reached ONLY
    # through STRUCTURAL nodes (Reports/Client Deliverable) -> never a deal
    r"Fund Alpha\_Admin\Reports\Client Deliverable\2024\9.30.24\Final Report.pdf",
    # an ordinary deal elsewhere under the client (control)
    r"Fund Alpha\Normal Deal\12.31.2025\Client\Normal Deal Valuation Memo.pdf",
]


@pytest.fixture(scope="module")
def admin_wrapped():
    conn, config = _ingest(ADMIN_WRAPPED_FILES)
    yield conn, config
    conn.close()


def test_admin_wrapped_deal_surfaces_inner_deal(admin_wrapped) -> None:
    conn, config = admin_wrapped
    deals = {d.name: d for d in discover_deals(conn, config, "Fund Alpha")}
    assert set(deals) == {"Misplaced Co", "Normal Deal"}
    misplaced = deals["Misplaced Co"]
    assert misplaced.folder_paths == [r"Fund Alpha\_Admin\Misplaced Co"]
    assert misplaced.evidence.admin_container is True
    # admin_container weight applied as a mild penalty in the breakdown
    assert misplaced.evidence.components["admin_container"] == pytest.approx(
        config.deal_discovery.weights.admin_container
    )
    # the ordinary deal carries no admin marker
    assert deals["Normal Deal"].evidence.admin_container is False


def test_admin_report_only_branches_never_become_deals(admin_wrapped) -> None:
    conn, config = admin_wrapped
    deals = discover_deals(conn, config, "Fund Alpha")
    # the only admin path that surfaces is the genuine buried deal
    admin_paths = [p for d in deals for p in d.folder_paths if "_Admin" in p]
    assert admin_paths == [r"Fund Alpha\_Admin\Misplaced Co"]
    # the report-only admin branch (reached only through structural nodes)
    # contributes nothing
    names = _names(deals)
    assert "Reports" not in names
    assert "Client Deliverable" not in names


# 2. Shared mixed-investment bucket: one neutral folder DIRECTLY holding memo
#    files for several distinct investments -> one synthetic deal per cluster.
SHARED_BUCKET_FILES = [
    r"Fund Beta\Shared Investments\Acme Valuation Memo.pdf",
    r"Fund Beta\Shared Investments\Acme Valuation Memo vf.pdf",  # same investment, version variant
    r"Fund Beta\Shared Investments\Beta Industries Valuation Memo.pdf",
    r"Fund Beta\Shared Investments\Gamma Holdings IC Memo.pdf",
    r"Fund Beta\Shared Investments\Zeta Random Notes.pdf",  # no memo keyword / empty asset key
]

# A folder whose memos all name the SAME investment must NOT be a bucket.
COHERENT_CLUSTER_FILES = [
    r"Fund Gamma\Helios Folder\Helios Valuation Memo.pdf",
    r"Fund Gamma\Helios Folder\Helios Valuation Memo v2.pdf",
    r"Fund Gamma\Helios Folder\Helios Portfolio Review.pdf",
]


@pytest.fixture(scope="module")
def shared_bucket():
    conn, config = _ingest(SHARED_BUCKET_FILES)
    yield conn, config
    conn.close()


def test_shared_bucket_emits_one_deal_per_cluster(shared_bucket) -> None:
    conn, config = shared_bucket
    deals = {d.name: d for d in discover_deals(conn, config, "Fund Beta")}
    assert set(deals) == {"Acme", "Beta Industries", "Gamma Holdings"}
    bucket_path = r"Fund Beta\Shared Investments"
    for name, deal in deals.items():
        assert deal.evidence.shared_bucket is True
        assert deal.folder_paths == [bucket_path]  # all clusters point at the one folder
        assert deal.evidence.name_filter  # each cluster carries representative tokens
        assert "shared_bucket" in deal.evidence.components
        # verified confidence model: memo_keyword + any_files + flat_default + shared_bucket
        assert deal.confidence == pytest.approx(0.70)
    assert deals["Acme"].evidence.name_filter == ["acme"]
    assert deals["Beta Industries"].evidence.name_filter == ["beta", "industries"]
    assert deals["Gamma Holdings"].evidence.name_filter == ["gamma", "holdings"]


def test_shared_bucket_splits_files_across_clusters(shared_bucket) -> None:
    conn, config = shared_bucket
    refresh_deals(conn, config, ["Fund Beta"])
    by_file = {
        row["file_name"]: row["deal"]
        for row in conn.execute(
            "SELECT file_name, deal FROM files WHERE client = 'Fund Beta'"
        )
    }
    assert by_file["Acme Valuation Memo.pdf"] == "Acme"
    assert by_file["Acme Valuation Memo vf.pdf"] == "Acme"
    assert by_file["Beta Industries Valuation Memo.pdf"] == "Beta Industries"
    assert by_file["Gamma Holdings IC Memo.pdf"] == "Gamma Holdings"
    # nothing silent: an unmatchable file in the bucket gets deal=NULL, never a guess
    assert by_file["Zeta Random Notes.pdf"] is None


def test_coherent_single_cluster_is_not_a_bucket() -> None:
    conn, config = _ingest(COHERENT_CLUSTER_FILES)
    try:
        deals = discover_deals(conn, config, "Fund Gamma")
        assert len(deals) == 1
        assert deals[0].evidence.shared_bucket is False
        assert deals[0].folder_paths == [r"Fund Gamma\Helios Folder"]
    finally:
        conn.close()


def test_shared_bucket_gate_restores_single_deal() -> None:
    conn, config = _ingest(SHARED_BUCKET_FILES)
    try:
        config.deal_discovery.shared_bucket_enabled = False
        deals = discover_deals(conn, config, "Fund Beta")
        assert len(deals) == 1  # the folder collapses to one ordinary deal
        assert deals[0].name == "Shared Investments"
        assert deals[0].evidence.shared_bucket is False
    finally:
        conn.close()


# Coverage guard: a qualifying bucket may also carry a STRUCTURAL subfolder
# (e.g. Client\) holding more memos. Those must be routed to their cluster, not
# silently orphaned to NULL (nothing-silent rule).
SHARED_BUCKET_WITH_SUBFOLDER_FILES = [
    r"Fund Epsilon\Mixed Bag\Acme Valuation Memo.pdf",
    r"Fund Epsilon\Mixed Bag\Beta Industries Valuation Memo.pdf",
    r"Fund Epsilon\Mixed Bag\Client\Acme Valuation Memo Final.pdf",  # one level below
]


def test_shared_bucket_assigns_memos_in_structural_subfolder() -> None:
    conn, config = _ingest(SHARED_BUCKET_WITH_SUBFOLDER_FILES)
    try:
        deals = {d.name: d for d in discover_deals(conn, config, "Fund Epsilon")}
        assert {"Acme", "Beta Industries"} <= set(deals)
        assert all(d.evidence.shared_bucket for d in deals.values())
        refresh_deals(conn, config, ["Fund Epsilon"])
        by_file = {
            row["file_name"]: row["deal"]
            for row in conn.execute(
                "SELECT file_name, deal FROM files WHERE client = 'Fund Epsilon'"
            )
        }
        # the memo one structural level below the bucket resolves to its cluster
        assert by_file["Acme Valuation Memo Final.pdf"] == "Acme"
        assert by_file["Acme Valuation Memo.pdf"] == "Acme"
        assert by_file["Beta Industries Valuation Memo.pdf"] == "Beta Industries"
    finally:
        conn.close()


# 3. Multi-folder deal: the same investment documented in several places merges
#    into ONE DealFolder carrying every folder path.
MULTI_FOLDER_FILES = [
    r"Fund Delta\Investments\12.31.23\Helios\Client\Helios Valuation Memo.pdf",
    r"Fund Delta\Investments\12.31.24\Helios\Client\Helios Valuation Memo.pdf",
    r"Fund Delta\Investments\12.31.25\Helios\Client\Helios Valuation Memo.pdf",
]


def test_multi_folder_deal_unions_paths() -> None:
    conn, config = _ingest(MULTI_FOLDER_FILES)
    try:
        deals = discover_deals(conn, config, "Fund Delta")
        assert _names(deals) == {"Helios"}
        deal = deals[0]
        assert len(deal.folder_paths) == 3
        assert deal.folder_paths == sorted(deal.folder_paths)  # de-duped + sorted
        assert deal.evidence.period_recurrence == 3
    finally:
        conn.close()


# 4. Codename / grouping wrapper extension: a pure-grouping-token wrapper around
#    one neutral deal resolves to the inner deal (not the wrapper name).
GROUPING_WRAPPER_FILES = [
    # pure-grouping wrapper 'Opportunities' around a single deal
    r"Fund Eps\Opportunities\Northstar\12.31.2025\Client\Northstar Valuation Memo.pdf",
    # strategy group: two period-bearing neutral children under a grouping name
    r"Fund Eps\Direct Lending\Polaris\3.31.2025\Client\Polaris Valuation Memo.pdf",
    r"Fund Eps\Direct Lending\Vega\3.31.2025\Client\Vega Valuation Memo.pdf",
]


def test_grouping_wrapper_resolves_to_inner_deals() -> None:
    conn, config = _ingest(GROUPING_WRAPPER_FILES)
    try:
        deals = {d.name: d for d in discover_deals(conn, config, "Fund Eps")}
        assert set(deals) == {"Northstar", "Polaris", "Vega"}
        # the grouping-token wrappers themselves never become deals
        assert "Opportunities" not in deals
        assert "Direct Lending" not in deals
        assert deals["Northstar"].folder_paths == [
            r"Fund Eps\Opportunities\Northstar"
        ]
    finally:
        conn.close()


# Name + embedded-date folders: an investment whose periods are encoded in the
# folder NAME ('PBC (12.31.2023)', 'PBC (22. 8.31.2024)', 'PBC (1. 2022.11.30)')
# is ONE deal 'PBC' observed at three periods — not three separate deals — and a
# generically-named bucket ('Research (2020.10.31)') is never a deal at all.
NAME_DATE_FILES = [
    r"Beacon Capital\PBC (12.31.2023)\PBC Valuation Memo.pdf",
    r"Beacon Capital\PBC (22. 8.31.2024)\PBC Valuation Memo.pdf",
    r"Beacon Capital\PBC (1. 2022.11.30)\PBC Valuation Memo.pdf",
    r"Beacon Capital\Research (2020.10.31)\market overview.pdf",
    r"Beacon Capital\Prior Period Reports\old summary.pdf",
    # a clean control deal with no embedded date
    r"Beacon Capital\Helios Therapeutics\12.31.2024\Helios Valuation Memo.pdf",
]


def test_name_with_embedded_date_merges_into_one_deal() -> None:
    conn, config = _ingest(NAME_DATE_FILES)
    try:
        deals = {d.name: d for d in discover_deals(conn, config, "Beacon Capital")}
        # PBC collapses to ONE deal spanning its three date-stamped folders.
        assert "PBC" in deals
        pbc = deals["PBC"]
        assert len(pbc.folder_paths) == 3
        assert set(pbc.folder_paths) == {
            r"Beacon Capital\PBC (12.31.2023)",
            r"Beacon Capital\PBC (22. 8.31.2024)",
            r"Beacon Capital\PBC (1. 2022.11.30)",
        }
        # three distinct embedded periods => multi-period evidence
        assert pbc.evidence.period_children >= 3
        # the control deal still resolves normally
        assert "Helios Therapeutics" in deals
    finally:
        conn.close()


def test_generic_named_folders_are_never_deals() -> None:
    conn, config = _ingest(NAME_DATE_FILES)
    try:
        names = _names(discover_deals(conn, config, "Beacon Capital"))
        # generic buckets are excluded outright, with or without an embedded date
        assert "Research" not in names
        assert "Prior Period Reports" not in names
        assert all("research" not in n.lower() for n in names)
    finally:
        conn.close()


def test_generic_exclusion_can_be_disabled() -> None:
    conn, config = _ingest(NAME_DATE_FILES)
    config.deal_discovery.exclude_generic_deal_names = False
    try:
        names = _names(discover_deals(conn, config, "Beacon Capital"))
        assert "Research" in names  # legacy behavior when the gate is off
    finally:
        conn.close()


def test_refresh_persists_and_reassigns_files(ares_like) -> None:
    conn, config = ares_like
    results = refresh_deals(conn, config, ["Ares Management", "Auldbrass Partners"])
    assert _names(results["Ares Management"]) >= {"Elevate", "Symplr"}
    # files.deal rewritten: group folder names are gone, admin files are NULL
    deals = db.deals_for_client(conn, "Ares Management")
    assert "Direct Lending Investments" not in deals
    assert "Symplr" in deals
    row = conn.execute(
        "SELECT deal FROM files WHERE file_path LIKE ?", ("%_Admin%Final Report.pdf",)
    ).fetchone()
    assert row["deal"] is None
    # the deal-below-period merge maps all three period paths to one deal
    rows = conn.execute(
        "SELECT DISTINCT deal FROM files WHERE client = 'Auldbrass Partners' AND deal IS NOT NULL"
    ).fetchall()
    assert len(rows) == 1
    # round-trip through the table
    stored = db.deal_folders_for_client(conn, "Auldbrass Partners")
    assert len(stored[0].folder_paths) == 3
    assert stored[0].evidence.period_recurrence == 3


def test_disabled_discovery_is_a_no_op(ares_like) -> None:
    conn, config = ares_like
    config2 = config.model_copy(deep=True)
    config2.deal_discovery.enabled = False
    assert refresh_deals(conn, config2, ["Axon"]) == {}


def test_fixture_layout_keeps_legacy_deal_names(fixture_pv_root) -> None:
    """Back-compat pin: the flat client\\deal\\period fixture must yield
    EXACTLY the legacy rel[1] names, or the locator e2e expectations,
    aliases.yaml and recorded overrides would all silently break."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from fixtures.build_fixture import FIXTURE_FILES

    from pv_extractor.indexer.scan_tree import scan_tree

    config = Config(
        pv_root=str(fixture_pv_root), db_path=Path("/tmp/unused.db"), output_dir=Path("/tmp")
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    scan_tree(conn, str(fixture_pv_root), config)

    legacy: dict[str, set[str]] = {}
    for rel, _, _ in FIXTURE_FILES:
        segs = rel.split("/")
        legacy.setdefault(segs[0], set()).add(segs[1])

    results = refresh_deals(conn, config)
    assert {c: _names(d) for c, d in results.items()} == legacy
    assert conn.execute("SELECT COUNT(*) FROM files WHERE deal IS NULL").fetchone()[0] == 0
    conn.close()


def test_needs_llm_assist_trigger() -> None:
    cfg = Config().deal_discovery
    cfg.llm.enabled = True
    strong = [DealFolder(client="c", name="X", folder_paths=["c\\X"], confidence=0.8)]
    weak = [DealFolder(client="c", name="X", folder_paths=["c\\X"], confidence=0.2)]
    assert needs_llm_assist([], cfg)
    assert needs_llm_assist(weak, cfg)
    assert not needs_llm_assist(strong, cfg)
    cfg.llm.enabled = False
    assert not needs_llm_assist([], cfg)


def test_merge_llm_corroborates_and_fills_gaps() -> None:
    cfg = Config().deal_discovery
    heuristic = [
        DealFolder(client="c", name="Elevate", folder_paths=["c\\G\\Elevate"], confidence=0.6),
    ]
    llm = [
        DealFolder(
            client="c", name="Elevate", folder_paths=["c\\G\\Elevate"], confidence=0.85,
            method="claude-code:sonnet:low", evidence=DealEvidence(llm_corroborated=True),
        ),
        DealFolder(
            client="c", name="NewCo", folder_paths=["c\\G\\NewCo"], confidence=0.65,
            method="claude-code:sonnet:low", evidence=DealEvidence(llm_corroborated=True),
        ),
    ]
    merged = merge_llm_deals(heuristic, llm, cfg)
    by_name = {d.name: d for d in merged}
    assert set(by_name) == {"Elevate", "NewCo"}
    elevate = by_name["Elevate"]
    assert elevate.method == "heuristic"  # deterministic stays primary
    assert elevate.evidence.llm_corroborated
    assert elevate.confidence == pytest.approx(0.95)  # max(0.6, 0.85) + bonus
    assert by_name["NewCo"].method == "claude-code:sonnet:low"


def test_llm_assist_corroboration_only_is_recorded(monkeypatch) -> None:
    """A run that only CORROBORATES heuristic deals leaves every row 'heuristic',
    so the per-deal claude-code method scan misses it. The index_meta stamp must
    still record the LLM-assist event so a re-run can warn / offer reuse."""
    from pv_extractor.indexer import deals as deals_mod

    conn, config = _ingest(ARES_LIKE_FILES)
    try:
        # Heuristic pass first so we know the discovered paths to corroborate.
        heuristic = discover_deals(conn, config, "Ares Management")
        assert heuristic, "fixture should discover deals"
        assert db.last_llm_discovery(conn, "Ares Management") is None

        # Fake LLM that only corroborates existing heuristic deals (no gap-fills),
        # the exact case where no row carries a claude-code method.
        def _fake_llm(conn_, config_, client, *, model=None, effort=None, **_):
            return (
                [
                    DealFolder(
                        client=client, name=d.name, folder_paths=list(d.folder_paths),
                        confidence=d.confidence, method="claude-code:sonnet:medium",
                        evidence=DealEvidence(llm_corroborated=True),
                    )
                    for d in heuristic
                ],
                None,
            )

        monkeypatch.setattr(
            "pv_extractor.llm.deal_discovery.llm_discover_deals", _fake_llm
        )
        refresh_deals(conn, config, ["Ares Management"], use_llm=True)

        # No persisted row carries a claude-code method (corroboration only)...
        persisted = db.deal_folders_for_client(conn, "Ares Management")
        assert persisted and all(p.method == "heuristic" for p in persisted)
        # ...but the LLM-assist event is recorded and surfaced.
        prior = db.last_llm_discovery(conn, "Ares Management")
        assert prior is not None
        assert prior["model"] == "sonnet"
        assert prior["effort"] == "medium"
        assert prior["deals"] == len(persisted)
        assert prior["at"]
    finally:
        conn.close()


def test_llm_discovery_grounds_paths_and_merges_periods(ares_like, monkeypatch) -> None:
    """Answers are grounded against the folder inventory: invented paths are
    discarded; one deal reported under several period paths merges."""
    from pv_extractor.llm import deal_discovery as dd
    from pv_extractor.llm.claude_code_client import ClaudeCodeResult

    conn, config = ares_like

    captured: dict = {}

    class _FakeClient:
        def extract_json(self, **kwargs):
            captured.update(kwargs)
            return ClaudeCodeResult(
                job_id=kwargs["job_id"], ok=True,
                structured={
                    "client_has_no_deals": False,
                    "deals": [
                        {"name": "LinkSquares",
                         "folder_path": r"Auldbrass Partners\Investments\12.31.22\LinkSquares",
                         "confidence": "high"},
                        {"name": "LinkSquares",
                         "folder_path": r"Auldbrass Partners/Investments/12.31.23/Linksquares",
                         "confidence": "medium"},
                        {"name": "Phantom",
                         "folder_path": r"Auldbrass Partners\Invented\Phantom",
                         "confidence": "high"},
                    ],
                },
            )

    deals, error = dd.llm_discover_deals(
        conn, config, "Auldbrass Partners", cc_client=_FakeClient()
    )
    assert error is None
    assert len(deals) == 1  # phantom discarded, two period paths merged
    assert deals[0].name == "LinkSquares"
    assert len(deals[0].folder_paths) == 2
    assert deals[0].confidence == pytest.approx(config.deal_discovery.llm.confidence_map["high"])
    assert deals[0].method.startswith("claude-code:sonnet:")
    # the alias travels to the CLI (floats with Claude Code updates), never a pinned id
    assert captured["model"] == "sonnet"
    assert "FOLDER INVENTORY" in captured["prompt"]
    assert captured["allow_read_tool"] is False


def test_llm_discovery_handles_unknown_model(ares_like) -> None:
    from pv_extractor.llm.deal_discovery import llm_discover_deals

    conn, config = ares_like
    deals, error = llm_discover_deals(conn, config, "Axon", model="no-such-model")
    assert deals == [] and error is not None


# --- regression: (client, deal) uniqueness must hold so deal_folders persistence
#     never crashes with "UNIQUE constraint failed" (a same-name collision that
#     the parent-suffix pass alone could not resolve). ---


def _df(name: str, path: str, conf: float = 0.5) -> DealFolder:
    return DealFolder(client="C", name=name, folder_paths=[path], confidence=conf)


def test_ensure_unique_names_same_name_same_parent() -> None:
    # Two deals with the SAME name AND the SAME parent folder: the parent-suffix
    # pass yields identical names, so the numeric-suffix guarantee must kick in.
    deals = [
        _df("Acme", r"C\Group\Acme", 0.7),
        _df("Acme", r"C\Group\Acme2", 0.6),  # parent "Group" for both
    ]
    _ensure_unique_names(deals)
    names = [d.name for d in deals]
    assert len(set(names)) == 2, names  # no duplicate
    assert "Acme (Group)" in names


def test_ensure_unique_names_three_way_and_suffix_collision() -> None:
    deals = [
        _df("Beta", r"C\X\Beta", 0.9),
        _df("Beta", r"C\X\Beta_b", 0.8),  # same parent "X" as the first
        _df("Beta", r"C\Y\Beta", 0.7),    # different parent "Y"
        _df("Beta (X)", r"C\Z\Beta (X)", 0.6),  # pre-existing collision with the suffixed form
    ]
    _ensure_unique_names(deals)
    names = [d.name for d in deals]
    assert len(set(names)) == len(names), names  # every name distinct


def test_replace_deal_folders_survives_would_be_collisions() -> None:
    # End to end: uniquified deals persist without an IntegrityError on the
    # UNIQUE(client, deal) index (the bug that aborted an Ares Management scan).
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    deals = [_df("Acme", r"C\G\Acme"), _df("Acme", r"C\G\Acme2"), _df("Acme", r"C\G\Acme3")]
    _ensure_unique_names(deals)
    db.replace_deal_folders(conn, "C", deals)  # must NOT raise IntegrityError
    stored = {d.name for d in db.deal_folders_for_client(conn, "C")}
    assert len(stored) == 3
    conn.close()
