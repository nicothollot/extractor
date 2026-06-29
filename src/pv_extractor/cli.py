"""Typer CLI — the only layer that prints.

Commands: locate, run, ingest-xlsx, scan, deals, compile-schema, init-db,
self-check, models, costs, doctor. Everything else logs JSONL to
output_dir/logs/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from pv_extractor.config import Config, load_config
from pv_extractor.io_guard import guarded_open_write
from pv_extractor.logging_setup import setup_logging
from pv_extractor.models import DocType, LocateQuery, ResolutionStatus

app = typer.Typer(name="pv-extractor", no_args_is_help=True, add_completion=False)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yaml")]


def _seed_config_if_missing(config_path: Path) -> None:
    """config.yaml is git-ignored (per-machine), so a fresh/just-pulled checkout
    may not have one. Seed it from the committed config.example.yaml template
    (next to it) rather than crashing — bootstrap does the same on first run."""
    if config_path.exists():
        return
    template = config_path.parent / "config.example.yaml"
    if not template.exists():
        return  # let load_config raise its clear FileNotFoundError
    # The seeded config.yaml is a verbatim copy of the template, so its effective
    # pv_root is the template's. Route the write through io_guard (Hard rule 1):
    # it refuses the production share unconditionally and any target under pv_root.
    data = template.read_bytes()
    seeded_pv_root = str((yaml.safe_load(data) or {}).get("pv_root", ""))
    with guarded_open_write(config_path, seeded_pv_root, mode="wb") as fh:
        fh.write(data)
    console.print(
        f"[yellow]No {config_path.name} found — seeded it from {template.name}.[/yellow] "
        "Edit it for this machine (set output_dir to a local writable folder)."
    )


def _setup(config_path: Path) -> Config:
    _seed_config_if_missing(config_path)
    config = load_config(config_path)
    setup_logging(config.output_dir, config.pv_root, config.logging.level)
    return config


@app.command()
def locate(
    client: Annotated[str, typer.Option(help="Client name (aliases allowed)")],
    deal: Annotated[str, typer.Option(help="Deal or asset name (aliases allowed)")],
    period: Annotated[str, typer.Option(help='Target period: "2025-01-31", "Q1 2026", "FY2025", ...')],
    doc_type: Annotated[DocType, typer.Option("--doc-type")] = DocType.any_client_valuation_doc,
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Locate a client document; print the ranked candidate table with score components."""
    from pv_extractor.indexer.db import open_db
    from pv_extractor.locator.locate import locate as run_locate

    config = _setup(config_path)
    conn = open_db(config.db_path, config.pv_root)
    try:
        query = LocateQuery(client=client, deal=deal, period=period, doc_type=doc_type)
        try:
            result = run_locate(conn, config, query)
        except ValueError as exc:
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
    finally:
        conn.close()

    status_color = {
        ResolutionStatus.FOUND: "green",
        ResolutionStatus.AMBIGUOUS: "yellow",
        ResolutionStatus.NOT_YET_UPLOADED: "cyan",
        ResolutionStatus.NOT_FOUND: "red",
        ResolutionStatus.ACCESS_ERROR: "red",
    }[result.status]
    console.print(f"\n[bold {status_color}]{result.status.value}[/bold {status_color}]  {result.evidence}")
    if result.query.as_of_date:
        console.print(f"target as-of: {result.query.as_of_date.isoformat()}")
    if result.winner:
        console.print(f"[bold]winner:[/bold] {result.winner.record.file_path}")

    if result.candidates:
        table = Table(title="Ranked candidates", show_lines=False)
        for col in ("#", "score", "clt/deal", "period", "doctype", "src", "ext", "ver", "neg", "arch", "path"):
            table.add_column(col)
        for i, cand in enumerate(result.candidates, 1):
            b = cand.breakdown
            table.add_row(
                str(i),
                f"{b.final_score:.1f}",
                f"{b.client_deal_score:.0f} ({b.client_deal_method})",
                f"{b.period_score:.0f} ({b.period_method})",
                f"{b.doctype_score:.0f}",
                f"{b.source_class_score:+.0f} {cand.record.source_class.value}",
                f"{b.extension_score:.0f}",
                f"{b.version_score:.0f}",
                f"{b.negative_score + b.do_not_use_penalty + b.zero_byte_penalty:.0f}",
                f"x{b.archive_multiplier:.1f}",
                cand.record.file_path,
            )
        console.print(table)
    raise typer.Exit(code=0 if result.status == ResolutionStatus.FOUND else 1)


