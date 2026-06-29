#!/usr/bin/env bash
# One-shot setup for PV Extractor on WSL / Linux / macOS.
#
# Guarantees a Python 3.12 environment even when your system Python is newer
# (3.13 / 3.14 may lack prebuilt wheels for pymupdf / onnxruntime / rapidocr),
# then hands off to scripts/bootstrap.py to create .venv and install everything.
#
#   ./scripts/setup.sh
#
# Idempotent: re-running detects the existing .venv and only installs what's
# missing. Nothing here touches your system Python — a fetched 3.12 is isolated.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "================================================"
echo " PV Extractor - setup (WSL / Linux / macOS)"
echo "================================================"

is_py312() {
  command -v "$1" >/dev/null 2>&1 && \
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1
}

PY=""
for cand in python3.12 python3 python; do
  if is_py312 "$cand"; then PY="$(command -v "$cand")"; break; fi
done

if [ -z "$PY" ]; then
  echo "No Python 3.12 found on PATH."
  echo "Fetching an isolated CPython 3.12 with 'uv' (does NOT change your system Python)..."
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    hash -r 2>/dev/null || true
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo
    echo "ERROR: could not install 'uv' automatically (no network, or download blocked)."
    echo "Install Python 3.12 yourself, then re-run this script:"
    echo "  Debian/Ubuntu/WSL:  sudo apt update && sudo apt install -y python3.12 python3.12-venv"
    echo "  macOS (Homebrew):   brew install python@3.12"
    echo "  or download from    https://www.python.org/downloads/"
    exit 1
  fi
  uv python install 3.12
  PY="$(uv python find 3.12)"
fi

echo "Using Python: $PY  ($("$PY" --version 2>&1))"
echo

# bootstrap.py builds .venv from the interpreter that runs it, installs the
# package (+gui extra), and seeds config.yaml from config.example.yaml.
"$PY" scripts/bootstrap.py --with-gui

echo
echo "================================================"
echo " Setup complete."
echo "   Start the analyst GUI:  .venv/bin/pv-extractor gui"
echo "   Health check:           .venv/bin/pv-extractor doctor"
echo " Edit config.yaml to point pv_root at your documents share."
echo "================================================"
