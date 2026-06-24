"""Escalated fields -> strict JSON Schema + byte-stable static prompt (D3).

Both artifacts are deterministic functions of the escalated field set and the
compiled master schema only — no timestamps, no memo names, no run ids, JSON
keys sorted — so identical field sets produce byte-identical prompt/schema
files and Claude Code's prompt cache can actually reuse them (rule 6; exact
cache behavior is the CLI's business, we just never sabotage it).

Default structured output is sparse schema v5:

    {
      "schema_version": 5,
      "scope": {"type": "deal|document", "id": "..."},
      "values": [{field_id, value, unit, document_id, page, as_of_date, quote,
                  evidence_kind, model_confidence}],
      "not_found": [...],
      "conflicts": [{field_id, reason, candidates: [...]}],
      "warnings": [...]
    }

Only found values get full result objects. The decoder validates that every
requested field id is accounted for exactly once. Legacy schema-v2 sparse and
schema-v1 band-grouped responses remain supported through explicit
compatibility helpers.
"""

from __future__ import annotations

import json
import math
import re

from pv_extractor.llm.response_validation import StructuredResponseError
from pv_extractor.models import SchemaField

# The Anthropic API rejects any tool input_schema property key that does not
# match this pattern (HTTP 400 "Property keys should match pattern ..."), so the
# human workbook headers ("Total Invested Capital ($M)") and band names
# ("METHODOLOGY: MULTIPLE") CANNOT be used as JSON keys directly. We sanitize
# every key to a legal token, annotate the prompt with the exact key per field,
# and reverse the mapping when parsing the response (see response_key_map).
_KEY_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_KEY_ILLEGAL = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_key(name: str, unique_id: int, used: set[str]) -> str:
    """Map an arbitrary band/field name to a key matching `_KEY_OK`, unique
    within `used`. `unique_id` (a stable, globally-unique int — the field's
    col_index or the band's order) disambiguates collisions deterministically,
    so the mapping does not depend on call order."""
    base = _KEY_ILLEGAL.sub("_", name).strip("_")[:48] or "field"
    key = base if base not in used else f"{base}__{unique_id}"[:64]
    while key in used:  # pathological: still collides after the id suffix
        unique_id += 1
        key = f"{base}__{unique_id}"[:64]
    used.add(key)
    return key

# One legacy escalated field's answer. Plain types only — structured-output schema
# support excludes numeric/string constraints, so length/range rules live in
# the prompt text and in the Phase-2 merge validation instead.
LEGACY_FIELD_RESULT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "value": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"}]},
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "page": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "verbatim_quote": {"type": "string"},
        "confidence": {"enum": ["high", "medium", "low"]},
        "not_found": {"type": "boolean"},
    },
    "required": ["value", "unit", "page", "verbatim_quote", "confidence", "not_found"],
}

# Backward-compatible export for provider wrapper tests and legacy callers.
FIELD_RESULT_SCHEMA = LEGACY_FIELD_RESULT_SCHEMA

LEGACY_SPARSE_RESULT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "field_key": {"type": "string"},
        "value": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "page": {"type": "integer"},
        "evidence_quote": {"type": "string"},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["field_key", "value", "unit", "page", "evidence_quote", "confidence", "notes"],
}

SPARSE_CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "field_id": {"type": "string"},
        "value": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "document_id": {"type": "string"},
        "page": {"type": "integer"},
        "as_of_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "quote": {"type": "string"},
        "evidence_kind": {"type": "string"},
        "model_confidence": {"type": "number"},
    },
    "required": [
        "field_id", "value", "unit", "document_id", "page", "as_of_date",
        "quote", "evidence_kind", "model_confidence",
    ],
}

SPARSE_CONFLICT_CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "value": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "document_id": {"type": "string"},
        "page": {"type": "integer"},
        "as_of_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "quote": {"type": "string"},
        "evidence_kind": {"type": "string"},
        "model_confidence": {"type": "number"},
    },
    "required": [
        "value", "unit", "document_id", "page", "as_of_date", "quote",
        "evidence_kind", "model_confidence",
    ],
}

# Backward-compatible export name: now the v5 value-candidate schema.
SPARSE_RESULT_SCHEMA = SPARSE_CANDIDATE_SCHEMA

