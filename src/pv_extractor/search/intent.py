"""Free-text query -> DocTypeSpec, RULE-FIRST (the mandatory no-LLM core).

`resolve_intent` ALWAYS runs a deterministic rule layer that yields a valid
DocTypeSpec, then OPTIONALLY augments it with one hidden Claude Code call. The
rule layer merges every matching phrase from config.smart_search.intent_rules
with a small built-in financial-doc lexicon, so common queries ("quarterly
reports", "annual report", "cap table", "audited financials", and the builtin
doc types) resolve well with zero CLI involvement.

ROBUSTNESS CONTRACT: the entire CLI attempt is wrapped in try/except. Any
failure — missing binary, not authenticated, timeout, malformed JSON, unknown
model, budget — is caught, logged via log_event, and resolution returns the
rule-only spec with provenance 'rules'. The call site never sees an exception
and the LLM is never required.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.model_registry import ModelRegistry
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DocTypeSpec
from pv_extractor.normalize import normalize_text

logger = logging.getLogger(__name__)

# Built-in financial-doc lexicon, ALWAYS in effect (independent of config so a
# fresh checkout resolves common queries without intent_rules edits). Each key
# is a normalized phrase; the longest matching phrases win their fragments.
# Keep this aligned with config.smart_search.intent_rules defaults — config
# fragments are UNION-merged on top, so an operator can only ever ADD anchors.
_BUILTIN_LEXICON: dict[str, dict[str, list[str]]] = {
    "quarterly report": {
        "filename_include": ["quarterly", "quarterly report", "10 q", "q1", "q2", "q3", "q4"],
        "filename_regex": ["10[- ]?q"],
        "folder_include": ["filings", "quarterly"],
        "extensions": [".pdf", ".htm", ".html"],
    },
    "quarterly": {
        "filename_include": ["quarterly", "q1", "q2", "q3", "q4"],
        "folder_include": ["quarterly"],
    },
    "annual report": {
        "filename_include": ["annual", "annual report", "10 k"],
        "filename_regex": ["10[- ]?k"],
        "folder_include": ["filings", "annual"],
    },
    "annual": {
        "filename_include": ["annual"],
        "folder_include": ["annual"],
    },
    "cap table": {
        "filename_include": ["cap table", "capitalization table", "captable"],
        "folder_include": ["legal", "cap table"],
    },
    "audited financials": {
        "filename_include": [
            "audited", "audited financials", "audited financial statements", "audit report"
        ],
        "folder_include": ["audit", "financials"],
    },
    "financial statements": {
        "filename_include": ["financial statements", "financials", "balance sheet", "income statement"],
        "folder_include": ["financials"],
    },
    "valuation memo": {
        "filename_include": ["valuation memo", "val memo", "valuation write up", "valuation summary"],
    },
    "ic memo": {
        "filename_include": ["ic memo", "investment committee"],
    },
    "portfolio review": {
        "filename_include": ["portfolio review", "quarterly review"],
    },
    "lender presentation": {
        "filename_include": ["lender presentation", "lender deck", "lender update"],
    },
}

# DocTypeSpec list-valued fields a rule fragment may contribute.
_FRAGMENT_FIELDS: tuple[str, ...] = (
    "filename_include",
    "filename_regex",
    "filename_exclude",
    "folder_include",
    "folder_exclude",
    "extensions",
)

_INTENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "filename_include", "filename_regex", "filename_exclude",
        "folder_include", "folder_exclude", "extensions",
    ],
    "properties": {
        "filename_include": {"type": "array", "items": {"type": "string"}},
        "filename_regex": {"type": "array", "items": {"type": "string"}},
        "filename_exclude": {"type": "array", "items": {"type": "string"}},
        "folder_include": {"type": "array", "items": {"type": "string"}},
        "folder_exclude": {"type": "array", "items": {"type": "string"}},
        "extensions": {"type": "array", "items": {"type": "string"}},
    },
}

_PROMPT = """\
You translate an analyst's free-text document search into filename and folder
anchors used to rank files on a private valuation network share. You are given
ONE search query. Reply ONLY with the JSON object the schema demands.

Rules:
- filename_include / folder_include: lowercase token or phrase synonyms a
  matching file's name / folder path would contain (e.g. for "quarterly
  report": "quarterly", "q1", "q2", "q3", "q4", "10 q"). Use the share's
  natural language, not regex.
- filename_regex: raw regex patterns ONLY when a synonym can't capture the
  variant (e.g. "10[- ]?q"). Otherwise leave empty.
- filename_exclude / folder_exclude: terms that would mean the WRONG document.
- extensions: likely file extensions WITH the dot (".pdf", ".xlsx"); empty if
  any extension is fine.
Return concise, high-precision lists. Do not invent client or deal names.

