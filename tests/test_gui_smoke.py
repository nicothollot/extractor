"""Phase-4 Playwright smoke test (opt-in: PV_GUI_SMOKE=1).

Boots the real uvicorn app against the test fixture DB, then drives the built
frontend in headless Chromium. Two flows:
  * Single Search — the full 7-step New Run wizard (scope -> template -> model ->
    preflight -> confirm documents -> launch), then the review queue renders a
    flag with its evidence image.
  * Multi Search — the Single | Multi mode switch, add a firm, preview the
    firm-grouped document selection, then launch a (dry) multi-firm run.
Requires the frontend bundle (src/frontend/dist) and the Playwright chromium
browser; everything stays on 127.0.0.1 and the LLM fallback is disabled
throughout (no Claude Code CLI is ever launched).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

if os.environ.get("PV_GUI_SMOKE") != "1":
    pytest.skip("set PV_GUI_SMOKE=1 to run the GUI smoke test", allow_module_level=True)

pytest.importorskip("fastapi", reason="gui extra not installed")
pytest.importorskip("playwright.sync_api", reason="playwright not installed")
pytest.importorskip("uvicorn", reason="gui extra not installed")

from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST = PROJECT_ROOT / "src" / "frontend" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").exists(),
    reason="frontend bundle missing — cd src/frontend && npm install && npm run build",
)


@pytest.fixture(scope="module")
def gui_server(fixture_pv_root, tmp_path_factory):
    import uvicorn

    from pv_extractor.api.app import create_app
    from pv_extractor.config import load_config
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.scan_tree import scan_tree

    base = tmp_path_factory.mktemp("gui_smoke")
    (base / "config").mkdir()
    for rel in ("config.yaml", "config/models.yaml", "rules.yaml", "aliases.yaml"):
        (base / rel).write_text((PROJECT_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")

    config = load_config(base / "config.yaml")
    config.pv_root = str(fixture_pv_root)
    config.output_dir = base / "output"
    config.db_path = base / "output" / "pv_index.db"

    conn = open_db(config.db_path, config.pv_root)
    init_schema(conn)
    scan_tree(conn, str(fixture_pv_root), config)
    conn.close()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(config, config_path=base / "config.yaml")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _next(page: Page) -> None:
    page.get_by_role("button", name="Next →").click()


def test_single_run_full_wizard_and_review_evidence(gui_server: str) -> None:
    """The full Single Search wizard end to end: scope -> template -> model ->
    preflight -> confirm documents -> launch, then a flag with evidence."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(gui_server)

        # App boots against the fixture DB.
        expect(page.get_by_text("PV Extractor").first).to_be_visible()

        # New Run defaults to Single Search (the mode switch is present but Single
        # is selected) — the existing 7-step wizard.
        page.get_by_role("link", name="New Run").click()
        expect(page.get_by_role("button", name="Single Search")).to_be_visible()

        # --- Scope -> Template -> AI/model ---
        page.get_by_label("Scope").select_option("all")
        page.locator("select").filter(has_text="select period").select_option(index=1)
        _next(page)  # -> Template
        _next(page)  # -> AI/model
        expect(page.get_by_text("Model cost table")).to_be_visible()
        # LLM off: this smoke run must never touch the Claude CLI.
        page.get_by_text("LLM fallback enabled").click()
        _next(page)  # -> Preflight (step 3)

        # --- Preflight: run it, wait for coverage + the cost estimate ---
        page.get_by_role("button", name="Run preflight").click()
        expect(page.get_by_text("FOUND").first).to_be_visible(timeout=120_000)
        expect(page.get_by_text("Cost estimate (ESTIMATED)")).to_be_visible(timeout=60_000)

        # Next unlocks only once preflight is done; advance to Confirm documents.
        _next(page)  # -> Confirm documents (step 4)
        confirm = page.get_by_text("These look right — proceed to launch")
        expect(confirm).to_be_visible(timeout=30_000)
        confirm.click()  # docsConfirmed = true
        _next(page)  # -> Launch (step 5)

        # --- Launch step: the run button is now present + enabled ---
        launch = page.get_by_role("button", name="Launch run")
        expect(launch).to_be_enabled(timeout=10_000)
        launch.click()

        # --- progress lanes tick, then the run flows into the review queue ---
        expect(page.get_by_text("Pipeline lanes")).to_be_visible()
        expect(page.get_by_role("button", name="Review queue →")).to_be_visible(timeout=240_000)
        page.get_by_role("button", name="Review queue →").click()

        # --- review queue renders a flag with its evidence image ---
        expect(page.get_by_text("open item").first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Verbatim evidence")).to_be_visible()
        image = page.locator("img[alt^='page ']").first
        expect(image).to_be_visible(timeout=30_000)
        assert image.evaluate("el => el.complete && el.naturalWidth > 50")

        browser.close()


def test_multi_search_preview_and_dry_run(gui_server: str) -> None:
    """The Multi Search flow: switch modes, add a firm, set its period, preview
    the firm-grouped document selection, confirm, and launch a dry multi-run."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(gui_server)

        page.get_by_role("link", name="New Run").click()
        page.get_by_role("button", name="Multi Search").click()
        expect(page.get_by_role("heading", name="Multi Search")).to_be_visible()

        # Add a firm by name (fuzzy-resolved against the index).
        page.get_by_placeholder("Angelo Gordon, Ares, Apollo").fill("Angelo Gordon")
        page.get_by_role("button", name="Add").click()
        # The per-firm region renders; give it a period (free-text input).
        period = page.get_by_placeholder("Q1 2026").first
        expect(period).to_be_visible(timeout=30_000)
        period.fill("2025-01-31")

        # Preview: locate + verify every firm's documents (no writes).
        page.get_by_role("button", name="Preview", exact=True).click()
        # The firm-grouped preview resolves at least one document for the firm.
        expect(page.get_by_text("FOUND").first).to_be_visible(timeout=120_000)

        # Confirm + dry-run + LLM off, then launch the batch.
        page.get_by_text("These look right — ready to launch").click()
        page.get_by_text("Dry run only (locate + verify; nothing written)").click()
        page.get_by_text("LLM fallback enabled (escalated fields only)").click()
        launch = page.get_by_role("button", name="Launch multi dry run")
        expect(launch).to_be_enabled(timeout=10_000)
        launch.click()

        # The batch run opens the live progress view (lanes laned by firm).
        expect(page.get_by_text("Pipeline lanes")).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Angelo Gordon").first).to_be_visible(timeout=120_000)

        browser.close()


def test_settings_full_scan_completes_without_blanking(gui_server: str) -> None:
    """Drive a full index scan from Settings to completion. Regression guard for
    the blank-screen crash: the scan's final 'discovering deal folders…' event
    carries no `root`, which used to throw in the progress render (.split of
    undefined) and unmount the app. The error boundary card must never appear and
    the page must stay alive through completion, which also stamps last_scan."""
    import time

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(gui_server)
        page.get_by_role("link", name="Settings").click()
        # "Scan everything" guards with a window.confirm — accept it.
        page.on("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Scan everything").click()

        # Poll across the whole scan (incl. the deal-discovery phase) — the error
        # boundary card or a blanked shell at ANY point is a failure.
        done = False
        for _ in range(200):
            assert page.get_by_text("This screen hit an error").count() == 0, "Settings crashed during scan"
            assert page.get_by_text("PV Extractor").count() > 0, "app shell blanked during scan"
            if page.get_by_text("done in", exact=False).count() > 0:
                done = True
                break
            time.sleep(0.25)
        assert done, "scan did not report completion"
        # the completed scan renders its summary and stamps a last-scan time
        expect(page.get_by_text("done in", exact=False).first).to_be_visible()
        expect(page.get_by_text("last scan", exact=False).first).to_be_visible(timeout=10_000)

        browser.close()
