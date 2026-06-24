"""Pydantic models shared across modules.

These are the cross-module contracts: the indexer produces FileRecord rows,
the locator consumes them and produces LocateResult, the schema compiler
produces SchemaField entries. No module defines its own ad-hoc dicts for
data that crosses a module boundary.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from pydantic import BaseModel, Field


class DocType(str, enum.Enum):
    valuation_memo = "valuation_memo"
    ic_memo = "ic_memo"
    portfolio_review = "portfolio_review"
    any_client_valuation_doc = "any_client_valuation_doc"


class SourceClass(str, enum.Enum):
    """Classification of the nearest classifiable ancestor folder."""

    client = "client"
    report = "report"
    analysis = "analysis"
    research = "research"
    support = "support"
    archive = "archive"
    admin = "admin"
    other = "other"


class ResolutionStatus(str, enum.Enum):
    FOUND = "FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    NOT_YET_UPLOADED = "NOT_YET_UPLOADED"
    ACCESS_ERROR = "ACCESS_ERROR"


class VerifyStatus(str, enum.Enum):
    UNVERIFIED = "UNVERIFIED"  # Phase 1: filename-only stub
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class PeriodStyleKind(str, enum.Enum):
    quarterly_calendar = "quarterly_calendar"
    monthly = "monthly"
    fiscal = "fiscal"


class PeriodStyle(BaseModel):
    """Per-client reporting cadence. Parsed from config strings like
    'quarterly_calendar', 'monthly', 'fiscal(6)' (fiscal year ending June)."""

    kind: PeriodStyleKind = PeriodStyleKind.quarterly_calendar
    fiscal_year_end_month: int | None = None  # 1..12, only for kind=fiscal


class SchemaField(BaseModel):
    """One column of the master index workbook, compiled from header rows 1-3."""

    col_index: int  # 1-based workbook column
    band: str  # row-1 group name, carried forward across the band
    header: str  # row-2 field header (verbatim)
    description: str  # row-3 description (verbatim; authoritative spec)
    dtype: str  # string | number | boolean | date | enum | percent | basis_points | multiple_x | years | integer
    controlled_vocab: list[str] | None = None
    unit: str | None = None  # e.g. USD_millions, millions_local, percent, bps, x, years, MW
    slot_group: str | None = None  # TC | TX | CS
    slot_number: int | None = None  # 1-based slot within the group
    required: bool = False


class VersionSignal(BaseModel):
    """Version decoration parsed from a filename stem (D5e)."""

    rank: int  # 3 = vf/final, 2 = vN, 1 = " (00N)" copy, 0 = undecorated
    version_number: int | None = None  # N for vN
    copy_number: int | None = None  # N for " (00N)"
    raw: str = ""  # the matched decoration, e.g. "vf", "v3", "(002)"


class FileRecord(BaseModel):
    """One row of the `files` table. The first 15 fields mirror the PV index
    export columns; everything below `archive_or_old_flag` is derived in
    Python from file_path (derived source columns are never trusted)."""

    file_name: str
    file_path: str
    folder_path: str
    parent_folder: str
    extension: str  # lowercase, leading dot: ".pdf"
    size_bytes: int | None = None
    modified_time: datetime | None = None
    depth_from_pv_root: int | None = None  # FOLDER segments below pv_root (file excluded)
    normalized_file_name: str = ""
    normalized_folder_path: str = ""
    normalized_full_path: str = ""
    contains_memo_keyword: bool = False
    contains_q4_2025_signal: bool = False
    contains_q1_2026_signal: bool = False
    archive_or_old_flag: bool = False
    # --- derived ---
    client: str | None = None  # first segment under pv_root
    deal: str | None = None  # second segment under pv_root
    date_folder: str | None = None  # nearest ancestor folder that parses to a date
    as_of_date: date | None = None  # parsed from date_folder
    source_class: SourceClass = SourceClass.other
    is_archive: bool = False
    version_signal: VersionSignal | None = None
    is_cloud_placeholder: bool = False  # OneDrive cloud-only file (present but not local)
    is_zero_byte: bool = False
    row_id: int | None = None  # sqlite rowid once stored


class ScanError(BaseModel):
    """One unreadable entry encountered during scan_tree."""

    path: str
    error_type: str  # PermissionError, OSError, ...
    message: str
    seen_at: datetime


class DealEvidence(BaseModel):
    """Why deal discovery believes a folder is a deal folder — every
    confidence component is kept so the decision is reproducible."""

    period_children: int = 0  # distinct periods among the folder's own date children
    period_recurrence: int = 0  # distinct period ancestors the name recurs under (deal-below-period layout)
    structural_children: int = 0  # Client/Analysis/Reports/... folders directly beneath it (or its periods)
    memo_keyword_files: int = 0  # files in the subtree hitting doc-type keywords
    total_files: int = 0  # all files in the subtree
    container_depth: int = 0  # grouping folders between the client and the deal
    llm_corroborated: bool = False  # an LLM pass independently named this folder
    admin_container: bool = False  # this deal was surfaced by recursing INTO an admin container
    shared_bucket: bool = False  # synthetic per-cluster deal carved out of a shared mixed-investment folder
    name_filter: list[str] = Field(  # representative cluster tokens used to assign files within a shared bucket
        default_factory=list
    )
    components: dict[str, float] = Field(default_factory=dict)  # name -> weighted contribution


class DealFolder(BaseModel):
    """One discovered deal under a client. `folder_paths` usually holds a
    single path; deal-below-period layouts (client\\...\\<period>\\<deal>)
    yield one path per period folder the deal recurs under."""

    client: str
    name: str  # raw folder name as it appears on the share
    folder_paths: list[str]
    confidence: float  # 0..1, from config.deal_discovery weights
    method: str = "heuristic"  # heuristic | claude-code:<model>:<effort>
    evidence: DealEvidence = Field(default_factory=DealEvidence)


class DocTypeSpec(BaseModel):
    """A learnable description of 'what document to find' — the configurable
    profile behind a doc-type slug. The builtin DocType enum values map to
    seeded specs; analysts can define and refine additional slugs whose
    filename/folder anchors and per-component weight nudges accumulate from
    learning. Token/phrase matching uses the repo's normalization convention."""

    slug: str
    label: str
    filename_include: list[str] = Field(default_factory=list)  # token/phrase synonyms (normalized match)
    filename_regex: list[str] = Field(default_factory=list)  # raw patterns e.g. r"10[- ]?q"
    filename_exclude: list[str] = Field(default_factory=list)
    folder_include: list[str] = Field(default_factory=list)  # folder-context anchors
    folder_exclude: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)  # ['.pdf', '.htm']; empty = any
    period_required: bool = True
    weight_overrides: dict[str, float] = Field(default_factory=dict)  # per-component nudges from learning


