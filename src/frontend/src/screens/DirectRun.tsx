import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { emptyFieldEdits, FieldEdits, JobInfo, ModelsResponse, post, TemplateInspect } from "../lib/api";
import { useLoad } from "../lib/hooks";
import { Button, Card, CardHeader, Field, inputCls, Toggle } from "../components/ui";
import { FolderPicker } from "../components/FolderPicker";
import { TemplatePicker } from "../components/TemplatePicker";
import { getLastTemplate } from "../lib/lastTemplate";
import { ModelEffortPicker } from "../components/ModelEffortPicker";

const KNOWN_EXT = /\.(pdf|docx?|pptx?|xlsx?)$/i;

/** Direct Run: a pure batch data-extraction surface. Build a list of source
 *  documents (browse-multi or paste paths), pick any reference workbook, edit
 *  its field set if you like, and extract every file into ONE output workbook —
 *  no locator, no deal selection, no index. */
export default function DirectRun() {
  const navigate = useNavigate();
  const templates = useLoad<{ default_template: string }>("/api/templates");
  const models = useLoad<ModelsResponse>("/api/models");

  const [files, setFiles] = useState<string[]>([]);
  const [paste, setPaste] = useState("");
  const [picking, setPicking] = useState(false);
  const [template, setTemplate] = useState<string>(getLastTemplate());
  const [, setInspect] = useState<TemplateInspect | null>(null);
  const [fieldEdits, setFieldEdits] = useState<FieldEdits>(emptyFieldEdits());
  const templateInit = useRef(false);

  const [llmEnabled, setLlmEnabled] = useState(true);
  const [manual, setManual] = useState(false);
  const [model, setModel] = useState("opus");
  const [effort, setEffort] = useState("medium");
  const [budget, setBudget] = useState("");
  const [forceAssist, setForceAssist] = useState(false);

  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const defaultTemplate = templates.data?.default_template ?? null;

  useEffect(() => {
    if (templates.data && !templateInit.current && !template) {
      templateInit.current = true;
      setTemplate(templates.data.default_template);
    }
  }, [templates.data, template]);

  // The reference workbook defines the columns; switching it invalidates any
  // field edits made against the previous workbook.
  useEffect(() => {
    setFieldEdits(emptyFieldEdits());
  }, [template]);

  // Parse pasted paths, one per line. Windows "Copy as path" wraps paths in
  // double quotes (and multi-select copy gives several space-separated quoted
  // paths on one line) — extract those; otherwise take the trimmed line. Strips
  // surrounding quotes so a quoted path never becomes an invalid filename.
  const parsePaste = (text: string): string[] => {
    const out: string[] = [];
    for (const line of text.split(/\r?\n/)) {
      const quoted = line.match(/"[^"]+"|'[^']+'/g);
      if (quoted) {
        for (const q of quoted) out.push(q.slice(1, -1).trim());
      } else {
        const t = line.trim().replace(/^["']|["']$/g, "").trim();
        if (t) out.push(t);
      }
    }
    return out.filter(Boolean);
  };

  const mergePaths = (base: string[], incoming: string[]) => {
    const seen = new Set(base);
    const merged = [...base];
    for (const p of incoming) {
      if (p && !seen.has(p)) {
        seen.add(p);
        merged.push(p);
      }
    }
    return merged;
  };

  const addPaths = (incoming: string[]) => setFiles((prev) => mergePaths(prev, incoming));

  const addPasted = () => {
    addPaths(parsePaste(paste));
    setPaste("");
  };

  const removeFile = (path: string) => setFiles((prev) => prev.filter((p) => p !== path));

  const customNeedsLlm =
    (fieldEdits.added.length > 0 || fieldEdits.renamed.length > 0 || fieldEdits.removed.length > 0) &&
    !llmEnabled;
  // A path typed into the paste box but not yet "added" still counts — Run
  // flushes it — so the button isn't mysteriously disabled.
  const effectiveCount = files.length + parsePaste(paste).length;
  const canLaunch = effectiveCount > 0 && !customNeedsLlm && !launching;

  const doLaunch = async () => {
    if (!canLaunch) return;
    setError(null);
    setLaunching(true);
    const launchFiles = mergePaths(files, parsePaste(paste));
    setFiles(launchFiles);
    setPaste("");
    try {
      const r = await post<{ job: JobInfo }>("/api/jobs/run", {
        scope: "deal",
        period: "",
        direct_files: launchFiles,
        direct_client: null,
        direct_deal: null,
        template: template || null,
        field_edits: fieldEdits,
        dry_run: false,
        force: false,
        llm: {
          enabled: llmEnabled,
          routing_mode: llmEnabled ? (manual ? "single_model" : "auto") : null,
          mode: llmEnabled ? (manual ? "single_model" : "auto") : null,
          model: llmEnabled && manual ? model : null,
          effort: llmEnabled && manual ? effort : null,
          single_model:
            llmEnabled && manual
              ? { provider: models.data?.llm.provider ?? "claude", model, effort }
              : null,
          repair_policy: "never",
          budget_usd: llmEnabled && budget ? Number(budget) : null,
          force_llm_assist: llmEnabled && forceAssist,
        },
      });
      navigate(`/jobs/${r.job.id}/progress`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div className="max-w-3xl space-y-5">
      <div>
        <h1 className="text-[20px] font-semibold text-ink-900 tracking-tight">Direct Run</h1>
        <p className="text-[13px] text-ink-500 mt-1">
          Extract data from a batch of documents directly — no deal selection, no locator. Add the
          files (analyst reports, annual reports, transcripts, valuation memos…), choose where the
          results land, and launch. One output workbook covers the whole batch.
        </p>
      </div>

      <Card>
        <CardHeader title="Documents" sub="The files to extract. Browse to select several, or paste paths." />
        <div className="px-4 pb-4 space-y-3">
          <div className="flex gap-2">
            <Button kind="secondary" onClick={() => setPicking(true)}>
              Browse…
            </Button>
            <span className="text-[12px] text-ink-400 self-center">
              {files.length} file{files.length === 1 ? "" : "s"} queued
            </span>
          </div>

          <div>
            <textarea
              className={inputCls + " w-full font-mono text-[12px] min-h-[64px]"}
              value={paste}
              placeholder='Paste file paths, one per line (Windows "Copy as path" quotes are handled)'
              onChange={(e) => setPaste(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  addPasted();
                }
              }}
            />
            <div className="mt-1 flex justify-end">
              <Button kind="ghost" onClick={addPasted} disabled={!paste.trim()}>
                Add paths
              </Button>
            </div>
          </div>

          {files.length > 0 && (
            <ul className="rounded-[var(--hl-radius)] border border-line divide-y divide-line">
              {files.map((f) => (
                <li key={f} className="flex items-center gap-2 px-3 py-1.5">
                  {!KNOWN_EXT.test(f) && (
                    <span title="Unrecognized extension" className="text-warn">
                      ⚠
                    </span>
                  )}
                  <span className="flex-1 truncate font-mono text-[12px] text-ink-700" title={f}>
                    {f}
                  </span>
                  <button
                    className="text-ink-400 hover:text-danger text-[13px]"
                    title="Remove"
                    onClick={() => removeFile(f)}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Reference workbook"
          sub="Where the extracted fields are written. Add, rename, or remove fields below."
        />
        <div className="px-4 pb-4">
          <TemplatePicker
            value={template}
            defaultTemplate={defaultTemplate}
            llmEnabled={llmEnabled}
            onChange={setTemplate}
            onInspect={setInspect}
            fieldEdits={fieldEdits}
            onFieldEdits={setFieldEdits}
          />
        </div>
      </Card>

      <Card>
        <CardHeader title="AI / model" sub="The local LLM assist used for extraction." />
        <div className="px-4 pb-4 space-y-3">
          <Toggle label="Use LLM assist" checked={llmEnabled} onChange={setLlmEnabled} />
          {llmEnabled && (
            <div className="space-y-3 pt-1">
              <Toggle
                label="Pick model manually (otherwise auto-routed)"
                checked={manual}
                onChange={setManual}
              />
              {manual && (
                <div className="grid grid-cols-2 gap-3">
                  <ModelEffortPicker
                    models={models.data?.models}
                    model={model}
                    effort={effort}
                    onModel={setModel}
                    onEffort={setEffort}
                  />
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <Field label="Budget cap (USD, optional)">
                  <input
                    className={inputCls}
                    value={budget}
                    inputMode="decimal"
                    placeholder="e.g. 5"
                    onChange={(e) => setBudget(e.target.value)}
                  />
                </Field>
              </div>
              <Toggle
                label="LLM only — skip the deterministic algorithm, extract every field with the LLM"
                checked={forceAssist}
                onChange={setForceAssist}
              />
            </div>
          )}
        </div>
      </Card>

      {error && <p className="text-[13px] text-danger">{error}</p>}

      <div className="flex items-center gap-3">
        <Button kind="primary" disabled={!canLaunch} onClick={doLaunch}>
          {launching ? "Launching…" : `Run extraction${effectiveCount ? ` (${effectiveCount})` : ""}`}
        </Button>
        {effectiveCount === 0 && <span className="text-[12px] text-ink-400">Add at least one document.</span>}
        {customNeedsLlm && (
          <span className="text-[12px] text-danger">
            Editing fields requires LLM assist — enable it above.
          </span>
        )}
      </div>

      {picking && (
        <FolderPicker
          title="Choose documents to extract"
          initial={files[files.length - 1] || ""}
          pickFiles
          multiple
          onSelect={(path) => {
            addPaths([path]);
            setPicking(false);
          }}
          onSelectMany={(paths) => {
            addPaths(paths);
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </div>
  );
}
