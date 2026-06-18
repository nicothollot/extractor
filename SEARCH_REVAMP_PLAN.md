# Search & Selection Revamp — Implementation Plan

> Hand-off plan for a coding agent. Three features, built **engines-first**:
> **A. Deal Finder 2.0** (smarter, self-learning deal-folder discovery) →
> **B. Smart Search** (intent-driven document finding from free-text) →
> **C. Multi-Search** (a new wizard/indexing mode spanning many firms at once).
>
> **This is an ENHANCEMENT of existing, working automations — not a rewrite.**
> The repo already has a sophisticated deal-discovery engine, a tunable locator
> cascade, a content peek-verifier, an opt-in local-LLM deal-discovery assist,
> a learned-override table, and a 3-mode New Run wizard. Every item below
> *extends* one of those — preserve current behavior as the default/degenerate
> case, layer the new capability on top, and regression-guard the existing
> tests. See **"What already exists"** immediately below before touching code.
>
> Read `CLAUDE.md` (authoritative rules) and `ARCHITECTURE.md` (state map)
> before starting. **Every hard rule still applies** — read-only on the share,
> client-docs-only, no Anthropic SDK / no API key (local `claude -p` only),
> nothing silent, everything tunable lives in `config.yaml` (+ mirror in
> `config.example.yaml`). Bump `EXTRACTOR_VERSION` / `LLM_VERSION` only where
> extraction/LLM output can change (these features mostly touch discovery,
> locator, and GUI — neither version key should need a bump unless you alter
> band extraction or LLM prompts/payloads).

---

## Decisions locked in (from the requirements review)

1. **Smart Search engine** = **Hybrid**: deterministic rule/synonym engine
   first → optional local `claude -p` CLI fallback for novel phrasings →
   learns from accept/reject into a saved, reusable "doc-type profile". Fully
   local, no API key, no new heavy dependency required (lexical core uses the
   `rapidfuzz` already in the stack; BM25 via a tiny in-house scorer over the
   FTS index — **do not** add `fastembed`/embeddings in this pass).
   **MANDATORY: Smart Search must be fully functional with the LLM fallback
   OFF.** The deterministic rule/synonym engine + lexical ranking + learning is
   the primary, self-sufficient path; the CLI fallback is a *bonus* for unusual
   phrasings only. With `smart_search.use_cli_fallback: false` (or no `claude`
   available, or it errors/times out) the feature degrades gracefully to the
   rule engine and still returns useful ranked results — never an error, never
   an empty result that depends on the LLM. The deterministic path must be rich
   enough (broad seeded financial-doc lexicon + learned profiles) that common
   queries like "quarterly reports", "annual report", "cap table", "audited
   financials" resolve well WITHOUT any LLM call.
2. **Deal Finder learning** = **per-client learned overrides + priors**:
   corrections are stored per-client and (a) pin/exclude exact folders AND
   (b) nudge the heuristic priors so a *new* deal in the same oddly-structured
   client benefits. Local, deterministic, inspectable.
3. **Sequencing** = **engines first, then Multi-Search UI**. Phase A and B ship
   stable backend contracts; Phase C's wizard consumes them.

### What already exists (enhance these — do NOT rebuild)
The goal of this work is to **greatly enhance the existing automations**, so the
agent must build on what's here rather than re-implement it. Inventory of the
relevant machinery already in the repo:

- **Deal discovery (`indexer/deals.py`)** — full segment-role classifier
  (PERIOD/STRUCTURAL/ADMIN/NEUTRAL), aggregate→walk→score→merge pipeline,
  additive confidence model, `deal_folders` table persistence, and
  `assign_file_deals` rewriting `files.deal`. **Phase A extends this engine's
  classifier/walker and adds a learning layer — it does not replace it.**
- **Opt-in LLM deal assist (`llm/deal_discovery.py`)** — already sends ONE
  grounded `claude -p` call per client over a folder inventory, with the
  floating `sonnet` alias and ungrounded-path discarding. **Reuse as-is.**
- **Locator cascade (`locator/`)** — FTS prefilter → tunable additive scorer →
  eligibility gate → version-family grouping → FOUND/AMBIGUOUS/… statuses.
  **Phase B's Smart Search routes THROUGH this scorer** (DocTypeSpec replaces the
  static `doc_type_keywords` lookup); it does not fork a parallel pipeline.
