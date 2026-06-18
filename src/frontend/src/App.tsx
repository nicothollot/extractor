import { MotionConfig } from "framer-motion";
import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { get, post, JobInfo } from "./lib/api";
import { useJobPolling } from "./lib/hooks";
import { WizardProvider } from "./lib/wizard";
import { ScanJobProvider, useScanJob } from "./lib/scanJob";
import { UIStateProvider } from "./lib/uiState";
import { StatusChip } from "./components/ui";
import { HLLogo } from "./components/branding";
import { ErrorBoundary } from "./components/ErrorBoundary";
import Dashboard from "./screens/Dashboard";
import Guide from "./screens/Guide";
import NewRun from "./screens/NewRun";
import OutputBrowser from "./screens/OutputBrowser";
import ReviewQueue from "./screens/ReviewQueue";
import RunProgress from "./screens/RunProgress";
import Settings from "./screens/Settings";

const NAV = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/new-run", label: "New Run" },
  { to: "/review", label: "Review Queue" },
  { to: "/output", label: "Output Browser" },
  { to: "/guide", label: "Guide" },
  { to: "/settings", label: "Settings" },
];

/** Non-blocking startup card: when auto_update_on_start is set, kick off
    `claude update` as a job and show its progress without holding anything up. */
function StartupCard() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const job = useJobPolling(jobId);

  useEffect(() => {
    get<{ auto_update_on_start: boolean }>("/api/health")
      .then((h) => {
        if (h.auto_update_on_start) {
          return post<{ job: JobInfo }>("/api/setup/claude-update").then((r) => setJobId(r.job.id));
        }
      })
      .catch((e: Error) => setFailed(e.message));
  }, []);

  if (dismissed || (!jobId && !failed)) return null;
  const status = failed ? "failed" : (job?.status ?? "running");
  if (status === "completed") {
    // auto-dismiss shortly after success
    window.setTimeout(() => setDismissed(true), 4000);
  }
  return (
    <div className="fixed bottom-4 right-4 bg-paper border border-line rounded-[var(--hl-radius)] shadow-lift px-4 py-3 w-80 z-50">
      <div className="flex items-center justify-between">
        <p className="text-[13px] font-medium text-ink-800">Claude Code update</p>
        <button className="text-ink-400 hover:text-ink-700 text-[13px]" onClick={() => setDismissed(true)}>
          ✕
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <StatusChip value={status} />
        <span className="text-[12px] text-ink-500">
          {failed ?? (status === "completed" ? "CLI is up to date" : "running `claude update`…")}
        </span>
      </div>
    </div>
  );
}

/** A pulsing dot on the Settings nav item while an index scan is in flight, so
    the analyst can tell indexing is still running from any tab. */
function ScanIndicator() {
  const { scanJobId } = useScanJob();
  const job = useJobPolling(scanJobId);
  if (!scanJobId || !job || !["queued", "running", "cancelling"].includes(job.status)) return null;
  return (
    <span
      title="Indexing in progress"
      className="ml-2 inline-block w-1.5 h-1.5 rounded-full bg-accent-bright animate-pulse align-middle"
    />
  );
}

/* The routed screens, behind an error boundary keyed on the path so a crash in
   one screen never blanks the whole app — the nav shell stays, and navigating
   to another tab (a new key) resets the boundary. */
function RoutedContent() {
  const location = useLocation();
  return (
    <ErrorBoundary key={location.pathname}>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/new-run" element={<NewRun />} />
        <Route path="/jobs/:jobId/progress" element={<RunProgress />} />
        <Route path="/review" element={<ReviewQueue />} />
        <Route path="/review/:runId" element={<ReviewQueue />} />
        <Route path="/output" element={<OutputBrowser />} />
        <Route path="/output/:runId" element={<OutputBrowser />} />
        <Route path="/guide" element={<Guide />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </ErrorBoundary>
  );
}

export default function App() {
  return (
    <MotionConfig reducedMotion="user">
      <WizardProvider>
      <ScanJobProvider>
      <UIStateProvider>
      <div className="min-h-screen flex">
        <aside
          className="w-56 shrink-0 text-white flex flex-col"
          style={{ background: "linear-gradient(180deg, var(--hl-navy) 0%, var(--hl-navy-deep) 100%)" }}
        >
          <div className="px-4 pt-5 pb-4 border-b border-white/10">
            <HLLogo tone="white" height={30} className="h-[30px] w-auto" />
            <div className="mt-3.5">
              <p className="text-[13.5px] font-semibold tracking-tight text-white">PV Extractor</p>
              <p className="text-[10.5px] uppercase tracking-[0.12em] text-white/45 mt-0.5">
                Valuation document index
              </p>
            </div>
          </div>
          <nav className="flex-1 py-3">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `block px-4 py-2 text-[13px] transition-colors duration-150 ${
                    isActive
                      ? "bg-white/10 text-white font-medium border-l-2 border-globe"
                      : "text-white/65 hover:text-white hover:bg-white/5 border-l-2 border-transparent"
                  }`
                }
              >
                {item.label}
                {item.to === "/settings" && <ScanIndicator />}
              </NavLink>
            ))}
          </nav>
          <p className="px-4 py-3 text-[10px] text-white/35 border-t border-white/10">
            localhost only · no telemetry
          </p>
        </aside>
        <main className="flex-1 min-w-0 px-6 py-5">
          <RoutedContent />
        </main>
        <StartupCard />
      </div>
      </UIStateProvider>
      </ScanJobProvider>
      </WizardProvider>
    </MotionConfig>
  );
}