class DealFinderPlan(BaseModel):
    """Structured output of Deal Finder 2.0 for ONE client: a layout
    classification plus the per-deal folder sets (reusing DealFolder), the
    learned corrections that were applied, and a human-readable rationale.
    Phase-A scaffold — the shape (notably `layout` labels and the
    learned-prior keys) will be refined as the finder is implemented."""

    client: str
    layout: str = "unknown"  # 'flat' | 'deal_below_period' | 'strategy_group' | 'admin_wrapped' | 'shared_bucket' | 'mixed'
    deals: list[DealFolder] = Field(default_factory=list)
    applied_feedback: list[str] = Field(default_factory=list)  # human-readable markers of learned corrections applied
    learned_priors: dict[str, float] = Field(default_factory=dict)  # per-client prior nudges in effect
    rationale: str = ""


# ---------------------------------------------------------------------------
# Multi-Search (Phase C) — scaffold contracts for the firm-level batch search.
# Behavior is NOT implemented here; these are the data shapes only.
# ---------------------------------------------------------------------------


class MultiSearchSlot(BaseModel):
    """One resolved (client, deal, period, doc_type) target within a
    multi-search request. `doc_type` is either a builtin DocType enum value
    or a DocTypeSpec profile slug."""

    client: str
    deal: str
    period: str
    doc_type: str = "any_client_valuation_doc"  # builtin enum value OR a doc_type_profile slug


