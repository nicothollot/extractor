import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { CostMeter } from "../components/charts";
import { LlmActivityView } from "../components/LlmActivityView";
import { LogTail } from "../components/LogTail";
import { ProgressLanes, buildLanes } from "../components/ProgressLanes";
import { Button, Card, CardHeader, Panel, StatusChip } from "../components/ui";
import { HLSpinner } from "../components/branding";
import { RunResult, post } from "../lib/api";
import { fmtDuration, useJobEvents } from "../lib/hooks";

export default function RunProgress() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const { events, job } = useJobEvents(jobId ?? null);

  const cost = useMemo(() => {
    let spent = 0;
    for (const e of events) {
      if (e.type === "cost_tick" && typeof e.payload.total_usd === "number") spent = e.payload.total_usd;
      if (e.type === "llm_phase" && typeof e.payload.total_cost_usd === "number") spent = e.payload.total_cost_usd;
    }
    return spent;
  }, [events]);

  const dryRun = Boolean((job?.params as Record<string, unknown> | undefined)?.dry_run);
  // Overall progress + rough ETA, folded from the event stream. "Done" for a
  // memo = validate finished (deterministic work complete) — the LLM pass and
  // workbook writes follow as labelled tail phases.
  const progress = useMemo(() => {
    let total = 0;
    let startMs: number | null = null;
    let lastMs: number | null = null;
    const finished = new Set<string>();
    let llmStatus: string | null = null;
    let writes = 0;
    for (const e of events) {
      lastMs = Date.parse(e.ts);
      if (e.type === "run_started") {
        total = Number(e.payload.pairs ?? 0);
        startMs = lastMs;
      }
      if (e.type === "stage") {
        const key = `${e.payload.client}|${e.payload.deal}`;
        const terminal = dryRun ? e.payload.stage === "verify" : e.payload.stage === "validate" && e.payload.status === "done";
        if (terminal) finished.add(key);
        if (e.payload.stage === "write" && e.payload.status === "done") writes++;
      }
      if (e.type === "llm_phase") llmStatus = String(e.payload.status);
    }
    const elapsed = startMs !== null && lastMs !== null ? (lastMs - startMs) / 1000 : 0;
    const done = finished.size;
    const eta = done > 0 && total > done ? (elapsed / done) * (total - done) : null;
    return { total, done, elapsed, eta, llmStatus, writes };
  }, [events, dryRun]);

  // When the multi-search batch path stamped a firm on the lanes, surface the
  // firm count in the lanes sub-header; single runs carry no group (unchanged).
  const firmCount = useMemo(() => {
    const groups = new Set<string>();
    for (const lane of buildLanes(events)) {
      if (lane.group != null) groups.add(lane.group);
    }
    return groups.size;
  }, [events]);

  const params = job?.params as Record<string, unknown> | undefined;
  const llmParams = (params?.llm ?? {}) as Record<string, unknown>;
  const budget = typeof llmParams.budget_usd === "number" ? llmParams.budget_usd : 25;
  const active = job ? ["queued", "running", "cancelling"].includes(job.status) : true;
  const result = job?.result as RunResult | null;
  const llmEnabled = llmParams.enabled !== false;
  const llmDiag = (result?.diagnostics?.llm ?? {}) as Record<string, unknown>;
  const [diagnosticsCopied, setDiagnosticsCopied] = useState(false);
  const diagnosticText = useMemo(
    () =>
      JSON.stringify(
        {
          job: job
            ? {
                id: job.id,
                kind: job.kind,
                status: job.status,
                run_id: job.run_id,
                error: job.error,
                diagnostics: job.diagnostics ?? null,
              }
            : null,
          run: result?.diagnostics ?? null,
        },
        null,
        2,
      ),
    [job, result?.diagnostics],
  );

  const copyDiagnostics = async () => {
    await navigator.clipboard.writeText(diagnosticText);
    setDiagnosticsCopied(true);
    window.setTimeout(() => setDiagnosticsCopied(false), 2000);
  };

  // Review is the natural last step: a completed non-dry run flows into the
  // review queue automatically (with an opt-out so the analyst can linger).
  const [autoReview, setAutoReview] = useState(true);
  const redirectedRef = useRef(false);
  const finishedRun = !active && Boolean(job?.run_id) && !(params?.dry_run as boolean) && job?.status === "completed";
  useEffect(() => {
    if (!finishedRun || !autoReview || redirectedRef.current) return;
    redirectedRef.current = true;
    const t = window.setTimeout(() => navigate(`/review/${job!.run_id}`), 1800);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedRun, autoReview]);

  return (
    <Panel className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {active && <HLSpinner size={28} />}
          <h1 className="text-xl font-semibold text-ink-900">Run progress</h1>
          {job && <StatusChip value={job.status} />}
          <span className="font-mono text-[12px] text-ink-500">{job?.run_id ?? jobId}</span>
        </div>
        <div className="flex gap-2">
          {active && job && (
            <Button kind="danger" onClick={() => post(`/api/jobs/${job.id}/cancel`)} disabled={job.status === "cancelling"}>
              {job.status === "cancelling" ? "Cancelling — finishing in-flight memo…" : "Cancel run"}
            </Button>
          )}
          {!active && job?.run_id && !(params?.dry_run as boolean) && (
            <>
              <Button kind="secondary" onClick={() => navigate(`/review/${job.run_id}`)}>
                Review queue →
              </Button>
              <Button kind="primary" onClick={() => navigate(`/output/${job.run_id}`)}>
                Output →
              </Button>
            </>
          )}
        </div>
      </div>

      {job?.error && (
        <Card className="px-4 py-3 space-y-2">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-[13px] text-err font-medium">{job.error}</p>
              {job.diagnostics?.context && (
                <p className="text-[12px] text-ink-600 mt-1">
                  context <span className="font-mono">{JSON.stringify(job.diagnostics.context)}</span>
                </p>
              )}
            </div>
            <Button kind="secondary" onClick={copyDiagnostics}>
              {diagnosticsCopied ? "Copied" : "Copy diagnostics"}
            </Button>
          </div>
        </Card>
      )}

      {finishedRun && autoReview && (
        <Card className="px-4 py-3 flex items-center justify-between bg-info-soft">
          <p className="text-[13px] text-ink-800">
            Run complete — opening the <span className="font-medium">review queue</span> for {job?.run_id}…
          </p>
          <Button kind="ghost" onClick={() => setAutoReview(false)}>
            Stay on this page
          </Button>
        </Card>
      )}

      {progress.total > 0 && (
        <Card className="px-4 py-3 space-y-2">
          <div className="flex items-baseline justify-between gap-4 text-[12.5px]">
            <p className="text-ink-800">
              {active ? (
                <>
                  Memo <span className="font-semibold">{Math.min(progress.done + 1, progress.total)}</span> of{" "}
                  <span className="font-semibold">{progress.total}</span>
                  {progress.llmStatus === "started" && <span className="text-ink-500"> · LLM escalation running</span>}
                  {progress.writes > 0 && progress.llmStatus !== "started" && (
                    <span className="text-ink-500"> · writing workbook + audits</span>
                  )}
                </>
              ) : (
                <>
                  <span className="font-semibold">{progress.done}</span> of <span className="font-semibold">{progress.total}</span>{" "}
                  memos processed
                </>
              )}
            </p>
            <p className="text-ink-500 shrink-0">
              elapsed {fmtDuration(progress.elapsed)}
              {active && progress.eta !== null && (
                <> · ~{fmtDuration(progress.eta)} remaining <span className="text-ink-400">(rough)</span></>
              )}
            </p>
          </div>
          <div className="h-1.5 bg-surface border border-line rounded overflow-hidden">
            <div
              className="h-full bg-[var(--hl-blue)] transition-all duration-500"
              style={{ width: `${progress.total > 0 ? Math.min(100, (progress.done / progress.total) * 100) : 0}%` }}
            />
          </div>
        </Card>
      )}

      <Card>
        <CardHeader
          title="Pipeline lanes"
          sub={
            firmCount > 0
              ? `locate ▸ verify ▸ read ▸ extract ▸ validate ▸ write — grouped by firm (${firmCount} firms)`
              : "locate ▸ verify ▸ read ▸ extract ▸ validate ▸ write — one lane per memo"
          }
        />
        <ProgressLanes events={events} />
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardHeader title="Cost meter" sub={llmEnabled ? "live spend vs hard budget cap" : "LLM fallback disabled for this run"} />
          <div className="px-4 pb-4">
            {llmEnabled ? <CostMeter spent={cost} budget={budget} source={cost > 0 ? "ledger" : null} /> : (
              <p className="text-[12px] text-ink-400 py-3">Pure Phase-2 run — escalation plans land in the audit records.</p>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="Result" />
          <div className="px-4 pb-4 text-[13px] text-ink-700 space-y-1">
            {result ? (
              <>
                <p>
                  memos <span className="font-mono">{result.memos}</span> · rows{" "}
                  <span className="font-mono">{result.rows_added ?? "—"}</span> · flags{" "}
                  <span className="font-mono">{result.flags_added ?? "—"}</span>
                </p>
                <p>
                  QA <span className="text-ok font-mono">{result.qa_counts?.qa_pass ?? 0}</span> /{" "}
                  <span className="text-warn font-mono">{result.qa_counts?.qa_pass_with_flags ?? 0}</span> /{" "}
                  <span className="text-err font-mono">{result.qa_counts?.qa_fail ?? 0}</span>
                </p>
                {result.llm?.executed && (
                  <p>
                    LLM: {result.llm.attempts} call(s), {result.llm.cache_hits} cached,{" "}
                    <span className="font-mono">${result.llm.total_cost_usd.toFixed(4)}</span>{" "}
                    <span className="uppercase text-[10px] text-ink-400">{result.llm.cost_source}</span>
                  </p>
                )}
                {result.diagnostics && (
                  <p className="text-ink-500">
                    diagnostics: {String(llmDiag.task_count_by_wave ? JSON.stringify(llmDiag.task_count_by_wave) : "0")} tasks by wave
                    {typeof llmDiag.selected_page_count === "number" && <> · {llmDiag.selected_page_count} page selections</>}
                    {typeof llmDiag.timeouts === "number" && llmDiag.timeouts > 0 && <> · {llmDiag.timeouts} timeout(s)</>}
                    {" "}
                    <button className="underline" onClick={copyDiagnostics}>
                      {diagnosticsCopied ? "copied" : "copy summary"}
                    </button>
                  </p>
                )}
              </>
            ) : (
              <p className="text-ink-400">running…</p>
            )}
          </div>
        </Card>
      </div>

      <LlmActivityView events={events} />

      <Card>
        <CardHeader title="Log tail" sub="structured pipeline records (identifiers only, never memo content)" />
        <div className="px-4 pb-4">
          <LogTail events={events} />
        </div>
      </Card>
    </Panel>
  );
}
