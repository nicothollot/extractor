"""Typed configuration loaded from config.yaml.

Relative paths (output_dir, db_path, aliases_path) resolve against the
config file's directory so the tool behaves the same from any CWD.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from pv_extractor.models import PeriodStyle, PeriodStyleKind

_FISCAL_RE = re.compile(r"^fiscal\((\d{1,2})\)$")


def parse_period_style(value: str) -> PeriodStyle:
    """'quarterly_calendar' | 'monthly' | 'fiscal(<month-end 1..12>)'."""
    value = value.strip()
    m = _FISCAL_RE.match(value)
    if m:
        month = int(m.group(1))
        if not 1 <= month <= 12:
            raise ValueError(f"fiscal month-end out of range: {value!r}")
        return PeriodStyle(kind=PeriodStyleKind.fiscal, fiscal_year_end_month=month)
    return PeriodStyle(kind=PeriodStyleKind(value))


class FirstRunConfig(BaseModel):
    install_missing_deps: bool = True


class ClaudeCodeConfig(BaseModel):
    command: str = "claude"
    # Extra argv inserted between the command and claude's own arguments.
    # Lets Windows route calls through WSL's claude without a batch wrapper
    # (no cmd.exe quoting): command: wsl, command_args: ["-e", "claude"].
    command_args: list[str] = []
    auto_update_on_start: bool = False
    default_timeout_seconds: int = 120
    allow_cli_usage: bool = True


class GuiConfig(BaseModel):
    """Phase-4 local web GUI: one uvicorn process on the analyst's machine.
    Loopback-only by design — there is no auth system, so binding any
    non-loopback interface is refused at config load."""

    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True  # `pv-extractor gui` opens the default browser
    evidence_dpi: int = 144  # review-queue page renders (pymupdf)
    frontend_dist: str | None = None  # default: <repo>/src/frontend/dist

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"gui.host must be a loopback address, got {v!r} — the GUI is a "
                "single-analyst localhost app with no auth system"
            )
        return v


class IndexerConfig(BaseModel):
    batch_size: int = 5000
    follow_symlinks: bool = False
    # Opt-in quick rescan: a leaf folder whose mtime predates (last scan start −
    # this margin) is taken as unchanged and its listing skipped. The margin
    # absorbs clock skew between this machine and the file server (AD domains
    # cap Kerberos skew at 5 min); larger only re-checks very recently touched
    # folders, so it is cheap insurance.
    quick_rescan_margin_seconds: int = 300


class ClientConfig(BaseModel):
    period_style: str = "quarterly_calendar"

    @field_validator("period_style")
    @classmethod
    def _valid_style(cls, v: str) -> str:
        parse_period_style(v)  # raises on bad input
        return v

    def style(self) -> PeriodStyle:
        return parse_period_style(self.period_style)


class LocatorWeights(BaseModel):
    client_deal_exact: float = 30.0
    client_deal_normalized: float = 27.0
    client_deal_fuzzy_max: float = 22.0
    period_folder_exact: float = 25.0
    period_folder_mismatch: float = -20.0
    period_in_filename: float = 15.0
    period_mtime_window: float = 3.0
    doctype_keyword: float = 20.0
    negative_keyword: float = -25.0
    source_class_client_bonus: float = 25.0
    source_class_report_penalty: float = -30.0
    archive_score_multiplier: float = 0.4
    do_not_use_penalty: float = -40.0
    version_rank_step: float = 2.0
    zero_byte_penalty: float = -10.0
    extension_prior: dict[str, float] = Field(
        default_factory=lambda: {
            ".pdf": 5.0,
            ".docx": 4.0,
            ".pptx": 3.0,
            ".xlsx": 2.0,
            ".xlsm": 2.0,
            ".doc": 1.0,
        }
    )


class LocatorConfig(BaseModel):
    aliases_path: str = "./aliases.yaml"
    fts_candidate_limit: int = 500
    min_accept_score: float = 45.0
    min_gap: float = 8.0
    floor_score: float = 20.0
    ambiguous_top_n: int = 5
    # When nothing matches the requested DOC TYPE but real documents DO exist for
    # the target period (right period, above floor, not a pure-negative file like
    # an NDA), surface them as AMBIGUOUS for human pick instead of returning a
    # bare NOT_YET_UPLOADED with no candidates. Lets the analyst "Replace" with a
    # real document. False = the strict doc-type-only behavior.
    surface_period_matches_without_doctype: bool = True
    mtime_window_days: int = 75
    family_ratio_threshold: int = 92
    fuzzy_match_threshold: int = 80
    weights: LocatorWeights = Field(default_factory=LocatorWeights)
    doc_type_keywords: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "valuation_memo": ["valuation memo", "val memo", "valuation write up", "valuation summary"],
            "ic_memo": ["ic memo", "investment committee"],
            "portfolio_review": ["portfolio review", "quarterly review"],
            # Prewritten catalog (Title-Cased labels in search/doc_type_spec._BUILTIN_LABELS).
            "quarterly_report": ["quarterly report", "quarterly update", "10 q", "10q"],
            "annual_report": ["annual report", "annual update", "10 k", "10k"],
            "houlihan_valuation": ["houlihan", "houlihan lokey", "hl valuation", "third party valuation", "independent valuation"],
            "investor_presentation": ["investor presentation", "investor deck", "lp presentation", "investor update"],
            "fund_report": ["fund report", "fund update", "fund performance", "quarterly fund"],
            "capital_account_statement": ["capital account", "capital account statement", "statement of capital"],
            "financial_statements": ["financial statements", "income statement", "balance sheet", "audited financials"],
            "board_materials": ["board deck", "board materials", "board presentation", "board meeting"],
        }
    )
    negative_keywords: list[str] = Field(
        default_factory=lambda: [
            "nda", "engagement", "proposal", "invoice", "wire",
            "agenda", "minutes", "draft request list", "kyc",
        ]
    )


class DealDiscoveryLlmConfig(BaseModel):
    """Optional Claude Code CLI assist for deal discovery (same hard rules as
    Phase 3: local `claude -p` subprocess, no SDK, no API key). Off by
    default — the analyst opts in per call (CLI --llm / GUI button) or by
    flipping `enabled`, which auto-triggers it for low-confidence clients."""

    enabled: bool = False  # auto-run after scans when heuristics are weak
    # Alias resolved against config/models.yaml; latest_alias entries float to
    # the newest model of that tier as Claude Code updates, so a cheap default
    # keeps pointing at the current cheap tier without config edits.
    model: str = "sonnet"
    effort: str = "low"
    trigger_confidence: float = 0.45  # auto-assist when the best heuristic deal scores below this
    timeout_seconds: int = 300
    max_folders: int = 500  # folder-inventory cap in the prompt (shallowest first)
    max_sample_files: int = 3  # example file names listed per folder
    # LLM self-reported confidence -> DealFolder.confidence
    confidence_map: dict[str, float] = Field(
        default_factory=lambda: {"high": 0.85, "medium": 0.65, "low": 0.45}
    )


class DealDiscoveryWeights(BaseModel):
    """Additive confidence components, clamped to [0, 1]."""

    period_evidence: float = 0.40  # the folder sits directly above (or below) date folders
    multi_period_bonus: float = 0.15  # >= 2 distinct periods observed
    structural_children: float = 0.15  # Client/Analysis/... structure beneath it
    memo_keyword_files: float = 0.15  # subtree contains doc-type-keyword files
    any_files: float = 0.10  # subtree contains files at all
    flat_default_bonus: float = 0.15  # directly under the client with no contrary signal (legacy layout prior)
    grouping_name_penalty: float = -0.25  # name looks like a strategy-group folder, not a company
    container_depth_penalty: float = -0.05  # per grouping folder between client and deal
    llm_corroboration_bonus: float = 0.10  # heuristic deal independently confirmed by the LLM pass
    admin_container: float = -0.10  # deal surfaced by recursing into an admin container (mild penalty: unusual placement)
    shared_bucket: float = 0.30  # per-cluster synthetic deal carved out of a shared mixed-investment folder


class DealDiscoveryLearningConfig(BaseModel):
    """Per-client learned overrides + layout priors (Search & Selection
    Revamp, Phase A). The live priors are auto-maintained in the index DB at
    runtime; this config only carries the on/off switch and the nudge cap."""

    enabled: bool = True  # per-client learned overrides + priors (Phase A)
    prior_bump: float = 0.25  # max confidence nudge a learned prior contributes


class DealDiscoveryConfig(BaseModel):
    """Smart deal-folder discovery (indexer/deals.py). Deal folders are NOT
    assumed to sit directly under the client folder: discovery classifies
    every segment (period / structural / admin / neutral) and finds the
    folders adjacent to the period folders. All lexicons are normalized-token
    lists (lowercase, non-alphanumerics stripped)."""

    enabled: bool = True  # off = legacy behavior (deal = first segment under the client)
    # A folder is STRUCTURAL when every token is structural/glue/numeric:
    # 'Client', 'Info from Client', 'Analysis' — but not 'Legal & General'.
    structural_tokens: list[str] = Field(
        default_factory=lambda: [
            "client", "analysis", "report", "reports", "reporting", "legal", "diligence",
            "correspondence", "resources", "resource", "model", "models", "info",
            "information", "data", "dataroom", "deliverable", "deliverables", "docs",
            "document", "documents", "executed", "draft", "drafts", "tax", "kyc", "nda",
            "invoice", "invoices", "billing", "email", "emails", "misc", "other", "support",
            "backup", "wire", "archive", "archived", "old", "prior", "superseded",
            "received", "sent", "final", "workpapers", "working", "papers",
        ]
    )
    # Glue words that never decide anything on their own.
    glue_tokens: list[str] = Field(
        default_factory=lambda: ["from", "to", "for", "of", "the", "and", "a", "an", "with", "re"]
    )
    # 'From Ares' / 'To Ares': short correspondence folders led by these tokens.
    correspondence_prefixes: list[str] = Field(default_factory=lambda: ["from", "to"])
    admin_tokens: list[str] = Field(
        default_factory=lambda: ["admin", "administration", "internal", "template", "templates"]
    )
    # Tokens that smell like a strategy-group/container, not a company name.
    grouping_tokens: list[str] = Field(
        default_factory=lambda: [
            "investments", "lending", "opinion", "opinions", "situations", "opportunities",
            "engagement", "engagements", "monthly", "quarterly", "marketing", "initiatives",
            "research", "download", "downloads", "funds", "strategies",
        ]
    )
    # A NEUTRAL leaf folder whose (date-stripped) name consists ENTIRELY of these
    # generic/reporting words is never a deal — it is a bucket of documents, not
    # an investment ('Research (2020.10.31)', 'Q4 2025 Reports', 'Prior Period').
    # Combined with structural/glue/grouping/admin tokens for the generic test.
    exclude_generic_deal_names: bool = True  # drop generic-named leaf folders from the deal list
    deal_name_stopwords: list[str] = Field(
        default_factory=lambda: [
            "prior", "current", "latest", "period", "periods", "ytd", "ltm", "ntm",
            "monitor", "monitoring", "summary", "update", "updates", "overview",
            "snapshot", "package", "materials", "general", "misc", "reference",
        ]
    )
    min_confidence: float = 0.0  # deals below this are dropped entirely (0 = keep all, rank by confidence)
    review_confidence: float = 0.45  # below this a deal is flagged low-confidence in CLI/GUI
    display_min_confidence: float = 0.70  # GUI only: hide discovered deals below this (storage keeps all)
    # Shared mixed-investment bucket (one neutral folder directly holding memo
    # files for several DIFFERENT investments, no per-deal subfolder). Off-able.
    shared_bucket_enabled: bool = True  # master gate for the shared-bucket branch
    shared_bucket_min_clusters: int = 2  # min distinct asset clusters to treat a folder as a shared bucket
    shared_bucket_name_match_threshold: int = 85  # rapidfuzz floor to assign a file to a cluster-deal
    cluster_ratio_threshold: int = 80  # rapidfuzz floor that groups two file stems into the same asset cluster
    weights: DealDiscoveryWeights = Field(default_factory=DealDiscoveryWeights)
    llm: DealDiscoveryLlmConfig = Field(default_factory=DealDiscoveryLlmConfig)
    learning: DealDiscoveryLearningConfig = Field(default_factory=DealDiscoveryLearningConfig)
    # The LIVE per-client priors are auto-maintained in the index DB at runtime
    # (Phase A3), NOT in config.yaml. This field is only an optional manual
    # override / documented default ({}); do not hand-edit it expecting it to be
    # authoritative. Shape: {client_name: {prior_key: weight}}.
    layout_priors: dict[str, dict[str, float]] = Field(default_factory=dict)


class SmartSearchConfig(BaseModel):
    """Smart Search (Search & Selection Revamp, Phase B). Free-text document
    search over the index with a BM25 base relevance, intent-rule boosts and an
    optional Claude Code CLI fallback for ambiguous queries. Must work WITHOUT
    the LLM (heuristics primary); the CLI only assists. All tunable."""

    enabled: bool = True
    use_cli_fallback: bool = True  # optional Claude Code assist for ambiguous queries
    cli_model: str = "sonnet"  # ALIAS from config/models.yaml — floats to current cheap tier
    cli_effort: str = "low"
    bm25_k1: float = 1.2  # BM25 term-frequency saturation
    bm25_b: float = 0.75  # BM25 length normalization
    min_score: float = 8.0  # results below this are dropped
    top_n: int = 25  # max results returned
    learning_weight: float = 0.3  # weight of learned per-query priors on ranking
    # Per-component additive rank weights (rank.py). Mirrors the locator's
    # transparent additive model; nothing magic inline.
    rank_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "filename_match": 30.0,  # scaled by the BM25/fuzzy lexical relevance (0..1)
            "folder_context": 8.0,  # a folder_include anchor hit on the folder path
            "extension_prior": 4.0,  # the file's extension is in spec.extensions
            "period_evidence": 10.0,  # filename/date-folder carries the target period
            "period_missing_penalty": -6.0,  # period_required but no period evidence
            "negative_penalty": -25.0,  # a filename_exclude/folder_exclude term hit
        }
    )
    # Intent lexicon: phrase -> DocTypeSpec fragment (filename_include /
    # filename_regex / folder_include / extensions). Seeds common financial-doc
    # phrases; override or extend here.
    intent_rules: dict[str, dict] = Field(
        default_factory=lambda: {
            "quarterly report": {
                "filename_include": ["quarterly", "10 q", "q1", "q2", "q3", "q4"],
                "filename_regex": ["10[- ]?q"],
                "folder_include": ["filings", "quarterly"],
                "extensions": [".pdf", ".htm", ".html"],
            },
            "annual report": {
                "filename_include": ["annual", "10 k"],
                "filename_regex": ["10[- ]?k"],
                "folder_include": ["filings", "annual"],
            },
            "cap table": {
                "filename_include": ["cap table", "capitalization table", "captable"],
            },
            "audited financials": {
                "filename_include": [
                    "audited", "audited financials", "audited financial statements", "audit report"
                ],
            },
            "valuation memo": {
                "filename_include": [
                    "valuation memo", "val memo", "valuation write up", "valuation summary"
                ],
            },
            "ic memo": {
                "filename_include": ["ic memo", "investment committee"],
            },
            "portfolio review": {
                "filename_include": ["portfolio review", "quarterly review"],
            },
        }
    )


class MultiSearchConfig(BaseModel):
    """Multi-Search (Search & Selection Revamp, Phase C): run one document
    search across many firms/clients at once."""

    max_firms: int = 25  # cap on firms/clients fanned out in a single multi-search
    enhanced_period_check_default: bool = False  # default for the stricter in-file period check
    default_doc_types: list[str] = Field(default_factory=lambda: ["any_client_valuation_doc"])


class OcrConfig(BaseModel):
    enabled: bool = True
    engine: str = "rapidocr"  # rapidocr | tesseract (optional extra)
    dpi: int = 300
    tesseract_cmd: str | None = None  # explicit binary path when engine=tesseract


class PageClassificationConfig(BaseModel):
    """Thresholds for TEXT / SCANNED / IMAGE_TABLE / MIXED (D1)."""

    min_text_chars: int = 64  # below this the text layer is 'negligible'
    image_area_threshold: float = 0.35  # image area ratio above => image-driven page
    image_table_min_area_ratio: float = 0.10  # single image block big enough to be a table
    image_table_min_aspect: float = 0.8  # width/height >= this looks tabular (wide block)
    image_table_min_width_ratio: float = 0.45  # block width / page width


class ConfidenceConfig(BaseModel):
    """Multiplicative confidence components for FieldHit scoring (D4)."""

    label_exact: float = 1.0
    label_fuzzy_floor: float = 0.70  # fuzzy label match scales between floor and exact
    parse_clean: float = 1.0
    parse_lenient: float = 0.85  # value needed repair (stripped footnotes, odd spacing)
    page_class_text: float = 1.0
    page_class_ocr: float = 0.70  # multiplied by the page's mean OCR confidence
    table_factor: float = 1.0
    prose_factor: float = 0.85
    ambiguity_penalty: float = 0.60  # applied when conflicting candidates were found


class ExtractionConfig(BaseModel):
    workers: int = 4  # network share: do not hammer it
    top_k_pages_per_band: int = 4
    summary_pages: int = 3  # pages 1..N always handed to every band extractor
    confidence_threshold: float = 0.75  # Phase-3 escalation seam
    vocab_fuzzy_threshold: int = 90  # below this a vocab field stays empty + flag
    max_evidence_chars: int = 200
    cache_enabled: bool = True
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    page_classification: PageClassificationConfig = Field(default_factory=PageClassificationConfig)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    # Extra anchor terms merged with the schema-seeded per-band lexicons (D2).
    band_anchor_overrides: dict[str, list[str]] = Field(default_factory=dict)


class PeekVerifyConfig(BaseModel):
    """Keyword/regex heuristics for the D3 peek-verifier; all tunable."""

    pages: int = 3
    min_confidence: float = 0.5
    client_doc_keywords: list[str] = Field(
        default_factory=lambda: [
            "valuation memo", "valuation memorandum", "investment committee", "ic memo",
            "portfolio review", "quarterly review", "valuation summary", "fair value",
            "valuation as of", "concluded value", "enterprise value", "net asset value",
        ]
    )
    hl_work_product_markers: list[str] = Field(
        default_factory=lambda: [
            "houlihan lokey", "prepared by hl", "hl financial advisors",
            "this report is confidential and was prepared exclusively",
        ]
    )
    # Client docs ADDRESSED TO HL must not classify as HL work product.
    hl_addressee_exceptions: list[str] = Field(
        default_factory=lambda: ["prepared for houlihan lokey", "delivered to houlihan lokey"]
    )
    asset_name_labels: list[str] = Field(
        default_factory=lambda: [
            "portfolio company", "company", "asset", "investment", "subject company",
            "operating company", "issuer", "borrower", "project",
        ]
    )


class LlmAutoRoutingConfig(BaseModel):
    """AUTO-mode routing table (Phase 3). Models are aliases/ids resolved
    against config/models.yaml; the router never hardcodes a model."""

    classification_model: str = "haiku"  # cheap classification tasks only
    classification_effort: str = "low"
    extraction_model: str = "sonnet"  # normal escalated-field extraction
    extraction_effort: str = "medium"
    ocr_hostile_model: str = "opus"  # memos with SCANNED/IMAGE_TABLE payload pages
    ocr_hostile_effort: str = "high"
    retry_model: str = "opus"  # second pass when fields are still failing
    retry_effort: str = "high"
    retry_effort_bump: str = "xhigh"  # retry effort when the model tier repeats
    fable_effort: str = "high"  # final pass, only when allow_fable is set


class LlmConfig(BaseModel):
    """Phase-3 Claude Code CLI fallback. NEVER the Anthropic SDK / an API
    key: extraction runs through hidden local `claude -p` sessions reusing
    the operator's `claude auth login` credentials."""

    enabled: bool = True  # master switch; --no-llm gives Phase-2 behavior
    mode: str = "auto"  # auto (route by task) | manual (one forced model+effort)
    manual_model: str = "sonnet"
    manual_effort: str = "low"
    allow_fable: bool = False  # explicit opt-in for the most expensive tier
    budget_usd: float = 25.0  # hard per-run cap; beyond it memos are LLM_DEFERRED
    workers: int = 2  # hidden Claude Code session queue concurrency (keep 1-2)
    models_path: str = "./config/models.yaml"
    cache_enabled: bool = True  # response cache; --force-llm bypasses reads
    timeout_seconds: int = 600  # per Claude Code extraction call
    max_pages_per_memo: int = 20  # payload page cap (candidate pages + pages 1-3)
    image_max_long_edge: int = 1080  # SCANNED/IMAGE_TABLE page render cap, PNG
    quote_match_threshold: int = 85  # fuzzy quote-grounding floor on OCR pages
    # Band-batched extraction: instead of one giant call over every escalated
    # field, make focused per-band calls ordered by deterministic page-anchor
    # relevance, sending only that band's pages + a small schema. Sharper model
    # focus, the most-relevant bands run first (so the budget is spent well),
    # and bands the document shows no evidence for are batched into one cheap
    # pass rather than expanded across every tier. False = legacy single call.
    band_batched: bool = True
    band_relevance_floor: float = 0.0  # band anchor score strictly above this = "has evidence"
    max_fields_per_call: int = 50  # split a band's call when it carries more fields than this
    no_evidence_effort: str = "low"  # effort for the single no-evidence sweep call
    exclude_dynamic_system_prompt_sections: bool = True  # pass flag when CLI supports it
    # LLM self-reported confidence -> FieldHit.confidence
    confidence_scores: dict[str, float] = Field(
        default_factory=lambda: {"high": 0.85, "medium": 0.60, "low": 0.35}
    )
    # ESTIMATED-token heuristics used when Claude Code reports no usage.
    chars_per_token: float = 4.0
    image_tokens_per_megapixel: int = 1500
    output_tokens_per_field: int = 80
    output_tokens_base: int = 300
    auto: LlmAutoRoutingConfig = Field(default_factory=LlmAutoRoutingConfig)