class MultiSearchFirmSpec(BaseModel):
    """One firm's slice of a multi-search request: the deals (explicit, or
    empty = all discovered for the client), the period, the doc-types to
    locate, and per-firm discovery toggles/overrides."""

    client: str
    deals: list[str] = Field(default_factory=list)  # explicit; empty = all discovered
    period: str
    doc_types: list[str] = Field(default_factory=list)  # builtin enums and/or profile slugs
    llm_assist: bool = False
    enhanced_period_check: bool = False
    deal_search_model: str | None = None
    added_folders: list[str] = Field(default_factory=list)
    removed_deals: list[str] = Field(default_factory=list)


class MultiSearchRequest(BaseModel):
    """Top-level firm-level batch search/launch request. The run-level LLM
    options are carried as a forward-compatible dict placeholder: the real
    typed contract (LlmRunOptions) lives in api/schemas.py, which imports FROM
    models.py — referencing it here would be a circular import. Phase C wires
    the concrete type at the api boundary."""

    firms: list[MultiSearchFirmSpec] = Field(default_factory=list)
    template: str | None = None
    dry_run: bool = False
    llm: dict = Field(default_factory=dict)  # forward-compat placeholder for api.schemas.LlmRunOptions (wired in Phase C)


class LocateQuery(BaseModel):
    client: str
    deal: str
    period: str  # raw user input, e.g. "2025-01-31", "Q1 2026"
    doc_type: DocType = DocType.any_client_valuation_doc
    doc_type_profile: str | None = None  # DocTypeSpec slug; None = use the builtin doc_type enum exactly as today
    as_of_date: date | None = None  # resolved from `period` + client period_style
    # When True (default): HL's own work product is rejected by the peek-verifier
    # and report/analysis folders are penalized in scoring (CLAUDE.md rule 2).
    # When False ("don't restrict to client-sourced"): those guards are OFF —
    # doc-type keywords still RANK matching files, but nothing is excluded for
    # being HL/non-client (client, HL, anything is a candidate).
    restrict_to_client_sourced: bool = True


class ScoreBreakdown(BaseModel):
    """Every scoring component for one candidate, for transparent ranking."""

    client_deal_score: float = 0.0
    client_deal_method: str = "none"  # exact | normalized | fuzzy | none
    period_score: float = 0.0
    period_method: str = "none"  # folder | filename | mtime | none
    doctype_score: float = 0.0
    matched_keywords: list[str] = Field(default_factory=list)
    negative_score: float = 0.0
    matched_negative_keywords: list[str] = Field(default_factory=list)
    source_class_score: float = 0.0
    extension_score: float = 0.0
    version_score: float = 0.0
    do_not_use_penalty: float = 0.0
    zero_byte_penalty: float = 0.0
    raw_total: float = 0.0  # sum of components before the archive multiplier
    archive_multiplier: float = 1.0
    final_score: float = 0.0


class CandidateFile(BaseModel):
    record: FileRecord
    breakdown: ScoreBreakdown
    family_key: str | None = None  # version-family id (normalized stem of family head)
    family_rank: int = 0  # 0 = best within its family


class DocClass(str, enum.Enum):
    """Peek-verifier document classification (D3)."""

    CLIENT_VALUATION_DOC = "CLIENT_VALUATION_DOC"
    HL_WORK_PRODUCT = "HL_WORK_PRODUCT"
    OTHER = "OTHER"


class VerifyResult(BaseModel):
    """Peek-verification result (Phase 2, D3). The verifier reads only the
    first few pages; everything here is keyword/regex-derived, no LLM."""

    status: VerifyStatus = VerifyStatus.UNVERIFIED
    reason: str = ""
    doc_class: DocClass = DocClass.OTHER
    asof_date: date | None = None  # as-of date stated INSIDE the document
    asset_names: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    evidence_snippets: list[str] = Field(default_factory=list)


class LocateResult(BaseModel):
    status: ResolutionStatus
    query: LocateQuery
    candidates: list[CandidateFile] = Field(default_factory=list)  # ranked, top N
    winner: CandidateFile | None = None  # set only for FOUND
    evidence: str = ""  # human-readable reason for the status
    from_override: bool = False  # winner is an explicit analyst pick (forces past peek-verify)


