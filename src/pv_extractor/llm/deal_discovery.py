"""Claude Code CLI assist for deal-folder discovery (opt-in, Phase-3 rules).

When the heuristic pass in indexer/deals.py finds nothing convincing for a
client, ONE hidden `claude -p --output-format json --json-schema` call reads a
folder INVENTORY (relative folder paths, file counts, a few example file
names — never document contents) and names the deal folders. Same hard rules
as the extraction fallback: local CLI subprocess only, ANTHROPIC_* stripped,
no SDK, no API key. Answers are grounded against the inventory — a returned
folder path that does not exist in the index is discarded, never invented
into the deal table.

The default model is an ALIAS (config.deal_discovery.llm.model, seeded
'sonnet') resolved through config/models.yaml: latest_alias entries are passed
to Claude Code as the alias itself, so the cheap default keeps floating to
the current cheap tier as the CLI updates — no config edit needed when model
names roll over.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

from pv_extractor.config import Config
from pv_extractor.indexer import db
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.llm.claude_code_client import ClaudeCodeClient
from pv_extractor.llm.model_registry import ModelRegistry
from pv_extractor.logging_setup import log_event
from pv_extractor.models import DealEvidence, DealFolder
from pv_extractor.normalize import normalize_text, relative_segments

logger = logging.getLogger(__name__)

DISCOVERY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["deals", "client_has_no_deals"],
    "properties": {
        "client_has_no_deals": {
            "type": "boolean",
            "description": "True when the folder tree contains no per-deal folders at all",
        },
        "deals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "folder_path", "confidence"],
                "properties": {
                    "name": {"type": "string", "description": "Deal/portfolio-company name"},
                    "folder_path": {
                        "type": "string",
                        "description": "EXACT relative folder path copied verbatim from the inventory",
                    },
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

_PROMPT_HEADER = """\
You are identifying DEAL FOLDERS inside one client's folder tree on a private
valuation network share. A deal folder is the folder dedicated to a single
deal / portfolio company / investment whose documents (IC memos, valuation
memos, portfolio reviews, lender presentations) are filed beneath it,
usually organized by reporting period (date folders like "2025 Q1",
"(5) 1.31.25", "12.31.2024", "FY2025").

Layouts vary by client:
- deal folders may sit directly under the client folder;
- or under strategy-group folders ("Direct Lending Investments",
  "Special Situations", "Fund Opinion") or project codenames;
- or BELOW the period folders ("Investments/12.31.22/<deal>") — in that case
  the same deal name recurs under several period folders: report it once per
  period path it appears under;
- some client folders contain no deals at all (admin, correspondence,
  marketing, working-paper archives).

NOT deals: period/date folders, structural folders (Client, Analysis,
Reports, Legal, Diligence, Info from Client, Archive, _Admin),
correspondence folders (From X / To X), and HL's own work-product folders.

Below is the complete folder inventory: one line per folder —
relative path | files in subtree | doc-keyword files | example file names.
Reply ONLY with the JSON object the schema demands. Copy folder_path values
VERBATIM from the inventory lines (they are matched exactly; an invented or
edited path is discarded). List every distinct deal you can identify.

