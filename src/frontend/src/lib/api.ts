/* Typed client for the local backend. The frontend renders these payloads
   verbatim — every business value is computed server-side. */

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: string, rawDetail?: unknown) {
    super(detail);
    this.status = status;
    this.detail = rawDetail ?? detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    let rawDetail: unknown = detail;
    try {
      const body = await res.json();
      const d = body.detail ?? body;
      rawDetail = d;
      // FastAPI validation errors carry detail as a list of objects — render
      // them as text, never as "[object Object]".
      detail = typeof d === "string" ? d : JSON.stringify(d);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail, rawDetail);
  }
  return res.json() as Promise<T>;
}

export const get = <T>(path: string, init?: RequestInit) => request<T>(path, init);
export const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

/* ---------- shared types (mirroring api/schemas.py + services) ---------- */

export interface JobInfo {
  id: string;
  kind: string;
  status: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  run_id: string | null;
  params: Record<string, unknown>;
  result: RunResult | Record<string, unknown> | null;
  error: string | null;
  diagnostics?: JobDiagnostics | null;
  last_seq: number;
}

export interface JobDiagnostics {
  summary?: string;
  exception_type?: string;
  stage?: string;
  context?: Record<string, unknown>;
  active_job?: JobConflictJob;
  [key: string]: unknown;
}

export interface JobConflictJob {
  id: string;
  kind: string;
  status: string;
  created_at: string;
}

export interface JobConflictDetail {
  code: "active_pipeline_job";
  message: string;
  active_job: JobConflictJob;
}

