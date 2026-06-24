"""D2 model registry: menu loading, alias/id resolution, AUTO/MANUAL routing,
Fable gating, pricing math. No subprocesses, no network."""

from __future__ import annotations

import pytest

from pv_extractor.config import LlmAutoRoutingConfig
from pv_extractor.llm.model_registry import ExtractionPlanMetrics, ModelRegistry, UnknownModelError
from pv_extractor.models import LlmUsage


@pytest.fixture(scope="module")
def registry(project_root) -> ModelRegistry:
    return ModelRegistry.load(project_root / "config" / "models.yaml")


AUTO = LlmAutoRoutingConfig()


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------


def test_seed_menu_aliases_and_ids(registry):
    by_alias = {e.alias: e for e in registry.entries}
    assert {"fable", "opus", "sonnet", "haiku", "provider-default"} <= set(by_alias)
    assert by_alias["fable"].id == "claude-fable-5"
    assert by_alias["opus"].id == "claude-opus-4-8"
    assert by_alias["sonnet"].id == "claude-sonnet-4-6"
    assert by_alias["haiku"].id == "claude-haiku-4-5"
    assert all(e.latest_alias and not e.pinned for e in registry.entries if e.provider == "claude")
    assert by_alias["fable"].requires_explicit_enable is True
    assert not any(
        e.requires_explicit_enable for e in registry.entries if e.alias != "fable"
    )
    assert registry.menu.last_reviewed  # GUI/CLI surface "Last reviewed"


def test_seed_pricing(registry):
    expected = {
        "fable": (10.0, 50.0, 1.0, 12.5, 20.0),
        "opus": (5.0, 25.0, 0.5, 6.25, 10.0),
        "sonnet": (3.0, 15.0, 0.3, 3.75, 6.0),
        "haiku": (1.0, 5.0, 0.1, 1.25, 2.0),
    }
    for entry in registry.entries:
        if entry.pricing_per_mtok is None:
            continue
        p = entry.pricing_per_mtok
        assert (p.input, p.output, p.cache_hit, p.cache_write_5m, p.cache_write_1h) == expected[entry.alias]


def test_resolve_by_alias_and_full_id(registry):
    assert registry.resolve("sonnet").id == "claude-sonnet-4-6"
    assert registry.resolve("claude-sonnet-4-6").alias == "sonnet"


def test_unknown_model_raises_with_allowed_list(registry):
    with pytest.raises(UnknownModelError) as exc:
        registry.resolve("gpt-12")
    assert "sonnet" in str(exc.value)


def test_cli_model_arg_floats_unless_pinned(registry):
    sonnet = registry.resolve("sonnet")
    assert sonnet.cli_model_arg() == "sonnet"  # latest_alias floats with CLI updates
    pinned = sonnet.model_copy(update={"pinned": True})
    assert pinned.cli_model_arg() == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_auto_normal_extraction_routes_single_sonnet_primary(registry):
    tiers = registry.extraction_tiers(mode="auto", auto=AUTO)
    assert [(t.entry.alias, t.effort, t.reason) for t in tiers] == [("sonnet", "medium", "auto")]


def test_auto_large_image_heavy_deal_uses_opus_whole_deal(registry):
    plan = registry.resolve_extraction_plan(
        routing_mode="auto",
        metrics=ExtractionPlanMetrics(
            estimated_input_tokens=220_000, documents=3, image_pages=40, fields=203
        ),
        auto=AUTO,
    )
    assert (plan.model, plan.effort, plan.execution_shape, plan.expected_primary_calls) == (
        "opus", "high", "deal", 1,
    )


def test_auto_never_selects_fable_without_opt_in(registry):
    tiers = registry.extraction_tiers(mode="auto", auto=AUTO, allow_fable=False)
    assert all(t.entry.alias != "fable" for t in tiers)
    assert len(tiers) == 1


def test_auto_rejects_explicit_only_model_as_routine_tier(registry):
    auto = LlmAutoRoutingConfig(extraction_model="fable")
    with pytest.raises(ValueError, match="explicit"):
        registry.extraction_tiers(mode="auto", auto=auto)


def test_manual_forces_single_pass(registry):
    tiers = registry.extraction_tiers(
        mode="manual", auto=AUTO, manual_model="sonnet", manual_effort="low"
    )
    assert [(t.entry.alias, t.effort, t.reason) for t in tiers] == [("sonnet", "low", "single_model")]


def test_manual_sonnet_stays_sonnet_even_for_ocr_hostile(registry):
    """Manual means EVERYTHING uses the chosen model. OCR-hostile docs force
    opus under AUTO, but must NOT escalate off the manual model — one sonnet
    tier, no opus anywhere."""
    tiers = registry.extraction_tiers(
        mode="manual", auto=AUTO, manual_model="sonnet", manual_effort="low",
        ocr_hostile=True,
    )
    assert [t.entry.alias for t in tiers] == ["sonnet"]


def test_manual_naming_fable_is_explicit_enablement(registry):
    tiers = registry.extraction_tiers(
        mode="manual", auto=AUTO, manual_model="fable", manual_effort="high"
    )
    assert [(t.entry.alias, t.effort) for t in tiers] == [("fable", "high")]


def test_sonnet_profile_can_choose_deal_or_document_shape(registry):
    small = registry.resolve_extraction_plan(
        routing_mode="single_model",
        metrics=ExtractionPlanMetrics(estimated_input_tokens=84_500, documents=2, image_pages=4, fields=203),
        auto=AUTO,
        manual_model="sonnet",
        manual_effort="medium",
    )
    assert small.execution_shape == "deal"
    larger = registry.resolve_extraction_plan(
        routing_mode="single_model",
        metrics=ExtractionPlanMetrics(estimated_input_tokens=120_000, documents=3, image_pages=4, fields=203),
        auto=AUTO,
        manual_model="sonnet",
        manual_effort="medium",
    )
    assert larger.execution_shape == "document"


def test_manual_unknown_model_and_bad_effort_raise(registry):
    with pytest.raises(UnknownModelError):
        registry.extraction_tiers(mode="manual", auto=AUTO, manual_model="nope")
    with pytest.raises(ValueError, match="effort"):
        registry.extraction_tiers(
            mode="manual", auto=AUTO, manual_model="sonnet", manual_effort="turbo"
        )


def test_non_claude_provider_uses_cli_default_when_manual_model_is_claude_default(registry):
    tiers = registry.extraction_tiers(
        mode="manual",
        auto=AUTO,
        manual_model="sonnet",
        manual_effort="high",
        provider="codex",
        provider_default_model=None,
        provider_default_effort="high",
    )
    assert [(t.entry.provider, t.entry.alias, t.entry.id, t.effort) for t in tiers] == [
        ("codex", "provider-default", "", "high")
    ]


def test_classification_tier_is_haiku(registry):
    selection = registry.select_classification(AUTO)
    assert (selection.entry.alias, selection.effort) == ("haiku", "low")


# ---------------------------------------------------------------------------
# pricing math
# ---------------------------------------------------------------------------


def test_cost_usd_per_component(registry):
    sonnet = registry.resolve("sonnet")
    usage = LlmUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert registry.cost_usd(usage, sonnet) == pytest.approx(18.0)
    usage = LlmUsage(
        input_tokens=100_000, output_tokens=10_000,
        cache_read_input_tokens=1_000_000, cache_creation_input_tokens=100_000,
    )
    # 0.3 + 0.15 + 0.30 + 0.375
    assert registry.cost_usd(usage, sonnet) == pytest.approx(1.125)
