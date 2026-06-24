# PV Extractor — Phases 1+2+3+4

> **`ARCHITECTURE.md` is the living current-state map of this repo** (subsystem
> inventory, the end-to-end pipeline, the deal-selector flow, API surface, test
> status). **Whenever you change code in a way that affects what it describes —
> a new/renamed/removed module, a changed pipeline stage or endpoint, a new
> screen, a different deal-discovery rule, a new config key or data contract —
> update `ARCHITECTURE.md` in the same change.** This `CLAUDE.md` is the
> authoritative rules/spec; `ARCHITECTURE.md` is the authoritative state map.

Internal HL tool: locate client-provided valuation documents (IC memos, valuation
memos, portfolio reviews) on the PV network share and extract ~600 structured
fields per memo into a master Excel index. Phase 1: schema compiler, file-index
database, document locator. Phase 2: deterministic extraction engine — document
readers + local OCR, candidate-page targeting, peek-verifier, band extractors
with a multiplicative confidence model, validation/QA, workbook writer, run
orchestrator. Low-confidence and required-but-empty fields land in a per-memo
`EscalationPlan` inside the audit record. Phase 3: local CLI LLM assist — a
surgical second pass executing those plans through a provider-neutral seam
(Claude Code by default; temporary Codex CLI provider available; NEVER a hosted
LLM API called directly from Python): pages-not-documents payloads, one call per
memo per router tier, strict JSON schemas with embedded provenance,
quote-grounding, response cache, hard per-run budget cap, cost ledger. The
deterministic engine remains primary. Phase 4: the
analyst-facing LOCAL web GUI — one FastAPI/uvicorn process on 127.0.0.1 (no
auth, no telemetry, no external calls) serving a built Vite+React+TS frontend;
it wraps the SAME pipeline functions the CLI calls as background jobs (live
WebSocket progress, graceful cancel via the `RunControl` seam), plus the
review queue (accept/edit/unresolvable through writer entry points, evidence
pages rendered by pymupdf with bbox highlights + a full-document page viewer),
the New Run wizard's "Confirm documents" step (curate the locator's
auto-selection before launch — swap/remove/add files; AMBIGUOUS resolution now
lives here, writing the learned-override table consumed by `locate()`; the
standalone Locator Review screen was retired, override admin moved to Settings),
preflight cost ESTIMATES, editable model pricing/config (ruamel round-trip,
comments preserved), and setup/doctor flows.

## Hard rules

1. **Read-only on the share.** The tool NEVER writes under `pv_root`. Every file
   open goes through `io_guard.open_read` ('rb') or `io_guard.guarded_open_write`
   (refuses pv_root targets; the production share `\\hlhz\dfs\nyfva\PV` is refused
   even if config points elsewhere). `tests/test_readonly_guard.py` greps src/ for
   stray write-mode `open()` calls. Readers parse from in-memory bytes obtained
   via `open_read`; the writer only ever touches a COPY of the template.
2. **Client docs only (by default).** HL's own work product (`Analysis`/`Report`
   folders) is NOT a valid extraction source; the locator penalizes it heavily
   and the peek-verifier REJECTS files carrying HL letterhead/disclaimer
   language. This is the DEFAULT and prevents *accidental* use of HL work
   product. Two explicit, logged opt-outs exist: a per-run
   `restrict_to_client_sourced=False` (`LocateQuery`/`RunSlot`/`RunRequest`, the
   New Run "Restrict to client-sourced documents" toggle) turns BOTH guards off
   so the doc-type still ranks but nothing is excluded for being HL-sourced; and
   an explicit `LocateResult.from_override` manual pick runs even a REJECTED file
   (the verdict still rides along and the row gets a `MANUAL OVERRIDE` flag —
   never silent).
