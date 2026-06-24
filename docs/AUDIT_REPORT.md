# Integrity and Dead-Code Audit

Date: 2026-06-24

Scope: CLI/entry point, API app and job manager, preflight/dry-run, deterministic extraction and targeting, validation/finalization, provider assistance, workbook writer/audit log, and review/evidence UI.

## Active/reachable

- CLI entry: `pyproject.toml` exposes `pv-extractor = pv_extractor.cli:app`; `src/pv_extractor/cli.py` calls the same `run()` pipeline used by the GUI.
- API app and job manager: `src/pv_extractor/api/app.py` wires `routes_core`, `routes_runs`, and `JobManager`; `tests/test_gui_api.py` and `tests/test_job_manager.py` exercise this path.
- Preflight/dry-run: `POST /api/jobs/run` with `dry_run=true` reaches `JobManager.start_run()`, `run(..., dry_run=True)`, and `preflight_service.estimate_from_dry_run()`.
- Deterministic extraction and targeting: `run.py` calls locator, readers, `extract.engine`, page targeting, derived fields, and validation before write.
- Validation/finalization: `validate.finalize.finalize_asset_after_assistance()` and validation rules are active; `tests/test_finalization.py` covers LLM-filled value finalization and flag counts.
- Provider assistance: `llm.escalate` chooses provider clients through `llm.provider`; Claude and Codex clients are reachable by config. Fake-provider tests cover planner/timeouts without live CLI calls.
- Workbook writer/audit log: `write.workbook`, `write.audit`, and run summaries are exercised by writer, golden extraction, GUI API, and review tests.
- Review/evidence UI: `review_service`, `evidence_service`, `ReviewQueue.tsx`, and `reviewEvidence.ts` are active; tests cover field-specific bbox selection and one-highlight-at-a-time behavior.

## Configuration-gated/optional

- OCR: RapidOCR is configured under `extraction.ocr`; Tesseract remains optional via the `tesseract` extra. Do not remove.
- Non-PDF readers: DOCX/PPTX/XLSX readers are optional but reachable through `reader_for_extension()`. Do not remove.
- Model providers: Claude and Codex paths are both config-selected. Claude setup/update UI is provider-specific; startup auto-update is now gated to `llm.provider == "claude"`.
- Smart Search and deal discovery LLM assists are optional local CLI paths. Deal discovery still uses the Claude-specific assist path by design.
- GUI frontend build, setup checks, and first-run install helpers are local-operator paths and remain reachable.

## Compatibility shim/deprecated

- `llm.one_call_per_deal` is deprecated and mapped to `llm.combine_deal_documents` in `load_config()` when the new key is absent.
- Legacy `claude-code:<model>:<effort>` method strings are normalized to `llm:` chips in review UI and retained in deal-discovery index metadata for old rows.
- Legacy evidence fields (`page`, `bbox`, `evidence`) are synchronized with `EvidenceRef` for old audit JSON compatibility.
- `selection_service._selection_for_job` remains as a back-compat alias for the original preflight path.
- `llm.band_batched` and `llm.single_call_max_pages` remain compatibility knobs; bounded task planning is the active behavior.

## Proven unreachable/dead

- No code was removed. The audit did not find a path that was both demonstrably unreachable and covered by tests strongly enough to justify deletion in this stage.

## Suspicious/requires runtime evidence

- Deal-discovery storage still names legacy methods as `claude-code:*`; this is intentional compatibility, but a future provider-neutral deal-discovery refactor should migrate the stored method namespace.
- Claude-specific setup endpoints (`/api/claude/sources`, `/api/setup/claude-update`) are intentionally provider-specific and hidden/limited in the UI when provider is not Claude. Keep watching for accidental unconditional callers.
- Static dead-code tooling is not configured in this repo. `tsc -b` covers frontend type reachability; Python currently relies on tests plus `compileall`.

## Cleanups performed

- Refactored job polling to a resilient state machine with deterministic tests.
- Made pipeline job reservation atomic and added preflight fingerprint reuse.
- Added structured 409 active-job details and frontend active-job actions.
- Persisted safe failed-job diagnostics in `jobs.sqlite` and exposed copyable summaries in the GUI.
- Marked stale queued/running/cancelling jobs interrupted on startup with a visible safe summary.
- Changed stale provider-neutral wording in the payload builder and New Run model label.
- Gated startup Claude auto-update on active provider being Claude.

## Migrations/deprecations

- `jobs.sqlite` now adds nullable `fingerprint` and `diagnostic_json` columns on startup.
- `llm.one_call_per_deal` remains deprecated; use `llm.combine_deal_documents`.
