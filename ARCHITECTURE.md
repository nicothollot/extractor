# PV Extractor — Architecture & Current State

> **Purpose.** This is the living "state of the repository" document. It explains
> *what exists today*, how the pieces fit together, and how the user-facing flows
> (notably the deal-selector dropdown) actually work — at a level of detail you
> can act on without re-reading every file.
>
> **⚠️ Maintenance directive (for Claude).** Whenever you change the code in a way
> that affects anything described here — a new module, a changed pipeline stage,
> a renamed endpoint, a new screen, a different deal-discovery rule, a config key,
> a new data contract — **update this file in the same change.** Keep section
> headings stable so diffs stay readable. If you add a subsystem, add a section.
> If you remove one, remove its section. Treat drift between this file and the
> code as a bug. `CLAUDE.md` remains the authoritative *rules/spec*; this file is
> the authoritative *current-state map*.
>
> _Last verified against the tree: see `git log`; the Search & Selection revamp
> (Phases G/A/B/C) landed across several commits on `main`, the latest being the
> Phase-C frontend commit `37b9ba4`._

---

## 1. What this tool is

Internal Houlihan Lokey tool. It **locates** client-provided valuation documents
(IC memos, valuation memos, portfolio reviews) on the PV network share and
**extracts ~600 structured fields per memo** into a master Excel index. It is
**read-only on the share** and runs the LLM fallback through the **local Claude
Code CLI** — never the Anthropic SDK, never an API key.

The project is organized in four phases, all present in the tree:

| Phase | Scope | Primary code |
| ----- | ----- | ------------ |
| **1** | Schema compiler, SQLite file index (FTS5), document locator | `schema/`, `indexer/`, `locator/` |
| **2** | Deterministic extraction engine (readers + local OCR, targeting, peek-verifier, band extractors, validation/QA, workbook writer, orchestrator) | `extract/`, `validate/`, `write/`, `run.py` |
| **3** | Claude Code CLI fallback — surgical LLM second pass for low-confidence/missing fields, with hard cost controls | `llm/` |
| **4** | Local web GUI (FastAPI + built Vite/React frontend) wrapping the same pipeline functions | `api/`, `src/frontend/` |

The deterministic engine is always primary; the LLM only ever touches fields the
deterministic pass escalated.

---

## 2. Quick facts (current snapshot)

- **Language/runtime:** Python ≥ 3.12 (src layout), pydantic v2 contracts.
- **Backend size:** ~91 Python files. Approx LOC by area:
  `extract/` ~3.7k · `api/` ~2.5k · `llm/` ~2.1k · `indexer/` ~1.4k ·
  `locator/` ~1.1k · `validate/` ~0.5k · `write/` ~0.35k · `schema/` ~0.46k ·
  `system/` ~0.45k. Root: `run.py` 721, `cli.py` 531, `models.py` 453,
  `config.py` 405.
- **Frontend:** Vite 6 + React 18 + TypeScript + Tailwind v4 + framer-motion,
  self-hosted Inter font (no CDN). 7 screens, ~9 shared components (the
  standalone Locator Review screen was retired — its workflow folded into the
  New Run wizard, §7). Built to `src/frontend/dist/` and served by the backend.
- **Tests:** **515 passing (2 skipped)** across **28 `test_*.py` files** under
  `tests/` (the perf module is deselected by `-m "not perf"`). No test launches
  the real Claude CLI by default (fakes are injected); one opt-in live test
  gated by `PV_LIVE_CLAUDE_CODE_TESTS=1`.
- **Git:** multiple commits on `main` — the Search & Selection revamp (Phases
  G/A/B/C) landed across several commits, latest the Phase-C frontend
  (`37b9ba4`).
- **Committed artifacts:** `schema/master_schema.json` + `schema/band_routing.json`
  (compiled, byte-stable), reference workbook in `reference/`.

---

## 3. The pipeline (end to end)

`run.py` orchestrates one run. Per memo, in order:

```
locate ─▶ verify ─▶ read ─▶ target ─▶ extract ─▶ validate ─▶ [LLM escalation] ─▶ write
```

1. **locate** (`locator/`): FTS5 prefilter → deterministic scoring cascade →
   resolution status (FOUND / AMBIGUOUS / NOT_FOUND / NOT_YET_UPLOADED /
   ACCESS_ERROR).
2. **verify** (`locator/verify.py`): peek-verifier opens the candidate, rejects
   HL work product / wrong-quarter / wrong-asset, and can upgrade AMBIGUOUS→FOUND.
3. **read** (`extract/readers/`): parse bytes (from `io_guard.open_read`) into
   `PageContent`; classify each page TEXT / SCANNED / IMAGE_TABLE / MIXED; OCR
   scanned pages locally.
4. **target** (`extract/targeting.py`): score pages against per-band anchor
   lexicons → top-K pages per band (+ pages 1–3); persist a page→band map (also
   used to route the Phase-3 LLM payload).
5. **extract** (`extract/engine.py` + `extract/bands/`): methodology-routed band
   extraction, multi-asset scoping, derived computation. Each value is a
   `FieldHit` with verbatim evidence + multiplicative confidence.
6. **validate** (`validate/`): schema checks, cross-field rules, QoQ continuity,
   hard-fail checks → QA verdict.
7. **LLM escalation** (`llm/`, optional): low-confidence/missing fields go through
   the Claude Code fallback (one call per memo per router tier). The plan
   BROADENS to every empty LLM-extractable field when an asset QA-fails (the
   engine recognized nothing / found no valuation value) or when the analyst
   sets **force LLM assist** (CLI `--force-llm-assist`, the wizard AI-step
   toggle, `LlmRunOptions.force_llm_assist`) — making the LLM the primary
   extractor; force-assist also bypasses the deterministic result cache.
8. **write** (`write/`): append rows to a COPY of the template workbook + per-memo
   audit JSON.

**Orchestration properties:** thread pool for I/O, per-memo failure isolation, a
`sha256 + schema + extractor-version` result cache, `--dry-run` coverage. Two
seams keep CLI behavior exact when unused: `llm_settings=None` = pure Phase-2;
`RunControl=None` = exact CLI behavior (the GUI passes a `RunControl` for live
progress events + cooperative cancel).

**Multi-firm (Phase C):** `run()` also accepts `slots: list[RunSlot]|None`.
`slots=None` is the legacy single run-wide-period path, byte-for-byte unchanged.
With slots, one work-item per `RunSlot`
(`client`/`deal`/`period`/`doc_type`/`doc_type_spec`/`firm`) is located with its
OWN period and doc-type; a bad period isolates to that slot's ERROR (the batch
still completes); ONE workbook is written for the batch; and every progress event
carries a firm `group` lane (the `None` path emits no group, so single-firm runs
stay flat).

---

## 4. Repository layout

