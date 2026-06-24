#!/usr/bin/env python3
"""First-run setup for PV Extractor: create .venv and install the package.

Stdlib-only on purpose — it runs before any dependency exists.

    python scripts/bootstrap.py            # .venv + editable install (.[dev])
    python scripts/bootstrap.py --with-gui # adds the gui extra; builds frontend if missing

Idempotent: re-running detects the existing .venv and skips completed steps —
but completeness is judged by REQUIRED_IMPORTS (every runtime dependency),
not just the package import, so re-running bootstrap after pulling a new
phase installs whatever dependencies the update added.
Every failure path prints a remediation message instead of a traceback.
(print() is allowed here: this is a script, not a library module.)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path

MIN_PYTHON = (3, 12)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = PROJECT_ROOT / ".venv"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "config.example.yaml"
_FALSE_VALUES = frozenset({"false", "no", "off", "0"})

# Import name of every runtime dependency in pyproject.toml. The venv counts
# as installed only when ALL of these resolve — an old venv from a previous
# phase then triggers a re-install instead of silently missing modules at
# runtime ("No module named docx"). Keep in sync with pyproject.toml and the
# $Probe list in bootstrap.ps1.
REQUIRED_IMPORTS = (
    "pv_extractor",
    "fitz",         # pymupdf
    "rapidfuzz",
    "pydantic",
    "typer",
    "dateutil",     # python-dateutil
    "rich",
    "yaml",         # PyYAML
    "openpyxl",
    "pdfplumber",
    "docx",         # python-docx
    "pptx",         # python-pptx
    "rapidocr",
    "onnxruntime",
)
GUI_IMPORTS = (
    "fastapi",
    "uvicorn",
    "ruamel.yaml",
)


class BootstrapError(RuntimeError):
    """A failure with a user-facing remediation message."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly; no side effects)
# ---------------------------------------------------------------------------


def extras_spec(with_gui: bool) -> str:
    """The editable-install requirement spec for pip."""
    return ".[dev,gui]" if with_gui else ".[dev]"


def required_imports(with_gui: bool) -> tuple[str, ...]:
    """Import probes required for the selected install profile."""
    return REQUIRED_IMPORTS + (GUI_IMPORTS if with_gui else ())


