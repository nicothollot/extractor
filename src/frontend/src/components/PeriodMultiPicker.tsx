import { useMemo, useState } from "react";
import { get } from "../lib/api";
import { useLoad } from "../lib/hooks";
import { Button, Field, inputCls } from "./ui";

interface Period {
  period: string; // the submit value (reporting-period label, e.g. "Q1 2026")
  as_of_date: string; // representative underlying date, for display only
  label: string;
}

/** Multi-period picker for a run: toggle discovered periods, or add a range
 *  (start..end) that expands to every period between. Periods are DEDUPED to one
 *  entry per reporting period (one "Q1 2026", never one per month-end), and the
 *  stored value is the period label so a run finds every deal in that quarter
 *  regardless of its exact month-end. Chips show the human label. An empty list
 *  = the single primary period chosen above. */
export function PeriodMultiPicker({
  client,
  deal,
  value,
  onChange,
}: {
  client: string;
  deal?: string;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const periods = useLoad<{ periods: Period[] }>(
    client
      ? `/api/index/periods?client=${encodeURIComponent(client)}${deal ? `&deal=${encodeURIComponent(deal)}` : ""}`
      : null,
    [client, deal],
  );
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [rangeErr, setRangeErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const discovered = periods.data?.periods ?? [];
  const labelByValue = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of discovered) m.set(p.period, p.label);
    return m;
  }, [discovered]);

  const has = (v: string) => value.includes(v);
  const toggle = (v: string) => onChange(has(v) ? value.filter((x) => x !== v) : [...value, v]);
  const merge = (incoming: string[]) => {
    const next = [...value];
    for (const v of incoming) if (!next.includes(v)) next.push(v);
    onChange(next);
  };

  const addRange = async () => {
    setRangeErr(null);
    if (!start.trim() || !end.trim()) {
      setRangeErr("Enter both a start and an end period.");
      return;
    }
    setBusy(true);
    try {
      const params = new URLSearchParams({ start: start.trim(), end: end.trim() });
      if (client) params.set("client", client);
      const res = await get<{ periods: Period[]; error: string | null }>(
        `/api/index/periods/expand?${params}`,
      );
      if (res.error) {
        setRangeErr(res.error);
        return;
      }
      for (const p of res.periods) labelByValue.set(p.period, p.label);
      merge(res.periods.map((p) => p.period));
      setStart("");
      setEnd("");
    } catch (e) {
      setRangeErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-2">
      <p className="text-[12px] text-ink-600 font-medium">Run multiple periods (optional)</p>
      {discovered.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {discovered.map((p) => (
            <button
              key={p.period}
              type="button"
              onClick={() => toggle(p.period)}
              className={`px-2 py-0.5 text-[12px] rounded border ${
                has(p.period)
                  ? "bg-info-soft border-[var(--hl-blue)] text-ink-900"
                  : "bg-paper border-line text-ink-600 hover:bg-ink-50"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2 flex-wrap">
        <Field label="Range start">
          <input className={inputCls} value={start} placeholder="Q1 2024" onChange={(e) => setStart(e.target.value)} />
        </Field>
        <Field label="Range end">
          <input className={inputCls} value={end} placeholder="Q4 2025" onChange={(e) => setEnd(e.target.value)} />
        </Field>
        <Button kind="secondary" onClick={addRange} disabled={busy}>
          {busy ? "Expanding…" : "Add range"}
        </Button>
      </div>
      {rangeErr && <p className="text-[12px] text-err">{rangeErr}</p>}
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1 border-t border-line">
          <span className="text-[11px] text-ink-400 self-center">running {value.length}:</span>
          {value.map((v) => (
            <span
              key={v}
              className="inline-flex items-center gap-1 px-2 py-0.5 text-[12px] bg-paper border border-line rounded"
            >
              {labelByValue.get(v) ?? v}
              <button
                type="button"
                className="text-ink-400 hover:text-err text-[11px]"
                onClick={() => onChange(value.filter((x) => x !== v))}
                title="Remove"
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
