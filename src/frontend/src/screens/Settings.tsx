import { useEffect, useMemo, useState } from "react";
import { DataTable } from "../components/DataTable";
import { FolderPicker } from "../components/FolderPicker";
import { ModelPricingTable } from "../components/ModelPricingTable";
import { Button, Card, CardHeader, Field, InfoDot, Panel, StatusChip, Toggle, inputCls } from "../components/ui";
import { HLSpinner } from "../components/branding";
import {
  ClaudeSource,
  ClaudeSourcesResponse,
  ClientsStatus,
  ConfigResponse,
  DoctorCheck,
  IndexDiscoverResponse,
  IndexStatus,
  JobInfo,
  ModelsResponse,
  RawConfigResponse,
  ScanProgress,
  ScanStats,
  SetupItem,
  del,
  get,
  post,
  put,
} from "../lib/api";
import { fmtAgo, fmtDuration, useJobEvents, useLoad } from "../lib/hooks";
import { useScanJob } from "../lib/scanJob";
import { useStickyState } from "../lib/uiState";

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

const ACTIVE_JOB = ["queued", "running", "cancelling"];

interface OverrideRow {
  client: string;
  deal: string;
  as_of_date: string;
  doc_type: string;
  file_path: string;
  note: string | null;
  created_at: string;
}