# ---------------------------------------------------------------------------
# Phase 2: document readers (D1)
# ---------------------------------------------------------------------------


class PageClass(str, enum.Enum):
    """Per-page classification driving OCR routing (and Phase-3 vision routing)."""

    TEXT = "TEXT"  # normal text layer
    SCANNED = "SCANNED"  # no/negligible text layer
    IMAGE_TABLE = "IMAGE_TABLE"  # text layer present but large tabular image region
    MIXED = "MIXED"  # text layer plus significant non-tabular image area


class DocFlag(str, enum.Enum):
    """Reader-level hard conditions surfaced as review flags."""

    ACCESS_ERROR = "ACCESS_ERROR"  # encrypted / permission failure
    CORRUPT_FILE = "CORRUPT_FILE"  # zero-byte or unparseable
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"  # legacy .doc etc; manual conversion
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"  # scanned pages present but no OCR engine


class TableData(BaseModel):
    """One extracted table as a row-major cell grid (None = empty cell)."""

    page_number: int  # 1-based page/slide/sheet ordinal
    rows: list[list[str | None]]
    bbox: tuple[float, float, float, float] | None = None
    source: str = ""  # pymupdf | pdfplumber | docx | pptx | xlsx:<sheet>


class PageContent(BaseModel):
    """One page/slide/sheet of a document with its metrics and classification."""

    page_number: int  # 1-based
    text: str = ""
    tables: list[TableData] = Field(default_factory=list)
    text_char_count: int = 0
    image_area_ratio: float = 0.0  # image block area / page area
    has_text_layer: bool = True
    rotation: int = 0  # degrees, pymupdf rotation-aware extraction already applied
    page_class: PageClass = PageClass.TEXT
    ocr_engine: str | None = None  # set when text came from OCR
    ocr_mean_confidence: float | None = None  # mean word confidence 0-1
    unit_label: str = "page"  # page | slide | sheet | section
    unit_name: str | None = None  # e.g. worksheet name


class DocumentContent(BaseModel):
    """Reader output: per-page content plus document-level flags. Readers
    iterate pages lazily and keep only extracted text/tables, never the
    underlying page objects, so >200-page documents stay cheap."""

    file_path: str
    reader: str  # pdf | docx | pptx | xlsx
    page_count: int = 0
    pages: list[PageContent] = Field(default_factory=list)
    flags: list[DocFlag] = Field(default_factory=list)
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Phase 2: deterministic extraction (D4)
# ---------------------------------------------------------------------------


class ConflictingCandidate(BaseModel):
    """A losing candidate value for a field, retained for the audit record."""

    raw_text: str
    value: bool | int | float | str | None = None
    page: int | None = None
    confidence: float = 0.0
    evidence: str = ""


class FieldHit(BaseModel):
    """One extracted/computed cell. `value` is normalized to the schema's
    unit (dates as ISO strings); `raw_text` preserves the verbatim source."""

    field: str  # row-2 header, verbatim
    col_index: int
    band: str
    raw_text: str = ""
    value: bool | int | float | str | None = None
    unit: str | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    method: str = "deterministic"  # deterministic | computed | metadata | claude-code:<model>:<effort>
    confidence: float = 0.0  # 0-1, multiplicative components
    evidence: str = ""  # verbatim snippet <= ~200 chars
    confidence_components: dict[str, float] = Field(default_factory=dict)
    conflicts: list[ConflictingCandidate] = Field(default_factory=list)
    # When an investment spans several source documents (Feature: multi-doc
    # merge), the merged row records WHICH document each cell's value came from
    # (the highest-confidence one). None on single-document rows.
    source_file: str | None = None


# ---------------------------------------------------------------------------
# Phase 2: validation & QA (D5)
# ---------------------------------------------------------------------------


class FlagSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    hard_fail = "hard_fail"  # any one of these => qa_fail


class QaStatus(str, enum.Enum):
    qa_pass = "qa_pass"
    qa_pass_with_flags = "qa_pass_with_flags"
    qa_fail = "qa_fail"


