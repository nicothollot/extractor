import { motion, useReducedMotion } from "framer-motion";

export function Stepper({
  steps,
  current,
  onStep,
}: {
  steps: string[];
  current: number;
  onStep?: (index: number) => void;
}) {
  const reduced = useReducedMotion();
  return (
    <ol className="flex items-center gap-0 select-none">
      {steps.map((label, i) => {
        const state = i < current ? "done" : i === current ? "active" : "todo";
        return (
          <li key={label} className="flex items-center">
            {i > 0 && <span className={`w-10 h-px ${i <= current ? "bg-navy" : "bg-line-strong"}`} />}
            <button
              type="button"
              disabled={i > current || !onStep}
              onClick={() => onStep?.(i)}
              className="flex items-center gap-2 px-2 py-1 disabled:cursor-default"
            >
              <motion.span
                className={`w-6 h-6 rounded-full border text-[12px] font-semibold flex items-center justify-center ${
                  state === "done"
                    ? "bg-navy text-white border-navy"
                    : state === "active"
                      ? "bg-paper text-navy border-navy"
                      : "bg-paper text-ink-400 border-line-strong"
                }`}
                animate={reduced ? undefined : { scale: state === "active" ? 1.06 : 1 }}
                transition={{ duration: 0.15 }}
              >
                {state === "done" ? "✓" : i + 1}
              </motion.span>
              <span
                className={`text-[13px] ${
                  state === "active" ? "text-ink-900 font-semibold" : state === "done" ? "text-ink-700" : "text-ink-400"
                }`}
              >
                {label}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
