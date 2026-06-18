import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useStickyState } from "../lib/uiState";
import { DataTable } from "../components/DataTable";
import { Button, Card, CardHeader, EmptyState, ErrorState, Panel, SkeletonRows, StatusChip } from "../components/ui";
import { IndexRow, RunResult } from "../lib/api";
import { fmtUsd, useLoad } from "../lib/hooks";

type FlagRow = Record<string, string | number | null>;

/** Compact, comma-joined "Ares, Angelo Gordon, +3" from a name list. */
function namePreview(names: string[] | undefined, max = 3): string {
  const list = names ?? [];
  if (list.length === 0) return "—";
  const head = list.slice(0, max).join(", ");
  return list.length > max ? `${head}, +${list.length - max}` : head;
}

/** "Jun 17, 2026, 11:43:27 PM" from an ISO string, or "—". */
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

/** Minutes (float) -> "3m 12s" / "48s" / "1h 04m". */
function fmtDuration(minutes: number | null | undefined): string {
  if (minutes == null) return "—";
  const totalSec = Math.round(minutes * 60);
  if (totalSec < 60) return `${totalSec}s`;
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function QaPills({ qa }: { qa: Record<string, number> | undefined }) {
  return (
    <span className="font-mono">
      <span className="text-ok">{qa?.qa_pass ?? 0}✓</span>/
      <span className="text-warn">{qa?.qa_pass_with_flags ?? 0}⚑</span>/
      <span className="text-err">{qa?.qa_fail ?? 0}✗</span>
    </span>
  );
}

/** One-line digest for the run list (period · N memos · clients · $cost · QA). */
function RunDigest({ r }: { r: RunResult }) {
  const total = (r.qa_counts?.qa_pass ?? 0) + (r.qa_counts?.qa_pass_with_flags ?? 0) + (r.qa_counts?.qa_fail ?? 0);
  return (
    <span className="text-[12px] text-ink-500 flex items-center gap-2 flex-wrap justify-end">
      {r.period && <span className="text-ink-700">{r.period}</span>}
      <span>{r.memos} memos</span>
      {(r.clients?.length ?? 0) > 0 && <span className="text-ink-600">{namePreview(r.clients)}</span>}
      <span>{fmtUsd(r.llm?.total_cost_usd, 2)}</span>
      {total > 0 && <QaPills qa={r.qa_counts} />}
    </span>
  );
}

export default function OutputBrowser() {
  const { runId } = useParams<{ runId?: string }>();
  const navigate = useNavigate();
  const runs = useLoad<{ runs: RunResult[] }>("/api/runs");
  const [expanded, setExpanded] = useStickyState<string | null>("output.expanded", null);

  if (!runId) {
    return (
      <Panel className="space-y-4 max-w-5xl">
        <h1 className="text-xl font-semibold text-ink-900">Output browser</h1>
        <Card>
          <CardHeader title="Pick a run" sub="click a run for a quick preview, then open it" />
          {runs.loading && <SkeletonRows rows={4} cols={3} />}
          {runs.error && <ErrorState message={runs.error} onRetry={runs.reload} />}
          {runs.data && runs.data.runs.length === 0 && <EmptyState title="No runs yet" />}
          <ul>
            {(runs.data?.runs ?? []).map((r) => (
              <li key={r.run_id} className="border-b border-line last:border-0">
                <button
                  className={`w-full text-left px-4 py-3 hover:bg-ink-50 flex justify-between items-center gap-4 ${
                    expanded === r.run_id ? "bg-ink-50" : ""
                  }`}
                  onClick={() => setExpanded((e) => (e === r.run_id ? null : r.run_id))}
                >
                  <span className="flex items-center gap-2 shrink-0">
                    <span className="text-ink-400 text-[11px]">{expanded === r.run_id ? "▾" : "▸"}</span>
                    <span className="font-mono text-[13px]">{r.run_id}</span>
                    {r.dry_run && <span className="text-[10px] uppercase text-ink-400">dry run</span>}
                    {r.cancelled && <StatusChip value="cancelled" />}
                  </span>
                  <RunDigest r={r} />
                </button>
                {expanded === r.run_id && (
                  <div className="px-6 pb-4 pt-1 bg-ink-50/60">
                    <RunPreviewCard r={r} onOpen={() => navigate(`/output/${r.run_id}`)} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </Card>
      </Panel>
    );
  }
  return <RunOutput runId={runId} />;
}

function RunPreviewCard({ r, onOpen }: { r: RunResult; onOpen: () => void }) {
  return (
    <Card className="px-4 py-3 space-y-2">
      <div className="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-1 text-[12.5px] text-ink-700">
        <p>scope <span className="font-medium text-ink-900">{r.scope ?? "—"}</span></p>
        <p>period <span className="font-medium text-ink-900">{r.period ?? "—"}</span></p>
        <p>source files <span className="font-mono">{r.source_files ?? "—"}</span></p>
        <p>memos <span className="font-mono">{r.memos}</span> · rows <span className="font-mono">{r.rows_added ?? "—"}</span></p>
        <p>QA <QaPills qa={r.qa_counts} /></p>
        <p>
          LLM {fmtUsd(r.llm?.total_cost_usd, 4)}{" "}
          {r.llm?.cost_source && <span className="uppercase text-[10px] text-ink-400">{r.llm.cost_source}</span>}
        </p>
        {r.duration_minutes != null && <p>duration <span className="font-mono">{r.duration_minutes} min</span></p>}
      </div>
      {(r.clients?.length ?? 0) > 0 && (
        <p className="text-[12px] text-ink-600">
          <span className="text-ink-400">clients: </span>
          {(r.clients ?? []).join(", ")}
        </p>
      )}
      {(r.companies?.length ?? 0) > 0 && (
        <p className="text-[12px] text-ink-600">
          <span className="text-ink-400">companies: </span>
          {namePreview(r.companies, 8)}
        </p>
      )}
      <div className="flex gap-2 pt-1">
        <Button kind="primary" onClick={onOpen}>
          Open run →
        </Button>
      </div>
    </Card>
  );
}

function RunOutput({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const detail = useLoad<RunResult>(`/api/runs/${runId}`);
  const indexRows = useLoad<{ rows: IndexRow[] }>(`/api/runs/${runId}/index-rows`);
  const flags = useLoad<{ flags: FlagRow[] }>(`/api/runs/${runId}/flags`);
  const runLog = useLoad<{ run_log: FlagRow[] }>(`/api/runs/${runId}/run-log`);
  const [thisRunOnly, setThisRunOnly] = useStickyState("output.thisRunOnly", true);

  const flagRows = useMemo(() => {
    const rows = flags.data?.flags ?? null;
    if (!rows) return rows;
    return thisRunOnly ? rows.filter((r) => r["Run ID"] === runId) : rows;
  }, [flags.data, thisRunOnly, runId]);

  const d = detail.data;

  return (
    <Panel className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <h1 className="text-xl font-semibold text-ink-900">Output</h1>
          <span className="font-mono text-[12px] text-ink-500">{runId}</span>
        </div>
        <div className="flex gap-2">
          <Button kind="secondary" onClick={() => navigate(`/review/${runId}`)}>
            Review queue →
          </Button>
          <a href={`/api/runs/${runId}/workbook`} download>
            <Button kind="primary">Download workbook</Button>
          </a>
          <a href={`/api/runs/${runId}/audits.zip`} download>
            <Button kind="secondary">Download audit JSONs</Button>
          </a>
        </div>
      </div>

      {detail.loading && <Card><SkeletonRows rows={2} cols={4} /></Card>}
      {detail.error && <Card><ErrorState message={detail.error} onRetry={detail.reload} /></Card>}

      {d && (
        <Card>
          <CardHeader title="Run summary" sub={`${d.scope ?? "—"}${d.period ? ` · ${d.period}` : ""}`} />
          <div className="px-4 pb-3 grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-1 text-[13px] text-ink-700 border-b border-line/60 mb-2">
            <p>
              <span className="text-ink-400">started </span>
              <span className="font-mono text-ink-900">{fmtClock(d.started_at ?? d.created_at)}</span>
            </p>
            <p>
              <span className="text-ink-400">finished </span>
              <span className="font-mono text-ink-900">{fmtClock(d.finished_at)}</span>
            </p>
            <p>
              <span className="text-ink-400">took </span>
              <span className="font-mono text-ink-900">{fmtDuration(d.duration_minutes)}</span>
            </p>
          </div>
          <div className="px-4 pb-3 grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2 text-[13px] text-ink-700">
            <p>memos <span className="font-mono text-ink-900">{d.memos}</span></p>
            <p>source files <span className="font-mono">{d.source_files ?? "—"}</span></p>
            <p>rows added <span className="font-mono">{d.rows_added ?? "—"}</span></p>
            <p>flags <span className="font-mono">{d.flags_added ?? "—"}</span></p>
            <p>QA <QaPills qa={d.qa_counts} /></p>
            <p>
              LLM {fmtUsd(d.llm?.total_cost_usd, 4)}{" "}
              {d.llm?.cost_source && <span className="uppercase text-[10px] text-ink-400">{d.llm.cost_source}</span>}
            </p>
            <p>assets <span className="font-mono">{d.assets}</span></p>
          </div>
          {(d.clients?.length ?? 0) > 0 && (
            <p className="px-4 pb-1 text-[12px] text-ink-600">
              <span className="text-ink-400">clients ({d.clients?.length}): </span>
              {(d.clients ?? []).join(", ")}
            </p>
          )}
          {(d.deals?.length ?? 0) > 0 && (
            <p className="px-4 pb-3 text-[12px] text-ink-600">
              <span className="text-ink-400">deals ({d.deals?.length}): </span>
              {namePreview(d.deals, 12)}
            </p>
          )}
          {d.workbook_path && <p className="px-4 pb-3 font-mono text-[11px] text-ink-400 truncate">{d.workbook_path}</p>}
        </Card>
      )}

      {d && (d.sources?.length ?? 0) > 0 && (
        <Card>
          <CardHeader
            title="Source documents"
            sub={`the ${d.sources!.length} file${d.sources!.length === 1 ? "" : "s"} this run extracted from`}
          />
          <ul className="px-4 pb-3 max-h-[30vh] overflow-auto divide-y divide-line/60">
            {d.sources!.map((s, i) => (
              <li key={i} className="py-1.5">
                <p className="text-[12.5px] text-ink-800">
                  {s.file_name ?? "—"}
                  {(s.client || s.deal) && (
                    <span className="text-ink-400 ml-2 text-[11.5px]">
                      {[s.client, s.deal].filter(Boolean).join(" / ")}
                    </span>
                  )}
                </p>
                {s.file_path && (
                  <p className="font-mono text-[11px] text-ink-400 break-all">{s.file_path}</p>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        <CardHeader title="Index rows produced by this run" sub="key extracted columns per memo / asset — read-only from this run's workbook" />
        <DataTable<IndexRow>
          rows={indexRows.data?.rows ?? null}
          loading={indexRows.loading}
          error={indexRows.error}
          onRetry={indexRows.reload}
          emptyTitle="No index rows"
          emptyHint="This run wrote no Index rows (dry run, or every memo deferred)."
          rowKey={(r) => String(r.memo_id ?? Math.random())}
          maxHeight="50vh"
          filterable
          columns={[
            { key: "memo", header: "Memo", render: (r) => <span className="font-mono text-[11px]">{r.memo_id ?? ""}</span>, sortValue: (r) => String(r.memo_id ?? ""), filterValue: (r) => String(r.memo_id ?? "") },
            { key: "company", header: "Company", render: (r) => r.portfolio_company ?? "—", sortValue: (r) => String(r.portfolio_company ?? ""), filterValue: (r) => String(r.portfolio_company ?? "") },
            { key: "fund", header: "Fund manager", render: (r) => <span className="text-[12px]">{r.fund_manager ?? "—"}</span>, sortValue: (r) => String(r.fund_manager ?? ""), filterValue: (r) => String(r.fund_manager ?? "") },
            { key: "period", header: "Period", render: (r) => <span className="text-[12px]">{r.reporting_period || r.valuation_date || "—"}</span>, sortValue: (r) => String(r.reporting_period ?? r.valuation_date ?? ""), filterValue: (r) => `${r.reporting_period ?? ""} ${r.valuation_date ?? ""}` },
            { key: "method", header: "Methodology", render: (r) => <span className="text-[12px]">{r.primary_methodology ?? "—"}</span>, sortValue: (r) => String(r.primary_methodology ?? ""), filterValue: (r) => String(r.primary_methodology ?? "") },
            { key: "headline", header: "Equity Val 100% ($M)", align: "right", render: (r) => <span className="font-mono text-[12px]">{r.headline_value ?? "—"}</span>, sortValue: (r) => (typeof r.headline_value === "number" ? r.headline_value : null) },
            { key: "moic", header: "MOIC", align: "right", render: (r) => <span className="font-mono text-[12px]">{r.moic ?? "—"}</span>, sortValue: (r) => (typeof r.moic === "number" ? r.moic : null) },
            { key: "qa", header: "QA", render: (r) => (r.qa_status ? <StatusChip value={r.qa_status} /> : "—"), sortValue: (r) => r.qa_status, filterValue: (r) => r.qa_status },
          ]}
        />
      </Card>

      <Card>
        <CardHeader
          title="Review flags"
          sub="mirror of the workbook's Review Flags sheet"
          right={
            <label className="flex items-center gap-1.5 text-[12px] text-ink-600">
              <input type="checkbox" checked={thisRunOnly} onChange={(e) => setThisRunOnly(e.target.checked)} />
              this run only
            </label>
          }
        />
        <DataTable<FlagRow>
          rows={flagRows}
          loading={flags.loading}
          error={flags.error}
          onRetry={flags.reload}
          emptyTitle="No flags"
          rowKey={(r) => `${r["Memo ID"]}|${r["Flag #"]}|${r["Flag Description"]}`}
          maxHeight="50vh"
          filterable
          columns={[
            { key: "memo", header: "Memo", render: (r) => <span className="font-mono text-[12px]">{String(r["Memo ID"] ?? "")}</span>, sortValue: (r) => String(r["Memo ID"] ?? ""), filterValue: (r) => String(r["Memo ID"] ?? "") },
            { key: "company", header: "Company", render: (r) => String(r["Portfolio Company"] ?? ""), sortValue: (r) => String(r["Portfolio Company"] ?? ""), filterValue: (r) => String(r["Portfolio Company"] ?? "") },
            { key: "qa", header: "QA", render: (r) => <StatusChip value={String(r["QA Status"] ?? "")} /> },
            { key: "cat", header: "Category", render: (r) => String(r["Flag Category"] ?? ""), sortValue: (r) => String(r["Flag Category"] ?? ""), filterValue: (r) => String(r["Flag Category"] ?? "") },
            { key: "desc", header: "Description", render: (r) => <span className="text-[12px]">{String(r["Flag Description"] ?? "")}</span>, filterValue: (r) => String(r["Flag Description"] ?? "") },
            { key: "attn", header: "Attn", render: (r) => (r["Reviewer Attention (Y/N)"] === "Y" ? <span className="text-warn">⚑</span> : null) },
            { key: "res", header: "Resolved", render: (r) => String(r["Resolved (Y/N)"] ?? ""), sortValue: (r) => String(r["Resolved (Y/N)"] ?? "") },
            { key: "note", header: "Notes", render: (r) => <span className="text-[12px] text-ink-500">{String(r["Resolution Notes"] ?? "")}</span> },
          ]}
        />
      </Card>

      <Card>
        <CardHeader title="Run log" sub="mirror of the workbook's Run Log sheet" />
        <DataTable<FlagRow>
          rows={runLog.data?.run_log ?? null}
          loading={runLog.loading}
          error={runLog.error}
          onRetry={runLog.reload}
          emptyTitle="No run log rows"
          rowKey={(r) => String(r["Run ID"] ?? Math.random())}
          columns={[
            { key: "run", header: "Run", render: (r) => <span className="font-mono text-[12px]">{String(r["Run ID"] ?? "")}</span> },
            { key: "date", header: "Date", render: (r) => String(r["Run Date"] ?? "") },
            { key: "memos", header: "Memos", align: "right", render: (r) => String(r["Memos Processed"] ?? "") },
            { key: "rows", header: "Rows", align: "right", render: (r) => String(r["Records Added to Index"] ?? "") },
            { key: "flags", header: "Flags", align: "right", render: (r) => String(r["Total Flags"] ?? "") },
            { key: "dur", header: "Duration (min)", align: "right", render: (r) => String(r["Run Duration (mins)"] ?? "") },
            { key: "sessions", header: "Batch sessions", render: (r) => <span className="font-mono text-[11px] text-ink-500 break-all">{String(r["Batch Sessions"] ?? "")}</span> },
            { key: "notes", header: "Notes", render: (r) => <span className="text-[12px] text-ink-600">{String(r["Notes"] ?? "")}</span> },
          ]}
        />
      </Card>
    </Panel>
  );
}