_PROMPT_RULES = """\
<role>
You are an evidence-grounded extraction engine for private-equity and valuation documents.
</role>

<rules>
Use only the supplied documents. Check every requested field before responding
and do not stop after headline valuation metrics. Follow each field's type,
unit, allowed values, extraction policy, source priority, reporting-period rule,
and merge policy. Do not calculate generated/formula/QA fields. Every value
must cite document_id, page, and a short direct quote. Return a conflict instead
of silently guessing. Account for each requested field exactly once. Return only
the required JSON.
</rules>

<schema>
Return sparse schema_version=5 JSON with:
- scope: {"type": "deal" or "document", "id": "..."}
- values: one accepted candidate object per found field
- not_found: field ids that are absent from this response scope
- conflicts: fields with materially different candidates
- warnings: concise response-scope warnings

For a whole-deal task, not_found means absent across every supplied document.
For a per-document task, not_found means absent from that document only.
</schema>

<candidate_confidence>
For every value candidate, return model_confidence from 0.00 to 1.00. It is
your estimate that ALL of the following are correct together:
- the value belongs to the requested field;
- the normalized value and unit are correct;
- the requested entity and reporting period/as-of date are correct;
- the cited document and page support the value;
- the quote is direct evidence for that value.

Assess confidence after selecting the value and evidence. Do not use confidence
merely to describe document readability. Use these anchors consistently:
- 0.95-1.00: direct, unambiguous label or table row; exact entity, period, unit;
- 0.80-0.94: explicit evidence with only minor normalization/interpretation;
- 0.60-0.79: plausible candidate with meaningful ambiguity;
- 0.30-0.59: weak candidate useful mainly for human review;
- below 0.30: do not return as an accepted value; use not_found or conflict.
</candidate_confidence>

<examples>
{"schema_version":5,"scope":{"type":"document","id":"D01"},"values":[{"field_id":"F001","value":1349,"unit":"USD_millions","document_id":"D01","page":7,"as_of_date":"2026-03-31","quote":"Implied Enterprise Value of $1,349 million","evidence_kind":"explicit_label","model_confidence":0.88}],"not_found":[],"conflicts":[],"warnings":[]}
{"schema_version":5,"scope":{"type":"document","id":"D01"},"values":[],"not_found":["F002"],"conflicts":[],"warnings":[]}
{"schema_version":5,"scope":{"type":"deal","id":"D01-D02"},"values":[],"not_found":[],"conflicts":[{"field_id":"F003","reason":"different quarter-specific candidates","candidates":[{"value":100,"unit":"USD_millions","document_id":"D01","page":4,"as_of_date":"2026-03-31","quote":"Enterprise value was $100 million","evidence_kind":"table","model_confidence":0.84},{"value":92,"unit":"USD_millions","document_id":"D02","page":3,"as_of_date":"2025-12-31","quote":"Enterprise value was $92 million","evidence_kind":"explicit_label","model_confidence":0.76}]}],"warnings":[]}
</examples>
"""

_LEGACY_PROMPT_RULES = """\
You are extracting structured fields from selected pages of a private-equity
valuation document. The document pages appear first, then the list of fields to
extract.

Rules — follow every one exactly:
1. Use ONLY the supplied pages. Never use outside knowledge, never guess,
   never compute a value that is not stated on a page.
2. For every field, fill the response object with value, unit, page,
   verbatim_quote, confidence (high|medium|low), and not_found.
3. Controlled-vocabulary fields list their allowed values in the description;
   answer with one of the allowed values verbatim or set not_found.
4. Respond with a single JSON document conforming to the provided schema —
   one object per band, one object per field. No commentary.
5. The response schema uses short JSON keys, NOT the human field names. Each
   band and field below is annotated with the exact key to use, shown as
   `{json key: ...}`.
"""

_FIELDS_PREAMBLE = (
    "Fields to extract, grouped by workbook band (the description after each "
    "field header is the authoritative extraction instruction):"
)


def sorted_fields(fields: list[SchemaField]) -> list[SchemaField]:
    """Stable ordering: workbook column order (band order follows columns)."""
    return sorted(fields, key=lambda f: f.col_index)


def band_grouped(fields: list[SchemaField]) -> dict[str, list[SchemaField]]:
    """Band -> fields, both in workbook column order (deterministic)."""
    grouped: dict[str, list[SchemaField]] = {}
    for field in sorted_fields(fields):
        grouped.setdefault(field.band, []).append(field)
    return grouped


