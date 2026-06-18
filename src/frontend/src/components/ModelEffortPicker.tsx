import { ModelEntry } from "../lib/api";
import { Field, inputCls } from "./ui";

/** The five Claude Code reasoning-effort levels (model_registry.EFFORT_LEVELS). */
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

const CUSTOM = "__custom__";

/** Model + effort selector used everywhere a Claude model is chosen.
 *
 *  The model list comes from the live catalog (GET /api/models, sourced from
 *  config/models.yaml). Aliases (sonnet/opus/haiku/fable) FLOAT to the newest
 *  tier as the local `claude` CLI updates — so picking "opus" automatically
 *  uses Opus 4.9 once it ships and the CLI is updated, no app change needed.
 *  "Custom…" lets you type ANY model id (a pinned historical id, or a brand-new
 *  one the catalog hasn't been told about yet). Effort is always selectable. */
export function ModelEffortPicker({
  models,
  model,
  effort,
  onModel,
  onEffort,
  modelLabel = "Model",
  effortLabel = "Effort",
  compact = false,
}: {
  models: ModelEntry[] | undefined;
  model: string;
  effort?: string;
  onModel: (m: string) => void;
  onEffort?: (e: string) => void;
  modelLabel?: string;
  effortLabel?: string;
  compact?: boolean;
}) {
  const catalog = models ?? [];
  const known = new Set<string>([...catalog.map((m) => m.alias), ...catalog.map((m) => m.id)]);
  const isCustom = model !== "" && !known.has(model);

  return (
    <div className={compact ? "flex items-end gap-3" : "grid grid-cols-2 gap-3"}>
      <Field label={modelLabel}>
        <select
          className={inputCls}
          value={isCustom ? CUSTOM : model}
          onChange={(e) => onModel(e.target.value === CUSTOM ? "" : e.target.value)}
        >
          {catalog.map((m) => (
            <option key={m.alias} value={m.alias}>
              {m.alias} — {m.display_name}
              {m.requires_explicit_enable ? " (opt-in)" : ""}
            </option>
          ))}
          {/* pinned full ids, so a specific historical model can be chosen */}
          {catalog.filter((m) => m.id !== m.alias).map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
          <option value={CUSTOM}>Custom model id…</option>
        </select>
        {isCustom && (
          <input
            className={`${inputCls} mt-1`}
            value={model}
            placeholder="e.g. claude-opus-4-9 or a future model id"
            onChange={(e) => onModel(e.target.value)}
            autoFocus
          />
        )}
      </Field>
      {onEffort && (
        <Field label={effortLabel}>
          <select className={inputCls} value={effort ?? "medium"} onChange={(e) => onEffort(e.target.value)}>
            {EFFORTS.map((e) => (
              <option key={e}>{e}</option>
            ))}
          </select>
        </Field>
      )}
    </div>
  );
}
