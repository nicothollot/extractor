"""Slot bands (D4): TRADING COMPS (TC) and TRANSACTION COMPS (TX).

Comps are table rows. A trading-comps table qualifies when it has a company
name column and at least one EV-multiple column; a transaction-comps table
when it has acquirer + target columns. Rows sort by company/target name
(deterministic slot order, spec D4); aggregate rows (mean/median/...) never
occupy slots — the MULTIPLE band reads those separately.
"""

from __future__ import annotations

from pv_extractor.extract.bands.base import ExtractionContext
from pv_extractor.extract.bands.slots import (
    ColumnSpec,
    data_rows,
    emit_slot_hits,
    map_columns,
    parse_slot_cell,
)
from pv_extractor.models import FieldHit, PageContent, SchemaField
from pv_extractor.normalize import normalize_text

_TC_COLUMNS = [
    ColumnSpec("Name", ["company", "comparable", "comp", "name", "comparable companies"], "text", required=True),
    ColumnSpec("Ticker", ["ticker", "symbol"], "text"),
    ColumnSpec("Peer Subgroup", ["peer subgroup", "subgroup", "segment", "bucket"], "text"),
    ColumnSpec("Include", ["include", "included", "incl"], "boolean"),
    ColumnSpec("TEV ($M)", ["tev", "tev m", "enterprise value", "ev m"], "amount"),
    ColumnSpec("LTM EBITDA ($M)", ["ltm ebitda", "ltm ebitda m", "ebitda"], "amount"),
    ColumnSpec("EV/LTM EBITDA", ["ev ltm ebitda", "ev ebitda ltm", "ev ebitda"], "multiple"),
    ColumnSpec("EV/NTM EBITDA", ["ev ntm ebitda", "ev ebitda ntm", "ntm ebitda"], "multiple"),
    ColumnSpec("EV/CY+1 EBITDA", ["ev cy 1 ebitda", "cy 1 ebitda", "ev cy1 ebitda"], "multiple"),
    ColumnSpec("EV/CY+2 EBITDA", ["ev cy 2 ebitda", "cy 2 ebitda", "ev cy2 ebitda"], "multiple"),
    ColumnSpec("Beta", ["beta", "levered beta"], "number"),
]

_TX_COLUMNS = [
    ColumnSpec("Acquirer", ["acquirer", "buyer", "purchaser"], "text", required=True),
    ColumnSpec("Target", ["target", "company", "target company"], "text", required=True),
    ColumnSpec("Date", ["date", "announced", "announcement date", "close date"], "date"),
    ColumnSpec("EV/EBITDA", ["ev ebitda", "ev ltm ebitda", "multiple", "tv ebitda"], "multiple"),
]

# A TC table must carry at least one multiple column besides the name.
_TC_QUALIFYING = {"EV/LTM EBITDA", "EV/NTM EBITDA", "EV/CY+1 EBITDA", "EV/CY+2 EBITDA"}


def _extract_rows(
    table, mapping: dict[str, tuple[int, float]], colspecs: list[ColumnSpec], name_subfield: str
) -> list[dict]:
    name_col = mapping[name_subfield][0]
    rows = []
    for _, row in data_rows(table, name_col):
        evidence = " | ".join(str(cell).strip() for cell in row if cell and str(cell).strip())
        values: dict = {}
        for spec in colspecs:
            located = mapping.get(spec.subfield)
            if located is None:
                continue
            col_idx = located[0]
            cell = row[col_idx] if col_idx < len(row) else None
            if cell is None or not str(cell).strip():
                values[spec.subfield] = None
                continue
            parsed = parse_slot_cell(str(cell), spec.kind)
            values[spec.subfield] = None if parsed is None else (*parsed, evidence)
        rows.append(values)
    return rows


def _sort_key(subfield: str):
    def key(values: dict) -> str:
        payload = values.get(subfield)
        return normalize_text(payload[0]) if payload else "￿"

    return key


class TradingCompsExtractor:
    band = "TRADING COMPS (POSITIONAL SLOTS)"

    def extract(
        self, band_pages: list[PageContent], schema_fields: list[SchemaField], ctx: ExtractionContext
    ) -> list[FieldHit]:
        best: tuple[list[dict], PageContent, object, dict[str, float]] | None = None
        for page in band_pages:
            for table in page.tables:
                mapping = map_columns(table, _TC_COLUMNS)
                if mapping is None or not (_TC_QUALIFYING & mapping.keys()):
                    continue
                rows = _extract_rows(table, mapping, _TC_COLUMNS, "Name")
                if rows and (best is None or len(rows) > len(best[0])):
                    quality = {sub: q for sub, (_, q) in mapping.items()}
                    best = (rows, page, table, quality)
        if best is None:
            return []
        rows, page, table, quality = best
        rows.sort(key=_sort_key("Name"))  # comps fill slots by name (D4)
        return emit_slot_hits(
            group="TC", entity_rows=rows, schema_fields=schema_fields,
            page=page, table=table, column_quality=quality, ctx=ctx,
        )


class TransactionCompsExtractor:
    band = "TRANSACTION COMPS (POSITIONAL SLOTS)"

    def extract(
        self, band_pages: list[PageContent], schema_fields: list[SchemaField], ctx: ExtractionContext
    ) -> list[FieldHit]:
        best: tuple[list[dict], PageContent, object, dict[str, float]] | None = None
        for page in band_pages:
            for table in page.tables:
                mapping = map_columns(table, _TX_COLUMNS)
                if mapping is None:
                    continue
                rows = _extract_rows(table, mapping, _TX_COLUMNS, "Target")
                if rows and (best is None or len(rows) > len(best[0])):
                    quality = {sub: q for sub, (_, q) in mapping.items()}
                    best = (rows, page, table, quality)
        if best is None:
            return []
        rows, page, table, quality = best
        rows.sort(key=_sort_key("Target"))
        return emit_slot_hits(
            group="TX", entity_rows=rows, schema_fields=schema_fields,
            page=page, table=table, column_quality=quality, ctx=ctx,
        )


EXTRACTORS = [TradingCompsExtractor(), TransactionCompsExtractor()]