class ReviewFlag(BaseModel):
    """One QA finding; the writer maps these onto the 13-column Review
    Flags sheet (run/memo identity columns come from the run context)."""

    category: str  # reader | parse | vocab | range | cross_field | qoq_threshold | locator | verify
    description: str
    severity: FlagSeverity = FlagSeverity.warning
    reviewer_attention: bool = False
    field: str | None = None  # related schema header, when field-specific


# ---------------------------------------------------------------------------
# Phase 2: orchestration + Phase-3 escalation seam (D7)
# ---------------------------------------------------------------------------


class EscalationField(BaseModel):
    """One field Phase 3 should retry with the LLM fallback."""

    field: str
    col_index: int
    band: str
    reason: str  # below_confidence | required_empty | qa_fail_rescue | force_llm_assist
    confidence: float | None = None
    candidate_pages: list[int] = Field(default_factory=list)


class LlmUsage(BaseModel):
    """Token usage for one Claude Code call — actual when the CLI reported
    it, otherwise estimated from the prompt/payload and labeled as such."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    source: str = "estimated"  # actual | estimated


class LlmAttempt(BaseModel):
    """One Claude Code CLI call for one memo at one router tier. Serialized
    into the audit record and the per-run cost ledger; never contains memo
    text, client names, or page payload."""

    job_id: str  # pv-<run_id>-<memo_id>-t<tier>
    tier: int
    model_alias: str
    model_id: str
    effort: str
    session_id: str | None = None  # Claude Code session id when reported
    from_cache: bool = False  # served from the LLM response cache (rule 10)
    exit_code: int | None = None
    duration_seconds: float = 0.0
    usage: LlmUsage = Field(default_factory=LlmUsage)
    cost_usd: float = 0.0
    cost_source: str = "estimated"  # actual | estimated
    fields_requested: int = 0
    fields_returned: int = 0
    fields_merged: int = 0
    fields_rejected: int = 0
    fields_not_found: int = 0
    error: str | None = None


class EscalationPlan(BaseModel):
    """Per-memo seam for the Phase-3 Claude Code CLI fallback. Phase 2
    serializes the field list into the audit record; Phase 3 executes it
    through hidden local Claude Code sessions and records every attempt,
    merge and rejection here (the audit trail for rule 'nothing silent')."""

    memo_id: str
    confidence_threshold: float
    fields: list[EscalationField] = Field(default_factory=list)
    page_band_map: dict[str, list[int]] = Field(default_factory=dict)  # band -> 1-based pages
    # llm_fallback_disabled | not_needed | llm_completed | llm_partial |
    # llm_failed | llm_deferred_budget
    status: str = "llm_fallback_disabled"
    attempts: list[LlmAttempt] = Field(default_factory=list)
    merged_fields: list[str] = Field(default_factory=list)
    not_extractable: list[str] = Field(default_factory=list)
    merge_log: list[str] = Field(default_factory=list)  # one line per merge/overwrite/rejection


class AssetExtraction(BaseModel):
    """One output row: a single asset of a memo (joint memos yield several)."""

    asset_name: str | None = None
    row_memo_id: str  # memo_id, suffixed "-A2"... for assets beyond the first
    hits: list[FieldHit] = Field(default_factory=list)
    flags: list[ReviewFlag] = Field(default_factory=list)
    qa_status: QaStatus = QaStatus.qa_pass


class MemoResult(BaseModel):
    """Everything one memo produced; serialized as the audit sidecar."""

    memo_id: str
    run_id: str
    client: str
    deal: str
    file_path: str
    file_name: str
    file_sha256: str = ""
    as_of_date: date | None = None
    reporting_period: str = ""
    locate_status: ResolutionStatus | None = None
    locate_evidence: str = ""
    locator_breakdown: ScoreBreakdown | None = None
    verify: VerifyResult | None = None
    reader: str = ""
    page_count: int = 0
    page_classes: dict[int, PageClass] = Field(default_factory=dict)
    page_band_map: dict[str, list[int]] = Field(default_factory=dict)
    assets: list[AssetExtraction] = Field(default_factory=list)
    memo_flags: list[ReviewFlag] = Field(default_factory=list)  # not tied to one asset
    escalation: EscalationPlan | None = None
    from_cache: bool = False
    error: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
