export const meta = {
  name: 'pv-extractor-phase1-review',
  description: 'Adversarial spec-compliance review of Phase 1, with per-finding verification',
  phases: [
    { title: 'Review', detail: 'one reviewer per deliverable + engineering-rules reviewer' },
    { title: 'Verify', detail: 'adversarial verification of each finding' },
    { title: 'Critic', detail: 'completeness critic over the verified picture' },
  ],
}

const ROOT = '/home/nthollo26/dev/pv-extractor'
const PY = `${ROOT}/.venv/bin/python`
const SPEC = '/tmp/pvx-spec.md'

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          file: { type: 'string' },
          title: { type: 'string' },
          claim: { type: 'string', description: 'what is wrong / missing, with concrete evidence (line refs, command output)' },
          spec_ref: { type: 'string', description: 'the spec sentence this violates' },
          suggested_fix: { type: 'string' },
        },
        required: ['severity', 'file', 'title', 'claim', 'spec_ref'],
      },
    },
    checked_ok: { type: 'array', items: { type: 'string' }, description: 'spec requirements you verified ARE satisfied' },
  },
  required: ['findings', 'checked_ok'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    is_real: { type: 'boolean', description: 'true only if the finding is a genuine spec violation or defect worth fixing in Phase 1' },
    reasoning: { type: 'string' },
    corrected_fix: { type: 'string', description: 'the fix you would actually apply (may differ from the reviewer suggestion); empty if not real' },
  },
  required: ['is_real', 'reasoning'],
}

const REVIEW_COMMON = `
You are an adversarial spec-compliance REVIEWER for "PV Extractor" Phase 1 (read-only review: do NOT
modify any file). Project root: ${ROOT}. The full original specification is at ${SPEC} — read it FIRST,
then read CLAUDE.md. Venv: ${PY} (you may run pytest/scripts/snippets to gather evidence; tests currently
pass 258/258 — your job is to find what the tests DON'T prove).
Hunt for: spec requirements not implemented, implemented differently than specified, silently weakened
specs in tests, edge cases that would break on the REAL share (UNC paths, Windows, millions of rows,
weird folder names), read-only violations, config keys promised but missing/unused, dead code, missing
type hints/docstrings, print() outside the CLI layer.
Do NOT report: style nits, hypothetical Phase-2 features, things the spec explicitly defers, or
intentional documented deviations UNLESS they contradict a hard spec requirement. Quality over quantity —
each finding needs concrete evidence (file:line or command output). Verify a suspicion by running code
before reporting it.`

phase('Review')

