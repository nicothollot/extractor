import { useEffect, useRef } from "react";
import { JobEvent } from "../lib/api";

/** Live log tail: the pipeline's structured JSONL records bridged into the
    job event stream (identifiers and counters only — never memo content). */
export function LogTail({ events }: { events: JobEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  const logs = events.filter((e) => e.type === "log");

  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs.length]);

  return (
    <div
      ref={box}
      className="font-mono text-[11.5px] leading-5 bg-navy-deep text-ink-200 rounded-[var(--hl-radius)] px-3 py-2 h-56 overflow-auto"
    >
      {logs.length === 0 && <p className="text-ink-500">no log records yet</p>}
      {logs.map((e) => {
        const { message, logger, level, ...rest } = e.payload as Record<string, unknown>;
        const extras = Object.entries(rest)
          .filter(([k]) => k !== "ts")
          .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
          .join(" ");
        return (
          <p key={e.seq} className="whitespace-nowrap">
            <span className="text-ink-500">{e.ts.slice(11, 19)}</span>{" "}
            <span className={String(level) === "ERROR" ? "text-red-300" : "text-ink-400"}>
              {String(logger ?? "").replace("pv_extractor.", "")}
            </span>{" "}
            <span className="text-ink-100">{String(message ?? "")}</span>{" "}
            <span className="text-ink-400">{extras}</span>
          </p>
        );
      })}
    </div>
  );
}
