"""FastAPI application factory for the Phase-4 local GUI.

One process serves the JSON API under /api plus the built React frontend
(static files with an SPA fallback). The app binds 127.0.0.1 only — that
is enforced where uvicorn is launched (cli.gui) and by GuiConfig's
loopback validator. No telemetry, no external calls of any kind from this
layer; subprocesses are local provider CLIs triggered explicitly by setup or
LLM-assist endpoints."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from pv_extractor.api.jobs import JobManager
from pv_extractor.api.routes_core import router as core_router
from pv_extractor.api.routes_runs import router as runs_router
from pv_extractor.config import Config
from pv_extractor.system.setup_check import frontend_dist_dir

logger = logging.getLogger(__name__)


def create_app(config: Config, config_path: str | Path = "config.yaml") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jobs.attach_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="PV Extractor", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.config_path = Path(config_path).resolve()

    # Self-heal the index location: a db_path on a network/cross-boundary path
    # (e.g. \\wsl.localhost reached from native Windows, or a UNC share) can't
    # host SQLite WAL and breaks every read/scan. Relocate to a local copy in
    # this repo's output dir, cloning any existing index over, and persist the
    # new path so it sticks and Settings shows it.
    app.state.db_relocation = None
    try:
        from pv_extractor.indexer.db import relocate_db_if_needed

        local_fallback = app.state.config_path.parent / "output" / "pv_index.db"
        reloc = relocate_db_if_needed(Path(config.db_path), local_fallback)
        if reloc["relocated"]:
            logger.warning("index db relocated to local disk: %s", reloc["detail"])
            config.db_path = Path(reloc["path"])
            app.state.db_relocation = reloc
            try:
                from pv_extractor.api.yaml_edit import update_config_yaml

                update_config_yaml(app.state.config_path, config.pv_root, {"db_path": str(reloc["path"])})
            except Exception as exc:  # noqa: BLE001 — persisting is best-effort; the in-memory path already works
                logger.warning("could not persist relocated db_path to config.yaml: %s", exc)
    except Exception as exc:  # noqa: BLE001 — relocation must never block startup
        logger.warning("db relocation check failed (continuing with configured path): %s", exc)

    app.state.jobs = JobManager(config)

    app.include_router(core_router)
    app.include_router(runs_router)

    dist = frontend_dist_dir(config)

    # Cache strategy (so a new build is picked up WITHOUT a manual hard refresh):
    #   * index.html — never cache; the browser must always re-fetch it so it
    #     sees the new content-hashed asset filenames after an update.
    #   * /assets/* — Vite content-hashes these, so a new build = a new
    #     filename; cache them forever (immutable).
    _NO_STORE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    _IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

    def _index_response(index: Path) -> FileResponse:
        return FileResponse(index, headers=_NO_STORE)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):  # noqa: ANN202
        """Serve the built frontend; unknown non-API paths fall back to
        index.html (client-side routing)."""
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (dist / full_path).resolve() if full_path else dist / "index.html"
        # stay inside dist (no traversal), serve real files, else the SPA shell
        if (
            full_path
            and str(candidate).startswith(str(dist.resolve()))
            and candidate.is_file()
        ):
            if candidate.name == "index.html":
                return _index_response(candidate)
            headers = _IMMUTABLE if full_path.startswith("assets/") else None
            return FileResponse(candidate, headers=headers)
        index = dist / "index.html"
        if index.exists():
            return _index_response(index)
        return JSONResponse(
            {
                "detail": (
                    f"frontend bundle not found at {dist} — build it with "
                    "`cd src/frontend && npm install && npm run build`, or use the API under /api"
                )
            },
            status_code=503,
        )

    return app