const REVIEWERS = [
  {
    key: 'D0-bootstrap',
    prompt: `${REVIEW_COMMON}
Your scope: D0 (scripts/bootstrap.py, scripts/bootstrap.ps1, src/pv_extractor/system/claude_code.py,
the five required config.yaml keys, startup_checks.jsonl behavior, "never require ANTHROPIC_API_KEY",
no anthropic import / no external API anywhere in src/). Run bootstrap.py --help / a dry pass if safe.
Check the ps1 for logic errors by reading it carefully (no PowerShell available here).
Also: grep ALL of src/ for anthropic/requests/httpx/urllib network usage.`,
  },
  {
    key: 'D1-scaffold',
    prompt: `${REVIEW_COMMON}
Your scope: D1 (pyproject.toml pins + deps exactly as specified incl. python-dateutil actually used or
justified, src layout, CLAUDE.md <150 lines AND covering: architecture, module map, conventions,
three-header-row rule, client-docs-only rule, read-only rule, how to run tests; config.yaml contains
EVERY tunable named anywhere in the spec — go through the spec line by line hunting for numbers/lists
that should be config; the read-only rule enforcement + the grep unit test in tests/test_readonly_guard.py
— try to construct a write path that ESCAPES the guard, e.g. os.fdopen, Path.open, sqlite attach,
shutil, tempfile under pv_root, and check whether the grep test would catch a future regression).`,
  },
  {
    key: 'D2-schema',
    prompt: `${REVIEW_COMMON}
Your scope: D2 schema compiler (src/pv_extractor/schema/compile_schema.py, schema/master_schema.json,
schema/band_routing.json, tests/test_schema_compiler.py).
INDEPENDENTLY re-derive the truth: open reference/master_index_v4.xlsx yourself (read_only=True) and
sample 40+ columns across all bands; check the compiled dtype/unit/vocab/slot/required values against
YOUR OWN reading of the header+description text. Hunt for: wrong dtypes (numeric fields compiled as
string and vice versa), vocab lists that are wrong/truncated/bogus (enum where free text was meant),
band carry-forward errors, slot mis-parses, the drift test being weaker than byte-identical,
band_routing entries that would route a methodology to a wrong band. Report the WORST 40-column sample
disagreements as findings with column numbers.`,
  },
  {
    key: 'D3-indexer',
    prompt: `${REVIEW_COMMON}
Your scope: D3 indexer (src/pv_extractor/indexer/{db,derive,ingest_xlsx,scan_tree}.py, tests/test_indexer.py).
Check against spec D3 sentence by sentence: 15 mirror columns all re-derived; FTS5 over the two normalized
columns; executemany 5k batches; progress logging; os.scandir not walk; \\\\?\\ long paths; per-entry
PermissionError/OSError -> scan_errors; never follow symlinks/junctions; incremental (size,mtime) compare
updating ONLY changed rows; derived columns exactly as specified (client/deal/date_folder/source_class
8-value set/is_archive rules/version_signal). Stress-test derive_record yourself with nasty paths:
pv_root itself, file directly under pv_root, client-only depth, trailing slashes, mixed separators,
'\\\\?\\UNC\\' prefixed paths, folder named '+Prior (8.31.24) Reports', case differences in
Client/CLIENT folder names. Check FTS trigger correctness on UPDATE (does the fts row actually change?)
and that fts_candidates survives quotes/special chars in match expressions.`,
  },
  {
    key: 'D4-periods',
    prompt: `${REVIEW_COMMON}
Your scope: D4 period resolver (src/pv_extractor/indexer/periods.py, tests/test_periods.py).
Verify EVERY format in the spec list parses to the right date and the 70-pivot. Hunt for: false POSITIVES
(folder names that should be None but parse — try real-world garbage: 'HSRE 11', 'MHC 3', version strings,
'Top 10 Assets', 'Project 2025', 'Phase 1.2', IP-like '10.0.0.1', '(HL 9-30-22)' filenames),
resolve_target_period style handling (fiscal quarter math — recompute by hand for fiscal(3) and fiscal(6)),
period_label correctness, filename_contains_period token-boundary bugs (substring leaks like '103 31 2026'
or '12 31 2025 2' variants), and whether 60+ genuine test cases exist. Run your own probe snippets.`,
  },
  {
    key: 'D5-locator',
    prompt: `${REVIEW_COMMON}
Your scope: D5 locator (src/pv_extractor/locator/*.py, tests/test_locator_unit.py, the locate CLI command
in src/pv_extractor/cli.py). Check the cascade order and EVERY weight is read from config (grep for float
literals in scoring paths); alias resolution exact>normalized>fuzzy semantics; period match strong/medium/
weak incl. the [as-of, as-of+75d] window direction; doc-type keyword sets and negative keywords from
config; source-class gate; extension prior; version ranking vf>vN>(00N)>undecorated with mtime tiebreak;
family ratio >= 92 on normalized stems; verify_candidate stub returning UNVERIFIED; the five statuses'
exact semantics (FOUND gap logic vs family heads, AMBIGUOUS top-5, NOT_YET_UPLOADED vs NOT_FOUND
distinction, ACCESS_ERROR). Probe adversarially with a scratch db: two deals whose names fuzzy-collide
('Summit Ridge Energy' vs 'Summit Ridge Storage'), a deal name that is a substring of another ('Accell' vs
'Accell II'), case-only differences, a query period with no parseable form. Confirm every scoring
component is logged per candidate (spec: 'every component logged per candidate'). Check <2s perf claim
methodology in tests/test_perf_smoke.py (is the timed path actually representative? warm-up? FTS used?).`,
  },
  {
    key: 'D6-fixture',
    prompt: `${REVIEW_COMMON}
Your scope: D6 fixture + e2e suite (tests/fixtures/build_fixture.py, tests/test_locator_e2e.py,
tests/test_readonly_guard.py, tests/test_perf_smoke.py, tests/conftest.py).
Walk the spec's D6 scenario list item by item and verify each is genuinely exercised AND asserted (not
just present in the tree): 3 clients x 2-3 deals; >=6 observed date-folder formats incl '(N) m.d.yy' and
a '+' folder; Client/Report/Analysis/Archive subfolders; same memo in Client+Archive with Client winning;
HL lookalike losing on source_class; v1/v2/vf and (002) copies; DO NOT USE file; NOT_YET_UPLOADED deal;
NOT_FOUND deal; joint vehicle 'AIOF II ANRP III'; loose file with period in name; pymupdf 1-page PDFs;
zero-byte .pdf; a .doc; a .xlsm; ~20 e2e query cases with EXACT winner/status assertions; ingest test fed
'#NAME?' rows verifying correction; read-only guard grep test. Look for assertions weakened to make tests
pass (e.g. 'in candidates' where spec means 'is winner') and fixture files that exist but are never
queried. Also check the fixture tree builds deterministically twice (same mtimes).`,
  },
  {
    key: 'ENG-rules',
    prompt: `${REVIEW_COMMON}
Your scope: the == ENGINEERING RULES == and == DO NOT == sections, repo-wide. Mechanically verify:
type hints on every public function (spot-check 20 across modules); pydantic for cross-module data (find
dict-shaped data crossing module boundaries); module docstrings everywhere; dead code (grep for unused
functions/imports — run a quick vulture-style manual pass on each src module); print() outside cli.py/
scripts (grep); JSONL logging actually wired (does anything call logging_setup.setup_logging besides the
CLI? do indexer/locator emit log_event with components?); magic numbers inline that the spec says belong
in config; UTF-8/cp1252 safety; OneDrive placeholder attribute check present and correct (verify the
attribute constants against Windows docs from memory: FILE_ATTRIBUTE_OFFLINE=0x1000,
FILE_ATTRIBUTE_RECALL_ON_OPEN=0x40000, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS=0x400000); no anthropic
import; nothing modifies reference/ (check fixture/test tmp usage); no OCR/extraction/GUI code.`,
  },
]