def _keyed_bands(
    fields: list[SchemaField],
) -> list[tuple[str, str, list[tuple[str, SchemaField]]]]:
    """Deterministic [(band_key, band_name, [(field_key, field), ...]), ...].

    Every band_key and field_key matches `_KEY_OK` so the response schema is
    accepted by the API. Keys derive only from the field set (band order +
    col_index), so build_response_schema and response_key_map agree."""
    band_used: set[str] = set()
    out: list[tuple[str, str, list[tuple[str, SchemaField]]]] = []
    for band_idx, (band, band_fields) in enumerate(band_grouped(fields).items()):
        band_key = _safe_key(band, band_idx, band_used)
        field_used: set[str] = set()
        keyed = [(_safe_key(f.header, f.col_index, field_used), f) for f in band_fields]
        out.append((band_key, band, keyed))
    return out


def _keyed_fields(fields: list[SchemaField]) -> list[tuple[str, SchemaField]]:
    used: set[str] = set()
    return [(_safe_key(field.header, field.col_index, used), field) for field in sorted_fields(fields)]


_FIELD_DEF = "fieldresult"  # $defs name for the shared legacy per-field answer shape
_SPARSE_DEF = "candidate"
_SPARSE_CONFLICT_DEF = "conflictcandidate"


def build_response_schema(fields: list[SchemaField]) -> dict:
    """Sparse schema-v5 response shape for primary extraction tasks."""
    field_keys = [field_key for field_key, _field in _keyed_fields(fields)]
    result_schema = json.loads(json.dumps(SPARSE_CANDIDATE_SCHEMA))
    result_schema["properties"]["field_id"] = {"enum": field_keys}
    conflict_candidate_schema = json.loads(json.dumps(SPARSE_CONFLICT_CANDIDATE_SCHEMA))
    conflict_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "field_id": {"enum": field_keys},
            "reason": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": {"$ref": f"#/$defs/{_SPARSE_CONFLICT_DEF}"},
            },
        },
        "required": ["field_id", "reason", "candidates"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "$defs": {
            _SPARSE_DEF: result_schema,
            _SPARSE_CONFLICT_DEF: conflict_candidate_schema,
            "conflict": conflict_schema,
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"type": {"enum": ["deal", "document"]}, "id": {"type": "string"}},
                "required": ["type", "id"],
            },
        },
        "properties": {
            "schema_version": {"enum": [5]},
            "scope": {"$ref": "#/$defs/scope"},
            "values": {"type": "array", "items": {"$ref": f"#/$defs/{_SPARSE_DEF}"}},
            "not_found": {"type": "array", "items": {"enum": field_keys}},
            "conflicts": {"type": "array", "items": {"$ref": "#/$defs/conflict"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["schema_version", "scope", "values", "not_found", "conflicts", "warnings"],
    }


def build_legacy_response_schema(fields: list[SchemaField]) -> dict:
    """Strict band-grouped response schema for `claude --json-schema`. Property
    keys are sanitized to satisfy the API's `^[A-Za-z0-9_.-]{1,64}$` rule; the
    human header/band names live in the prompt's field list instead.

    The per-field answer shape (~330 chars) is defined ONCE under `$defs` and
    every field is a `$ref` to it — inlining it per field made a ~200-field
    schema ~100 KB, and the schema is passed INLINE on the `claude` command line
    where Windows caps the line at ~32 KB ([WinError 206]). With `$ref` the same
    schema is ~10 KB, so the whole escalate-everything field set fits in ONE
    call."""
    bands: dict[str, dict] = {}
    for band_key, _band, keyed in _keyed_bands(fields):
        bands[band_key] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {fk: {"$ref": f"#/$defs/{_FIELD_DEF}"} for fk, _f in keyed},
            "required": [fk for fk, _f in keyed],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "$defs": {_FIELD_DEF: LEGACY_FIELD_RESULT_SCHEMA},
        "properties": bands,
        "required": list(bands),
    }


def response_key_map(fields: list[SchemaField]) -> dict[str, dict[str, str]]:
    """Reverse of the schema keys: {band_key: {field_key: workbook header}}.
    Used to turn the model's response (keyed by the sanitized schema keys) back
    into header-keyed hits for the merge step."""
    return {bk: {fk: f.header for fk, f in keyed} for bk, _band, keyed in _keyed_bands(fields)}


def sparse_response_key_map(fields: list[SchemaField]) -> dict[str, str]:
    """{field_key: workbook header} for schema-v2 sparse responses."""
    return {field_key: field.header for field_key, field in _keyed_fields(fields)}


def sparse_field_keys(fields: list[SchemaField]) -> list[str]:
    return [field_key for field_key, _field in _keyed_fields(fields)]


def schema_json_bytes(schema: dict) -> bytes:
    """Canonical serialization: sorted keys, fixed separators, UTF-8."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _field_line(field_key: str, field: SchemaField, *, legacy: bool = False, inferable: bool = False) -> str:
    key_label = "json key" if legacy else "field_id"
    parts = [f'- "{field.header}" {{{key_label}: {field_key}}}']
    meta: list[str] = [f"type={field.dtype}"]
    if field.unit:
        meta.append(f"unit={field.unit}")
    if field.controlled_vocab:
        meta.append("allowed=[" + "; ".join(field.controlled_vocab) + "]")
    if inferable:
        meta.append("inferable=true")
    parts.append("(" + ", ".join(meta) + ")")
    description = " ".join(field.description.split()) if field.description else ""
    if description:
        parts.append("— " + description)
    return " ".join(parts)


def build_field_block(fields: list[SchemaField], *, legacy: bool = False, inferable_fields: set[str] | None = None) -> str:
    """The band-grouped field list (workbook row-3 descriptions verbatim), each
    field annotated with the key the response schema expects. This
    is the part of the prompt that VARIES per call (each chunk/retry asks for a
    different field subset)."""
    inferable_fields = inferable_fields or set()
    lines: list[str] = []
    if legacy:
        for band_key, band, keyed in _keyed_bands(fields):
            lines.append(f"[{band}] {{json key: {band_key}}}")
            lines.extend(
                _field_line(field_key, field, legacy=True, inferable=field.header in inferable_fields)
                for field_key, field in keyed
            )
            lines.append("")
    else:
        by_band: dict[str, list[tuple[str, SchemaField]]] = {}
        for field_key, field in _keyed_fields(fields):
            by_band.setdefault(field.band, []).append((field_key, field))
        for band, keyed in by_band.items():
            lines.append(f"[{band}]")
            lines.extend(
                _field_line(field_key, field, inferable=field.header in inferable_fields)
                for field_key, field in keyed
            )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_static_prompt(fields: list[SchemaField], *, inferable_fields: set[str] | None = None) -> str:
    """Byte-stable instruction block (rules + the field list). Used for the
    response-cache key and the audit copy — NOT the literal wire order, which
    `build_call_prompt` arranges cache-first."""
    return _PROMPT_RULES + "\n" + _FIELDS_PREAMBLE + "\n" + build_field_block(
        fields, inferable_fields=inferable_fields
    )


def build_legacy_static_prompt(fields: list[SchemaField]) -> str:
    return _LEGACY_PROMPT_RULES + "\n" + _FIELDS_PREAMBLE + "\n" + build_field_block(fields, legacy=True)


def build_call_prompt(
    fields: list[SchemaField],
    page_payload: str,
    *,
    inferable_fields: set[str] | None = None,
    corrective: bool = False,
) -> str:
    """The prompt actually sent to `claude -p`, ordered for prompt-cache reuse:
    the STABLE rules + the page payload first (identical across a memo's chunks
    and across a group's retry tiers — so it is created in the cache once and
    READ thereafter), then the per-call field list last. Same content as
    build_static_prompt + payload, only reordered so the expensive page payload
    is a cacheable prefix instead of being re-uploaded behind a varying field
    list."""
    return (
        _PROMPT_RULES + "\n"
        + page_payload.rstrip() + "\n\n"
        + (
            "CORRECTION: The previous response was invalid or incomplete. "
            "Return only schema-valid JSON and account for every requested field exactly once.\n\n"
            if corrective else ""
        )
        + _FIELDS_PREAMBLE + "\n"
        + build_field_block(fields, inferable_fields=inferable_fields)
    )


def build_legacy_call_prompt(fields: list[SchemaField], page_payload: str, *, corrective: bool = False) -> str:
    return (
        _LEGACY_PROMPT_RULES + "\n"
        + page_payload.rstrip() + "\n\n"
        + (
            "CORRECTION: The previous response was invalid. Return only schema-valid JSON.\n\n"
            if corrective else ""
        )
        + _FIELDS_PREAMBLE + "\n"
        + build_field_block(fields, legacy=True)
    )


def _confidence_label(score: object) -> str:
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        if score >= 0.8:
            return "high"
        if score >= 0.5:
            return "medium"
    return "low"


def _confidence_number(value: object, *, default: float = 0.35) -> float:
    if isinstance(value, bool):
        raise StructuredResponseError("confidence must be numeric, not boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        labels = {"high": 0.85, "medium": 0.60, "low": 0.35}
        key = str(value).strip().lower()
        if key not in labels:
            number = default
        else:
            number = labels[key]
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise StructuredResponseError(f"model_confidence {number!r} is outside [0, 1]")
    return number


def _candidate_from_v5(item: dict, *, field_id: str | None = None) -> dict:
    fid = field_id or item.get("field_id")
    if not isinstance(fid, str) or not fid:
        raise StructuredResponseError("candidate missing field_id")
    page = item.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise StructuredResponseError(f"candidate {fid!r} has invalid page {page!r}")
    document_id = item.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise StructuredResponseError(f"candidate {fid!r} missing document_id")
    quote = item.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise StructuredResponseError(f"candidate {fid!r} missing quote")
    confidence = _confidence_number(item.get("model_confidence"), default=0.0)
    return {
        "field_key": fid,
        "field_id": fid,
        "value": item.get("value"),
        "unit": item.get("unit"),
        "page": page,
        "document_id": document_id,
        "as_of_date": item.get("as_of_date"),
        "evidence_kind": item.get("evidence_kind") or "",
        "verbatim_quote": quote,
        "quote": quote,
        "confidence": confidence,
        "raw_model_confidence": confidence,
        "model_confidence": confidence,
        "confidence_label": _confidence_label(confidence),
        "not_found": False,
        "notes": "",
    }


def _decode_sparse_v5_response(structured: dict, fields: list[SchemaField]) -> dict[str, dict]:
    key_map = sparse_response_key_map(fields)
    requested = set(key_map)
    if structured.get("schema_version") != 5:
        raise StructuredResponseError("sparse response missing schema_version=5")
    values = structured.get("values")
    not_found = structured.get("not_found")
    conflicts = structured.get("conflicts")
    if not isinstance(values, list) or not isinstance(not_found, list) or not isinstance(conflicts, list):
        raise StructuredResponseError("schema v5 response requires values, not_found, and conflicts arrays")

    seen: set[str] = set()
    flat: dict[str, dict] = {}

    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise StructuredResponseError(f"values[{index}] is not an object")
        candidate = _candidate_from_v5(item)
        field_id = candidate["field_id"]
        if field_id not in requested:
            raise StructuredResponseError(f"unknown value field_id {field_id!r}")
        if field_id in seen:
            raise StructuredResponseError(f"duplicate field_id {field_id!r}")
        seen.add(field_id)
        flat[key_map[field_id]] = candidate

    for field_id in not_found:
        if field_id not in requested:
            raise StructuredResponseError(f"unknown not_found field_id {field_id!r}")
        if field_id in seen:
            raise StructuredResponseError(f"field_id {field_id!r} appears more than once")
        seen.add(field_id)
        flat[key_map[field_id]] = {
            "value": None,
            "unit": None,
            "page": None,
            "document_id": None,
            "as_of_date": None,
            "verbatim_quote": "",
            "quote": "",
            "confidence": 0.0,
            "raw_model_confidence": 0.0,
            "model_confidence": 0.0,
            "confidence_label": "low",
            "not_found": True,
            "notes": "",
            "field_key": field_id,
            "field_id": field_id,
        }

    for index, item in enumerate(conflicts):
        if not isinstance(item, dict):
            raise StructuredResponseError(f"conflicts[{index}] is not an object")
        field_id = item.get("field_id")
        if field_id not in requested:
            raise StructuredResponseError(f"unknown conflict field_id {field_id!r}")
        if field_id in seen:
            raise StructuredResponseError(f"field_id {field_id!r} appears more than once")
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise StructuredResponseError(f"conflict {field_id!r} requires candidates")
        decoded = [
            _candidate_from_v5(candidate, field_id=field_id)
            for candidate in candidates
            if isinstance(candidate, dict)
        ]
        if len(decoded) != len(candidates):
            raise StructuredResponseError(f"conflict {field_id!r} has non-object candidates")
        seen.add(field_id)
        flat[key_map[field_id]] = {
            "value": None,
            "unit": None,
            "page": None,
            "verbatim_quote": "",
            "confidence": max((c["model_confidence"] for c in decoded), default=0.0),
            "raw_model_confidence": max((c["model_confidence"] for c in decoded), default=0.0),
            "model_confidence": max((c["model_confidence"] for c in decoded), default=0.0),
            "confidence_label": "low",
            "not_found": False,
            "conflict": True,
            "conflict_reason": item.get("reason") or "materially different candidates",
            "conflict_candidates": decoded,
            "notes": item.get("reason") or "",
            "field_key": field_id,
            "field_id": field_id,
        }

    missing = requested - seen
    if missing:
        raise StructuredResponseError(f"sparse response did not account for field_id(s): {sorted(missing)!r}")
    return flat


def _decode_sparse_v2_response(structured: dict, fields: list[SchemaField]) -> dict[str, dict]:
    key_map = sparse_response_key_map(fields)
    requested = set(key_map)
    results = structured.get("results")
    not_found = structured.get("not_found_field_keys")
    if structured.get("schema_version") != 2:
        raise StructuredResponseError("sparse response missing schema_version=2")
    if not isinstance(results, list) or not isinstance(not_found, list):
        raise StructuredResponseError("sparse response requires results and not_found_field_keys arrays")

    seen: set[str] = set()
    flat: dict[str, dict] = {}
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise StructuredResponseError(f"results[{index}] is not an object")
        field_key = item.get("field_key")
        if field_key not in requested:
            raise StructuredResponseError(f"unknown result field_key {field_key!r}")
        if field_key in seen:
            raise StructuredResponseError(f"duplicate result field_key {field_key!r}")
        seen.add(field_key)
        header = key_map[field_key]
        confidence = _confidence_number(item.get("confidence"), default=0.35)
        flat[header] = {
            "value": item.get("value"),
            "unit": item.get("unit"),
            "page": item.get("page"),
            "verbatim_quote": item.get("evidence_quote") or "",
            "confidence": confidence,
            "raw_model_confidence": confidence,
            "model_confidence": confidence,
            "confidence_label": _confidence_label(confidence),
            "not_found": False,
            "notes": item.get("notes") or "",
            "field_key": field_key,
            "field_id": field_key,
        }

    for field_key in not_found:
        if field_key not in requested:
            raise StructuredResponseError(f"unknown not_found field_key {field_key!r}")
        if field_key in seen:
            raise StructuredResponseError(f"field_key {field_key!r} appears in results and not_found")
        seen.add(field_key)
        header = key_map[field_key]
        flat[header] = {
            "value": None,
            "unit": None,
            "page": None,
            "verbatim_quote": "",
            "confidence": 0.0,
            "raw_model_confidence": 0.0,
            "model_confidence": 0.0,
            "confidence_label": "low",
            "not_found": True,
            "notes": "",
            "field_key": field_key,
            "field_id": field_key,
        }

    missing = requested - seen
    if missing:
        raise StructuredResponseError(f"sparse response did not account for field_key(s): {sorted(missing)!r}")
    return flat


def decode_structured_response(structured: dict, fields: list[SchemaField]) -> dict[str, dict]:
    """Decode sparse v5/v2 or legacy v1 into {workbook header: result object}."""
    if structured.get("schema_version") == 5 or "values" in structured:
        return _decode_sparse_v5_response(structured, fields)
    if structured.get("schema_version") == 2 or "results" in structured:
        return _decode_sparse_v2_response(structured, fields)
    key_map = response_key_map(fields)
    flat: dict[str, dict] = {}
    for band_key, band_obj in structured.items():
        if not isinstance(band_obj, dict):
            continue
        field_map = key_map.get(band_key, {})
        for field_key, result in band_obj.items():
            header = field_map.get(field_key)
            if header is not None and isinstance(result, dict):
                copy = dict(result)
                copy.setdefault("field_key", field_key)
                copy.setdefault("field_id", field_key)
                confidence = _confidence_number(copy.get("confidence"), default=0.35)
                copy["confidence"] = confidence
                copy.setdefault("raw_model_confidence", confidence)
                copy.setdefault("model_confidence", confidence)
                copy.setdefault("confidence_label", _confidence_label(confidence))
                flat[header] = copy
    requested = {field.header for field in fields}
    missing = requested - set(flat)
    if missing:
        raise StructuredResponseError(f"legacy response did not account for field(s): {sorted(missing)!r}")
    return flat