3. **Three-header-row workbook.** `reference/master_index_v4.xlsx` sheet "Index":
   row 1 = band name (appears at the band's first column; carry it forward),
   row 2 = field header, row 3 = description (controlled vocab + extraction
   instructions — the authoritative spec). Data starts row 4. 604 columns.
   `schema/master_schema.json` is compiled from it and is the single source of
   truth; the workbook structure is never hand-mirrored in code. The writer
   asserts row-2 headers against the schema before any write (hard abort on
   drift) and writes by column INDEX, never by header lookup.
4. **Never trust derived export columns.** The PV index export has corrupt
   derived columns (`#NAME?` from folder names starting with `+`). Everything is
   re-derived in Python from `file_path`.
5. **No hosted LLM API from Python, no API keys.** No `anthropic` import;
   ANTHROPIC_API_KEY and OPENAI_API_KEY are never required for extraction. The
   Phase-3 assist runs exclusively through local provider CLI subprocesses:
   Claude Code (`claude -p ...`) or temporary Codex CLI (`codex exec ...`),
   reusing the operator's local CLI login. Provider clients strip provider API
   keys from child environments. OCR is fully local (RapidOCR/onnxruntime by
   default; pytesseract optional) — never cloud. INFO logs never carry memo
   contents, client names, or page payload (identifiers/counters only).
6. **Nothing silent.** A numeric parse failure is a review flag, never a silent
   None. Every extracted cell carries verbatim evidence (≤200 chars), its page,
   and its confidence components, reproducible by a reviewer in <10 seconds.
7. **Derived fields are computed, not extracted.** When a derived field's inputs
   are present (EBITDA Margin, Mult Change, NAV Change %, Bridge Reconciles,
   Multiple Drift, the bps change fields), the Python computation wins; a
   disagreeing extracted value becomes a cross-check flag + conflict entry.

## Module map (src layout, Python >= 3.12)

    src/pv_extractor/
      models.py          pydantic contracts (FileRecord, LocateResult, FieldHit,
                         PageContent, VerifyResult, ReviewFlag, MemoResult,
                         EscalationPlan, ...)
      api/               Phase 4: FastAPI backend for the local GUI (never
                         reimplements pipeline logic): app.py (factory, static
                         frontend + SPA fallback), jobs.py (sqlite jobs+events,
                         ONE pipeline run at a time, RunControl bridge, log/
                         cost-tick bridge), routes_core.py (health/setup/doctor/
                         index meta + status + clients-status [top-level folder
                         list + per-folder indexed counts] + scan job [selective:
                         clients list / one root / full pv_root; throttled
                         scan_progress events with prev_total as rescan-ETA
                         baseline; deal-discovery refresh at the end; job
                         cancel = graceful PAUSE: pending batch committed, the
                         vanished-paths deletion skipped, partial result kept,
                         rescan later fast-forwards and continues; opt-in
                         quick=true mtime-prune skips re-listing unchanged leaf
                         folders] + enriched
                         /index/deals [names + deal_folders detail] +
                         /index/deals/refresh job [optional LLM assist] +
                         /index/search/{clients,deals,periods} fuzzy lookups
                         for the wizard's manual mode]/fs listing (?files= for
                         the add-a-missed-file picker) for the
                         folder picker/models/
                         config — GUI-editable locations incl. pv_root/
                         output_dir plus a validated raw config.yaml editor),
                         routes_runs.py (runs, review queue, evidence [any page;
                         drives the full-document viewer], jobs+WS,
                         jobs/{id}/selection [Confirm-documents table] +
                         runs/{id}/index-rows [this run's key Index columns] +
                         locator locate/override/overrides/verify-file +
                         search/* [Smart Search profiles/resolve/preview/
                         feedback] + multi-search/{selection,run} [Phase-C
                         batch] + index/deals/{feedback,learned} [Phase-A
                         deal-discovery learning]), services:
                         runs_service (+index_rows_mirror) / review_service /
                         evidence_service / preflight_service / selection_service
                         (locate+peek-verify per slot; SlotSelection now carries
                         misfiled/detected_period under enhanced_period_check) /
                         multi_search_service (expand_slots + firm-grouped
                         read-only build_multi_selection) / yaml_edit (ruamel,
                         comment-safe)
      config.py          typed config.yaml loader; per-client period_style;
                         extraction/peek_verify/validation sections (Phase 2)
      io_guard.py        read-only enforcement (rule 1)
      normalize.py       text/path normalization, version-signal parsing
      logging_setup.py   JSONL logging to output_dir/logs/, UTF-8 forced
      cli.py             typer CLI (`pv-extractor locate|run|ingest-xlsx|scan|...`)
      run.py             orchestrator: locate -> verify -> read -> target
                         -> extract -> validate -> [LLM escalation] -> write;
                         thread pool (I/O), per-memo failure isolation,
                         sha256+schema+extractor-version result cache, --dry-run
                         coverage; llm_settings=None = pure Phase-2 behavior;
                         RunControl = Phase-4 seam (optional progress events +
                         cooperative cancel -> DEFERRED coverage; None = exact
                         CLI behavior); Phase-C Multi-Search seam: RunSlot
                         (client/deal/period/doc_type/doc_type_spec/firm) +
                         run(...,slots=None) — slots=None = legacy single
                         run-wide-period path byte-for-byte unchanged; with
                         slots, one work-item per slot located with its OWN
                         period + doc_type (+ optional DocTypeSpec), a bad period
                         isolates to that slot's ERROR, ONE workbook for the
                         batch, events carry a firm 'group' lane (None = no group)
      schema/compile_schema.py   workbook rows 1-3 -> schema/master_schema.json
                                 + schema/band_routing.json (methodology -> bands)
      indexer/           SQLite file index: db.py (FTS5 + deal_folders table +
                         index_meta kv table [per-root last-scan baseline] +
                         Phase-G tables deal_finder_feedback / doc_type_profiles
                         / doc_search_feedback [all CREATE IF NOT EXISTS,
                         additive] + conn-first accessors),
                         derive.py, periods.py, ingest_xlsx.py, scan_tree.py
                         (incremental walk; opt-in quick=True mtime-prune skips
                         re-listing unchanged LEAF folders vs the last-scan
                         baseline — fast on the share, correct for new uploads,
                         blind only to in-place same-name overwrites),
                         deals.py (smart deal-folder discovery: segment
                         classification PERIOD/STRUCTURAL/ADMIN/NEUTRAL, deals
                         found by adjacency to date folders — above OR below
                         them, merged across periods; Phase-A branches:
                         ADMIN-wrapped deal containers, gated shared
                         mixed-investment buckets [one synthetic deal per
                         clustered investment, files split per-stem], multi-folder
                         same-name merge into one DealFolder with multiple
                         folder_paths — confidence-scored, persisted, files.deal
                         rewritten; runs after every scan/ingest;
                         deal_discovery.enabled=false = legacy deal-is-rel[1]
                         behavior),
                         deal_learning.py (Phase-A per-client learning:
                         record/list/delete_correction [add_folder|remove_folder|
                         merge|split|rename], derive/cached_layout_priors
                         [client-scoped, capped at learning.prior_bump, cached in
                         index_meta layout_priors:<client>], apply_feedback at the
                         END of refresh_deals — hard pins/excludes/merge/rename/
                         split then capped prior nudges; a correction on one deal
                         generalizes to other new deals under the same client;
                         no-op when learning.enabled=false)
      search/            Phase B Smart Search (rule-first, LLM-optional; FULLY
                         FUNCTIONAL WITH THE LLM OFF): doc_type_spec.py (CRUD over
                         doc_type_profiles + builtins migrated from
                         locator.doc_type_keywords [builtin=1, forkable-not-
                         deletable, anchors re-derived live from config];
                         resolve_spec, seed_builtins), intent.py (free-text ->
                         DocTypeSpec: a RULE layer that ALWAYS runs first and is
                         self-sufficient [built-in financial-doc lexicon UNION
                         config.smart_search.intent_rules; unknown queries fall
                         back to their own tokens], plus an OPTIONAL local
                         claude -p augmentation that only ADDS anchors and is
                         fully try/except-wrapped so any failure degrades to the
                         rule-only spec with no exception; provenance 'rules' or
                         'rules+cli'; reuses llm/claude_code_client, no SDK/key),
                         rank.py (transparent additive scoring: BM25 over
                         filename_include + rapidfuzz phrase blend + guarded regex
                         anchors + folder context + extension prior + period
                         evidence + negative penalty; deterministic
                         score-desc/path-asc; doc_search_feedback folds into
                         bounded per-token weight_overrides)
      locator/           scoring cascade + resolution statuses (Phase 1);
                         locate(conn,config,query,*,doc_type_spec=None) +
                         LocateQuery.doc_type_profile (slug) + ScoreContext.
                         doc_type_spec: when a DocTypeSpec is supplied the doc-
                         type/negative scoring uses its filename_include/regex/
                         exclude + folder_include/exclude + weight_overrides
                         instead of the static locator.doc_type_keywords lookup
                         (eligibility gate keys off the resulting matched
                         keywords); doc_type_spec=None = builtin behavior
                         byte-for-byte unchanged;
                         verify.py = Phase-2 peek-verifier (doc class, in-file
                         as-of/asset cross-check, AMBIGUOUS re-rank);
                         overrides.py = Phase-4 learning table (analyst picks
                         from the GUI locator review short-circuit locate() on
                         the resolved key — the pick is STILL peek-verified)
      extract/
        patterns.py      shared parse toolkit: amounts (paren negatives, scale
                         words, currencies), percent vs bps, multiples, dates,
                         basis tags, label:value lines, fuzzy table-cell lookup,
                         LABEL_DISCRIMINATORS (qualifier tokens fuzzy match must
                         never bridge: primary/tertiary, LTM/NTM, gross/net...)
        confidence.py    multiplicative FieldHit confidence (label x parse x
                         page-class x table/prose x ambiguity), config-tunable
        targeting.py     per-band anchor lexicons (curated seeds + mined headers
                         + config overrides) -> top-K pages/band + pages 1-3;
                         page->band map persisted for Phase-3 LLM page routing
        engine.py        per-memo pipeline: read, classify, OCR scanned pages,
                         target, methodology-routed band extraction, multi-asset
                         scoping ('Asset Review:' markers / docx sections),
                         derived computation
        cache.py         extraction_cache table (sha256 + schema ver + EXTRACTOR_VERSION)
        readers/         D1: pdf.py (pymupdf text/tables + pdfplumber fallback,
                         TEXT/SCANNED/IMAGE_TABLE/MIXED classification), ocr.py
                         (RapidOCR default / pytesseract optional, 300dpi, word
                         confidences, de-glue), docx.py (.doc -> UNSUPPORTED_FORMAT),
                         pptx.py, xlsx.py (read-only)
        bands/           D4: base.py (spec-driven extraction machinery), one
                         module per band family (fund, methodology, headline,
                         bridge, dcf, multiple, cap_rate, yield_credit, waterfall,
                         narrative), slots.py + comps.py + cap_structure.py
                         (TC/TX/CS positional slots: column mapping, deterministic
                         sort, overflow flags)
        derived.py       computed fields + extracted-vs-computed cross-check (rule 7)
      llm/               Phase 3: Claude Code CLI fallback (no SDK, no API key)
        claude_code_client.py  subprocess wrapper around the local `claude`
                         binary: auth/version/update probes, hidden
                         `claude -p --output-format stream-json --verbose
                         --json-schema` calls (NDJSON: each event line is turned
                         into an LLM-activity progress note as the model works —
                         `session started`/`using Read`/`using StructuredOutput`/
                         etc. — plus a wall-clock HEARTBEAT every
                         _HEARTBEAT_SECONDS so even an output-silent call (a
                         text-only payload streams nothing until the final tool
                         call) shows it is alive; so a long call shows live
                         progress instead of going silent until
                         the final envelope; the closing {type:result} line is
                         the same envelope the old `--output-format json` emitted
                         as one object, parsed by _result_envelope, which also
                         falls back to whole-buffer parse for single-object
                         output), ANTHROPIC_* stripped from child env, log
                         redaction;
                         claude_code.command_args = Windows->WSL bridge
                         (command: wsl, command_args: [-e, /abs/path/claude] —
                         wsl -e skips the login shell, so the path must be
                         absolute); `--json-schema` takes the schema JSON
                         INLINE (a string), NOT a file path — the client reads
                         the compiled schema file and passes its content (a path
                         makes the CLI JSON.parse the path and exit 1); inline
                         also needs no cwd/path translation across the WSL bridge;
                         non-zero exits record the CLI's stderr in the result
                         error (never a bare "exit N")
        model_registry.py  loads config/models.yaml (aliases/ids, editable
                         pricing, latest_alias/pinned/requires_explicit_enable);
                         AUTO/MANUAL routing -> tier ladder (sonnet -> opus;
                         OCR-hostile straight to opus; fable only on explicit
                         opt-in); cost math from the menu's pricing
        schema_builder.py  escalated fields -> strict band-grouped JSON schema
                         (additionalProperties:false everywhere; per-field
                         {value, unit, page, verbatim_quote, confidence,
                         not_found} defined ONCE in $defs and $ref'd per field
                         so a ~200-field schema is ~10KB inline, not ~100KB) +
                         byte-stable static prompt from workbook row-3
                         descriptions (no timestamps/memo names)
        payload.py       pages-not-documents payload: candidate pages + pages
                         1-3; TEXT pages as text + markdown pipe tables,
                         IMAGE_TABLE (+ low-confidence SCANNED) pages as
                         <=1080px PNGs; SCANNED pages that OCR cleanly (>=
                         ocr_text_min_confidence) sent as OCR TEXT instead of a
                         slow page image; OCR text retained for quote-grounding;
                         manifest hash. assemble_deal_payload() combines ALL of a
                         deal-period's documents into ONE payload under a global
                         page index (per-document labels) for one_call_per_deal
        cache.py         llm_cache table: sha256(static prompt + payload +
                         field set + model + effort + LLM_VERSION); --force-llm
                         bypasses reads
        costs.py         token estimator (labeled ESTIMATED vs actual CLI
                         usage), JSONL cost ledger, thread-safe BudgetTracker
                         (hard cap -> LLM_DEFERRED, run finishes cleanly)
        escalate.py      worker queue (1-2 hidden sessions); process_deals (ONE
                         call per deal-period over the combined payload, default,
                         _single_group) or legacy process_memos (per-memo,
                         band-batched); quote-grounding (exact on text pages, fuzzy
                         vs OCR on image pages; failure -> UNGROUNDED_LLM_VALUE),
                         merge policy (never overwrite deterministic conf >=
                         threshold; old value kept as conflict; merge_log in
                         the audit), NOT_EXTRACTABLE/LLM_UNCONFIRMED flags
        deal_discovery.py  opt-in Claude Code assist for deal discovery: one
                         call per client over a folder INVENTORY (paths +
                         counts + sample file NAMES, never contents); answers
                         grounded against the inventory (invented paths
                         discarded); default model is the 'sonnet' ALIAS so it
                         floats to the current cheap tier as the CLI updates;
                         heuristics stay primary (LLM corroborates/fills,
                         never removes)
      validate/          D5: checks.py (type/vocab/range from schema), rules.py
                         (table-driven cross-field rules from rules.yaml),
                         qoq.py (THRESHOLD FLAGS vs prior-period row), __init__.py
                         (QA verdict: qa_pass / qa_pass_with_flags / qa_fail)
      write/             D6: workbook.py (template COPY, header-drift abort,
                         append by col index, Review Flags dedupe on
                         (memo_id, description), Run Log; Phase-4 entry points:
                         update_cell by Memo ID + col index, resolve_flag),
                         audit.py (per-memo provenance JSON:
                         output_dir/<run_id>/audit/<memo_id>.json; the GUI
                         appends review_actions entries to the same files)
      system/claude_code.py   startup self-checks -> output_dir/logs/startup_checks.jsonl
      system/doctor.py        doctor check collection shared by CLI + GUI
      system/setup_check.py   Phase-4 first-run checks (deps from dist metadata,
                              OCR, frontend bundle, output_dir writability,
                              claude auth) + guarded pip self-install
    src/frontend/        Phase 4: Vite + React + TS + Tailwind v4 + framer-motion
                         (self-hosted Inter, no CDN). theme/tokens.css is the
                         SINGLE source of truth for the HL palette (PLACEHOLDER
                         values — see its TODO banner) and Tailwind maps onto it
                         in index.css @theme. Wizard state lives in
                         lib/wizard.tsx (a context above the router; survives tab
                         switches, not full reloads); the active index-scan job id
                         lives in lib/scanJob.tsx (same above-router context — the
                         scan status survives tab switches AND a reload by
                         reattaching to any still-active scan job, with a pulsing
                         indicator on the Settings nav item). lib/uiState.tsx is a
                         generic above-router key/value store + useStickyState (a
                         useState drop-in) so per-screen UI state — Settings form
                         choices, Output/Review filters — survives tab switches
                         (not reload). Phase-C New Run gained a top-level
                         Single | Multi Search mode switch (lib/wizard.tsx
                         searchMode default "single" + FirmEntry[] multi state):
                         Single = the existing 7-step wizard unchanged; Multi =
                         comma/Browse firm entry + per-firm regions
                         (FirmRegion.tsx + DocTypePicker.tsx: deal multi-select,
                         deal-folder add/remove, period, Smart Search doc-type
                         picker, per-firm llm_assist/enhanced_period_check/
                         deal_search_model), a firm-grouped Confirm preview
                         (misfiled badges), launch via /multi-search/run;
                         RunProgress/ProgressLanes lane by firm 'group' when
                         present (flat when absent); Settings scan UI gained a
                         Single | Multi multi-firm scan switch feeding the
                         existing {clients:[...]} scan body. Screens: Dashboard,
                         New Run
                         wizard (7 steps Scope→Template→AI/model→Preflight→Confirm
                         documents→Launch→Review; THREE folder-discovery modes:
                         Browse [discovered dropdowns + confidence chips +
                         selected-deal folder-path confirmation], Search by name
                         [debounced fuzzy client/deal/period lookups with full
                         relative-path previews], LLM assist [deal_discovery
                         refresh job with llm:true, model selectable, cheap
                         alias default]; dropdown-first periods everywhere;
                         Confirm-documents step curates the locator selection,
                         organized as PERIOD TABS -> per-client sections ->
                         per-deal rows (ranked by confidence) that expand inline
                         to ranked candidates (preview/swap/replace) — shows EVERY
                         period's documents, not just the first; swap/remove/add,
                         the folded-in AMBIGUOUS workflow),
                         Run Progress (lanes/cost meter/log tail/cancel; auto-
                         routes a finished non-dry run into Review), Review Queue
                         (j/k/a/e/u keyboard, evidence image + full-document
                         viewer, per-category bulk accept + accept-all-pending),
                         Output Browser (run-list digests + expandable preview;
                         per-run in-depth summary + filterable Index-rows preview),
                         Guide (analyst walkthrough), Settings (locations with
                         FolderPicker browse modal, selective per-client index
                         scan with live progress + rescan ETA, Learned locator
                         overrides admin, raw config editor); the standalone
                         Locator Review screen was retired. DataTable supports
                         opt-in spreadsheet filtering. Run Progress shows memo
                         X/Y + elapsed + rough ETA folded from stage events. Build:
                         npm install && npm run build -> dist/ (served by
                         the backend; gui.frontend_dist overrides)
    schema/              compiled JSON artifacts (committed)
    config/models.yaml   Claude Code model menu: aliases/full ids + EDITABLE
                         price-per-1M-token assumptions (seeded fable/opus/
                         sonnet/haiku; estimates, not sacred constants)
    rules.yaml           cross-field validation rules + per-field range overrides
    config.example.yaml                    version-controlled config TEMPLATE;
                                           config.yaml itself is git-IGNORED
                                           (machine-specific paths) and seeded
                                           from this on first bootstrap
    scripts/bootstrap.py | bootstrap.ps1   first-run setup (.venv, editable
                                           install, seed config.yaml from
                                           config.example.yaml if missing)
    scripts/sync_to_windows.sh             WSL repo -> C:\dev\pv-extractor (rsync;
                                           ships dist/ so Windows needs no Node;
                                           never overwrites the dest config.yaml,
                                           seeds one if missing)
    Start PV Extractor.bat                 one-click Windows launcher: bootstraps
                                           .venv if missing, then pv-extractor gui
    tests/fixtures/build_fixture.py        synthetic PV tree generator
    tests/fixtures/docgen.py               synthetic document primitives (ruled
                                           tables, scanned pages, image tables,
                                           encrypted PDFs, docx/pptx/xlsx)
    tests/fixtures/build_memos.py          realistic memo content (RICH_BUILDERS)
                                           consumed by golden extraction tests

## Conventions

- Type hints everywhere; pydantic v2 models for all cross-module data.
- stdlib `logging` (JSONL); `print()`/rich only in `cli.py` and `scripts/`.
- Every tunable (weights, thresholds, keyword lists, OCR engine, confidence
  factors, paths) lives in `config.yaml`; aliases in `aliases.yaml`;
  cross-field rules in `rules.yaml`. Nothing magic inline. `config.yaml` is
  git-IGNORED (each machine keeps its own paths); the committed template is
  `config.example.yaml` — add any NEW tunable to BOTH so fresh checkouts get
  the default. `scripts/bootstrap.py` seeds `config.yaml` from it on first run.
  The Search & Selection revamp added `deal_discovery.learning`
  (`enabled`, `prior_bump`), `deal_discovery.layout_priors` (manual-override
  default `{}` — LIVE priors live in the index DB, not config),
  `deal_discovery.{shared_bucket_enabled, shared_bucket_min_clusters,
  shared_bucket_name_match_threshold, cluster_ratio_threshold}` +
  `deal_discovery.weights.{admin_container, shared_bucket}`, plus the
  `smart_search` and `multi_search` sections — all present in BOTH
  `config.py` and `config.example.yaml`. The deal-recognition refinements added
  `deal_discovery.{exclude_generic_deal_names, deal_name_stopwords}` and
  `locator.surface_period_matches_without_doctype` (likewise in BOTH).
- Normalization convention (matches the existing export): lowercase, any
  non-alphanumeric -> space, collapse runs; see `normalize.py` docstring.
- IDs: `MEMO_<YYYYMMDD>_<HHMMSS>_<NNN>` (multi-asset rows suffix `-A2`, `-A3`...),
  `RUN_<YYYYMMDD>_<HHMMSS>`.
- Windows-first: UNC paths, `\\?\` long-path prefix via
  `normalize.to_extended_path`, OneDrive cloud-only placeholders flagged
  (`is_cloud_placeholder`), UTF-8 logging regardless of console code page.
- Two-digit years pivot at 70 (>=70 -> 19xx else 20xx) in `indexer/periods.py`.
- FieldHit.method: `deterministic` (parsed from the document), `computed`
  (derived in Python), `metadata` (run identity),
  `llm:<provider>:<model>:<effort>` (Phase-3 merge; evidence = the grounded
  verbatim quote). Workbook booleans are written 'Yes'/'No', dates as ISO
  strings, never formulas.
- Field evidence has a first-class `EvidenceRef`. Legacy `FieldHit.page`,
  `FieldHit.evidence`, and `FieldHit.bbox` stay populated for migration, but
  new code should read/write `evidence_ref`. Any bbox is exactly
  `(x0, y0, x1, y1)` in PDF points in PyMuPDF page coordinates (top-left
  origin, `page.rect` units). Native/OCR quote alignment may produce a box;
  otherwise keep page + quote with `match_method="page_only"` and a
  `no_geometry_reason`. Never create a bbox from the extracted value alone.
- Bump `extract.EXTRACTOR_VERSION` on any change that can alter extraction
  output — it invalidates the result cache. Bump `llm.LLM_VERSION` on any
  change that can alter prompts/payloads/parsing/merging — it invalidates the
  local LLM response cache.

## Deal discovery in one paragraph

Deal folders are FOUND, not assumed (`indexer/deals.py`, after every
scan/ingest): the legacy deal-is-first-segment-under-the-client rule fails on
real trees (Ares: deals under strategy groups, under project codenames, or
BELOW the period folders; some client folders hold no deals). Discovery builds
the client's folder tree from the index, splits each segment name into its
investment-name part + any date it carries (`_name_and_period`), and classifies
it (PERIOD — name is PURELY a date, incl. bare years; NAME+EMBEDDED-DATE — `PBC
(12.31.2023)` is the deal `PBC` at that period, so siblings sharing the base name
MERGE into one deal across periods rather than becoming N date-stamped deals;
STRUCTURAL — all date-stripped tokens structural/glue, or From/To correspondence;
ADMIN; NEUTRAL), and walks down: a NEUTRAL node is a
container when its period children hold recurring neutral subfolders
(deal-below-period, merged across periods by normalized name), when >= 2
neutral children carry their own period evidence (strategy group), or when it
is a bare single-child wrapper; otherwise it IS a deal — UNLESS its date-stripped
name is entirely generic (structural/glue/grouping/admin/`deal_name_stopwords`:
`Research`, `Reports`, `Prior Period`), in which case it is a document bucket,
never a deal (dropped, gated by `exclude_generic_deal_names`). Three Phase-A branches
extend this: an ADMIN node that wraps a genuine period/memo-bearing descendant
becomes a CONTAINER (recursed into; the admin node is never itself a deal,
`evidence.admin_container`); a neutral folder directly holding memo files for
>= `shared_bucket_min_clusters` distinct investments (rapidfuzz asset-key
clustering, gated by `deal_discovery.shared_bucket_enabled`) emits ONE synthetic
deal per cluster sharing the folder path (`evidence.shared_bucket`/`name_filter`;
`assign_file_deals` splits the bucket's files per-stem to the best cluster,
unmatched -> deal=NULL); and same-name candidates sharing a non-period container
merge into one DealFolder with multiple `folder_paths`. Confidence is an
additive clamp-to-[0,1] of config-tunable components (period evidence,
multi-period, structural children, memo-keyword files, flat-layout prior,
grouping-name/depth penalties, the new admin_container/shared_bucket weights);
results persist to `deal_folders` and
files.deal is rewritten (NULL for files under no deal), so locate(), run
scopes, /index/deals and the wizard all see them. Same-name deals in
different branches get "(parent)" suffixes. The opt-in LLM assist
(`llm/deal_discovery.py`, manual or auto under llm.trigger_confidence) sends
ONE Claude Code call per client with a folder inventory; ungrounded paths are
discarded and heuristic deals are never removed, only corroborated
(+confidence bump) or gap-filled. A per-client LEARNING layer
(`indexer/deal_learning.py`, on when `deal_discovery.learning.enabled`) records
analyst corrections (add/remove/merge/split/rename) and, at the END of
`refresh_deals`, applies them as hard pins/excludes/merge/rename/split plus
capped client-scoped layout priors (bounded by `learning.prior_bump`, cached in
`index_meta`); a correction on one deal generalizes to other new deals under the
same client.

## Locator in one paragraph

FTS5 prefilter (client+deal alias tokens, capped at `locator.fts_candidate_limit`)
then deterministic Python scoring with per-component breakdowns: client/deal match
(exact > normalized > rapidfuzz token_set_ratio vs aliases.yaml), period match
(date-folder parse == target as-of > period in filename > modified-time window),
doc-type keywords vs negative keywords, source-class gate (client bonus,
report/analysis penalty, archive multiplier), extension prior, and version-family
ranking (vf/final > vN > " (00N)" > undecorated; ties by modified_time; families
via rapidfuzz ratio >= `family_ratio_threshold` on decoration-stripped stems).
Statuses: FOUND / AMBIGUOUS / NOT_FOUND / NOT_YET_UPLOADED (deal exists with
other-period date folders but nothing for the target period) / ACCESS_ERROR.
When nothing matches the requested doc TYPE but real (non-negative) documents DO
exist for the target period, the cascade surfaces them as AMBIGUOUS for human
pick rather than a bare NOT_YET_UPLOADED (gated by
`locator.surface_period_matches_without_doctype`, checked after the ACCESS_ERROR
gate) — so the preflight always has something to Replace with.
Phase 2 then content-verifies the ranked candidates (`locator/verify.py`):
HL work product or a wrong in-file quarter/asset REJECTS and re-ranks; an
AMBIGUOUS result whose survivors collapse to one candidate upgrades to FOUND.
On the AMBIGUOUS auto-select, peek confidence leads ONLY when a candidate
VERIFIED; when NONE verify (e.g. every candidate is a SCANNED PDF the peek can't
read — a scanned valuation memo reads 0.0 "couldn't inspect" and must not lose to
a readable-but-off-type doc like a DDQ), the locator's final score leads instead.
Phase-B doc-type routing: `locate()` accepts an optional `doc_type_spec`
(a Smart Search `DocTypeSpec`, addressable by `LocateQuery.doc_type_profile`
slug); when supplied, the doc-type/negative scoring and the eligibility gate use
the spec's filename/folder include-exclude lists + `weight_overrides` instead of
the static `locator.doc_type_keywords` lookup; when None, builtin DocType
behavior is byte-for-byte unchanged.

## Smart Search in one paragraph

Smart Search (`search/`, Phase B) turns free-text into a `DocTypeSpec` and is
FULLY FUNCTIONAL WITH THE LLM OFF. `intent.py` runs a RULE layer first that is
self-sufficient (a built-in financial-doc lexicon UNIONed with
`config.smart_search.intent_rules`; an unknown query falls back to its own
tokens), then an OPTIONAL local `claude -p` augmentation that only ADDS anchors
and is fully try/except-wrapped — a missing/unauthed binary, timeout, malformed
JSON or budget failure degrades to the rule-only spec with no exception
(provenance `rules` vs `rules+cli`; reuses `llm/claude_code_client`, no SDK/key).
`doc_type_spec.py` is CRUD over the `doc_type_profiles` table plus builtins
migrated from `locator.doc_type_keywords` (builtin=1, anchors re-derived live
from config, forkable-not-deletable). `rank.py` scores candidates with a
transparent additive model (BM25 over filename_include + rapidfuzz phrase blend +
guarded regex anchors + folder context + extension prior + period evidence +
negative penalty; deterministic score-desc/path-asc), and folds
`doc_search_feedback` corrections into bounded per-token `weight_overrides`. The
resolved spec routes through the locator (above), so Smart Search and the run
pipeline agree on doc-type scoring.

## Multi-Search in one paragraph

Multi-Search (Phase C, option-a) drives the SAME per-slot pipeline. `run()` gains
`slots: list[RunSlot]|None` — `slots=None` is the legacy single run-wide-period
path byte-for-byte unchanged; with slots, each `RunSlot`
(client/deal/period/doc_type/optional DocTypeSpec/firm) is located with its OWN
period and doc-type, a bad period isolates to that slot's ERROR (never aborts the
batch), ONE workbook is written for the batch, and every event carries a firm
`group` lane. `api/multi_search_service.py` `expand_slots()` builds the slots
(per-firm `refresh_deals` LLM assist via the existing local `claude -p` path,
`deal_learning` corrections, `search.doc_type_spec.resolve_spec`,
`selection_service.slot_selection`) and `build_multi_selection()` returns a
firm-grouped preview that is READ-ONLY on the learning table (corrections persist
only on the run path, deduped). Under `enhanced_period_check`,
`selection_service` surfaces a slot whose best doc's in-file as-of
(`VerifyResult.asof_date`) disagrees with the target as MISFILED with the
document's true `detected_period` (never fabricated; off by default = unchanged).
`api/jobs.py` `start_multi_run` runs under the same single-active-pipeline guard
with firm-grouped events and a `scope="multi"` run summary
(`multi_search={firm_count, slot_count, firms}`). The New Run wizard's
Single | Multi switch and the Settings multi-firm scan drive `/multi-search/*`.

## Extraction in one paragraph

The engine summarizes pages (text + image-geometry metrics -> TEXT / SCANNED /
IMAGE_TABLE / MIXED), OCRs SCANNED pages locally (IMAGE_TABLE pages are flagged
for Phase-3 vision, not OCR'd), scores pages against per-band anchor lexicons
and hands each band extractor only its top-K pages plus pages 1-3. Methodology
bands run only where `schema/band_routing.json` routes the extracted
Primary/Secondary Methodology. Scalar fields resolve via fuzzy table-cell
lookup (row label x column header) and label:value prose lines; positional
slots (TC/TX/CS) extract whole table rows, sort deterministically (comps by
name; cap structure by seniority then size) and flag overflow. Values are
unit-normalized (USD millions, %, bps, x; local-currency fields keep their
currency) with the verbatim raw preserved; controlled vocab maps
exact -> alias -> fuzzy >= 90, below which the field stays empty + flag.
Confidence = label x parse x page-class (OCR pages scale by mean word
confidence) x table/prose x ambiguity; conflicting candidates ride along on
the hit. Validation layers schema checks, rules.yaml cross-field rules, QoQ
continuity (THRESHOLD FLAGS vs the prior-period row of the same asset in the
output workbook) and hard failures (no valuation value, in-file as-of
mismatch, corrupt file) into qa_pass / qa_pass_with_flags / qa_fail.

## LLM fallback in one paragraph

The deterministic engine still runs first (fast/local: grounding, comps/cap
tables, derived fields). `llm.provider` selects the local structured-extraction
provider (`claude` or temporary `codex`), and merged hits are labelled
`llm:<provider>:<model>:<effort>`. Optional `llm.combine_deal_documents` groups a
deal-period's documents into one combined payload using the same merge key as
the multi-doc row collapse; it sends only escalated fields, does not imply
force_assist, and does not bypass the LLM response cache. Deprecated
`llm.one_call_per_deal` is still loaded for compatibility, maps to
`combine_deal_documents` only when the new key is absent, and warns.
`max_pages_per_deal` bounds the combined payload.

Default per-memo path (`combine_deal_documents=false`): each memo whose EscalationPlan
has fields goes through the
worker queue (config.llm.workers hidden provider CLI sessions). The plan
(run._build_escalation) is low-confidence hits + required-but-empty fields, and
— when an asset QA-FAILS (engine recognized nothing / no valuation value) or
force_assist is set — EVERY empty LLM-extractable field (excludes
IDENTIFICATION/QA/THRESHOLD bands, computed fields per rule 7, positional
slots), so a memo the engine could not parse still gets a real LLM pass rather
than an empty not_needed plan. Force LLM assist (CLI --force-llm-assist, the New
Run AI-step toggle, LlmRunOptions.force_llm_assist) makes the LLM the primary
extractor and ALSO bypasses the deterministic result cache (and is never written
back to it). payload.py
re-reads ONLY the candidate pages + pages 1-3 (text + pipe tables; image
pages as <=1080px PNGs viewed via the Read tool), schema_builder.py compiles
the escalated fields into a byte-stable static prompt + strict band-grouped
JSON schema. Extraction is BAND-BATCHED by default (llm.band_batched):
escalate._build_groups splits the plan into focused work groups — one per band
that has page-anchor evidence (the Phase-2 targeting._page_score scorer run over
the band's candidate-page text) OR a required/low-confidence field, ordered by
descending relevance, plus ONE cheap sweep over the bands with no evidence
(no_evidence_effort, single pass, no retry ladder). Each group's call sends only
that band's pages + a small schema, so the model engages deeply instead of
rushing one ~190-field giant call; the most-relevant bands run first so the
budget is spent well. band_batched=false = single-call-per-memo. SMALL-DOC
COLLAPSE (llm.single_call_max_pages, default 8): when the LLM payload is <= that
many pages, _build_groups ignores band_batched and runs the whole doc + all
fields over one page set — band-batching's per-call page re-uploads only pay off
on large memos, so short client memos collapse to cheap/fast calls (0 disables).
The field set is still chunked by max_fields_per_call: the response schema is
passed INLINE on the `claude` command line and Windows caps that at ~32 KB, so a
one-shot ~200-field schema (~40 KB) fails to launch ([WinError 206]).
The response schema uses $defs/$ref (the per-field shape defined ONCE) so it is
~10x smaller and the whole ~200-field set fits ONE inline-schema call under the
Windows limit (max_fields_per_call default 200). Cleanly-OCR'd SCANNED pages are
sent as OCR TEXT, not page images (prefer_ocr_text_over_image / ocr_text_min_
confidence) — vision/Read-tool calls are slow and re-done per call; IMAGE_TABLE
stays an image. New tunables (BOTH config.py + config.example.yaml):
combine_deal_documents, max_pages_per_deal, band_batched,
single_call_max_pages, retry_not_found, surface_ungrounded_values,
ungrounded_confidence_cap, prefer_ocr_text_over_image, ocr_text_min_confidence,
band_relevance_floor, max_fields_per_call, no_evidence_effort. The router picks a
tier ladder per group — MANUAL forces one
model+effort for EVERYTHING (no escalation, even OCR-hostile docs stay on the
chosen model); AUTO runs sonnet/medium, retries failures on opus/high
(OCR-hostile bands start at opus, retry at xhigh), and touches fable only on
explicit opt-in. A field the model answers not_found is RESOLVED, not retried
(llm.retry_not_found=False default): a confirmed absence is not a failure, so it
neither re-asks the expensive tier nor raises a NOT_EXTRACTABLE flag — only
FAILED (call error) or REJECTED (ungrounded/type/vocab) fields escalate. Each group-tier is ONE local
provider CLI structured-output call (job id pv-<run>-<memo>-g<group>t<tier>; cached by
prompt+payload+fields+page-scope+model+effort; budget
reserved before launch, LLM_DEFERRED past the cap). Answers are
quote-grounded against local page text (fuzzy vs OCR on image pages),
type/vocab-checked, then merged: fill empty fields, replace only below-threshold
deterministic values (loser kept as a conflict), never touch
confident/computed/metadata hits. A value that PARSES but whose quote can't be
matched on the page (common on SCANNED/OCR pages) is, by default
(llm.surface_ungrounded_values), SHOWN as a low-confidence UNGROUNDED_LLM_VALUE
hit for review rather than discarded — but it only fills EMPTY fields, never
overwrites an existing deterministic value (it rides along as a conflict
instead). When EVERY call for a memo fails, ONE LLM_PASS_FAILED flag carries the
CLI's real error (e.g. the 400/timeout) instead of a flag per field. Leftover
fields that genuinely FAILED/were rejected (not confirmed-absent) get
NOT_EXTRACTABLE / LLM_UNCONFIRMED reviewer flags; every attempt
(job id, session id, tokens, cost actual-vs-ESTIMATED) lands in the audit
record, the run's cost_ledger.jsonl and the Run Log "Batch Sessions" column.
After all LLM merges and after any multi-document merge, `finalize_asset_after_assistance`
reruns deterministic derived fields, validation, QA, threshold fields and flag
counts from the final hit set so stale pre-LLM failures are not persisted.

## Running

    python scripts/bootstrap.py            # creates .venv, editable install
    claude auth login                      # once when llm.provider=claude
    codex login                            # once when llm.provider=codex (uses local CLI auth)
    .venv/bin/pv-extractor locate --client "Angelo Gordon" --deal "Accell" \
        --period "2025-01-31" --doc-type valuation_memo
    .venv/bin/pv-extractor run --scope deal --client "Angelo Gordon" \
        --deal "Accell" --period "2025-01-31"          # full pipeline (LLM fallback on)
    .venv/bin/pv-extractor run --scope all --period "Q1 2026" --dry-run
    .venv/bin/pv-extractor run ... --no-llm            # pure Phase-2 behavior
    .venv/bin/pv-extractor run ... --llm-model sonnet --llm-effort low \
        --llm-budget 5 --force-llm                     # manual routing / cache bypass
    .venv/bin/pv-extractor run ... --llm-model opus --llm-effort low \
        --force-llm-assist                             # LLM as primary extractor (escalate everything)
    .venv/bin/pv-extractor deals --client "Ares Management" --refresh   # discovered deal folders
    .venv/bin/pv-extractor deals --client "Ares Infrastructure" --llm   # + local LLM second opinion
    .venv/bin/pv-extractor deals --client "Ares Management" --show-learned  # learned priors + corrections
    .venv/bin/pv-extractor deals --client "Ares Management" --forget    # clear this client's corrections
    .venv/bin/pv-extractor models          # model menu + editable pricing
    .venv/bin/pv-extractor costs --run RUN_20260611_120000
    .venv/bin/pv-extractor doctor          # provider CLI / auth / flags / menu / cost accounting
    .venv/bin/pv-extractor gui             # Phase-4 analyst GUI (127.0.0.1, opens browser;
                                           # self-installs the gui extra per first_run config)
    .venv/bin/python -m pytest             # full suite
    .venv/bin/python -m pytest -m "not perf"   # skip perf smoke tests
    .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium  # one-time, for the GUI smoke
    PV_GUI_SMOKE=1 .venv/bin/python -m pytest tests/test_gui_smoke.py  # Playwright (opt-in): full Single + Multi wizard flows

`run` copies the template (default `reference/master_index_v4.xlsx`, or pass a
previous output workbook for cumulative runs) to
`output_dir/<run_id>/master_index_<run_id>.xlsx`, appends one Index row per
memo-asset, deduped Review Flags, a Run Log row, and writes per-memo audit
JSON. Re-running against the same output copy is idempotent (result cache +
existing-row/flag dedupe); `--force` bypasses the cache.

GUI notes: `pv-extractor gui` starts uvicorn bound to the loopback only
(GuiConfig refuses any other host — there is no auth) and serves the built
frontend from `src/frontend/dist` (`scripts/bootstrap.py --with-gui` builds
it). All long operations are jobs persisted in `output_dir/gui/jobs.sqlite`
(events replayable — reopening the browser reattaches to a running job);
GUI-launched runs leave a `run_summary.json` in the run dir for the
dashboard, and CLI runs are summarized from audits + the cost ledger. The
review queue edits ONLY the run's own workbook copy via the writer seam and
appends every action to the memo's audit JSON. Optional stretch (not built):
PyInstaller onefile of `pv_extractor.cli:app` with `src/frontend/dist` and
`schema/` as added data — document, don't block on it.

Tests build a synthetic PV tree under `tests/fixtures/pv_root/` (generated, not
committed) including realistic memos (multiples/DCF/yield+cap-structure/
waterfall, a scanned memo for the OCR path, an image-table memo, joint
multi-asset PDF/docx reviews, an xlsx workbook). The reference workbooks in
`reference/` are read-only inputs — never modify them. Schema drift test:
recompiling against the reference workbook must produce byte-identical
`schema/master_schema.json`. Golden tests freeze >=40 fields per text memo;
if you change extractor behavior deliberately, re-freeze from a verified run.
OCR golden values depend on the RapidOCR model version. The default models
ship INSIDE the rapidocr pip package (verified via its wheel RECORD) — no
runtime downloads ever happen with the default config; `pip install` is the
only step that needs network access. NO test launches the real Claude Code
CLI by default: escalation tests inject `tests/fixtures/fake_claude.py`
(canned schema-valid responses incl. a fabricated quote, malformed JSON and
not_found-heavy cases) and the client tests run against a fake `claude`
executable; the single opt-in live test requires
`PV_LIVE_CLAUDE_CODE_TESTS=1` plus a passing `claude auth status`.