def venv_python_path(venv_dir: Path) -> Path:
    """The venv's python interpreter (Windows vs POSIX layout)."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def seed_config(config_path: Path, template_path: Path) -> None:
    """Create config.yaml from the version-controlled template on first run.
    config.yaml is git-ignored (machine-specific paths), so a fresh checkout
    has only the template. No-op when config.yaml already exists — a machine's
    own config is never overwritten."""
    if config_path.exists():
        return
    if not template_path.exists():
        return  # nothing to seed from; the config loader will error clearly
    config_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Seeded {config_path.name} from {template_path.name} — edit it for this machine "
          "(set output_dir to a local writable folder).")


def read_install_missing_deps(config_path: Path) -> bool:
    """first_run.install_missing_deps from config.yaml via a plain line scan
    (PyYAML may not be installed yet). Defaults to True when the file or the
    key is absent."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return True
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("install_missing_deps:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"").lower()
            return value not in _FALSE_VALUES
    return True


def python_remediation() -> str:
    found = ".".join(str(v) for v in sys.version_info[:3])
    wanted = ".".join(str(v) for v in MIN_PYTHON)
    return (
        f"Python >= {wanted} is required (this interpreter is {found}).\n"
        "Install a newer Python and re-run bootstrap with it:\n"
        "  Windows:        winget install Python.Python.3.12\n"
        "                  then:  py -3.12 scripts\\bootstrap.py\n"
        "  Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv\n"
        "  Or download from https://www.python.org/downloads/"
    )


def node_remediation() -> str:
    return (
        "Node.js and npm are required to rebuild the GUI frontend but were not found on PATH.\n"
        "Install Node.js 20+ from https://nodejs.org, or:\n"
        "  Windows:        winget install OpenJS.NodeJS.LTS\n"
        "  Debian/Ubuntu:  sudo apt install nodejs npm\n"
        "Then re-run: python scripts/bootstrap.py --with-gui"
    )


def install_remediation(venv_python: Path, with_gui: bool) -> str:
    spec = extras_spec(with_gui)
    return (
        "pv_extractor is not installed in .venv and first_run.install_missing_deps\n"
        "is false in config.yaml, so bootstrap will not install it. Either install\n"
        "manually:\n"
        f"  cd {PROJECT_ROOT}\n"
        f'  "{venv_python}" -m pip install -e "{spec}"\n'
        "or set first_run.install_missing_deps: true in config.yaml and re-run\n"
        "python scripts/bootstrap.py"
    )


# ---------------------------------------------------------------------------
# Steps with side effects
# ---------------------------------------------------------------------------


def ensure_venv() -> Path:
    """Create .venv if missing; return its python interpreter path."""
    py = venv_python_path(VENV_DIR)
    if py.exists():
        print(f"Found existing virtualenv: {VENV_DIR}")
        return py
    print(f"Creating virtualenv at {VENV_DIR} ...")
    try:
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    except OSError as exc:
        raise BootstrapError(
            f"Could not create the virtualenv ({exc}).\n"
            f'Try manually:  "{sys.executable}" -m venv "{VENV_DIR}"'
        ) from exc
    if not py.exists():
        raise BootstrapError(
            f"venv created but {py} is missing.\n"
            f'Delete "{VENV_DIR}" and try manually:  "{sys.executable}" -m venv "{VENV_DIR}"'
        )
    return py


def check_node_npm() -> None:
    """Verify node and npm are on PATH and respond to --version."""
    for tool in ("node", "npm"):
        path = shutil.which(tool)
        if path is None:
            raise BootstrapError(node_remediation())
        proc = subprocess.run([path, "--version"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise BootstrapError(f"`{tool} --version` failed (exit {proc.returncode}).\n{node_remediation()}")
        print(f"  {tool} {proc.stdout.strip()}")


def build_frontend() -> None:
    """npm install + production build of the Phase-4 GUI bundle (skipped
    when an up-to-date dist already exists is not checked — a rebuild is
    cheap and always safe)."""
    frontend_dir = PROJECT_ROOT / "src" / "frontend"
    npm = shutil.which("npm")
    assert npm is not None  # check_node_npm ran first
    for step, argv in (("npm install", [npm, "install"]), ("npm run build", [npm, "run", "build"])):
        print(f"  {step} (src/frontend) ...")
        proc = subprocess.run(argv, cwd=frontend_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            raise BootstrapError(
                f"{step} failed:\n  " + "\n  ".join(tail) + "\n"
                f"Retry manually:  cd {frontend_dir} && npm install && npm run build"
            )
    print(f"  frontend bundle ready: {frontend_dir / 'dist'}")


def frontend_dist_ready() -> bool:
    """The committed/built frontend bundle is present."""
    return (PROJECT_ROOT / "src" / "frontend" / "dist" / "index.html").exists()


def missing_imports(venv_python: Path, imports: tuple[str, ...] | None = None) -> list[str]:
    """Import entries that do not resolve inside the venv.
    Uses find_spec (no module code executed, so the probe stays fast)."""
    probe = (
        "import importlib.util, sys\n"
        "missing = []\n"
        "for n in sys.argv[1:]:\n"
        "    try:\n"
        "        ok = importlib.util.find_spec(n) is not None\n"
        "    except (ImportError, ValueError):\n"
        "        ok = False\n"
        "    if not ok:\n"
        "        missing.append(n)\n"
        "print(' '.join(missing))\n"
    )
    names = imports or REQUIRED_IMPORTS
    proc = subprocess.run(
        [str(venv_python), "-c", probe, *names], capture_output=True, text=True
    )
    if proc.returncode != 0:  # broken venv: treat everything as missing
        return list(names)
    return proc.stdout.split()


def install_package(venv_python: Path, with_gui: bool) -> None:
    spec = extras_spec(with_gui)
    print(f'Installing pv-extractor (editable, "{spec}") — this may take a minute ...')
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "-e", spec],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
        raise BootstrapError(
            "pip install failed:\n  " + "\n  ".join(tail) + "\n"
            "Retry manually:\n"
            f"  cd {PROJECT_ROOT}\n"
            f'  "{venv_python}" -m pip install -e "{spec}"'
        )


def print_next_steps(venv_python: Path) -> None:
    cli = venv_python.parent / ("pv-extractor.exe" if sys.platform == "win32" else "pv-extractor")
    print(
        "\nBootstrap complete. Next steps:\n"
        f'  Run the tests:   "{venv_python}" -m pytest -q\n'
        f'  Try the CLI:     "{cli}" locate --client "Angelo Gordon" --deal "Accell" '
        '--period "2025-01-31" --doc-type valuation_memo\n'
        f'  Analyst GUI:     "{cli}" gui   (127.0.0.1 only; opens the browser)'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="First-run setup for PV Extractor (.venv + editable install)."
    )
    parser.add_argument(
        "--with-gui",
        action="store_true",
        help="also verify node/npm, install the 'gui' extra and build the frontend bundle (Phase 4)",
    )
    args = parser.parse_args(argv)

    if sys.version_info < MIN_PYTHON:
        print(python_remediation())
        return 1
    print(f"Using Python {sys.version.split()[0]} ({sys.executable})")
    seed_config(CONFIG_PATH, CONFIG_TEMPLATE_PATH)

    try:
        if args.with_gui and not frontend_dist_ready():
            print("Built frontend bundle missing; checking Node.js / npm ...")
            check_node_npm()
            build_frontend()
        venv_python = ensure_venv()
        imports = required_imports(args.with_gui)
        missing = missing_imports(venv_python, imports)
        if not missing:
            print("pv_extractor and all dependencies already installed in .venv (skipping install).")
        else:
            print(f"Missing from .venv: {', '.join(missing)}")
            if not read_install_missing_deps(CONFIG_PATH):
                print(install_remediation(venv_python, args.with_gui))
                return 1
            install_package(venv_python, args.with_gui)
            still_missing = missing_imports(venv_python, imports)
            if still_missing:
                raise BootstrapError(
                    f"still missing after install: {', '.join(still_missing)}\n"
                    f"Retry manually:\n"
                    f"  cd {PROJECT_ROOT}\n"
                    f'  "{venv_python}" -m pip install -e "{extras_spec(args.with_gui)}"'
                )
        print_next_steps(venv_python)
        return 0
    except BootstrapError as exc:
        print(f"\nBootstrap failed:\n{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — never show a bare traceback
        print(
            f"\nBootstrap failed unexpectedly ({type(exc).__name__}): {exc}\n"
            "Re-run after fixing the issue; bootstrap is safe to re-run."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
