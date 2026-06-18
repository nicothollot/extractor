"""Generic spec-driven band extraction (D4).

Every band module declares FieldSpecs (accepted label spellings, optional
table row/column hints, vocab aliases) and reuses one engine:

  candidates   = fuzzy table-cell lookups + label:value prose lines on the
                 band's targeted pages only
  parsing      = patterns.py by field kind (derived from the schema dtype +
                 unit), unit-normalized HERE with raw_text preserved
  vocab        = controlled-vocab mapping exact -> alias -> fuzzy >=
                 config vocab_fuzzy_threshold; below that the field stays
                 EMPTY and a flag is raised — never guessed
  confidence   = confidence.hit_confidence (multiplicative components)
  conflicts    = distinct candidate values beyond the winner are kept on the
                 hit (audit record) and trigger the ambiguity penalty
  failures     = a label that matched but whose value did not parse raises a
                 parse flag — a numeric parse failure is never a silent None
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from rapidfuzz import fuzz

from pv_extractor.config import ExtractionConfig
from pv_extractor.extract import patterns
from pv_extractor.extract.confidence import hit_confidence
from pv_extractor.models import (
    ConflictingCandidate,
    FieldHit,
    FlagSeverity,
    PageContent,
    ReviewFlag,
    SchemaField,
)
from pv_extractor.normalize import normalize_text

# Column headers tried when a spec gives no explicit table_col: the generic
# 'value of this metric' columns found in memo tables.
DEFAULT_VALUE_COLS = ["value", "selected", "current", "amount", "total"]

# table_col values that still mean 'the value of the metric' — these specs
# may fall back to a headerless label|value pair row.
_PAIR_FALLBACK_COLS = frozenset({*DEFAULT_VALUE_COLS, "mid"})

_MAX_CONFLICTS_KEPT = 5


@dataclass
class FieldSpec:
    """How to locate and parse ONE schema field deterministically."""

    header: str  # schema row-2 header, exact
    labels: list[str]  # accepted document label spellings
    table_row: list[str] | None = None  # row labels (default: labels)
    table_col: list[str] | None = None  # column headers (default: DEFAULT_VALUE_COLS)
    vocab_aliases: dict[str, list[str]] = dc_field(default_factory=dict)  # vocab entry -> spellings


@dataclass
class ExtractionContext:
    """Shared state for one memo-asset extraction pass."""

    cfg: ExtractionConfig
    flags: list[ReviewFlag] = dc_field(default_factory=list)
    _line_cache: dict[int, list[patterns.LabeledLine]] = dc_field(default_factory=dict)

    def labeled_lines(self, page: PageContent) -> list[patterns.LabeledLine]:
        key = id(page)
        if key not in self._line_cache:
            self._line_cache[key] = patterns.split_labeled_lines(page.text)
        return self._line_cache[key]


@dataclass
class _Candidate:
    raw_text: str
    value: bool | int | float | str | None
    clean: bool
    label_quality: float
    page: PageContent | None
    from_table: bool
    evidence: str
    bbox: tuple[float, float, float, float] | None = None
    unit: str | None = None


def kind_for_field(field: SchemaField) -> str:
    """Parse kind from the schema dtype + unit (single source of truth)."""
    dtype = field.dtype
    if dtype == "percent":
        return "percent"
    if dtype == "basis_points":
        return "bps"
    if dtype == "multiple_x":
        return "multiple"
    if dtype == "years":
        return "years"
    if dtype == "integer":
        return "integer"
    if dtype == "boolean":
        return "boolean"
    if dtype == "date":
        return "date"
    if dtype == "enum":
        return "vocab"
    if dtype == "number":
        return "amount" if field.unit in ("USD_millions", "millions_local") else "number"
    return "text"


# ---------------------------------------------------------------------------
# value parsing per kind (unit normalization happens here, raw preserved)
# ---------------------------------------------------------------------------


def parse_value(
    text: str, kind: str, field: SchemaField, spec: FieldSpec, ctx: ExtractionContext
) -> tuple[bool | int | float | str | None, bool, str, str | None] | None:
    """(value, clean, raw, unit) for one candidate text, or None when the
    text does not contain a value of this kind (caller flags it)."""
    text = text.strip()
    if not text:
        return None

    if kind == "amount":
        parsed = patterns.parse_amount(text)
        if parsed is None:
            return None
        value, clean = patterns.normalize_amount_to_millions(parsed)
        unit = field.unit
        if field.unit == "millions_local":
            unit = f"{parsed.currency}_millions" if parsed.currency else "millions_local"
        elif field.unit == "USD_millions" and parsed.currency not in (None, "USD"):
            clean = False  # non-USD currency in a USD field: keep, but lenient
        return value, clean, parsed.raw, unit
    if kind == "percent":
        parsed = patterns.parse_percent(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw, "percent")
    if kind == "bps":
        parsed = patterns.parse_bps(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw, "bps")
    if kind == "multiple":
        parsed = patterns.parse_multiple(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw, "x")
    if kind == "years":
        parsed = patterns.parse_years(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw, "years")
    if kind == "number":
        parsed = patterns.parse_number(text)
        return None if parsed is None else (parsed.value, parsed.clean, parsed.raw, field.unit)
    if kind == "integer":
        parsed = patterns.parse_number(text)
        if parsed is None or float(parsed.value) != int(float(parsed.value)):
            return None
        return int(float(parsed.value)), parsed.clean, parsed.raw, field.unit
    if kind == "boolean":
        parsed = patterns.parse_boolean(text)
        return None if parsed is None else (parsed.value, True, parsed.raw, None)
    if kind == "date":
        result = patterns.parse_date_text(text)
        return None if result is None else (result[0].isoformat(), True, result[1], None)
    if kind == "vocab":
        return _map_vocab(text, field, spec, ctx)
    # text
    return text, True, text, None


def _map_vocab(
    text: str, field: SchemaField, spec: FieldSpec, ctx: ExtractionContext
) -> tuple[str, bool, str, None] | None:
    """Controlled vocab: normalized exact -> alias -> fuzzy >= threshold;
    below threshold the field stays empty (the caller raises a vocab flag)."""
    vocab = field.controlled_vocab or []
    norm = normalize_text(text)
    if not norm:
        return None
    for entry in vocab:
        if norm == normalize_text(entry):
            return entry, True, text, None
    for entry, spellings in spec.vocab_aliases.items():
        if entry in vocab and any(norm == normalize_text(spelling) for spelling in spellings):
            return entry, True, text, None
    best_entry, best_ratio = None, 0.0
    for entry in vocab:
        ratio = fuzz.token_set_ratio(norm, normalize_text(entry))
        if ratio > best_ratio:
            best_entry, best_ratio = entry, ratio
    if best_entry is not None and best_ratio >= ctx.cfg.vocab_fuzzy_threshold:
        return best_entry, False, text, None  # lenient: fuzzy vocab map
    ctx.flags.append(
        ReviewFlag(
            category="vocab",
            description=(
                f"{field.header}: {text!r} did not map to the controlled vocabulary "
                f"(best ratio {best_ratio:.0f} < {ctx.cfg.vocab_fuzzy_threshold}); left empty"
            ),
            severity=FlagSeverity.warning,
            field=field.header,
        )
    )
    return None


# ---------------------------------------------------------------------------
# candidate gathering
# ---------------------------------------------------------------------------


def _table_pair_lookup(
    table, labels: list[str], fuzzy_threshold: int = 85
) -> tuple[str, float, str] | None:
    """Headerless 2-column style lookup: a row whose first non-empty cell
    matches and that has exactly one other non-empty cell."""
    located = patterns.find_table_row(table, labels, fuzzy_threshold)
    if located is None:
        return None
    row_idx, quality = located
    row = table.rows[row_idx]
    non_empty = [cell for cell in row if cell and str(cell).strip()]
    if len(non_empty) != 2:
        return None
    evidence = " | ".join(str(cell).strip() for cell in non_empty)
    return str(non_empty[1]).strip(), quality, evidence


def gather_candidates(
    spec: FieldSpec,
    field: SchemaField,
    kind: str,
    band_pages: list[PageContent],
    ctx: ExtractionContext,
) -> tuple[list[_Candidate], list[str]]:
    """All parseable candidates for one field across the band's pages, plus
    the raw texts whose label matched but whose value failed to parse."""
    candidates: list[_Candidate] = []
    failures: list[str] = []

    def add(text: str, quality: float, page: PageContent, from_table: bool, evidence: str, bbox=None) -> None:
        parsed = parse_value(text, kind, field, spec, ctx)
        if parsed is None:
            if kind not in ("vocab", "text"):  # vocab misses are flagged in _map_vocab
                failures.append(evidence)
            return
        value, clean, raw, unit = parsed
        candidates.append(
            _Candidate(
                raw_text=raw if kind != "text" else text,
                value=value,
                clean=clean,
                label_quality=quality,
                page=page,
                from_table=from_table,
                evidence=patterns.snippet(evidence, ctx.cfg.max_evidence_chars),
                bbox=bbox,
                unit=unit,
            )
        )

    row_labels = spec.table_row or spec.labels
    col_headers = spec.table_col or DEFAULT_VALUE_COLS
    # The headerless 2-column fallback only serves specs after THE value of a
    # metric; a spec pinned to a positional column (low/high/prior) must
    # never take a bare label|value pair for it.
    pair_ok = spec.table_col is None or bool(set(map(str.lower, spec.table_col)) & _PAIR_FALLBACK_COLS)
    for page in band_pages:
        for table in page.tables:
            cell = patterns.find_table_cell(table, row_labels, col_headers)
            if cell is not None:
                row = table.rows[cell.row_index]
                evidence = " | ".join(str(c).strip() for c in row if c and str(c).strip())
                add(cell.text, cell.quality, page, True, evidence, table.bbox)
                continue
            if not pair_ok:
                continue
            pair = _table_pair_lookup(table, row_labels)
            if pair is not None:
                text, quality, evidence = pair
                add(text, quality, page, True, evidence, table.bbox)
        for line in ctx.labeled_lines(page):
            quality = patterns.label_match_quality(line.label, spec.labels)
            if quality > 0.0:
                add(line.value, quality, page, False, line.line)

    return candidates, failures


# ---------------------------------------------------------------------------
# hit assembly
# ---------------------------------------------------------------------------


def _value_key(value: bool | int | float | str | None) -> object:
    return round(value, 6) if isinstance(value, float) else value


def build_hit(
    spec: FieldSpec,
    field: SchemaField,
    candidates: list[_Candidate],
    failures: list[str],
    ctx: ExtractionContext,
) -> FieldHit | None:
    """Best candidate -> FieldHit; conflicting values ride along and apply
    the ambiguity penalty; parse failures raise flags (never silent)."""
    if failures and not candidates:
        ctx.flags.append(
            ReviewFlag(
                category="parse",
                description=(
                    f"{field.header}: label matched but the value did not parse as "
                    f"{kind_for_field(field)} — {patterns.snippet('; '.join(failures), 160)}"
                ),
                severity=FlagSeverity.warning,
                field=field.header,
            )
        )
    if not candidates:
        return None

    distinct = {}
    for cand in candidates:
        distinct.setdefault(_value_key(cand.value), []).append(cand)
    has_conflicts = len(distinct) > 1

    def rank(cand: _Candidate) -> tuple:
        conf, _ = hit_confidence(
            ctx.cfg.confidence,
            label_quality=cand.label_quality,
            parse_clean=cand.clean,
            page=cand.page,
            from_table=cand.from_table,
            has_conflicts=has_conflicts,
        )
        return (
            -conf,
            not cand.from_table,
            cand.page.page_number if cand.page else 1_000_000,
        )

    ordered = sorted(candidates, key=rank)
    winner = ordered[0]
    confidence, components = hit_confidence(
        ctx.cfg.confidence,
        label_quality=winner.label_quality,
        parse_clean=winner.clean,
        page=winner.page,
        from_table=winner.from_table,
        has_conflicts=has_conflicts,
    )

    conflicts: list[ConflictingCandidate] = []
    seen_keys = {_value_key(winner.value)}
    for cand in ordered[1:]:
        key = _value_key(cand.value)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        conf, _ = hit_confidence(
            ctx.cfg.confidence,
            label_quality=cand.label_quality,
            parse_clean=cand.clean,
            page=cand.page,
            from_table=cand.from_table,
            has_conflicts=True,
        )
        conflicts.append(
            ConflictingCandidate(
                raw_text=cand.raw_text,
                value=cand.value,
                page=cand.page.page_number if cand.page else None,
                confidence=conf,
                evidence=cand.evidence,
            )
        )
        if len(conflicts) >= _MAX_CONFLICTS_KEPT:
            break

    return FieldHit(
        field=field.header,
        col_index=field.col_index,
        band=field.band,
        raw_text=winner.raw_text,
        value=winner.value,
        unit=winner.unit if winner.unit is not None else field.unit,
        page=winner.page.page_number if winner.page else None,
        bbox=winner.bbox,
        method="deterministic",
        confidence=confidence,
        evidence=winner.evidence,
        confidence_components=components,
        conflicts=conflicts,
    )


class SpecBandExtractor:
    """A band extractor defined entirely by its FieldSpecs.

    `requires_band` ties an extractor to a methodology band beyond its own
    (the RECOVERED FIELDS extractors only run when their methodology routed)."""

    def __init__(self, band: str, specs: list[FieldSpec], requires_band: str | None = None) -> None:
        self.band = band
        self.specs = specs
        self.requires_band = requires_band

    def extract(
        self,
        band_pages: list[PageContent],
        schema_fields: list[SchemaField],
        ctx: ExtractionContext,
    ) -> list[FieldHit]:
        by_header = {field.header: field for field in schema_fields}
        hits: list[FieldHit] = []
        for spec in self.specs:
            field = by_header.get(spec.header)
            if field is None:
                continue
            kind = kind_for_field(field)
            candidates, failures = gather_candidates(spec, field, kind, band_pages, ctx)
            hit = build_hit(spec, field, candidates, failures, ctx)
            if hit is not None:
                hits.append(hit)
        return hits


# ---------------------------------------------------------------------------
# label helpers used by band modules
# ---------------------------------------------------------------------------

_HEADER_UNIT_NOISE = (
    " ($M)", " ($M, local)", " ($M, USD)", " (x)", " (bps)", " (yrs)", " %", " Y/N", " (Current)",
    " (Prior Qtr)", " (Mult of EBITDA)",
)


def header_label(header: str) -> str:
    """Document-facing label derived from a schema header: unit decorations
    stripped ('Net Debt ($M)' -> 'Net Debt')."""
    label = header.replace("\U0001f511", " ").strip()
    for noise in _HEADER_UNIT_NOISE:
        label = label.replace(noise, "")
    return label.strip()


def spec(header: str, *extra_labels: str, **kwargs) -> FieldSpec:
    """FieldSpec shorthand: labels = stripped header + extras."""
    labels = [header_label(header), *extra_labels]
    return FieldSpec(header=header, labels=labels, **kwargs)
