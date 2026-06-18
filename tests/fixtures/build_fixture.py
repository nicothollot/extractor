"""Synthetic PV share fixture (D6): a deterministic miniature of the real tree.

Covers the locator's hard cases: version chains (v1/v2/vf), same-name archive
duplicates, HL work-product lookalikes in Analysis folders, sequence-prefixed
and dot-prefixed date folders, '(00N)' copy decorations, 'DO NOT USE' files,
'+'-decorated deal and prior-period folders, loose files at the deal root,
month-name and FY date folders, zero-byte uploads, and a deal with no date
folders at all. PDFs are real 1-page documents (pymupdf) so content peeks
work in Phase 2; other extensions get small placeholder bytes. Every file
receives an explicit mtime because modified-time drives family tie-breaks.

This module lives in tests/ and writes only inside tests/fixtures; the src
read-only rules do not apply here, but the production share is still refused.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import fitz

from pv_extractor.io_guard import assert_write_allowed

_PRODUCTION_PV_ROOT = "\\\\hlhz\\dfs\\nyfva\\PV"

_PDF = "pdf"  # real 1-page PDF
_RAW = "raw"  # b"placeholder" bytes
_EMPTY = "empty"  # zero-byte file


def _mt(year: int, month: int, day: int) -> float:
    """Deterministic mtime epoch for a fixture file (noon, local time)."""
    return datetime(year, month, day, 12, 0, 0).timestamp()


# (relative posix path under the fixture root, content kind, mtime epoch).
# Exported so other tests (e.g. the perf smoke) can rebuild matching rows.
FIXTURE_FILES: tuple[tuple[str, str, float], ...] = (
    # --- Angelo Gordon / Accell: version chain + archive dup + HL lookalike
    ("Angelo Gordon/Accell/(5) 1.31.25/Client/Accell Valuation Memo 1.31.25 v1.pdf", _PDF, _mt(2025, 2, 10)),
    ("Angelo Gordon/Accell/(5) 1.31.25/Client/Accell Valuation Memo 1.31.25 v2.pdf", _PDF, _mt(2025, 2, 12)),
    ("Angelo Gordon/Accell/(5) 1.31.25/Client/Accell Valuation Memo 1.31.25 vf.pdf", _PDF, _mt(2025, 2, 14)),
    ("Angelo Gordon/Accell/(5) 1.31.25/Archive/Accell Valuation Memo 1.31.25 vf.pdf", _PDF, _mt(2025, 2, 14)),
    ("Angelo Gordon/Accell/(5) 1.31.25/Analysis/Accell 1.31.2025 Report v1.pdf", _PDF, _mt(2025, 2, 15)),
    ("Angelo Gordon/Accell/(5) 1.31.25/Analysis/Accell Valuation Memo 1.31.25.pdf", _PDF, _mt(2025, 2, 16)),
    ("Angelo Gordon/Accell/(3) 11.30.24/Client/Accell Valuation Memo 11.30.24.pdf", _PDF, _mt(2024, 12, 10)),
    ("Angelo Gordon/Accell/(3) 11.30.24/Client/Accell NDA.pdf", _PDF, _mt(2024, 12, 1)),
    ("Angelo Gordon/Accell/1. 11.30.25/Client/Accell Valuation Memo 11.30.25.pdf", _PDF, _mt(2025, 12, 10)),
    # --- Angelo Gordon / T.D. Williamson: DO NOT USE + (00N) copies
    ("Angelo Gordon/T.D. Williamson/(1) 9.30.24/Client/TDW Valuation Memo 9.30.24.pdf", _PDF, _mt(2024, 10, 20)),
    ("Angelo Gordon/T.D. Williamson/(1) 9.30.24/Client/TDW Valuation Memo 9.30.24 OLD DO NOT USE.pdf", _PDF, _mt(2024, 10, 25)),
    ("Angelo Gordon/T.D. Williamson/(2) 12-31-2025/Client/TDW Valuation Memo 12-31-2025 (002).pdf", _PDF, _mt(2026, 1, 10)),
    ("Angelo Gordon/T.D. Williamson/(2) 12-31-2025/Client/TDW Valuation Memo 12-31-2025 (003).pdf", _PDF, _mt(2026, 1, 12)),
    # --- Angelo Gordon / +Digital Edge: decorated deal folder
    ("Angelo Gordon/+Digital Edge/Q1 2026/Client/Digital Edge Valuation Memo Q1 2026.pdf", _PDF, _mt(2026, 4, 15)),
    # --- Apollo / Summit Ridge Energy: loose root file, FY folder, +Prior
    ("Apollo Global Management/Summit Ridge Energy/03-31-2026 SRE Valuation Memo_vf.pdf", _PDF, _mt(2026, 4, 20)),
    ("Apollo Global Management/Summit Ridge Energy/FY2025/Client/SRE Valuation Memo FY2025.pdf", _PDF, _mt(2026, 1, 20)),
    ("Apollo Global Management/Summit Ridge Energy/+Prior (8.31.24) Reports/SRE Valuation Memo 8.31.24.pdf", _PDF, _mt(2024, 9, 15)),
    # --- Apollo / AIOF II ANRP III: joint vehicle
    ("Apollo Global Management/AIOF II ANRP III/Q1 2026/Client/AIOF II ANRP III Portfolio Review Q1 2026.pdf", _PDF, _mt(2026, 4, 18)),
    # --- Apollo / Hyperoptic: month-name folder, three doc types
    ("Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic IC Memo Mar-26.pdf", _PDF, _mt(2026, 4, 10)),
    ("Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic Valuation Memo Q1 2026.pdf", _PDF, _mt(2026, 4, 11)),
    ("Apollo Global Management/Hyperoptic/Mar-26/Client/Hyperoptic Valuation Write Up Q1 2026.pdf", _PDF, _mt(2026, 4, 12)),
    # --- Angeles / Andover Storage: extension prior, zero-byte, 'old' doc
    ("Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Memo 03.2026.pdf", _PDF, _mt(2026, 4, 5)),
    ("Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Memo 03.2026.xlsm", _RAW, _mt(2026, 4, 6)),
    ("Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Summary empty.pdf", _EMPTY, _mt(2026, 4, 7)),
    ("Angeles Investments/Andover Storage/03.2026/Client/Andover Storage Valuation Memo old.doc", _RAW, _mt(2026, 4, 1)),
    ("Angeles Investments/Andover Storage/12.31.2024/Client/Andover NDA.pdf", _PDF, _mt(2025, 1, 5)),
    # --- Angeles / Carlsbad Desal: deal with no date folders anywhere
    ("Angeles Investments/Carlsbad Desal/Carlsbad overview.docx", _RAW, _mt(2025, 6, 1)),
    # --- Angeles / Blocked Deal: only a junk file (scan error injected by tests)
    ("Angeles Investments/Blocked Deal/9.30.24/Client/placeholder.txt", _RAW, _mt(2024, 10, 1)),
    # --- Blue Owl (Phase 2): docx portfolio review + xlsx valuation workbook
    ("Blue Owl/Mountain Peak Holdings/Q1 2026/Client/Mountain Peak Portfolio Review Q1 2026.docx", _RAW, _mt(2026, 4, 20)),
    ("Blue Owl/Riverbend Power/Q1 2026/Client/Riverbend Power Valuation Summary Q1 2026.xlsx", _RAW, _mt(2026, 4, 21)),
)


def _write_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def build_fixture(root: Path) -> None:
    """(Re)build the synthetic PV tree at `root`, deterministically.

    Safety before the destructive rmtree: the target must be outside the
    production PV share AND inside a tests/fixtures directory.
    """
    root = Path(root)
    assert_write_allowed(root, _PRODUCTION_PV_ROOT)
    assert "tests/fixtures" in str(root).replace("\\", "/"), (
        f"refusing to rebuild fixture outside tests/fixtures: {root}"
    )
    from fixtures.build_memos import RICH_BUILDERS  # late import: heavy deps

    if root.exists():
        shutil.rmtree(root)
    for rel, kind, mtime in FIXTURE_FILES:
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        builder = RICH_BUILDERS.get(rel)
        if builder is not None:  # Phase-2 realistic memo content (D8)
            builder(path)
        elif kind == _PDF:
            _write_pdf(path, path.stem)
        elif kind == _RAW:
            with open(path, "wb") as fh:
                fh.write(b"placeholder")
        else:  # _EMPTY: a zero-byte upload accident
            with open(path, "wb"):
                pass
        os.utime(path, (mtime, mtime))
