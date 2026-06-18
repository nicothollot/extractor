import { motion, useReducedMotion } from "framer-motion";

/* Hand-rolled SVG charts: deterministic, restrained, no chart library. */

const DONUT_COLORS: Record<string, string> = {
  FOUND: "var(--hl-success)",
  AMBIGUOUS: "var(--hl-warning)",
  NOT_FOUND: "var(--hl-error)",
  NOT_YET_UPLOADED: "var(--hl-blue)",
  ACCESS_ERROR: "var(--hl-error)",
  ERROR: "var(--hl-error)",
  DEFERRED: "var(--hl-gray-400)",
};

export function CoverageDonut({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const reduced = useReducedMotion();
  if (total === 0) {
    return <p className="text-[12px] text-ink-400 py-8 text-center">No coverage data</p>;
  }
  const radius = 52;
  const stroke = 18;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  return (
    <div className="flex items-center gap-6">
      <svg width="140" height="140" viewBox="0 0 140 140" role="img" aria-label="Coverage donut">
        <circle cx="70" cy="70" r={radius} fill="none" stroke="var(--hl-gray-100)" strokeWidth={stroke} />
        {entries.map(([status, count]) => {
          const fraction = count / total;
          const dash = fraction * circumference;
          const el = (
            <motion.circle
              key={status}
              cx="70"
              cy="70"
              r={radius}
              fill="none"
              stroke={DONUT_COLORS[status] ?? "var(--hl-gray-400)"}
              strokeWidth={stroke}
              strokeDasharray={`${dash} ${circumference - dash}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 70 70)"
              initial={reduced ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.18 }}
            />
          );
          offset += dash;
          return el;
        })}
        <text x="70" y="66" textAnchor="middle" className="fill-ink-900" fontSize="22" fontWeight="600">
          {total}
        </text>
        <text x="70" y="84" textAnchor="middle" className="fill-ink-500" fontSize="10">
          deals
        </text>
      </svg>
      <ul className="space-y-1.5">
        {entries.map(([status, count]) => (
          <li key={status} className="flex items-center gap-2 text-[12px] text-ink-700">
            <span
              className="w-2.5 h-2.5 rounded-sm inline-block"
              style={{ background: DONUT_COLORS[status] ?? "var(--hl-gray-400)" }}
            />
            <span className="font-mono w-8 text-right">{count}</span>
            <span>{status.replace(/_/g, " ")}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Sparkline({
  points,
  labels,
}: {
  points: number[];
  labels?: string[];
}) {
  if (points.length === 0) {
    return <p className="text-[12px] text-ink-400 py-6 text-center">No cost history yet</p>;
  }
  const w = 280;
  const h = 56;
  const pad = 4;
  const max = Math.max(...points, 0.0001);
  const step = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0;
  const xy = points.map((v, i) => [pad + i * step, h - pad - (v / max) * (h - pad * 2)]);
  const path = xy.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const total = points.reduce((s, v) => s + v, 0);
  return (
    <div>
      <svg width={w} height={h} role="img" aria-label="Cumulative cost sparkline">
        <path d={path} fill="none" stroke="var(--hl-blue)" strokeWidth="1.5" />
        {xy.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r="2" fill="var(--hl-navy)">
            {labels?.[i] && <title>{`${labels[i]}: $${points[i].toFixed(4)}`}</title>}
          </circle>
        ))}
      </svg>
      <p className="text-[12px] text-ink-500 mt-1">
        cumulative LLM spend <span className="font-mono text-ink-800">${total.toFixed(2)}</span> across{" "}
        {points.length} run{points.length === 1 ? "" : "s"}
      </p>
    </div>
  );
}

/** Determinate cost meter against the hard budget cap. */
export function CostMeter({ spent, budget, source }: { spent: number; budget: number; source: string | null }) {
  const fraction = budget > 0 ? Math.min(1, spent / budget) : 0;
  const tone = fraction < 0.7 ? "var(--hl-success)" : fraction < 0.95 ? "var(--hl-warning)" : "var(--hl-error)";
  const reduced = useReducedMotion();
  return (
    <div>
      <div className="flex justify-between text-[12px] text-ink-600 mb-1">
        <span>
          LLM spend{" "}
          {source && <span className="uppercase text-[10px] tracking-wide text-ink-400">({source})</span>}
        </span>
        <span className="font-mono">
          ${spent.toFixed(4)} / ${budget.toFixed(2)}
        </span>
      </div>
      <div className="h-2 bg-ink-100 rounded overflow-hidden">
        <motion.div
          className="h-full"
          style={{ background: tone }}
          initial={reduced ? { width: `${fraction * 100}%` } : { width: 0 }}
          animate={{ width: `${fraction * 100}%` }}
          transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
        />
      </div>
    </div>
  );
}