```
pv-extractor/
  CLAUDE.md              authoritative rules + module map (the spec)
  ARCHITECTURE.md        THIS FILE — current-state map
  README.md              user-facing setup + Phase-3 cost docs
  config.yaml            all tunables (the live config; git-IGNORED, per-machine)
  config.example.yaml    committed config TEMPLATE; bootstrap seeds config.yaml
                         from it on first run (add new tunables to BOTH)
  config/models.yaml     Claude Code model menu + editable pricing
  aliases.yaml           client/deal alias expansions for the locator
  rules.yaml             cross-field validation rules + per-field range overrides
  schema/                compiled JSON artifacts (master_schema.json, band_routing.json)
  reference/             read-only input workbooks (master_index_v4.xlsx)
  scripts/               bootstrap.py / bootstrap.ps1 / sync_to_windows.sh
  Start PV Extractor.bat one-click Windows launcher
  src/pv_extractor/      backend package (see §5)
  src/frontend/          GUI (see §6); build output in dist/
  tests/                 28 test modules + fixtures/ synthetic PV tree generators
  output/                run outputs (generated; per-run dirs + gui/jobs.sqlite)
```

---

## 5. Backend subsystems (`src/pv_extractor/`)

### Core / shared
- **`models.py`** — pydantic v2 contracts shared across modules: `FileRecord`,
  `LocateResult`, `FieldHit`, `PageContent`, `VerifyResult`, `ReviewFlag`,
  `MemoResult`, `EscalationPlan`, `DealFolder`, `DealEvidence`, … This is the
  cross-module data vocabulary — change here ripples everywhere.
- **`config.py`** — typed `config.yaml` loader. Sections: paths, per-client
  `period_style`, `extraction`, `peek_verify`, `validation`, `deal_discovery`,
  `locator`, `llm`, `gui`. `DealDiscoveryConfig` holds the discovery weights.
- **`io_guard.py`** — read-only enforcement (Hard rule 1). `open_read` ('rb'
  only) for all share reads; `guarded_open_write` refuses `pv_root` targets and
  refuses the production share `\\hlhz\dfs\nyfva\PV` even if config points
  elsewhere. `tests/test_readonly_guard.py` greps src for stray write-mode opens.
