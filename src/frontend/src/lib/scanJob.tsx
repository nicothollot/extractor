import { ReactNode, createContext, useContext, useEffect, useState } from "react";
import { JobInfo, get } from "./api";

/* The id of the current index-scan job, lifted above the router so switching
   tabs and coming back keeps the live status (Settings re-subscribes and
   replays the job's events from seq 0 on remount). Unlike the wizard, it also
   reattaches after a full page reload: the in-memory id is gone but the backend
   job keeps running, so on first load we adopt any still-ACTIVE scan job. */

const ACTIVE = ["queued", "running", "cancelling"];

interface ScanJobValue {
  scanJobId: string | null;
  setScanJobId: (id: string | null) => void;
}

const ScanJobContext = createContext<ScanJobValue | null>(null);

export function ScanJobProvider({ children }: { children: ReactNode }) {
  const [scanJobId, setScanJobId] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    get<{ jobs: JobInfo[] }>("/api/jobs?kind=scan")
      .then((r) => {
        if (!alive) return;
        // newest active scan job wins; never resurrect a completed one
        const running = r.jobs
          .filter((j) => ACTIVE.includes(j.status))
          .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
        if (running) setScanJobId((cur) => cur ?? running.id);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  return <ScanJobContext.Provider value={{ scanJobId, setScanJobId }}>{children}</ScanJobContext.Provider>;
}

export function useScanJob(): ScanJobValue {
  const ctx = useContext(ScanJobContext);
  if (!ctx) throw new Error("useScanJob must be used within ScanJobProvider");
  return ctx;
}
