import { useEffect, useRef, useState } from "react";
import { FieldEdits, fieldEditsEmpty, inspectTemplate, TemplateInspect } from "../lib/api";
import { setLastTemplate } from "../lib/lastTemplate";
import { EditableFieldList } from "./EditableFieldList";
import { FolderPicker } from "./FolderPicker";
import { Button, inputCls } from "./ui";

/** Reference-workbook picker with live inspection.
 *
 *  The analyst types or browses to any workbook. On every change we call
 *  /api/templates/inspect, which autodetects the worksheet, the fields it will
 *  populate, and the identity columns it will prepend — and reports whether the
 *  sheet is ready for the next step. This is the dynamic, "show me what you'll
 *  do" replacement for a bare path input: a custom workbook with brand-new
 *  headers is supported (extracted LLM-first); the master template is detected
 *  and reported as the deterministic path. The parent owns the path string and
 *  receives the inspection so it can gate Next on `inspect.ready`. */
export function TemplatePicker({
  value,
  defaultTemplate,
  llmEnabled,
  onChange,
  onInspect,
  fieldEdits,
  onFieldEdits,
}: {
  value: string;
  defaultTemplate: string | null;
  llmEnabled: boolean;
  onChange: (path: string) => void;
  onInspect?: (inspect: TemplateInspect | null) => void;
  // When onFieldEdits is provided, the detected-field list becomes editable
  // (add/rename/remove) and emits a FieldEdits payload. Absent = read-only.
  fieldEdits?: FieldEdits;
  onFieldEdits?: (edits: FieldEdits) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [inspect, setInspect] = useState<TemplateInspect | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reqRef = useRef(0);

  useEffect(() => {
    if (!value) {
      setInspect(null);
      onInspect?.(null);
      return;
    }
    const token = ++reqRef.current;
    setLoading(true);
    setError(null);
    const handle = window.setTimeout(() => {
      inspectTemplate(value)
        .then((r) => {
          if (token !== reqRef.current) return;
          setInspect(r);
          onInspect?.(r);
        })
        .catch((e: Error) => {
          if (token !== reqRef.current) return;
          setInspect(null);
          onInspect?.(null);
          setError(e.message);
        })
        .finally(() => {
          if (token === reqRef.current) setLoading(false);
        });
    }, 250);
    return () => window.clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  // Persist every non-empty pick so New Run / Direct Run can prefill it next
  // time instead of forcing a fresh browse.
  const change = (path: string) => {
    setLastTemplate(path);
    onChange(path);
  };

  // Editing the field set switches a run to the custom / LLM-first path, so an
  // edited (even master) workbook needs LLM assist just like a custom one.
  const hasEdits = !!fieldEdits && !fieldEditsEmpty(fieldEdits);
  const effectiveCustom = !!inspect?.is_custom || hasEdits;
  const customNeedsLlm = effectiveCustom && !llmEnabled;
  const ready = !!inspect?.ready && !customNeedsLlm;

  return (
    <div>
      <div className="flex gap-2 items-center">
        <input
          className={inputCls + " flex-1"}
          value={value}
          placeholder="Path to a reference workbook (.xlsx)"
          onChange={(e) => onChange(e.target.value)}
        />
        <Button kind="secondary" onClick={() => setPicking(true)}>
          Browse…
        </Button>
        {defaultTemplate && value !== defaultTemplate && (
          <Button kind="ghost" onClick={() => change(defaultTemplate)}>
            Use local default
          </Button>
        )}
      </div>

      {loading && <p className="mt-3 text-[12px] text-ink-400">Inspecting workbook…</p>}
      {error && (
        <p className="mt-3 text-[12px] text-danger">Could not inspect workbook: {error}</p>
      )}

      {inspect && !loading && (
        <div className="mt-3 rounded-[var(--hl-radius)] border border-line bg-paper-soft px-4 py-3">
          <div className="flex items-center justify-between">
            <div className="text-[13px] text-ink-800">
              <span className="font-medium">{inspect.sheet_name}</span>
              <span className="text-ink-400">
                {" "}
                · {inspect.field_count} field{inspect.field_count === 1 ? "" : "s"} ·{" "}
                {effectiveCustom
                  ? inspect.is_custom
                    ? "custom reference (LLM-first)"
                    : "master template (edited · LLM-first)"
                  : "master template"}
              </span>
            </div>
            <span
              className={`text-[11px] px-2 py-0.5 rounded-full ${
                ready
                  ? "bg-globe/15 text-globe"
                  : "bg-danger/10 text-danger"
              }`}
            >
              {ready ? "Ready ✓" : "Not ready"}
            </span>
          </div>

          {inspect.sheets.length > 1 && (
            <p className="mt-2 text-[11px] text-ink-400">
              Worksheets: {inspect.sheets.join(", ")} — using {inspect.sheet_name}
            </p>
          )}

          {inspect.prepended_admin.length > 0 && (
            <p className="mt-2 text-[12px] text-ink-500">
              Identity columns prepended at the front:{" "}
              <span className="text-ink-700">{inspect.prepended_admin.join(", ")}</span>
            </p>
          )}

          {onFieldEdits ? (
            <EditableFieldList
              inspect={inspect}
              value={fieldEdits ?? { added: [], renamed: [], removed: [] }}
              onChange={onFieldEdits}
            />
          ) : (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {inspect.fields.slice(0, 40).map((f) => (
                <span
                  key={f.header}
                  title={`${f.dtype} · ${f.band}`}
                  className={`text-[11px] px-1.5 py-0.5 rounded ${
                    f.band === "IDENTIFICATION"
                      ? "bg-ink-100 text-ink-500"
                      : "bg-globe/10 text-ink-700"
                  }`}
                >
                  {f.header}
                </span>
              ))}
              {inspect.fields.length > 40 && (
                <span className="text-[11px] text-ink-400 px-1">
                  +{inspect.fields.length - 40} more
                </span>
              )}
            </div>
          )}

          {inspect.messages.map((m, i) => (
            <p key={i} className="mt-2 text-[12px] text-ink-500">
              {m}
            </p>
          ))}
          {customNeedsLlm && (
            <p className="mt-2 text-[12px] text-danger">
              A custom reference workbook is extracted LLM-first — enable LLM assist to run it.
            </p>
          )}
        </div>
      )}

      {picking && (
        <FolderPicker
          title="Choose a reference workbook"
          initial={value || defaultTemplate || ""}
          pickFiles
          onSelect={(path) => {
            change(path);
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </div>
  );
}
