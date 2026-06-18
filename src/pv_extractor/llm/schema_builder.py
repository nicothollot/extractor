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

from pv_extractor.models import SchemaField

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

_PROMPT_HEADER = """\
You are extracting structured fields from selected pages of a private-equity
valuation document. The pages follow after the field list; some pages are
plain text, some are page images you must view with the Read tool.

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

Fields to extract, grouped by workbook band (the description after each
field header is the authoritative extraction instruction):
"""


def sorted_fields(fields: list[SchemaField]) -> list[SchemaField]:
    """Stable ordering: workbook column order (band order follows columns)."""
    return sorted(fields, key=lambda f: f.col_index)


def band_grouped(fields: list[SchemaField]) -> dict[str, list[SchemaField]]:
    """Band -> fields, both in workbook column order (deterministic)."""
    grouped: dict[str, list[SchemaField]] = {}
    for field in sorted_fields(fields):
        grouped.setdefault(field.band, []).append(field)
    return grouped


def build_response_schema(fields: list[SchemaField]) -> dict:
    """Strict band-grouped response schema for `claude --json-schema`."""
    bands: dict[str, dict] = {}
    for band, band_fields in band_grouped(fields).items():
        bands[band] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {f.header: FIELD_RESULT_SCHEMA for f in band_fields},
            "required": [f.header for f in band_fields],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": bands,
        "required": list(bands),
    }


def schema_json_bytes(schema: dict) -> bytes:
    """Canonical serialization: sorted keys, fixed separators, UTF-8."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _field_line(field: SchemaField) -> str:
    parts = [f'- "{field.header}"']
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


def build_static_prompt(fields: list[SchemaField]) -> str:
    """Byte-stable instruction block: header + band-grouped field list with
    the workbook row-3 descriptions verbatim (whitespace-collapsed only)."""
    lines: list[str] = [_PROMPT_HEADER]
    for band, band_fields in band_grouped(fields).items():
        lines.append(f"[{band}]")
        lines.extend(_field_line(field) for field in band_fields)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
