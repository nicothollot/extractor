import { useNavigate } from "react-router-dom";
import { CoverageDonut, Sparkline } from "../components/charts";
import { DataTable } from "../components/DataTable";
import { Button, Card, CardHeader, Panel, StatusChip } from "../components/ui";
import { JobInfo, RunResult } from "../lib/api";
import { fmtUsd, useLoad } from "../lib/hooks";

export default function Dashboard() {
  const navigate = useNavigate();
  const runs = useLoad<{ runs: RunResult[] }>("/api/runs");
  const costs = useLoad<{ points: { run_id: string; total_usd: number }[] }>("/api/costs/history");
  const jobs = useLoad<{ jobs: JobInfo[] }>("/api/jobs?kind=run");

  const activeJob = jobs.data?.jobs.find((j) => ["queued", "running", "cancelling"].includes(j.status));
  const latest = runs.data?.runs[0];

  return (
    <Panel className="space-y-4 max-w-[1600px]">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">Dashboard</h1>
        <div className="flex gap-2">
          {activeJob && (
            <Button kind="secondary" onClick={() => navigate(`/jobs/${activeJob.id}/progress`)}>
              View running job
            </Button>
          )}
          <Button kind="primary" onClick={() => navigate("/new-run")}>
            New run
          </Button>
        </div>
      </div>

      {activeJob && (
        <Card className="px-4 py-3 flex items-center gap-3">
          <StatusChip value={activeJob.status} />
          <span className="text-[13px] text-ink-700">
            Run in progress — {String(activeJob.params.scope)} / {String(activeJob.params.period)}
          </span>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-4">
        <Card lift>
          <CardHeader title="Coverage — latest run" sub={latest ? latest.run_id : undefined} />
          <div className="px-4 pb-4">
            {latest ? (
              <CoverageDonut counts={latest.coverage_counts ?? {}} />
            ) : (
              <p className="text-[12px] text-ink-400 py-8 text-center">Run the pipeline to see coverage</p>
            )}
          </div>
        </Card>
        <Card lift>
          <CardHeader title="LLM cost" sub="per-run ledger totals" />
          <div className="px-4 pb-4">
            <Sparkline
              points={(costs.data?.points ?? []).map((p) => p.total_usd)}
              labels={(costs.data?.points ?? []).map((p) => p.run_id)}
            />
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Recent runs" sub="status · scope · memos · flags · QA · cost" />
        <DataTable<RunResult>
          rows={runs.data?.runs ?? null}
          loading={runs.loading}
          error={runs.error}
          onRetry={runs.reload}
          emptyTitle="No runs yet"
          emptyHint="Start with the New Run wizard — a dry run is free and read-only."
          rowKey={(r) => r.run_id}
          onRowClick={(r) => navigate(`/output/${r.run_id}`)}
          columns={[
            { key: "run", header: "Run", render: (r) => <span className="font-mono text-[12px]">{r.run_id}</span>, sortValue: (r) => r.run_id },
            {
              key: "source",
              header: "Source",
              render: (r) => <span className="text-ink-500 text-[12px]">{r.source ?? "gui"}</span>,
            },
            { key: "scope", header: "Scope", render: (r) => r.scope ?? "—", sortValue: (r) => r.scope ?? "" },
            { key: "period", header: "Period", render: (r) => r.period ?? "—" },
            { key: "memos", header: "Memos", align: "right", render: (r) => r.memos, sortValue: (r) => r.memos },
            {
              key: "flags",
              header: "Flags",
              align: "right",
              render: (r) => r.flags_added ?? "—",
              sortValue: (r) => r.flags_added ?? 0,
            },
            {
              key: "qa",
              header: "QA",
              render: (r) => (
                <span className="font-mono text-[12px]">
                  <span className="text-ok">{r.qa_counts?.qa_pass ?? 0}</span>
                  {" / "}
                  <span className="text-warn">{r.qa_counts?.qa_pass_with_flags ?? 0}</span>
                  {" / "}
                  <span className="text-err">{r.qa_counts?.qa_fail ?? 0}</span>
                </span>
              ),
            },
            {
              key: "cost",
              header: "LLM cost",
              align: "right",
              render: (r) => (
                <span className="font-mono text-[12px]">
                  {fmtUsd(r.llm?.total_cost_usd, 4)}
                  {r.llm?.cost_source === "estimated" && (
                    <span className="text-[10px] text-ink-400 ml-1">EST</span>
                  )}
                </span>
              ),
              sortValue: (r) => r.llm?.total_cost_usd ?? 0,
            },
            {
              key: "review",
              header: "",
              render: (r) => (
                <button
                  className="text-accent text-[12px] hover:underline"
                  onClick={(e) => {
                    e.stopPropagation();
                    navigate(`/review/${r.run_id}`);
                  }}
                >
                  review →
                </button>
              ),
            },
          ]}
        />
      </Card>
    </Panel>
  );
}
