"""Positional-slot extraction helpers (D4): TC / TX / CS bands.

Slot bands extract WHOLE TABLE ROWS (one comp, one transaction, one debt
tranche each), sort them deterministically, and fill the schema's numbered
slots in order; rows beyond the slot count raise an overflow flag.

Column mapping is stricter than scalar-field lookup: token_set_ratio would
score 'LTM EBITDA' a perfect match against 'EV/LTM EBITDA' (token subset),
so columns map by normalized exact match first and plain fuzz.ratio second.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from pv_extractor.extract import patterns
from pv_extractor.extract.bands.base import ExtractionContext, FieldSpec, _Candidate, build_hit
from pv_extractor.models import FieldHit, FlagSeverity, PageContent, ReviewFlag, SchemaField, TableData
from pv_extractor.normalize import normalize_text

_COLUMN_FUZZY_THRESHOLD = 85

# Aggregate rows are statistics, not comps/tranches; they never fill slots.
AGGREGATE_ROW_LABELS = frozenset(
    {"mean", "median", "average", "min", "max", "high", "low", "total", "sum", "wtd avg", "weighted average"}
)

_SLOT_PREFIX_RE = re.compile(r"^(TC|TX|CS)(\d{2}) (.+)$")


@dataclass
class ColumnSpec:
    """One slot sub-field <-> table column mapping rule."""

    subfield: str  # schema header after the slot prefix, e.g. 'EV/LTM EBITDA'
    headers: list[str]  # accepted column-header spellings
    kind: str  # text | amount | multiple | percent | boolean | date | vocab
    required: bool = False  # a table must map every required column to qualify


def split_slot_header(header: str) -> tuple[str, int, str] | None:
    """'TC01 EV/LTM EBITDA' -> ('TC', 1, 'EV/LTM EBITDA')."""
    m = _SLOT_PREFIX_RE.match(header)
    if m is None:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def map_columns(table: TableData, colspecs: list[ColumnSpec]) -> dict[str, tuple[int, float]] | None:
    """subfield -> (column index, match quality), or None when any required
    column is missing. Exact-normalized match wins; fuzz.ratio >= 85 second,
    discriminator-token conflicts excluded ('EV/LTM EBITDA' is one character
    from 'EV/NTM EBITDA' but never the same column). Each column serves at
    most ONE subfield — the best-quality claim owns it."""
    if not table.rows:
        return None
    header_row = table.rows[0]
    normalized = [normalize_text(str(cell)) if cell else "" for cell in header_row]

    claims: list[tuple[float, int, str]] = []  # (quality, column index, subfield)
    for spec in colspecs:
        best: tuple[int, float] | None = None
        for idx, cell in enumerate(normalized):
            if not cell:
                continue
            for wanted in spec.headers:
                wanted_norm = normalize_text(wanted)
                if cell == wanted_norm:
                    best = (idx, 1.0)
                    break
                if patterns.discriminator_conflict(cell, wanted_norm):
                    continue
                ratio = fuzz.ratio(cell, wanted_norm)
                if ratio >= _COLUMN_FUZZY_THRESHOLD and (best is None or ratio / 100.0 > best[1]):
                    best = (idx, ratio / 100.0)
            if best is not None and best[1] == 1.0:
                break
        if best is not None:
            claims.append((best[1], best[0], spec.subfield))

    mapping: dict[str, tuple[int, float]] = {}
    owned: set[int] = set()
    for quality, col_idx, subfield in sorted(claims, key=lambda claim: -claim[0]):
        if col_idx in owned:
            continue
        owned.add(col_idx)
        mapping[subfield] = (col_idx, quality)

    if any(spec.required and spec.subfield not in mapping for spec in colspecs):
        return None
    return mapping


def is_aggregate_row(label: str) -> bool:
    return normalize_text(label) in AGGREGATE_ROW_LABELS


def data_rows(table: TableData, name_col: int) -> list[tuple[int, list[str | None]]]:
    """Non-header, non-aggregate, non-empty rows of a slot table."""
    out: list[tuple[int, list[str | None]]] = []
    for idx, row in enumerate(table.rows[1:], start=1):
        name = row[name_col] if name_col < len(row) else None
        if name is None or not str(name).strip():
            continue
        if is_aggregate_row(str(name)):
            continue
        out.append((idx, row))
    return out


def parse_slot_cell(text: str, kind: str, vocab_map: dict[str, list[str]] | None = None):
    """(value, clean, raw) or None. Multiples in slot tables are often bare
    numbers ('9.1' without the x) — accepted leniently."""
    text = str(text).strip()
    if not text:
        return None
    if kind == "amount":
        parsed = patterns.parse_amount(text)
        if parsed is None:
            return None
        value, clean = patterns.normalize_amount_to_millions(parsed)
        return value, clean, parsed.raw
    if kind == "multiple":
        parsed = patterns.parse_multiple(text)
        if parsed is not None:
            return parsed.value, parsed.clean, parsed.raw
        bare = patterns.parse_number(text)
        if bare is not None:
            return bare.value, False, bare.raw
        return None
    if kind == "percent":
        parsed = patterns.parse_percent(text)
        if parsed is not None:
            return parsed.value, parsed.clean, parsed.raw
        bare = patterns.parse_number(text)  # rate columns often omit the % sign
        if bare is not None and -100 <= float(bare.value) <= 100:
            return bare.value, False, bare.raw
        return None
    if kind == "boolean":
        parsed = patterns.parse_boolean(text)
        return None if parsed is None else (parsed.value, True, parsed.raw)
    if kind == "date":
        result = patterns.parse_date_text(text)
        return None if result is None else (result[0].isoformat(), True, result[1])
    if kind == "number":
        parsed = patterns.parse_number(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw)
    if kind == "vocab" and vocab_map is not None:
        norm = normalize_text(text)
        for entry, spellings in vocab_map.items():
            if norm == normalize_text(entry) or any(norm == normalize_text(s) for s in spellings):
                return entry, True, text
        return None
    return text, True, text


def slot_schema_fields(schema_fields: list[SchemaField], group: str) -> dict[tuple[int, str], SchemaField]:
    """(slot_number, subfield) -> SchemaField for one slot group."""
    out: dict[tuple[int, str], SchemaField] = {}
    for field in schema_fields:
        if field.slot_group != group:
            continue
        parts = split_slot_header(field.header)
        if parts is not None:
            out[(parts[1], parts[2])] = field
    return out


def emit_slot_hits(
    *,
    group: str,
    entity_rows: list[dict],  # [{subfield: (value, clean, raw, evidence)}, ...] sorted
    schema_fields: list[SchemaField],
    page: PageContent,
    table: TableData,
    column_quality: dict[str, float],
    ctx: ExtractionContext,
) -> list[FieldHit]:
    """Fill slots in order from sorted entity rows; overflow raises a flag."""
    fields = slot_schema_fields(schema_fields, group)
    slot_count = max((number for number, _ in fields), default=0)
    hits: list[FieldHit] = []

    if len(entity_rows) > slot_count:
        ctx.flags.append(
            ReviewFlag(
                category="slots",
                description=(
                    f"{group}: {len(entity_rows)} rows found, {slot_count} slots — "
                    f"{len(entity_rows) - slot_count} overflow row(s) dropped"
                ),
                severity=FlagSeverity.warning,
                reviewer_attention=True,
            )
        )

    for slot_number, row_values in enumerate(entity_rows[:slot_count], start=1):
        for subfield, payload in row_values.items():
            field = fields.get((slot_number, subfield))
            if field is None or payload is None:
                continue
            value, clean, raw, evidence = payload
            candidate = _Candidate(
                raw_text=raw,
                value=value,
                clean=clean,
                label_quality=column_quality.get(subfield, 1.0),
                page=page,
                from_table=True,
                evidence=patterns.snippet(evidence, ctx.cfg.max_evidence_chars),
                bbox=table.bbox,
                unit=field.unit,
            )
            hit = build_hit(FieldSpec(header=field.header, labels=[subfield]), field, [candidate], [], ctx)
            if hit is not None:
                hits.append(hit)
    return hits
