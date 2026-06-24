"""D3 schema builder: strict JSON schema (additionalProperties:false
everywhere, everything required) and byte-stable static prompts built
verbatim from the compiled workbook row-3 descriptions."""

from __future__ import annotations

import random

import pytest

import re

from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.schema_builder import (
    build_call_prompt,
    build_response_schema,
    build_static_prompt,
    response_key_map,
    schema_json_bytes,
)

# Deliberately includes headers with spaces / % — illegal as raw JSON keys.
HEADERS = ["Fund Name", "Gross IRR %", "MOIC", "Primary Methodology"]
_KEY_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@pytest.fixture(scope="module")
def fields():
    by_header = {f.header: f for f in load_schema_fields()}
    missing = [h for h in HEADERS if h not in by_header]
    assert not missing, f"schema headers changed: {missing}"
    return [by_header[h] for h in HEADERS]


def _walk_objects(schema: dict):
    if schema.get("type") == "object":
        yield schema
        for sub in schema.get("properties", {}).values():
            yield from _walk_objects(sub)


def test_schema_is_strict_everywhere(fields):
    schema = build_response_schema(fields)
    # the per-field shape is defined once under $defs and referenced by $ref
    assert "fieldresult" in schema["$defs"]
    objects = list(_walk_objects(schema))  # root + bands + the $defs field shape
    assert len(objects) >= 1 + 1 + 1
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    # every field property is a $ref to the shared shape (keeps the schema small)
    for band in schema["properties"].values():
        for prop in band["properties"].values():
            assert prop == {"$ref": "#/$defs/fieldresult"}


def _all_property_keys(schema: dict):
    """Every property key at every level of the schema."""
    for obj in _walk_objects(schema):
        yield from obj.get("properties", {})


def test_schema_property_keys_are_api_legal(fields):
    """The API rejects tool input_schema keys that don't match the pattern with
    HTTP 400 — raw headers ("Gross IRR %") and band names ("METHODOLOGY:
    MULTIPLE") would fail, so every key must be sanitized."""
    schema = build_response_schema(fields)
    keys = list(_all_property_keys(schema))
    assert keys, "schema produced no property keys"
    for key in keys:
        assert _KEY_OK.match(key), f"illegal schema key: {key!r}"


def test_schema_is_band_grouped_and_reverses_to_headers(fields):
    schema = build_response_schema(fields)
    key_map = response_key_map(fields)
    bands = {f.band for f in fields}
    # one top-level (sanitized) key per band, reversing to the band names
    assert set(schema["properties"]) == set(key_map)
    # the reverse map recovers exactly the escalated headers, nothing else
    headers_in_map = {h for fmap in key_map.values() for h in fmap.values()}
    assert headers_in_map == set(HEADERS)
    assert len(key_map) == len(bands)
    # every schema field key has a header in the reverse map
    for band_key, band_obj in schema["properties"].items():
        assert set(band_obj["properties"]) == set(key_map[band_key])


def test_field_object_carries_embedded_provenance(fields):
    schema = build_response_schema(fields)
    field_obj = schema["$defs"]["fieldresult"]  # the shared $ref target
    assert field_obj["additionalProperties"] is False
    assert set(field_obj["properties"]) == {
        "value", "unit", "page", "verbatim_quote", "confidence", "not_found",
    }
    assert sorted(field_obj["required"]) == sorted(field_obj["properties"])
    assert field_obj["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


def test_ref_schema_is_compact_enough_for_windows_cmdline(fields):
    """The schema is passed inline on the `claude` command line; Windows caps
    that at ~32 KB. $ref keeps even a large field set well under it."""
    from pv_extractor.extract.engine import load_schema_fields

    big = load_schema_fields()[:200]
    size = len(schema_json_bytes(build_response_schema(big)))
    assert size < 20_000, f"$ref schema for 200 fields is {size} bytes — too big for Windows"


def test_schema_and_prompt_are_byte_stable_and_order_independent(fields):
    shuffled = list(fields)
    random.Random(42).shuffle(shuffled)
    assert schema_json_bytes(build_response_schema(fields)) == schema_json_bytes(
        build_response_schema(shuffled)
    )
    assert build_static_prompt(fields) == build_static_prompt(shuffled)
    assert build_static_prompt(fields) == build_static_prompt(fields)  # no timestamps etc.


def test_response_round_trips_sanitized_keys_back_to_headers(fields):
    """A model response keyed by the sanitized schema keys must flatten back to
    the workbook headers the merge step expects."""
    from pv_extractor.llm.escalate import _flatten_response

    schema = build_response_schema(fields)
    answer = {"value": 1.0, "unit": None, "page": 1, "verbatim_quote": "x",
              "confidence": "high", "not_found": False}
    structured = {
        band_key: {field_key: answer for field_key in band_obj["properties"]}
        for band_key, band_obj in schema["properties"].items()
    }
    flat = _flatten_response(structured, fields)
    assert set(flat) == set(HEADERS)


def test_prompt_carries_row3_descriptions_verbatim(fields):
    prompt = build_static_prompt(fields)
    for field in fields:
        assert f'"{field.header}"' in prompt
        assert f"[{field.band}]" in prompt
        collapsed = " ".join(field.description.split())
        if collapsed:
            assert collapsed in prompt
    # provenance contract is spelled out for the model
    assert "verbatim_quote" in prompt and "not_found" in prompt and "DISCARDED" in prompt


def test_prompt_contains_only_requested_fields(fields):
    prompt = build_static_prompt(fields[:2])
    assert f'"{fields[3].header}"' not in prompt


def test_call_prompt_is_cache_first(fields):
    """The wire prompt puts the stable rules + page payload BEFORE the per-call
    field list, so a memo's chunks/retries reuse the page payload from the
    prompt cache instead of re-uploading it behind a varying field list."""
    pages = "== DOCUMENT PAGES ==\n--- page 1 (TEXT) ---\nEnterprise Value 100"
    prompt = build_call_prompt(fields, pages)
    i_rules = prompt.index("You are extracting")
    i_pages = prompt.index("DOCUMENT PAGES")
    i_fields = prompt.index("Fields to extract")
    assert i_rules < i_pages < i_fields  # rules -> pages (stable prefix) -> fields
    # the page payload is identical for two different field subsets, so the
    # cacheable prefix (rules + pages) is shared across a memo's calls
    a = build_call_prompt(fields[:2], pages)
    b = build_call_prompt(fields[2:], pages)
    prefix = prompt[:i_fields]
    assert a.startswith(prefix) and b.startswith(prefix)
