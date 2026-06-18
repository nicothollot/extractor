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

**New here / setting up a clone?** See **`INSTALL.md`** for step-by-step setup
on WSL and native Windows (including how to point `pv_root` at the real share
`\\hlhz\dfs\nyfva\PV`). See `CLAUDE.md` for the hard rules and module map and
`ARCHITECTURE.md` for the current-state map. Quick setup:

    python scripts/bootstrap.py --with-gui   # .venv + deps + GUI bundle (drop --with-gui if you have no Node)
    .venv/bin/pv-extractor doctor            # claude CLI, auth, model menu, schema artifacts
    .venv/bin/pv-extractor gui               # local analyst GUI on 127.0.0.1

## Phase 3 — Claude Code fallback

### No API keys, ever

Phase 3 does **not** use the Anthropic SDK and never reads
`ANTHROPIC_API_KEY` (any `ANTHROPIC_*` variable is stripped from the child
environment). Instead, the app launches **hidden local Claude Code sessions**:

    claude auth login        # once, in any terminal

After that one-time login the extractor reuses your local Claude Code
session via non-interactive print-mode calls
(`claude -p --output-format json --json-schema ... --model ... --effort ...`).
Think "the app opens hidden Claude Code terminals", not "the app calls an
API". `pv-extractor doctor` tells you if anything is missing.

### What gets escalated, and what gets sent

A field only reaches Claude Code after the deterministic + local-OCR pass
scored it below `extraction.confidence_threshold` (default 0.75) or left a
required field empty. Per memo, **one** Claude Code call per router tier
carries *all* escalated fields in a band-grouped strict JSON schema, and the
payload contains **pages, not documents**: the escalated fields' candidate
pages (from the Phase-2 page→band map) plus pages 1–3.

- TEXT pages travel as extracted text, tables serialized as markdown pipe
  tables.
- SCANNED / IMAGE_TABLE pages travel as PNG page images (≤1080 px long edge).
- Every answer must carry `{value, unit, page, verbatim_quote, confidence,
  not_found}`; the quote is machine-verified against the cited page — a quote
  that doesn't appear discards the value and raises `UNGROUNDED_LLM_VALUE`.
- A Claude Code value **never** overwrites a deterministic value with
  confidence ≥ threshold. Every merge/overwrite/rejection is recorded in the
  memo's audit record (`output/<run_id>/audit/<memo_id>.json`).
- Fields that fail every pass are flagged `NOT_EXTRACTABLE` with reviewer
  attention — values are never invented.

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
