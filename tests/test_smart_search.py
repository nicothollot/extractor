"""Smart Search (Phase B) tests — the rule engine is PRIMARY and fully usable
with the LLM OFF; the optional Claude Code CLI fallback only augments and never
blocks; ranking + learning are deterministic; builtin DocTypeSpecs routed
through the locator reproduce the legacy doc-type behavior byte-for-byte.

The CLI fallback is exercised exclusively through tests/fixtures/fake_claude.py
(an injected cc_client) — NO test launches the real Claude Code CLI.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from fixtures.fake_claude import FakeClaudeCodeClient

from pv_extractor.config import ClientConfig, Config, load_config
from pv_extractor.indexer import db
from pv_extractor.indexer.db import init_schema, open_db
from pv_extractor.indexer.derive import derive_record
from pv_extractor.indexer.scan_tree import scan_tree
from pv_extractor.locator.locate import locate
from pv_extractor.models import DocType, DocTypeSpec, LocateQuery, ResolutionStatus
from pv_extractor.search import doc_type_spec as profiles
from pv_extractor.search import intent, rank

PV_ROOT = "\\\\testsrv\\share\\PV"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _config() -> Config:
    config = Config(pv_root=PV_ROOT, db_path=Path("/tmp/unused.db"), output_dir=Path("/tmp"))
    config.llm.models_path = str(Path(__file__).parent.parent / "config" / "models.yaml")
    return config


def _ingest(paths: list[str]) -> tuple[sqlite3.Connection, Config]:
    """Build an in-memory index from relative paths (the deal-discovery idiom)."""
    config = _config()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
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


# A small SEC-filings-style tree: 10-Q quarterlies + 10-K annuals + noise.
FILINGS_FILES = [
    r"Acme Capital\Globex\Filings\Globex 10-Q Q1 2026.pdf",
    r"Acme Capital\Globex\Filings\Globex 10-Q Q2 2026.pdf",
    r"Acme Capital\Globex\Filings\Globex Quarterly Report Q3 2026.pdf",
    r"Acme Capital\Globex\Filings\Globex 10-K Annual Report 2025.pdf",
    r"Acme Capital\Globex\Client\Globex Valuation Memo Q1 2026.pdf",
    r"Acme Capital\Globex\Client\Globex NDA.pdf",
    r"Acme Capital\Globex\Legal\Globex Cap Table.xlsx",
    r"Acme Capital\Globex\Client\Globex Audited Financial Statements 2025.pdf",
    r"Acme Capital\Globex\Client\Globex random notes.docx",
]


# ---------------------------------------------------------------------------
# 1. HEADLINE — NO-LLM rule resolution (the feature must work with LLM off)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expect_include", "expect_folder"),
    [
        ("quarterly reports", "quarterly", "quarterly"),
        ("quarterly report", "quarterly", "filings"),
        ("annual report", "annual", "annual"),
        ("cap table", "cap table", "legal"),
        ("audited financials", "audited", "audit"),
        ("valuation memo", "valuation memo", None),
    ],
)
def test_rule_resolution_no_cli(query, expect_include, expect_folder) -> None:
    """resolve_intent with use_cli=False yields a sensible spec, provenance
    'rules', and ZERO CLI involvement. THIS proves the no-LLM core."""
    config = _config()
    config.smart_search.use_cli_fallback = False  # config default also OFF for this call
    spec, provenance = intent.resolve_intent(query, config, use_cli=False)

    assert provenance == "rules"
    assert isinstance(spec, DocTypeSpec)
    assert spec.slug  # non-empty stable slug
    assert any(expect_include in inc for inc in spec.filename_include), spec.filename_include
    if expect_folder is not None:
        assert any(expect_folder in f for f in spec.folder_include), spec.folder_include
    # period-optional for free-text search (so a bare query never zeroes results)
    assert spec.period_required is False


def test_rule_resolution_config_default_off_means_no_cli() -> None:
    """With config.smart_search.use_cli_fallback=False and no use_cli override,
    the CLI is never consulted and provenance stays 'rules'."""
    config = _config()
    config.smart_search.use_cli_fallback = False
    # Inject a cc_client that, if ever called, would flip provenance — proving
    # it is NOT consulted when the fallback is off.
    spy = FakeClaudeCodeClient(mode="intent", intent_anchors={"filename_include": ["SHOULD-NOT-APPEAR"]})
    spec, provenance = intent.resolve_intent("quarterly reports", config, cc_client=spy)

    assert provenance == "rules"
    assert spy.calls == []
    assert all("SHOULD-NOT-APPEAR" not in inc for inc in spec.filename_include)


def test_rule_resolution_unknown_query_degrades_to_tokens() -> None:
    """An unknown query still yields a non-empty spec (its own tokens become
    the includes) — never an empty/exception result."""
    config = _config()
    spec, provenance = intent.resolve_intent("zorblax frobnitz dossier", config, use_cli=False)
    assert provenance == "rules"
    assert spec.filename_include  # tokens, not empty
    assert "zorblax" in spec.filename_include


# ---------------------------------------------------------------------------
# 2. GRACEFUL DEGRADATION — CLI unavailable / CLI errors -> rule-only spec
# ---------------------------------------------------------------------------


def test_degrades_when_no_binary() -> None:
    """use_cli=True but the client reports no binary (and no command_args
    bridge) -> the CLI is skipped, provenance 'rules', spec still useful."""
    config = _config()
    no_binary = FakeClaudeCodeClient(mode="intent", binary=None)
    spec, provenance = intent.resolve_intent(
        "quarterly reports", config, use_cli=True, cc_client=no_binary
    )
    assert provenance == "rules"
    assert no_binary.calls == []  # never even attempted the call
    assert any("quarterly" in inc for inc in spec.filename_include)


def test_degrades_when_cli_raises() -> None:
    """An injected client whose extract_json RAISES never propagates — the
    robustness contract: rule-only spec, provenance 'rules'."""
    config = _config()

    class Boom(FakeClaudeCodeClient):
        def extract_json(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("simulated CLI explosion")

    spec, provenance = intent.resolve_intent(
        "annual report", config, use_cli=True, cc_client=Boom(mode="intent")
    )
    assert provenance == "rules"
    assert any("annual" in inc for inc in spec.filename_include)


def test_degrades_when_cli_malformed() -> None:
    """An injected client returning a non-ok / malformed result -> rule-only
    spec, provenance 'rules', no exception."""
    config = _config()
    bad = FakeClaudeCodeClient(mode="intent", behaviors=["malformed"])
    spec, provenance = intent.resolve_intent(
        "cap table", config, use_cli=True, cc_client=bad
    )
    assert provenance == "rules"
    assert any("cap table" in inc for inc in spec.filename_include)


# ---------------------------------------------------------------------------
# 3. CLI fallback happy path — merges extra anchors, provenance 'rules+cli'
# ---------------------------------------------------------------------------


def test_cli_augment_merges_anchors() -> None:
    """A valid fake DocTypeSpec response UNION-merges its synonyms / folder
    anchors into the rule spec; provenance becomes 'rules+cli'."""
    config = _config()
    fake = FakeClaudeCodeClient(
        mode="intent",
        intent_anchors={
            "filename_include": ["10 q", "form 10q", "interim report"],
            "folder_include": ["sec filings"],
            "extensions": [".htm"],
        },
    )
    spec, provenance = intent.resolve_intent(
        "quarterly reports", config, use_cli=True, cc_client=fake
    )
    assert provenance == "rules+cli"
    assert len(fake.calls) == 1
    # rule anchors preserved AND cli anchors added (union merge)
    assert "quarterly" in spec.filename_include  # from the rule layer
    assert "form 10q" in spec.filename_include   # from the cli layer
    assert "interim report" in spec.filename_include
    assert "sec filings" in spec.folder_include
    assert ".htm" in spec.extensions


def test_cli_augment_no_new_anchors_stays_rules() -> None:
    """If the CLI returns only anchors already present, nothing changed ->
    provenance falls back to 'rules' (it can only ADD)."""
    config = _config()
    # Echo back anchors the rule layer already produced for "cap table".
    echo = FakeClaudeCodeClient(
        mode="intent", intent_anchors={"filename_include": ["cap table"]}
    )
    spec, provenance = intent.resolve_intent(
        "cap table", config, use_cli=True, cc_client=echo
    )
    assert provenance == "rules"
    assert "cap table" in spec.filename_include


# ---------------------------------------------------------------------------
# 4. RANKING on a fixture tree — right files above noise, deterministic
# ---------------------------------------------------------------------------


def test_rank_quarterly_above_noise() -> None:
    conn, config = _ingest(FILINGS_FILES)
    spec, _ = intent.resolve_intent("quarterly report", config, use_cli=False)
    # generous pool: keep everything (min_score 0) so ordering is fully visible
    config.smart_search.min_score = 0.0
    results = rank.rank_files(conn, config, spec)

    names = [r["file_name"] for r in results]
    # the three quarterly-ish files rank above the valuation memo / NDA noise
    top3 = set(names[:3])
    assert "Globex 10-Q Q1 2026.pdf" in top3
    assert "Globex 10-Q Q2 2026.pdf" in top3
    assert "Globex Quarterly Report Q3 2026.pdf" in top3
    assert "Globex NDA.pdf" not in top3
    # every result carries an inspectable components dict
    assert all("lexical_relevance" in r["components"] for r in results)
    conn.close()


def test_rank_tolerates_invalid_regex() -> None:
    # The optional CLI augmentation can suggest an uncompilable pattern; ranking
    # must skip it, never crash (nothing silent: it just doesn't match).
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = 0.0
    spec = DocTypeSpec(
        slug="bad-regex", label="Bad Regex",
        filename_include=["quarterly"], filename_regex=["10[- ?q", "(unclosed"],
    )
    results = rank.rank_files(conn, config, spec)  # must not raise
    assert isinstance(results, list)
    conn.close()


def test_rank_is_deterministic() -> None:
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = 0.0
    spec, _ = intent.resolve_intent("quarterly report", config, use_cli=False)
    first = rank.rank_files(conn, config, spec)
    second = rank.rank_files(conn, config, spec)
    assert [r["file_path"] for r in first] == [r["file_path"] for r in second]
    assert [r["score"] for r in first] == [r["score"] for r in second]
    conn.close()


def test_rank_limit_and_min_score_honored() -> None:
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = 0.0
    spec, _ = intent.resolve_intent("quarterly report", config, use_cli=False)
    capped = rank.rank_files(conn, config, spec, limit=2)
    assert len(capped) == 2

    # a high floor drops everything
    config.smart_search.min_score = 999.0
    assert rank.rank_files(conn, config, spec) == []
    conn.close()


def test_rank_period_required_penalizes_missing_period() -> None:
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = -999.0  # keep penalized rows so we can inspect
    spec, _ = intent.resolve_intent("quarterly report", config, use_cli=False)
    spec.period_required = True

    target = date(2026, 3, 31)  # Q1 2026 — only the Q1 file carries this period
    results = rank.rank_files(conn, config, spec, target_as_of=target)
    by_name = {r["file_name"]: r for r in results}
    q1 = by_name["Globex 10-Q Q1 2026.pdf"]
    q2 = by_name["Globex 10-Q Q2 2026.pdf"]
    # the matching-period file earns the period bonus; the off-period file is penalized
    assert q1["components"]["period_evidence"] > 0
    assert q2["components"]["period_evidence"] < 0
    assert q1["score"] > q2["score"]
    conn.close()


def test_rank_audited_financials_query() -> None:
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = 0.0
    spec, _ = intent.resolve_intent("audited financials", config, use_cli=False)
    results = rank.rank_files(conn, config, spec)
    assert results
    assert results[0]["file_name"] == "Globex Audited Financial Statements 2025.pdf"
    conn.close()


# ---------------------------------------------------------------------------
# 5. LEARNING — feedback shifts ranking deterministically
# ---------------------------------------------------------------------------


def test_feedback_label_validation() -> None:
    conn, config = _ingest(FILINGS_FILES)
    with pytest.raises(ValueError):
        rank.record_search_feedback(
            conn, profile_slug="s", file_path="x", label=0, context=None
        )
    conn.close()


def test_learning_reranks_deterministically() -> None:
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.min_score = 0.0
    config.smart_search.learning_weight = 5.0  # make the nudge clearly visible
    spec, _ = intent.resolve_intent("quarterly report", config, use_cli=False)
    profiles.save_profile(conn, spec, query_seed="quarterly report")

    q1_path = f"{PV_ROOT}\\Acme Capital\\Globex\\Filings\\Globex 10-Q Q1 2026.pdf"
    q2_path = f"{PV_ROOT}\\Acme Capital\\Globex\\Filings\\Globex 10-Q Q2 2026.pdf"

    base = {r["file_path"]: r["score"] for r in rank.rank_files(conn, config, spec)}

    # accept Q1's distinctive tokens, reject Q2's
    rank.record_search_feedback(conn, profile_slug=spec.slug, file_path=q1_path, label=1, context=None)
    rank.record_search_feedback(conn, profile_slug=spec.slug, file_path=q2_path, label=-1, context=None)

    overrides = rank.effective_weight_overrides(conn, spec, config)
    learn_keys = [k for k in overrides if k.startswith("learn:")]
    assert learn_keys, overrides  # learning produced token nudges
    # 'q1' earns a positive nudge, 'q2' a negative one
    assert overrides.get("learn:q1", 0.0) > 0
    assert overrides.get("learn:q2", 0.0) < 0

    after = {r["file_path"]: r["score"] for r in rank.rank_files(conn, config, spec)}
    assert after[q1_path] > base[q1_path]   # accepted file rose
    assert after[q2_path] < base[q2_path]   # rejected file fell
    conn.close()


def test_feedback_on_non_stored_slug_returns_learn_only() -> None:
    """effective_weight_overrides against a bare DocTypeSpec (no stored row)
    returns ONLY the freshly learned learn:* nudges — mirrors the API path for
    feedback on an inline-spec preview's intent slug."""
    conn, config = _ingest(FILINGS_FILES)
    config.smart_search.learning_weight = 1.0
    q1_path = f"{PV_ROOT}\\Acme Capital\\Globex\\Filings\\Globex 10-Q Q1 2026.pdf"
    rank.record_search_feedback(conn, profile_slug="ephemeral-slug", file_path=q1_path, label=1, context=None)

    bare = DocTypeSpec(slug="ephemeral-slug", label="ephemeral-slug")
    overrides = rank.effective_weight_overrides(conn, bare, config)
    assert overrides  # non-empty
    assert all(k.startswith("learn:") for k in overrides)
    conn.close()