export default function Settings() {
  const config = useLoad<ConfigResponse>("/api/config");
  const models = useLoad<ModelsResponse>("/api/models");
  const index = useLoad<IndexStatus>("/api/index/status");
  const clientsStatus = useLoad<ClientsStatus>("/api/index/clients-status");
  const raw = useLoad<RawConfigResponse>("/api/config/raw");
  const overrides = useLoad<{ overrides: OverrideRow[] }>("/api/locator/overrides");
  const setup = useLoad<{ items: SetupItem[]; all_ok: boolean; install_command: string | null; can_auto_install: boolean }>(
    "/api/setup/status?include_claude=false",
  );
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const deleteOverride = async (o: OverrideRow) => {
    setOverrideError(null);
    try {
      const params = new URLSearchParams({
        client: o.client, deal: o.deal, as_of_date: o.as_of_date, doc_type: o.doc_type,
      });
      await del(`/api/locator/overrides?${params}`);
      overrides.reload();
    } catch (e) {
      setOverrideError((e as Error).message);
    }
  };

  const [draft, setDraft] = useStickyState<Record<string, unknown>>("settings.draft", {});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Lifted above the router so the scan status survives tab switches and a
  // page reload (see lib/scanJob.tsx).
  const { scanJobId, setScanJobId } = useScanJob();
  const [scanError, setScanError] = useState<string | null>(null);
  const { events: scanEvents, job: scanJob } = useJobEvents(scanJobId);
  const scanRunning = scanJob !== null && ACTIVE_JOB.includes(scanJob.status);
  const scanStats =
    scanJob && ["completed", "cancelled"].includes(scanJob.status) && scanJob.result
      ? (scanJob.result as unknown as ScanStats)
      : null;
  const scanPaused = scanJob?.status === "cancelled" || scanStats?.stopped_early === true;
  const scanProgress = useMemo(() => {
    for (let i = scanEvents.length - 1; i >= 0; i--) {
      if (scanEvents[i].type === "scan_progress") return scanEvents[i].payload as unknown as ScanProgress;
    }
    return null;
  }, [scanEvents]);

  const [selectedFolders, setSelectedFolders] = useStickyState<Set<string>>(
    "settings.selectedFolders",
    new Set(),
  );
  const [folderFilter, setFolderFilter] = useStickyState("settings.folderFilter", "");
  const [quickRescan, setQuickRescan] = useStickyState("settings.quickRescan", false);

  // Phase C: Single | Multi scan-target switch. Single is the existing scan UI
  // (one root / one client / full pv_root) — unchanged. Multi is a first-class
  // multi-firm selection that starts ONE scan with {clients: [...selected]} and,
  // optionally, fires the existing per-firm deal-discovery LLM assist afterward.
  const [scanMode, setScanMode] = useStickyState<"single" | "multi">("settings.scanMode", "single");
  const [multiScanFirms, setMultiScanFirms] = useStickyState<string[]>("settings.multiScanFirms", []);
  // Deal-discovery mode for the scan's end-of-scan refresh (single + multi):
  // Smart Scan = deterministic heuristics only; LLM-Assisted Scan = the local
  // claude -p deal-discovery pass with the chosen model + effort. The scan job
  // performs the refresh in-process, so there is no separate post-scan queue.
  const [scanDealMode, setScanDealMode] = useStickyState<"smart" | "llm">("settings.scanDealMode", "smart");
  const [scanLlmModel, setScanLlmModel] = useStickyState("settings.scanLlmModel", "sonnet");
  const [scanLlmEffort, setScanLlmEffort] = useStickyState("settings.scanLlmEffort", "low");

  const [rawDraft, setRawDraft] = useStickyState<string | null>("settings.rawDraft", null);
  const [rawSaving, setRawSaving] = useState(false);
  const [rawError, setRawError] = useState<string | null>(null);
  const [rawSaved, setRawSaved] = useState(false);

  // which dotted config path the folder picker is currently browsing for
  const [picking, setPicking] = useState<{ field: string; title: string; initial: string; pickFiles?: boolean } | null>(null);

  const [discover, setDiscover] = useStickyState<IndexDiscoverResponse | null>("settings.discover", null);
  const [discovering, setDiscovering] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  const [doctor, setDoctor] = useStickyState<{ checks: DoctorCheck[]; all_ok: boolean } | null>(
    "settings.doctor",
    null,
  );
  const [doctorBusy, setDoctorBusy] = useState(false);
  const [updateMsg, setUpdateMsg] = useState<string | null>(null);

  const [sources, setSources] = useStickyState<ClaudeSourcesResponse | null>("settings.sources", null);
  const [detecting, setDetecting] = useState(false);
  const [sourcesError, setSourcesError] = useState<string | null>(null);

  useEffect(() => {
    if (scanJob && !ACTIVE_JOB.includes(scanJob.status)) {
      index.reload();
      clientsStatus.reload();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanJob?.status]);

  const value = (path: string, fallback: unknown): unknown => (path in draft ? draft[path] : fallback);
  const setValue = (path: string, v: unknown) => {
    setDraft((d) => ({ ...d, [path]: v }));
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await put("/api/config", { values: draft });
      setSaved(true);
      setDraft({});
      // Refresh everything the change could affect so the page reflects the
      // new config immediately — no tab-switch/remount needed.
      config.reload();
      models.reload();
      index.reload();
      clientsStatus.reload();
      overrides.reload();
      setup.reload();
      raw.reload();
      setDiscover(null);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const startScan = async (body: Record<string, unknown>) => {
    setScanError(null);
    setScanJobId(null);
    try {
      const r = await post<{ job: JobInfo }>("/api/index/scan", body);
      setScanJobId(r.job.id);
    } catch (e) {
      setScanError((e as Error).message);
    }
  };

  const pauseScan = async () => {
    if (!scanJobId) return;
    try {
      await post(`/api/jobs/${scanJobId}/cancel`);
    } catch (e) {
      setScanError((e as Error).message);
    }
  };

  const toggleFolder = (name: string) => {
    setSelectedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const toggleMultiFirm = (name: string) => {
    setMultiScanFirms((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
  };

  // The scan body's deal-discovery options: Smart Scan sends nothing extra
  // (deterministic heuristics); LLM-Assisted Scan opts the end-of-scan refresh
  // into the local claude -p pass with the chosen model + effort.
  const dealDiscoveryBody = (): Record<string, unknown> =>
    scanDealMode === "llm"
      ? { use_llm: true, llm_model: scanLlmModel, llm_effort: scanLlmEffort }
      : {};

  // Multi mode: one scan over the selected firms; the scan job refreshes deal
  // discovery for every firm at the end (LLM-assisted when chosen).
  const startMultiScan = async () => {
    if (multiScanFirms.length === 0) return;
    await startScan({ clients: [...multiScanFirms], quick: quickRescan, ...dealDiscoveryBody() });
  };

  // Smart Scan vs LLM-Assisted Scan selector (+ model/effort), shared by the
  // single- and multi-firm scan panels.
  const scanDealModePanel = (
    <div className="px-3 py-2.5 border-b border-line flex items-center gap-3 flex-wrap">
      <span className="text-[11px] uppercase tracking-wide text-ink-400">Deal discovery</span>
      <div className="inline-flex rounded-[var(--hl-radius)] border border-line overflow-hidden">
        {(["smart", "llm"] as const).map((m) => (
          <button
            key={m}
            type="button"
            disabled={scanRunning}
            onClick={() => setScanDealMode(m)}
            className={`px-3 py-1 text-[12px] ${
              scanDealMode === m ? "bg-navy text-white" : "text-ink-600 hover:bg-surface"
            } disabled:opacity-50`}
          >
            {m === "smart" ? "Smart Scan" : "LLM-Assisted"}
          </button>
        ))}
      </div>
      {scanDealMode === "llm" ? (
        <>
          <label className="flex items-center gap-1.5 text-[11.5px] text-ink-600">
            model
            <select
              className={`${inputCls} w-44`}
              value={scanLlmModel}
              disabled={scanRunning}
              onChange={(e) => setScanLlmModel(e.target.value)}
            >
              {(models.data?.models ?? [{ alias: "sonnet", display_name: "default" }]).map((m) => (
                <option key={m.alias} value={m.alias}>
                  {m.alias}
                  {"display_name" in m && m.display_name ? ` — ${m.display_name}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[11.5px] text-ink-600">
            effort
            <select
              className={`${inputCls} w-28`}
              value={scanLlmEffort}
              disabled={scanRunning}
              onChange={(e) => setScanLlmEffort(e.target.value)}
            >
              {["low", "medium", "high", "xhigh", "max"].map((e) => (
                <option key={e} value={e}>{e}</option>
              ))}
            </select>
          </label>
          <span className="text-[11px] text-ink-400">
            local <code className="text-[10.5px]">claude -p</code> corroborates the heuristics — no SDK, no API key
          </span>
        </>
      ) : (
        <span className="text-[11px] text-ink-400">
          deterministic heuristics — fast, fully offline; best for clean layouts
        </span>
      )}
      <InfoDot
        align="right"
        text={
          <>
            <span className="font-semibold text-ink-800">Smart Scan</span> finds deal folders with
            deterministic rules only — fast and fully local.{" "}
            <span className="font-semibold text-ink-800">LLM-Assisted</span> additionally runs one local
            Claude Code call per firm over a folder <em>inventory</em> (paths + counts + sample file names,
            never contents) to corroborate and gap-fill dense or unusual layouts. Heuristics always stay
            primary — the LLM never removes a discovered deal.
          </>
        }
      />
    </div>
  );

  const saveRaw = async () => {
    if (rawDraft === null) return;
    setRawSaving(true);
    setRawError(null);
    try {
      await put("/api/config/raw", { text: rawDraft });
      setRawSaved(true);
      setRawDraft(null);
      raw.reload();
      config.reload();
      models.reload();
      index.reload();
    } catch (e) {
      setRawError((e as Error).message);
    } finally {
      setRawSaving(false);
    }
  };

  const runDoctor = async () => {
    setDoctorBusy(true);
    setDoctor(null);
    try {
      setDoctor(await get<{ checks: DoctorCheck[]; all_ok: boolean }>("/api/doctor"));
    } catch (e) {
      setDoctor({ checks: [{ check: "doctor", ok: false, detail: (e as Error).message }], all_ok: false });
    } finally {
      setDoctorBusy(false);
    }
  };

  const detectIndexes = async () => {
    setDiscovering(true);
    setDiscoverError(null);
    try {
      setDiscover(await get<IndexDiscoverResponse>("/api/index/discover"));
    } catch (e) {
      setDiscoverError((e as Error).message);
    } finally {
      setDiscovering(false);
    }
  };

  const detectSources = async () => {
    setDetecting(true);
    setSourcesError(null);
    try {
      setSources(await get<ClaudeSourcesResponse>("/api/claude/sources"));
    } catch (e) {
      setSourcesError((e as Error).message);
    } finally {
      setDetecting(false);
    }
  };

  const triggerUpdate = async () => {
    setUpdateMsg("running `claude update`…");
    try {
      await post("/api/setup/claude-update");
      setUpdateMsg("update job started — re-run doctor in a moment to confirm the version");
    } catch (e) {
      setUpdateMsg((e as Error).message);
    }
  };

  const c = config.data;
  const auto = c?.llm.auto as Record<string, string> | undefined;
  const activeProvider = c ? String(value("llm.provider", c.llm.provider ?? "claude")) : "claude";
  const dirty = Object.keys(draft).length > 0;
  const rawDirty = rawDraft !== null && rawDraft !== raw.data?.text;

  return (
    <Panel className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">Settings</h1>
        <div className="flex items-center gap-3">
          {saveError && <span className="text-[12px] text-err">{saveError}</span>}
          {saved && !dirty && <span className="text-[12px] text-ok">saved to config.yaml</span>}
          <Button kind="primary" onClick={save} disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </div>

      {c && (
        <>
          <Card>
            <CardHeader title="Locations & file index" sub="where documents are read from (read-only, never written) and where outputs land" />
            <div className="px-4 pb-4 space-y-4">
              <div className="grid grid-cols-2 gap-4 max-w-3xl">
                <Field label="PV share root (pv_root)">
                  <div className="flex gap-2">
                    <input className={`${inputCls} flex-1`} value={String(value("pv_root", c.pv_root))} onChange={(e) => setValue("pv_root", e.target.value)} />
                    <Button
                      kind="secondary"
                      onClick={() => setPicking({ field: "pv_root", title: "Choose the PV share root", initial: String(value("pv_root", c.pv_root)) })}
                    >
                      Browse…
                    </Button>
                  </div>
                </Field>
                <Field label="Output directory">
                  <div className="flex gap-2">
                    <input className={`${inputCls} flex-1`} value={String(value("output_dir", c.output_dir))} onChange={(e) => setValue("output_dir", e.target.value)} />
                    <Button
                      kind="secondary"
                      onClick={() => setPicking({ field: "output_dir", title: "Choose the output directory", initial: String(value("output_dir", c.output_dir)) })}
                    >
                      Browse…
                    </Button>
                  </div>
                </Field>
              </div>

              <div className="max-w-3xl space-y-2">
                <Field label="Index database (db_path)">
                  <div className="flex gap-2">
                    <input
                      className={`${inputCls} flex-1 font-mono text-[12px]`}
                      value={String(value("db_path", c.db_path))}
                      onChange={(e) => setValue("db_path", e.target.value)}
                    />
                    <Button
                      kind="secondary"
                      onClick={() => setPicking({ field: "db_path", title: "Choose the index database file", initial: String(value("db_path", c.db_path)), pickFiles: true })}
                    >
                      Browse…
                    </Button>
                    <Button kind="ghost" onClick={detectIndexes} disabled={discovering}>
                      {discovering ? "Detecting…" : "Detect existing"}
                    </Button>
                  </div>
                </Field>
                <p className="text-[11px] text-ink-400">
                  The index is a single SQLite file (never committed to git, never shared on push). Point every machine
                  at the same file to share one index. Changing this then <span className="font-medium">Save changes</span>{" "}
                  switches which index the app uses.
                </p>
                {index.data?.relocation && (
                  <p className="text-[12px] text-warn break-all">
                    Index auto-moved to local disk: {index.data.relocation.detail}
                    <span className="block text-[11px] text-ink-500 mt-0.5">
                      The configured path was on a network/cross-boundary location that can't host the database. The path
                      above is now saved in config.yaml.
                    </span>
                  </p>
                )}
                {(index.data?.db_error || clientsStatus.data?.db_error) && (
                  <p className="text-[12px] text-err break-all">
                    {index.data?.db_error || clientsStatus.data?.db_error}
                    <span className="block text-[11px] text-ink-500 mt-0.5">
                      A DB reached over <code className="text-[11px]">\\wsl.localhost</code> or a network share can't use
                      SQLite's WAL mode. Use a path on this machine's local disk, or run the GUI from WSL and point at the
                      WSL path directly.
                    </span>
                  </p>
                )}
                {discoverError && <p className="text-[12px] text-err break-all">{discoverError}</p>}
                {discover && (
                  <div className="border border-line rounded-[var(--hl-radius)] divide-y divide-line">
                    {discover.found.length === 0 && (
                      <p className="px-3 py-2 text-[12px] text-ink-500">
                        no index databases found in {discover.scanned_dirs.join(", ")} — use Browse… to locate one.
                      </p>
                    )}
                    {discover.found.map((f) => (
                      <div key={f.path} className="px-3 py-2 flex items-center gap-3">
                        <span className="flex-1 min-w-0">
                          <span className="block font-mono text-[11.5px] text-ink-800 break-all">{f.path}</span>
                          <span className="block text-[11px] text-ink-500">
                            {(f.size_bytes / 1_048_576).toFixed(1)} MB ·{" "}
                            {f.readable
                              ? `${(f.files ?? 0).toLocaleString()} files / ${f.clients ?? 0} clients`
                              : `unreadable: ${f.detail}`}
                            {f.is_current && <span className="text-ok"> · current</span>}
                          </span>
                        </span>
                        <Button
                          kind={f.is_current ? "ghost" : "secondary"}
                          disabled={f.is_current || String(value("db_path", c.db_path)) === f.path}
                          onClick={() => setValue("db_path", f.path)}
                        >
                          {f.is_current ? "in use" : "Use this"}
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="text-[12.5px] text-ink-600 space-y-1">
                {index.loading && !index.data && <div className="skeleton h-4 w-1/2" />}
                {index.data &&
                  (index.data.ready ? (
                    <p>
                      <span className="font-medium text-ink-800">{index.data.files.toLocaleString()}</span> files indexed across{" "}
                      <span className="font-medium text-ink-800">{index.data.clients.toLocaleString()}</span> clients
                    </p>
                  ) : (
                    <p className="text-warn">
                      No file index yet — pick the client folders you need below and scan just those. The New Run dropdowns
                      fill in as folders get indexed.
                    </p>
                  ))}
                {index.data && !index.data.pv_root_exists && (
                  <p className="text-err">
                    pv_root is not reachable from this machine: <code className="text-[11px]">{index.data.pv_root}</code> — fix the
                    path above (on WSL, mount the share first)
                  </p>
                )}
                {dirty && <p className="text-[11px] text-ink-400">unsaved changes — scans use the saved pv_root</p>}
              </div>

              <div className="flex items-center gap-2 max-w-3xl">
                <span className="text-[11px] uppercase tracking-wide text-ink-400">Scan target</span>
                <div className="inline-flex rounded-[var(--hl-radius)] border border-line overflow-hidden">
                  {(["single", "multi"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      disabled={scanRunning}
                      onClick={() => setScanMode(m)}
                      className={`px-3 py-1 text-[12px] capitalize ${
                        scanMode === m ? "bg-[var(--hl-blue)] text-white" : "text-ink-600 hover:bg-surface"
                      } disabled:opacity-50`}
                    >
                      {m === "single" ? "Single" : "Multi-firm"}
                    </button>
                  ))}
                </div>
                <span className="text-[11px] text-ink-400">
                  {scanMode === "single"
                    ? "scan one client folder, or the entire share"
                    : "scan several firms in one job, optional per-firm deal-discovery afterward"}
                </span>
              </div>

              {clientsStatus.loading && !clientsStatus.data && (
                <div className="border border-line rounded-[var(--hl-radius)] max-w-3xl px-3 py-3 space-y-2">
                  <p className="text-[12px] text-ink-500">listing client folders and their indexed counts…</p>
                  <div className="skeleton h-4 w-2/3" />
                  <div className="skeleton h-4 w-1/2" />
                  <div className="skeleton h-4 w-3/5" />
                </div>
              )}
              {clientsStatus.data && scanMode === "single" && (
                <div className="border border-line rounded-[var(--hl-radius)] max-w-3xl">
                  <div className="px-3 py-2 border-b border-line flex items-center gap-2 flex-wrap">
                    <input
                      className={`${inputCls} w-52`}
                      placeholder="filter folders…"
                      value={folderFilter}
                      onChange={(e) => setFolderFilter(e.target.value)}
                    />
                    <span className="text-[11px] text-ink-400">
                      {clientsStatus.data.folders.length} client folders — scan only what you need
                    </span>
                    <span className="flex-1" />
                    <label className="flex items-center gap-1.5 text-[11.5px] text-ink-600 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={quickRescan}
                        disabled={scanRunning}
                        onChange={(e) => setQuickRescan(e.target.checked)}
                      />
                      Quick rescan
                      <InfoDot
                        align="right"
                        text={
                          <>
                            <span className="font-semibold text-ink-800">Quick rescan</span> skips re-listing
                            folders whose contents haven't changed since the last scan (judged by the folder's
                            timestamp). Much faster on the share, and it still picks up newly-added files,
                            periods, and deals.
                            <span className="block mt-1.5 text-ink-500">
                              Trade-off: it won't notice a file that was <em>overwritten in place</em> under the
                              same name. Leave it unchecked for a normal full scan to catch those.
                            </span>
                          </>
                        }
                      />
                    </label>
                    <span className="inline-flex items-center gap-1">
                      <Button
                        kind="primary"
                        disabled={scanRunning || selectedFolders.size === 0 || dirty}
                        onClick={() => startScan({ clients: [...selectedFolders], quick: quickRescan, ...dealDiscoveryBody() })}
                      >
                        {scanRunning ? "Scanning…" : `Scan selected (${selectedFolders.size})`}
                      </Button>
                      <InfoDot
                        align="right"
                        text={
                          <>
                            Index just the client folders you've ticked — the recommended path: fast and
                            targeted. Already-indexed files are skipped, so re-running only picks up what's new.
                          </>
                        }
                      />
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <Button
                        kind="ghost"
                        disabled={scanRunning || dirty}
                        onClick={() => {
                          if (window.confirm("Scan the ENTIRE share? On the full PV tree this can take hours — scanning selected client folders is usually all you need.")) {
                            startScan({ quick: quickRescan, ...dealDiscoveryBody() });
                          }
                        }}
                      >
                        Scan everything
                      </Button>
                      <InfoDot
                        align="right"
                        text={
                          <>
                            Walk the entire PV share from the top. Correct but slow on the full tree — it can
                            take a long while. Prefer scanning the specific client folders you need.
                          </>
                        }
                      />
                    </span>
                  </div>
                  {scanDealModePanel}
                  <div className="max-h-60 overflow-y-auto">
                    {clientsStatus.data.folders
                      .filter((f) => f.name.toLowerCase().includes(folderFilter.toLowerCase()))
                      .map((f) => (
                        <label
                          key={f.path}
                          className="flex items-center gap-2.5 px-3 py-1 text-[12.5px] text-ink-800 hover:bg-surface cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={selectedFolders.has(f.name)}
                            onChange={() => toggleFolder(f.name)}
                            disabled={scanRunning}
                          />
                          <span className="truncate flex-1">{f.name}</span>
                          <span className={`text-[11px] shrink-0 ${f.files > 0 ? "text-ok" : "text-ink-400"}`}>
                            {f.files > 0 ? `${f.files.toLocaleString()} files indexed` : "not indexed"}
                          </span>
                          <span className="text-[11px] shrink-0 text-ink-400" title={f.last_scan ?? "never scanned"}>
                            {f.last_scan ? `· last scan ${fmtAgo(f.last_scan)}` : "· never scanned"}
                          </span>
                        </label>
                      ))}
                  </div>
                </div>
              )}

              {clientsStatus.data && scanMode === "multi" && (
                <div className="border border-line rounded-[var(--hl-radius)] max-w-3xl">
                  <div className="px-3 py-2 border-b border-line flex items-center gap-2 flex-wrap">
                    <input
                      className={`${inputCls} w-52`}
                      placeholder="filter firms…"
                      value={folderFilter}
                      onChange={(e) => setFolderFilter(e.target.value)}
                    />
                    <span className="text-[11px] text-ink-400">
                      tick the firms to scan together — one job, {clientsStatus.data.folders.length} client folders
                    </span>
                    <span className="flex-1" />
                    <Button
                      kind="ghost"
                      disabled={scanRunning || multiScanFirms.length === 0}
                      onClick={() => setMultiScanFirms([])}
                    >
                      Clear
                    </Button>
                    <label className="flex items-center gap-1.5 text-[11.5px] text-ink-600 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={quickRescan}
                        disabled={scanRunning}
                        onChange={(e) => setQuickRescan(e.target.checked)}
                      />
                      Quick rescan
                    </label>
                    <span className="inline-flex items-center gap-1">
                      <Button
                        kind="primary"
                        disabled={scanRunning || multiScanFirms.length === 0 || dirty}
                        onClick={startMultiScan}
                      >
                        {scanRunning ? "Scanning…" : `Scan ${multiScanFirms.length} firm${multiScanFirms.length === 1 ? "" : "s"}`}
                      </Button>
                      <InfoDot
                        align="right"
                        text={
                          <>
                            Index every ticked firm in a single scan job (body{" "}
                            <code className="text-[11px]">{"{clients: [...]}"}</code>). Already-indexed files are
                            skipped; the scan refreshes deal discovery for each firm at the end.
                          </>
                        }
                      />
                    </span>
                  </div>
                  {scanDealModePanel}
                  <div className="max-h-60 overflow-y-auto">
                    {clientsStatus.data.folders
                      .filter((f) => f.name.toLowerCase().includes(folderFilter.toLowerCase()))
                      .map((f) => (
                        <label
                          key={f.path}
                          className="flex items-center gap-2.5 px-3 py-1 text-[12.5px] text-ink-800 hover:bg-surface cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={multiScanFirms.includes(f.name)}
                            onChange={() => toggleMultiFirm(f.name)}
                            disabled={scanRunning}
                          />
                          <span className="truncate flex-1">{f.name}</span>
                          <span className={`text-[11px] shrink-0 ${f.files > 0 ? "text-ok" : "text-ink-400"}`}>
                            {f.files > 0 ? `${f.files.toLocaleString()} files indexed` : "not indexed"}
                          </span>
                          <span className="text-[11px] shrink-0 text-ink-400" title={f.last_scan ?? "never scanned"}>
                            {f.last_scan ? `· last scan ${fmtAgo(f.last_scan)}` : "· never scanned"}
                          </span>
                        </label>
                      ))}
                  </div>
                </div>
              )}

              {clientsStatus.error && (
                <p className="text-[12px] text-ink-400">client folder list unavailable: {clientsStatus.error}</p>
              )}

              {scanRunning && scanProgress && (() => {
                // The final "discovering deal folders…" event carries only the
                // running counters (no root/root_index/prev_total), so every
                // field is read defensively — a partial event must never crash.
                const elapsed = scanProgress.elapsed_seconds ?? 0;
                const filesSeen = scanProgress.files_seen ?? 0;
                const prevTotal = scanProgress.prev_total ?? 0;
                const rootsTotal = scanProgress.roots_total ?? 1;
                const rootIndex = scanProgress.root_index ?? 1;
                const rate = elapsed > 0.5 ? filesSeen / elapsed : 0;
                const rescan = prevTotal > 0;
                const pct = rescan ? Math.min(99, (filesSeen / prevTotal) * 100) : null;
                const eta = rescan && rate > 0 && prevTotal > filesSeen
                  ? (prevTotal - filesSeen) / rate
                  : null;
                const folderName = scanProgress.root
                  ? scanProgress.root.split(/[\\/]/).filter(Boolean).pop() ?? scanProgress.root
                  : "deal folders";
                return (
                  <div className="space-y-1.5 max-w-3xl">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-[12.5px] text-ink-700">
                        Scanning <span className="font-medium text-ink-900">{folderName}</span>
                        {rootsTotal > 1 && ` (${rootIndex}/${rootsTotal})`} ·{" "}
                        <span className="font-mono">{filesSeen.toLocaleString()}</span> files seen
                        {rate > 0 && <> · <span className="font-mono">{Math.round(rate).toLocaleString()}</span>/s</>} · elapsed{" "}
                        {fmtDuration(elapsed)}
                        {eta !== null && <> · ~{fmtDuration(eta)} remaining <span className="text-ink-400">(rescan estimate)</span></>}
                      </p>
                      <Button kind="secondary" onClick={pauseScan} disabled={scanJob?.status === "cancelling"}>
                        {scanJob?.status === "cancelling" ? "Pausing…" : "Pause scan"}
                      </Button>
                    </div>
                    <p className="text-[11px] text-ink-400">
                      Pausing keeps everything indexed so far — you can run extractions on it now and rescan later to
                      continue where it stopped (already-indexed files are skipped).
                    </p>
                    {pct !== null && (
                      <div className="h-1.5 bg-surface border border-line rounded overflow-hidden">
                        <div className="h-full bg-[var(--hl-blue)] transition-all duration-500" style={{ width: `${pct}%` }} />
                      </div>
                    )}
                    <p className="font-mono text-[10.5px] text-ink-400 truncate" title={scanProgress.dir}>
                      {scanProgress.dir}
                    </p>
                  </div>
                );
              })()}
              {scanRunning && !scanProgress && (
                <div className="flex items-center gap-2.5 text-[12.5px] text-ink-500">
                  <HLSpinner size={26} />
                  starting scan…
                </div>
              )}
              {scanStats && !scanRunning && !scanPaused && (
                <p className="text-[12.5px] text-ok">
                  {scanStats.quick ? "quick rescan" : "scan"} done in {fmtDuration(scanStats.elapsed_seconds)} — seen{" "}
                  {scanStats.files_seen.toLocaleString()}, added {scanStats.added.toLocaleString()}, updated{" "}
                  {scanStats.updated.toLocaleString()}, removed {scanStats.removed.toLocaleString()}, errors {scanStats.errors}
                </p>
              )}
              {scanPaused && !scanRunning && (
                <p className="text-[12.5px] text-warn">
                  scan paused{scanStats ? <> after {fmtDuration(scanStats.elapsed_seconds)} — seen {scanStats.files_seen.toLocaleString()}, added {scanStats.added.toLocaleString()}, updated {scanStats.updated.toLocaleString()}</> : null}.
                  Everything scanned so far is indexed and usable now; re-run the same scan to continue where it left off.
                </p>
              )}
              {scanJob?.status === "failed" && <p className="text-[12.5px] text-err">scan failed: {scanJob.error}</p>}
              {scanError && <p className="text-[12.5px] text-err">{scanError}</p>}
            </div>
          </Card>

          <Card>
            <CardHeader title="LLM provider CLI" sub="local command execution only — no hosted API call from Python" />
            <div className="px-4 pb-4 space-y-4 max-w-3xl">
              {activeProvider === "claude" && (() => {
                const curCmd = String(value("claude_code.command", c.claude_code.command));
                const curArgs = (value("claude_code.command_args", c.claude_code.command_args) as string[]) ?? [];
                const argsEqual = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);
                const isSel = (s: ClaudeSource) => s.command === curCmd && argsEqual(s.command_args ?? [], curArgs);
                const pick = (s: ClaudeSource) => {
                  setValue("claude_code.command", s.command);
                  setValue("claude_code.command_args", s.command_args ?? []);
                };
                return (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <p className="text-[12.5px] text-ink-700">
                        Where to run <code className="text-[11px]">claude</code> — pick the install to use for every LLM call,
                        then <span className="font-medium">Save changes</span>.
                      </p>
                      <span className="flex-1" />
                      <Button kind="secondary" onClick={detectSources} disabled={detecting}>
                        {detecting ? "Detecting…" : "Detect installs"}
                      </Button>
                    </div>
                    {sourcesError && <p className="text-[12px] text-err">{sourcesError}</p>}
                    {sources && (
                      <div className="space-y-1.5">
                        {sources.sources.map((s) => (
                          <label
                            key={s.id}
                            className={`flex items-start gap-2.5 px-3 py-2 border rounded-[var(--hl-radius)] cursor-pointer ${
                              isSel(s) ? "border-[var(--hl-blue)] bg-surface" : "border-line hover:bg-surface"
                            }`}
                          >
                            <input type="radio" className="mt-1" checked={isSel(s)} onChange={() => pick(s)} />
                            <span className="flex-1 min-w-0">
                              <span className="flex items-center gap-2">
                                <span className="text-[13px] font-medium text-ink-900">{s.label}</span>
                                <StatusChip value={s.available ? "completed" : "failed"} />
                                {s.version && <span className="text-[11px] text-ink-500">{s.version}</span>}
                              </span>
                              <span className="block font-mono text-[11px] text-ink-500 break-all">
                                {[s.command, ...(s.command_args ?? [])].join(" ")}
                              </span>
                              {s.detail && <span className="block text-[11px] text-ink-400 break-all">{s.detail}</span>}
                            </span>
                          </label>
                        ))}
                        <p className="text-[11px] text-ink-400">
                          “failed” means that claude install didn’t answer <code className="text-[11px]">--version</code> — it
                          may not be installed there, or (for WSL) not authenticated yet.
                        </p>
                        <details className="text-[11px] text-ink-400">
                          <summary className="cursor-pointer">Diagnostics (what this GUI process sees)</summary>
                          <div className="font-mono mt-1 space-y-0.5 break-all">
                            <div>platform: {sources.diagnostics.sys_platform} ({sources.platform})</div>
                            <div>python: {sources.diagnostics.python}</div>
                            <div>which claude: {sources.diagnostics.which_claude ?? "—"}</div>
                            <div>which wsl: {sources.diagnostics.which_wsl ?? "—"}</div>
                          </div>
                        </details>
                      </div>
                    )}
                    {!sources && !detecting && (
                      <p className="text-[11px] text-ink-400">
                        Current:{" "}
                        <code className="text-[11px]">{[curCmd, ...curArgs].join(" ")}</code>. Click “Detect installs” to
                        find the Windows-native and WSL/Linux claude binaries and switch between them.
                      </p>
                    )}
                  </div>
                );
              })()}

              <div className="grid grid-cols-2 gap-4">
                <Field label="Command (advanced)">
                  <input className={inputCls} value={String(value("claude_code.command", c.claude_code.command))} onChange={(e) => setValue("claude_code.command", e.target.value)} />
                </Field>
                <Field label="Command args (advanced — space-separated)">
                  <input
                    className={inputCls}
                    value={((value("claude_code.command_args", c.claude_code.command_args) as string[]) ?? []).join(" ")}
                    onChange={(e) => setValue("claude_code.command_args", e.target.value.trim() === "" ? [] : e.target.value.trim().split(/\s+/))}
                  />
                </Field>
                <Field label="Per-call timeout (seconds)">
                  <input className={inputCls} type="number" value={Number(value("claude_code.default_timeout_seconds", c.claude_code.default_timeout_seconds))} onChange={(e) => setValue("claude_code.default_timeout_seconds", Number(e.target.value))} />
                </Field>
                <div className="pt-6 space-y-2">
                  <Toggle checked={Boolean(value("claude_code.auto_update_on_start", c.claude_code.auto_update_on_start))} onChange={(v) => setValue("claude_code.auto_update_on_start", v)} label="Run `claude update` on every GUI launch" />
                  <Toggle checked={Boolean(value("first_run.install_missing_deps", c.first_run.install_missing_deps))} onChange={(v) => setValue("first_run.install_missing_deps", v)} label="Auto-install missing Python deps into .venv" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4 border-t border-line pt-4">
                <Field label="Codex command">
                  <input className={inputCls} value={String(value("codex_cli.command", c.codex_cli.command))} onChange={(e) => setValue("codex_cli.command", e.target.value)} />
                </Field>
                <Field label="Codex command args">
                  <input
                    className={inputCls}
                    value={((value("codex_cli.command_args", c.codex_cli.command_args) as string[]) ?? []).join(" ")}
                    onChange={(e) => setValue("codex_cli.command_args", e.target.value.trim() === "" ? [] : e.target.value.trim().split(/\s+/))}
                  />
                </Field>
                <Field label="Codex timeout (seconds)">
                  <input className={inputCls} type="number" value={Number(value("codex_cli.default_timeout_seconds", c.codex_cli.default_timeout_seconds))} onChange={(e) => setValue("codex_cli.default_timeout_seconds", Number(e.target.value))} />
                </Field>
                <Field label="Codex reasoning effort">
                  <select className={inputCls} value={String(value("codex_cli.reasoning_effort", c.codex_cli.reasoning_effort))} onChange={(e) => setValue("codex_cli.reasoning_effort", e.target.value)}>
                    {EFFORTS.map((e) => <option key={e}>{e}</option>)}
                  </select>
                </Field>
              </div>
            </div>
          </Card>

          <Card>
            <CardHeader title="LLM routing" sub="model profiles choose whole-deal or per-document extraction" />
            <div className="px-4 pb-4 grid grid-cols-3 gap-4 max-w-4xl">
              <Field label="Provider">
                <select
                  className={inputCls}
                  value={activeProvider}
                  onChange={(e) => {
                    setValue("llm.provider", e.target.value);
                    if (e.target.value === "codex") setValue("llm.single_model_model", "provider-default");
                  }}
                >
                  <option value="claude">claude</option>
                  <option value="codex">codex</option>
                </select>
              </Field>
              <Field label="Mode">
                <select className={inputCls} value={String(value("llm.routing_mode", c.llm.routing_mode ?? c.llm.mode))} onChange={(e) => setValue("llm.routing_mode", e.target.value)}>
                  <option value="auto">auto</option>
                  <option value="per_deal">per_deal</option>
                  <option value="single_model">single_model</option>
                </select>
              </Field>
              <Field label="Budget cap (USD / run)">
                <input className={inputCls} type="number" step="0.5" value={Number(value("llm.budget_usd", c.llm.budget_usd))} onChange={(e) => setValue("llm.budget_usd", Number(e.target.value))} />
              </Field>
              <div className="pt-6">
                <Toggle checked={Boolean(value("llm.allow_fable", c.llm.allow_fable))} onChange={(v) => setValue("llm.allow_fable", v)} label="Allow Fable tier (most expensive — explicit opt-in)" />
              </div>
              <Field label="Single-model default">
                <select className={inputCls} value={String(value("llm.single_model_model", c.llm.single_model_model ?? c.llm.manual_model))} onChange={(e) => setValue("llm.single_model_model", e.target.value)}>
                  {(models.data?.models ?? []).map((m) => (
                    <option key={m.alias} value={m.alias}>{m.alias}</option>
                  ))}
                </select>
              </Field>
              <Field label="Single-model effort">
                <select className={inputCls} value={String(value("llm.single_model_effort", c.llm.single_model_effort ?? c.llm.manual_effort))} onChange={(e) => setValue("llm.single_model_effort", e.target.value)}>
                  {EFFORTS.map((e) => <option key={e}>{e}</option>)}
                </select>
              </Field>
              <Field label="Escalation confidence threshold">
                <input className={inputCls} type="number" step="0.05" min="0" max="1" value={Number(value("extraction.confidence_threshold", c.extraction.confidence_threshold))} onChange={(e) => setValue("extraction.confidence_threshold", Number(e.target.value))} />
              </Field>
              <Field label="Repair policy">
                <select className={inputCls} value={String(value("llm.candidate_arbitration.repair_policy", "never"))} onChange={(e) => setValue("llm.candidate_arbitration.repair_policy", e.target.value)}>
                  <option value="never">never</option>
                  <option value="core_only">core_only</option>
                </select>
              </Field>
              <Field label="Confirm-documents auto-select floor (%)">
                <input
                  className={inputCls}
                  type="number"
                  step="1"
                  min="0"
                  max="100"
                  value={Math.round(Number(value("selection.min_confidence", c.selection?.min_confidence ?? 0)) * 100)}
                  onChange={(e) => setValue("selection.min_confidence", Math.max(0, Math.min(100, Number(e.target.value))) / 100)}
                />
              </Field>
              {activeProvider === "claude" && auto &&
                ([
                  ["llm.auto.extraction_model", "AUTO: extraction model", auto.extraction_model],
                  ["llm.auto.extraction_effort", "AUTO: extraction effort", auto.extraction_effort],
                  ["llm.auto.ocr_hostile_model", "AUTO: large/image model", auto.ocr_hostile_model],
                  ["llm.auto.ocr_hostile_effort", "AUTO: OCR-hostile effort", auto.ocr_hostile_effort],
                ] as const
                ).map(([path, label, current]) => (
                  <Field key={path} label={label}>
                    {path.endsWith("_effort") ? (
                      <select className={inputCls} value={String(value(path, current))} onChange={(e) => setValue(path, e.target.value)}>
                        {EFFORTS.map((e) => <option key={e}>{e}</option>)}
                      </select>
                    ) : (
                      <select className={inputCls} value={String(value(path, current))} onChange={(e) => setValue(path, e.target.value)}>
                        {(models.data?.models ?? []).map((m) => <option key={m.alias} value={m.alias}>{m.alias}</option>)}
                      </select>
                    )}
                  </Field>
                ))}
            </div>
          </Card>

          <Card>
            <CardHeader title="Deal discovery" sub="how discovered deal folders are shown in New Run (storage always keeps every folder)" />
            <div className="px-4 pb-4 grid grid-cols-3 gap-4 max-w-3xl">
              <Field label="Show deals at or above confidence (%)">
                <input
                  className={inputCls}
                  type="number"
                  min="0"
                  max="100"
                  step="5"
                  value={Math.round(Number(value("deal_discovery.display_min_confidence", c.deal_discovery?.display_min_confidence ?? 0.7)) * 100)}
                  onChange={(e) => setValue("deal_discovery.display_min_confidence", Math.max(0, Math.min(100, Number(e.target.value))) / 100)}
                />
              </Field>
              <p className="col-span-2 pt-6 text-[12px] text-ink-500">
                Discovered deal folders scoring below this are hidden in Browse / LLM-assist. Lower it to
                reveal more candidates; nothing is ever dropped from the index.
              </p>
            </div>
          </Card>

          <Card>
            <CardHeader title="GUI" sub="loopback only — non-loopback hosts are refused at config load" />
            <div className="px-4 pb-4 grid grid-cols-3 gap-4 max-w-3xl">
              <Field label="Port">
                <input className={inputCls} type="number" value={Number(value("gui.port", c.gui.port))} onChange={(e) => setValue("gui.port", Number(e.target.value))} />
              </Field>
              <Field label="Evidence render DPI">
                <input className={inputCls} type="number" value={Number(value("gui.evidence_dpi", c.gui.evidence_dpi))} onChange={(e) => setValue("gui.evidence_dpi", Number(e.target.value))} />
              </Field>
              <div className="pt-6">
                <Toggle checked={Boolean(value("gui.open_browser", c.gui.open_browser))} onChange={(v) => setValue("gui.open_browser", v)} label="Open browser on launch" />
              </div>
            </div>
          </Card>
        </>
      )}

      <Card>
        <CardHeader title="Model pricing" sub="USD per 1M tokens — editable estimates backing the cost ledger" />
        <ModelPricingTable data={models.data} loading={models.loading} error={models.error} onRetry={models.reload} onSaved={models.reload} />
      </Card>

      <Card>
        <CardHeader
          title="Advanced — full config.yaml"
          sub="every tunable in one place; edits are validated before they touch disk, comments preserved"
          right={
            <Button kind="primary" onClick={saveRaw} disabled={!rawDirty || rawSaving}>
              {rawSaving ? "Saving…" : "Save config.yaml"}
            </Button>
          }
        />
        <div className="px-4 pb-4 space-y-2">
          {rawError && <p className="text-[12px] text-err whitespace-pre-wrap">{rawError}</p>}
          {rawSaved && !rawDirty && <p className="text-[12px] text-ok">saved to config.yaml</p>}
          {raw.error && <p className="text-[12px] text-err">{raw.error}</p>}
          {raw.data && (
            <textarea
              className={`${inputCls} font-mono text-[12px] leading-5 min-h-[420px] whitespace-pre`}
              spellCheck={false}
              value={rawDraft ?? raw.data.text}
              onChange={(e) => {
                setRawDraft(e.target.value);
                setRawSaved(false);
              }}
            />
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Learned locator overrides"
          sub="analyst picks recorded in New Run → Confirm documents; consumed by the locator on every run (still peek-verified)"
          right={overrideError ? <span className="text-[12px] text-err">{overrideError}</span> : undefined}
        />
        <DataTable<OverrideRow>
          rows={overrides.data?.overrides ?? null}
          loading={overrides.loading}
          error={overrides.error}
          onRetry={overrides.reload}
          emptyTitle="No overrides recorded"
          emptyHint="Swap or add a file in New Run → Confirm documents to record one."
          rowKey={(o) => `${o.client}|${o.deal}|${o.as_of_date}|${o.doc_type}`}
          filterable
          columns={[
            { key: "client", header: "Client", render: (o) => o.client, sortValue: (o) => o.client, filterValue: (o) => o.client },
            { key: "deal", header: "Deal", render: (o) => o.deal, sortValue: (o) => o.deal, filterValue: (o) => o.deal },
            { key: "asof", header: "As-of", render: (o) => o.as_of_date, sortValue: (o) => o.as_of_date },
            { key: "doc", header: "Doc type", render: (o) => <span className="text-[12px]">{o.doc_type.replace(/_/g, " ")}</span>, filterValue: (o) => o.doc_type },
            { key: "file", header: "File", render: (o) => <span className="font-mono text-[11px] break-all">{o.file_path}</span>, filterValue: (o) => o.file_path },
            { key: "ts", header: "Recorded", render: (o) => <span className="text-[12px] text-ink-500">{o.created_at}</span>, sortValue: (o) => o.created_at },
            { key: "rm", header: "", render: (o) => <Button kind="ghost" onClick={() => deleteOverride(o)} title="Forget this override">remove</Button> },
          ]}
        />
      </Card>

      <Card>
        <CardHeader
          title="Environment / doctor"
          sub="provider CLI availability, configured models, actual-vs-estimated cost accounting"
          right={
            <span className="flex gap-2">
              {activeProvider === "claude" && <Button kind="secondary" onClick={triggerUpdate}>Update Claude Code</Button>}
              <Button kind="primary" onClick={runDoctor} disabled={doctorBusy}>
                {doctorBusy ? "Running checks…" : "Run doctor"}
              </Button>
            </span>
          }
        />
        <div className="px-4 pb-4 space-y-3">
          {updateMsg && <p className="text-[12px] text-ink-500">{updateMsg}</p>}
          {setup.data && (
            <div>
              <p className="text-[11px] uppercase tracking-wide text-ink-400 mb-1">Setup checks</p>
              <ul className="space-y-1">
                {setup.data.items.map((item) => (
                  <li key={item.name} className="flex items-start gap-2 text-[12.5px]">
                    <StatusChip value={item.ok ? "completed" : "failed"} />
                    <span className="text-ink-800 font-medium">{item.name}</span>
                    <span className="text-ink-500">{item.detail}</span>
                    {item.remediation && <code className="text-[11px] bg-surface border border-line rounded px-1.5 py-0.5">{item.remediation}</code>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {doctorBusy && (
            <div className="space-y-2 py-2">
              <div className="skeleton h-4 w-2/3" />
              <div className="skeleton h-4 w-1/2" />
              <div className="skeleton h-4 w-3/5" />
            </div>
          )}
          {doctor && (
            <DataTable<DoctorCheck>
              rows={doctor.checks}
              rowKey={(d) => d.check}
              columns={[
                { key: "check", header: "Check", render: (d) => d.check },
                { key: "ok", header: "OK", render: (d) => <StatusChip value={d.ok ? "completed" : "failed"} /> },
                { key: "detail", header: "Detail", render: (d) => <span className="text-[12px] text-ink-600 break-all">{d.detail}</span> },
              ]}
            />
          )}
        </div>
      </Card>

      {picking && (
        <FolderPicker
          title={picking.title}
          initial={picking.initial}
          pickFiles={picking.pickFiles}
          onClose={() => setPicking(null)}
          onSelect={(path) => {
            setValue(picking.field, path);
            setPicking(null);
          }}
        />
      )}
    </Panel>
  );
}
