import { useCallback, useEffect, useRef, useState } from "react";
import { get, JobEvent, JobInfo } from "./api";
import { initialJobPollingSnapshot, JobPollingMachine, JobPollingSnapshot } from "./jobPolling";

export interface Loadable<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** Fetch-on-mount with manual reload; drives skeleton/error/empty states. */
export function useLoad<T>(path: string | null, deps: unknown[] = []): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (path === null) return;
    let alive = true;
    setLoading(true);
    setError(null);
    get<T>(path)
      .then((d) => alive && setData(d))
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, loading, error, reload };
}

/** Live job events: WebSocket with replay (?since=) + polling fallback.
    Refresh-safe — reconnecting replays the persisted event log. */
export function useJobEvents(jobId: string | null) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [job, setJob] = useState<JobInfo | null>(null);
  const lastSeq = useRef(0);

  useEffect(() => {
    if (!jobId) return;
    lastSeq.current = 0;
    setEvents([]);
    let alive = true;
    let ws: WebSocket | null = null;
    let pollTimer: number | undefined;

    const refreshJob = () =>
      get<JobInfo>(`/api/jobs/${jobId}`).then((j) => alive && setJob(j)).catch(() => undefined);

    const append = (incoming: JobEvent[]) => {
      const fresh = incoming.filter((e) => e.seq > lastSeq.current);
      if (!fresh.length) return;
      lastSeq.current = fresh[fresh.length - 1].seq;
      setEvents((prev) => [...prev, ...fresh]);
      if (fresh.some((e) => e.type === "done")) refreshJob();
    };

    refreshJob();
    try {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/api/ws/jobs/${jobId}?since=0`);
      ws.onmessage = (msg) => append([JSON.parse(msg.data) as JobEvent]);
      ws.onerror = () => ws?.close();
      ws.onclose = () => {
        // Poll fallback keeps the page live if the socket drops mid-run.
        if (!alive) return;
        pollTimer = window.setInterval(async () => {
          const res = await get<{ events: JobEvent[] }>(
            `/api/jobs/${jobId}/events?since=${lastSeq.current}`,
          ).catch(() => null);
          if (res) append(res.events);
          const j = await get<JobInfo>(`/api/jobs/${jobId}`).catch(() => null);
          if (j && alive) {
            setJob(j);
            if (!["queued", "running", "cancelling"].includes(j.status)) {
              window.clearInterval(pollTimer);
            }
          }
        }, 1500);
      };
    } catch {
      /* WS construction failed; rely on polling via onclose path */
    }
    return () => {
      alive = false;
      ws?.close();
      if (pollTimer) window.clearInterval(pollTimer);
    };
  }, [jobId]);

  return { events, job };
}

/** Poll a job until it leaves the active states, retrying transient request failures. */
export function useJobPolling(jobId: string | null, intervalMs = 1200): JobPollingSnapshot & { refresh: () => void } {
  const [snapshot, setSnapshot] = useState<JobPollingSnapshot>(() => initialJobPollingSnapshot(jobId));
  const machineRef = useRef<JobPollingMachine | null>(null);

  useEffect(() => {
    const machine = new JobPollingMachine(
      (id, signal) => get<JobInfo>(`/api/jobs/${id}`, { signal }),
      setSnapshot,
      { intervalMs },
    );
    machineRef.current = machine;
    machine.setJobId(jobId);
    return () => {
      machine.stop();
      if (machineRef.current === machine) machineRef.current = null;
    };
  }, [jobId, intervalMs]);

  const refresh = useCallback(() => {
    machineRef.current?.refresh();
  }, []);

  return { ...snapshot, refresh };
}

export const fmtUsd = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `$${v.toFixed(digits)}`;

/** Seconds -> "m:ss" (or "Xh Ym" past an hour) for elapsed/ETA displays. */
export const fmtDuration = (totalSeconds: number): string => {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(s % 60).padStart(2, "0")}`;
};

export const fmtNum = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v.toLocaleString();

export const fmtConfidence = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v.toFixed(2);

/** Coarse "N <unit> ago" for a scan/event timestamp (minutes → years), or ""
    when there is no timestamp. Robust to bad/missing input (never throws). */
export const fmtAgo = (iso: string | null | undefined): string => {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 45) return "just now";
  const units: [number, string][] = [
    [60, "minute"],
    [3600, "hour"],
    [86_400, "day"],
    [2_592_000, "month"], // 30 days
    [31_536_000, "year"], // 365 days
  ];
  // pick the largest unit whose value is < the next threshold
  const thresholds = [3600, 86_400, 2_592_000, 31_536_000, Infinity];
  for (let i = 0; i < units.length; i++) {
    if (secs < thresholds[i]) {
      const n = Math.round(secs / units[i][0]);
      return `${n} ${units[i][1]}${n === 1 ? "" : "s"} ago`;
    }
  }
  return "";
};