# ---------------------------------------------------------------------------
# profile CRUD + builtins (B1 doc_type_spec contracts)
# ---------------------------------------------------------------------------


def test_profile_crud_and_builtin_guard() -> None:
    conn, config = _ingest(FILINGS_FILES)
    profiles.seed_builtins(conn, config)

    listed = {s.slug for s in profiles.list_profiles(conn)}
    for builtin in ("valuation_memo", "ic_memo", "portfolio_review", "any_client_valuation_doc"):
        assert builtin in listed

    # save + read back a learned profile
    spec = DocTypeSpec(slug="my-quarterly", label="My Quarterly", filename_include=["quarterly"])
    profiles.save_profile(conn, spec, query_seed="quarterly")
    got = profiles.get_profile(conn, "my-quarterly")
    assert got is not None and got.filename_include == ["quarterly"]

    # delete a learned profile -> True; deleting a builtin -> False
    assert profiles.delete_profile(conn, "my-quarterly") is True
    assert profiles.get_profile(conn, "my-quarterly") is None
    assert profiles.delete_profile(conn, "valuation_memo") is False
    assert profiles.delete_profile(conn, "nonexistent") is False
    conn.close()


def test_resolve_spec_builtin_and_slug() -> None:
    conn, config = _ingest(FILINGS_FILES)
    # builtin enum value resolves live from config.locator.doc_type_keywords
    vm = profiles.resolve_spec(conn, "valuation_memo", config)
    assert vm is not None
    assert vm.filename_include == config.locator.doc_type_keywords["valuation_memo"]
    assert vm.filename_exclude == list(config.locator.negative_keywords)

    # learned slug resolves to the stored spec; unknown slug -> None
    profiles.save_profile(conn, DocTypeSpec(slug="learned", label="L"))
    assert profiles.resolve_spec(conn, "learned", config) is not None
    assert profiles.resolve_spec(conn, "no-such-slug", config) is None
    conn.close()


