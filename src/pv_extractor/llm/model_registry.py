"""Model menu + router (D2): loads config/models.yaml, validates aliases/ids,
holds the editable price-per-1M-token assumptions, and resolves AUTO/MANUAL
model selection into a sequence of escalation tiers.

The registry is the ONLY place model names are resolved — nothing else in the
codebase hardcodes a model. Pricing numbers here are user-editable estimates
(see config/models.yaml header), never scraped from anywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from pv_extractor.config import LlmAutoRoutingConfig
from pv_extractor.io_guard import open_read
from pv_extractor.logging_setup import log_event
from pv_extractor.models import LlmUsage

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class UnknownModelError(ValueError):
    """Raised when an alias/id is not in config/models.yaml."""


class ModelPricing(BaseModel):
    """USD per 1M tokens. Editable estimates, not sacred constants."""

    input: float
    output: float
    cache_hit: float
    cache_write_5m: float
    cache_write_1h: float


class ExtractionProfile(BaseModel):
    """Operational thresholds for choosing deal-pass vs document-pass.

    These are quality/latency defaults, not advertised provider limits. They
    live in models.yaml so an analyst can tune behavior without code changes.
    """

    default_shape: Literal["adaptive", "deal", "document"] = "adaptive"
    deal_pass_max_input_tokens: int = 100_000
    deal_pass_max_documents: int = 2
    deal_pass_max_image_pages: int = 15
    deal_pass_max_fields: int = 220
    oversized_target_fields: int = 100
    max_parallel_document_calls: int = 3


class ModelEntry(BaseModel):
    provider: str = "claude"
    alias: str
    id: str
    display_name: str
    context_window: int
    default_effort: str = "medium"
    latest_alias: bool = False  # pass the alias to the provider CLI so it floats
    pinned: bool = False  # always pass the full id
    requires_explicit_enable: bool = False  # AUTO router needs explicit opt-in
    pricing_per_mtok: ModelPricing | None = None
    extraction_profile: ExtractionProfile = Field(default_factory=ExtractionProfile)

    @field_validator("default_effort")
    @classmethod
    def _valid_effort(cls, v: str) -> str:
        if v not in EFFORT_LEVELS:
            raise ValueError(f"unknown effort {v!r} (expected one of {EFFORT_LEVELS})")
        return v

    def cli_model_arg(self) -> str:
        """What goes after the provider's model flag: full id when pinned, the
        alias otherwise (latest_alias entries float as the CLI updates)."""
        if not self.id:
            return ""
        return self.id if self.pinned else self.alias


class ModelSelection(BaseModel):
    """One model/effort resolved by the router."""

    entry: ModelEntry
    effort: str
    reason: str  # extraction | ocr_hostile | auto | per_deal | single_model | provider_default


class ExtractionPlanMetrics(BaseModel):
    """Transparent preflight inputs used for plan selection."""

    estimated_input_tokens: int = 0
    documents: int = 1
    image_pages: int = 0
    fields: int = 0


class ResolvedExtractionPlan(BaseModel):
    provider: str
    model: str
    model_id: str = ""
    effort: str
    execution_shape: Literal["deal", "document"]
    expected_primary_calls: int
    repair_policy: Literal["never", "core_only"] = "never"
    max_repair_calls: int = 0
    reason: str
    selection: ModelSelection


class ModelMenu(BaseModel):
    last_reviewed: str = ""
    models: list[ModelEntry] = Field(default_factory=list)


class ModelRegistry:
    def __init__(self, menu: ModelMenu, path: Path | None = None) -> None:
        if not menu.models:
            raise ValueError("config/models.yaml defines no models")
        seen: set[tuple[str, str]] = set()
        for entry in menu.models:
            for key in (entry.alias, entry.id):
                if not key:
                    continue
                scoped = (entry.provider, key)
                if scoped in seen:
                    raise ValueError(
                        f"duplicate model alias/id {key!r} for provider {entry.provider!r} in models.yaml"
                    )
                seen.add(scoped)
        self.menu = menu
        self.path = path
        self.cli_version: str | None = None
        self.cli_auth_ok: bool | None = None

    # ------------------------------------------------------------------
    # loading / lookup
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "ModelRegistry":
        with open_read(path) as fh:
            data = yaml.safe_load(fh) or {}
        return cls(ModelMenu.model_validate(data), Path(path))

    @property
    def entries(self) -> list[ModelEntry]:
        return list(self.menu.models)

    def entries_for_provider(self, provider: str) -> list[ModelEntry]:
        return [entry for entry in self.menu.models if entry.provider == provider]

    def resolve(self, alias_or_id: str, *, provider: str | None = None) -> ModelEntry:
        for entry in self.menu.models:
            if provider is not None and entry.provider != provider:
                continue
            if alias_or_id in (entry.alias, entry.id):
                return entry
        known = sorted(
            {e.alias for e in self.menu.models if provider is None or e.provider == provider}
            | {e.id for e in self.menu.models if e.id and (provider is None or e.provider == provider)}
        )
        raise UnknownModelError(
            f"unknown model {alias_or_id!r}; allowed (from {self.path or 'models.yaml'}): {known}"
        )

    def refresh_cli_status(self, client) -> dict[str, object]:
        """Record the active local provider CLI version/auth at startup."""
        self.cli_version = client.version()
        self.cli_auth_ok, detail = client.auth_status()
        log_event(
            logger, "llm provider cli status refreshed",
            provider=getattr(client, "provider_name", "unknown"),
            version=self.cli_version, auth_ok=self.cli_auth_ok,
        )
        return {"version": self.cli_version, "auth_ok": self.cli_auth_ok, "detail": detail}

    # ------------------------------------------------------------------
    # routing
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_effort(effort: str) -> str:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"unknown effort {effort!r} (expected one of {EFFORT_LEVELS})")
        return effort

    def select_classification(self, auto: LlmAutoRoutingConfig) -> ModelSelection:
        """Cheap classification tier (AUTO mode helper for future tasks)."""
        return ModelSelection(
            entry=self.resolve(auto.classification_model),
            effort=self._valid_effort(auto.classification_effort),
            reason="classification",
        )

    def extraction_tiers(
        self,
        *,
        mode: str,
        auto: LlmAutoRoutingConfig,
        manual_model: str | None = None,
        manual_effort: str | None = None,
        ocr_hostile: bool = False,
        allow_fable: bool = False,
        provider: str = "claude",
        provider_default_model: str | None = None,
        provider_default_effort: str | None = None,
    ) -> list[ModelSelection]:
        """Compatibility wrapper returning the single primary extraction model.

        Older callers/tests used this method as a retry ladder. The normal
        engine now performs one primary extraction per natural request unit;
        repair is handled separately by explicit policy.
        """
        legacy_mode = "single_model" if mode == "manual" else mode
        metrics = ExtractionPlanMetrics(
            documents=1,
            image_pages=1 if ocr_hostile else 0,
            fields=1,
        )
        plan = self.resolve_extraction_plan(
            routing_mode=legacy_mode,
            metrics=metrics,
            auto=auto,
            provider=provider,
            manual_model=manual_model,
            manual_effort=manual_effort,
            allow_fable=allow_fable,
            provider_default_model=provider_default_model,
            provider_default_effort=provider_default_effort,
        )
        return [plan.selection]

    def _provider_default_selection(
        self,
        *,
        provider: str,
        model: str | None,
        effort: str | None,
    ) -> ModelSelection:
        checked_effort = self._valid_effort(effort or "medium")
        entry: ModelEntry | None = None
        if model:
            try:
                entry = self.resolve(model, provider=provider)
            except UnknownModelError:
                entry = None
        if entry is None:
            model_id = model or ""
            if model_id == "provider-default":
                model_id = ""
            elif model_id:
                try:
                    self.resolve(model_id)
                except UnknownModelError:
                    pass
                else:
                    # A saved Claude alias/id should not become a Codex model id
                    # merely because the provider changed. Use the CLI default.
                    model_id = ""
            entry = next(
                (e for e in self.entries_for_provider(provider) if e.alias == "provider-default"),
                ModelEntry(
                    provider=provider,
                    alias="provider-default",
                    id=model_id,
                    display_name=f"{provider} CLI default",
                    context_window=0,
                    default_effort=checked_effort,
                    pricing_per_mtok=None,
                    extraction_profile=ExtractionProfile(
                        default_shape="document",
                        deal_pass_max_input_tokens=80_000,
                        deal_pass_max_documents=2,
                        deal_pass_max_image_pages=5,
                        deal_pass_max_fields=180,
                        oversized_target_fields=100,
                        max_parallel_document_calls=2,
                    ),
                ),
            )
        return ModelSelection(entry=entry, effort=checked_effort, reason="provider_default")

    def _selection_for_mode(
        self,
        *,
        routing_mode: str,
        metrics: ExtractionPlanMetrics,
        auto: LlmAutoRoutingConfig,
        provider: str,
        manual_model: str | None,
        manual_effort: str | None,
        allow_fable: bool,
        provider_default_model: str | None,
        provider_default_effort: str | None,
    ) -> ModelSelection:
        if provider != "claude":
            return self._provider_default_selection(
                provider=provider,
                model=manual_model if routing_mode in ("single_model", "per_deal") else provider_default_model,
                effort=manual_effort or provider_default_effort,
            )

        if routing_mode in ("single_model", "per_deal"):
            entry = self.resolve(manual_model or "", provider=provider)
            effort = self._valid_effort(manual_effort or entry.default_effort)
            return ModelSelection(entry=entry, effort=effort, reason=routing_mode)
        if routing_mode != "auto":
            raise ValueError(
                f"unknown llm routing mode {routing_mode!r} "
                "(expected auto|per_deal|single_model)"
            )

        if metrics.image_pages > 15 or metrics.documents > 3 or metrics.estimated_input_tokens > 140_000:
            selection = ModelSelection(
                entry=self.resolve(auto.ocr_hostile_model, provider=provider),
                effort=self._valid_effort(auto.ocr_hostile_effort),
                reason="auto",
            )
        else:
            selection = ModelSelection(
                entry=self.resolve(auto.extraction_model, provider=provider),
                effort=self._valid_effort(auto.extraction_effort),
                reason="auto",
            )
        if selection.entry.requires_explicit_enable and not allow_fable:
            raise ValueError(
                f"auto routing selected {selection.entry.alias!r} which requires explicit "
                "enablement (llm.allow_fable / --llm-model)"
            )
        return selection

    def resolve_extraction_plan(
        self,
        *,
        routing_mode: str,
        metrics: ExtractionPlanMetrics,
        auto: LlmAutoRoutingConfig,
        provider: str = "claude",
        manual_model: str | None = None,
        manual_effort: str | None = None,
        execution_shape: str = "profile",
        allow_fable: bool = False,
        provider_default_model: str | None = None,
        provider_default_effort: str | None = None,
        repair_policy: str = "never",
        max_repair_calls_per_deal: int = 1,
    ) -> ResolvedExtractionPlan:
        """Resolve provider/model/effort plus deal-vs-document shape."""
        selection = self._selection_for_mode(
            routing_mode=routing_mode,
            metrics=metrics,
            auto=auto,
            provider=provider,
            manual_model=manual_model,
            manual_effort=manual_effort,
            allow_fable=allow_fable,
            provider_default_model=provider_default_model,
            provider_default_effort=provider_default_effort,
        )
        profile = selection.entry.extraction_profile
        shape_override = execution_shape or "profile"
        if shape_override not in ("profile", "deal", "document"):
            raise ValueError("execution_shape must be profile|deal|document")
        if shape_override in ("deal", "document"):
            shape = shape_override
            shape_reason = "explicit override"
        elif metrics.documents <= 1:
            shape = "document"
            shape_reason = "one ordinary document means one request"
        elif profile.default_shape == "deal":
            shape = "deal"
            shape_reason = "configured whole-deal profile"
        elif profile.default_shape == "document":
            shape = "document"
            shape_reason = "configured per-document profile"
        elif (
            metrics.estimated_input_tokens <= profile.deal_pass_max_input_tokens
            and metrics.documents <= profile.deal_pass_max_documents
            and metrics.image_pages <= profile.deal_pass_max_image_pages
            and metrics.fields <= profile.deal_pass_max_fields
        ):
            shape = "deal"
            shape_reason = (
                f"{metrics.documents} documents and {metrics.fields} fields fit the "
                f"configured {selection.entry.alias} whole-deal profile"
            )
        else:
            shape = "document"
            shape_reason = f"{metrics.documents} documents will run once each in parallel"

        max_repair = max_repair_calls_per_deal if repair_policy == "core_only" else 0
        expected_calls = 1 if shape == "deal" else max(1, metrics.documents)
        pretty_shape = "whole deal" if shape == "deal" else "per document"
        reason = f"{selection.entry.alias.title()} / {pretty_shape} — {shape_reason}"
        return ResolvedExtractionPlan(
            provider=selection.entry.provider,
            model=selection.entry.alias,
            model_id=selection.entry.id,
            effort=selection.effort,
            execution_shape=shape,
            expected_primary_calls=expected_calls,
            repair_policy=repair_policy,  # type: ignore[arg-type]
            max_repair_calls=max_repair,
            reason=reason,
            selection=selection,
        )

    # ------------------------------------------------------------------
    # cost
    # ------------------------------------------------------------------

    def cost_usd(self, usage: LlmUsage, entry: ModelEntry) -> float | None:
        """Cost from the menu's pricing assumptions. Cache writes are priced
        at the 5-minute rate (print-mode sessions are short-lived)."""
        if entry.pricing_per_mtok is None:
            return None
        p = entry.pricing_per_mtok
        cost = (
            usage.input_tokens * p.input
            + usage.output_tokens * p.output
            + usage.cache_read_input_tokens * p.cache_hit
            + usage.cache_creation_input_tokens * p.cache_write_5m
        ) / 1_000_000.0
        return round(cost, 6)

    def fallback_cost_usd(self, usage: LlmUsage, *, provider: str | None = None) -> float | None:
        """Conservative estimate for an unpriced/default model.

        Uses the highest configured price for the provider when available, or
        across all configured models otherwise. This is a budget reservation
        guardrail, not a claim about actual provider billing.
        """
        priced = [
            entry.pricing_per_mtok
            for entry in self.menu.models
            if entry.pricing_per_mtok is not None and (provider is None or entry.provider == provider)
        ]
        if not priced and provider is not None:
            priced = [entry.pricing_per_mtok for entry in self.menu.models if entry.pricing_per_mtok is not None]
        if not priced:
            return None
        input_price = max(p.input for p in priced if p is not None)
        output_price = max(p.output for p in priced if p is not None)
        cache_hit_price = max(p.cache_hit for p in priced if p is not None)
        cache_write_price = max(p.cache_write_5m for p in priced if p is not None)
        cost = (
            usage.input_tokens * input_price
            + usage.output_tokens * output_price
            + usage.cache_read_input_tokens * cache_hit_price
            + usage.cache_creation_input_tokens * cache_write_price
        ) / 1_000_000.0
        return round(cost, 6)
