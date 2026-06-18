import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { DataTable } from "../components/DataTable";
import { FirmRegion } from "../components/FirmRegion";
import { DocTypeListBuilder } from "../components/DocTypeListBuilder";
import { FolderPicker } from "../components/FolderPicker";
import { ModelEffortPicker } from "../components/ModelEffortPicker";
import { ModelPricingTable } from "../components/ModelPricingTable";
import { PeriodMultiPicker } from "../components/PeriodMultiPicker";
import { Stepper } from "../components/Stepper";
import { Button, Card, CardHeader, Field, Panel, StatusChip, Toggle, inputCls } from "../components/ui";
import {
  CoverageEntry,
  JobInfo,
  ModelsResponse,
  MultiFirmSelection,
  MultiSearchFirm,
  MultiSearchRunRequest,
  MultiSelectionResponse,
  MultiSlot,
  PreflightEstimate,
  RunResult,
  SelectionResponse,
  SelectionSlot,
  VerifyFileResponse,
  get,
  post,
} from "../lib/api";
import { fmtUsd, useJobPolling, useLoad } from "../lib/hooks";
import { FirmEntry, useWizard, WizardState } from "../lib/wizard";

const STEPS = ["Scope", "Template", "AI / model", "Preflight", "Confirm documents", "Launch", "Review"];

interface Period {
  as_of_date: string;
  label: string;
}

interface DealFolderInfo {
  name: string;
  confidence: number | null;
  method: string;
  low_confidence: boolean;
  folder_paths: string[];
  periods: number;
  file_count: number;
  memo_file_count: number;
  llm_corroborated: boolean;
  score?: number;
}

interface PeriodMatch {
  as_of_date: string;
  label: string;
  date_folders: string[];
  exact: boolean;
  distance_days: number | null;
}

/** Debounce a changing value (manual-search inputs hit the API as you type). */
function useDebounced<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

const confidencePct = (c: number | null) => (c === null ? "—" : `${Math.round(c * 100)}%`);
// The four DocType enum values RunRequest.doc_type accepts; any other slug is a
// profile/free-text doc type that must travel in doc_types (slot expansion).
const ENUM_DOC_TYPES = new Set(["valuation_memo", "ic_memo", "portfolio_review", "any_client_valuation_doc"]);

const slotKeyParts = (key: string): { client: string; deal: string } => {
  const sep = key.indexOf("|");
  return { client: key.slice(0, sep), deal: key.slice(sep + 1) };
};

/* Top-level mode switch. Single Search is the existing wizard, byte-for-behavior
   unchanged; Multi Search is purely additive. The choice persists on the wizard
   state so it survives tab switches. */
