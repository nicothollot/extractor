"""Structured-output parsing and validation shared by local LLM providers."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


class StructuredResponseError(ValueError):
    pass


def parse_json_object(text: str) -> dict:
    raw = text.strip()
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group("body").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredResponseError(f"invalid JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StructuredResponseError("structured response is not a JSON object")
    return parsed


def validate_structured_response(schema: dict, value: Any) -> None:
    _validate(schema, value, schema, "$")


def _validate(schema: dict, value: Any, root: dict, path: str) -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise StructuredResponseError(f"{path}: unsupported schema ref {ref!r}")
        name = ref.rsplit("/", 1)[-1]
        target = (root.get("$defs") or {}).get(name)
        if not isinstance(target, dict):
            raise StructuredResponseError(f"{path}: missing schema ref {ref!r}")
        _validate(target, value, root, path)
        return

    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                _validate(option, value, root, path)
                return
            except StructuredResponseError as exc:
                errors.append(str(exc))
        raise StructuredResponseError(f"{path}: did not match any allowed type ({'; '.join(errors[:3])})")

    if "enum" in schema and value not in schema["enum"]:
        raise StructuredResponseError(f"{path}: {value!r} not in enum {schema['enum']!r}")

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise StructuredResponseError(f"{path}: expected object")
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [name for name in required if name not in value]
        if missing:
            raise StructuredResponseError(f"{path}: missing required keys {missing!r}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise StructuredResponseError(f"{path}: unexpected keys {extra!r}")
        for name, child_schema in props.items():
            if name in value:
                _validate(child_schema, value[name], root, f"{path}.{name}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise StructuredResponseError(f"{path}: expected array")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate(child, item, root, f"{path}[{index}]")
        return
    if expected == "string" and not isinstance(value, str):
        raise StructuredResponseError(f"{path}: expected string")
    elif expected == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise StructuredResponseError(f"{path}: expected number")
    elif expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise StructuredResponseError(f"{path}: expected integer")
    elif expected == "boolean" and not isinstance(value, bool):
        raise StructuredResponseError(f"{path}: expected boolean")
    elif expected == "null" and value is not None:
        raise StructuredResponseError(f"{path}: expected null")
