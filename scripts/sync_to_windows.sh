#!/usr/bin/env bash
# Sync the WSL repo to the Windows copy (default C:\dev\pv-extractor).
#
# Includes src/frontend/dist (the built GUI) so Windows needs no Node, and
# excludes machine-local state. The Windows copy keeps its OWN config.yaml:
# it is never overwritten by a sync — only seeded once if missing, with
# Windows-appropriate defaults (UNC pv_root, ./output, claude via WSL).
#
#   scripts/sync_to_windows.sh [/mnt/c/dev/pv-extractor]
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-/mnt/c/dev/pv-extractor}"
mkdir -p "$DEST"

rsync -a --delete \
  --exclude '.venv/' \
  --exclude 'output/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude 'node_modules/' \
  --exclude 'tests/fixtures/pv_root/' \
  --exclude 'config.yaml' \
  "$SRC/" "$DEST/"

if [ ! -f "$DEST/config.yaml" ]; then
  # config.yaml is git-ignored; seed the Windows copy from this machine's
  # config.yaml if present, else from the version-controlled template.
  SRC_CONFIG="$SRC/config.yaml"
  [ -f "$SRC_CONFIG" ] || SRC_CONFIG="$SRC/config.example.yaml"
  # wsl -e skips the login shell (no ~/.local/bin on PATH), so the bridge
  # needs the absolute path of this machine's WSL claude binary.
  CLAUDE_BIN="$(command -v claude || echo claude)"
  "$SRC/.venv/bin/python" - "$SRC_CONFIG" "$DEST/config.yaml" "$CLAUDE_BIN" <<'PYEOF'
import sys
from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 120
src, dest, claude_bin = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, encoding="utf-8") as fh:
    data = yaml.load(fh)
data["pv_root"] = "\\\\hlhz\\dfs\\nyfva\\PV"
data["output_dir"] = "./output"
data["db_path"] = "./output/pv_index.db"
data["claude_code"]["command"] = "wsl"
data["claude_code"]["command_args"] = ["-e", claude_bin]
with open(dest, "w", encoding="utf-8", newline="\n") as fh:
    yaml.dump(data, fh)
print(f"seeded {dest} (UNC pv_root, ./output, claude bridged via WSL: {claude_bin})")
PYEOF
fi

echo "synced $SRC -> $DEST"
