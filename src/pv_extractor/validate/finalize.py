"""Canonical post-assistance asset finalization.

This is the single cleanup/recompute path used after LLM assistance and after
multi-document merging. It is deterministic and idempotent: generated QA,
threshold, validation and derived artifacts are removed, then derived fields
and validation/QA are recomputed from the final hit set.
"""

from __future__ import annotations

from datetime import date

from pv_extractor.config import Config
from pv_extractor.extract.bands.base import ExtractionContext
from pv_extractor.extract.derived import apply_derived, derived_specs
from pv_extractor.models import AssetExtraction, FieldHit, ReviewFlag, SchemaField, VerifyResult
from pv_extractor.validate import RuleSet, validate_asset
from pv_extractor.validate.flags import deduplicate_review_flags, is_generated_flag

_GENERATED_BANDS = {"QA", "THRESHOLD FLAGS"}


def _is_generated_hit(hit: FieldHit, derived_headers: set[str]) -> bool:
    if hit.band in _GENERATED_BANDS:
        return True
    return hit.field in derived_headers and hit.method == "computed"


def _base_hits(asset: AssetExtraction, derived_headers: set[str]) -> list[FieldHit]:
    hits: list[FieldHit] = []
    for hit in asset.hits:
        if not _is_generated_hit(hit, derived_headers):
            hits.append(hit.model_copy(deep=True))
            continue
        # A computed derived hit may carry the extracted candidate it replaced
        # as a conflict. Rehydrate that candidate before recomputing derived
        # fields so finalization remains idempotent without losing provenance.
        if hit.field in derived_headers and hit.method == "computed" and hit.conflicts:
            conflict = hit.conflicts[0]
            hits.append(
                FieldHit(
                    field=hit.field,
                    col_index=hit.col_index,
                    band=hit.band,
                    raw_text=conflict.raw_text,
                    value=conflict.value,
                    unit=hit.unit,
                    page=conflict.page,
                    method="deterministic",
                    confidence=conflict.confidence,
                    evidence=conflict.evidence,
                    evidence_ref=conflict.evidence_ref,
                )
            )
    return hits


def _preserved_flags(asset: AssetExtraction, extra_flags: list[ReviewFlag] | None) -> list[ReviewFlag]:
    flags = [flag.model_copy(deep=True) for flag in asset.flags if not is_generated_flag(flag)]
    flags.extend(flag.model_copy(deep=True) for flag in (extra_flags or []) if not is_generated_flag(flag))
    return deduplicate_review_flags(flags)


def finalize_asset_after_assistance(
    asset: AssetExtraction,
    *,
    config: Config,
    schema_by_header: dict[str, SchemaField],
    ruleset: RuleSet,
    routing_table: dict[str, list[str]],
    as_of_date: date | None,
    verify: VerifyResult | None,
    prior_row: dict[str, object] | None,
    client: str | None = None,
    extra_flags: list[ReviewFlag] | None = None,
) -> AssetExtraction:
    """Finalize one asset in place and return it.

    `extra_flags` is for source/run flags that live outside the asset before
    the first validation pass (for example memo-level reader/run flags).
    Generated validation/QA flags in either location are discarded.
    """
    derived_headers = {spec.header for spec in derived_specs(config.validation)}
    base_hits = _base_hits(asset, derived_headers)
    preserved_flags = _preserved_flags(asset, extra_flags)

    ctx = ExtractionContext(cfg=config.extraction)
    with_derived = apply_derived(base_hits, schema_by_header, config.validation, ctx)
    validation_input_flags = deduplicate_review_flags([*preserved_flags, *ctx.flags])
    validated = validate_asset(
        hits=with_derived,
        extraction_flags=validation_input_flags,
        schema_by_header=schema_by_header,
        config=config,
        ruleset=ruleset,
        routing_table=routing_table,
        as_of_date=as_of_date,
        verify=verify,
        prior_row=prior_row,
        client=client,
    )

    asset.hits = validated.hits
    asset.flags = deduplicate_review_flags(validated.flags)
    asset.qa_status = validated.qa_status
    return asset