@app.command("run")
def run_cmd(
    scope: Annotated[str, typer.Option(help="client | deal | all")],
    period: Annotated[str, typer.Option(help='Target period: "2026-03-31", "Q1 2026", ...')],
    client: Annotated[Optional[str], typer.Option(help="Client (scope=client|deal)")] = None,
    deal: Annotated[Optional[str], typer.Option(help="Deal/asset (scope=deal)")] = None,
    doc_type: Annotated[DocType, typer.Option("--doc-type")] = DocType.any_client_valuation_doc,
    template: Annotated[
        Optional[Path],
        typer.Option(help="Template or previous output workbook (default: reference/master_index_v4.xlsx)"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Locate+verify only; print coverage")] = False,
    force: Annotated[bool, typer.Option("--force", help="Bypass the extraction result cache")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Disable the local LLM assist pass (pure deterministic run)")] = False,
    llm_budget: Annotated[Optional[float], typer.Option("--llm-budget", help="Hard per-run LLM budget cap in USD (default: config llm.budget_usd)")] = None,
    llm_mode: Annotated[Optional[str], typer.Option("--llm-mode", help="auto | per_deal | single_model (legacy manual accepted)")] = None,
    llm_model: Annotated[Optional[str], typer.Option("--llm-model", help="Model alias/id from config/models.yaml; implies --llm-mode single_model")] = None,
    llm_effort: Annotated[Optional[str], typer.Option("--llm-effort", help="low | medium | high | xhigh | max")] = None,
    force_llm: Annotated[bool, typer.Option("--force-llm", help="Bypass the local LLM response cache")] = False,
    force_llm_assist: Annotated[bool, typer.Option("--force-llm-assist", help="Use the LLM as the primary extractor: escalate every empty extractable field (bypasses the deterministic result cache)")] = False,
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Full pipeline: locate -> verify -> extract -> validate -> [local LLM assist] -> write."""
    from pv_extractor.llm.escalate import resolve_settings
    from pv_extractor.run import run as run_pipeline
    from pv_extractor.write import HeaderDriftError

    config = _setup(config_path)
    llm_settings = resolve_settings(
        config, no_llm=no_llm, mode=llm_mode, model=llm_model,
        effort=llm_effort, budget=llm_budget, force=force_llm,
        force_assist=force_llm_assist,
    )
    try:
        report = run_pipeline(
            config, scope=scope, period=period, client=client, deal=deal,
            doc_type=doc_type, template=template, dry_run=dry_run, force=force,
            llm_settings=llm_settings,
        )
    except HeaderDriftError as exc:
        console.print(f"[red]template header drift:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title=f"Coverage — {report.run_id}")
    for col in ("client", "deal", "status", "detail"):
        table.add_column(col)
    status_color = {
        "FOUND": "green", "AMBIGUOUS": "yellow", "NOT_YET_UPLOADED": "cyan",
        "NOT_FOUND": "red", "ACCESS_ERROR": "red", "ERROR": "red",
    }
    for entry in report.coverage:
        color = status_color.get(entry.status, "white")
        table.add_row(entry.client, entry.deal, f"[{color}]{entry.status}[/{color}]", entry.detail)
    console.print(table)

    if not dry_run:
        qa = report.qa_counts()
        console.print(
            f"memos={len(report.memos)} rows_added={report.rows_added} "
            f"flags={report.flags_added} cache_hits={report.cache_hits} "
            f"qa_pass={qa['qa_pass']} with_flags={qa['qa_pass_with_flags']} qa_fail={qa['qa_fail']}"
        )
        console.print(f"workbook: [bold]{report.workbook_path}[/bold]")
        console.print(f"audit:    {report.run_dir}/audit/")
        llm = report.llm
        if llm is None or not llm.enabled:
            console.print("[dim]LLM fallback disabled — escalation plans in the audit records[/dim]")
        elif not llm.executed:
            console.print(f"[yellow]LLM fallback unavailable:[/yellow] {llm.detail}")
        else:
            label = "actual+estimated" if llm.any_actual_costs else "ESTIMATED"
            console.print(
                f"LLM assist ({config.llm.provider}): {llm.memos_escalated} memo(s), {llm.attempts} call(s) "
                f"({llm.cache_hits} cached), {llm.memos_deferred} deferred, "
                f"[bold]${llm.total_cost_usd:.4f}[/bold] ({label})"
            )
            if llm.ledger_path:
                console.print(f"cost ledger: {llm.ledger_path}  (pv-extractor costs --run {report.run_id})")
    raise typer.Exit(code=0)


@app.command("ingest-xlsx")
def ingest_xlsx_cmd(
    xlsx_path: Annotated[Path, typer.Argument(help="Index export workbook (strip_from_index format)")],
    limit: Annotated[Optional[int], typer.Option(help="Stop after N rows (testing)")] = None,
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Bulk-load an existing PV index export into the SQLite store (re-deriving every column)."""
    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.deals import refresh_deals
    from pv_extractor.indexer.ingest_xlsx import ingest_xlsx

    config = _setup(config_path)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        n = ingest_xlsx(conn, xlsx_path, config, limit=limit)
        refresh_deals(conn, config)
    finally:
        conn.close()
    console.print(f"ingested [bold]{n}[/bold] rows into {config.db_path}")


def _clients_under_root(config: Config, root: str | None) -> list[str] | None:
    """Clients whose deal discovery a scan of `root` can affect: all of them
    for a pv_root scan, the owning client for a subtree scan."""
    from pv_extractor.normalize import relative_segments

    if root is None:
        return None  # full pv_root walk -> refresh every client
    rel = relative_segments(root, config.pv_root)
    return None if not rel else [rel[0]]


@app.command()
def scan(
    root: Annotated[Optional[str], typer.Argument(help="Subtree to scan (default: pv_root)")] = None,
    config_path: ConfigOpt = Path("config.yaml"),
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help="Skip re-listing unchanged leaf folders (mtime predates the last scan). "
            "Big speedup on the share for new-uploads-only changes; misses in-place file "
            "overwrites until a full rescan.",
        ),
    ] = False,
) -> None:
    """Scan a directory tree (incremental refresh: only changed rows are rewritten).

    Ctrl+C pauses gracefully: everything scanned so far stays committed and
    usable; re-running the same command later fast-forwards through indexed
    files and continues where it stopped."""
    import signal
    import threading

    from pv_extractor.indexer.db import init_schema, open_db
    from pv_extractor.indexer.deals import refresh_deals
    from pv_extractor.indexer.scan_tree import scan_tree

    config = _setup(config_path)

    stop = threading.Event()

    def _on_sigint(signum, frame) -> None:  # second Ctrl+C falls back to default
        stop.set()
        console.print("\n[yellow]pausing — committing what was scanned so far…[/yellow]")
        signal.signal(signal.SIGINT, signal.default_int_handler)

    previous_handler = signal.signal(signal.SIGINT, _on_sigint)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        stats = scan_tree(conn, root or config.pv_root, config, should_stop=stop.is_set, quick=quick)
        discovered = refresh_deals(conn, config, _clients_under_root(config, root))
    finally:
        conn.close()
        signal.signal(signal.SIGINT, previous_handler)
    console.print(
        f"seen={stats.files_seen} added={stats.added} updated={stats.updated} "
        f"unchanged={stats.unchanged} removed={stats.removed} errors={stats.errors}"
    )
    if discovered:
        n_deals = sum(len(d) for d in discovered.values())
        console.print(f"deal discovery: {n_deals} deals across {len(discovered)} client(s)")
    if stats.stopped_early:
        console.print(
            "[yellow]scan paused[/yellow] — the index keeps everything scanned so far "
            "(no deletions were applied). Re-run the same scan later to continue; "
            "already-indexed files are skipped."
        )


@app.command()
def deals(
    client: Annotated[Optional[str], typer.Option(help="Client folder name (as indexed); omit to refresh/summarize ALL indexed clients")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-run discovery before printing (no rescan needed — works on the existing index)")] = False,
    llm: Annotated[bool, typer.Option("--llm", help="Include the local LLM assist pass (local CLI, no API key)")] = False,
    llm_model: Annotated[Optional[str], typer.Option(help="Model alias/id for --llm (default: deal_discovery.llm.model)")] = None,
    llm_effort: Annotated[Optional[str], typer.Option(help="Effort for --llm (default: deal_discovery.llm.effort)")] = None,
    show_learned: Annotated[bool, typer.Option("--show-learned", help="Print this client's learned layout priors + recorded corrections, then exit")] = False,
    forget: Annotated[bool, typer.Option("--forget", help="Clear this client's recorded deal-discovery corrections (and cached priors), then exit")] = False,
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Show the discovered deal folders for a client (or a per-client summary), with confidence."""
    from pv_extractor.indexer import db as index_db
    from pv_extractor.indexer import deal_learning
    from pv_extractor.indexer.db import deal_folders_for_client, init_schema, open_db
    from pv_extractor.indexer.deals import refresh_deals

    config = _setup(config_path)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
        if show_learned or forget:
            if client is None:
                console.print("[red]--show-learned / --forget require [cyan]--client[/cyan][/red]")
                raise typer.Exit(2)
            if forget:
                corrections = deal_learning.list_corrections(conn, client)
                for row in corrections:
                    deal_learning.delete_correction(conn, row["id"])
                # Drop the cached priors meta so /learned + --show-learned reflect the wipe.
                index_db.set_meta(conn, f"layout_priors:{client}", "{}")
                console.print(
                    f"forgot [bold]{len(corrections)}[/bold] correction(s) for "
                    f"[bold]{client}[/bold] (re-run [cyan]--refresh[/cyan] to re-discover without them)"
                )
                return
            # --show-learned
            priors = deal_learning.cached_layout_priors(conn, client)
            corrections = deal_learning.list_corrections(conn, client)
            ptable = Table(title=f"Learned layout priors — {client}")
            ptable.add_column("Signal")
            ptable.add_column("Nudge", justify="right")
            for signal, nudge in sorted(priors.items()):
                ptable.add_row(signal, f"{nudge:.4f}")
            if not priors:
                ptable.add_row("[dim](none)[/dim]", "—")
            console.print(ptable)
            ctable = Table(title=f"Recorded corrections — {client}")
            ctable.add_column("ID", justify="right")
            ctable.add_column("Deal")
            ctable.add_column("Action")
            ctable.add_column("Folder")
            ctable.add_column("When")
            for row in corrections:
                ctable.add_row(
                    str(row["id"]), row["deal"], row["action"],
                    row.get("folder_path") or "—", row.get("created_at") or "—",
                )
            if not corrections:
                ctable.add_row("—", "[dim](none)[/dim]", "—", "—", "—")
            console.print(ctable)
            return
        if refresh or llm:
            refresh_deals(
                conn, config, [client] if client else None,
                use_llm=llm or None, llm_model=llm_model, llm_effort=llm_effort,
            )
        if client is None:
            # Summary across every indexed client.
            review_floor = config.deal_discovery.review_confidence
            table = Table(title="Discovered deals — all indexed clients")
            table.add_column("Client")
            table.add_column("Deals", justify="right")
            table.add_column("Low confidence", justify="right")
            for name in index_db.distinct_clients(conn):
                folders = deal_folders_for_client(conn, name)
                low = sum(1 for d in folders if d.confidence < review_floor)
                table.add_row(name, str(len(folders)), str(low) if low else "—")
            console.print(table)
            if not refresh and not llm:
                console.print("(showing stored results — add [cyan]--refresh[/cyan] to recompute from the current index)")
            return
        found = deal_folders_for_client(conn, client)
    finally:
        conn.close()

    if not found:
        console.print(
            f"no deal folders discovered under [bold]{client}[/bold] — the client folder may be "
            f"incomplete, or not scanned yet (try [cyan]pv-extractor scan[/cyan], then "
            f"[cyan]pv-extractor deals --client ... --refresh[/cyan])"
        )
        return
    review_floor = config.deal_discovery.review_confidence
    table = Table(title=f"Discovered deals — {client}")
    table.add_column("Deal")
    table.add_column("Confidence", justify="right")
    table.add_column("Method")
    table.add_column("Periods", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Folder(s)")
    for deal in found:
        ev = deal.evidence
        color = "green" if deal.confidence >= review_floor else "yellow"
        table.add_row(
            deal.name,
            f"[{color}]{deal.confidence:.2f}[/{color}]",
            deal.method,
            str(ev.period_children + ev.period_recurrence),
            str(ev.total_files),
            "\n".join(deal.folder_paths),
        )
    console.print(table)
    low = sum(1 for d in found if d.confidence < review_floor)
    if low:
        console.print(
            f"[yellow]{low} deal(s) below review confidence {review_floor:.2f}[/yellow] — "
            f"verify in the GUI, or re-run with [cyan]--llm[/cyan] for a second opinion"
        )


@app.command("compile-schema")
def compile_schema_cmd(
    workbook: Annotated[Path, typer.Option(help="Master index workbook")] = Path("reference/master_index_v4.xlsx"),
    out_dir: Annotated[Path, typer.Option(help="Output directory for JSON artifacts")] = Path("schema"),
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Compile the three-header-row workbook into schema/master_schema.json + band_routing.json."""
    from pv_extractor.schema.compile_schema import compile_schema

    config = _setup(config_path)
    fields, routing_doc = compile_schema(workbook, out_dir, config.pv_root)
    n_routes = len(routing_doc.get("routing", routing_doc))
    console.print(f"compiled [bold]{len(fields)}[/bold] fields, {n_routes} methodology routes -> {out_dir}/")


@app.command("init-db")
def init_db_cmd(config_path: ConfigOpt = Path("config.yaml")) -> None:
    """Create the SQLite database and schema (idempotent)."""
    from pv_extractor.indexer.db import init_schema, open_db

    config = _setup(config_path)
    conn = open_db(config.db_path, config.pv_root)
    try:
        init_schema(conn)
    finally:
        conn.close()
    console.print(f"database ready at {config.db_path}")


@app.command("self-check")
def self_check_cmd(config_path: ConfigOpt = Path("config.yaml")) -> None:
    """Run read-only startup checks (provider CLI availability/auth); never sends data."""
    from pv_extractor.system.claude_code import run_startup_checks

    config = _setup(config_path)
    snapshot = run_startup_checks(config)
    table = Table(title="Startup checks")
    table.add_column("check")
    table.add_column("ok")
    table.add_column("detail")
    for res in snapshot.results:
        table.add_row(res.check, "[green]yes[/green]" if res.ok else "[red]NO[/red]", res.detail)
    console.print(table)
    console.print(f"snapshot appended to {config.output_dir}/logs/startup_checks.jsonl")


@app.command()
def models(config_path: ConfigOpt = Path("config.yaml")) -> None:
    """Show the active LLM provider model menu and pricing assumptions."""
    from pv_extractor.llm.model_registry import ModelRegistry

    config = _setup(config_path)
    registry = ModelRegistry.load(config.llm.models_path)

    provider = config.llm.provider
    entries = registry.entries_for_provider(provider)
    table = Table(title=f"{provider} model menu — prices are editable estimates when available")
    for col in ("provider", "alias", "model id", "display name", "context", "effort", "in", "out",
                "cache hit", "5m write", "1h write", "notes"):
        table.add_column(col)
    for entry in entries:
        p = entry.pricing_per_mtok
        notes = []
        if entry.latest_alias:
            notes.append("floats with CLI updates")
        if entry.pinned:
            notes.append("pinned")
        if entry.requires_explicit_enable:
            notes.append("[red]explicit enable only[/red]")
        table.add_row(
            entry.provider, entry.alias, entry.id, entry.display_name, f"{entry.context_window:,}",
            entry.default_effort,
            f"${p.input:.2f}" if p else "unavailable",
            f"${p.output:.2f}" if p else "unavailable",
            f"${p.cache_hit:.2f}" if p else "unavailable",
            f"${p.cache_write_5m:.2f}" if p else "unavailable",
            f"${p.cache_write_1h:.2f}" if p else "unavailable",
            ", ".join(notes),
        )
    console.print(table)
    console.print(f"last reviewed: [bold]{registry.menu.last_reviewed or 'never'}[/bold]")
    console.print(f"edit prices: {config.llm.models_path}")
    console.print(
        f"routing_mode={config.llm.routing_mode}  single_model="
        f"{config.llm.single_model_provider}/{config.llm.single_model_model}/{config.llm.single_model_effort}  "
        f"budget=${config.llm.budget_usd:.2f}/run  allow_fable={config.llm.allow_fable}"
    )


@app.command()
def costs(
    run: Annotated[str, typer.Option("--run", help="Run id, e.g. RUN_20260611_120000")],
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Show the local LLM cost ledger for one run."""
    from pv_extractor.llm.costs import LEDGER_FILENAME, read_ledger, summarize_ledger

    config = _setup(config_path)
    ledger_path = Path(config.output_dir) / run / "llm" / LEDGER_FILENAME
    if not ledger_path.exists():
        console.print(f"[yellow]no cost ledger for run {run!r}[/yellow] (expected {ledger_path})")
        raise typer.Exit(code=1)
    entries = read_ledger(ledger_path)

    table = Table(title=f"LLM cost ledger — {run}")
    for col in ("memo", "tier", "model", "effort", "cached", "in tok", "out tok",
                "cost USD", "source", "session", "error"):
        table.add_column(col)
    for entry in entries:
        usage = entry.get("usage") or {}
        source = entry.get("cost_source", "estimated")
        source_label = source.upper() if source in {"estimated", "unavailable"} else source
        cost_text = "unavailable" if source == "unavailable" else f"{entry.get('cost_usd', 0.0):.4f}"
        table.add_row(
            entry.get("memo_id", ""), str(entry.get("tier", "")),
            entry.get("model_alias", ""), entry.get("effort", ""),
            "yes" if entry.get("from_cache") else "",
            f"{usage.get('input_tokens', 0):,}", f"{usage.get('output_tokens', 0):,}",
            cost_text, source_label,
            entry.get("session_id") or "", entry.get("error") or "",
        )
    console.print(table)
    summary = summarize_ledger(entries)
    console.print(
        f"memos={summary['memos']} attempts={summary['attempts']} cache_hits={summary['cache_hits']} "
        f"tokens={summary['input_tokens']:,}/{summary['output_tokens']:,}"
    )
    console.print(
        f"total: [bold]${summary['total_usd']:.4f}[/bold] "
        f"(actual ${summary['actual_usd']:.4f} + ESTIMATED ${summary['estimated_usd']:.4f}; "
        f"unavailable attempts {summary.get('unavailable_attempts', 0)})"
    )


@app.command()
def doctor(config_path: ConfigOpt = Path("config.yaml")) -> None:
    """Diagnose the Phase-3/4 setup: provider CLI, auth, structured-output flags, model menu,
    schema artifacts, cost accounting (shared with the GUI Settings screen)."""
    from pv_extractor.system.doctor import collect_doctor_checks

    config = _setup(config_path)
    checks = collect_doctor_checks(config)

    table = Table(title="pv-extractor doctor")
    table.add_column("check")
    table.add_column("ok")
    table.add_column("detail")
    for check in checks:
        table.add_row(check.check, "[green]yes[/green]" if check.ok else "[red]NO[/red]", check.detail)
    console.print(table)
    raise typer.Exit(code=0 if all(check.ok for check in checks) else 1)


@app.command()
def gui(
    port: Annotated[Optional[int], typer.Option(help="Override gui.port from config.yaml")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open the default browser")] = False,
    config_path: ConfigOpt = Path("config.yaml"),
) -> None:
    """Start the local analyst GUI: one uvicorn process on 127.0.0.1 serving
    the API and the built frontend, then open the default browser."""
    import importlib.util

    config = _setup(config_path)

    # Self-install GUI deps before importing them (first_run behavior).
    gui_modules = {"fastapi": "fastapi", "uvicorn": "uvicorn", "ruamel.yaml": "ruamel.yaml"}

    def _module_present(name: str) -> bool:
        # find_spec on a dotted name imports the parent package first; when the
        # parent is absent (e.g. 'ruamel' before ruamel.yaml is installed) it
        # RAISES ModuleNotFoundError instead of returning None. Treat any import
        # failure as "missing" so the self-install path can run.
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    missing_modules = [name for name in gui_modules if not _module_present(name)]
    if missing_modules:
        from pv_extractor.system.setup_check import _requirements_by_extra, install_missing

        gui_reqs = _requirements_by_extra().get("gui", [])
        if config.first_run.install_missing_deps and gui_reqs:
            console.print(f"installing GUI dependencies into this environment: {', '.join(gui_reqs)}")
            ok, output = install_missing(config, gui_reqs)
            if not ok:
                console.print(f"[red]dependency install failed:[/red]\n{output}")
                raise typer.Exit(code=2)
        else:
            console.print(
                "[red]GUI dependencies missing.[/red] Install them with:\n  "
                f"{Path(sys.executable)} -m pip install " + " ".join(f'"{r}"' for r in gui_reqs or missing_modules)
            )
            raise typer.Exit(code=2)

    import threading
    import webbrowser

    import uvicorn

    from pv_extractor.api.app import create_app

    resolved_port = port or config.gui.port
    host = config.gui.host  # validated loopback-only at config load
    fastapi_app = create_app(config, config_path=config_path)

    url = f"http://{host}:{resolved_port}/"
    console.print(f"PV Extractor GUI: [bold]{url}[/bold]  (Ctrl+C to stop)")
    if config.gui.open_browser and not no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(fastapi_app, host=host, port=resolved_port, log_level="warning")


if __name__ == "__main__":
    app()
