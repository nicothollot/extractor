"""First-run / startup environment checks for the Phase-4 GUI.

Checks: Python package dependencies (core + gui extra, read from the
installed distribution metadata — never a hand-maintained list), optional
OCR dependencies, the built frontend bundle (Node is only required when a
build is missing), write permission on output_dir, and Claude Code
availability/auth (via the existing startup checks).

When config.first_run.install_missing_deps is true, `install_missing`
pip-installs the missing pins into THIS interpreter's environment (the
local .venv); otherwise callers present the exact commands to the user.
ANTHROPIC_API_KEY is never involved anywhere in setup.
"""

from __future__ import annotations

import importlib.metadata
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from pv_extractor.config import Config
from pv_extractor.io_guard import guarded_open_write

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER_RE = re.compile(r'extra\s*==\s*"(?P<extra>[^"]+)"')


class SetupItem(BaseModel):
    name: str
    ok: bool
    detail: str
    remediation: str | None = None  # exact command when not ok


class SetupStatus(BaseModel):
    items: list[SetupItem] = Field(default_factory=list)
    missing_packages: list[str] = Field(default_factory=list)  # exact pip requirement strings
    can_auto_install: bool = False
    install_command: str | None = None

    @property
    def all_ok(self) -> bool:
        return all(item.ok for item in self.items)


def _requirements_by_extra() -> dict[str | None, list[str]]:
    """Requirement strings from the installed pv-extractor distribution,
    grouped by extra (None = core)."""
    grouped: dict[str | None, list[str]] = {}
    try:
        requires = importlib.metadata.requires("pv-extractor") or []
    except importlib.metadata.PackageNotFoundError:
        return grouped
    for req in requires:
        marker = _EXTRA_MARKER_RE.search(req)
        extra = marker.group("extra") if marker else None
        spec = req.split(";")[0].strip()
        grouped.setdefault(extra, []).append(spec)
    return grouped


def _dist_installed(requirement: str) -> bool:
    match = _REQ_NAME_RE.match(requirement)
    if not match:
        return True  # unparseable spec: do not block startup on it
    name = match.group(1)
    # strip extras like uvicorn[standard]
    name = name.split("[")[0]
    try:
        importlib.metadata.version(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _check_packages(extra: str | None, label: str, status: SetupStatus) -> None:
    reqs = _requirements_by_extra().get(extra, [])
    missing = [r for r in reqs if not _dist_installed(r)]
    if missing:
        status.missing_packages.extend(missing)
        status.items.append(
            SetupItem(
                name=label, ok=False,
                detail=f"missing: {', '.join(missing)}",
                remediation=f"{sys.executable} -m pip install " + " ".join(f'"{m}"' for m in missing),
            )
        )
    else:
        detail = f"{len(reqs)} packages present" if reqs else "no requirements recorded"
        status.items.append(SetupItem(name=label, ok=True, detail=detail))


def default_frontend_dist() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "frontend" / "dist"


def frontend_dist_dir(config: Config) -> Path:
    return Path(config.gui.frontend_dist) if config.gui.frontend_dist else default_frontend_dist()


def collect_setup_status(config: Config, *, include_claude: bool = True) -> SetupStatus:
    status = SetupStatus(can_auto_install=config.first_run.install_missing_deps)

    _check_packages(None, "python core dependencies", status)
    _check_packages("gui", "python GUI dependencies", status)

    # OCR: rapidocr/onnxruntime are core deps; tesseract is the optional engine.
    ocr = config.extraction.ocr
    if ocr.engine == "tesseract":
        binary = ocr.tesseract_cmd or "tesseract"
        have_mod = _dist_installed("pytesseract")
        have_bin = shutil.which(binary) is not None
        status.items.append(
            SetupItem(
                name="ocr (tesseract engine)", ok=have_mod and have_bin,
                detail=f"pytesseract={'ok' if have_mod else 'missing'}, binary {binary!r}={'ok' if have_bin else 'missing'}",
                remediation=None if (have_mod and have_bin) else
                f"{sys.executable} -m pip install pytesseract  # plus a system tesseract install",
            )
        )
    else:
        status.items.append(
            SetupItem(name="ocr (rapidocr engine)", ok=_dist_installed("rapidocr"),
                      detail="models ship inside the rapidocr wheel — fully local, no downloads")
        )

    # Frontend bundle; Node is only needed when a build is required.
    dist = frontend_dist_dir(config)
    if (dist / "index.html").exists():
        status.items.append(SetupItem(name="frontend build", ok=True, detail=str(dist)))
    else:
        node = shutil.which("node")
        npm = shutil.which("npm")
        frontend_dir = dist.parent
        status.items.append(
            SetupItem(
                name="frontend build", ok=False,
                detail=(
                    f"no built bundle at {dist}"
                    + ("" if (node and npm) else " — Node.js/npm not found (required only to build)")
                ),
                remediation=f"cd {frontend_dir} && npm install && npm run build",
            )
        )

    # output_dir write permission (through the guard — pv_root is refused anyway).
    probe = Path(config.output_dir) / ".write_check"
    try:
        with guarded_open_write(probe, config.pv_root) as fh:
            fh.write("ok")
        probe.unlink(missing_ok=True)
        status.items.append(SetupItem(name="output_dir writable", ok=True, detail=str(config.output_dir)))
    except Exception as exc:  # noqa: BLE001 — a permission problem is a check result
        status.items.append(
            SetupItem(name="output_dir writable", ok=False,
                      detail=f"{type(exc).__name__}: {exc}",
                      remediation=f"grant write access to {config.output_dir} or change output_dir in config.yaml")
        )

    if include_claude:
        from pv_extractor.system.claude_code import run_startup_checks

        snapshot = run_startup_checks(config)
        for res in snapshot.results:
            status.items.append(SetupItem(name=f"claude {res.check}", ok=res.ok, detail=res.detail))

    if status.missing_packages:
        status.install_command = (
            f"{sys.executable} -m pip install " + " ".join(f'"{m}"' for m in status.missing_packages)
        )
    return status


def install_missing(config: Config, packages: list[str], *, timeout: int = 900) -> tuple[bool, str]:
    """pip-install exact requirement pins into this interpreter's
    environment. Refused (returns False + the manual command) when
    first_run.install_missing_deps is off."""
    if not packages:
        return True, "nothing to install"
    command = [sys.executable, "-m", "pip", "install", *packages]
    if not config.first_run.install_missing_deps:
        return False, (
            "first_run.install_missing_deps is false — run manually: " + " ".join(command)
        )
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pip launch failed: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output[-4000:]
