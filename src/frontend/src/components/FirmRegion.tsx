import { useMemo, useState } from "react";
import { useLoad } from "../lib/hooks";
import { FirmEntry } from "../lib/wizard";
import { ModelsResponse } from "../lib/api";
import { Button, Card, Field, Toggle, inputCls } from "./ui";
import { ModelEffortPicker } from "./ModelEffortPicker";
import { DocTypePicker } from "./DocTypePicker";
import { FolderPicker } from "./FolderPicker";

interface DealFolderInfo {
  name: string;
  confidence: number | null;
  method: string;
  low_confidence: boolean;
  folder_paths: string[];
  periods: number;
  file_count: number;
  memo_file_count: number;
  llm_corroborated: boolean;
}

interface Period {
  as_of_date: string;
  label: string;
}

const confidencePct = (c: number | null) => (c === null ? "—" : `${Math.round(c * 100)}%`);

/* One expandable per-firm card in the Multi-Search flow. Edits patch back into
   the parent's multiFirms array through onChange so they survive tab switches.
   deals=[] is the explicit "all discovered deals" selection. */
export function FirmRegion({
  firm,
  models,
  onChange,
  onRemove,
}: {
  firm: FirmEntry;
  models: ModelsResponse | null;
  onChange: (next: FirmEntry) => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(true);
  const [folderPicker, setFolderPicker] = useState(false);

  const deals = useLoad<{ deals: string[]; deal_folders: DealFolderInfo[] }>(
    `/api/index/deals?client=${encodeURIComponent(firm.client)}`,
    [firm.client],
  );
  const periods = useLoad<{ periods: Period[] }>(
    `/api/index/periods?client=${encodeURIComponent(firm.client)}`,
    [firm.client],
  );

  const dealFolders = deals.data?.deal_folders ?? [];
  const allDiscovered = firm.deals.length === 0;
  const selectedDeals = useMemo(() => new Set(firm.deals), [firm.deals]);
  const removedDeals = useMemo(() => new Set(firm.removedDeals), [firm.removedDeals]);

  const set = (p: Partial<FirmEntry>) => onChange({ ...firm, ...p });

  const toggleDeal = (name: string) => {
    const next = new Set(selectedDeals);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    set({ deals: [...next] });
  };

  const toggleRemoveDeal = (name: string) => {
    const next = new Set(removedDeals);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    set({ removedDeals: [...next] });
  };

  const removeAddedFolder = (path: string) => set({ addedFolders: firm.addedFolders.filter((p) => p !== path) });

  return (
    <Card>
      <div className="flex items-center justify-between px-4 py-3 border-b border-line">
        <button type="button" className="flex items-center gap-2 text-left" onClick={() => setOpen((o) => !o)}>
          <span className="text-ink-400 text-[12px]">{open ? "▼" : "▶"}</span>
          <span className="font-semibold text-ink-900 text-[14px]">{firm.client}</span>
          <span className="text-[11.5px] text-ink-400">
            · {allDiscovered ? "all discovered deals" : `${firm.deals.length} deal(s)`}
            {firm.period ? ` · ${firm.period}` : " · no period"}
            {firm.docTypes.length > 0 ? ` · ${firm.docTypes.length} doc type(s)` : ""}
          </span>
        </button>
        <Button kind="ghost" onClick={onRemove} title="Remove this firm from the run">
          remove firm
        </Button>
      </div>

      {open && (
        <div className="px-4 py-4 space-y-4">
          {/* deals ----------------------------------------------------- */}
          <Field label="Deals (none selected = all discovered deals for the client)">
            <div className="space-y-1.5">
              <button
                type="button"
                className={`px-2.5 py-1 rounded-[var(--hl-radius)] text-[12px] border transition-colors ${
                  allDiscovered ? "bg-navy text-white border-navy" : "bg-paper text-ink-700 border-line-strong hover:bg-ink-50"
                }`}
                onClick={() => set({ deals: [] })}
              >
                All discovered deals
              </button>
              <div className="border border-line rounded-[var(--hl-radius)] divide-y divide-line max-h-56 overflow-y-auto">
                {deals.loading && <p className="px-3 py-2 text-[12px] text-ink-400">loading deals…</p>}
                {deals.error && <p className="px-3 py-2 text-[12px] text-err">{deals.error}</p>}
                {!deals.loading && dealFolders.length === 0 && (
                  <p className="px-3 py-2 text-[12px] text-ink-400">no deal folders discovered for this client</p>
                )}
                {dealFolders.map((d) => {
                  const sel = selectedDeals.has(d.name);
                  const removed = removedDeals.has(d.name);
                  return (
                    <div key={d.name} className="px-3 py-1.5 flex items-start justify-between gap-3">
                      <label className={`flex items-start gap-2 cursor-pointer min-w-0 ${removed ? "opacity-50 line-through" : ""}`}>
                        <input type="checkbox" className="mt-0.5" checked={sel} onChange={() => toggleDeal(d.name)} />
                        <span className="min-w-0">
                          <span className="text-[12.5px] font-medium text-ink-800">
                            {d.name}
                            {d.low_confidence && <span className="text-warn"> · low confidence</span>}
                            {d.llm_corroborated && <span className="text-info"> · LLM</span>}
                          </span>
                          <span className="block font-mono text-[11px] text-ink-400">
                            conf {confidencePct(d.confidence)} · {d.periods} period(s) · {d.file_count} file(s)
                          </span>
                        </span>
                      </label>
                      <button
                        type="button"
                        className="text-[11px] text-ink-400 hover:text-err shrink-0"
                        onClick={() => toggleRemoveDeal(d.name)}
                        title={removed ? "Restore this discovered deal" : "Suppress this discovered deal at launch"}
                      >
                        {removed ? "restore" : "remove"}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          </Field>

          {/* added folders --------------------------------------------- */}
          <Field label="Add a deal folder the discovery missed">
            <div className="space-y-1.5">
              <Button kind="secondary" onClick={() => setFolderPicker(true)}>
                + Add a folder from the share
              </Button>
              {firm.addedFolders.map((p) => (
                <div key={p} className="flex items-center justify-between gap-2 bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-1.5">
                  <span className="font-mono text-[11px] text-ink-500 truncate">{p}</span>
                  <button type="button" className="text-[11px] text-ink-400 hover:text-err shrink-0" onClick={() => removeAddedFolder(p)}>
                    remove
                  </button>
                </div>
              ))}
            </div>
          </Field>

          {/* period ---------------------------------------------------- */}
          <div className="grid grid-cols-2 gap-4">
            <Field label="Period (date folders in the index)">
              <select className={inputCls} value={firm.period} onChange={(e) => set({ period: e.target.value })}>
                <option value="">— select period —</option>
                {(periods.data?.periods ?? []).map((p) => (
                  <option key={p.as_of_date} value={p.as_of_date}>
                    {p.label} ({p.as_of_date})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="…or type a period (Q1 2026, 2025-01-31)">
              <input className={inputCls} value={firm.period} onChange={(e) => set({ period: e.target.value })} placeholder="Q1 2026" />
            </Field>
          </div>

          {/* doc types ------------------------------------------------- */}
          <DocTypePicker value={firm.docTypes} onChange={(slugs) => set({ docTypes: slugs })} />

          {/* per-firm toggles ------------------------------------------ */}
          <div className="grid grid-cols-2 gap-4 pt-2 border-t border-line">
            <Toggle checked={firm.llmAssist} onChange={(v) => set({ llmAssist: v })} label="LLM-assisted deal discovery for this firm" />
            <Toggle
              checked={firm.enhancedPeriodCheck}
              onChange={(v) => set({ enhancedPeriodCheck: v })}
              label="Enhanced period check (flag misfiled documents)"
            />
            {firm.llmAssist && (
              <ModelEffortPicker
                models={models?.models}
                model={firm.dealSearchModel}
                onModel={(m) => set({ dealSearchModel: m })}
                modelLabel="Deal-search model"
              />
            )}
          </div>
        </div>
      )}

      {folderPicker && (
        <FolderPicker
          title={`Add a deal folder for ${firm.client}`}
          initial=""
          onClose={() => setFolderPicker(false)}
          onSelect={(path) => {
            if (!firm.addedFolders.includes(path)) set({ addedFolders: [...firm.addedFolders, path] });
            setFolderPicker(false);
          }}
        />
      )}
    </Card>
  );
}
