"""Doctor checks shared by the CLI (`pv-extractor doctor`) and the GUI
Settings screen. One function assembles the full check list; the CLI
renders it as a rich table, the API serializes it as JSON. Collection
never raises — a broken dependency becomes a failing check, not a crash.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from pv_extractor.config import Config
from pv_extractor.io_guard import open_read


class DoctorCheck(BaseModel):
    check: str
    ok: bool
    detail: str


def _cost_accounting_check(config: Config) -> DoctorCheck:
    """Whether recent runs settled costs from ACTUAL CLI-reported usage or
    from the ESTIMATED heuristics — read from the newest cost ledger."""
    from pv_extractor.llm.costs import LEDGER_FILENAME

    output_dir = Path(config.output_dir)
    ledgers = sorted(output_dir.glob(f"RUN_*/llm/{LEDGER_FILENAME}"), reverse=True)
    if not ledgers:
        return DoctorCheck(
            check="cost accounting", ok=True,
            detail="no run ledgers yet — costs will be ACTUAL when the CLI reports usage, ESTIMATED otherwise",
        )
    try:
        with open_read(ledgers[0]) as fh:
            entries = [json.loads(line) for line in fh.read().decode("utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        return DoctorCheck(check="cost accounting", ok=False, detail=f"ledger unreadable: {exc}")
    sources = {e.get("cost_source", "estimated") for e in entries}
    label = "actual" if sources == {"actual"} else ("estimated" if sources == {"estimated"} else "actual+estimated")
    return DoctorCheck(
        check="cost accounting", ok=True,
        detail=f"latest run ({ledgers[0].parent.parent.name}) used {label.upper()} token/cost accounting",
    )


def collect_doctor_checks(config: Config) -> list[DoctorCheck]:
    """Active LLM provider CLI / model menu / schema artifacts / cost
    accounting — the Phase-3+4 health snapshot."""
    from pv_extractor.llm.codex_cli_client import CodexCliClient
    from pv_extractor.llm.claude_code_client import ClaudeCodeClient
    from pv_extractor.llm.model_registry import ModelRegistry
    from pv_extractor.system.claude_code import run_startup_checks

    checks: list[DoctorCheck] = []
    provider = config.llm.provider

    if provider == "claude":
        snapshot = run_startup_checks(config)
        for res in snapshot.results:
            checks.append(DoctorCheck(check=f"claude {res.check}", ok=res.ok, detail=res.detail))
        client = ClaudeCodeClient(config)
        for flag in ("--json-schema", "--output-format", "--effort",
                     "--exclude-dynamic-system-prompt-sections"):
            supported = client.supports(flag)
            checks.append(
                DoctorCheck(
                    check=f"claude supports {flag}", ok=supported,
                    detail="yes" if supported else "not advertised by --help (call proceeds without it)",
                )
            )
    elif provider == "codex":
        client = CodexCliClient(config)
        ok, detail = client.check_available()
        checks.append(DoctorCheck(check="codex CLI", ok=ok, detail=detail))
        caps = client.capabilities()
        checks.append(
            DoctorCheck(
                check="codex structured output",
                ok=caps.output_schema_file and caps.output_last_message_file,
                detail=(
                    "supports --output-schema and --output-last-message"
                    if caps.output_schema_file and caps.output_last_message_file
                    else "codex exec --help does not advertise the required structured-output file flags"
                ),
            )
        )
        checks.append(
            DoctorCheck(
                check="codex JSON telemetry", ok=True,
                detail="supported" if caps.json_telemetry else "not advertised; token usage may be unavailable",
            )
        )
    else:
        checks.append(DoctorCheck(check="llm provider", ok=False, detail=f"unknown provider {provider!r}"))

    try:
        registry = ModelRegistry.load(config.llm.models_path)
        checks.append(
            DoctorCheck(
                check="models.yaml", ok=True,
                detail=f"{len(registry.entries)} models, last reviewed {registry.menu.last_reviewed or 'never'}",
            )
        )
        if provider == "claude":
            for name in (config.llm.auto.extraction_model, config.llm.auto.retry_model,
                         config.llm.manual_model):
                registry.resolve(name, provider=provider)
        elif config.codex_cli.model:
            registry.resolve(config.codex_cli.model, provider=provider)
        checks.append(
            DoctorCheck(check="routing models resolvable", ok=True,
                        detail="auto/manual routing aliases all in the menu")
        )
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        checks.append(DoctorCheck(check="models.yaml", ok=False, detail=str(exc)))

    schema_path = Path(__file__).resolve().parents[3] / "schema" / "master_schema.json"
    checks.append(
        DoctorCheck(check="schema/master_schema.json", ok=schema_path.exists(), detail=str(schema_path))
    )
    checks.append(
        DoctorCheck(
            check="llm enabled", ok=config.llm.enabled,
            detail=(
                f"provider={config.llm.provider} mode={config.llm.mode} "
                f"budget=${config.llm.budget_usd:.2f} workers={config.llm.workers}"
            ),
        )
    )
    checks.append(_cost_accounting_check(config))
    return checks
