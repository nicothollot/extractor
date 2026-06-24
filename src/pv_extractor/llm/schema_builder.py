"""Escalated fields -> strict JSON Schema + byte-stable static prompt (D3).

Both artifacts are deterministic functions of the escalated field set and the
compiled master schema only — no timestamps, no memo names, no run ids, JSON
keys sorted — so identical field sets produce byte-identical prompt/schema
files and Claude Code's prompt cache can actually reuse them (rule 6; exact
cache behavior is the CLI's business, we just never sabotage it).

Every response field is an object with embedded provenance (rule 5):
    {value, unit, page, verbatim_quote, confidence: high|medium|low, not_found}
`additionalProperties: false` everywhere; every property required, so the
model cannot skip fields silently.
"""

from __future__ import annotations

import json
import re

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

# One escalated field's answer. Plain types only — structured-output schema
# support excludes numeric/string constraints, so length/range rules live in
# the prompt text and in the Phase-2 merge validation instead.
FIELD_RESULT_SCHEMA: dict = {
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

_PROMPT_RULES = """\
You are extracting structured fields from selected pages of a private-equity
valuation document. The document pages appear first, then the list of fields to
extract; some pages are plain text, some are page images you must view with the
Read tool.

Rules — follow every one exactly:
1. Use ONLY the supplied pages. Never use outside knowledge, never guess,
   never compute a value that is not stated on a page.
2. For every field, fill the response object:
   - value: the extracted value, normalized per the field description
     (currency amounts in USD millions as plain numbers unless the field says
     otherwise; percentages as plain numbers, e.g. 12.5 for 12.5%; basis
     points as plain numbers; multiples as plain numbers, e.g. 8.4 for 8.4x;
     dates as ISO YYYY-MM-DD strings; booleans as true/false).
   - unit: the unit of the value as written in the field description
     (e.g. "USD_millions", "percent", "bps", "x") or null.
   - page: the 1-based page number the value was found on (as labeled in the
     page sections below), or null when not_found.
   - verbatim_quote: an EXACT copy of the source text supporting the value,
     at most 200 characters, copied character-for-character from that page.
     This quote is machine-verified against the page; a quote that does not
     appear on the cited page causes the value to be DISCARDED.
   - confidence: high (explicit label and value), medium (clear but indirect),
     low (uncertain reading, e.g. degraded scan).
   - not_found: true when the field is not present on the supplied pages.
     Then value must be null and verbatim_quote must be "".
3. Controlled-vocabulary fields list their allowed values in the description;
   answer with one of the allowed values verbatim or set not_found.
4. Respond with a single JSON document conforming to the provided schema —
   one object per band, one object per field. No commentary.
5. The response schema uses short JSON keys, NOT the human field names. Each
   band and field below is annotated with the exact key to use, shown as
   `{json key: ...}`. Put each field's object under its band's json key and its
   own json key, exactly as annotated.
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


_FIELD_DEF = "fieldresult"  # $defs name for the shared per-field answer shape


def build_response_schema(fields: list[SchemaField]) -> dict:
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
        "$defs": {_FIELD_DEF: FIELD_RESULT_SCHEMA},
        "properties": bands,
        "required": list(bands),
    }


def response_key_map(fields: list[SchemaField]) -> dict[str, dict[str, str]]:
    """Reverse of the schema keys: {band_key: {field_key: workbook header}}.
    Used to turn the model's response (keyed by the sanitized schema keys) back
    into header-keyed hits for the merge step."""
    return {bk: {fk: f.header for fk, f in keyed} for bk, _band, keyed in _keyed_bands(fields)}


def schema_json_bytes(schema: dict) -> bytes:
    """Canonical serialization: sorted keys, fixed separators, UTF-8."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _field_line(field_key: str, field: SchemaField) -> str:
    parts = [f'- "{field.header}" {{json key: {field_key}}}']
    meta: list[str] = [f"type={field.dtype}"]
    if field.unit:
        meta.append(f"unit={field.unit}")
    if field.controlled_vocab:
        meta.append("allowed=[" + "; ".join(field.controlled_vocab) + "]")
    parts.append("(" + ", ".join(meta) + ")")
    description = " ".join(field.description.split()) if field.description else ""
    if description:
        parts.append("— " + description)
    return " ".join(parts)


def build_field_block(fields: list[SchemaField]) -> str:
    """The band-grouped field list (workbook row-3 descriptions verbatim), each
    band and field annotated with the JSON key the response schema expects. This
    is the part of the prompt that VARIES per call (each chunk/retry asks for a
    different field subset)."""
    lines: list[str] = []
    for band_key, band, keyed in _keyed_bands(fields):
        lines.append(f"[{band}] {{json key: {band_key}}}")
        lines.extend(_field_line(field_key, field) for field_key, field in keyed)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_static_prompt(fields: list[SchemaField]) -> str:
    """Byte-stable instruction block (rules + the field list). Used for the
    response-cache key and the audit copy — NOT the literal wire order, which
    `build_call_prompt` arranges cache-first."""
    return _PROMPT_RULES + "\n" + _FIELDS_PREAMBLE + "\n" + build_field_block(fields)


def build_call_prompt(fields: list[SchemaField], page_payload: str) -> str:
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
        + _FIELDS_PREAMBLE + "\n"
        + build_field_block(fields)
    )
