import { motion, useReducedMotion } from "framer-motion";
import { JobEvent } from "../lib/api";

export const STAGES = ["locate", "verify", "read", "extract", "validate", "write"] as const;

export interface Lane {
  client: string;
  deal: string;
  stages: Record<string, string>; // stage -> status
  fileName: string | null;
  memoId: string | null;
  group: string | null; // firm name on the multi-search path; null on the single path
}

/** Fold the job's stage events into per-memo pipeline lanes. */
export function buildLanes(events: JobEvent[]): Lane[] {
  const lanes = new Map<string, Lane>();
  for (const e of events) {
    if (e.type !== "stage") continue;
    const p = e.payload as Record<string, string | undefined>;
    const key = `${p.client}|${p.deal}`;
    if (!lanes.has(key)) {
      lanes.set(key, { client: p.client ?? "", deal: p.deal ?? "", stages: {}, fileName: null, memoId: null, group: null });
    }
    const lane = lanes.get(key)!;
    if (p.group != null) lane.group = p.group;
    if (p.stage) lane.stages[p.stage] = p.status ?? "";
    if (p.stage === "extract" && p.status === "cached") {
      lane.stages.read = "cached";
      lane.stages.extract = "cached";
    }
    if (p.file_name) lane.fileName = p.file_name;
    if (p.memo_id) lane.memoId = p.memo_id;
  }
  return [...lanes.values()];
}

const TERMINAL_LOCATE = new Set(["NOT_FOUND", "NOT_YET_UPLOADED", "ACCESS_ERROR"]);

function tickState(lane: Lane, stage: string): "done" | "active" | "skipped" | "failed" | "pending" {
  const status = lane.stages[stage];
  if (stage === "locate") {
    if (!status) return "pending";
    if (TERMINAL_LOCATE.has(status)) return "failed";
    return "done";
  }
  const locate = lane.stages.locate;
  if (locate && TERMINAL_LOCATE.has(locate)) return "skipped";
  if (stage === "verify") {
    if (!status) return lane.stages.locate ? "active" : "pending";
    if (status === "started") return "active";
    return status === "FOUND" ? "done" : "failed";
  }
  if (!status) {
    const verify = lane.stages.verify;
    if (verify && verify !== "FOUND" && verify !== "started") return "skipped";
    return "pending";
  }
  if (status === "started") return "active";
  return "done";
}

/** A single memo lane row (stage ticks). Shared by the flat + grouped layouts. */
function LaneRow({ lane, reduced }: { lane: Lane; reduced: boolean | null }) {
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2">
        <span className="font-medium text-ink-800">{lane.client}</span>
        <span className="text-ink-400"> / </span>
        <span className="text-ink-700">{lane.deal}</span>
        {lane.fileName && <p className="text-[11px] text-ink-400 truncate max-w-[28ch]">{lane.fileName}</p>}
      </td>
      {STAGES.map((stage) => {
        const state = tickState(lane, stage);
        return (
          <td key={stage} className="text-center px-2 py-2">
            {state === "done" && (
              <motion.span
                className="text-ok font-semibold"
                initial={reduced ? false : { scale: 0.6, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ duration: 0.15 }}
              >
                ✓
              </motion.span>
            )}
            {state === "active" && (
              <span className="inline-block w-2 h-2 rounded-full bg-info align-middle" aria-label="in progress" />
            )}
            {state === "failed" && <span className="text-err font-semibold">✕</span>}
            {state === "skipped" && <span className="text-ink-300">·</span>}
            {state === "pending" && <span className="text-ink-200">○</span>}
          </td>
        );
      })}
    </tr>
  );
}

function LanesHead() {
  return (
    <thead>
      <tr>
        <th className="text-left text-[11px] uppercase tracking-wide text-ink-500 font-semibold px-3 py-2 border-b border-line">
          memo
        </th>
        {STAGES.map((s) => (
          <th
            key={s}
            className="text-center text-[11px] uppercase tracking-wide text-ink-500 font-semibold px-2 py-2 border-b border-line"
          >
            {s}
          </th>
        ))}
      </tr>
    </thead>
  );
}

/** A lane is "done" once its terminal deterministic stage finished. */
function laneDone(lane: Lane): boolean {
  const locate = lane.stages.locate;
  if (locate && TERMINAL_LOCATE.has(locate)) return true;
  return lane.stages.validate === "done";
}

export function ProgressLanes({ events }: { events: JobEvent[] }) {
  const lanes = buildLanes(events);
  const reduced = useReducedMotion();
  if (lanes.length === 0) {
    return <p className="text-[12px] text-ink-400 px-4 py-6">Waiting for the first locate event…</p>;
  }

  // Grouped layout only when the multi-search path stamped a firm on a lane;
  // otherwise (every single-firm run) render the flat table exactly as before.
  const grouped = lanes.some((l) => l.group != null);

  if (!grouped) {
    return (
      <div className="overflow-auto">
        <table className="w-full text-[13px]">
          <LanesHead />
          <tbody>
            {lanes.map((lane) => (
              <LaneRow key={`${lane.client}|${lane.deal}`} lane={lane} reduced={reduced} />
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // Preserve first-seen order of firms; lanes without a group fall under "—".
  const order: string[] = [];
  const byGroup = new Map<string, Lane[]>();
  for (const lane of lanes) {
    const g = lane.group ?? "—";
    if (!byGroup.has(g)) {
      byGroup.set(g, []);
      order.push(g);
    }
    byGroup.get(g)!.push(lane);
  }

  return (
    <div className="overflow-auto space-y-4">
      {order.map((g) => {
        const groupLanes = byGroup.get(g)!;
        const done = groupLanes.filter(laneDone).length;
        return (
          <div key={g}>
            <div className="flex items-baseline justify-between px-3 py-1.5">
              <span className="text-[12px] font-semibold text-ink-800">{g}</span>
              <span className="text-[11px] text-ink-500">
                {done}/{groupLanes.length} memos
              </span>
            </div>
            <table className="w-full text-[13px]">
              <LanesHead />
              <tbody>
                {groupLanes.map((lane) => (
                  <LaneRow key={`${lane.client}|${lane.deal}`} lane={lane} reduced={reduced} />
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}