export interface JobEvent {
  seq: number;
  ts: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface CoverageEntry {
  client: string;
  deal: string;
  status: string;
  detail: string;
}

export interface LlmSummary {
  enabled: boolean;
  executed: boolean;
  memos_escalated: number;
  memos_deferred: number;
  attempts: number;
  cache_hits: number;
  total_cost_usd: number;
  cost_source: string | null;
  detail: string;
}

export interface RunResult {
  run_id: string;
  dry_run: boolean;
  scope: string | null;
  client?: string | null;
  deal?: string | null;
  period: string | null;
  doc_type?: string;
  coverage: CoverageEntry[];
  coverage_counts: Record<string, number>;
  clients?: string[];
  deals?: string[];
  companies?: string[];
  source_files?: number;
  sources?: { file_name: string | null; file_path: string | null; client: string | null; deal: string | null }[];
  memos: number;
  assets: number;
  rows_added: number | null;
  flags_added: number | null;
  cache_hits?: number;
  qa_counts: Record<string, number>;
  duration_minutes: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string;
  workbook_path: string | null;
  llm: LlmSummary;
  diagnostics?: Record<string, unknown>;
  source?: string;
  cancelled?: boolean;
}

export interface IndexRow {
  memo_id: string | null;
  fund_manager: string | null;
  portfolio_company: string | null;
  reporting_period: string | null;
  valuation_date: string | null;
  primary_methodology: string | null;
  headline_value: number | string | null;
  moic: number | string | null;
  qa_status: string;
}

export interface EvidenceRef {
  source_id: string | null;
  source_file: string | null;
  display_page: number | null;
  pdf_page_index: number | null;
  quote: string;
  raw_text: string;
  bbox: number[] | null;
  bbox_coordinate_system: "pdf_points_topleft_page_rect" | string;
  match_method: "native_text" | "table_cell" | "ocr_word_alignment" | "manual_box" | "page_only";
  match_score: number | null;
  word_ids: string[];
  span_ids: string[];
  provenance: string;
  provider: string | null;
  extraction_method: string | null;
  no_geometry_reason: string | null;
}

export interface MemoIssue {
  id: string;
  run_id: string;
  memo_id: string;
  source_filename: string;
  descriptions: string[];
  categories: string[];
  severity: string;
  reviewer_attention: boolean;
  resolved: boolean;
  resolution: Record<string, unknown> | null;
}

export interface ReviewItem {
  id: string;
  kind: "flag" | "low_confidence";
  run_id: string;
  memo_id: string;
  row_memo_id: string;
  client: string;
  deal: string;
  asset_name: string | null;
  qa_status: string;
  source_filename: string;
  field: string | null;
  band: string | null;
  value: boolean | number | string | null;
  raw_text: string;
  unit: string | null;
  method: string | null;
  confidence: number | null;
  evidence: string;
  evidence_ref: EvidenceRef | null;
  evidence_refs: EvidenceRef[];
  grounding_status: "box" | "page_only" | "none" | string;
  grounding_reason: string;
  issue_code: string;
  issue_descriptions: string[];
  reviewer_comment: string | null;
  page: number | null;
  bbox: number[] | null;
  has_page_image: boolean;
  reader: string;
  source_page_count: number;
  category: string;
  description: string;
  severity: string;
  reviewer_attention: boolean;
  qa_fail_reasons: string[];
  conflicts: Record<string, unknown>[];
  resolved: boolean;
  resolution: Record<string, unknown> | null;
}

export interface ReviewQueueResponse {
  items: ReviewItem[];
  memo_issues: MemoIssue[];
  confidence_threshold: number;
}

export interface PageWords {
  page: number;
  page_count: number;
  width: number; // PDF points
  height: number; // PDF points
  words: { x0: number; y0: number; x1: number; y1: number; text: string }[];
}

export interface ModelEntry {
  provider: string;
  alias: string;
  id: string;
  display_name: string;
  context_window: number;
  default_effort: string;
  latest_alias: boolean;
  pinned: boolean;
  requires_explicit_enable: boolean;
  pricing_per_mtok: {
    input: number;
    output: number;
    cache_hit: number;
    cache_write_5m: number;
    cache_write_1h: number;
  } | null;
  extraction_profile?: {
    default_shape: "adaptive" | "deal" | "document";
    deal_pass_max_input_tokens: number;
    deal_pass_max_documents: number;
    deal_pass_max_image_pages: number;
    deal_pass_max_fields: number;
    oversized_target_fields: number;
    max_parallel_document_calls: number;
  };
}

export interface ModelsResponse {
  last_reviewed: string;
  models_path: string;
  provider: string;
  models: ModelEntry[];
  all_models: ModelEntry[];
  llm: {
    enabled: boolean;
    provider: string;
    routing_mode?: string;
    mode: string;
    single_model_provider?: string;
    single_model_model?: string;
    single_model_effort?: string;
    manual_model: string;
    manual_effort: string;
    allow_fable: boolean;
    budget_usd: number;
    auto: Record<string, string>;
  };
}

export interface SetupItem {
  name: string;
  ok: boolean;
  detail: string;
  remediation: string | null;
}

export interface DoctorCheck {
  check: string;
  ok: boolean;
  detail: string;
}

export interface PreflightEstimate {
  label: string;
  mode: string;
  found: number;
  estimated_total_usd: number;
  estimated_worst_case_usd: number;
  budget_usd: number;
  over_budget: boolean;
  memos: {
    client: string;
    deal: string;
    file_name: string | null;
    page_count: number | null;
    payload_pages: number;
    first_tier: string;
    first_tier_usd: number;
    ladder_usd: number;
    documents?: number;
    pages?: number;
    image_pages?: number;
    fields?: number;
    estimated_input_tokens?: number;
    provider?: string;
    model?: string;
    effort?: string;
    execution_shape?: string;
    expected_primary_calls?: number;
    repair_policy?: string;
    max_repair_calls?: number;
    reason?: string;
  }[];
  assumptions: Record<string, unknown>;
}

export interface LocateCandidate {
  record: { file_path: string; file_name: string; source_class: string; modified_time: string | null };
  breakdown: Record<string, number | string | string[]>;
  family_rank: number;
}

export interface LocateResult {
  status: string;
  evidence: string;
  candidates: LocateCandidate[];
  winner: LocateCandidate | null;
  query: { client: string; deal: string; period: string; doc_type: string; as_of_date: string | null };
}

export interface SelectionCandidate {
  file_name: string;
  file_path: string;
  last_modified: string | null;
  score: number;
  family_rank: number;
  verify_status: string;
  doc_class: string;
  verify_reason: string;
  is_selected: boolean;
}

export interface SelectionSlot {
  client: string;
  deal: string;
  slot_key: string;
  period: string;  // the slot's requested period label (drives the period tabs)
  doc_type: string;  // the slot's requested doc-type (slug or enum value)
  status: string;
  as_of_date: string | null;
  predicted_period: string;
  override_in_effect: boolean;
  detail: string;
  file_name: string | null;
  file_path: string | null;
  last_modified: string | null;
  page_count: number | null;
  doc_class: string;
  verify_status: string;
  confidence: number | null;
  score: number | null;
  extra_docs?: string[];  // multi-doc merge: extra source files for this slot
  candidates: SelectionCandidate[];
  // Multi-search additions (backend now returns these on multi-path slots;
  // optional so the single-firm SelectionResponse path still type-checks).
  doc_type_slug?: string;
  misfiled?: boolean;
  detected_period?: string | null;
  detected_as_of?: string | null;
}

export interface SelectionResponse {
  job_id: string;
  scope: string;
  period: string;
  doc_type: string;
  found: number;
  slots: SelectionSlot[];
  doc_types?: string[];
  periods?: string[];
  slot_count?: number;
}

/* ---------- multi-search (Phase C) ---------- */

/* The llm option block shared by the single-firm /jobs/run request and the
   multi-search /multi-search/run request. */
export interface LlmRunOptions {
  enabled: boolean;
  routing_mode?: string | null;
  mode: string | null;
  model: string | null;
  effort: string | null;
  single_model?: { provider?: string; model?: string; effort?: string } | null;
  deal_overrides?: Record<string, unknown>[];
  repair_policy?: "never" | "core_only" | null;
  budget_usd: number | null;
  force_llm_assist?: boolean;
}

/* One firm row in a multi-search request, mirroring the backend MultiSearchFirm. */
export interface MultiSearchFirm {
  client: string;
  deals: string[]; // [] means all-discovered deals for the client
  period: string;
  doc_types: string[]; // builtin DocType values and/or Smart Search profile slugs; [] -> config default
  llm_assist: boolean;
  enhanced_period_check: boolean;
  deal_search_model: string | null;
  added_folders: string[];
  removed_deals: string[];
}

/* A deal folder preview row returned in the multi-search selection. */
export interface DealFolderPreview {
  name: string;
  confidence: number;
  method: string;
  low_confidence: boolean;
  folder_paths: string[];
  periods: string;
  file_count: number;
  memo_file_count: number;
  llm_corroborated: boolean;
}

/* A multi-search slot: the existing SelectionSlot shape plus the new fields
   the backend attaches on the multi path (doc_type_slug always present here). */
export interface MultiSlot extends SelectionSlot {
  doc_type_slug: string;
  misfiled: boolean;
  detected_period: string | null;
  detected_as_of: string | null;
}

/* Per-firm selection result. */
export interface MultiFirmSelection {
  client: string;
  period: string;
  doc_types: string[];
  enhanced_period_check: boolean;
  deal_folders_preview: DealFolderPreview[];
  slots: MultiSlot[];
  found: number;
}

/* Response of POST /multi-search/selection (read-only preview). */
export interface MultiSelectionResponse {
  firms: MultiFirmSelection[];
}

/* Request body of POST /multi-search/run. */
export interface MultiSearchRunRequest {
  firms: MultiSearchFirm[];
  template?: string | null;
  dry_run?: boolean;
  force?: boolean;
  llm: LlmRunOptions;
}

/* ---------- Smart Search profiles (GET /search/profiles, POST /search/profiles/resolve) ---------- */

export interface DocTypeProfile {
  slug: string;
  label: string;
  filename_include: string[];
  filename_regex: string[];
  filename_exclude: string[];
  folder_include: string[];
  folder_exclude: string[];
  extensions: string[];
  period_required: boolean;
  weight_overrides: Record<string, number>;
}

export interface ProfileResolveResponse {
  spec: DocTypeProfile;
  provenance: string;
}

export interface VerifyFileResponse {
  client: string;
  deal: string;
  as_of_date: string;
  file_path: string;
  indexed: boolean;
  status: string;
  doc_class: string;
  reason: string;
  asof_date: string | null;
  asset_names: string[];
  confidence: number;
  would_pass: boolean;
}

export interface ConfigResponse {
  config_path: string;
  pv_root: string;
  output_dir: string;
  db_path: string;
  claude_code: { command: string; command_args: string[]; auto_update_on_start: boolean; default_timeout_seconds: number; allow_cli_usage: boolean };
  codex_cli: { command: string; command_args: string[]; default_timeout_seconds: number; model: string | null; reasoning_effort: string; debug_capture_raw_response: boolean };
  first_run: { install_missing_deps: boolean };
  gui: { host: string; port: number; open_browser: boolean; evidence_dpi: number; frontend_dist: string | null };
  llm: Record<string, unknown> & {
    enabled: boolean;
    provider: string;
    routing_mode?: string;
    mode: string;
    single_model_provider?: string;
    single_model_model?: string;
    single_model_effort?: string;
    manual_model: string;
    manual_effort: string;
    allow_fable: boolean;
    budget_usd: number;
    auto: Record<string, string>;
  };
  extraction: { confidence_threshold: number };
  deal_discovery: { display_min_confidence: number };
  selection: { min_confidence: number };
}

export interface RawConfigResponse {
  config_path: string;
  text: string;
}

export interface BuildInfo {
  version: string;
  commit: string | null;
  commit_full: string | null;
  committed_at: string | null;
  branch: string | null;
  dirty: boolean | null;
  python: string;
  label: string;
}

export interface HealthResponse {
  ok: boolean;
  version: string | null;
  build?: BuildInfo;
  llm_provider: string;
  auto_update_on_start: boolean;
}

export interface ClaudeSource {
  id: string;
  label: string;
  command: string;
  command_args: string[];
  available: boolean;
  version: string | null;
  detail: string;
  selected: boolean;
}

export interface ClaudeSourcesResponse {
  platform: "windows" | "posix";
  current: { command: string; command_args: string[] };
  sources: ClaudeSource[];
  diagnostics: {
    python: string;
    sys_platform: string;
    which_claude: string | null;
    which_wsl: string | null;
  };
}

export interface DiscoveredIndex {
  path: string;
  files: number | null;
  clients: number | null;
  readable: boolean;
  detail: string;
  size_bytes: number;
  modified: string | null;
  is_current: boolean;
}

export interface IndexDiscoverResponse {
  current_db_path: string;
  current_exists: boolean;
  scanned_dirs: string[];
  found: DiscoveredIndex[];
}

export interface IndexStatus {
  db_path: string;
  pv_root: string;
  pv_root_exists: boolean;
  ready: boolean;
  files: number;
  clients: number;
  db_error?: string | null;
  relocation?: { path: string; from: string; detail: string } | null;
}

export interface ScanStats {
  roots: string[];
  files_seen: number;
  added: number;
  updated: number;
  unchanged: number;
  removed: number;
  errors: number;
  elapsed_seconds: number;
  stopped_early?: boolean; // paused — the index keeps what was scanned; a rescan continues
  quick?: boolean; // ran as a quick rescan (unchanged leaf folders skipped by mtime)
}

export interface ScanProgress {
  root: string;
  root_index: number;
  roots_total: number;
  prev_total: number;
  files_seen: number;
  added: number;
  updated: number;
  unchanged: number;
  removed: number;
  errors: number;
  dir: string;
  elapsed_seconds: number;
}

export interface ClientFolder {
  name: string;
  path: string;
  files: number;
  last_scan?: string | null; // ISO timestamp of this client's last index scan (null = never)
}

export interface ClientsStatus {
  pv_root: string;
  folders: ClientFolder[];
  db_error?: string | null;
}

export const evidenceUrl = (runId: string, memoId: string, page: number, bbox: number[] | null) => {
  const params = new URLSearchParams({ page: String(page) });
  if (bbox && bbox.length === 4) {
    params.set("l", String(bbox[0]));
    params.set("t", String(bbox[1]));
    params.set("r", String(bbox[2]));
    params.set("b", String(bbox[3]));
  }
  return `/api/runs/${runId}/evidence/${memoId}?${params}`;
};

/** Rendered page image of an arbitrary candidate file (Confirm documents). */
export const candidatePreviewUrl = (filePath: string, page = 1) =>
  `/api/locator/preview?file_path=${encodeURIComponent(filePath)}&page=${page}`;