- **Peek-verifier (`locator/verify.py`)** — the **"enhanced period check"** the
  user wants (open the doc, confirm its report/as-of date matches the selected
  period instead of trusting the date folder) **already exists** here:
  `verify_candidate` → `_extract_asof` → REJECTED on mismatch, surfaced via
  `verify_and_rerank`. Phase C's toggle is mostly **plumbing this existing
  capability into the Multi-Search flow + reporting it clearly**, not new
  extraction logic. The one genuinely new behavior: when the in-file date
  disagrees with the folder date, surface the *document's true period* as a
  first-class **"misfiled document"** result instead of a silent rejection.
- **Learned overrides (`locator/overrides.py`)** — analyst picks short-circuit
  `locate()` on the resolved key (still peek-verified). Phase A's per-client
  learning is the **structural generalization** of this idea (new tables, not a
  rewrite); keep the existing exact-pick table untouched.
- **New Run wizard (`screens/NewRun.tsx`, `lib/wizard.tsx`)** — 7 steps, THREE
  discovery modes (Browse / Search / LLM assist), and a Confirm-documents step
  with swap/remove/add. **Phase C adds a Single | Multi mode switch and reuses
  these components** (FolderPicker, selection table, discovery search endpoints)
  inside the per-firm regions; Single Search stays exactly as it is today.
- **Selective index scan (`routes_core.py` scan job, `screens/Settings.tsx`)** —
  the scan endpoint already accepts `clients: list[str]`. Phase C's indexing
  Multi tab makes multi-firm selection first-class on top of it.

**Net effect to aim for:** discovery finds the right folders in more layouts and
keeps getting better from corrections; document finding understands plain-English
intent; and a single run/scan can cover many firms at once — all as additive
upgrades to the pipeline that already works.

---

## Cross-cutting groundwork (do this first)

These are shared by all three phases.

### G1. Generalize the learning table beyond `(client,deal,as_of,doc_type)`
Today `locator/overrides.py` has one table `locator_overrides` keyed on the full
resolved tuple. Phase A and B need broader-scoped learning. **Add two new
tables** in `indexer/db.py` migrations (do NOT widen the existing one — keep it
for exact locator picks):

- `deal_finder_feedback` — per-client deal-structure corrections (Phase A):
  ```sql
  CREATE TABLE deal_finder_feedback (
    id INTEGER PRIMARY KEY,
    client TEXT NOT NULL,
    deal TEXT NOT NULL,             -- the deal the analyst was correcting
    action TEXT NOT NULL,           -- 'add_folder' | 'remove_folder' | 'merge' | 'split' | 'rename'
    folder_path TEXT,               -- relative path acted on (NULL for rename)
    payload TEXT,                   -- JSON: new name, merge target, etc.
    created_at TEXT NOT NULL
  );
  CREATE INDEX idx_dff_client ON deal_finder_feedback (client);
  ```
- `doc_type_profiles` — learned/saved Smart Search intents (Phase B):
  ```sql
  CREATE TABLE doc_type_profiles (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,        -- 'quarterly_reports', normalized
    label TEXT NOT NULL,             -- 'Quarterly Reports' (display)
    query_seed TEXT,                 -- the original free-text that created it
    spec TEXT NOT NULL,              -- JSON DocTypeSpec (see Phase B)
    builtin INT NOT NULL DEFAULT 0,  -- 1 for the seeded ic_memo/valuation_memo/etc.
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  ```
  Plus a feedback table for ranking corrections:
  ```sql
  CREATE TABLE doc_search_feedback (
    id INTEGER PRIMARY KEY,
    profile_slug TEXT NOT NULL,
    file_path TEXT NOT NULL,
    label INT NOT NULL,              -- +1 accepted as a match, -1 rejected
    context TEXT,                    -- JSON: client/deal/period at decision time
    created_at TEXT NOT NULL
  );
  ```
  All DB writes go to the **local index DB** (gitignored, never the share).
  Migrations must be additive + idempotent (the indexer already does
  `CREATE TABLE IF NOT EXISTS`; follow that pattern and bump any
  `SCHEMA_VERSION` constant if one exists).

