import { useState } from "react";
import { useLoad } from "../lib/hooks";
import { Button, Field, inputCls } from "./ui";

interface Profile {
  slug: string;
  label: string;
  builtin?: number;
}

const OTHER = "__other__";

/** Build a list of document types for a run: pick from the prewritten catalog
 *  (Title-Cased) or "Other — type your own" free text, with a removable chip
 *  list. An empty list = the broad "any client valuation document" default. */
export function DocTypeListBuilder({
  value,
  onChange,
}: {
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const profiles = useLoad<{ profiles: Profile[] }>("/api/search/profiles");
  const [picking, setPicking] = useState("");
  const [other, setOther] = useState("");
  const list = profiles.data?.profiles ?? [];
  const labelFor = (slug: string) => list.find((p) => p.slug === slug)?.label ?? slug;

  const add = (slug: string) => {
    const s = slug.trim();
    if (s && !value.includes(s)) onChange([...value, s]);
  };
  const addOther = () => {
    if (other.trim()) {
      add(other.trim());
      setOther("");
      setPicking("");
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-end gap-2 flex-wrap">
        <Field label="Document types (add one or more)">
          <select
            className={inputCls}
            value={picking}
            onChange={(e) => {
              const v = e.target.value;
              if (v && v !== OTHER) {
                add(v);
                setPicking("");
              } else {
                setPicking(v);
              }
            }}
          >
            <option value="">Add a document type…</option>
            <option value={OTHER}>Other — type your own</option>
            {list
              .filter((p) => p.slug !== "any_client_valuation_doc")
              .map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.label}
                </option>
              ))}
          </select>
        </Field>
        {picking === OTHER && (
          <div className="flex items-end gap-1">
            <Field label="Custom document type">
              <input
                className={inputCls}
                value={other}
                placeholder="e.g. ESG Report"
                autoFocus
                onChange={(e) => setOther(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addOther();
                  }
                }}
              />
            </Field>
            <Button kind="secondary" onClick={addOther}>
              Add
            </Button>
          </div>
        )}
      </div>
      {value.length === 0 ? (
        <p className="text-[12px] text-ink-400">
          None selected — defaults to <b>any client valuation document</b>.
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {value.map((slug) => (
            <span
              key={slug}
              className="inline-flex items-center gap-1 px-2 py-0.5 text-[12px] bg-surface border border-line rounded"
            >
              {labelFor(slug)}
              <button
                type="button"
                className="text-ink-400 hover:text-err text-[11px]"
                onClick={() => onChange(value.filter((s) => s !== slug))}
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
