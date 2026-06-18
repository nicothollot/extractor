"""Performance smoke (D6): locate() answers in under two seconds against a
~250k-row index (about 40 clients x 60 deals x 4 periods x 26 files).

Synthetic FileRecord rows are constructed directly (derive_record is
bypassed for generation speed) with export-consistent normalized fields so
the FTS5 prefilter behaves exactly as in production. The real Angelo
Gordon/Accell fixture files are ingested via derive_record so the timed
query has genuine candidates to rank."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fixtures.build_fixture import FIXTURE_FILES
from pv_extractor.config import Config, load_config
from pv_extractor.indexer import db
from pv_extractor.indexer.derive import derive_record
from pv_extractor.locator.locate import locate
from pv_extractor.models import DocType, FileRecord, LocateQuery, ResolutionStatus, SourceClass
from pv_extractor.normalize import normalize_text

PROD_PV_ROOT = "\\\\hlhz\\dfs\\nyfva\\PV"

N_CLIENTS = 40
N_DEALS = 60
PERIODS = (date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30))
FILES_PER_PERIOD = 26  # 40 * 60 * 4 * 26 = 249,600 synthetic rows

_NAME_PATTERNS = (
    "{deal} Valuation Memo {label} v{n}.pdf",
    "{deal} IC Memo {label} ({n:03d}).pdf",
    "{deal} Portfolio Review {label} {n}.pdf",
    "{deal} Board Presentation {label} part {n}.pptx",
    "{deal} Financial Model {label} v{n}.xlsx",
    "{deal} Diligence Notes {label} {n}.docx",
)


def _client_name(i: int) -> str:
    return f"Client {i:02d} Capital"


def _deal_name(j: int) -> str:
    return f"Deal {j:03d} Holdings"


def _synthetic_records() -> Iterator[FileRecord]:
    for i in range(N_CLIENTS):
        client = _client_name(i)
        for j in range(N_DEALS):
            deal = _deal_name(j)
            for as_of in PERIODS:
                date_folder = f"{as_of.month}.{as_of.day}.{as_of.year % 100:02d}"
                label = date_folder
                folder = f"{PROD_PV_ROOT}\\{client}\\{deal}\\{date_folder}\\Client"
                norm_folder = normalize_text(folder.replace("\\", " "))
                mtime = datetime(as_of.year, as_of.month, as_of.day) + timedelta(days=20)
                for n in range(FILES_PER_PERIOD):
                    file_name = _NAME_PATTERNS[n % len(_NAME_PATTERNS)].format(
                        deal=deal, label=label, n=n
                    )
                    norm_name = normalize_text(file_name)
                    yield FileRecord(
                        file_name=file_name,
                        file_path=f"{folder}\\{file_name}",
                        folder_path=folder,
                        parent_folder="Client",
                        extension=file_name[file_name.rfind(".") :].lower(),
                        size_bytes=50_000 + n,
                        modified_time=mtime,
                        depth_from_pv_root=4,
                        normalized_file_name=norm_name,
                        normalized_folder_path=norm_folder,
                        normalized_full_path=f"{norm_folder} {norm_name}",
                        contains_memo_keyword="Memo" in file_name,
                        client=client,
                        deal=deal,
                        date_folder=date_folder,
                        as_of_date=as_of,
                        source_class=SourceClass.client,
                    )


def _fixture_accell_records(config: Config) -> Iterator[FileRecord]:
    """The synthetic fixture's Angelo Gordon/Accell files, re-rooted on the
    production share so the timed query ranks real candidates."""
    for rel, kind, mtime in FIXTURE_FILES:
        if not rel.startswith("Angelo Gordon/Accell/"):
            continue
        yield derive_record(
            PROD_PV_ROOT + "\\" + rel.replace("/", "\\"),
            size_bytes=0 if kind == "empty" else 200_000,
            modified_time=datetime.fromtimestamp(mtime),
            config=config,
        )


@pytest.mark.perf
def test_locate_stays_under_two_seconds_on_250k_rows(
    tmp_path: Path, project_root: Path
) -> None:
    config = load_config(project_root / "config.yaml")  # pv_root stays the prod share
    config.output_dir = tmp_path / "out"
    config.db_path = tmp_path / "out" / "perf.db"

    conn = db.open_db(config.db_path, config.pv_root)
    try:
        db.init_schema(conn)
        batch = config.indexer.batch_size
        total = db.insert_records(conn, _synthetic_records(), batch)
        total += db.insert_records(conn, _fixture_accell_records(config), batch)
        assert total >= 249_000
        assert db.count_files(conn) == total

        def query(client: str, deal: str) -> LocateQuery:
            return LocateQuery(
                client=client, deal=deal, period="2025-01-31",
                doc_type=DocType.valuation_memo,
            )

        locate(conn, config, query(_client_name(7), _deal_name(42)))  # warm-up, different deal

        start = time.perf_counter()
        result = locate(conn, config, query("Angelo Gordon", "Accell"))
        elapsed = time.perf_counter() - start

        assert result.status in (ResolutionStatus.FOUND, ResolutionStatus.AMBIGUOUS)
        assert elapsed < 2.0, f"locate took {elapsed:.2f}s on {total} rows"
    finally:
        conn.close()
