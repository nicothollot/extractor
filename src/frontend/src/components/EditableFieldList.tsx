import { useMemo, useState } from "react";
import { FieldEdits, TemplateInspect, fieldEditsEmpty } from "../lib/api";
import { Button, inputCls } from "./ui";

interface Row {
  header: string; // current (post-rename) header
  origin: "detected" | "added";
  originalHeader: string; // detected: source header; added: same as header
  band: string;
  dtype: string;
  locked: boolean; // identity/admin columns cannot be renamed/removed
}

const norm = (s: string) => s.trim().toLowerCase();
const COLLAPSED = 40;

/** Editable view of a reference workbook's field set. Detected columns +
 *  manually-added fields are shown as a single list; hovering a row reveals
 *  Edit (rename) and X (remove). Identity columns are locked. Emits a sparse
 *  FieldEdits payload the run applies. Used by Direct Run and the New Run
 *  Template step. */
export function EditableFieldList({
  inspect,
  value,
  onChange,
}: {
  inspect: TemplateInspect | null;
  value: FieldEdits;
  onChange: (edits: FieldEdits) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [adding, setAdding] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [error, setError] = useState<string | null>(null);

  const renameMap = useMemo(() => {
    const m = new Map<string, string>();
    value.renamed.forEach((r) => m.set(norm(r.from), r.to));
    return m;
  }, [value.renamed]);
  const removedSet = useMemo(() => new Set(value.removed.map(norm)), [value.removed]);

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    for (const f of inspect?.fields ?? []) {
      if (removedSet.has(norm(f.header))) continue;
      const locked = f.band === "IDENTIFICATION";
      const renamed = !locked ? renameMap.get(norm(f.header)) : undefined;
      out.push({
        header: renamed ?? f.header,
        origin: "detected",
        originalHeader: f.header,
        band: f.band,
        dtype: f.dtype,
        locked,
      });
    }
    for (const a of value.added) {
      out.push({
        header: a.header,
        origin: "added",
        originalHeader: a.header,
        band: "REFERENCE",
        dtype: a.dtype ?? "auto",
        locked: false,
      });
    }
    return out;
  }, [inspect?.fields, value.added, renameMap, removedSet]);

  const headerExists = (h: string, exceptKey?: string) =>
    rows.some((r) => keyOf(r) !== exceptKey && norm(r.header) === norm(h));

  if (!inspect) return null;

  const addField = () => {
    const header = adding.trim();
    if (!header) return;
    if (headerExists(header)) {
      setError(`"${header}" already exists.`);
      return;
    }
    setError(null);
    setAdding("");
    onChange({ ...value, added: [...value.added, { header }] });
  };

  const removeRow = (row: Row) => {
    if (row.locked) return;
    if (row.origin === "added") {
      onChange({ ...value, added: value.added.filter((a) => norm(a.header) !== norm(row.header)) });
    } else {
      // Drop any rename for this column and mark its ORIGINAL header removed.
      onChange({
        ...value,
        renamed: value.renamed.filter((r) => norm(r.from) !== norm(row.originalHeader)),
        removed: [...value.removed, row.originalHeader],
      });
    }
  };

  const commitRename = (row: Row, raw: string) => {
    const next = raw.trim();
    setEditingKey(null);
    if (!next || next === row.header) return;
    if (headerExists(next, keyOf(row))) {
      setError(`"${next}" already exists.`);
      return;
    }
    setError(null);
    if (row.origin === "added") {
      onChange({
        ...value,
        added: value.added.map((a) =>
          norm(a.header) === norm(row.header) ? { ...a, header: next } : a,
        ),
      });
    } else {
      const others = value.renamed.filter((r) => norm(r.from) !== norm(row.originalHeader));
      // Renaming back to the original header clears the edit.
      const renamed =
        norm(next) === norm(row.originalHeader)
          ? others
          : [...others, { from: row.originalHeader, to: next }];
      onChange({ ...value, renamed });
    }
  };

  const shown = expanded ? rows : rows.slice(0, COLLAPSED);
  const edited = !fieldEditsEmpty(value);

  return (
    <div className="mt-3">
      <div className="flex items-center justify-between">
        <p className="text-[12px] text-ink-500">
          {rows.length} field{rows.length === 1 ? "" : "s"}
          {edited && <span className="text-globe"> · edited</span>}
        </p>
        {rows.length > COLLAPSED && (
          <button
            className="text-[11.5px] text-globe hover:underline"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "Collapse" : `Extend fields (${rows.length})`}
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {shown.map((r) => {
          const key = keyOf(r);
          const editing = editingKey === key;
          if (editing) {
            return (
              <input
                key={key}
                autoFocus
                className={`${inputCls} text-[11px] py-0.5 px-1.5 w-44`}
                value={editingText}
                onChange={(e) => setEditingText(e.target.value)}
                onBlur={() => commitRename(r, editingText)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename(r, editingText);
                  if (e.key === "Escape") setEditingKey(null);
                }}
              />
            );
          }
          return (
            <span
              key={key}
              title={`${r.dtype} · ${r.band}${r.origin === "added" ? " · added" : ""}`}
              className={`group inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded ${
                r.locked
                  ? "bg-ink-100 text-ink-500"
                  : r.origin === "added"
                  ? "bg-globe/20 text-ink-800"
                  : "bg-globe/10 text-ink-700"
              }`}
            >
              {r.header}
              {!r.locked && (
                <span className="hidden group-hover:inline-flex items-center gap-1">
                  <button
                    className="text-ink-400 hover:text-ink-800"
                    title="Edit field name"
                    onClick={() => {
                      setEditingKey(key);
                      setEditingText(r.header);
                    }}
                  >
                    ✎
                  </button>
                  <button
                    className="text-ink-400 hover:text-danger"
                    title="Remove field"
                    onClick={() => removeRow(r)}
                  >
                    ✕
                  </button>
                </span>
              )}
            </span>
          );
        })}
      </div>

      <div className="mt-3 flex gap-2 items-center">
        <input
          className={`${inputCls} flex-1 text-[12px]`}
          value={adding}
          placeholder="Add a field — type a name and press Enter"
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addField();
            }
          }}
        />
        <Button kind="secondary" onClick={addField} disabled={!adding.trim()}>
          Add
        </Button>
      </div>
      {error && <p className="mt-2 text-[12px] text-danger">{error}</p>}
      {edited && !inspect.is_custom && (
        <p className="mt-2 text-[12px] text-ink-500">
          Editing fields switches this run to LLM-first extraction (LLM assist required).
        </p>
      )}
    </div>
  );
}

const keyOf = (r: Row) => `${r.origin}:${r.originalHeader}`;