const reviewed = await pipeline(
  REVIEWERS,
  (r) => agent(r.prompt, { label: `review:${r.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  (res, r) => {
    if (!res) return null
    const fs = (res.findings || []).map((f) => ({ ...f, area: r.key }))
    log(`${r.key}: ${fs.length} findings, ${(res.checked_ok || []).length} requirements verified ok`)
    return parallel(
      fs.map((f) => () =>
        agent(
          `You are an adversarial VERIFIER for a code-review finding on PV Extractor Phase 1
(root ${ROOT}, spec at ${SPEC}, venv ${PY}, full test suite currently 258/258 green). Read-only: modify nothing.
Your default stance is SKEPTICAL: reviewers over-report. Reproduce the claim yourself (read the exact
files/lines, run code if needed). Rule it NOT real if: the spec doesn't actually require it, the behavior
is correct on closer reading, it's an explicitly documented+acceptable deviation, or it's purely
hypothetical for Phase 1 (e.g. requires Windows-only verification we cannot do here — unless the code is
clearly wrong by inspection). Rule it REAL only if you can state precisely what is wrong and how to fix it.

FINDING (area ${f.area}, severity ${f.severity}):
file: ${f.file}
title: ${f.title}
claim: ${f.claim}
spec_ref: ${f.spec_ref}
suggested_fix: ${f.suggested_fix || '(none given)'}`,
          { label: `verify:${f.title.slice(0, 40)}`, phase: 'Verify', schema: VERDICT_SCHEMA },
        ).then((v) => ({ ...f, verdict: v })),
      ),
    )
  },
)

const allVerified = reviewed.filter(Boolean).flat().filter(Boolean)
const confirmed = allVerified.filter((f) => f.verdict && f.verdict.is_real)
const rejected = allVerified.filter((f) => f.verdict && !f.verdict.is_real)
log(`Verification done: ${confirmed.length} confirmed, ${rejected.length} rejected`)

phase('Critic')
const critic = await agent(
  `You are a completeness CRITIC for PV Extractor Phase 1 (root ${ROOT}, spec ${SPEC}, venv ${PY};
read-only). ${confirmed.length} findings were already confirmed by other reviewers (titles:
${confirmed.map((f) => f.title).join(' | ') || 'none'}). Do NOT re-report those. Your question is purely:
WHAT IS MISSING that nobody checked? Go through the spec's '== DELIVERABLES ==' and 'Work plan' sections
one clause at a time and tick them off against the repo (e.g.: does a perf smoke test exist AND run? does
the CLI print score components? is there a startup self-check CLI entry? does ingest show progress? are
aliases user-extensible/documented? does anything verify Review Flags / Run Log sheet header awareness
promised by 'inspect their headers'? is the work plan's 'coverage report of ~20 locator cases' satisfiable
from the tests?). Report ONLY genuine gaps as findings (same bar of evidence).`,
  { label: 'critic:completeness', phase: 'Critic', schema: FINDINGS_SCHEMA },
)
const criticFindings = (critic?.findings || []).map((f) => ({ ...f, area: 'CRITIC' }))
const criticVerified = await parallel(
  criticFindings.map((f) => () =>
    agent(
      `Adversarial verifier (skeptical default; read-only; root ${ROOT}; spec ${SPEC}; venv ${PY}).
Reproduce and judge this completeness-gap claim; NOT real if the spec doesn't require it for Phase 1 or
it exists somewhere the critic missed.
FINDING (severity ${f.severity}): file: ${f.file}; title: ${f.title}; claim: ${f.claim};
spec_ref: ${f.spec_ref}; suggested_fix: ${f.suggested_fix || '(none)'}`,
      { label: `verify:${f.title.slice(0, 40)}`, phase: 'Critic', schema: VERDICT_SCHEMA },
    ).then((v) => ({ ...f, verdict: v })),
  ),
)
const criticConfirmed = criticVerified.filter(Boolean).filter((f) => f.verdict && f.verdict.is_real)

return {
  confirmed: confirmed.concat(criticConfirmed).map((f) => ({
    area: f.area, severity: f.severity, file: f.file, title: f.title, claim: f.claim,
    spec_ref: f.spec_ref, fix: (f.verdict && f.verdict.corrected_fix) || f.suggested_fix || '',
    verifier_reasoning: f.verdict ? f.verdict.reasoning : '',
  })),
  rejected: rejected.concat(criticVerified.filter(Boolean).filter((f) => f.verdict && !f.verdict.is_real)).map((f) => ({
    area: f.area, severity: f.severity, title: f.title, why_rejected: f.verdict ? f.verdict.reasoning : '',
  })),
}