### G2. New pydantic contracts (`models.py`)
Add (names indicative — match existing style, pydantic v2):
- `DocTypeSpec` — the structured, learnable description of "what to find":
  ```python
  class DocTypeSpec(BaseModel):
      slug: str
      label: str
      filename_include: list[str] = []      # token/phrase synonyms (normalized match)
      filename_regex: list[str] = []         # optional raw patterns (e.g. r"10[- ]?q")
      filename_exclude: list[str] = []
      folder_include: list[str] = []         # folder-context anchors ("filings", "quarterly")
      folder_exclude: list[str] = []
      extensions: list[str] = []             # ['.pdf', '.htm'] etc; empty = any
      period_required: bool = True
      weight_overrides: dict[str, float] = {}  # per-component nudges from learning
  ```
- `DealFinderPlan` — the structured output of Deal Finder 2.0 for one client
  (layout classification + per-deal folder sets + confidence + rationale +
  applied-feedback markers).
- `MultiSearchRequest` / `MultiSearchFirmSpec` / `MultiSearchSlot` (Phase C).

### G3. Config additions (add to BOTH `config.py` typed models AND
`config.example.yaml`, with the same defaults — fresh checkouts must work):
- `deal_discovery.learning.enabled: bool = True`
- `deal_discovery.learning.prior_bump: float = 0.25` (confidence nudge a learned
  prior contributes)
- `deal_discovery.layout_priors: {}` (auto-maintained per-client cache; see A3)
- `smart_search:` new top-level section — see Phase B (`enabled`, `use_cli_fallback`,
  `cli_model` default `"sonnet"` alias, `cli_effort` default `"low"`,
  `bm25_k1`/`bm25_b`, `min_score`, `top_n`, `learning_weight`).
- `multi_search:` new section — `max_firms`, `enhanced_period_check_default: false`,
  per-firm `default_doc_types`.
- Bump the per-component weights only via config — never inline.

---

## Phase A — Deal Finder 2.0 (engine)

**Goal:** more accurate, structure-aware deal-folder discovery that handles the
hard real layouts (deal under `_Admin`; no deal folder at all — one folder of
mixed-investment PDFs; deals below period folders; project-codename wrappers)
and **learns from corrections per client**.

### A1. Files
- `src/pv_extractor/indexer/deals.py` — extend, don't rewrite. Keep the
  classify→aggregate→walk→score→merge pipeline; add the hooks below.
- `src/pv_extractor/indexer/deal_learning.py` — NEW. Reads/writes
  `deal_finder_feedback`, derives per-client **layout priors**, exposes
  `apply_feedback(plan, conn, client)` and `record_correction(...)`.
- `src/pv_extractor/llm/deal_discovery.py` — keep as the opt-in LLM corroborator
  (unchanged contract; it already grounds against an inventory).

### A2. Algorithm upgrades (heuristic, deterministic)
Implement as new branches in the segment-role classifier + walker:
1. **ADMIN-wrapped deals.** Today an `_Admin`/admin node is classed ADMIN and
   effectively skipped. New rule: an ADMIN node that itself contains
   period-bearing or memo-keyword-bearing neutral children is a **container**,
   not a dead end — recurse into it. Add `admin_container` evidence component.
2. **No-deal-folder / flat document bucket.** Detect the "one folder holds PDFs
   for many investments" layout: a NEUTRAL node with many direct memo-keyword
   files whose **derived asset names diverge** (cluster file stems by
   rapidfuzz; if ≥2 distinct asset clusters, the folder is a *shared bucket*,
   not a single deal). Emit one synthetic deal **per cluster** with
   `folder_paths` = [the shared folder] and a `file_glob`/name filter stored in
   evidence, plus a `shared_bucket=true` flag so `assign_file_deals` can split
   files in a shared folder across deals by name match. (This is the biggest
   new capability — gate it behind `deal_discovery.weights.shared_bucket_*` and
   test heavily.)
3. **Codename/strategy wrappers** already handled — extend the existing
   `_is_container` checks to also treat a node as a wrapper when its name is a
   pure grouping token even with mixed children.
4. **Multi-folder deals.** A deal whose documents are split across sibling
   folders (e.g. `Reports/` and `Filings/` under the same deal) should merge
   into one `DealFolder.folder_paths` list — extend the merge step to union
   sibling structural folders under the same deal node.

### A3. Learning: per-client overrides + priors
- **Corrections captured** when the analyst edits the discovered set in the GUI
  (Phase C Confirm step, and a Settings deal-admin panel): add/remove a folder,
  merge/split deals, rename. Each writes a `deal_finder_feedback` row.
