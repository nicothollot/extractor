# PV Extractor

Internal HL tool: locate client-provided valuation documents (IC memos,
valuation memos, portfolio reviews) on the PV network share and extract ~600
structured fields per memo into a master Excel index.

- **Phase 1** — schema compiler, SQLite file index (FTS5), document locator.
- **Phase 2** — deterministic extraction engine: document readers + local OCR,
  candidate-page targeting, peek-verifier, band extractors with a
  multiplicative confidence model, validation/QA, workbook writer, run
  orchestrator. Zero LLM calls.
- **Phase 3** — Claude Code CLI fallback (this section): a surgical LLM second
  pass for low-confidence / missing fields and OCR-hostile pages, with
  aggressive cost controls. The deterministic engine stays primary.

> **Beta release.** The setup scripts below provision everything (including the
> right Python) into a project-local virtualenv — they do not change your
> system Python.

## Setup (one click)

You need this repo and an internet connection. **You do NOT need to install
Python yourself** — setup fetches an isolated Python **3.12** if you don't
already have one (3.13/3.14 won't work yet: some native dependencies have no
wheels for them). Everything installs into a project-local `.venv`.

> **Pick the setup that matches where the repo lives.** If the repo is inside
> **WSL** (`\\wsl.localhost\...` / `~/...`), set it up **from a WSL terminal**
> with `./scripts/setup.sh` — the Windows `setup.bat` cannot run against a WSL
> path (`cmd.exe` rejects UNC working directories). If the repo is on a
> **Windows drive** (`C:\...`), use `setup.bat`. Don't cross them.

### Windows (PowerShell)

