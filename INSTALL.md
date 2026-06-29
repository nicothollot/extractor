# PV Extractor — installation & running

This is the onboarding guide for a fresh clone. It covers **WSL/Linux** (the
dev path) and **native Windows** (the simplest way to open the real PV share
`\\hlhz\dfs\nyfva\PV`). For the rules and architecture see `CLAUDE.md` and
`ARCHITECTURE.md`; for the Phase-3 LLM cost model see `README.md`.

> **The fastest path is the one-click setup** (`setup.bat` on Windows,
> `./scripts/setup.sh` on WSL/Linux/macOS). It creates `.venv`, installs
> everything, and — crucially — **provisions Python 3.12 for you if you don't
> have it** (the native deps need 3.12; 3.13/3.14 have no wheels yet). It never
> changes your system Python. The manual steps below are the same thing done by
> hand if you'd rather control each step.

---

## Prerequisites

| Need | Why | Required? |
| ---- | --- | --------- |
| **Python 3.12** (+ `venv`, `pip`) | runs the whole tool | **always** — but `setup` auto-installs an isolated 3.12 if you don't have one. Must be **3.12.x** (not 3.13/3.14: no wheels yet) |
| **Node.js 20+ / npm** | only to *rebuild* the web GUI bundle | only if you change frontend code (the built bundle is committed — see "Frontend bundle" below) |
| **Claude Code CLI** (`claude`), logged in | Phase-3 LLM fallback (optional) | only for the LLM second pass; the deterministic engine runs without it |
| **Access to `\\hlhz\dfs\nyfva\PV`** | the documents to extract | to run against the real share |

The tool is **read-only on the share** and makes **no external network calls**
(the GUI binds to `127.0.0.1` only; the LLM fallback runs the *local* `claude`
CLI, never an API key).

---

## A. WSL / Linux (the dev path)

```bash
# 1. clone, then from the repo root — one command does everything:
./scripts/setup.sh
#    Finds Python 3.12 (or fetches an isolated one via uv), creates .venv,
#    installs deps, and seeds config.yaml. The GUI bundle ships prebuilt, so
#    Node is NOT needed unless you change frontend code.

# 2. (optional) enable the LLM fallback — one time, reused after that
claude auth login

# 3. sanity check, then run
.venv/bin/pv-extractor doctor               # claude CLI/auth, model menu, schema artifacts
.venv/bin/pv-extractor gui                  # local GUI on http://127.0.0.1:8765 (opens browser)
```

Prefer to do it by hand (e.g. you already have 3.12)? The setup script just
wraps `bootstrap.py` with a guaranteed-3.12 interpreter:

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip
python3.12 scripts/bootstrap.py --with-gui   # MUST be 3.12 — 3.13/3.14 lack wheels
```

`setup.sh` / `bootstrap.py` are **idempotent** — re-run any time (e.g. after a
`git pull`) and only what's missing is installed.

### CLI instead of the GUI

```bash
.venv/bin/pv-extractor run --scope deal --client "Angelo Gordon" \
    --deal "Accell" --period "2025-01-31"          # full pipeline
