import { useState } from "react";
import { ModelEntry, ModelsResponse, put } from "../lib/api";
import { DataTable } from "./DataTable";
import { Button, inputCls } from "./ui";

/** Sortable model cost table with inline-editable pricing overrides.
    Edits PUT to the backend, which round-trips config/models.yaml
    (comment-preserving). The GUI never computes costs itself. */
export function ModelPricingTable({
  data,
  loading,
  error,
  onRetry,
  onSaved,
}: {
  data: ModelsResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const startEdit = (m: ModelEntry) => {
    if (!m.pricing_per_mtok) return;
    setEditing(m.alias);
    setSaveError(null);
    setDraft({
      input: String(m.pricing_per_mtok.input),
      output: String(m.pricing_per_mtok.output),
      cache_hit: String(m.pricing_per_mtok.cache_hit),
      cache_write_5m: String(m.pricing_per_mtok.cache_write_5m),
      cache_write_1h: String(m.pricing_per_mtok.cache_write_1h),
    });
  };

  const save = async (alias: string) => {
    setSaving(true);
    setSaveError(null);
    try {
      await put(`/api/models/${alias}/pricing`, {
        input: Number(draft.input),
        output: Number(draft.output),
        cache_hit: Number(draft.cache_hit),
        cache_write_5m: Number(draft.cache_write_5m),
        cache_write_1h: Number(draft.cache_write_1h),
        last_reviewed: new Date().toISOString().slice(0, 10),
      });
      setEditing(null);
      onSaved();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  type PriceKey = "input" | "output" | "cache_hit" | "cache_write_5m" | "cache_write_1h";
  const priceCell = (m: ModelEntry, key: PriceKey) =>
    !m.pricing_per_mtok ? (
      <span className="text-[12px] text-ink-400">unavailable</span>
    ) :
    editing === m.alias ? (
      <input
        className={`${inputCls} w-20 text-right font-mono`}
        value={draft[key] ?? ""}
        onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
      />
    ) : (
      <span className="font-mono text-[12px]">${m.pricing_per_mtok[key].toFixed(2)}</span>
    );

  return (
    <div>
      <DataTable<ModelEntry>
        rows={data?.models ?? null}
        loading={loading}
        error={error}
        onRetry={onRetry}
        rowKey={(m) => m.alias}
        columns={[
          {
            key: "model",
            header: "Model",
            render: (m) => (
              <span>
                <span className="font-medium text-ink-900">{m.display_name}</span>
                {m.provider !== "claude" && (
                  <span className="ml-2 text-[10px] uppercase tracking-wide text-ink-400">{m.provider}</span>
                )}
                {m.requires_explicit_enable && (
                  <span className="ml-2 text-[10px] uppercase tracking-wide text-err">explicit enable</span>
                )}
              </span>
            ),
            sortValue: (m) => m.display_name,
          },
          {
            key: "alias",
            header: "Alias / ID",
            render: (m) => (
              <span className="font-mono text-[11.5px] text-ink-600">
                {m.alias}
                <br />
                {m.id}
              </span>
            ),
            sortValue: (m) => m.alias,
          },
          {
            key: "ctx",
            header: "Context",
            align: "right",
            render: (m) => <span className="font-mono text-[12px]">{m.context_window.toLocaleString()}</span>,
            sortValue: (m) => m.context_window,
          },
          { key: "in", header: "In $/1M", align: "right", render: (m) => priceCell(m, "input"), sortValue: (m) => m.pricing_per_mtok?.input ?? -1 },
          { key: "out", header: "Out $/1M", align: "right", render: (m) => priceCell(m, "output"), sortValue: (m) => m.pricing_per_mtok?.output ?? -1 },
          { key: "chit", header: "Cache read", align: "right", render: (m) => priceCell(m, "cache_hit"), sortValue: (m) => m.pricing_per_mtok?.cache_hit ?? -1 },
          { key: "cw5", header: "Cache write 5m", align: "right", render: (m) => priceCell(m, "cache_write_5m"), sortValue: (m) => m.pricing_per_mtok?.cache_write_5m ?? -1 },
          { key: "cw1h", header: "Cache write 1h", align: "right", render: (m) => priceCell(m, "cache_write_1h"), sortValue: (m) => m.pricing_per_mtok?.cache_write_1h ?? -1 },
          {
            key: "reviewed",
            header: "Last reviewed",
            render: () => <span className="text-[12px] text-ink-500">{data?.last_reviewed || "never"}</span>,
          },
          {
            key: "edit",
            header: "Override",
            render: (m) =>
              editing === m.alias ? (
                <span className="flex gap-1">
                  <Button kind="primary" disabled={saving} onClick={() => save(m.alias)}>
                    Save
                  </Button>
                  <Button kind="ghost" onClick={() => setEditing(null)}>
                    Cancel
                  </Button>
                </span>
              ) : (
                <Button kind="ghost" disabled={!m.pricing_per_mtok} onClick={() => startEdit(m)}>
                  Edit
                </Button>
              ),
          },
        ]}
      />
      {saveError && <p className="text-[12px] text-err px-4 pb-3">{saveError}</p>}
      <p className="text-[11px] text-ink-400 px-4 pb-3">
        Prices are editable ESTIMATES used for cost accounting when the CLI reports no usage — stored in{" "}
        <span className="font-mono">{data?.models_path}</span> (comments preserved).
      </p>
    </div>
  );
}