# ---------------------------------------------------------------------------
# 6. BUILTIN parity through the locator (B2 integration == legacy DocType path)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def locator_env(fixture_pv_root, tmp_path_factory):
    """The real fixture tree scanned into an index, repo-default locator config."""
    project_root = Path(__file__).resolve().parent.parent
    out = tmp_path_factory.mktemp("smart_search_locator")
    config = load_config(project_root / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = out
    config.db_path = out / "pv_index.db"
    config.clients = {
        "default": ClientConfig(period_style="quarterly_calendar"),
        "Angelo Gordon": ClientConfig(period_style="monthly"),
    }
    conn = open_db(config.db_path, config.pv_root)
    init_schema(conn)
    scan_tree(conn, str(fixture_pv_root), config)
    yield conn, config
    conn.close()


@pytest.mark.parametrize(
    ("client", "deal", "period", "doc_type"),
    [
        ("Angelo Gordon", "Accell", "2025-01-31", DocType.valuation_memo),
        ("Angelo Gordon", "TDW", "2024-09-30", DocType.valuation_memo),
        ("Apollo Global Management", "Hyperoptic", "Q1 2026", DocType.ic_memo),
        ("Apollo Global Management", "AIOF II / ANRP III", "Q1 2026", DocType.portfolio_review),
        ("Angelo Gordon", "Accell", "2024-11-30", DocType.any_client_valuation_doc),
    ],
)
def test_builtin_spec_matches_legacy_locator(locator_env, client, deal, period, doc_type) -> None:
    """locate(..., doc_type_spec=<builtin spec>) selects the SAME file as the
    legacy builtin DocType enum path — proving B2 integration matches builtins."""
    conn, config = locator_env

    legacy = locate(
        conn, config,
        LocateQuery(client=client, deal=deal, period=period, doc_type=doc_type),
    )
    assert legacy.status == ResolutionStatus.FOUND
    assert legacy.winner is not None

    spec = profiles.resolve_spec(conn, doc_type.value, config)
    assert spec is not None
    via_spec = locate(
        conn, config,
        LocateQuery(client=client, deal=deal, period=period, doc_type=doc_type),
        doc_type_spec=spec,
    )
    assert via_spec.status == ResolutionStatus.FOUND
    assert via_spec.winner is not None
    assert via_spec.winner.record.file_path == legacy.winner.record.file_path
    # the doc-type/negative component magnitudes match the legacy path too
    assert via_spec.winner.breakdown.doctype_score == legacy.winner.breakdown.doctype_score
    assert via_spec.winner.breakdown.negative_score == legacy.winner.breakdown.negative_score
