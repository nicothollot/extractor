"""API request/response contracts (pydantic v2). The frontend renders these
verbatim; it never computes business values from raw inputs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from pv_extractor.models import DocType, DocTypeSpec


class LlmRunOptions(BaseModel):
    enabled: bool = True
    mode: str | None = None  # auto | manual; None = config default
    model: str | None = None  # alias/id; implies manual
    effort: str | None = None
    budget_usd: float | None = None
    force_llm: bool = False  # bypass the Claude Code response cache
    # force_llm_assist: use the LLM as the primary extractor — escalate every
    # empty extractable field, not just low-confidence / required ones (also
    # bypasses the deterministic result cache).
    force_llm_assist: bool = False
    allow_fable: bool | None = None


class SlotRef(BaseModel):
    """One (client, deal) selection slot — the unit the New Run 'Confirm
    documents' step curates and the run resolves a single document for."""

    client: str
    deal: str


class MultiSearchFirm(BaseModel):
    """One firm's slice of a multi-search request (api boundary).

    Mirrors models.MultiSearchFirmSpec but lives here so the request can carry
    the typed LlmRunOptions without the circular import documented in
    models.MultiSearchRequest. `deals` empty = all discovered for the client;
    `doc_types` empty = config.multi_search.default_doc_types. Each doc_type is
    a builtin DocType enum value OR a learned DocTypeSpec profile slug."""

    client: str
    deals: list[str] = Field(default_factory=list)  # explicit; empty = all discovered
    period: str
    doc_types: list[str] = Field(default_factory=list)  # builtin enums and/or profile slugs
    llm_assist: bool = False  # run the deal-discovery Claude Code assist first
    enhanced_period_check: bool = False  # surface misfiled docs (stricter in-file period check)
    deal_search_model: str | None = None  # alias/id for the discovery assist; None = config default
    added_folders: list[str] = Field(default_factory=list)  # analyst add_folder corrections (persisted + learned)
    removed_deals: list[str] = Field(default_factory=list)  # analyst remove_folder corrections (persisted + learned)


class MultiSearchSelectionRequest(BaseModel):
    """Build the firm-grouped selection preview (synchronous; no run launched).
    Discovery honors per-firm llm_assist / added_folders / removed_deals; each
    slot is resolved through the SAME locate()+peek-verifier the run uses."""

    firms: list[MultiSearchFirm] = Field(default_factory=list)


class MultiSearchRunRequest(MultiSearchSelectionRequest):
    """Launch a firm-level batch run: one pipeline job over the expanded slot
    set (one workbook for the whole batch), events laned by firm. The run-level
    LLM options are the same typed contract a single-firm run uses."""

    template: str | None = None  # workbook to copy (None = reference template)
    dry_run: bool = False
    force: bool = False  # bypass the deterministic result cache
    llm: LlmRunOptions = Field(default_factory=LlmRunOptions)


class RunRequest(BaseModel):
    scope: str  # client | deal | all
    period: str
    client: str | None = None
    deal: str | None = None
    doc_type: DocType = DocType.any_client_valuation_doc
    # Multiple doc types and/or periods fan the run out into one slot per
    # (pair × doc type × period). Empty/single -> the legacy single path.
    doc_types: list[str] = Field(default_factory=list)  # profile slugs or enum values
    periods: list[str] = Field(default_factory=list)  # explicit period list (incl. expanded ranges)
    # When False, drop the client-source restriction: HL work product is not
    # rejected and report/analysis folders are not penalized (rank-only).
    restrict_to_client_sourced: bool = True
    template: str | None = None  # workbook to copy (None = reference template)
    dry_run: bool = False
    force: bool = False  # bypass the deterministic result cache
    # Slots the analyst removed in the "Confirm documents" step; dropped from
    # the run scope. Empty = every in-scope pair (exact CLI behavior).
    exclude: list[SlotRef] = Field(default_factory=list)
    llm: LlmRunOptions = Field(default_factory=LlmRunOptions)


class JobInfo(BaseModel):
    id: str
    kind: str  # run | scan | claude_update | install_deps
    status: str  # queued | running | cancelling | completed | failed | cancelled | interrupted
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None
    params: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    last_seq: int = 0


class JobEvent(BaseModel):
    seq: int
    ts: str
    type: str
    payload: dict = Field(default_factory=dict)


class LocateRequest(BaseModel):
    client: str
    deal: str
    period: str
    doc_type: DocType = DocType.any_client_valuation_doc


class OverrideRequest(LocateRequest):
    file_path: str
    note: str | None = None