class ValidationConfig(BaseModel):
    rules_path: str = "./rules.yaml"
    equity_bridge_tolerance: float = 0.02  # |EV - ND - Equity| / |Equity| tolerance
    weights_sum_tolerance: float = 1.0  # method weights must sum to 100 +/- this
    bridge_tolerance_ratio: float = 0.05  # QoQ bridge deltas vs NAV change
    computed_crosscheck_tolerance: float = 0.02  # extracted vs computed disagreement
    percent_range: tuple[float, float] = (-100.0, 200.0)
    date_year_min: int = 2000
    date_max_years_after_asof: int = 1
    wacc_qoq_threshold_bps: float = 50.0
    multiple_qoq_threshold_x: float = 0.5
    nav_qoq_threshold_pct: float = 5.0


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Config(BaseModel):
    pv_root: str = "\\\\hlhz\\dfs\\nyfva\\PV"
    output_dir: Path = Path("./output")
    db_path: Path = Path("./output/pv_index.db")
    first_run: FirstRunConfig = Field(default_factory=FirstRunConfig)
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)
    indexer: IndexerConfig = Field(default_factory=IndexerConfig)
    clients: dict[str, ClientConfig] = Field(default_factory=lambda: {"default": ClientConfig()})
    locator: LocatorConfig = Field(default_factory=LocatorConfig)
    deal_discovery: DealDiscoveryConfig = Field(default_factory=DealDiscoveryConfig)
    smart_search: SmartSearchConfig = Field(default_factory=SmartSearchConfig)
    multi_search: MultiSearchConfig = Field(default_factory=MultiSearchConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    peek_verify: PeekVerifyConfig = Field(default_factory=PeekVerifyConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def client_period_style(self, client: str) -> PeriodStyle:
        cfg = self.clients.get(client) or self.clients.get("default") or ClientConfig()
        return cfg.style()

    def aliases_path_resolved(self, base_dir: Path | None = None) -> Path:
        p = Path(self.locator.aliases_path)
        return p if p.is_absolute() else (base_dir or Path.cwd()) / p


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config.yaml and resolve relative paths against its directory."""
    cfg_path = Path(path).resolve()
    with open(cfg_path, "rb") as fh:  # noqa: io-guard-exempt (read-only)
        data = yaml.safe_load(fh) or {}
    config = Config.model_validate(data)
    base = cfg_path.parent
    if not config.output_dir.is_absolute():
        config.output_dir = (base / config.output_dir).resolve()
    if not config.db_path.is_absolute():
        config.db_path = (base / config.db_path).resolve()
    if not Path(config.locator.aliases_path).is_absolute():
        config.locator.aliases_path = str((base / config.locator.aliases_path).resolve())
    if not Path(config.validation.rules_path).is_absolute():
        config.validation.rules_path = str((base / config.validation.rules_path).resolve())
    if not Path(config.llm.models_path).is_absolute():
        config.llm.models_path = str((base / config.llm.models_path).resolve())
    return config