SEARCH QUERY: """


def _slug_from_query(query: str) -> str:
    """A stable, readable slug from free text: normalized, words joined by '-',
    capped. Empty queries fall back to 'search'."""
    norm = normalize_text(query)
    slug = "-".join(norm.split())[:64].strip("-")
    return slug or "search"


def _label_from_query(query: str) -> str:
    cleaned = " ".join(query.split()).strip()
    if not cleaned:
        return "Search"
    return cleaned[:80]


def _merge_fragment(target: dict[str, list[str]], fragment: dict) -> None:
    """Union-merge a rule fragment into the accumulating field lists,
    de-duplicating while preserving first-seen order."""
    for field in _FRAGMENT_FIELDS:
        values = fragment.get(field)
        if not values:
            continue
        bucket = target.setdefault(field, [])
        for raw in values:
            value = str(raw).strip()
            if value and value not in bucket:
                bucket.append(value)


def _rule_spec(query: str, config: Config) -> tuple[DocTypeSpec, bool]:
    """Build a DocTypeSpec from the built-in lexicon + config.intent_rules.
    Returns (spec, matched_any_phrase). Always yields a valid spec — when no
    phrase matches, the query's own normalized tokens become the includes."""
    norm = normalize_text(query)
    padded = f" {norm} "
    accumulated: dict[str, list[str]] = {}
    matched = False

    # Built-in lexicon first (longest phrases first so specific beats generic),
    # then config.smart_search.intent_rules layered on top (operator additions).
    sources: list[dict] = [_BUILTIN_LEXICON, dict(config.smart_search.intent_rules)]
    for source in sources:
        for phrase in sorted(source, key=lambda p: -len(p)):
            if f" {normalize_text(phrase)} " in padded:
                _merge_fragment(accumulated, source[phrase])
                matched = True

    if not matched:
        # No known phrase — fall back to the query's own meaningful tokens so
        # ranking still has something to match on (degrade gracefully, never
        # an empty spec). Single-letter tokens are dropped as noise.
        tokens = [t for t in norm.split() if len(t) > 1]
        if tokens:
            accumulated["filename_include"] = tokens

    return (
        DocTypeSpec(
            slug=_slug_from_query(query),
            label=_label_from_query(query),
            filename_include=accumulated.get("filename_include", []),
            filename_regex=accumulated.get("filename_regex", []),
            filename_exclude=accumulated.get("filename_exclude", []),
            folder_include=accumulated.get("folder_include", []),
            folder_exclude=accumulated.get("folder_exclude", []),
            extensions=accumulated.get("extensions", []),
            period_required=False,  # free-text search is period-optional by default
        ),
        matched,
    )


def _cli_augment(
    spec: DocTypeSpec,
    query: str,
    config: Config,
    *,
    cc_client: ClaudeCodeClient | None,
) -> bool:
    """ONE hidden Claude Code call; UNION-merges its anchors into `spec`
    in place. Returns True iff anchors were actually merged. Never raises —
    any failure is caught + logged and returns False (spec unchanged)."""
    cfg = config.smart_search
    try:
        registry = ModelRegistry.load(config.llm.models_path)
        entry = registry.resolve(cfg.cli_model)
        effort = cfg.cli_effort or entry.default_effort
        client = cc_client or ClaudeCodeClient(config)
        if client.binary_path() is None and not config.claude_code.command_args:
            log_event(logger, "smart search cli skipped: no claude binary")
            return False
        # ignore_cleanup_errors: a bridged `claude` (WSL) can still hold the
        # schema file at block exit on Windows (rmtree -> WinError 32); the OS
        # reclaims the temp dir later, so a cleanup failure must never abort us.
        with tempfile.TemporaryDirectory(prefix="pv_smart_search_", ignore_cleanup_errors=True) as tmp:
            schema_path = Path(tmp) / "schema.json"
            with guarded_open_write(schema_path, config.pv_root, mode="wb") as fh:
                fh.write(json.dumps(_INTENT_SCHEMA).encode("utf-8"))
            result = client.extract_json(
                job_id=f"smart-search-{_slug_from_query(query)}",
                prompt=_PROMPT + query.strip(),
                schema_path=schema_path,
                model=entry.cli_model_arg(),
                effort=effort,
                cwd=Path(tmp),
                allow_read_tool=False,
            )
        if not result.ok or not isinstance(result.structured, dict):
            log_event(
                logger, "smart search cli no result",
                error=result.error or "no structured output",
            )
            return False
        before = spec.model_dump()
        accumulated = {field: list(getattr(spec, field)) for field in _FRAGMENT_FIELDS}
        _merge_fragment(accumulated, result.structured)
        for field, values in accumulated.items():
            setattr(spec, field, values)
        changed = spec.model_dump() != before
        log_event(
            logger, "smart search cli augmented",
            model=entry.alias, effort=effort, changed=changed,
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
        )
        return changed
    except Exception as exc:  # noqa: BLE001 — robustness contract: never propagate
        log_event(logger, "smart search cli failed, using rules only", error=str(exc))
        return False


def resolve_intent(
    query: str,
    config: Config,
    *,
    conn: sqlite3.Connection | None = None,
    use_cli: bool | None = None,
    cc_client: ClaudeCodeClient | None = None,
) -> tuple[DocTypeSpec, str]:
    """Resolve free text into a (DocTypeSpec, provenance) pair.

    The rule layer ALWAYS runs first and always yields a valid spec. The CLI
    layer only fires when (use_cli if not None else
    config.smart_search.use_cli_fallback) is True; it can only ADD anchors and
    never blocks — any failure leaves the rule spec untouched. provenance is
    'rules' or 'rules+cli'. `conn` is accepted for symmetry/future profile
    persistence; resolution itself needs no DB.
    """
    # The rule layer is the mandatory self-sufficient path: it must never raise.
    # normalize_text/DocTypeSpec don't raise on a str, but guard defensively so
    # a pathological query still yields a usable (empty-include) spec.
    try:
        spec, _matched = _rule_spec(query, config)
    except Exception as exc:  # noqa: BLE001 — the no-LLM contract must always return a spec
        log_event(logger, "smart search rule layer fell back to empty spec", error=str(exc))
        spec = DocTypeSpec(slug=_slug_from_query(query), label=query.strip() or "Search")
    want_cli = config.smart_search.use_cli_fallback if use_cli is None else use_cli
    provenance = "rules"
    if want_cli:
        if _cli_augment(spec, query, config, cc_client=cc_client):
            provenance = "rules+cli"
    log_event(
        logger, "smart search intent resolved",
        slug=spec.slug, provenance=provenance,
        filename_includes=len(spec.filename_include),
    )
    return spec, provenance