- **`apply_feedback`** runs at the END of `refresh_deals` for each client:
  - Hard layer: pinned folders are force-added to the right deal; excluded
    folders are removed; renames/merges applied. These always win.
  - Prior layer: from the corrections, derive reusable signals and persist them
    into `deal_discovery.layout_priors[client]` (a JSON cache in the index, NOT
    config.yaml). Examples of learnable priors:
    - "deals for this client live under an ADMIN node" → raise
      `admin_container` weight for this client.
    - "this client uses shared buckets" → raise `shared_bucket` weight.
    - "ignore folder named X" → per-client folder-name exclude.
  - The prior contributes at most `deal_discovery.learning.prior_bump` to
    confidence and is **inspectable** (surface in the GUI deal-admin panel:
    "Learned for {client}: …").
- **Generalization test:** correcting deal *Foo* under a client must improve a
  *different* new deal *Bar* under the same client on the next refresh (write a
  fixture for exactly this).

### A4. Backend surface
- Extend `POST /api/index/deals/refresh` to accept `apply_learning: bool = true`.
- NEW `POST /api/index/deals/feedback` — body `{client, deal, action, folder_path?, payload?}`
  → writes `deal_finder_feedback`, re-runs discovery for that client, returns the
  updated `DealFolderInfo[]` + a `learned: [...]` summary of active priors.
- NEW `GET /api/index/deals/learned?client=` — list active learned priors +
  corrections for the deal-admin panel.
- `DELETE /api/index/deals/feedback/{id}` — undo a correction.

### A5. CLI
- `pv-extractor deals --client X --refresh` already exists; add
  `--show-learned` to print the per-client priors, and `--forget` to clear them.

### A6. Tests
- Fixtures in `tests/fixtures/build_fixture.py`: add the hard layouts
  (deal-under-`_Admin`, shared mixed-investment bucket, multi-folder deal,
  codename wrapper). Generate deterministically.
- `tests/test_deals.py`: assert correct discovery on each new layout; assert
  shared-bucket file splitting in `assign_file_deals`; assert the
  generalization property in A3; assert priors are additive and capped.

---

## Phase B — Smart Search (engine)

