"""D3 schema builder: strict JSON schema (additionalProperties:false
everywhere, everything required) and byte-stable static prompts built
verbatim from the compiled workbook row-3 descriptions."""

from __future__ import annotations

import random

import pytest

from pv_extractor.extract.engine import load_schema_fields
from pv_extractor.llm.schema_builder import (
    build_response_schema,
    build_static_prompt,
    schema_json_bytes,
)

HEADERS = ["Fund Name", "Gross IRR %", "MOIC", "Primary Methodology"]


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
    objects = list(_walk_objects(schema))
    assert len(objects) >= 1 + 1 + len(fields)  # root + bands + field objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_schema_is_band_grouped_and_restricted(fields):
    schema = build_response_schema(fields)
    bands = {f.band for f in fields}
    assert set(schema["properties"]) == bands
    headers_in_schema = {
        header for band in schema["properties"].values() for header in band["properties"]
    }
    assert headers_in_schema == set(HEADERS)  # escalated fields only, nothing else


def test_field_object_carries_embedded_provenance(fields):
    schema = build_response_schema(fields)
    band = next(iter(schema["properties"].values()))
    field_obj = next(iter(band["properties"].values()))
    assert set(field_obj["properties"]) == {
        "value", "unit", "page", "verbatim_quote", "confidence", "not_found",
    }
    assert field_obj["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


def test_schema_and_prompt_are_byte_stable_and_order_independent(fields):
    shuffled = list(fields)
    random.Random(42).shuffle(shuffled)
    assert schema_json_bytes(build_response_schema(fields)) == schema_json_bytes(
        build_response_schema(shuffled)
    )
    assert build_static_prompt(fields) == build_static_prompt(shuffled)
    assert build_static_prompt(fields) == build_static_prompt(fields)  # no timestamps etc.


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