.venv/bin/pv-extractor run --scope all --period "Q1 2026" --dry-run
.venv/bin/python -m pytest -m "not perf"           # the test suite
```

### Reaching `\\hlhz\dfs\nyfva\PV` from WSL

WSL does **not** auto-mount Windows network shares, so the committed
`pv_root` default won't resolve until you mount the share. Either:

```bash
sudo mkdir -p /mnt/pv
sudo mount -t drvfs '\\hlhz\dfs\nyfva\PV' /mnt/pv
```

then point `pv_root` at `/mnt/pv` (GUI → **Settings → Locations**, or edit
`config.yaml`). **If your goal is just to browse and extract from the real
share, running natively on Windows (section B) is simpler** — no mount needed.

---

## B. Native Windows (simplest for the real share)

On Windows the backend can open `\\hlhz\dfs\nyfva\PV` **directly** (UNC paths
work in the folder picker and the indexer), so this is the path of least
resistance for production use.

1. **Get the repo onto the machine.** Either `git clone`, or copy the folder
   (e.g. to `C:\dev\pv-extractor`). On the WSL dev box you can push a copy with
   `scripts/sync_to_windows.sh` — it ships the built GUI bundle so the Windows
   box needs no Node, and it never overwrites the destination `config.yaml`.
2. **Run setup once:** double-click **`setup.bat`**. It finds Python 3.12 (or
   installs an isolated 3.12 via uv — you do **not** need to install Python
   yourself), creates `.venv`, and installs everything. (Equivalent in
   PowerShell: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.)
3. **Every run after that:** double-click **`Start PV Extractor.bat`** — it
   starts the GUI and opens your browser.
4. **Point it at the share.** Make sure you can open `\\hlhz\dfs\nyfva\PV` in
   File Explorer first (so Windows has your credentials), then in the GUI:
   **Settings → Locations & file index → pv_root → Browse…**, navigate to
   `\\hlhz\dfs\nyfva\PV`, pick an output folder (e.g.
   `C:\Users\<you>\PVExtractorOutput`), **Save**, then scan the client folders
   you need. Or set it in `config.yaml` before launching:

   ```yaml
   pv_root: '\\hlhz\dfs\nyfva\PV'            # single quotes: backslashes stay literal
   output_dir: 'C:\Users\<you>\PVExtractorOutput'
   ```

> **Reading the share is allowed and is the whole point.** The read-only guard
> only ever blocks *writes* under `pv_root` (and hard-refuses *writing* to
> `\\hlhz\dfs\nyfva\PV` no matter what config says); pointing `pv_root` at it
> for reading is the normal production setup.

### Claude Code on Windows (optional, for the LLM fallback)

- If `claude` is installed **natively on Windows**, leave the default
  `claude_code.command: claude` and run `claude auth login` once.
- If `claude` lives only in **WSL**, bridge to it in `config.yaml` — note
  `wsl -e` skips the login shell, so use an **absolute** path:

  ```yaml
  claude_code:
    command: wsl
    command_args: [-e, /home/<you>/.local/bin/claude]   # absolute path
  ```

`pv-extractor doctor` (or the GUI's **Settings → Environment / doctor**) tells
you if the CLI, auth, or flags are missing. The deterministic extraction runs
fine without any of this — the LLM pass is a gap-filler.

> Native Windows end-to-end (the WSL→Windows Claude bridge especially) is
> implemented but **not yet verified on real hardware** — treat it as beta and
> report anything that breaks.

---

## Frontend bundle (why teammates usually don't need Node)

The built GUI lives in `src/frontend/dist/` and **is committed to git**, so a
teammate who pulls the repo can run the GUI with just Python — no Node. The
trade-off: **after any change to `src/frontend/`, the committed bundle must be
rebuilt and committed**, or pullers will see the old UI. To rebuild:

```bash
cd src/frontend && npm install && npm run build     # writes src/frontend/dist
# then commit src/frontend/dist along with the source change
```

`python scripts/bootstrap.py --with-gui` (and `bootstrap.ps1 -WithGui`) do this
rebuild for you when Node is present.

---

## Updating after a `git pull`

```bash
./scripts/setup.sh                  # re-runs setup; installs any new deps (idempotent)
# Windows: double-click setup.bat. If the pull changed src/frontend/ and you
# have Node, rebuild the bundle with: cd src/frontend && npm install && npm run build
```

Re-running an extraction against the same output workbook is idempotent
(already-extracted memos are skipped).

---

## Troubleshooting

- **pip can't install / "Requires-Python" error / missing wheels** → you're on
  the wrong Python. This beta needs **3.12.x**. Re-run `setup.bat` / `setup.sh`
  (it provisions an isolated 3.12), or delete `.venv` and run setup again.
- **`ensurepip`/venv errors on WSL** → `sudo apt install python3.12-venv`.
- **`No module named fastapi` when starting the GUI** → install the GUI extra:
  `.venv/bin/pip install -e ".[dev,gui]"` (or set
  `first_run.install_missing_deps: true` in `config.yaml` and the `gui` command
  self-installs it). This needs no Node — it uses the committed bundle.
- **GUI dropdowns are empty** → the file index is empty for that area. In
  **Settings → Locations & file index**, scan the client folders you need
  (selective scanning avoids walking the whole share).
- **`pv_root is not reachable`** → on WSL, mount the share (section A); on
  Windows, open the UNC path in File Explorer first to authenticate.
- **Anything Claude-related fails** → `.venv/bin/pv-extractor doctor`. The tool
  never needs `ANTHROPIC_API_KEY`; it reuses your local `claude auth login`.
