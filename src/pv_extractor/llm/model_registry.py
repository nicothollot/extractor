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


class ModelEntry(BaseModel):
    alias: str
    id: str
    display_name: str
    context_window: int
    default_effort: str = "medium"
    latest_alias: bool = False  # pass the alias to Claude Code so it floats
    pinned: bool = False  # always pass the full id
    requires_explicit_enable: bool = False  # AUTO router needs explicit opt-in
    pricing_per_mtok: ModelPricing

    @field_validator("default_effort")
    @classmethod
    def _valid_effort(cls, v: str) -> str:
        if v not in EFFORT_LEVELS:
            raise ValueError(f"unknown effort {v!r} (expected one of {EFFORT_LEVELS})")
        return v

    def cli_model_arg(self) -> str:
        """What goes after `claude --model`: the full id when pinned, the
        alias otherwise (latest_alias entries float as Claude Code updates)."""
        return self.id if self.pinned else self.alias


class ModelSelection(BaseModel):
    """One escalation tier resolved by the router."""

    entry: ModelEntry
    effort: str
    reason: str  # extraction | ocr_hostile | retry | fable | manual


class ModelMenu(BaseModel):
    last_reviewed: str = ""
    models: list[ModelEntry] = Field(default_factory=list)


class ModelRegistry:
    def __init__(self, menu: ModelMenu, path: Path | None = None) -> None:
        if not menu.models:
            raise ValueError("config/models.yaml defines no models")
        seen: set[str] = set()
        for entry in menu.models:
            for key in (entry.alias, entry.id):
                if key in seen:
                    raise ValueError(f"duplicate model alias/id {key!r} in models.yaml")
                seen.add(key)
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

    def resolve(self, alias_or_id: str) -> ModelEntry:
        for entry in self.menu.models:
            if alias_or_id in (entry.alias, entry.id):
                return entry
        known = sorted({e.alias for e in self.menu.models} | {e.id for e in self.menu.models})
        raise UnknownModelError(
            f"unknown model {alias_or_id!r}; allowed (from {self.path or 'models.yaml'}): {known}"
        )

    def refresh_cli_status(self, client) -> dict[str, object]:
        """Record the local Claude Code version/auth at startup (read-only)."""
        self.cli_version = client.version()
        self.cli_auth_ok, detail = client.auth_status()
        log_event(
            logger, "claude code status refreshed",
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
    ) -> list[ModelSelection]:
        """The ordered escalation passes for one memo (rule 4: one Claude
        Code call per memo per tier).

        MANUAL: the user's model+effort, forced, single pass. Naming a
        requires_explicit_enable model (Fable) manually IS the explicit
        opt-in. AUTO: Sonnet for normal extraction, Opus when the memo is
        OCR-hostile or the first pass left failures, Fable only when the
        user enabled it (it is the most expensive tier — never a default).
        """
        if mode == "manual":
            entry = self.resolve(manual_model or "")
            effort = self._valid_effort(manual_effort or entry.default_effort)
            return [ModelSelection(entry=entry, effort=effort, reason="manual")]
        if mode != "auto":
            raise ValueError(f"unknown llm mode {mode!r} (expected auto|manual)")

        tiers: list[ModelSelection] = []
        if ocr_hostile:
            first = ModelSelection(
                entry=self.resolve(auto.ocr_hostile_model),
                effort=self._valid_effort(auto.ocr_hostile_effort),
                reason="ocr_hostile",
            )
        else:
            first = ModelSelection(
                entry=self.resolve(auto.extraction_model),
                effort=self._valid_effort(auto.extraction_effort),
                reason="extraction",
            )
        if first.entry.requires_explicit_enable and not allow_fable:
            raise ValueError(
                f"auto routing selected {first.entry.alias!r} which requires explicit "
                "enablement (llm.allow_fable / --llm-model)"
            )
        tiers.append(first)

        retry_entry = self.resolve(auto.retry_model)
        retry_effort = self._valid_effort(
            auto.retry_effort_bump if retry_entry.alias == first.entry.alias else auto.retry_effort
        )
        if not (retry_entry.requires_explicit_enable and not allow_fable):
            tiers.append(ModelSelection(entry=retry_entry, effort=retry_effort, reason="retry"))

        if allow_fable:
            fable = next((e for e in self.menu.models if e.requires_explicit_enable), None)
            if fable is not None and all(t.entry.alias != fable.alias for t in tiers):
                tiers.append(
                    ModelSelection(
                        entry=fable,
                        effort=self._valid_effort(auto.fable_effort),
                        reason="fable",
                    )
                )
        return tiers

    # ------------------------------------------------------------------
    # cost
    # ------------------------------------------------------------------

    def cost_usd(self, usage: LlmUsage, entry: ModelEntry) -> float:
        """Cost from the menu's pricing assumptions. Cache writes are priced
        at the 5-minute rate (print-mode sessions are short-lived)."""
        p = entry.pricing_per_mtok
        cost = (
            usage.input_tokens * p.input
            + usage.output_tokens * p.output
            + usage.cache_read_input_tokens * p.cache_hit
            + usage.cache_creation_input_tokens * p.cache_write_5m
        ) / 1_000_000.0
        return round(cost, 6)