class VerifyFileRequest(LocateRequest):
    """Peek-verify an analyst-chosen file before recording it as a learned
    override for a (client, deal, period, doc_type) slot — the 'Add a missed
    file' / swap-to-arbitrary preview in the Confirm-documents step."""

    file_path: str


class OpenFolderRequest(BaseModel):
    path: str  # file or folder; the containing folder is opened


class ReviewActionRequest(BaseModel):
    action: str  # accept | edit | unresolvable | add_value
    note: str | None = None
    value: bool | int | float | str | None = None  # edit / add_value
    field: str | None = None  # add_value: target schema header (when the item has none)
    page: int | None = None  # add_value: evidence page (1-based)
    bbox: list[float] | None = None  # add_value: evidence region [x0,y0,x1,y1] in PDF points
    evidence: str | None = None  # add_value: selected/quoted source text


class BulkAcceptRequest(BaseModel):
    category: str | None = None  # None = accept ALL pending items
    note: str | None = None


class PricingUpdate(BaseModel):
    input: float
    output: float
    cache_hit: float
    cache_write_5m: float
    cache_write_1h: float
    last_reviewed: str | None = None  # updates the menu-level stamp when given


class ConfigUpdate(BaseModel):
    """Partial update of the editable config surface; unknown keys are
    rejected against the whitelist in routes_config."""

    values: dict = Field(default_factory=dict)  # dotted path -> value


class RawConfigUpdate(BaseModel):
    """Full-text replacement of config.yaml from the GUI's advanced editor.
    Validated through the typed loader before anything lands on disk."""

    text: str


class ScanRequest(BaseModel):
    root: str | None = None  # subtree to scan; None = pv_root
    clients: list[str] | None = None  # top-level folder names to scan selectively
    quick: bool = False  # opt-in mtime-prune: skip re-listing unchanged leaf folders
    # Deal-discovery mode for the end-of-scan refresh. False = Smart Scan
    # (deterministic heuristics only). True = LLM-Assisted Scan (the local
    # `claude -p` deal-discovery pass corroborates/fills the heuristics — no
    # SDK, no API key). model/effort are aliases from config/models.yaml;
    # None defers to config.deal_discovery.llm.
    use_llm: bool = False
    llm_model: str | None = None
    llm_effort: str | None = None


class DealRefreshRequest(BaseModel):
    """Re-run smart deal discovery for one client, optionally with the
    Claude Code assist pass (local CLI subprocess; no SDK, no API key)."""

    client: str
    llm: bool = False
    llm_model: str | None = None  # alias/id from config/models.yaml; None = config default
    llm_effort: str | None = None
    # Replay recorded analyst corrections + client-scoped layout priors during
    # discovery (the default). Set False for a raw re-discovery that ignores
    # the learning layer.
    apply_learning: bool = True


class IntentResolveRequest(BaseModel):
    """Resolve a free-text Smart Search query into a DocTypeSpec. ``use_cli``
    overrides config.smart_search.use_cli_fallback for this one call; None =
    use the config default. Never raises if the CLI is unavailable — the rule
    engine always yields a spec."""

    query: str
    use_cli: bool | None = None


class ProfileSaveRequest(BaseModel):
    """Save/update a learned Smart Search DocTypeSpec profile. Builtins are
    forkable but not overwritable: a save whose slug collides with a builtin is
    refused (HTTP 409)."""

    spec: DocTypeSpec
    query_seed: str | None = None


class SearchPreviewRequest(BaseModel):
    """Live 'is it finding the right docs?' ranking preview. ``spec_or_slug`` is
    EITHER a profile/doc-type slug (str) OR an inline DocTypeSpec (dict). The
    optional client/deal scope the candidate pool; period resolves to an as-of
    date under the client's period style."""

    spec_or_slug: DocTypeSpec | str
    client: str | None = None
    deal: str | None = None
    period: str | None = None


class SearchFeedbackRequest(BaseModel):
    """Record one accept (+1) / reject (-1) signal for a profile's ranking."""

    profile_slug: str
    file_path: str
    label: int  # +1 accept / -1 reject (validated downstream)
    context: dict | None = None


class DealFeedbackRequest(BaseModel):
    """Record one analyst correction to deal discovery for a client, then
    re-discover so the result reflects the new feedback. ``action`` is one of
    add_folder / remove_folder / merge / split / rename (validated downstream
    by deal_learning.record_correction)."""

    client: str
    deal: str
    action: str
    folder_path: str | None = None
    payload: dict | None = None