FOLDER INVENTORY:
"""


def build_folder_inventory(
    conn: sqlite3.Connection, config: Config, client: str
) -> list[dict]:
    """One entry per folder under the client INCLUDING intermediate folders
    that hold no files directly (a deal folder usually only contains period/
    structural subfolders) — subtree file counts, and up to N example file
    names (names only — no contents)."""
    cfg = config.deal_discovery.llm
    folders: dict[str, dict] = {}
    sample_counts: dict[str, int] = defaultdict(int)
    for rec in db.files_for_client(conn, client):
        rel = relative_segments(rec.folder_path, config.pv_root)
        if not rel or rel[0] != client:
            continue
        for depth in range(1, len(rel) + 1):  # every ancestor, client root included
            key = "\\".join(rel[:depth])
            entry = folders.setdefault(
                key, {"path": key, "files": 0, "memo_files": 0, "samples": []}
            )
            entry["files"] += 1
            if rec.contains_memo_keyword:
                entry["memo_files"] += 1
            if sample_counts[key] < cfg.max_sample_files:
                entry["samples"].append(rec.file_name)
                sample_counts[key] += 1
    inventory = sorted(folders.values(), key=lambda e: (e["path"].count("\\"), e["path"].lower()))
    if len(inventory) > cfg.max_folders:
        log_event(
            logger, "deal discovery inventory truncated", client=client,
            folders=len(inventory), cap=cfg.max_folders,
        )
        inventory = inventory[: cfg.max_folders]
    return inventory


def build_prompt(inventory: list[dict]) -> str:
    lines = [
        f"{e['path']} | files={e['files']} | doc_keyword_files={e['memo_files']} | "
        + "; ".join(e["samples"])
        for e in inventory
    ]
    return _PROMPT_HEADER + "\n".join(lines) + "\n"


def llm_discover_deals(
    conn: sqlite3.Connection,
    config: Config,
    client: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    cc_client: ClaudeCodeClient | None = None,
    registry: ModelRegistry | None = None,
) -> tuple[list[DealFolder], str | None]:
    """One Claude Code call naming the deal folders for `client`. Returns
    (grounded deals, error). Errors never raise — discovery falls back to the
    heuristic result and the caller logs the reason."""
    cfg = config.deal_discovery.llm
    try:
        registry = registry or ModelRegistry.load(config.llm.models_path)
        entry = registry.resolve(model or cfg.model)
    except Exception as exc:  # unreadable menu / unknown alias
        return [], f"model resolution failed: {exc}"
    effort = effort or cfg.effort or entry.default_effort
    cc_client = cc_client or ClaudeCodeClient(config)

    inventory = build_folder_inventory(conn, config, client)
    if not inventory:
        return [], "no indexed folders for client"
    prompt = build_prompt(inventory)
    by_path = {e["path"].lower(): e["path"] for e in inventory}

    # ignore_cleanup_errors: on Windows the bridged `claude` (WSL) can still hold
    # the schema file when the block exits, making rmtree raise WinError 32 — the
    # OS reclaims the temp dir later, so a cleanup failure must never abort us.
    with tempfile.TemporaryDirectory(prefix="pv_deal_discovery_", ignore_cleanup_errors=True) as tmp:
        schema_path = Path(tmp) / "schema.json"
        with guarded_open_write(schema_path, config.pv_root, mode="wb") as fh:
            fh.write(json.dumps(DISCOVERY_SCHEMA).encode("utf-8"))
        result = cc_client.extract_json(
            job_id=f"deal-discovery-{normalize_text(client).replace(' ', '-')}",
            prompt=prompt,
            schema_path=schema_path,
            model=entry.cli_model_arg(),
            effort=effort,
            cwd=Path(tmp),
            allow_read_tool=False,
            timeout=cfg.timeout_seconds,
        )
    if not result.ok or result.structured is None:
        return [], result.error or "claude code call failed"

    method = f"claude-code:{entry.alias}:{effort}"
    deals: list[DealFolder] = []
    rejected = 0
    for item in result.structured.get("deals", []):
        raw_path = str(item.get("folder_path", "")).replace("/", "\\").strip("\\")
        grounded = by_path.get(raw_path.lower())
        if grounded is None:
            rejected += 1  # ungrounded path: discard, never invent (rule 6 spirit)
            continue
        confidence = cfg.confidence_map.get(str(item.get("confidence")), 0.45)
        deals.append(
            DealFolder(
                client=client,
                name=str(item.get("name") or grounded.split("\\")[-1]),
                folder_paths=[grounded],
                confidence=confidence,
                method=method,
                evidence=DealEvidence(llm_corroborated=True),
            )
        )
    # Same deal reported under several period paths -> one deal, many paths.
    merged: dict[str, DealFolder] = {}
    for deal in deals:
        key = normalize_text(deal.name)
        if key in merged:
            merged[key].folder_paths = sorted(set(merged[key].folder_paths + deal.folder_paths))
            merged[key].confidence = max(merged[key].confidence, deal.confidence)
        else:
            merged[key] = deal
    log_event(
        logger, "deal discovery llm pass", client=client, model=entry.alias, effort=effort,
        deals=len(merged), rejected_ungrounded=rejected,
        no_deals_claimed=bool(result.structured.get("client_has_no_deals")),
        input_tokens=result.usage.input_tokens if result.usage else None,
        output_tokens=result.usage.output_tokens if result.usage else None,
        total_cost_usd=result.total_cost_usd,
    )
    return list(merged.values()), None