export default function NewRun() {
  const { state, patch } = useWizard();
  const mode = state.searchMode;
  return (
    <div className="space-y-3 max-w-6xl">
      <div className="flex gap-1 bg-surface border border-line rounded-[var(--hl-radius)] p-1 w-fit">
        {(
          [
            ["single", "Single Search"],
            ["multi", "Multi Search"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => patch({ searchMode: key })}
            className={`px-4 py-1.5 rounded-[calc(var(--hl-radius)-2px)] text-[13px] transition-colors ${
              mode === key ? "bg-[var(--hl-blue)] text-white font-medium" : "text-ink-600 hover:bg-line/50"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {mode === "single" ? <SingleRun /> : <MultiRun />}
    </div>
  );
}

function SingleRun() {
  const navigate = useNavigate();
  const { state, patch, reset } = useWizard();
  const {
    step, scope, client, deal, period, periods, docType, docTypes, restrictClientSourced, discoveryMode, llmDiscoverModel, llmDiscoverEffort,
    template, dryRunOnly, llmEnabled, mode, manualModel, manualEffort, budget,
    forceLlmAssist,
    preflightJobId, estimate, removedSlots, docsConfirmed, runJobId, runId,
  } = state;

  // ----- setters over the persisted wizard store -----
  const setStep = (v: number) => patch({ step: v });
  // Changing scope-defining inputs invalidates the preflight + selection.
  const patchScope = (p: Partial<typeof state>) =>
    patch({ ...p, preflightJobId: null, estimate: null, docsConfirmed: false, removedSlots: [] });
  // Changing AI/cost settings only invalidates the cost estimate (re-fetched).
  const patchAi = (p: Partial<typeof state>) => patch({ ...p, estimate: null });

  // transient (no need to survive tab switches)
  const [clientQuery, setClientQuery] = useState("");
  const [dealQuery, setDealQuery] = useState("");
  const [periodQuery, setPeriodQuery] = useState("");
  const debClientQuery = useDebounced(clientQuery);
  const debDealQuery = useDebounced(dealQuery);
  const debPeriodQuery = useDebounced(periodQuery);
  const [llmDiscoverJobId, setLlmDiscoverJobId] = useState<string | null>(null);
  const [llmDiscoverError, setLlmDiscoverError] = useState<string | null>(null);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);

  const clients = useLoad<{ clients: string[] }>("/api/index/clients");
  const deals = useLoad<{
    deals: string[];
    deal_folders: DealFolderInfo[];
    last_llm_discovery: { model: string; effort: string; at: string; deals: number } | null;
  }>(
    client ? `/api/index/deals?client=${encodeURIComponent(client)}` : null,
    [client],
  );

  // manual fuzzy search (debounced as-you-type)
  const clientMatches = useLoad<{ matches: { client: string; score: number }[] }>(
    discoveryMode === "search" && debClientQuery.trim()
      ? `/api/index/search/clients?q=${encodeURIComponent(debClientQuery)}`
      : null,
    [debClientQuery, discoveryMode],
  );
  const dealMatches = useLoad<{ matches: DealFolderInfo[] }>(
    discoveryMode === "search" && client && debDealQuery.trim()
      ? `/api/index/search/deals?client=${encodeURIComponent(client)}&q=${encodeURIComponent(debDealQuery)}`
      : null,
    [client, debDealQuery, discoveryMode],
  );
  const periodMatches = useLoad<{
    resolved_as_of: string | null;
    resolved_label: string | null;
    parse_error: string | null;
    matches: PeriodMatch[];
  }>(
    discoveryMode === "search" && scope === "deal" && client && deal && debPeriodQuery.trim()
      ? `/api/index/search/periods?client=${encodeURIComponent(client)}&deal=${encodeURIComponent(deal)}&q=${encodeURIComponent(debPeriodQuery)}`
      : null,
    [client, deal, debPeriodQuery, discoveryMode, scope],
  );

  // LLM-assisted discovery job
  const llmDiscoverJob = useJobPolling(llmDiscoverJobId);
  useEffect(() => {
    if (llmDiscoverJob?.status === "completed") deals.reload();
    if (llmDiscoverJob?.status === "failed") setLlmDiscoverError(llmDiscoverJob.error ?? "discovery failed");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [llmDiscoverJob?.status]);

  const startLlmDiscovery = async () => {
    setLlmDiscoverError(null);
    const prior = deals.data?.last_llm_discovery;
    if (prior) {
      const when = new Date(prior.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      const ok = window.confirm(
        `You already ran an LLM deal discovery for ${client} with ${prior.model} on ${when} ` +
          `(${prior.deals} deal folder${prior.deals === 1 ? "" : "s"}), saved in the index.\n\n` +
          `OK = run a new LLM discovery and replace it (incurs cost).\n` +
          `Cancel = keep the existing discovery (use the Browse tab to pick from it).`,
      );
      if (!ok) return;
    }
    try {
      const r = await post<{ job: JobInfo }>("/api/index/deals/refresh", {
        client,
        llm: true,
        llm_model: llmDiscoverModel,
        llm_effort: llmDiscoverEffort,
      });
      setLlmDiscoverJobId(r.job.id);
    } catch (e) {
      setLlmDiscoverError((e as Error).message);
    }
  };

  const llmDiscoveredDeals = useMemo(() => {
    const result = llmDiscoverJob?.result as { deals?: DealFolderInfo[] } | null;
    return llmDiscoverJob?.status === "completed" ? (result?.deals ?? []) : null;
  }, [llmDiscoverJob]);

  const selectedDealInfo = useMemo(
    () => (deal ? (deals.data?.deal_folders ?? []).find((f) => f.name === deal) ?? null : null),
    [deal, deals.data],
  );
  const discoveredPeriods = useLoad<{ periods: Period[] }>(
    scope === "deal" && client && deal
      ? `/api/index/periods?client=${encodeURIComponent(client)}&deal=${encodeURIComponent(deal)}`
      : scope === "client" && client
        ? `/api/index/periods?client=${encodeURIComponent(client)}`
        : "/api/index/periods",
    [scope, client, deal],
  );
  const templates = useLoad<{ default_template: string; previous_outputs: { run_id: string; path: string }[] }>("/api/templates");
  const models = useLoad<ModelsResponse>("/api/models");

  // one-time defaults from config (guarded so a tab-switch round trip never
  // clobbers the analyst's choices)
  useEffect(() => {
    if (models.data && !state.aiInitialized) {
      patch({
        budget: budget || String(models.data.llm.budget_usd),
        manualModel: models.data.llm.manual_model,
        manualEffort: models.data.llm.manual_effort,
        aiInitialized: true,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models.data]);

  useEffect(() => {
    if (templates.data && !state.templateInitialized) {
      patch({
        template: templates.data.previous_outputs[0]?.path ?? templates.data.default_template,
        templateInitialized: true,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templates.data]);

  const preflightJob = useJobPolling(preflightJobId);
  useEffect(() => {
    if (preflightJob?.status === "completed" && preflightJobId && !estimate) {
      const params = new URLSearchParams();
      if (llmEnabled) {
        params.set("mode", mode);
        if (mode === "manual") {
          params.set("model", manualModel);
          params.set("effort", manualEffort);
        }
        if (budget) params.set("budget", budget);
        if (forceLlmAssist) params.set("force_assist", "true");
      }
      get<PreflightEstimate>(`/api/jobs/${preflightJobId}/preflight?${params}`)
        .then((e) => patch({ estimate: e }))
        .catch((e: Error) => setEstimateError(e.message));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preflightJob?.status, estimate]);

  const scopeValid =
    (period.trim() !== "" || periods.length > 0) &&
    (scope === "all" || (scope === "client" && client !== "") || (scope === "deal" && client !== "" && deal !== ""));
  const preflightDone = preflightJob?.status === "completed" && !!estimate;

  const runRequest = (dryRun: boolean) => ({
    scope,
    period,
    client: scope === "all" ? null : client,
    deal: scope === "deal" ? deal : null,
    // doc_type must be a valid enum; profile/free-text doc types ride in
    // doc_types and route through slot expansion on the backend.
    doc_type: ENUM_DOC_TYPES.has(docTypes[0]) ? docTypes[0] : docType,
    doc_types: docTypes,
    periods,
    restrict_to_client_sourced: restrictClientSourced,
    template,
    dry_run: dryRun,
    exclude: removedSlots.map(slotKeyParts),
    llm: {
      enabled: llmEnabled,
      mode: llmEnabled ? mode : null,
      model: llmEnabled && mode === "manual" ? manualModel : null,
      effort: llmEnabled && mode === "manual" ? manualEffort : null,
      budget_usd: llmEnabled && budget ? Number(budget) : null,
      force_llm_assist: llmEnabled && forceLlmAssist,
    },
  });

  const startPreflight = async () => {
    setEstimateError(null);
    setLaunchError(null);
    patch({ estimate: null, docsConfirmed: false });
    try {
      const r = await post<{ job: JobInfo }>("/api/jobs/run", runRequest(true));
      patch({ preflightJobId: r.job.id });
    } catch (e) {
      setEstimateError((e as Error).message);
    }
  };

  const launchRun = async () => {
    setLaunchError(null);
    try {
      const r = await post<{ job: JobInfo }>("/api/jobs/run", runRequest(dryRunOnly));
      patch({ runJobId: r.job.id, runId: r.job.run_id, step: 6 });
      navigate(`/jobs/${r.job.id}/progress`);
    } catch (e) {
      setLaunchError((e as Error).message);
    }
  };

  const preflightCoverage = useMemo(() => {
    const result = preflightJob?.result as RunResult | null;
    return result?.coverage ?? null;
  }, [preflightJob]);

  const auto = models.data?.llm.auto;

  return (
    <Panel className="space-y-4 max-w-6xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">New Run</h1>
        {(state.step > 0 || preflightJobId || runJobId) && (
          <Button kind="ghost" onClick={() => reset()} title="Clear every field and start over">
            Start over
          </Button>
        )}
      </div>
      <Stepper steps={STEPS} current={step} onStep={setStep} />

      <AnimatePresence mode="wait">
        <motion.div
          key={step}
          initial={{ opacity: 0, x: 8 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -8 }}
          transition={{ duration: 0.16, ease: [0.2, 0, 0, 1] }}
        >
          {step === 0 && (
            <Card>
              <CardHeader title="Scope" sub="what to locate and extract" />
              <div className="px-4 pb-4 space-y-4 max-w-3xl">
                <div className="grid grid-cols-2 gap-4">
                  <Field label="Scope">
                    <select className={inputCls} value={scope} onChange={(e) => patchScope({ scope: e.target.value as typeof scope })}>
                      <option value="deal">One deal</option>
                      <option value="client">All deals for a client</option>
                      <option value="all">Everything in the index</option>
                    </select>
                  </Field>
                  <div>
                    <DocTypeListBuilder value={docTypes} onChange={(v) => patchScope({ docTypes: v })} />
                  </div>
                </div>

                <div className="bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-2">
                  <Toggle
                    checked={restrictClientSourced}
                    onChange={(v) => patchScope({ restrictClientSourced: v })}
                    label="Restrict to client-sourced documents"
                  />
                  <p className="text-[12px] text-ink-500 mt-1">
                    {restrictClientSourced
                      ? "On: HL work product is rejected and HL report/analysis folders are penalized — only client-provided documents extract."
                      : "Off: no source restriction — client, HL, or any document can be used. The document type still ranks matches; nothing is excluded for being HL-sourced."}
                  </p>
                </div>

                {scope !== "all" && (
                  <Field label="Folder discovery">
                    <div className="flex gap-1 bg-surface border border-line rounded-[var(--hl-radius)] p-1 w-fit">
                      {(
                        [
                          ["browse", "Browse"],
                          ["search", "Search by name"],
                          ["llm", "LLM assist"],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          onClick={() => patch({ discoveryMode: key })}
                          className={`px-3 py-1 rounded-[calc(var(--hl-radius)-2px)] text-[12.5px] transition-colors ${
                            discoveryMode === key
                              ? "bg-[var(--hl-blue)] text-white font-medium"
                              : "text-ink-600 hover:bg-line/50"
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <p className="text-[11px] text-ink-400 mt-1">
                      {discoveryMode === "browse" && "Pick from the deal folders discovered during the index scan."}
                      {discoveryMode === "search" && "Type names — fuzzy search shows matching folders with their full path so you can confirm the right one."}
                      {discoveryMode === "llm" && "A local Claude Code session maps the client folder and proposes its deal folders (no API key — your claude login)."}
                    </p>
                  </Field>
                )}

                {/* ---- browse / llm: client dropdown ---- */}
                {scope !== "all" && discoveryMode !== "search" && (
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="Client">
                      <select
                        className={inputCls}
                        value={client}
                        onChange={(e) => {
                          patchScope({ client: e.target.value, deal: "" });
                          setLlmDiscoverJobId(null);
                        }}
                      >
                        <option value="">— select client —</option>
                        {(clients.data?.clients ?? []).map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                      <p className="text-[11px] text-ink-400 mt-1">
                        Only indexed clients appear here — missing one? Scan its folder in{" "}
                        <Link className="text-[var(--hl-blue)] underline" to="/settings">
                          Settings → Locations &amp; file index
                        </Link>
                        .
                      </p>
                    </Field>
                    {scope === "deal" && discoveryMode === "browse" && (
                      <Field label="Deal">
                        <select className={inputCls} value={deal} onChange={(e) => patchScope({ deal: e.target.value })} disabled={!client}>
                          <option value="">— select deal —</option>
                          {(deals.data?.deals ?? []).map((d) => {
                            const info = (deals.data?.deal_folders ?? []).find((f) => f.name === d);
                            return (
                              <option key={d} value={d}>
                                {d}
                                {info?.low_confidence ? " · low confidence" : ""}
                              </option>
                            );
                          })}
                        </select>
                        {client && (deals.data?.deals ?? []).length === 0 && !deals.loading && (
                          <p className="text-[11px] text-warn mt-1">
                            No deal folders discovered under this client — the folder may be incomplete. Try the LLM assist mode for a second opinion.
                          </p>
                        )}
                      </Field>
                    )}
                  </div>
                )}

                {/* ---- search mode: fuzzy client / deal / period ---- */}
                {scope !== "all" && discoveryMode === "search" && (
                  <div className="space-y-3">
                    <Field label={client ? `Client — selected: ${client}` : "Client (type to search)"}>
                      <input
                        className={inputCls}
                        value={clientQuery}
                        onChange={(e) => setClientQuery(e.target.value)}
                        placeholder="e.g. angelo, ares mgmt…"
                      />
                      {clientQuery.trim() && (
                        <div className="mt-1 border border-line rounded-[var(--hl-radius)] divide-y divide-line overflow-hidden">
                          {(clientMatches.data?.matches ?? []).map((m) => (
                            <button
                              key={m.client}
                              type="button"
                              onClick={() => {
                                patchScope({ client: m.client, deal: "" });
                                setClientQuery("");
                              }}
                              className="w-full text-left px-3 py-1.5 text-[12.5px] hover:bg-surface flex justify-between"
                            >
                              <span className="font-medium text-ink-800">{m.client}</span>
                              <span className="font-mono text-[11px] text-ink-400">match {m.score}</span>
                            </button>
                          ))}
                          {clientMatches.data && clientMatches.data.matches.length === 0 && (
                            <p className="px-3 py-1.5 text-[12px] text-ink-400">no indexed client folder matches</p>
                          )}
                        </div>
                      )}
                    </Field>
                    {scope === "deal" && client && (
                      <Field label={deal ? `Deal — selected: ${deal}` : "Deal (type to search)"}>
                        <input
                          className={inputCls}
                          value={dealQuery}
                          onChange={(e) => setDealQuery(e.target.value)}
                          placeholder="e.g. accell, linksquares…"
                        />
                        {dealQuery.trim() && (
                          <div className="mt-1 border border-line rounded-[var(--hl-radius)] divide-y divide-line overflow-hidden">
                            {(dealMatches.data?.matches ?? []).map((m) => (
                              <button
                                key={m.name}
                                type="button"
                                onClick={() => {
                                  patchScope({ deal: m.name });
                                  setDealQuery("");
                                }}
                                className="w-full text-left px-3 py-1.5 text-[12.5px] hover:bg-surface"
                              >
                                <div className="flex justify-between">
                                  <span className="font-medium text-ink-800">
                                    {m.name}
                                    {m.low_confidence && <span className="text-warn"> · low confidence</span>}
                                  </span>
                                  <span className="font-mono text-[11px] text-ink-400">
                                    {m.confidence !== null ? `conf ${confidencePct(m.confidence)} · ` : ""}match {m.score}
                                  </span>
                                </div>
                                {m.folder_paths.map((p) => (
                                  <div key={p} className="font-mono text-[11px] text-ink-400 truncate">
                                    {p}
                                  </div>
                                ))}
                              </button>
                            ))}
                            {dealMatches.data && dealMatches.data.matches.length === 0 && (
                              <p className="px-3 py-1.5 text-[12px] text-ink-400">no discovered deal folder matches</p>
                            )}
                          </div>
                        )}
                      </Field>
                    )}
                    {scope === "deal" && client && deal && periodQuery.trim() && periodMatches.data && (
                      <Field label="Period name resolver (optional helper)">
                        <input
                          className={inputCls}
                          value={periodQuery}
                          onChange={(e) => setPeriodQuery(e.target.value)}
                          placeholder="e.g. 3.31.25, Q1 2025, March 2025"
                        />
                        <div className="mt-1 space-y-1">
                          {periodMatches.data.parse_error && (
                            <p className="text-[12px] text-warn">{periodMatches.data.parse_error}</p>
                          )}
                          {periodMatches.data.resolved_label && (
                            <p className="text-[11.5px] text-ink-500">
                              interpreted as <span className="font-medium text-ink-700">{periodMatches.data.resolved_label}</span> (
                              {periodMatches.data.resolved_as_of}) — indexed periods for this deal, closest first:
                            </p>
                          )}
                          <div className="border border-line rounded-[var(--hl-radius)] divide-y divide-line overflow-hidden">
                            {periodMatches.data.matches.map((m) => (
                              <button
                                key={m.as_of_date}
                                type="button"
                                onClick={() => {
                                  patchScope({ period: m.as_of_date });
                                  setPeriodQuery("");
                                }}
                                className="w-full text-left px-3 py-1.5 text-[12.5px] hover:bg-surface flex justify-between"
                              >
                                <span>
                                  <span className="font-medium text-ink-800">{m.label}</span>{" "}
                                  <span className="text-ink-400">({m.as_of_date})</span>
                                  {m.exact && <span className="text-ok font-medium"> · exact match</span>}
                                </span>
                                <span className="font-mono text-[11px] text-ink-400 truncate max-w-[40%]">
                                  {m.date_folders.join(", ")}
                                </span>
                              </button>
                            ))}
                          </div>
                        </div>
                      </Field>
                    )}
                    {scope === "deal" && client && deal && !periodQuery.trim() && (
                      <input
                        className={`${inputCls} max-w-md`}
                        value={periodQuery}
                        onChange={(e) => setPeriodQuery(e.target.value)}
                        placeholder="period name resolver (optional): 3.31.25, Q1 2025…"
                      />
                    )}
                  </div>
                )}

                {/* ---- llm mode: discover-with-Claude panel ---- */}
                {scope === "deal" && discoveryMode === "llm" && client && (
                  <div className="space-y-2">
                    {deals.data?.last_llm_discovery && (
                      <div className="rounded border border-info/40 bg-info-soft px-3 py-2 text-[12px] text-ink-700">
                        You already have a saved deal discovery for <b>{client}</b>, searched with{" "}
                        <span className="font-mono">{deals.data.last_llm_discovery.model}</span> on{" "}
                        {new Date(deals.data.last_llm_discovery.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}{" "}
                        ({deals.data.last_llm_discovery.deals} deal folder
                        {deals.data.last_llm_discovery.deals === 1 ? "" : "s"}). Browse mode already shows it —
                        running again replaces it.
                      </div>
                    )}
                    <div className="flex items-end gap-3">
                      <div className="flex-1">
                        <ModelEffortPicker
                          models={models.data?.models}
                          model={llmDiscoverModel}
                          effort={llmDiscoverEffort}
                          onModel={(m) => patch({ llmDiscoverModel: m })}
                          onEffort={(e) => patch({ llmDiscoverEffort: e })}
                          modelLabel="Model (aliases float to the newest of each tier as claude updates)"
                          effortLabel="Effort"
                        />
                      </div>
                      <Button
                        kind="secondary"
                        onClick={startLlmDiscovery}
                        disabled={!client || ["queued", "running"].includes(llmDiscoverJob?.status ?? "")}
                      >
                        {["queued", "running"].includes(llmDiscoverJob?.status ?? "") ? "Discovering…" : "Discover deal folders"}
                      </Button>
                    </div>
                    {llmDiscoverError && <p className="text-[12px] text-err">{llmDiscoverError}</p>}
                    {llmDiscoveredDeals && (
                      <div className="border border-line rounded-[var(--hl-radius)] divide-y divide-line overflow-hidden">
                        {llmDiscoveredDeals.map((m) => (
                          <button
                            key={m.name}
                            type="button"
                            onClick={() => patchScope({ deal: m.name })}
                            className={`w-full text-left px-3 py-1.5 text-[12.5px] hover:bg-surface ${deal === m.name ? "bg-surface" : ""}`}
                          >
                            <div className="flex justify-between">
                              <span className="font-medium text-ink-800">
                                {m.name}
                                {deal === m.name && <span className="text-ok"> · selected</span>}
                              </span>
                              <span className="font-mono text-[11px] text-ink-400">
                                conf {confidencePct(m.confidence)} · {m.method}
                              </span>
                            </div>
                            {m.folder_paths.map((p) => (
                              <div key={p} className="font-mono text-[11px] text-ink-400 truncate">
                                {p}
                              </div>
                            ))}
                          </button>
                        ))}
                        {llmDiscoveredDeals.length === 0 && (
                          <p className="px-3 py-1.5 text-[12px] text-ink-400">
                            nothing discovered — the client folder may genuinely contain no deals
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {/* ---- confirmation: the selected deal's actual folder ---- */}
                {scope === "deal" && selectedDealInfo && (
                  <div className="bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-2 text-[12px] space-y-0.5">
                    <p className="text-ink-700">
                      <span className="font-semibold">{selectedDealInfo.name}</span> · confidence{" "}
                      <span className={selectedDealInfo.low_confidence ? "text-warn font-medium" : "text-ok font-medium"}>
                        {confidencePct(selectedDealInfo.confidence)}
                      </span>{" "}
                      · {selectedDealInfo.periods} period(s) · {selectedDealInfo.file_count} file(s)
                      {selectedDealInfo.llm_corroborated ? " · LLM-corroborated" : ""}
                    </p>
                    {selectedDealInfo.folder_paths.map((p) => (
                      <p key={p} className="font-mono text-[11px] text-ink-400 truncate">
                        {p}
                      </p>
                    ))}
                  </div>
                )}

                {/* ---- period: dropdown-first everywhere, free-text fallback ---- */}
                <div className="grid grid-cols-2 gap-4">
                  <Field label="Period (date folders that exist in the index)">
                    <select className={inputCls} value={period} onChange={(e) => patchScope({ period: e.target.value })}>
                      <option value="">— select period —</option>
                      {(discoveredPeriods.data?.periods ?? []).map((p) => (
                        <option key={p.as_of_date} value={p.as_of_date}>
                          {p.label} ({p.as_of_date})
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="…or type a period (e.g. Q1 2026, 2025-01-31)">
                    <input className={inputCls} value={period} onChange={(e) => patchScope({ period: e.target.value })} placeholder="Q1 2026" />
                  </Field>
                </div>
                {scope !== "all" && client && (
                  <PeriodMultiPicker
                    client={client}
                    deal={scope === "deal" ? deal : undefined}
                    value={periods}
                    onChange={(v) => patchScope({ periods: v })}
                  />
                )}
              </div>
            </Card>
          )}

          {step === 1 && (
            <Card>
              <CardHeader title="Template" sub="the master workbook COPY this run appends to" />
              <div className="px-4 pb-4 space-y-3 max-w-3xl">
                <label className="flex items-start gap-2 text-[13px]">
                  <input
                    type="radio"
                    checked={template === templates.data?.default_template}
                    onChange={() => patch({ template: templates.data?.default_template ?? null })}
                  />
                  <span>
                    <span className="font-medium text-ink-800">Reference template</span>
                    <span className="block text-[11.5px] text-ink-400 font-mono">{templates.data?.default_template}</span>
                  </span>
                </label>
                {(templates.data?.previous_outputs ?? []).map((o) => (
                  <label key={o.run_id} className="flex items-start gap-2 text-[13px]">
                    <input type="radio" checked={template === o.path} onChange={() => patch({ template: o.path })} />
                    <span>
                      <span className="font-medium text-ink-800">Previous output — {o.run_id}</span>
                      <span className="block text-[11.5px] text-ink-400 font-mono">{o.path}</span>
                    </span>
                  </label>
                ))}
                <Field label="…or a workbook path">
                  <input className={inputCls} value={template ?? ""} onChange={(e) => patch({ template: e.target.value })} />
                </Field>
                <div className="pt-2 border-t border-line">
                  <Toggle checked={dryRunOnly} onChange={(v) => patch({ dryRunOnly: v })} label="Dry run only (locate + verify; nothing written)" />
                </div>
              </div>
            </Card>
          )}

          {step === 2 && (
            <div className="space-y-4">
              <Card>
                <CardHeader title="AI / model settings" sub="Claude Code CLI fallback — local sessions, never an API key" />
                <div className="px-4 pb-4 space-y-4 max-w-3xl">
                  <Toggle checked={llmEnabled} onChange={(v) => patchAi({ llmEnabled: v })} label="LLM fallback enabled (escalated fields only)" />
                  {llmEnabled && (
                    <>
                      <div className="grid grid-cols-3 gap-4">
                        <Field label="Routing">
                          <select className={inputCls} value={mode} onChange={(e) => patchAi({ mode: e.target.value as "auto" | "manual" })}>
                            <option value="auto">AUTO — route by task</option>
                            <option value="manual">MANUAL — one model for everything</option>
                          </select>
                        </Field>
                        <Field label="Budget cap (USD, hard)">
                          <input className={inputCls} type="number" min="0" step="0.5" value={budget} onChange={(e) => patchAi({ budget: e.target.value })} />
                        </Field>
                      </div>
                      {mode === "auto" && auto && (
                        <div className="bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-2 text-[12.5px] text-ink-700 space-y-1">
                          <p className="font-semibold text-ink-800 text-[12px] uppercase tracking-wide">Router rules (config)</p>
                          <p>
                            extraction → <span className="font-mono">{auto.extraction_model}/{auto.extraction_effort}</span> · OCR-hostile memos →{" "}
                            <span className="font-mono">{auto.ocr_hostile_model}/{auto.ocr_hostile_effort}</span>
                          </p>
                          <p>
                            retry → <span className="font-mono">{auto.retry_model}/{auto.retry_effort}</span> (effort bump{" "}
                            <span className="font-mono">{auto.retry_effort_bump}</span> when the tier repeats) · fable only on explicit opt-in
                          </p>
                        </div>
                      )}
                      {mode === "manual" && (
                        <div className="max-w-md">
                          <ModelEffortPicker
                            models={models.data?.models}
                            model={manualModel}
                            effort={manualEffort}
                            onModel={(m) => patchAi({ manualModel: m })}
                            onEffort={(e) => patchAi({ manualEffort: e })}
                            modelLabel="Model (any alias, full ID, or custom)"
                            effortLabel="Effort"
                          />
                        </div>
                      )}
                      <div className="border-t border-line pt-3">
                        <Toggle
                          checked={forceLlmAssist}
                          onChange={(v) => patchAi({ forceLlmAssist: v })}
                          label="Force LLM assist — extract with the LLM, not just the algorithm"
                        />
                        <p className="text-[12px] text-ink-600 mt-1">
                          Escalates every empty extractable field to the model (not only low-confidence ones) and
                          bypasses the deterministic result cache. Use when the smart extractor misses a memo. Higher cost.
                        </p>
                      </div>
                    </>
                  )}
                </div>
              </Card>
              <Card>
                <CardHeader title="Model cost table" sub="editable pricing assumptions (USD per 1M tokens)" />
                <ModelPricingTable data={models.data} loading={models.loading} error={models.error} onRetry={models.reload} onSaved={models.reload} />
              </Card>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-4">
              <Card>
                <CardHeader
                  title="Preflight"
                  sub="server-side dry run + cost ESTIMATE — required before confirming documents"
                  right={
                    <Button kind="secondary" onClick={startPreflight} disabled={preflightJob?.status === "running" || preflightJob?.status === "queued"}>
                      {preflightJobId ? "Re-run preflight" : "Run preflight"}
                    </Button>
                  }
                />
                <div className="px-4 pb-4">
                  {!preflightJobId && <p className="text-[12.5px] text-ink-500">Preflight locates and verifies every document in scope without writing anything.</p>}
                  {preflightJob && ["queued", "running"].includes(preflightJob.status) && (
                    <div className="space-y-2 py-2">
                      <div className="skeleton h-4 w-1/2" />
                      <div className="skeleton h-4 w-2/3" />
                      <p className="text-[12px] text-ink-500">locating + verifying…</p>
                    </div>
                  )}
                  {preflightCoverage && (
                    <DataTable<CoverageEntry>
                      rows={preflightCoverage}
                      rowKey={(c) => `${c.client}|${c.deal}`}
                      columns={[
                        { key: "client", header: "Client", render: (c) => c.client, sortValue: (c) => c.client },
                        { key: "deal", header: "Deal", render: (c) => c.deal, sortValue: (c) => c.deal },
                        { key: "status", header: "Status", render: (c) => <StatusChip value={c.status} />, sortValue: (c) => c.status },
                        { key: "detail", header: "Detail", render: (c) => <span className="text-[12px] text-ink-600">{c.detail}</span> },
                      ]}
                    />
                  )}
                  {estimateError && <p className="text-[12px] text-err mt-2">{estimateError}</p>}
                </div>
              </Card>

              {estimate && (
                <Card>
                  <CardHeader title={`Cost estimate (${estimate.label})`} sub="page counts × configured model prices — actuals replace estimates in the ledger" />
                  <div className="px-4 pb-2 flex gap-8 text-[13px]">
                    <p>
                      first tier <span className="font-mono font-semibold">{fmtUsd(estimate.estimated_total_usd, 4)}</span>
                    </p>
                    <p>
                      worst case (full ladder) <span className="font-mono">{fmtUsd(estimate.estimated_worst_case_usd, 4)}</span>
                    </p>
                    <p>
                      budget <span className="font-mono">{fmtUsd(estimate.budget_usd)}</span>{" "}
                      {estimate.over_budget && <span className="text-err font-semibold">over budget — memos will be deferred</span>}
                    </p>
                  </div>
                  <DataTable
                    rows={estimate.memos}
                    rowKey={(m) => `${m.client}|${m.deal}`}
                    columns={[
                      { key: "deal", header: "Memo", render: (m) => `${m.client} / ${m.deal}` },
                      { key: "file", header: "File", render: (m) => <span className="text-[12px] text-ink-500">{m.file_name ?? "—"}</span> },
                      { key: "pages", header: "Pages", align: "right", render: (m) => m.page_count ?? "?" },
                      { key: "payload", header: "Payload pages", align: "right", render: (m) => m.payload_pages },
                      { key: "tier", header: "First tier", render: (m) => <span className="font-mono text-[12px]">{m.first_tier}</span> },
                      { key: "usd", header: "Est. USD", align: "right", render: (m) => <span className="font-mono text-[12px]">{m.first_tier_usd.toFixed(4)}</span>, sortValue: (m) => m.first_tier_usd },
                    ]}
                  />
                </Card>
              )}
            </div>
          )}

          {step === 4 && (
            <ConfirmDocuments
              preflightJobId={preflightJobId}
              preflightReady={preflightDone}
              period={period}
              docType={docType}
              removedSlots={removedSlots}
              docsConfirmed={docsConfirmed}
              patch={patch}
            />
          )}

          {step === 5 && (
            <Card>
              <CardHeader title="Launch" sub="final review before the run starts" />
              <div className="px-4 pb-4 space-y-3 text-[13px] text-ink-700">
                <div className="grid grid-cols-2 gap-x-8 gap-y-1 max-w-3xl">
                  <p>scope <span className="font-medium text-ink-900">{scope}</span>{scope !== "all" && client ? ` · ${client}` : ""}{scope === "deal" && deal ? ` / ${deal}` : ""}</p>
                  <p>period{periods.length > 1 ? "s" : ""} <span className="font-medium text-ink-900">{periods.length > 0 ? `${periods.length} selected` : period}</span></p>
                  <p>document type{docTypes.length > 1 ? "s" : ""} <span className="font-mono">{docTypes.length > 0 ? docTypes.join(", ") : docType.replace(/_/g, " ")}</span></p>
                  <p>removed slots <span className="font-mono">{removedSlots.length}</span></p>
                  <p>LLM fallback <span className="font-medium">{llmEnabled ? `${mode}${mode === "manual" ? ` · ${manualModel}/${manualEffort}` : ""}${forceLlmAssist ? " · force assist (LLM primary)" : ""}` : "disabled"}</span></p>
                  {estimate && <p>est. first-tier cost <span className="font-mono">{fmtUsd(estimate.estimated_total_usd, 4)}</span></p>}
                </div>
                <p className="text-[12px] text-ink-500 border-t border-line pt-3">
                  Copies the chosen template and appends one Index row per memo-asset. The run opens in the live progress view, then the review queue.
                </p>
                {!docsConfirmed && <p className="text-[12px] text-warn">Confirm the document selection (step 5) before launching.</p>}
                {!preflightDone && <p className="text-[12px] text-warn">Run preflight (step 4) before launching.</p>}
                {launchError && <p className="text-[12px] text-err">{launchError}</p>}
              </div>
            </Card>
          )}

          {step === 6 && (
            <ReviewStep runJobId={runJobId} runId={runId} onReset={() => { reset(); }} />
          )}
        </motion.div>
      </AnimatePresence>

      <div className="flex justify-between pt-2">
        <Button kind="ghost" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>
          ← Back
        </Button>
        {step < 5 ? (
          <Button
            kind="primary"
            onClick={() => setStep(step + 1)}
            disabled={
              (step === 0 && !scopeValid) ||
              (step === 3 && !preflightDone) ||
              (step === 4 && !docsConfirmed)
            }
            title={
              step === 3 && !preflightDone
                ? "Run preflight first — Confirm documents unlocks once the estimate is shown"
                : step === 4 && !docsConfirmed
                  ? "Acknowledge the document selection to continue"
                  : undefined
            }
          >
            Next →
          </Button>
        ) : step === 5 ? (
          <Button
            kind="primary"
            onClick={launchRun}
            disabled={!preflightDone || !docsConfirmed}
            title={!preflightDone ? "Run preflight first" : !docsConfirmed ? "Confirm the document selection first" : undefined}
          >
            {dryRunOnly ? "Launch dry run" : "Launch run"}
          </Button>
        ) : (
          <span />
        )}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Multi Search — many firms in one run (additive; never touches the single flow)
// ---------------------------------------------------------------------------

const newFirm = (client: string): FirmEntry => ({
  client,
  deals: [],
  period: "",
  docTypes: [],
  llmAssist: false,
  enhancedPeriodCheck: false,
  dealSearchModel: "sonnet",
  addedFolders: [],
  removedDeals: [],
});

const firmToApi = (f: FirmEntry): MultiSearchFirm => ({
  client: f.client,
  deals: f.deals,
  period: f.period,
  doc_types: f.docTypes,
  llm_assist: f.llmAssist,
  enhanced_period_check: f.enhancedPeriodCheck,
  deal_search_model: f.llmAssist ? f.dealSearchModel : null,
  added_folders: f.addedFolders,
  removed_deals: f.removedDeals,
});

function MultiRun() {
  const navigate = useNavigate();
  const { state, patch, reset } = useWizard();
  const {
    multiFirms, multiTemplate, multiDryRunOnly, multiConfirmed, multiSelection,
    llmEnabled, mode, manualModel, manualEffort, budget, forceLlmAssist,
  } = state;

  const [clientInput, setClientInput] = useState("");
  const [browse, setBrowse] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);

  const models = useLoad<ModelsResponse>("/api/models");
  const clientsStatus = useLoad<{ folders: { name: string; path: string; files: number }[] }>(
    browse ? "/api/index/clients-status" : null,
    [browse],
  );
  const templates = useLoad<{ default_template: string; previous_outputs: { run_id: string; path: string }[] }>("/api/templates");

  useEffect(() => {
    if (templates.data && multiTemplate === null) {
      patch({ multiTemplate: templates.data.previous_outputs[0]?.path ?? templates.data.default_template });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templates.data]);

  const existingClients = useMemo(() => new Set(multiFirms.map((f) => f.client)), [multiFirms]);

  const addFirm = (client: string) => {
    const name = client.trim();
    if (!name || existingClients.has(name)) return;
    patch((prev) => ({ multiFirms: [...prev.multiFirms, newFirm(name)], multiConfirmed: false, multiSelection: null }));
  };

  const updateFirm = (index: number, next: FirmEntry) => {
    patch((prev) => ({
      multiFirms: prev.multiFirms.map((f, i) => (i === index ? next : f)),
      multiConfirmed: false,
      multiSelection: null,
    }));
  };

  const removeFirm = (index: number) => {
    patch((prev) => ({
      multiFirms: prev.multiFirms.filter((_, i) => i !== index),
      multiConfirmed: false,
      multiSelection: null,
    }));
  };

  // Resolve comma-separated tokens to indexed clients (fuzzy). Tokens that
  // resolve unambiguously are added; the rest stay in the box for the analyst.
  const resolveTokens = async () => {
    const tokens = clientInput.split(",").map((t) => t.trim()).filter(Boolean);
    if (tokens.length === 0) return;
    setResolving(true);
    setAddError(null);
    const unresolved: string[] = [];
    try {
      for (const token of tokens) {
        const r = await get<{ matches: { client: string; score: number }[] }>(
          `/api/index/search/clients?q=${encodeURIComponent(token)}`,
        ).catch(() => null);
        const top = r?.matches?.[0];
        // Exact-ish (single match, or first match clearly wins) -> add.
        if (top && (r!.matches.length === 1 || top.client.toLowerCase() === token.toLowerCase())) {
          addFirm(top.client);
        } else if (top) {
          // ambiguous — add the best match but keep the token visible as a hint
          addFirm(top.client);
        } else {
          unresolved.push(token);
        }
      }
      setClientInput(unresolved.join(", "));
      if (unresolved.length) setAddError(`No indexed client matched: ${unresolved.join(", ")}`);
    } finally {
      setResolving(false);
    }
  };

  const llmOptions = () => ({
    enabled: llmEnabled,
    mode: llmEnabled ? mode : null,
    model: llmEnabled && mode === "manual" ? manualModel : null,
    effort: llmEnabled && mode === "manual" ? manualEffort : null,
    budget_usd: llmEnabled && budget ? Number(budget) : null,
    force_llm_assist: llmEnabled && forceLlmAssist,
  });

  const runPreview = async () => {
    setPreviewing(true);
    setPreviewError(null);
    patch({ multiConfirmed: false });
    try {
      const r = await post<MultiSelectionResponse>("/api/multi-search/selection", {
        firms: multiFirms.map(firmToApi),
      });
      patch({ multiSelection: r });
    } catch (e) {
      setPreviewError((e as Error).message);
    } finally {
      setPreviewing(false);
    }
  };

  const launch = async () => {
    setLaunchError(null);
    const body: MultiSearchRunRequest = {
      firms: multiFirms.map(firmToApi),
      template: multiTemplate,
      dry_run: multiDryRunOnly,
      llm: llmOptions(),
    };
    try {
      const r = await post<{ job: JobInfo }>("/api/multi-search/run", body);
      patch({ multiRunJobId: r.job.id, multiRunId: r.job.run_id });
      navigate(`/jobs/${r.job.id}/progress`);
    } catch (e) {
      const err = e as { status?: number; message: string };
      setLaunchError(
        err.status === 409
          ? "A pipeline run is already active. Wait for it to finish (or cancel it) before launching a multi-firm run."
          : err.message,
      );
    }
  };

  const firmsValid = multiFirms.length > 0 && multiFirms.every((f) => f.period.trim() !== "");

  return (
    <Panel className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">Multi Search</h1>
        {(multiFirms.length > 0 || multiSelection) && (
          <Button kind="ghost" onClick={() => reset()} title="Clear every firm and start over">
            Start over
          </Button>
        )}
      </div>

      {/* firm entry ---------------------------------------------------- */}
      <Card>
        <CardHeader
          title="Firms"
          sub="add one or more managers — type names (comma-separated) or browse the indexed folders"
          right={
            <Button kind="secondary" onClick={() => setBrowse(true)}>
              Browse firms
            </Button>
          }
        />
        <div className="px-4 pb-4 space-y-3">
          <div className="flex items-end gap-2">
            <Field label="Add firms (comma-separated; fuzzy-resolved against the index)">
              <input
                className={inputCls}
                value={clientInput}
                onChange={(e) => setClientInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    resolveTokens();
                  }
                }}
                placeholder="Angelo Gordon, Ares, Apollo"
              />
            </Field>
            <Button kind="secondary" onClick={resolveTokens} disabled={resolving || !clientInput.trim()}>
              {resolving ? "Resolving…" : "Add"}
            </Button>
          </div>
          {addError && <p className="text-[12px] text-warn">{addError}</p>}
          {multiFirms.length === 0 && (
            <p className="text-[12.5px] text-ink-400">
              No firms yet. Add at least one to configure its deals, period and document types.
            </p>
          )}
        </div>
      </Card>

      {/* per-firm regions --------------------------------------------- */}
      {multiFirms.map((f, i) => (
        <FirmRegion
          key={f.client}
          firm={f}
          models={models.data}
          onChange={(next) => updateFirm(i, next)}
          onRemove={() => removeFirm(i)}
        />
      ))}

      {/* shared run-level settings ------------------------------------ */}
      {multiFirms.length > 0 && (
        <Card>
          <CardHeader title="Run settings" sub="shared across every firm in this run" />
          <div className="px-4 pb-4 space-y-3 max-w-3xl">
            <Field label="Template (master workbook COPY this run appends to)">
              <select
                className={inputCls}
                value={multiTemplate ?? ""}
                onChange={(e) => patch({ multiTemplate: e.target.value })}
              >
                {templates.data && (
                  <option value={templates.data.default_template}>Reference template — {templates.data.default_template}</option>
                )}
                {(templates.data?.previous_outputs ?? []).map((o) => (
                  <option key={o.run_id} value={o.path}>
                    Previous output — {o.run_id}
                  </option>
                ))}
              </select>
            </Field>
            <Toggle
              checked={multiDryRunOnly}
              onChange={(v) => patch({ multiDryRunOnly: v })}
              label="Dry run only (locate + verify; nothing written)"
            />
            <div className="pt-2 border-t border-line space-y-3">
              <Toggle checked={llmEnabled} onChange={(v) => patch({ llmEnabled: v })} label="LLM fallback enabled (escalated fields only)" />
              {llmEnabled && (
                <div className="grid grid-cols-3 gap-4">
                  <Field label="Routing">
                    <select className={inputCls} value={mode} onChange={(e) => patch({ mode: e.target.value as "auto" | "manual" })}>
                      <option value="auto">AUTO — route by task</option>
                      <option value="manual">MANUAL — one model for everything</option>
                    </select>
                  </Field>
                  <Field label="Budget cap (USD, hard)">
                    <input className={inputCls} type="number" min="0" step="0.5" value={budget} onChange={(e) => patch({ budget: e.target.value })} />
                  </Field>
                  {mode === "manual" && (
                    <div className="col-span-2">
                      <ModelEffortPicker
                        models={models.data?.models}
                        model={manualModel}
                        effort={manualEffort}
                        onModel={(m) => patch({ manualModel: m })}
                        onEffort={(e) => patch({ manualEffort: e })}
                        modelLabel="Model (any alias, full ID, or custom)"
                        effortLabel="Effort"
                      />
                    </div>
                  )}
                </div>
              )}
              {llmEnabled && (
                <div className="pt-1">
                  <Toggle
                    checked={forceLlmAssist}
                    onChange={(v) => patch({ forceLlmAssist: v })}
                    label="Force LLM assist — extract with the LLM, not just the algorithm"
                  />
                  <p className="text-[12px] text-ink-600 mt-1">
                    Escalates every empty extractable field to the model and bypasses the deterministic result cache. Higher cost.
                  </p>
                </div>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* preview / confirm -------------------------------------------- */}
      {multiFirms.length > 0 && (
        <Card>
          <CardHeader
            title="Preview document selection"
            sub="locate + verify every firm's documents without writing anything (corrections persist only at launch)"
            right={
              <Button kind="secondary" onClick={runPreview} disabled={previewing || !firmsValid}>
                {previewing ? "Locating…" : multiSelection ? "Re-run preview" : "Preview"}
              </Button>
            }
          />
          <div className="px-4 pb-4 space-y-4">
            {!firmsValid && <p className="text-[12px] text-warn">Every firm needs a period before previewing.</p>}
            {previewError && <p className="text-[12px] text-err">{previewError}</p>}
            {multiSelection?.firms.map((fs) => (
              <MultiFirmPreview key={fs.client} firm={fs} />
            ))}
            {multiSelection && (
              <div className="border-t border-line pt-3">
                <Toggle
                  checked={multiConfirmed}
                  onChange={(v) => patch({ multiConfirmed: v })}
                  label="These look right — ready to launch"
                />
              </div>
            )}
          </div>
        </Card>
      )}

      {launchError && (
        <Card className="px-4 py-3">
          <p className="text-[12.5px] text-err">{launchError}</p>
        </Card>
      )}

      <div className="flex justify-end pt-1">
        <Button
          kind="primary"
          onClick={launch}
          disabled={!firmsValid || !multiSelection || !multiConfirmed}
          title={
            !firmsValid
              ? "Add firms and give each a period"
              : !multiSelection
                ? "Run the preview first"
                : !multiConfirmed
                  ? "Confirm the selection first"
                  : undefined
          }
        >
          {multiDryRunOnly ? "Launch multi dry run" : "Launch multi run"}
        </Button>
      </div>

      {browse && (
        <FirmBrowseModal
          folders={clientsStatus.data?.folders ?? []}
          loading={clientsStatus.loading}
          existing={existingClients}
          onClose={() => setBrowse(false)}
          onConfirm={(names) => {
            names.forEach(addFirm);
            setBrowse(false);
          }}
        />
      )}
    </Panel>
  );
}

/* One firm's slice of the multi preview: discovered deal folders + a slots
   table (misfiled badge on flagged slots). */
function MultiFirmPreview({ firm }: { firm: MultiFirmSelection }) {
  return (
    <div className="border border-line rounded-[var(--hl-radius)]">
      <div className="px-3 py-2 border-b border-line flex items-center justify-between">
        <p className="font-semibold text-ink-900 text-[13.5px]">{firm.client}</p>
        <p className="text-[11.5px] text-ink-500">
          {firm.period} · {firm.found} found · {firm.slots.length} slot(s) · {firm.deal_folders_preview.length} deal folder(s)
        </p>
      </div>
      {firm.deal_folders_preview.length > 0 && (
        <div className="px-3 py-2 flex flex-wrap gap-1.5 border-b border-line">
          {firm.deal_folders_preview.map((d) => (
            <span
              key={d.name}
              className={`px-2 py-0.5 rounded text-[11px] border ${
                d.low_confidence ? "border-warn text-warn" : "border-line-strong text-ink-600"
              }`}
              title={d.folder_paths.join("\n")}
            >
              {d.name} · {confidencePct(d.confidence)}
              {d.llm_corroborated ? " · LLM" : ""}
            </span>
          ))}
        </div>
      )}
      <DataTable<MultiSlot>
        rows={firm.slots}
        rowKey={(s) => s.slot_key}
        maxHeight="40vh"
        emptyTitle="No documents located for this firm"
        columns={[
          {
            key: "deal",
            header: "Deal",
            sortValue: (s) => s.deal,
            render: (s) => (
              <div>
                <p className="font-medium text-ink-800">{s.deal}</p>
                <p className="text-[11px] text-ink-400 font-mono">{s.doc_type_slug}</p>
              </div>
            ),
          },
          {
            key: "file",
            header: "File",
            sortValue: (s) => s.file_name ?? "",
            render: (s) => <span className="text-[12px]">{s.file_name ?? <span className="text-ink-400">— none —</span>}</span>,
          },
          {
            key: "period",
            header: "Period",
            render: (s) => (
              <span className="flex flex-col items-start gap-0.5 text-[12px]">
                <span>{s.predicted_period || "—"}</span>
                {s.misfiled && (
                  <span className="text-warn text-[10.5px] font-semibold" title={`detected: ${s.detected_period ?? "?"} / ${s.detected_as_of ?? "?"}`}>
                    misfiled · detected {s.detected_period ?? s.detected_as_of ?? "?"}
                  </span>
                )}
              </span>
            ),
          },
          { key: "pages", header: "Pages", align: "right", sortValue: (s) => s.page_count ?? -1, render: (s) => s.page_count ?? "—" },
          {
            key: "status",
            header: "Status",
            sortValue: (s) => s.status,
            render: (s) => (
              <span className="flex flex-col items-start gap-0.5">
                <StatusChip value={s.status} />
                {s.override_in_effect && <span className="text-[10px] text-info font-semibold">override</span>}
              </span>
            ),
          },
          {
            key: "conf",
            header: "Confidence",
            align: "right",
            sortValue: (s) => s.confidence ?? -1,
            render: (s) => <span className="font-mono text-[12px]">{confidencePct(s.confidence)}</span>,
          },
        ]}
      />
    </div>
  );
}

/* Multi-select modal over the indexed top-level folders. */
function FirmBrowseModal({
  folders,
  loading,
  existing,
  onClose,
  onConfirm,
}: {
  folders: { name: string; path: string; files: number }[];
  loading: boolean;
  existing: Set<string>;
  onClose: () => void;
  onConfirm: (names: string[]) => void;
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const visible = folders.filter((f) => f.name.toLowerCase().includes(filter.trim().toLowerCase()));

  const toggle = (name: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-[var(--hl-radius)] shadow-lift w-[560px] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-line flex items-center justify-between">
          <p className="text-[13.5px] font-semibold text-ink-900">Select firms</p>
          <button className="text-ink-400 hover:text-ink-700 text-[14px]" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="px-4 py-2 border-b border-line">
          <input className={inputCls} value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter firms…" />
        </div>
        <div className="flex-1 overflow-y-auto min-h-[200px]">
          {loading && <p className="px-4 py-6 text-[12.5px] text-ink-400">loading…</p>}
          {!loading && visible.length === 0 && <p className="px-4 py-6 text-[12.5px] text-ink-400">no indexed firm folders</p>}
          {visible.map((f) => {
            const already = existing.has(f.name);
            return (
              <label
                key={f.path}
                className={`w-full text-left px-4 py-1.5 text-[12.5px] flex items-center gap-2 ${
                  already ? "opacity-50" : "hover:bg-surface cursor-pointer"
                }`}
              >
                <input type="checkbox" disabled={already} checked={already || picked.has(f.name)} onChange={() => toggle(f.name)} />
                <span className="truncate text-ink-800">{f.name}</span>
                <span className="ml-auto text-[11px] text-ink-400">{f.files} file(s)</span>
              </label>
            );
          })}
        </div>
        <div className="px-4 py-3 border-t border-line flex items-center justify-between">
          <p className="text-[11.5px] text-ink-500">{picked.size} selected</p>
          <div className="flex gap-2">
            <Button kind="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button kind="primary" disabled={picked.size === 0} onClick={() => onConfirm([...picked])}>
              Add {picked.size > 0 ? `${picked.size} firm(s)` : "firms"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 5 — Confirm documents (curate the locator's auto-selection)
// ---------------------------------------------------------------------------

function ConfirmDocuments({
  preflightJobId,
  preflightReady,
  period,
  docType,
  removedSlots,
  docsConfirmed,
  patch,
}: {
  preflightJobId: string | null;
  preflightReady: boolean;
  period: string;
  docType: string;
  removedSlots: string[];
  docsConfirmed: boolean;
  patch: (p: Partial<WizardState>) => void;
}) {
  const selection = useLoad<SelectionResponse>(
    preflightJobId && preflightReady ? `/api/jobs/${preflightJobId}/selection` : null,
    [preflightJobId, preflightReady],
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [picker, setPicker] = useState<{ slot: SelectionSlot } | null>(null);
  const [verifyResult, setVerifyResult] = useState<VerifyFileResponse | null>(null);
  const [pendingPath, setPendingPath] = useState<string | null>(null);
  const [pendingSlot, setPendingSlot] = useState<SelectionSlot | null>(null);

  const removed = useMemo(() => new Set(removedSlots), [removedSlots]);
  const slots = selection.data?.slots ?? [];
  const selectedSlot = slots.find((s) => s.slot_key === selectedKey) ?? null;

  const toggleRemove = (slot: SelectionSlot) => {
    const next = new Set(removed);
    if (next.has(slot.slot_key)) next.delete(slot.slot_key);
    else next.add(slot.slot_key);
    patch({ removedSlots: [...next], docsConfirmed: false });
  };

  const recordOverride = async (slot: SelectionSlot, filePath: string) => {
    setActionError(null);
    try {
      await post("/api/locator/override", {
        client: slot.client, deal: slot.deal, period, doc_type: docType,
        file_path: filePath, note: "selected in New Run → Confirm documents",
      });
      patch({ docsConfirmed: false });
      setVerifyResult(null);
      setPendingPath(null);
      setPendingSlot(null);
      setPicker(null);
      selection.reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const verifyFile = async (slot: SelectionSlot, filePath: string) => {
    setActionError(null);
    setVerifyResult(null);
    setPendingPath(filePath);
    setPendingSlot(slot);
    try {
      const v = await post<VerifyFileResponse>("/api/locator/verify-file", {
        client: slot.client, deal: slot.deal, period, doc_type: docType, file_path: filePath,
      });
      setVerifyResult(v);
    } catch (e) {
      setActionError((e as Error).message);
      setPendingPath(null);
      setPendingSlot(null);
    }
  };

  const activeCount = slots.filter((s) => !removed.has(s.slot_key)).length;
  const foundActive = slots.filter((s) => !removed.has(s.slot_key) && s.status === "FOUND").length;
  const slotCount = selection.data?.slot_count ?? slots.length;
  const isMultiSlot = slotCount > slots.length;

  return (
    <div className="space-y-4">
      {isMultiSlot && (
        <div className="rounded border border-info/40 bg-info-soft px-3 py-2 text-[12.5px] text-ink-700">
          This run executes <b>{slotCount} slots</b> — {(selection.data?.doc_types ?? []).length} document
          type(s) × {(selection.data?.periods ?? []).length} period(s) × {slots.length} deal(s). The table
          previews the first type/period per deal; all slots run at launch. Removing a deal drops it from every
          slot.
        </div>
      )}
      <Card>
        <CardHeader
          title="Confirm documents"
          sub="exactly the files the locator auto-selected — swap, remove or add before launch"
          right={
            <Button
              kind="secondary"
              disabled={slots.length === 0}
              onClick={() => setPicker({ slot: selectedSlot ?? slots[0] })}
            >
              + Add a missed file
            </Button>
          }
        />
        <div className="px-1 pb-1">
          <DataTable<SelectionSlot>
            rows={selection.data ? slots : null}
            loading={selection.loading}
            error={selection.error}
            onRetry={selection.reload}
            emptyTitle="Nothing in scope"
            emptyHint="Go back and adjust the scope or re-run preflight."
            rowKey={(s) => s.slot_key}
            onRowClick={(s) => setSelectedKey(s.slot_key)}
            selectedKey={selectedKey}
            maxHeight="48vh"
            filterable
            columns={[
              {
                key: "deal",
                header: "Company / Deal",
                sortValue: (s) => s.deal,
                filterValue: (s) => `${s.client} ${s.deal}`,
                render: (s) => (
                  <div className={removed.has(s.slot_key) ? "opacity-50 line-through" : ""}>
                    <p className="font-medium text-ink-800">{s.deal}</p>
                    <p className="text-[11px] text-ink-400">{s.client}</p>
                  </div>
                ),
              },
              {
                key: "file",
                header: "File",
                sortValue: (s) => s.file_name ?? "",
                filterValue: (s) => s.file_name ?? "",
                render: (s) => <span className="text-[12px]">{s.file_name ?? <span className="text-ink-400">— none —</span>}</span>,
              },
              {
                key: "location",
                header: "Location",
                filterValue: (s) => s.file_path ?? "",
                render: (s) => <span className="font-mono text-[11px] text-ink-400 break-all">{s.file_path ?? "—"}</span>,
              },
              {
                key: "modified",
                header: "Last modified",
                sortValue: (s) => s.last_modified ?? "",
                render: (s) => <span className="text-[11.5px] text-ink-500">{s.last_modified ? s.last_modified.slice(0, 10) : "—"}</span>,
              },
              {
                key: "period",
                header: "Predicted period",
                filterValue: (s) => s.predicted_period,
                render: (s) => <span className="text-[12px]">{s.predicted_period || "—"}</span>,
              },
              {
                key: "doc",
                header: "Doc type",
                filterValue: (s) => s.doc_class,
                render: (s) => <span className="text-[11.5px] text-ink-600">{s.doc_class ? s.doc_class.replace(/_/g, " ").toLowerCase() : "—"}</span>,
              },
              { key: "pages", header: "Pages", align: "right", sortValue: (s) => s.page_count ?? -1, render: (s) => s.page_count ?? "—" },
              {
                key: "status",
                header: "Status",
                sortValue: (s) => s.status,
                filterValue: (s) => s.status,
                render: (s) => (
                  <span className="flex flex-col items-start gap-0.5">
                    {removed.has(s.slot_key) ? <StatusChip value="DEFERRED" /> : <StatusChip value={s.status} />}
                    {s.override_in_effect && <span className="text-[10px] text-info font-semibold">override</span>}
                  </span>
                ),
              },
              {
                key: "conf",
                header: "Confidence",
                align: "right",
                sortValue: (s) => s.confidence ?? -1,
                render: (s) => <span className="font-mono text-[12px]">{confidencePct(s.confidence)}</span>,
              },
              {
                key: "actions",
                header: "",
                render: (s) => (
                  <span className="flex gap-1.5 justify-end whitespace-nowrap">
                    <Button kind="ghost" onClick={() => setSelectedKey(s.slot_key)} title="View candidates / swap">
                      ⇄ swap
                    </Button>
                    <Button kind="ghost" onClick={() => toggleRemove(s)} title={removed.has(s.slot_key) ? "Restore to the run" : "Exclude from the run"}>
                      {removed.has(s.slot_key) ? "restore" : "remove"}
                    </Button>
                  </span>
                ),
              },
            ]}
          />
        </div>
        {actionError && <p className="px-4 pb-3 text-[12px] text-err">{actionError}</p>}
      </Card>

      {/* selected-slot detail: the candidate list (swap) + add-from-share */}
      {selectedSlot && (
        <Card>
          <CardHeader
            title={`${selectedSlot.deal} — candidates`}
            sub={`${selectedSlot.client} · ${selectedSlot.predicted_period || period} · the locator ranked these; pick a different one to record a learned override`}
            right={
              <Button kind="secondary" onClick={() => setPicker({ slot: selectedSlot })}>
                Replace with a file from the share
              </Button>
            }
          />
          <div className="px-4 pb-4 space-y-2">
            {selectedSlot.candidates.length === 0 && (
              <p className="text-[12.5px] text-ink-500">
                No ranked candidates for this slot ({selectedSlot.status}). Use “Replace with a file from the share” to point at the right document.
              </p>
            )}
            {selectedSlot.candidates.map((c) => (
              <div
                key={c.file_path}
                className={`border rounded-[var(--hl-radius)] px-3 py-2 flex items-start justify-between gap-3 ${
                  c.is_selected ? "border-ok bg-ok-soft" : "border-line"
                }`}
              >
                <div className="min-w-0">
                  <p className="text-[12.5px] font-medium text-ink-800">
                    {c.file_name}
                    {c.is_selected && <span className="text-ok text-[11px] font-semibold"> · auto-selected</span>}
                  </p>
                  <p className="font-mono text-[11px] text-ink-400 break-all">{c.file_path}</p>
                  <p className="text-[11px] text-ink-500 mt-0.5">
                    score <span className="font-mono">{c.score.toFixed(1)}</span> · verify{" "}
                    <span className={c.verify_status === "REJECTED" ? "text-err" : c.verify_status === "VERIFIED" ? "text-ok" : "text-ink-500"}>
                      {c.verify_status || "—"}
                    </span>
                    {c.doc_class ? ` · ${c.doc_class.toLowerCase().replace(/_/g, " ")}` : ""}
                  </p>
                </div>
                <Button kind="primary" disabled={c.is_selected} onClick={() => recordOverride(selectedSlot, c.file_path)}>
                  {c.is_selected ? "Current" : "Use this one"}
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* footer: acknowledge selection */}
      <Card className="px-4 py-3 flex items-center justify-between">
        <p className="text-[12.5px] text-ink-600">
          {activeCount} document slot(s) in scope · {foundActive} resolved
          {removed.size > 0 && <span className="text-warn"> · {removed.size} removed</span>}
        </p>
        <Toggle
          checked={docsConfirmed}
          onChange={(v) => patch({ docsConfirmed: v })}
          label="These look right — proceed to launch"
        />
      </Card>

      {/* add-a-missed-file / swap-to-arbitrary picker + peek-verify preview */}
      {picker && (
        <FolderPicker
          title={`Pick a file for ${picker.slot.deal}`}
          initial={picker.slot.file_path ?? ""}
          pickFiles
          onClose={() => setPicker(null)}
          onSelect={(path) => {
            const slot = picker.slot;
            setPicker(null);
            verifyFile(slot, path);
          }}
        />
      )}
      {verifyResult && pendingPath && pendingSlot && (
        <Card className="px-4 py-3 space-y-2">
          <CardHeader
            title="Peek-verify preview"
            sub="the analyst-chosen file is checked against this slot before it becomes a learned override"
          />
          <div className="px-1 text-[12.5px] space-y-1">
            <p className="font-mono text-[11px] text-ink-500 break-all">{pendingPath}</p>
            <p>
              verdict{" "}
              <span className={verifyResult.would_pass ? "text-ok font-semibold" : "text-err font-semibold"}>
                {verifyResult.status}
              </span>{" "}
              · {verifyResult.doc_class.toLowerCase().replace(/_/g, " ")} · conf {confidencePct(verifyResult.confidence)}
            </p>
            <p className="text-ink-600">{verifyResult.reason}</p>
            {!verifyResult.indexed && (
              <p className="text-warn">
                This file is not in the index — overrides need an indexed target. Scan its folder in Settings first.
              </p>
            )}
            {!verifyResult.would_pass && (
              <p className="text-warn">⚠ The run-time peek-verifier would reject this file; record it only if you are sure.</p>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              kind="primary"
              disabled={!verifyResult.indexed}
              onClick={() => recordOverride(pendingSlot, pendingPath)}
            >
              Record this file for {pendingSlot.deal}
            </Button>
            <Button kind="ghost" onClick={() => { setVerifyResult(null); setPendingPath(null); setPendingSlot(null); }}>
              Cancel
            </Button>
          </div>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 7 — Review (terminal): the launched run, then into the review queue
// ---------------------------------------------------------------------------

function ReviewStep({ runJobId, runId, onReset }: { runJobId: string | null; runId: string | null; onReset: () => void }) {
  const navigate = useNavigate();
  const job = useJobPolling(runJobId);
  const status = job?.status ?? "running";
  const done = !["queued", "running", "cancelling"].includes(status);
  const resolvedRunId = job?.run_id ?? runId;

  return (
    <Card>
      <CardHeader title="Review" sub="the run, then its extracted fields" />
      <div className="px-4 pb-4 space-y-3">
        {!runJobId ? (
          <p className="text-[13px] text-ink-500">Launch a run from the previous step — it opens here when complete.</p>
        ) : (
          <>
            <div className="flex items-center gap-3 text-[13px]">
              <StatusChip value={status} />
              <span className="font-mono text-[12px] text-ink-500">{resolvedRunId}</span>
            </div>
            <p className="text-[12.5px] text-ink-600">
              {done
                ? "Run finished. Review every flagged and low-confidence field, with the source page beside each value."
                : "Run in progress — the live pipeline view is open. You can keep working; this opens in the review queue when complete."}
            </p>
            <div className="flex gap-2">
              <Button kind="secondary" onClick={() => navigate(`/jobs/${runJobId}/progress`)}>
                Live progress →
              </Button>
              <Button kind="primary" disabled={!done || !resolvedRunId} onClick={() => resolvedRunId && navigate(`/review/${resolvedRunId}`)}>
                Open review queue →
              </Button>
              {done && resolvedRunId && (
                <Button kind="secondary" onClick={() => navigate(`/output/${resolvedRunId}`)}>
                  Output →
                </Button>
              )}
              <Button kind="ghost" onClick={onReset}>
                Start a new run
              </Button>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