- **`normalize.py`** — text/path normalization (lowercase, non-alphanumeric→space,
  collapse), version-signal parsing, `relative_segments`, `to_extended_path`
  (`\\?\` long-path prefix).
- **`logging_setup.py`** — JSONL logging to `output_dir/logs/`, UTF-8 forced.
  INFO logs never carry memo contents/client names/page payload (Hard rule 5).
- **`cli.py`** — typer CLI: `locate | run | ingest-xlsx | scan | deals | models |
  costs | doctor | gui | …`. The only place (with `scripts/`) allowed to
  `print()`/use rich.
- **`run.py`** — the orchestrator described in §3.

### `schema/` — schema compiler
- **`compile_schema.py`** — reads rows 1–3 of `reference/master_index_v4.xlsx`
  sheet "Index" → `schema/master_schema.json` (single source of truth, 604
  columns) + `schema/band_routing.json` (methodology → bands). Recompiling must
  produce **byte-identical** JSON (drift test). The workbook structure is never
  hand-mirrored in code.

### `indexer/` — SQLite file index + deal discovery
- **`db.py`** — SQLite schema (FTS5 search + `files` + `deal_folders` tables).
  Deal-relevant funcs: `deals_for_client`, `deal_folders_for_client`,
  `replace_deal_folders`, `update_file_deals`, `as_of_dates_for_deal`,
  `distinct_clients`. The revamp added three tables (all `CREATE TABLE IF NOT
  EXISTS`, additive/idempotent) + thin conn-first accessors:
  `deal_finder_feedback` (Phase-A deal-discovery corrections),
  `doc_type_profiles` (Phase-B learned/seeded Smart Search intents),
  `doc_search_feedback` (Phase-B ranking corrections).
- **`derive.py`** — re-derives every column in Python from `file_path` (the PV
  export's derived columns are corrupt — Hard rule 4). Legacy deal = `rel[1]`.
- **`periods.py`** — date-folder parsing (`parse_date_folder`), period labels,
  two-digit-year pivot at 70.
- **`ingest_xlsx.py`** — ingest the PV index export.
- **`scan_tree.py`** — walk the share into the index (iterative scandir stack,
  incremental: unchanged files skip on size+mtime, vanished paths deleted).
  Opt-in **quick rescan** (`quick=True`): skip re-listing unchanged LEAF folders
  — a folder that held no subfolders last scan and whose mtime predates the last
  completed scan (recorded per-root in `index_meta`, minus a clock-skew margin)
  can only re-confirm its indexed files, so its scandir round-trip is skipped.
  Correct for new uploads (any add/remove bumps the parent's mtime; every
  non-leaf folder is still walked); blind only to in-place same-name overwrites
  until the next full scan.
- **`deals.py`** — **smart deal discovery** (see §7.5); runs after every
  scan/ingest. Phase-A added ADMIN-wrapped containers, gated shared
  mixed-investment buckets, and multi-folder same-name merge (§7.5).
- **`deal_learning.py`** — **Phase-A per-client learning.** Records analyst
  corrections (`add_folder`/`remove_folder`/`merge`/`split`/`rename` via
  `record_correction`; `list_corrections`/`delete_correction`) and derives
  client-scoped layout priors (`derive_layout_priors`/`cached_layout_priors`,
  capped at `deal_discovery.learning.prior_bump`, cached in `index_meta` key
  `layout_priors:<client>`). `apply_feedback(deals, conn, config, client)` runs
  at the END of `refresh_deals` — hard pins/excludes/merge/rename/split, then
  capped prior nudges; a no-op when `deal_discovery.learning.enabled=false`. A
  correction on one deal generalizes to other new deals under the same client.

### `search/` — Phase-B Smart Search (rule-first, LLM-optional)
> **Mandatory property:** Smart Search is fully functional with the LLM OFF.
- **`doc_type_spec.py`** — CRUD over `doc_type_profiles` + builtins migrated from
  `locator.doc_type_keywords` (`builtin=1`, anchors re-derived live from config,
  forkable-not-deletable). `resolve_spec`, `seed_builtins`.
- **`intent.py`** — free-text → `DocTypeSpec`. A RULE layer always runs first and
  is self-sufficient (built-in financial-doc lexicon UNION
  `config.smart_search.intent_rules`; unknown queries fall back to their own
  tokens), plus an OPTIONAL local `claude -p` augmentation that only ADDS anchors
  and is fully try/except-wrapped — any failure (missing binary, not authed,
  timeout, malformed JSON, budget) degrades to the rule-only spec with no
  exception. `resolve_intent` returns `(spec, provenance)` where provenance is
  `rules` or `rules+cli`. Reuses `llm/claude_code_client` (no SDK/API key).
- **`rank.py`** — transparent additive scoring (BM25 over `filename_include` +
  rapidfuzz phrase blend + guarded regex anchors + folder context + extension
  prior + period evidence + negative penalty); deterministic score-desc/path-asc
  order; folds `doc_search_feedback` into bounded per-token `weight_overrides`.

### `locator/` — document locator
- Scoring cascade + resolution statuses (Phase 1). FTS5 prefilter (client+deal
  alias tokens) → deterministic Python scoring with per-component breakdowns
  (client/deal match, period match, doc-type keywords vs negatives, source-class
  gate, extension prior, version-family ranking).
- **Phase-B doc-type routing:** `LocateQuery` gains an optional
  `doc_type_profile` (slug); `ScoreContext` an optional `doc_type_spec`;
  `locate(conn, config, query, *, doc_type_spec=None)`. When a `DocTypeSpec` is
  supplied, the doc-type/negative scoring and the eligibility gate use its
  `filename_include`/`filename_regex`/`filename_exclude`/`folder_include`/
  `folder_exclude` + `weight_overrides` instead of the static
  `locator.doc_type_keywords` lookup. With `doc_type_spec=None`, builtin DocType
  behavior is byte-for-byte unchanged.
- **Period fallback (preflight).** When nothing matches the requested doc TYPE
  but real documents DO exist for the target period (right period, above floor,
  not a pure-negative like an NDA/invoice), `locate()` returns those as
  **AMBIGUOUS** candidates instead of a bare `NOT_YET_UPLOADED` with nothing to
  act on — so the analyst can pick/Replace in the Confirm-documents step. Checked
  AFTER the ACCESS_ERROR gate; off-able via
  `locator.surface_period_matches_without_doctype` (default on,
  `_has_period_evidence`).
- **`verify.py`** — Phase-2 peek-verifier: doc-class detection, in-file
  as-of/asset cross-check, AMBIGUOUS re-rank. Rejects HL letterhead/disclaimer
  files (Hard rule 2).
- **`overrides.py`** — Phase-4 learning table: analyst picks from the GUI locator
  review short-circuit `locate()` on the resolved key (still peek-verified).

### `extract/` — deterministic extraction engine
- **`patterns.py`** — shared parse toolkit (amounts with paren negatives/scale
  words/currencies, percent vs bps, multiples, dates, basis tags, label:value
  lines, fuzzy table-cell lookup, `LABEL_DISCRIMINATORS` qualifier tokens fuzzy
  match must never bridge).
- **`confidence.py`** — multiplicative `FieldHit` confidence
  (label × parse × page-class × table/prose × ambiguity), config-tunable.
- **`targeting.py`** — per-band anchor lexicons → top-K pages/band; persists the
  page→band map.
- **`engine.py`** — per-memo pipeline: read, classify, OCR, target,
  methodology-routed band extraction, multi-asset scoping, derived computation.
- **`cache.py`** — `extraction_cache` table (sha256 + schema ver +
  `EXTRACTOR_VERSION`).
- **`readers/`** — `pdf.py` (pymupdf text/tables + pdfplumber fallback,
  TEXT/SCANNED/IMAGE_TABLE/MIXED classification), `ocr.py` (RapidOCR default /
  pytesseract optional, 300dpi, word confidences), `docx.py` (`.doc` →
  UNSUPPORTED_FORMAT), `pptx.py`, `xlsx.py` (read-only).
- **`bands/`** — `base.py` (spec-driven extraction machinery) + one module per
  band family: fund, methodology, headline, bridge, dcf, multiple, cap_rate,
  yield_credit, waterfall, narrative; plus `slots.py`/`comps.py`/
  `cap_structure.py` (positional TC/TX/CS slots).
- **`derived.py`** — computed fields + extracted-vs-computed cross-check
  (Hard rule 7: Python computation wins; disagreement = cross-check flag).

### `validate/` — validation & QA
- **`checks.py`** (type/vocab/range from schema), **`rules.py`** (table-driven
  cross-field rules from `rules.yaml`), **`qoq.py`** (threshold flags vs the
  prior-period row), **`__init__.py`** (QA verdict: `qa_pass` /
  `qa_pass_with_flags` / `qa_fail`).

### `write/` — workbook writer + audit
- **`workbook.py`** — operates on a COPY of the template; asserts row-2 headers
  against the schema (hard abort on drift); appends by **column index**, never by
  header lookup; deduped Review Flags on `(memo_id, description)`; Run Log.
  Phase-4 entry points: `update_cell` (by Memo ID + col index), `resolve_flag`.
- **`audit.py`** — per-memo provenance JSON at
  `output_dir/<run_id>/audit/<memo_id>.json`. The GUI appends `review_actions`
  to the same files.

### `llm/` — Phase-3 Claude Code CLI fallback (see §8 for flow)
- `claude_code_client.py` (subprocess wrapper; strips `ANTHROPIC_*`; Windows→WSL
  bridge), `model_registry.py` (loads `config/models.yaml`, AUTO/MANUAL routing,
  cost math), `schema_builder.py` (escalated fields → strict band-grouped JSON
  schema + byte-stable static prompt), `payload.py` (pages-not-documents payload),
  `cache.py` (`llm_cache` table), `costs.py` (token estimator, JSONL ledger,
  `BudgetTracker`), `escalate.py` (worker queue, quote-grounding, merge policy),
  `deal_discovery.py` (opt-in Claude Code assist for deal discovery).

### `api/` — Phase-4 FastAPI backend (see §6 for endpoints)
- `app.py` (factory, static frontend + SPA fallback; `index.html` served
  `no-store` + content-hashed `/assets/*` served `immutable`, so a new build is
  picked up on the next load without a manual hard refresh), `jobs.py` (sqlite jobs +
  events, one pipeline run at a time, RunControl bridge; threads
  `RunRequest.exclude` through to `run()`; enriches the run summary digest with
  clients/deals/companies/source_files), `routes_core.py`
  (health/setup/doctor/index/config/models; `/fs/list` gained `?files=true`),
  `routes_runs.py` (runs, review queue, evidence, the `/runs/{id}/page-words/
  {memo}` endpoint [page geometry + selectable word boxes for the Add-Value
  highlighter], jobs+WS, locator, the
  `/jobs/{id}/selection` and `/runs/{id}/index-rows` endpoints). Services:
  `runs_service` (+`index_rows_mirror`; run summary now carries `started_at`/
  `finished_at` — from the `RunReport` for GUI runs, derived from the run-id
  timestamp + newest audit/workbook mtime for CLI runs), `review_service`
  (ReviewItem carries `reader` + `source_page_count` for the full-document
  viewer, `qa_fail_reasons` [the asset's hard-fail reasons, attached to every
  item of a failed memo], and an `add_value` action that writes a value with
  page/bbox/quote provenance — a `method="manual"` hit is upserted into the
  audit so the cell reads back with its evidence region), `evidence_service`
  (renders any page, bbox optional; `page_words()` returns PDF-point word boxes),
  `preflight_service`, `selection_service`
  (builds the Confirm-documents table by re-running locate()+peek-verifier per
  in-scope slot; `SlotSelection` gained `misfiled`/`detected_period`/
  `detected_as_of`, and `slot_selection(..., *, target=None,
  enhanced_period_check=False, doc_type_spec=None)` flags a slot whose best
  doc's in-file as-of (`VerifyResult.asof_date`) disagrees with the target as
  MISFILED with the document's true `detected_period` — off by default =
  unchanged), `multi_search_service` (NEW Phase-C: `expand_slots(conn, config,
  request) → list[RunSlot]` and the firm-grouped read-only
  `build_multi_selection(...)` — reuses `indexer.deals.refresh_deals` per-firm
  llm_assist, `deal_learning` corrections, `search.doc_type_spec.resolve_spec`,
  and `selection_service.slot_selection`; the selection preview is READ-ONLY on
  the learning table — corrections persist only on the run path, deduped),
  `yaml_edit` (ruamel, comment-safe), and `run_slots` (single-run slot fan-out:
  `needs_expansion`/`build_run_slots`/`resolve_doc_type` — a run naming multiple
  `RunRequest.doc_types` and/or `periods`, or a single non-enum doc-type slug,
  expands into one RunSlot per pair × doc type × period and runs through
  `run(slots=)`; `build_selection` previews one row per deal and reports
  `slot_count`/`doc_types`/`periods` for the Confirm banner). `jobs.py` gained
  `start_multi_run`/`_execute_multi_run` under the same single-active-pipeline
  guard (firm-grouped events, one workbook, run summary `scope="multi"` +
  `multi_search={firm_count, slot_count, firms}`), and `_execute_run` routes
  through `run_slots` when a single run names multiple doc types/periods. New
  endpoints: `GET /index/periods/expand` (range -> period list, each with as-of)
  and the prewritten Title-Cased doc-type catalog seeded into `doc_type_profiles`
  (Quarterly Report, Annual Report, Houlihan Valuation, Investor Presentation,
  Fund Report, Capital Account Statement, Financial Statements, Board Materials).
  `RunRequest.restrict_to_client_sourced` (default true; New Run toggle) flows to
  `LocateQuery`/`ScoreContext` to switch the HL-work REJECT + report/analysis
  penalty off ("rank only, never exclude").

### `system/` — self-checks
- `claude_code.py` (startup self-checks → `startup_checks.jsonl`), `doctor.py`
  (doctor checks shared by CLI + GUI), `setup_check.py` (first-run checks +
  guarded pip self-install).

---

## 6. Frontend & API surface (Phase 4)

**Server:** `pv-extractor gui` starts uvicorn bound to **loopback only**
(`GuiConfig` refuses any other host — there is no auth, no telemetry, no external
calls). It serves the built frontend from `src/frontend/dist`. All long
operations are **jobs** persisted in `output_dir/gui/jobs.sqlite` (events
replayable — reopening the browser reattaches to a running job).

**Screens** (`src/frontend/src/screens/`): Dashboard, **NewRun** (the 7-step
wizard, see §7), RunProgress (lanes / cost meter / log tail / cancel; a
completed non-dry run flows into the review queue automatically, with an
opt-out), ReviewQueue (j/k/a/e/u keyboard, evidence image, bulk accept, plus a
**full-document viewer** that pages the whole source PDF materializing only the
current page), OutputBrowser (run list with an inline digest + expandable
preview card per run; the per-run page is an in-depth summary + a filterable
**Index-rows preview** + Review Flags / Run Log mirrors), Guide, Settings
(locations + FolderPicker modal [folder or file mode], an **index-database
picker** [editable `db_path` + Browse for the `.db` file + "Detect existing
indexes" to adopt a found DB — the index is one gitignored SQLite file, point
machines at the same path to share it], selective per-client index scan with
live progress, a **Claude Code source picker** [Detect installs → radio-select
Windows-native vs WSL/Linux claude, persisted to `claude_code.command` +
`command_args`], raw config editor, **Learned locator overrides** admin panel).
The standalone **Locator Review** screen was removed — see §4/§7.

**Shared components** (`src/frontend/src/components/`): `DataTable` (sortable +
opt-in spreadsheet-style filtering: a global free-text box and per-column filter
inputs), `ProgressLanes`, `LogTail`, `ModelPricingTable`, `Stepper`,
`FolderPicker` (folder mode + opt-in `pickFiles` file mode), `charts`, `ui`
(Button/Card/Field/Panel/StatusChip/Toggle/inputCls), `branding`
(`HLLogo`/`HLMark`/`HLSpinner`/`HLLoading` — official HL logo SVGs from
`src/assets/`, unmodified; `HLSpinner` orbits a Sapphire arc around the static
globe mark, used on Run progress and scan-start states).
**Lib:** `lib/api.ts` (`get`/`post`/`put`/`del` + response types), `lib/hooks.ts`
(`useLoad`, `useJobPolling`, `fmtUsd`), `lib/wizard.tsx` (the New Run wizard
state context, lifted above the router so a tab switch and back keeps progress —
not persisted across a full page reload), `lib/scanJob.tsx` (the active
index-scan job id, also above the router — Settings re-subscribes and replays
events on remount so the scan status survives tab switches, and the provider
reattaches to any still-active scan job after a full reload; drives a pulsing
indicator on the Settings nav item), `lib/uiState.tsx` (a generic above-router
key/value store + `useStickyState` — a `useState` drop-in that keeps per-screen
UI state across tab switches: Settings form choices, Output/Review filters; not
across a full reload). **Theme:** `theme/tokens.css` is the single source of truth
for the palette — the authentic **Houlihan Lokey** brand (Oxford Blue `#002855`
anchor, Sapphire `#0067A5` / Tufts `#4f8bc9` / Azure `#24a4f2` accents, Roman
Silver `#7e8597` / Independence `#525766` neutrals, HL secondary status colors;
Segoe UI primary with self-hosted Inter fallback). Tailwind maps onto it in
`index.css @theme`. The app shell carries the official HL signature+mark logo
(`src/assets/hl-logo*.svg`); the favicon is the HL globe (`public/favicon.svg`).

**Key API endpoints** (mounted under `/api`, in `routes_core.py` /
`routes_runs.py`):
- Index meta: `/index/status`, `/index/clients`, `/index/clients-status`,
  `/index/deals?client=`, `/index/periods`, `/index/doc-types`,
  `/index/discover` (find existing `*.db` indexes in the db_path folder /
  output_dir / `./output`, each peeked read-only for file+client counts — the
  Settings "Detect existing indexes" / adopt-an-index flow). View endpoints
  (`/index/status`, `/index/clients-status`) open the DB via
  `db.open_db_readonly` (`mode=ro`, NO WAL — WAL fails with 'disk I/O error' on
  network paths like a DB reached over `\\wsl.localhost` or a UNC share) and
  degrade to a `db_error` field instead of a 500 when the DB can't be read.
  **Self-healing index location:** at GUI startup `create_app` calls
  `db.relocate_db_if_needed` — if the configured `db_path` can't host WAL here
  (`db.db_supports_wal` probe), it clones any existing index (SQLite online
  backup, read-only source so it's safe across a network path) to a local copy
  in the repo's `output/`, switches to it, persists the new path to
  `config.yaml`, and reports it via `/index/status.relocation` (shown in
  Settings). `db.open_db` also falls back WAL→DELETE journal so writes never
  hard-crash on a filesystem without WAL.
- Deal selector (see §7): `/index/deals`, `/index/deals/refresh` (POST job; now
  also accepts `apply_learning:bool=true`), `/index/search/clients`,
  `/index/search/deals`, `/index/search/periods`.
- Deal-discovery learning (Phase A): `POST /index/deals/feedback` (record a
  correction + re-discover; returns deals + learned priors), `GET
  /index/deals/learned?client=` (active priors + recorded corrections), `DELETE
  /index/deals/feedback/{feedback_id}`.
- Smart Search (Phase B): `GET /search/profiles`, `POST /search/profiles/resolve`,
  `POST /search/profiles`, `DELETE /search/profiles/{slug}`, `POST /search/preview`,
  `POST /search/feedback`.
- Multi-Search (Phase C): `POST /multi-search/selection` (firm-grouped read-only
  preview), `POST /multi-search/run` (batch; returns 409 if a pipeline run is
  already active).
- Scan: `/index/scan` (POST job, selective / one-root / full; `quick:true` =
  opt-in mtime-prune that skips unchanged leaf folders). **Deal-discovery mode:**
  `use_llm:false` = **Smart Scan** (deterministic heuristics only); `use_llm:true`
  = **LLM-Assisted Scan** — the end-of-scan `refresh_deals` runs the local
  `claude -p` deal-discovery pass with `llm_model`/`llm_effort` (aliases from
  `config/models.yaml`; corroborates/gap-fills, never removes). Both single- and
  multi-firm scans send these from the Settings scan UI's Smart | LLM-Assisted
  toggle (the old separate post-scan per-firm assist queue was retired).
- Runs & review: `/jobs/run` (POST; `dry_run:true` = preflight; `exclude` drops
  slots), `/jobs/{id}/preflight`, `/jobs/{id}/selection` (the Confirm-documents
  table: per-slot auto-selection + candidates + override flag), `/jobs/{id}` +
  WebSocket, review queue + evidence (`/runs/{id}/evidence/{memo}?page=`,
  bbox optional → also drives the full-document viewer), `/runs/{id}/index-rows`
  (this run's key Index columns + QA status).
- Locator: `/locator/locate`, `/locator/override` (POST), `/locator/overrides`
  (GET/DELETE), `/locator/verify-file` (POST — peek-verify an analyst-chosen
  file before recording it as an override), `/locator/open-folder`.
- Config/models: `/models`, `/models/{alias}/pricing` (PUT), `/config`
  (GET/PUT; editable: `pv_root`, `output_dir`, `db_path`, `claude_code.*`,
  `first_run.*`, `gui.*`, `llm.*`, `extraction.confidence_threshold`),
  `/config/raw` (validated raw `config.yaml` editor), `/templates`,
  `/fs/list?files=` (folder/file picker), health/setup/doctor.
- Claude source picker: `/claude/sources` (GET) — detects the reachable
  `claude` installs (this machine's PATH + a bridged WSL/Linux binary, the
  absolute WSL path resolved via a login-shell `command -v`), each probed with
  `--version`; Settings → Claude Code lets the analyst pick one, persisted as
  `claude_code.command` + `command_args` through `PUT /config`.

---

## 7. Deep dive: the New Run wizard (deal selector + Confirm documents)

> The New Run wizard (**`src/frontend/src/screens/NewRun.tsx`**) is a **7-step**
> flow: **Scope → Template → AI/model → Preflight → Confirm documents → Launch →
> Review**. All wizard state lives in a context store (`lib/wizard.tsx`) mounted
> above the router, so navigating to another tab and back keeps the analyst's
> progress (step, every field, the preflight job + estimate, document-selection
> edits); it is intentionally NOT persisted across a full page reload.
>
> - **Scope** (§7.1–7.5 below) — the deal selector, backed by `/index/deals*`
>   and `/index/search/*` and the discovered `deal_folders` table
>   (`indexer/deals.py`). Period selection is dropdown-first everywhere (driven
>   by `/index/periods`) with a free-text fallback.
> - **Template / AI-model / Preflight** — unchanged behavior; preflight is a
>   dry-run job + server-side cost ESTIMATE and must complete before Confirm.
> - **Confirm documents** (§7.6) — curate exactly the files the locator
>   auto-selected before launch.
> - **Launch** — gated on a completed preflight AND a confirmed selection;
>   removals ride on `RunRequest.exclude`. Launching opens the live progress view.
> - **Review** — a completed (non-dry) run flows into the Review Queue for that
>   run; RunProgress auto-navigates there (opt-out), and the wizard's Review step
>   links to it if the analyst returns to New Run.
>
> **Single | Multi Search (Phase C).** New Run gained a top-level mode switch
> (`lib/wizard.tsx` `searchMode`, default `"single"` + a `FirmEntry[]` multi
> state). **Single** = the 7-step wizard above, unchanged. **Multi** =
> comma/Browse firm entry with per-firm regions (`FirmRegion.tsx` +
> `DocTypePicker.tsx`: deal multi-select, deal-folder add/remove, period, a Smart
> Search doc-type picker, per-firm `llm_assist`/`enhanced_period_check`/
> `deal_search_model`), a firm-grouped Confirm preview (misfiled badges), and
> launch via `POST /multi-search/run`; RunProgress/`ProgressLanes` lane by firm
> `group` when present (flat/unchanged when absent). The Multi flow consumes the
> `/multi-search/*` endpoints (§6). The Settings index-scan UI gained a parallel
> Single | Multi multi-firm scan switch feeding the existing `{clients:[…]}`
> scan body. See §7.7 for Smart Search and §7.8 for Multi-Search.

### 7.1 Where deals come from (the data behind every dropdown)

The dropdown options are **not** raw folder names — they are **discovered deal
folders**. Discovery (`indexer/deals.py`, §7.5) runs after every scan/ingest,
classifies each path segment, walks the client tree, emits confidence-scored
deals into the `deal_folders` table, and rewrites `files.deal`. Everything the
selector shows (names, confidence %, folder paths, period/file counts,
LLM-corroborated flag) comes from that table via:
- `db.deals_for_client(conn, client)` → list of deal **names** (the `<option>`s).
- `db.deal_folders_for_client(conn, client)` → full `DealFolder` detail, shaped
  by `_deal_folder_payload()` into `{name, confidence, method, low_confidence,
  folder_paths, periods, file_count, memo_file_count, llm_corroborated}`.

### 7.2 Three discovery modes (a segmented toggle)

When scope ≠ "all", the wizard shows a **Folder discovery** toggle with three
modes (`discoveryMode` state: `"browse" | "search" | "llm"`):

**A. Browse** (default) — pick from discovered dropdowns.
- **Client** `<select>` ← `GET /api/index/clients`. Changing it resets the deal
  and clears any LLM job.
- **Deal** `<select>` ← `GET /api/index/deals?client=<c>` (`deals.data.deals`).
  Each option appends `· low confidence` when its `deal_folders` entry has
  `low_confidence` (confidence < `deal_discovery.review_confidence`). If a client
  has **zero** discovered deals, a warning suggests the LLM-assist mode.

**B. Search by name** — debounced fuzzy lookup as you type (`useDebounced`, 300ms).
- Client: `GET /api/index/search/clients?q=` → `{matches:[{client,score}]}`,
  rendered as a clickable list (selecting sets `client`, clears query).
- Deal: `GET /api/index/search/deals?client=&q=` → `{matches: DealFolderInfo[]}`,
  each row showing confidence %, fuzzy match score, **and the full folder
  path(s)** so the analyst can confirm the right one. (Backend expands the query
  through `aliases.yaml` deal expansions before fuzzy-matching.)
- Period: `GET /api/index/search/periods?client=&deal=&q=` → parses free-text
  ("Q1 2025", "3.31.25", "FY2025"), returns `resolved_as_of`/`resolved_label`,
  a `parse_error` if unparseable, and the deal's indexed periods closest-first
  with an `exact` flag.

**C. LLM assist** — a hidden local Claude Code session maps the client folder.
- Model picker defaults to the `sonnet` alias (floats to the current cheap tier).
- "Discover deal folders" → `POST /api/index/deals/refresh {client, llm:true,
  llm_model}` starts a **background job** (`deal_discovery`). The wizard polls it
  via `useJobPolling`; on completion it lists the proposed deals (name, confidence,
  method, folder paths) as clickable options and calls `deals.reload()` so the
  Browse dropdown also picks up any newly-persisted deals.
- Backend: `refresh_deals(..., use_llm=True)` → `llm/deal_discovery.py` sends
  **one** Claude Code call per client over a folder **inventory** (paths + counts
  + sample file *names*, never contents). Ungrounded/invented paths are discarded;
  heuristic deals are never removed, only corroborated (+confidence) or gap-filled.
- **Re-run guard.** Every successful LLM assist (including a corroboration-only
  pass, which leaves all rows `method="heuristic"`) is stamped in `index_meta`
  key `llm_discovery:<client>` (`{model, effort, at, deals}`) by `refresh_deals`.
  `db.last_llm_discovery(conn, client)` prefers that stamp (falling back to the
  legacy per-deal `claude-code:%` method scan), is surfaced on `GET
  /api/index/deals` as `last_llm_discovery`, and the wizard's `startLlmDiscovery`
  uses it to **warn before paying for a re-run** (OK = replace, Cancel = keep the
  existing discovery and pick from it in Browse).

### 7.3 The selected-deal confirmation card

Independent of mode, once a deal is selected the wizard renders a confirmation
card (from `selectedDealInfo`, matched out of `deals.data.deal_folders` by name)
showing: deal name, confidence % (green or warn if low-confidence), period count,
file count, an "LLM-corroborated" tag when applicable, and the **actual folder
path(s)** on the share. This is the "did I pick the right folder?" check.

### 7.4 Period selection

In Browse/LLM modes (and in Search mode when scope ≠ "deal"), the period is a
`<select>` from `GET /api/index/periods` (scoped by client+deal when available),
**plus** a free-text input ("Q1 2026", "2025-01-31") writing the same `period`
state. In Search mode for a deal, period uses the fuzzy `search/periods` list
described above. `scopeValid` requires a non-empty period and the
scope-appropriate client/deal before "Next" unlocks.

### 7.5 How discovery actually classifies & walks (`indexer/deals.py`)

The legacy "deal = first segment under client" rule fails on real trees (deals
under strategy groups, under project codenames, or **below** period folders; some
clients have no deals). So discovery:

1. **Builds** the client's folder subtree from the index into `_Node`s.
2. **Classifies** each segment (`_classify`) into one of four roles — precedence
   PERIOD → ADMIN → STRUCTURAL → NEUTRAL. Each name is first split by
   `_name_and_period` into its investment-name part and any date it carries:
   - **PERIOD** — the name is PURELY a date (`12.31.2023`, `2025 Q1`, `(4) 2025`):
     no investment-name residual after the date is stripped.
   - **Name + embedded date** — `PBC (12.31.2023)` / `PBC 8.31.2024` is the deal
     `PBC` observed at an embedded period (`_Node.embedded_period`); folders
     sharing the base name **merge into ONE deal across periods**, NOT three
     separate PBC deals. The display name is the date-stripped original casing.
   - **STRUCTURAL** — every (date-stripped) token is structural/glue/numeric, or a
     short correspondence folder ("From Ares", "To Ares").
   - **ADMIN** / **NEUTRAL** — admin folders vs candidate deal containers.
   A NEUTRAL **leaf** whose date-stripped name is ENTIRELY generic — every token
   structural/glue/grouping/admin or in `deal_name_stopwords` — is a document
   bucket, never a deal (`Research (2020.10.31)`, `Q4 2025 Reports`, `Prior
   Period`); it is dropped (gated by `exclude_generic_deal_names`, default on).
3. **Walks down** from the client. A NEUTRAL node is treated as a *container*
   (recurse) when: its period children hold recurring neutral subfolders
   (deal-below-period), or ≥2 neutral children carry their own period evidence
   (a strategy group), or it is a bare single-child wrapper. Otherwise the node
   **is a deal**. Recursion stops at STRUCTURAL folders and at emitted deals.
   Deals found under period folders are **merged across sibling periods** by
   normalized name. **Phase-A branches:**
   - **ADMIN-wrapped deals** — an ADMIN node containing a genuine period/memo-
     bearing neutral descendant becomes a CONTAINER (recursed into); the admin
     node is never itself a deal (`evidence.admin_container`, weight
     `admin_container` default −0.10).
   - **Shared mixed-investment bucket** (gated by
     `deal_discovery.shared_bucket_enabled`) — a neutral folder directly holding
     memo files for ≥ `shared_bucket_min_clusters` distinct investments
     (rapidfuzz asset-key clustering at `cluster_ratio_threshold`) emits ONE
     synthetic deal per cluster (`evidence.shared_bucket`/`name_filter`), all
     sharing the folder path; `assign_file_deals` splits the bucket's files (and
     files in a structural subfolder of it) per-stem to the best cluster at
     `shared_bucket_name_match_threshold`, unmatched → `deal=NULL` (nothing
     silent). Weight `shared_bucket` default 0.30.
   - **Multi-folder deals** — same-name candidates sharing a non-period container
     merge into one `DealFolder` with multiple `folder_paths`.
   These add `admin_container`/`shared_bucket`/`name_filter` to the existing
   `DealEvidence` JSON (no `deal_folders` schema change).
4. **Scores** each deal with an additive, clamp-to-[0,1] confidence from
   config-tunable components (period evidence, multi-period, structural children,
   memo-keyword files, flat-layout prior, grouping-name/depth penalties) and
   records the full `DealEvidence` breakdown.
5. **Persists** to `deal_folders` and rewrites `files.deal` (NULL for files under
   no deal). Same-name deals in different branches get "(parent)" suffixes.

Entry points: `discover_deals(conn, config, client)` (one client) and
`refresh_deals(conn, config, clients, use_llm=…)` (batch, optional LLM assist).
With `deal_discovery.enabled=false`, none of this runs and files keep the legacy
`rel[1]` assignment from `derive.py`.

**Per-client learning** (`indexer/deal_learning.py`, on when
`deal_discovery.learning.enabled`). The GUI records analyst corrections
(`add_folder`/`remove_folder`/`merge`/`split`/`rename`) into
`deal_finder_feedback` via `POST /index/deals/feedback`. At the END of
`refresh_deals`, `apply_feedback(deals, conn, config, client)` applies them — hard
pins/excludes/merge/rename/split, then capped client-scoped layout-prior nudges
(bounded by `learning.prior_bump`, cached in `index_meta` key
`layout_priors:<client>`). A correction on one deal **generalizes** to other new
deals under the same client. `GET /index/deals/learned?client=` surfaces active
priors + corrections; `DELETE /index/deals/feedback/{id}` and the CLI
`deals --forget` clear them; `deals --show-learned` prints them.

### 7.6 The "Confirm documents" step (and the retired Locator Review)

After a successful preflight, the wizard shows the exact files the locator
auto-selected so the analyst can curate them before launch. `GET
/api/jobs/{id}/selection` (`api/selection_service.py`) re-runs the SAME
`locate()` + Phase-2 `verify_and_rerank()` the run uses, for every in-scope
`(client, deal)` slot, and returns per slot: the auto-selected file (name, full
path, last-modified, predicted period, detected doc class, page count, locate
status, locator score + peek-verify confidence), the ranked alternative
candidates, and whether a learned override is already in effect. The frontend
renders this as a filterable `DataTable` (a global filter + per-column filters)
with a selected-slot detail panel for swapping.

Three actions, all through existing seams (nothing written under `pv_root`):
- **Swap** to a different candidate → records a learned override via
  `POST /api/locator/override` (the same `locator/overrides.py` table the locator
  consults at run time; the pick is still peek-verified).
- **Remove** a slot → tracked in wizard state and passed to launch as
  `RunRequest.exclude` (a list of `{client, deal}`); `run()` drops those pairs in
  `_resolve_pairs` — the `exclude` seam is `None`/empty for every CLI caller.
- **Add a missed file** → a file picker (`FolderPicker` `pickFiles` mode +
  `/fs/list?files=true`) → `POST /api/locator/verify-file` peek-verifies the
  chosen file against the slot and surfaces a warning if it would be rejected /
  is not indexed → `POST /api/locator/override` records it.

The AMBIGUOUS-resolution workflow that used to live in the standalone **Locator
Review** screen now lives here. That screen and its `/locator` nav entry were
removed; override visibility/deletion moved to **Settings → Learned locator
overrides** (`/api/locator/overrides` GET/DELETE). The `/api/locator/*` endpoints
are unchanged and still consumed by `locate()`.

### 7.7 Smart Search (Phase B)

Smart Search (`src/pv_extractor/search/`) turns a free-text query into a
`DocTypeSpec` and is **fully functional with the LLM OFF**. `intent.py` runs a
RULE layer first (built-in financial-doc lexicon UNION
`config.smart_search.intent_rules`; an unknown query falls back to its own
tokens), then an OPTIONAL local `claude -p` augmentation that only ADDS anchors
and is fully try/except-wrapped — any failure degrades to the rule-only spec with
no exception (`provenance` = `rules` vs `rules+cli`). `doc_type_spec.py` is CRUD
over `doc_type_profiles` plus builtins migrated from `locator.doc_type_keywords`
(`builtin=1`, anchors re-derived live from config, forkable-not-deletable).
`rank.py` scores candidates with a transparent additive model and folds
`doc_search_feedback` into bounded per-token `weight_overrides`. The resolved spec
routes through `locate()` (§5 locator), so Smart Search and the run pipeline agree
on doc-type scoring. Endpoints: `/search/profiles` (GET/POST/DELETE),
`/search/profiles/resolve`, `/search/preview`, `/search/feedback` (§6).

### 7.8 Multi-Search (Phase C)

Multi-Search drives the SAME per-slot pipeline (option-a). `expand_slots`
(`api/multi_search_service.py`) turns a multi-firm request into a list of
`RunSlot`s — reusing `indexer.deals.refresh_deals` per-firm llm_assist (the
existing local `claude -p` path), `deal_learning` corrections,
`search.doc_type_spec.resolve_spec`, and `selection_service.slot_selection` — and
`build_multi_selection` returns a firm-grouped preview that is READ-ONLY on the
learning table (corrections persist only on the run path, deduped). Each slot
carries its OWN period and doc-type (+ optional `DocTypeSpec`); a bad period
isolates to that slot's ERROR; ONE workbook is written for the batch; events are
firm-laned (§3). Under `enhanced_period_check`, `selection_service` surfaces a
slot whose best doc's in-file as-of (`VerifyResult.asof_date`) disagrees with the
target as **MISFILED** with the document's true `detected_period` (never
fabricated; off by default). `jobs.py` `start_multi_run` runs under the same
single-active-pipeline guard, writing a `scope="multi"` run summary
(`multi_search={firm_count, slot_count, firms}`). Frontend: the New Run
Single | Multi switch + per-firm regions (§7) launch via `POST /multi-search/run`;
`POST /multi-search/selection` returns 409-free read-only previews.

---

## 8. LLM fallback flow (Phase 3, summary)

After validation, each memo whose `EscalationPlan` has fields goes through the
worker queue (`llm.workers` hidden Claude Code sessions). The plan is built in
`run._build_escalation`: always low-confidence hits + required-but-empty fields,
and — when the asset QA-fails or `force_assist` is on — every empty
LLM-extractable field (excludes IDENTIFICATION/QA/THRESHOLD bands, computed
fields, positional slots). This closes the gap where a memo the engine could not
recognize produced an empty plan (`status: not_needed`) and was never escalated.
`payload.py` re-reads
**only** the escalated fields' candidate pages + pages 1–3 (text + pipe tables;
image pages as ≤1080px PNGs) and exposes per-page prompt blocks so calls can be
page-scoped. `schema_builder.py` compiles a byte-stable static prompt + strict
band-grouped JSON schema.

**Band-batched, relevance-ordered extraction** (`llm.band_batched`, default on):
instead of one giant call over every escalated field, `escalate._build_groups`
splits the plan into work groups — one focused group per band that has page
evidence (the Phase-2 `targeting._page_score` anchor scorer run over the band's
candidate-page text) OR a required/low-confidence field, ordered by descending
relevance, plus ONE cheap sweep group for the bands the document shows no
evidence for. Each group's call sends only that band's pages + a small schema, so
the model engages deeply (the fix for the "190-field giant call returns in 7s
with all not_found" failure). The most-relevant bands run first, so the budget is
spent well; the no-evidence sweep gets a single cheap pass (`no_evidence_effort`)
and never the expensive retry ladder. A group's `ocr_hostile` is per-band, so a
text band starts at sonnet even when the memo also has image pages.
`band_batched: false` reproduces the legacy single-call-per-memo path. New config
keys (in `config.py` + `config.example.yaml`): `band_batched`,
`band_relevance_floor`, `max_fields_per_call`, `no_evidence_effort`.

The router picks a tier ladder per group —
MANUAL forces one model+effort; AUTO runs sonnet/medium, retries on opus/high
(OCR-hostile bands start at opus, retry xhigh), fable only on explicit opt-in.
Each tier is **one** `claude -p --output-format json --json-schema` call (job id
`pv-<run>-<memo>-g<group>t<tier>`; cached by `sha256(prompt + payload + fields +
page-scope + model + effort)`; budget reserved before launch, `LLM_DEFERRED` past
the cap). Answers are quote-grounded against local
page text (failures discarded + `UNGROUNDED_LLM_VALUE`), type/vocab-checked, then
merged: fill empty fields, replace only below-threshold deterministic values
(loser kept as conflict), never touch confident/computed/metadata hits. Every
attempt lands in the audit record, `cost_ledger.jsonl`, and the Run Log.
`LLM_VERSION` bumped to 3.1.0 (cache key change).

---

## 9. Configuration files

- **`config.yaml`** — git-IGNORED, per-machine (seeded from `config.example.yaml`
  by bootstrap; add new tunables to both). Every tunable: paths (`pv_root`,
  `output_dir`, `db_path`),
  per-client `period_style`, `extraction` (incl. `confidence_threshold` default
  0.75), `peek_verify`, `validation`, `deal_discovery` (weights +
  `review_confidence` + `enabled`; plus the revamp's `learning`
  [`enabled`/`prior_bump`], `layout_priors` [manual-override default `{}` — LIVE
  priors live in the index DB], `shared_bucket_*`/`cluster_ratio_threshold`, and
  `weights.{admin_container, shared_bucket}`), the new `smart_search` and
  `multi_search` sections, `locator`, `llm` (workers, `budget_usd`
  default $25, routing, `allow_fable`), `gui`. Nothing magic inline.
- **`config/models.yaml`** — Claude Code model menu: aliases/full ids + **editable**
  price-per-1M-token assumptions (seeded fable/opus/sonnet/haiku), `latest_alias`,
  pinned ids, `requires_explicit_enable`.
- **`aliases.yaml`** — client/deal alias token expansions for the locator + deal
  search.
- **`rules.yaml`** — cross-field validation rules + per-field range overrides.
- **`schema/*.json`** — compiled, committed, byte-stable.

---

## 10. Testing

- **515 passing (2 skipped) / 28 modules** (the perf module is deselected by
  `-m "not perf"`). Coverage spans schema compiler, indexer/periods, deal
  discovery, locator (unit/e2e/overrides), patterns, targeting, readers, bands,
  golden extraction (freezes ≥40 fields per text memo), validate, verify, writer,
  LLM (client/registry/schema-builder/escalation/live), GUI (api + opt-in
  Playwright smoke), readonly guard, system checks, perf smoke. The Search &
  Selection revamp added `test_deal_learning.py` (Phase-A corrections + capped
  priors + generalization), `test_smart_search.py` (rule-first intent, LLM-off
  parity, ranking + learning) and `test_multi_search.py` (per-slot expansion,
  isolated bad-period slots, firm-laned events, read-only preview, misfiled),
  and expanded the deal-discovery and GUI suites. The GUI API tests
  include the New Run document-selection endpoint, the `exclude` launch seam, the
  run-summary digest fields, the per-run Index-rows mirror, the `?files=` folder
  listing, the `/locator/verify-file` preview, and an **evidence exact-page**
  consistency check (every deterministic text-page FieldHit renders the page the
  value actually sits on — no off-by-one).
- **Fixtures** (`tests/fixtures/`): `build_fixture.py` (synthetic PV tree),
  `docgen.py` (document primitives incl. scanned/image-table/encrypted),
  `build_memos.py` (realistic memo content / RICH_BUILDERS for golden tests),
  `fake_claude.py` (canned schema-valid + malformed LLM responses).
- **No real Claude CLI by default.** Escalation tests inject the fake; the single
  live test needs `PV_LIVE_CLAUDE_CODE_TESTS=1` + passing `claude auth status`.
- **Markers:** `-m "not perf"` skips perf smoke; `PV_GUI_SMOKE=1` enables the
  Playwright GUI test (now drives BOTH the full Single-Search wizard end to end —
  scope→template→model→preflight→confirm→launch→review with an evidence image —
  and the Multi-Search flow — mode switch, add a firm, preview the firm-grouped
  selection, launch a dry multi-run; both confirmed passing in headless chromium).
- **Drift tests:** recompiling the schema must be byte-identical;
  `test_readonly_guard.py` greps src for stray write-mode `open()`.

Run: `.venv/bin/python -m pytest` (full) · `-m "not perf"` (fast).

---

## 11. Build & run

```bash
python scripts/bootstrap.py          # .venv + editable install
python scripts/bootstrap.py --with-gui   # also builds src/frontend/dist
claude auth login                    # once — Phase 3 reuses this local session
.venv/bin/pv-extractor doctor        # claude CLI / auth / menu / artifacts
.venv/bin/pv-extractor gui           # local GUI (127.0.0.1, opens browser)
.venv/bin/pv-extractor run --scope deal --client "Angelo Gordon" \
    --deal "Accell" --period "2025-01-31"
```

Windows: `Start PV Extractor.bat` (bootstraps then `gui`);
`scripts/sync_to_windows.sh` ships `dist/` so Windows needs no Node, and never
overwrites the destination `config.yaml`.

---

## 12. Current state / known gaps

- **History:** the four-phase implementation landed as one initial commit; the
  Search & Selection revamp (Phases G/A/B/C) then landed across several commits on
  `main` (latest the Phase-C frontend `37b9ba4`).
- **Theme is the real HL brand.** `src/frontend/src/theme/tokens.css` now carries
  the authentic Houlihan Lokey palette (Oxford Blue anchor + Sapphire/Tufts/Azure
  accents), the official logo SVGs ship in `src/assets/`, and the favicon is the
  HL globe — replacing the former placeholder approximation.
- **Windows path not yet verified end-to-end.** The WSL→Windows bridge for the
  Claude CLI and the Phase-4 GUI on Windows are implemented but not confirmed on
  real hardware (per project memory).
- **PyInstaller onefile** packaging is documented as an optional stretch, not built.
- The deterministic engine is primary by design; the LLM fallback is strictly a
  gap-filler bounded by a hard budget cap.

---

*Keep this file current — see the maintenance directive at the top.*