**Goal:** free-text → a structured, learnable document search. "quarterly
reports" resolves to a `DocTypeSpec` that knows the filename synonyms
(quarterly, 10-Q, Q#…), folder context (`Filings`, `Quarterly`), extensions,
and period requirement — then ranks files, and **gets better from your
accept/reject**.

### B1. Files
- `src/pv_extractor/search/` — NEW package:
  - `doc_type_spec.py` — `DocTypeSpec` load/save (CRUD over `doc_type_profiles`),
    plus the **seeded builtins** migrated from `locator.doc_type_keywords`
    (`valuation_memo`, `ic_memo`, `portfolio_review`) so existing behavior is
    preserved and now editable.
  - `intent.py` — free-text → `DocTypeSpec`:
    1. **Rule layer (primary):** a curated synonym/pattern map
       (`config.smart_search.intent_rules`, tunable) — phrase → spec fragments.
       Handles the common cases deterministically. Includes a financial-doc
       lexicon seed (quarterly/annual report → 10-Q/10-K patterns, "filings",
       "audited financials", "cap table", etc.).
    2. **Local CLI fallback (OPTIONAL, `use_cli_fallback`, default usable but
       never required):** for phrasings the rules don't cover, ONE hidden
       `claude -p --output-format json --json-schema` call (reuse
       `llm/claude_code_client.py`; strict schema = `DocTypeSpec`; byte-stable
       static prompt; cached). NEVER the SDK / API key. Output is grounded:
       every suggested folder/filename token must be a hint only — the spec is
       still validated against the schema and the result is still rule-checked.
       Default model = `"sonnet"` alias, effort `low`.
       **Robustness contract:** `intent.py` must ALWAYS return a valid
       `DocTypeSpec` from the rule layer first. The CLI fallback only *augments*
       that spec (merges extra synonyms/folder anchors) and only fires when
       (a) `use_cli_fallback` is true AND (b) the `claude` CLI is available. Any
       CLI failure (missing binary, not authed, timeout, malformed JSON, budget)
       is caught and logged, and resolution falls back to the rule-only spec —
       the call site never sees an exception and never blocks. The provenance
       returned to the UI states `rules` vs `rules+cli` so the analyst knows.
    3. **Merge + persist:** offer to save the resolved spec as a named profile.
- `search/rank.py` — score files against a `DocTypeSpec`:
  - **Lexical core (no new deps):** BM25 over the FTS5
    `files_fts(normalized_file_name, normalized_folder_path)` — implement a
    small BM25 scorer (`k1`, `b` from config) reading FTS term stats, OR reuse
    rapidfuzz token_set_ratio against `filename_include`. Folder-context anchors
    score the `normalized_folder_path`.
  - Component model mirrors the locator's additive style so it's inspectable:
    filename-match, folder-context, extension prior, period evidence (reuse
    `indexer/periods.filename_contains_period` + date-folder `as_of_date`),
    negative-term penalty.
  - **Learning layer:** fold `doc_search_feedback` into per-spec
    `weight_overrides` (accepted files' distinctive tokens get a small positive
    bump; rejected patterns a penalty), weighted by
    `smart_search.learning_weight`. Keep it a transparent linear nudge — no
    opaque model. (This is the "online learning to rank from feedback" idea kept
    deliberately simple and local.)

### B2. Integration with the locator
Smart Search is a **doc-type generalization**, so wire it through the existing
locator rather than forking a parallel pipeline:
- Extend `DocType` handling so `LocateQuery.doc_type` can be a profile slug
  (keep the enum for builtins; add a `doc_type_profile: str | None` field, or
  let `doc_type` accept arbitrary slugs resolved via `doc_type_profiles`).
- In `locator/scoring.py`, when a `DocTypeSpec` is in play, use its
  include/exclude/folder lists in place of the static `doc_type_keywords`
  lookup, and apply `weight_overrides`. The eligibility gate
  (`_is_eligible` requiring ≥1 doc-type keyword) keys off
  `filename_include`/`filename_regex` hits.
- Peek-verifier stays as-is (still rejects HL work product + wrong period/asset).

### B3. Backend surface
- `GET /api/search/profiles` — list profiles (builtin + learned).
- `POST /api/search/profiles/resolve` — body `{query: str, use_cli?: bool}` →
  returns a (possibly unsaved) `DocTypeSpec` + provenance (`rules` | `cli`) for
  preview/edit.
- `POST /api/search/profiles` — save/update a profile (analyst-editable spec).
- `DELETE /api/search/profiles/{slug}` (builtins are not deletable, only forkable).
- `POST /api/search/preview` — body `{spec_or_slug, client?, deal?, period?}` →
  ranked file list (the live "is this finding the right docs?" view).
- `POST /api/search/feedback` — `{profile_slug, file_path, label, context}` →
  writes `doc_search_feedback`, returns updated effective weights.

### B4. Config (`smart_search` section)
```yaml
smart_search:
  enabled: true
  use_cli_fallback: true
  cli_model: sonnet        # alias, floats to current cheap tier
  cli_effort: low
  bm25_k1: 1.2
  bm25_b: 0.75
  min_score: 8.0
  top_n: 25
  learning_weight: 0.3
  intent_rules:            # phrase -> spec fragment (tunable, seeded)
    quarterly report: {filename_include: [quarterly, 10 q, q1, q2, q3, q4], filename_regex: ["10[- ]?q"], folder_include: [filings, quarterly], extensions: [.pdf, .htm, .html]}
    annual report:    {filename_include: [annual, 10 k], filename_regex: ["10[- ]?k"], folder_include: [filings, annual]}
    # ... more seeds
```
Mirror into `config.example.yaml`.

### B5. Tests
- `tests/test_smart_search.py`: rule resolution for several free-text queries
  **with `use_cli_fallback: false`** (the no-LLM path is the headline test — it
  must resolve common queries and rank correctly with zero CLI involvement);
  CLI fallback path using the existing `tests/fixtures/fake_claude.py` harness
  (canned `DocTypeSpec` JSON — add one); **a CLI-unavailable / CLI-errors test
  proving graceful degradation to the rule-only spec (no exception, still
  returns results)**; ranking on a fixture tree with `Filings/` subfolders +
  10-Q-style filenames; feedback shifts ranking deterministically; builtins
  reproduce current locator doc-type behavior (regression: existing locator
  tests must still pass).
- NO test launches the real CLI by default (respect the existing convention).

---

## Phase C — Multi-Search (wizard + indexing mode)

**Goal:** a "Multi Search" tab alongside the current "Single Search" for BOTH
the New Run wizard and the indexing/Settings flow: pick several firms (comma
slices or browse), and for each firm an independent region to choose its deals,
period, document type(s), and per-firm parameters (LLM assist, enhanced period
check, deal-search model), with a live preview of determined deal folders that
the analyst can add to / remove from.

### C1. Backend — make a run span multiple firms
Today `run()` takes one `scope`+`client`+`deal`+`period`. Two options; **do
option (a)** to avoid touching the core engine:
- **(a) Batch of locate-slots (recommended).** Add a backend that expands a
  `MultiSearchRequest` into a flat list of `(client, deal, period, doc_type)`
  slots, then drives the EXISTING per-slot pipeline. The orchestrator
  (`run.py`) already loops over `(client, deal)` pairs sharing one period — generalize
  `_resolve_pairs` / the run entry so each slot can carry its **own** period and
  doc_type instead of a single run-wide value. Keep single-firm runs as the
  degenerate case (one firm, one period) so nothing regresses.
- Contracts:
  ```python
  class MultiSearchFirmSpec(BaseModel):
      client: str
      deals: list[str]                 # explicit, OR empty = all discovered
      period: str
      doc_types: list[str]             # builtin enums and/or profile slugs
      llm_assist: bool = False         # deal-discovery LLM assist for this firm
      enhanced_period_check: bool = False   # surface peek-verifier as-of cross-check
      deal_search_model: str | None = None
      added_folders: list[str] = []    # analyst-added deal folders (-> deal_finder_feedback)
      removed_deals: list[str] = []

  class MultiSearchRequest(BaseModel):
      firms: list[MultiSearchFirmSpec]
      template: str | None = None
      dry_run: bool = False
      llm: LlmRunOptions = ...
  ```
- **Enhanced period check** = when set, the slot's selection/preflight reports
  the peek-verifier's in-file as-of result prominently, and a folder/in-file
  date disagreement yields a `misfiled` status carrying the document's TRUE
  detected period (new light field on the selection slot; verifier already
  extracts the date — just surface it instead of only REJECTING).

### C2. Backend endpoints
- `POST /api/multi-search/selection` — given a `MultiSearchRequest` (dry-run
  style), return per-firm → per-deal selection slots (reuse
  `selection_service` per slot; run discovery with `llm_assist` where set;
  apply `enhanced_period_check`). Returns a grouped structure:
  `{firms: [{client, deal_folders_preview, slots: [SlotSelection...] }]}`.
- `POST /api/multi-search/run` — launch the batch run (one job; events grouped
  by firm). Reuse the existing jobs/WS infrastructure; add a `firm`/`group`
  field to run events so the UI can lane by firm.
- Reuse Phase A's `/api/index/deals/feedback` for add/remove of deal folders
  from within Multi-Search.
- Reuse Phase B's profile endpoints for the document-type selector.

### C3. Frontend
- **Wizard restructure (`lib/wizard.tsx`, `screens/NewRun.tsx`):** introduce a
  top-level mode switch **Single Search** | **Multi Search**.
  - Single Search = the current wizard, unchanged (lowest risk).
  - Multi Search = new flow. State shape:
    ```ts
    interface MultiSearchState {
      firms: FirmEntry[];          // added via comma-slice input or Browse modal
      template, dryRunOnly, llm;   // shared run-level settings
    }
    interface FirmEntry {
      client: string;
      deals: string[];             // selected; [] = all discovered
      dealFoldersPreview: DealFolderInfo[];   // from discovery, editable
      period: string;
      docTypes: string[];          // builtin + profile slugs (Smart Search picker)
      llmAssist: boolean;
      enhancedPeriodCheck: boolean;
      dealSearchModel: string;
      addedFolders: string[];
      removedDeals: string[];
    }
    ```
  - **Firm entry:** a comma-separated text input ("Angelo Gordon, Ares, Apollo")
    that resolves each token via `/api/index/search/clients` (fuzzy, confirm
    ambiguous), plus a Browse modal (reuse the FolderPicker / clients-status
    list) to multi-select.
  - **Per-firm region** (accordion/card per firm): deal multi-select (from
    discovered `DealFolderInfo[]`, with confidence chips), **deal-folder preview
    with add/remove** (remove → `removed_deals`; add → FolderPicker →
    `added_folders`, persisted as `deal_finder_feedback` so it's learned),
    period dropdown (reuse `/index/periods` + free-text resolve), **document-type
    multi-select** using the Smart Search profile picker (builtins + a "＋ search
    by description" box that calls `/api/search/profiles/resolve`), and the
    per-firm toggles (LLM assist, enhanced period check, deal-search model).
  - **Confirm documents (multi):** the existing Confirm step generalized to
    group slots by firm; per-slot swap/remove/add unchanged; show the
    `enhanced_period_check` true-period / `misfiled` badges.
  - **Launch + Progress:** one batch job; Run Progress lanes grouped by firm
    (the run events already stream; add the `firm` group field).
- **Indexing Multi-Search (`screens/Settings.tsx`):** add the same Single |
  Multi switch to the scan UI so a single scan job can target several specific
  firms (the scan endpoint already accepts `clients: string[]` — the Multi tab
  just makes selecting several firms first-class, with optional per-firm
  deal-discovery LLM assist + model after the scan).
- Persist Multi-Search state with the existing `useStickyState` / above-router
  contexts so it survives tab switches.

### C4. Tests
- API: `tests/test_gui_*` — multi-search selection groups slots per firm;
  per-firm period/doc_type honored; enhanced_period_check surfaces a misfiled
  document; batch run executes all slots and writes one workbook.
- Frontend smoke (opt-in Playwright, `PV_GUI_SMOKE=1`): mode switch renders both
  tabs; adding two firms via comma input builds two regions; deal add/remove
  round-trips.

---

## Documentation & versioning (do alongside code, not after)
- **`ARCHITECTURE.md`**: update the subsystem inventory (new `search/` package,
  `indexer/deal_learning.py`, new DB tables), the deal-discovery rule paragraph
  (ADMIN-container, shared-bucket, multi-folder, learning), the locator
  paragraph (DocTypeSpec-driven doc types), the API surface (all new endpoints),
  and the wizard description (Single | Multi modes).
- **`CLAUDE.md`**: extend the module map + the "Deal discovery in one paragraph"
  / "Locator in one paragraph" sections; add a "Smart Search in one paragraph"
  and "Multi-Search in one paragraph". Add the new config sections to the
  conventions note (every tunable in `config.yaml` + `config.example.yaml`).
- **Config parity:** every new key in `config.py` MUST also be in
  `config.example.yaml` with the default.
- **No version bumps expected** for `EXTRACTOR_VERSION` / `LLM_VERSION` unless
  band extraction or LLM extraction prompts/payloads change. Discovery/locator
  learning lives in the index DB, which is rebuildable; document that clearing
  `deal_finder_feedback` / `doc_type_profiles` resets learning.

## Suggested build order (checklist)
1. G1–G3 groundwork: DB tables, contracts, config scaffolding (+ example.yaml).
2. Phase A engine + `deal_learning.py` + tests + fixtures.
3. Phase A backend endpoints + CLI flags.
4. Phase B `search/` package (specs, intent rules, CLI fallback, rank, learning)
   + locator integration + tests (regression-guard the builtins).
5. Phase B backend endpoints.
6. Phase C backend (multi-slot expansion, selection, batch run, event grouping).
7. Phase C frontend (Single | Multi switch, per-firm regions, Confirm/Progress,
   Settings multi-scan).
8. Docs (`ARCHITECTURE.md`, `CLAUDE.md`), final `pytest` (+ optional smoke).

## Risks / watch-items for the implementer
- **Shared-bucket splitting** (A2.2) is the riskiest new heuristic — it changes
  `assign_file_deals` semantics (one folder → many deals). Gate it, test it, and
  make it inspectable; default conservative.
- **Doc-type as arbitrary slug** (B2) touches the locator's eligibility gate and
  the existing `DocType` enum — keep builtins behaving exactly as today
  (regression tests on `tests/test_locator*.py`).
- **Multi-slot per-period run** (C1a) generalizes `run.py`'s single run-wide
  period — verify single-firm runs are byte-for-byte unchanged (idempotency /
  result-cache tests).
- Keep every CLI fallback **cached, schema-strict, API-key-free**, and never run
  the real `claude` in the default test suite.
- **Smart Search must never hard-depend on the LLM.** Build and test the
  rule-only path first and treat the CLI fallback as a strictly additive,
  catch-all-failures augmentation. If you find yourself needing the CLI to get a
  usable result for a common query, fix the rule lexicon instead.
- **Enhance, don't rebuild.** Each phase extends an existing module
  (`indexer/deals.py`, the locator scorer, the wizard) with current behavior
  preserved as the default — verify the pre-existing test suites still pass
  unchanged after each phase.
