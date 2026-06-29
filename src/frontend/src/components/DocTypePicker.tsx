import { useMemo, useState } from "react";
import { DocTypeProfile, ProfileResolveResponse, post } from "../lib/api";
import { useLoad } from "../lib/hooks";
import { Button, Field, inputCls } from "./ui";

/* Multi-select for the document-type profiles a firm should search for.
   Choices come from the Smart Search profile catalog (builtins + learned,
   GET /api/search/profiles) and the analyst can also describe a NEW profile
   in prose, which POST /api/search/profiles/resolve previews + adds by slug.
   The selection is a list of profile slugs persisted on the FirmEntry. An
   empty selection means "use the config default doc type". */
export function DocTypePicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (slugs: string[]) => void;
}) {
  const profiles = useLoad<{ profiles: DocTypeProfile[] }>("/api/search/profiles");
  const [resolveQuery, setResolveQuery] = useState("");
  const [resolved, setResolved] = useState<ProfileResolveResponse | null>(null);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const selected = useMemo(() => new Set(value), [value]);
  const catalog = profiles.data?.profiles ?? [];

  // Slugs the analyst selected that aren't in the catalog (e.g. a freshly
  // resolved profile, or a builtin DocType passed in) — show them too.
  const extraSlugs = value.filter((s) => !catalog.some((p) => p.slug === s));

  const toggle = (slug: string) => {
    const next = new Set(selected);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    onChange([...next]);
  };

  const resolve = async () => {
    if (!resolveQuery.trim()) return;
    setResolving(true);
    setResolveError(null);
    setResolved(null);
    try {
      const r = await post<ProfileResolveResponse>("/api/search/profiles/resolve", {
        query: resolveQuery.trim(),
      });
      setResolved(r);
    } catch (e) {
      setResolveError((e as Error).message);
    } finally {
      setResolving(false);
    }
  };

  const chipCls = (active: boolean) =>
    `px-2.5 py-1 rounded-[var(--hl-radius)] text-[12px] border transition-colors ${
      active
        ? "bg-navy text-white border-navy"
        : "bg-paper text-ink-700 border-line-strong hover:bg-ink-50"
    }`;

  return (
    <div className="space-y-2">
      <Field label="Document types (Smart Search profiles · empty = config default)">
        <div className="flex flex-wrap gap-1.5">
          {profiles.loading && <span className="text-[11.5px] text-ink-400">loading profiles…</span>}
          {profiles.error && <span className="text-[11.5px] text-err">{profiles.error}</span>}
          {catalog.map((p) => (
            <button key={p.slug} type="button" className={chipCls(selected.has(p.slug))} onClick={() => toggle(p.slug)} title={p.slug}>
              {p.label}
            </button>
          ))}
          {extraSlugs.map((s) => (
            <button key={s} type="button" className={chipCls(true)} onClick={() => toggle(s)} title={s}>
              {s}
            </button>
          ))}
        </div>
      </Field>

      <div className="flex items-end gap-2">
        <Field label="＋ search by description">
          <input
            className={inputCls}
            value={resolveQuery}
            onChange={(e) => setResolveQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                resolve();
              }
            }}
            placeholder="e.g. quarterly valuation memo, IC approval deck…"
          />
        </Field>
        <Button kind="secondary" onClick={resolve} disabled={resolving || !resolveQuery.trim()}>
          {resolving ? "Resolving…" : "Resolve"}
        </Button>
      </div>
      {resolveError && <p className="text-[12px] text-err">{resolveError}</p>}
      {resolved && (
        <div className="bg-surface border border-line rounded-[var(--hl-radius)] px-3 py-2 text-[12px] space-y-1">
          <div className="flex items-center justify-between gap-3">
            <p className="text-ink-700">
              <span className="font-semibold">{resolved.spec.label}</span>{" "}
              <span className="font-mono text-[11px] text-ink-400">{resolved.spec.slug}</span>
            </p>
            <Button
              kind="primary"
              disabled={selected.has(resolved.spec.slug)}
              onClick={() => {
                if (!selected.has(resolved.spec.slug)) onChange([...value, resolved.spec.slug]);
              }}
            >
              {selected.has(resolved.spec.slug) ? "Added" : "Add this profile"}
            </Button>
          </div>
          <p className="text-[11px] text-ink-400">provenance: {resolved.provenance}</p>
          {resolved.spec.filename_include.length > 0 && (
            <p className="text-[11px] text-ink-500">
              filename includes: <span className="font-mono">{resolved.spec.filename_include.join(", ")}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
