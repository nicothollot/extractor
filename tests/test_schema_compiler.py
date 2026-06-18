"""Schema compiler tests: drift (byte-stability), band carry-forward, slot
integrity, dtype/vocab spot checks against the workbook, and band routing."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from pv_extractor.models import SchemaField
from pv_extractor.schema.compile_schema import (
    BAND_ROUTING_FILENAME,
    MASTER_SCHEMA_FILENAME,
    compile_schema,
)


@pytest.fixture(scope="session")
def compiled(tmp_path_factory, master_workbook_path, default_config):
    out_dir = tmp_path_factory.mktemp("schema_out")
    fields, routing_doc = compile_schema(master_workbook_path, out_dir, default_config.pv_root)
    return fields, routing_doc, out_dir


@pytest.fixture(scope="session")
def fields(compiled) -> list[SchemaField]:
    return compiled[0]


@pytest.fixture(scope="session")
def routing(compiled) -> dict:
    return compiled[1]["routing"]


def field(fields: list[SchemaField], col: int) -> SchemaField:
    f = fields[col - 1]
    assert f.col_index == col
    return f


# --------------------------------------------------------------------------
# Drift: byte-identical recompiles, matching the committed artifacts
# --------------------------------------------------------------------------


def test_recompile_is_byte_identical(tmp_path, compiled, master_workbook_path, default_config):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    compile_schema(master_workbook_path, out_a, default_config.pv_root)
    compile_schema(master_workbook_path, out_b, default_config.pv_root)
    for name in (MASTER_SCHEMA_FILENAME, BAND_ROUTING_FILENAME):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_compile_matches_committed_artifacts(compiled, project_root: Path):
    _, _, out_dir = compiled
    for name in (MASTER_SCHEMA_FILENAME, BAND_ROUTING_FILENAME):
        committed = project_root / "schema" / name
        assert committed.is_file(), f"missing committed artifact {committed}"
        assert (out_dir / name).read_bytes() == committed.read_bytes(), (
            f"{name} drifted from the committed artifact; recompile schema/"
        )


# --------------------------------------------------------------------------
# Structure: field count, band carry-forward, slot integrity
# --------------------------------------------------------------------------


def test_field_count(fields):
    assert len(fields) == 604


def test_band_carry_forward(fields):
    assert field(fields, 1).band == "IDENTIFICATION"
    assert field(fields, 2).band == "IDENTIFICATION"  # row-1 cell is blank here
    assert field(fields, 604).band == "RECOVERED FIELDS (SUPPLEMENTAL)"


@pytest.mark.parametrize(
    ("group", "slot_count", "fields_per_slot"),
    [("TC", 15, 11), ("TX", 35, 4), ("CS", 10, 8)],
)
def test_slot_integrity(fields, group, slot_count, fields_per_slot):
    slotted = [f for f in fields if f.slot_group == group]
    assert len(slotted) == slot_count * fields_per_slot
    per_slot = Counter(f.slot_number for f in slotted)
    assert sorted(per_slot) == list(range(1, slot_count + 1))
    assert all(n == fields_per_slot for n in per_slot.values())


def test_slot_number_parsing(fields):
    tc01 = field(fields, 189)
    assert (tc01.header, tc01.slot_group, tc01.slot_number) == ("TC01 Name", "TC", 1)
    tx35 = field(fields, 490)
    assert (tx35.header, tx35.slot_group, tx35.slot_number) == ("TX35 Acquirer", "TX", 35)
    cs10 = field(fields, 566)
    assert (cs10.header, cs10.slot_group, cs10.slot_number) == ("CS10 Facility Name", "CS", 10)
    assert field(fields, 1).slot_group is None


# --------------------------------------------------------------------------
# Spot checks: required, vocabs, dtypes
# --------------------------------------------------------------------------


def test_identification_required(fields):
    memo_id = field(fields, 1)
    assert memo_id.header == "🔑 Memo ID"
    assert memo_id.required is True
    assert all(f.required == (f.band == "IDENTIFICATION") for f in fields)


def test_fund_strategy_vocab(fields):
    f = field(fields, 10)
    assert f.dtype == "enum"
    assert f.controlled_vocab == ["Core", "Core-Plus", "Value-Add", "Opportunistic", "Credit"]


def test_primary_methodology_vocab(fields):
    f = field(fields, 40)
    assert f.header == "Primary Methodology"
    assert f.dtype == "enum"
    assert len(f.controlled_vocab) == 9
    assert "DCF" in f.controlled_vocab
    assert "Recent Tx Price" in f.controlled_vocab


def test_secondary_methodology_copies_primary(fields):
    assert field(fields, 41).controlled_vocab == field(fields, 40).controlled_vocab


def test_entry_multiple_metric_copies_mult_metric(fields):
    assert field(fields, 75).header == "Entry Multiple Metric"
    assert field(fields, 130).header == "Mult Metric"
    assert field(fields, 75).controlled_vocab == field(fields, 130).controlled_vocab
    assert field(fields, 75).dtype == "enum"


def test_fv_hierarchy_level_compact_expansion(fields):
    f = field(fields, 31)
    assert f.dtype == "enum"
    assert f.controlled_vocab == ["Level 1", "Level 2", "Level 3"]


def test_tranche_rank_vocab(fields):
    f = field(fields, 495)
    assert f.header == "CS01 Tranche Rank"
    assert f.dtype == "enum"
    assert len(f.controlled_vocab) == 7
    assert f.controlled_vocab[0] == "Cash"


def test_mult_basis_year_is_not_enum(fields):
    f = field(fields, 131)
    assert f.header == "Mult Basis Year"
    assert f.dtype == "string"  # "LTM, NTM, FY+1, FY+2 etc." is a label list
    assert f.controlled_vocab is None


def test_supplemental_enums(fields):
    blend = field(fields, 592)
    assert blend.dtype == "enum"
    assert len(blend.controlled_vocab) == 4
    confidence = field(fields, 596)
    assert confidence.dtype == "enum"
    assert confidence.controlled_vocab == ["High", "Medium", "Low"]


@pytest.mark.parametrize(
    ("col", "dtype", "unit"),
    [
        (45, "number", "USD_millions"),  # Implied EV ($M)
        (103, "basis_points", "bps"),  # DCF Discount Rate Change (bps)
        (100, "percent", "percent"),  # DCF Discount Rate Mid %
        (54, "multiple_x", "x"),  # MOIC
        (144, "number", "millions_local"),  # Cap NOI Base ($M, local)
        (34, "number", "months"),  # Months Since 3P Corroboration
        (9, "integer", None),  # Fund Vintage
        (81, "years", "years"),  # Underwrite Holding Period (yrs)
    ],
)
def test_numeric_dtypes(fields, col, dtype, unit):
    f = field(fields, col)
    assert (f.dtype, f.unit) == (dtype, unit), f.header


@pytest.mark.parametrize("col", [5, 356, 501])  # Valuation Date, TX01 Date, CS01 Maturity Date
def test_date_dtypes(fields, col):
    f = field(fields, col)
    assert f.dtype == "date"
    assert f.unit is None


def test_threshold_flags_are_boolean(fields):
    flags = [f for f in fields if f.band == "THRESHOLD FLAGS"]
    assert len(flags) == 8
    assert all(f.dtype == "boolean" for f in flags)
    assert field(fields, 574).dtype == "boolean"


def test_yn_descriptions_are_boolean(fields):
    assert field(fields, 32).dtype == "boolean"  # "Y/N."
    assert field(fields, 192).dtype == "boolean"  # TC01 Include "NEW. Y/N."
    assert field(fields, 96).dtype == "boolean"  # header ends with "Y/N"


def test_enum_boolean_date_have_no_unit(fields):
    assert all(f.unit is None for f in fields if f.dtype in ("enum", "boolean", "date", "string"))


# --------------------------------------------------------------------------
# Band routing
# --------------------------------------------------------------------------


def test_routing_keys_come_from_primary_vocab(fields, routing):
    assert list(routing) == field(fields, 40).controlled_vocab


def test_routing_dcf(routing):
    assert routing["DCF"] == ["METHODOLOGY: DCF"]


def test_routing_multiple_market(routing):
    assert "METHODOLOGY: MULTIPLE" in routing["Multiple-Market"]
    assert "TRADING COMPS (POSITIONAL SLOTS)" in routing["Multiple-Market"]


def test_routing_multiple_transaction(routing):
    assert "METHODOLOGY: MULTIPLE" in routing["Multiple-Transaction"]
    assert "TRANSACTION COMPS (POSITIONAL SLOTS)" in routing["Multiple-Transaction"]


@pytest.mark.parametrize(
    ("methodology", "band"),
    [
        ("Cap Rate", "METHODOLOGY: CAP RATE"),
        ("Yield/Spread", "METHODOLOGY: YIELD / CREDIT"),
        ("Waterfall", "METHODOLOGY: WATERFALL / STRUCTURED EQUITY"),
        ("Cost+Accrued", "METHODOLOGY: YIELD / CREDIT"),
        ("Recent Tx Price", "CALIBRATION (ENTRY-PRICE ANCHOR)"),
        ("Cost", "CALIBRATION (ENTRY-PRICE ANCHOR)"),
    ],
)
def test_routing_targets(routing, methodology, band):
    assert band in routing[methodology]


def test_routing_band_lists_sorted_by_first_column(fields, routing):
    first_col = {}
    for f in fields:
        first_col.setdefault(f.band, f.col_index)
    for bands in routing.values():
        assert bands == sorted(bands, key=first_col.__getitem__)
