import { motion } from "framer-motion";
import { ReactNode } from "react";

/* Restrained building blocks: 1px borders, 8px grid, shadow only on lift. */

export function Card({
  children,
  className = "",
  lift = false,
}: {
  children: ReactNode;
  className?: string;
  lift?: boolean;
}) {
  return (
    <div
      className={`bg-paper border border-line rounded-[var(--hl-radius)] ${lift ? "lift" : ""} ${className}`}
    >
      {children}
    </div>
  );
}

export function CardHeader({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div className="flex items-start justify-between px-4 pt-4 pb-2">
      <div>
        <h2 className="text-[13px] font-semibold tracking-wide text-ink-800 uppercase">{title}</h2>
        {sub && <p className="text-[12px] text-ink-500 mt-1">{sub}</p>}
      </div>
      {right}
    </div>
  );
}

export function Button({
  children,
  onClick,
  kind = "secondary",
  disabled = false,
  title,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: "primary" | "secondary" | "danger" | "ghost";
  disabled?: boolean;
  title?: string;
  type?: "button" | "submit";
}) {
  const styles: Record<string, string> = {
    primary: "bg-navy text-white border-navy hover:bg-navy-deep",
    secondary: "bg-paper text-ink-800 border-line-strong hover:bg-ink-50",
    danger: "bg-paper text-err border-err hover:bg-err-soft",
    ghost: "bg-transparent text-ink-600 border-transparent hover:bg-ink-100",
  };
  return (
    <button
      type={type}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`px-3 py-1.5 text-[13px] font-medium rounded-[var(--hl-radius)] border transition-colors duration-150 disabled:opacity-45 disabled:cursor-not-allowed ${styles[kind]}`}
    >
      {children}
    </button>
  );
}

/** A small "ⓘ" affordance that reveals a details popover on hover/focus.
 * `align` controls which edge the popover anchors to so it never spills off
 * screen next to right-aligned controls. */
export function InfoDot({
  text,
  align = "center",
  className = "",
}: {
  text: ReactNode;
  align?: "left" | "center" | "right";
  className?: string;
}) {
  const anchor =
    align === "left"
      ? "left-0"
      : align === "right"
        ? "right-0"
        : "left-1/2 -translate-x-1/2";
  return (
    <span className={`group relative inline-flex items-center align-middle ${className}`}>
      <button
        type="button"
        tabIndex={0}
        aria-label="More information"
        className="flex items-center justify-center w-3.5 h-3.5 rounded-full border border-ink-300 text-ink-400 text-[9px] font-semibold leading-none cursor-help select-none hover:border-ink-500 hover:text-ink-600 focus:outline-none focus:border-accent"
      >
        i
      </button>
      <span
        role="tooltip"
        className={`pointer-events-none absolute top-full z-30 mt-1.5 w-64 ${anchor} rounded-[var(--hl-radius)] border border-line-strong bg-paper px-3 py-2 text-[11.5px] leading-snug text-ink-700 opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100`}
      >
        {text}
      </span>
    </span>
  );
}

const STATUS_STYLES: Record<string, string> = {
  FOUND: "bg-ok-soft text-ok",
  AMBIGUOUS: "bg-warn-soft text-warn",
  NOT_YET_UPLOADED: "bg-info-soft text-info",
  NOT_FOUND: "bg-err-soft text-err",
  ACCESS_ERROR: "bg-err-soft text-err",
  ERROR: "bg-err-soft text-err",
  DEFERRED: "bg-ink-100 text-ink-600",
  qa_pass: "bg-ok-soft text-ok",
  qa_pass_with_flags: "bg-warn-soft text-warn",
  qa_fail: "bg-err-soft text-err",
  completed: "bg-ok-soft text-ok",
  running: "bg-info-soft text-info",
  queued: "bg-ink-100 text-ink-600",
  cancelling: "bg-warn-soft text-warn",
  cancelled: "bg-ink-100 text-ink-600",
  failed: "bg-err-soft text-err",
  interrupted: "bg-warn-soft text-warn",
};

export function StatusChip({ value }: { value: string }) {
  const style = STATUS_STYLES[value] ?? "bg-ink-100 text-ink-700";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-semibold tracking-wide ${style}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function MethodChip({ method }: { method: string | null }) {
  if (!method) return <span className="text-ink-400 text-[12px]">—</span>;
  const style = method.startsWith("llm:")
    ? "bg-info-soft text-info"
    : method === "ocr"
      ? "bg-warn-soft text-warn"
      : method === "computed"
        ? "bg-ink-100 text-ink-700"
        : "bg-ok-soft text-ok";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-mono ${style}`}>{method}</span>
  );
}

export function ConfidenceBar({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span className="text-ink-400">—</span>;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const tone = value >= 0.75 ? "bg-ok" : value >= 0.5 ? "bg-warn" : "bg-err";
  return (
    <span className="inline-flex items-center gap-2">
      <span className="w-16 h-1.5 bg-ink-100 rounded overflow-hidden inline-block">
        <span className={`block h-full ${tone}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="text-[12px] text-ink-600 font-mono">{value.toFixed(2)}</span>
    </span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-12 text-center">
      <p className="text-ink-600 font-medium">{title}</p>
      {hint && <p className="text-[12px] text-ink-400 mt-2">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="py-10 text-center">
      <p className="text-err font-medium">Something went wrong</p>
      <p className="text-[12px] text-ink-500 mt-2 max-w-xl mx-auto break-words">{message}</p>
      {onRetry && (
        <div className="mt-4">
          <Button onClick={onRetry}>Retry</Button>
        </div>
      )}
    </div>
  );
}

export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="px-4 pb-4 space-y-2">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }).map((_, c) => (
            <div key={c} className="skeleton h-5" style={{ width: `${100 / cols - 3}%` }} />
          ))}
        </div>
      ))}
    </div>
  );
}

/** Page/panel transition wrapper — ≤200ms, opacity+4px slide only. */
export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
      className={className}
    >
      {children}
    </motion.div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[12px] font-medium text-ink-600 mb-1">{label}</span>
      {children}
    </label>
  );
}

export const inputCls =
  "w-full px-2.5 py-1.5 text-[13px] bg-paper border border-line-strong rounded-[var(--hl-radius)] " +
  "focus:outline-none focus:border-accent text-ink-900";

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 text-[13px] text-ink-800"
    >
      <span
        className={`w-8 h-4.5 rounded-full p-0.5 transition-colors duration-150 ${checked ? "bg-navy" : "bg-ink-300"}`}
      >
        <span
          className={`block w-3.5 h-3.5 bg-paper rounded-full transition-transform duration-150 ${checked ? "translate-x-3.5" : ""}`}
        />
      </span>
      {label}
    </button>
  );
}
