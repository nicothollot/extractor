"""Schema-driven per-field checks (D5): type, controlled vocab, ranges.

Generic rules from the compiled schema JSON plus config:
  * percents inside validation.percent_range (rules.yaml `ranges` overrides
    per field — "unless schema says otherwise")
  * any '... Weight %' field >= 0
  * dates inside [validation.date_year_min, as-of + N years]
  * enum values must be members of the field's controlled vocabulary
  * dtype sanity (numbers numeric, booleans boolean)
"""

from __future__ import annotations

import re
from datetime import date

from pv_extractor.config import ValidationConfig
from pv_extractor.models import FieldHit, FlagSeverity, ReviewFlag, SchemaField

_NUMERIC_DTYPES = {"number", "percent", "basis_points", "multiple_x", "years", "integer"}

# Forward-looking contractual dates (maturities, projection horizons, modeled
# exits) legitimately sit far beyond as-of + 1y; only the lower bound applies.
_FORWARD_DATE_RE = re.compile(r"maturity|projection|exit|drag", re.IGNORECASE)


def check_hits(
    hits: list[FieldHit],
    schema_by_header: dict[str, SchemaField],
    validation: ValidationConfig,
    as_of_date: date | None,
    range_overrides: dict[str, dict[str, float]] | None = None,
) -> list[ReviewFlag]:
    flags: list[ReviewFlag] = []
    overrides = range_overrides or {}

    for hit in hits:
        field = schema_by_header.get(hit.field)
        if field is None or hit.value is None or hit.method == "metadata":
            continue
        value = hit.value

        if field.dtype in _NUMERIC_DTYPES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                flags.append(_flag(hit, f"expected a {field.dtype} value, got {value!r}", "type_mismatch"))
                continue
        if field.dtype == "boolean" and not isinstance(value, bool):
            flags.append(_flag(hit, f"expected a boolean, got {value!r}", "type_mismatch"))
            continue

        if field.dtype == "percent" and isinstance(value, (int, float)):
            low, high = validation.percent_range
            override = overrides.get(hit.field)
            if override:
                low = override.get("min", low)
                high = override.get("max", high)
            if not low <= float(value) <= high:
                flags.append(_flag(hit, f"{value} % outside [{low}, {high}]", "percent_range"))
            if "weight" in hit.field.lower() and float(value) < 0:
                flags.append(_flag(hit, f"weight {value} % is negative", "negative_weight"))

        if field.dtype == "date" and isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                flags.append(_flag(hit, f"unparseable date {value!r}", "date_parse"))
                continue
            year_min = validation.date_year_min
            latest = (
                date(as_of_date.year + validation.date_max_years_after_asof, as_of_date.month, 28)
                if as_of_date and not _FORWARD_DATE_RE.search(hit.field)
                else None
            )
            if parsed.year < year_min or (latest is not None and parsed > latest):
                flags.append(
                    _flag(
                        hit,
                        f"date {parsed.isoformat()} outside [{year_min}, "
                        f"as-of + {validation.date_max_years_after_asof}y]",
                        "date_range",
                    )
                )

        if field.controlled_vocab and isinstance(value, str) and value not in field.controlled_vocab:
            flags.append(_flag(hit, f"{value!r} is not in the controlled vocabulary", "controlled_vocab"))

    return flags


def _flag(hit: FieldHit, detail: str, code: str) -> ReviewFlag:
    return ReviewFlag(
        category="range",
        description=f"{hit.field}: {detail}",
        severity=FlagSeverity.warning,
        field=hit.field,
        origin="validation",
        code=code,
    )
