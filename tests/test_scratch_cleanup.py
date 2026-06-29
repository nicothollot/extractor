"""LlmScratchReaper: per-memo LLM working-dir cleanup that bounds peak disk on
large runs. Verifies the retention window, the keep-data allowlist, full delete,
and the containment guard (never deletes outside output_dir/<run_id>/llm/ or
under pv_root)."""

from __future__ import annotations

from pathlib import Path

from pv_extractor.llm.escalate import LlmScratchReaper


def _make_memo_dir(llm_root: Path, memo_id: str) -> Path:
    """Build a realistic per-memo working dir: heavy scratch + small data JSONs."""
    d = llm_root / memo_id
    (d / "pages").mkdir(parents=True)
    (d / ".claude").mkdir()
    (d / "pages" / "page_001.png").write_bytes(b"\x89PNG heavy")
    (d / "pages" / "page_002.txt").write_text("page text")
    (d / ".claude" / "session.json").write_text("{}")
    (d / "D01_memo.pdf").write_bytes(b"%PDF heavy source copy")
    (d / "prompt_call_task-001-t0.txt").write_text("prompt")
    (d / "schema_task-001-t0.json").write_text("{}")
    # data we keep
    (d / "manifest.json").write_text("{}")
    (d / "extracted_task-001-t0.json").write_text('{"field": 1}')
    (d / "answers_pv-run-memo-g0t0.json").write_text('{"answer": 1}')
    return d


def _reaper(run_dir: Path, *, retain: int, keep_data: bool = True, enabled: bool = True) -> LlmScratchReaper:
    return LlmScratchReaper(
        run_dir, pv_root=str(run_dir.parent / "pv_root"),
        enabled=enabled, retain=retain, keep_data=keep_data,
    )


def test_retention_window_prunes_oldest_first(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    llm_root = run_dir / "llm"
    dirs = [_make_memo_dir(llm_root, f"memo_{i}") for i in range(5)]
    reaper = _reaper(run_dir, retain=2)
    for d in dirs:
        reaper.on_memo_done(d)
    # First three are past the window -> heavy scratch gone; last two intact.
    for d in dirs[:3]:
        assert not (d / "pages").exists()
        assert not (d / ".claude").exists()
        assert not (d / "D01_memo.pdf").exists()
    for d in dirs[3:]:
        assert (d / "pages" / "page_001.png").exists()
        assert (d / ".claude").exists()


def test_keep_data_preserves_data_jsons_and_deletes_heavy(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    d = _make_memo_dir(run_dir / "llm", "memo_0")
    reaper = _reaper(run_dir, retain=0)  # prune immediately
    reaper.on_memo_done(d)
    assert d.exists()  # dir kept, only thinned
    assert (d / "manifest.json").exists()
    assert (d / "extracted_task-001-t0.json").exists()
    assert (d / "answers_pv-run-memo-g0t0.json").exists()
    # heavy / regenerable gone
    assert not (d / "pages").exists()
    assert not (d / ".claude").exists()
    assert not (d / "D01_memo.pdf").exists()
    assert not (d / "prompt_call_task-001-t0.txt").exists()
    assert not (d / "schema_task-001-t0.json").exists()


def test_keep_data_false_removes_whole_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    d = _make_memo_dir(run_dir / "llm", "memo_0")
    reaper = _reaper(run_dir, retain=0, keep_data=False)
    reaper.on_memo_done(d)
    assert not d.exists()


def test_disabled_reaper_is_noop(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    d = _make_memo_dir(run_dir / "llm", "memo_0")
    reaper = _reaper(run_dir, retain=0, enabled=False)
    reaper.on_memo_done(d)
    assert (d / "pages" / "page_001.png").exists()  # untouched


def test_containment_guard_refuses_outside_llm_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    (run_dir / "llm").mkdir(parents=True)
    # A path NOT under run_dir/llm must never be deleted.
    stray = tmp_path / "important"
    stray.mkdir()
    (stray / "keep.txt").write_text("do not delete")
    reaper = _reaper(run_dir, retain=0)
    reaper.on_memo_done(stray)
    assert (stray / "keep.txt").exists()


def test_containment_guard_refuses_under_pv_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "RUN_X"
    (run_dir / "llm").mkdir(parents=True)
    pv_root = tmp_path / "pv_root"
    victim = pv_root / "client" / "doc"
    victim.mkdir(parents=True)
    (victim / "source.pdf").write_bytes(b"%PDF")
    reaper = LlmScratchReaper(
        run_dir, pv_root=str(pv_root), enabled=True, retain=0, keep_data=False,
    )
    reaper.on_memo_done(victim)
    assert (victim / "source.pdf").exists()
