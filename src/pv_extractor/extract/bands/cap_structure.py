"""Slot band (D4): CAPITAL STRUCTURE (CS) — debt tranches from the cap
table. Slots fill by seniority (the Tranche Rank vocab order: Cash, 1L, 2L,
Unsec, Mezz, Pref Equity, Common Equity) then by notional size descending."""

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

_RANK_ALIASES: dict[str, list[str]] = {
    "Cash": ["cash", "cash & equivalents"],
    "1L": ["1l", "first lien", "senior secured", "senior", "1st lien"],
    "2L": ["2l", "second lien", "2nd lien"],
    "Unsec": ["unsecured", "senior unsecured", "unsec"],
    "Mezz": ["mezzanine", "mezz", "junior", "subordinated"],
    "Pref Equity": ["preferred equity", "preferred", "pref", "pref equity"],
    "Common Equity": ["common equity", "common", "ordinary equity"],
}
_RANK_ORDER = {rank: idx for idx, rank in enumerate(_RANK_ALIASES)}

_CS_COLUMNS = [
    ColumnSpec("Facility Name", ["facility", "facility name", "tranche", "instrument", "debt instrument"], "text", required=True),
    ColumnSpec("Tranche Rank", ["rank", "seniority", "priority", "lien"], "vocab"),
    ColumnSpec("Currency", ["currency", "ccy"], "text"),
    ColumnSpec("Notional ($M, local)", ["notional", "commitment", "amount", "principal"], "amount"),
    ColumnSpec("Drawn ($M, local)", ["drawn", "outstanding", "funded", "balance"], "amount"),
    ColumnSpec("Notional ($M, USD)", ["notional usd", "usd notional", "amount usd", "usd amount"], "amount"),
    ColumnSpec("Coupon Rate %", ["coupon", "coupon rate", "rate", "interest rate", "pricing"], "percent"),
    ColumnSpec("Maturity Date", ["maturity", "maturity date", "due"], "date"),
]

# A cap table must show size or tenor besides the facility name.
_CS_QUALIFYING = {"Notional ($M, local)", "Maturity Date", "Drawn ($M, local)"}


def _seniority_key(values: dict) -> tuple:
    rank_payload = values.get("Tranche Rank")
    rank = rank_payload[0] if rank_payload else None
    notional_payload = values.get("Notional ($M, local)") or values.get("Notional ($M, USD)")
    notional = float(notional_payload[0]) if notional_payload else 0.0
    return (_RANK_ORDER.get(rank, len(_RANK_ORDER)), -notional)


class CapStructureExtractor:
    band = "CAPITAL STRUCTURE (POSITIONAL SLOTS)"

    def extract(
        self, band_pages: list[PageContent], schema_fields: list[SchemaField], ctx: ExtractionContext
    ) -> list[FieldHit]:
        best: tuple[list[dict], PageContent, object, dict[str, float]] | None = None
        for page in band_pages:
            for table in page.tables:
                mapping = map_columns(table, _CS_COLUMNS)
                if mapping is None or not (_CS_QUALIFYING & mapping.keys()):
                    continue
                name_col = mapping["Facility Name"][0]
                rows: list[dict] = []
                for _, row in data_rows(table, name_col):
                    evidence = " | ".join(str(c).strip() for c in row if c and str(c).strip())
                    values: dict = {}
                    for spec in _CS_COLUMNS:
                        located = mapping.get(spec.subfield)
                        if located is None:
                            continue
                        cell = row[located[0]] if located[0] < len(row) else None
                        if cell is None or not str(cell).strip():
                            values[spec.subfield] = None
                            continue
                        parsed = parse_slot_cell(
                            str(cell), spec.kind,
                            vocab_map=_RANK_ALIASES if spec.subfield == "Tranche Rank" else None,
                        )
                        values[spec.subfield] = None if parsed is None else (*parsed, evidence)
                    rows.append(values)
                if rows and (best is None or len(rows) > len(best[0])):
                    quality = {sub: q for sub, (_, q) in mapping.items()}
                    best = (rows, page, table, quality)
        if best is None:
            return []
        rows, page, table, quality = best
        rows.sort(key=_seniority_key)  # seniority then size (D4)
        return emit_slot_hits(
            group="CS", entity_rows=rows, schema_fields=schema_fields,
            page=page, table=table, column_quality=quality, ctx=ctx,
        )


EXTRACTORS = [CapStructureExtractor()]