1. Clone/download this repo.
2. **Double-click `setup.bat`** — or in PowerShell from the repo root:
   `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
3. When it finishes, **double-click `Start PV Extractor.bat`** — the local GUI
   opens in your browser.

### WSL / Linux / macOS

```bash
./scripts/setup.sh             # finds/fetches Python 3.12, builds .venv, installs deps
.venv/bin/pv-extractor gui     # local analyst GUI on http://127.0.0.1
```

### What setup does
- Finds a Python **3.12** on your PATH; if none, installs an isolated 3.12 via
  [uv](https://docs.astral.sh/uv/) (your system Python is left alone).
- Creates `.venv` and installs the package + dependencies (editable).
- Seeds `config.yaml` from `config.example.yaml` on first run — **edit it** to
  point `pv_root` at your documents share (e.g. `\\hlhz\dfs\nyfva\PV`) and
  `output_dir` at a local writable folder.
- The web GUI bundle ships prebuilt, so **Node.js is not required** unless you
  change frontend code.

After setup, `pv-extractor doctor` checks the Claude Code CLI / auth / model
menu / schema artifacts. The LLM second pass is **optional** — the deterministic
engine runs without it; run `claude auth login` once to enable it.

See **`INSTALL.md`** for the manual/step-by-step path and prerequisites,
`CLAUDE.md` for the hard rules and module map, and `ARCHITECTURE.md` for the
current-state map.

## Phase 3 — Claude Code fallback

### No API keys, ever

Phase 3 does **not** use the Anthropic SDK and never reads
`ANTHROPIC_API_KEY` (any `ANTHROPIC_*` variable is stripped from the child
environment). Instead, the app launches **hidden local Claude Code sessions**:

    claude auth login        # once, in any terminal

After that one-time login the extractor reuses your local Claude Code
session via non-interactive print-mode calls (`claude -p … --model … --effort …`).
Think "the app opens hidden Claude Code terminals", not "the app calls an
API". `pv-extractor doctor` tells you if anything is missing.

By default the model **reads the source documents itself** (they're copied
into the call's working directory and the model opens them with its Read tool)
and **writes its answer to a JSON file** (`answers.json`) that the app then
reads and validates — a missing/malformed file is repaired by reprompting in
the *same* session. (Toggle `llm.file_based_output` off to fall back to the
older inline-schema StructuredOutput call.)

### What gets escalated, and what gets sent

A field only reaches Claude Code after the deterministic + local-OCR pass
scored it below `extraction.confidence_threshold` (default 0.75) or left a
required field empty. By default the LLM pass makes **one call per deal/period
over all that deal's documents combined** (`llm.combine_deal_documents`),
requesting the whole escalated field set at once.

- Each answer carries `{value, unit, page, verbatim_quote, confidence,
  not_found}`. The quote is checked against the cited page; on a clean match
  the value is grounded. A value whose quote can't be matched (common when the
  model lightly rewords text it read off a scanned page) is **surfaced as a
  low-confidence, flagged `UNGROUNDED_LLM_VALUE` for review** rather than
  silently dropped (`llm.surface_ungrounded_values`).
- `llm.confidence_selection` (Settings → LLM routing) governs this gating. On:
  values are quote-grounded and arbitration-gated, ungrounded ones capped low +
  flagged. Off: **trust the model** — every value is accepted at the model's own
  confidence (no cap, no grounding gate).
- A Claude Code value **never** overwrites a confident deterministic value.
  Every merge/overwrite/rejection is recorded in the memo's audit record
  (`output/<run_id>/audit/<memo_id>.json`), and a human-readable
  `extracted_*.json` (one row per field) is written next to each call's payload.
- Fields that genuinely fail are flagged `NOT_EXTRACTABLE` with reviewer
  attention — values are never invented.

### Review queue & auto-approval

The GUI review queue shows **every extracted value** (not just flagged ones),
each with its value, confidence, the model's quote, and the source page
auto-rendered. A **Needs approval | All values** switch filters the list. With
`llm.auto_approve_enabled` on (default), values at or above
`llm.auto_approve_confidence` (default 80%) are auto-approved and the rest —
plus any flagged item — land in *Needs approval* for a banker to sign off. Both
settings are editable in **Settings → LLM routing**.

### Model menu, AUTO/MANUAL routing

`config/models.yaml` is the single source of truth for the model aliases,
full ids and **editable** pricing assumptions (`pv-extractor models` shows
it; edit the file to change prices and update `last_reviewed`).

- **AUTO** (default): Haiku is reserved for cheap classification, Sonnet
  (medium) handles normal extraction, Opus (high/xhigh) runs only when the
  memo is OCR-hostile or fields still fail after the first pass. **Fable**
  is the most expensive tier and is never used unless you explicitly enable
  it (`llm.allow_fable: true`, or naming it with `--llm-model fable`).
- **MANUAL**: one model + effort forced for every LLM extraction, e.g.
  `--llm-model sonnet --llm-effort low`.

### Cost controls

- Hard per-run budget cap (`llm.budget_usd`, default **$25**; override with
  `--llm-budget`). When projected spend would exceed it, no more Claude Code
  jobs are submitted, the remaining memos are marked `LLM_DEFERRED`, and the
  run finishes cleanly.
- Responses are cached on `sha256(static prompt + page payload + field set +
  model + effort)` — re-running unchanged memos never re-pays (`--force-llm`
  bypasses).
- A small worker queue (default concurrency 2) launches the hidden sessions;
  every attempt lands in `output/<run_id>/llm/cost_ledger.jsonl` and the Run
  Log's "Batch Sessions" column records the `pv-<run_id>-<memo_id>-tN`
  job/session identifiers.
- When Claude Code reports token usage / cost, the ledger records **actual**
  numbers; otherwise tokens are estimated from the prompt and page images and
  clearly labeled **ESTIMATED** (`pv-extractor costs --run <id>` shows the
  split).

### Expected cost per memo (ESTIMATED)

Computed from the pricing assumptions seeded in `config/models.yaml`
(reviewed 2026-06-11) for a *typical* escalation: ~8 payload pages (~6,000
input tokens of text), 2 page images (~2,700 input tokens), ~30 escalated
fields (~2,500 output tokens) — about 9K input / 2.5K output per call. These
are planning estimates; your runs show actual numbers in the cost ledger
whenever Claude Code reports usage.

| Model (alias)     | $/MTok in/out | Est. cost per memo per pass |
| ----------------- | ------------- | --------------------------- |
| Claude Haiku 4.5 (`haiku`)   | $1 / $5    | ≈ $0.02 |
| Claude Sonnet 4.6 (`sonnet`) | $3 / $15   | ≈ $0.07 |
| Claude Opus 4.8 (`opus`)     | $5 / $25   | ≈ $0.11 |
| Claude Fable 5 (`fable`)     | $10 / $50  | ≈ $0.23 (tokenizes ~30% heavier → plan ≈ $0.30) |

Rule of thumb under the default AUTO routing: a Sonnet-only memo costs a few
cents; an OCR-hostile memo that needs both Opus passes ~$0.25. The default
$25 budget therefore covers roughly 100–300 memos per run depending on mix.
Update `config/models.yaml` when prices change — nothing here is hardcoded.

### Running

    # full pipeline with the Claude Code fallback (default)
    .venv/bin/pv-extractor run --scope deal --client "Angelo Gordon" \
        --deal "Accell" --period "2025-01-31"

    # pure Phase-2 behavior (no LLM at all)
    .venv/bin/pv-extractor run ... --no-llm

    # force one model/effort for every escalation; tighter budget
    .venv/bin/pv-extractor run ... --llm-model sonnet --llm-effort low --llm-budget 5

    # re-pay/re-run cached Claude Code responses
    .venv/bin/pv-extractor run ... --force-llm

    .venv/bin/pv-extractor models            # model menu + editable pricing
    .venv/bin/pv-extractor costs --run RUN_20260611_120000
    .venv/bin/pv-extractor doctor            # claude CLI / auth / flags / menu diagnosis

### Tests

No test launches the real Claude Code CLI by default — escalation tests run
against a fake client (`tests/fixtures/fake_claude.py`) and a fake `claude`
executable. One opt-in live test exists:

    PV_LIVE_CLAUDE_CODE_TESTS=1 .venv/bin/python -m pytest -m live

It runs a single tiny haiku/low call and is skipped unless the variable is
set **and** `claude auth status` passes.
