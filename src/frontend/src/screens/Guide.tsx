import { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Card, CardHeader, Panel } from "../components/ui";

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <Card>
      <div className="px-4 py-3 flex items-start gap-3">
        <span className="shrink-0 w-7 h-7 rounded-full bg-navy text-white text-[13px] font-semibold flex items-center justify-center mt-0.5">
          {n}
        </span>
        <div className="min-w-0">
          <p className="text-[13.5px] font-semibold text-ink-900">{title}</p>
          <div className="mt-1 text-[12.5px] text-ink-600 leading-relaxed space-y-1.5">{children}</div>
        </div>
      </div>
    </Card>
  );
}

const kbd = "inline-block px-1.5 py-0.5 text-[11px] font-mono bg-surface border border-line rounded";

export default function Guide() {
  return (
    <Panel className="space-y-4 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold text-ink-900">Guide</h1>
        <p className="text-[12.5px] text-ink-500 mt-1">
          PV Extractor finds client-provided valuation documents (IC memos, valuation memos, portfolio reviews) on the
          PV share and extracts ~600 structured fields per memo into the master index workbook. Every extracted value
          carries verbatim evidence, its page, and a confidence score — and the share is <b>never written to</b>.
        </p>
      </div>

      <Step n={1} title="One-time setup — tell it where things live">
        <p>
          Go to <Link className="text-[var(--hl-blue)] underline" to="/settings">Settings</Link> →{" "}
          <b>Locations &amp; file index</b>. Use <b>Browse…</b> to pick the <b>PV share root</b> (where the client
          folders live) and an <b>Output directory</b> (where workbooks, logs and audits land). Click{" "}
          <b>Save changes</b>.
        </p>
        <p>
          Optional: the <b>Claude Code</b> card configures the local LLM fallback. It needs the <code>claude</code> CLI
          logged in once (<code>claude auth login</code>) — but every run also works with the LLM disabled; fields the
          deterministic engine can&apos;t extract confidently simply stay as review flags.
        </p>
      </Step>

      <Step n={2} title="Index the clients you work on">
        <p>
          The New Run dropdowns are driven by a local index of the share — and you never need to index all of it. In{" "}
          <b>Settings → Locations &amp; file index</b>, tick the client folders you need and click{" "}
          <b>Scan selected</b>: only those folders are walked, with a live counter and progress while it runs. Add more
          clients any time; re-scans of already-indexed folders are incremental and fast.
        </p>
        <p>
          For a complete index in one go: <code>pv-extractor ingest-xlsx &lt;export.xlsx&gt;</code> bulk-loads the PV
          index export in seconds. (<b>Scan everything</b> also exists, but on the full share it can take hours.)
        </p>
      </Step>

      <Step n={3} title="Find the deal — three discovery modes">
        <p>
          In <Link className="text-[var(--hl-blue)] underline" to="/new-run">New Run</Link> → Scope, deals are{" "}
          <i>discovered</i>, not assumed. Pick the discovery mode that fits:
        </p>
        <p>
          <b>Browse</b> — choose from the deal folders found during the index scan (each with a confidence chip).{" "}
          <b>Search by name</b> — type a name for fuzzy matches with their full folder path. <b>LLM assist</b> — a local
          Claude Code session maps the client folder and proposes its deals; pick the <b>model and effort</b> (aliases
          like <code>sonnet</code>/<code>opus</code> float to the newest tier as your <code>claude</code> updates, or
          type a custom model id). LLM-assisted results are <b>saved</b> — Browse shows them next time, and re-running
          warns you that a saved discovery already exists.
        </p>
        <p>
          Too many low-confidence folders? <Link className="text-[var(--hl-blue)] underline" to="/settings">Settings →
          Deal discovery</Link> sets the confidence floor for what New Run shows (default 70%); lowering it reveals more
          without ever dropping folders from the index.
        </p>
      </Step>

      <Step n={4} title="Run the extraction">
        <p>
          Set the <b>valuation period</b> and <b>document type</b> (<i>any client valuation doc</i> is the safe
          default). On the <b>AI / model</b> step, AUTO routing is fine for most runs; switch to MANUAL to force a
          specific model + effort, or turn on <b>Force LLM assist</b> to extract with the model instead of just the
          deterministic engine. Keep the reference <b>template</b> for a fresh workbook, or pick a previous output to
          append cumulatively. Review the preflight cost <i>ESTIMATE</i>, then launch.
        </p>
        <p>
          The Run Progress screen shows each memo moving through locate → verify → extract → validate, the LLM cost
          meter, and a log tail. Cancel is graceful — finished memos are kept, the rest are marked deferred.
        </p>
      </Step>

      <Step n={5} title="Confirm the right documents (before the run)">
        <p>
          If the locator wasn&apos;t sure which file was the right memo, you catch it before extraction: New Run&apos;s{" "}
          <b>Confirm documents</b> step (after preflight) lists every auto-selected file with its alternatives — swap,
          remove or add a file there. <b>Use this one</b> forces your pick to run even if content verification would
          normally reject it (e.g. an HL work-product file): the row still gets a <b>MANUAL OVERRIDE</b> flag so it&apos;s
          reviewable, but your choice is honored. Picks are remembered for future runs (view/clear under{" "}
          <Link className="text-[var(--hl-blue)] underline" to="/settings">Settings → Learned locator overrides</Link>).
        </p>
      </Step>

      <Step n={6} title="Review what the run flagged">
        <p>
          Open the <Link className="text-[var(--hl-blue)] underline" to="/review">Review Queue</Link> (the picker lists
          each run with its first clients + memo count). Each item shows the extracted value, its verbatim evidence and
          the source page image with the evidence highlighted; a failed memo shows a <b>Why this memo failed QA</b> box.
          Keyboard: <span className={kbd}>j</span>/<span className={kbd}>k</span> next/previous ·{" "}
          <span className={kbd}>a</span> accept · <span className={kbd}>e</span> edit · <span className={kbd}>v</span> add
          value · <span className={kbd}>u</span> unresolvable.
        </p>
        <p>
          <b>Add value</b> opens the document: page through it, drag a highlight over the value (on text pages the
          highlighted words are captured as the verbatim evidence; on scanned pages it&apos;s a marker box), type the
          value, and it&apos;s written to the workbook with that page + region as provenance. Use <b>Accept all
          pending</b> (or per-category bulk accept) once you&apos;ve sampled enough. Every action is recorded in the
          memo&apos;s audit file.
        </p>
      </Step>

      <Step n={7} title="Get the output">
        <p>
          The <Link className="text-[var(--hl-blue)] underline" to="/output">Output Browser</Link> has the master index
          workbook for each run (one row per memo-asset, plus Review Flags and a Run Log sheet) and the per-memo audit
          JSONs. The run detail shows exactly when the run <b>started</b>, <b>finished</b> and how long it took, plus the{" "}
          <b>source document paths</b> it extracted from. Re-running against the same output workbook is idempotent —
          already-extracted memos are skipped.
        </p>
      </Step>

      <Card>
        <CardHeader title="If something looks wrong" sub="the three most common situations" />
        <div className="px-4 pb-4 text-[12.5px] text-ink-600 space-y-1.5">
          <p>
            <b>“no file index yet”</b> — step 2 hasn&apos;t run. Settings → Scan &amp; build index.
          </p>
          <p>
            <b>“pv_root is not reachable from this machine”</b> — the share path is wrong or the share isn&apos;t
            accessible (VPN / network). On Windows use the UNC path directly (<code>\\hlhz\dfs\nyfva\PV</code>); under
            WSL the share must be mounted first and pv_root pointed at the mount.
          </p>
          <p>
            <b>LLM_DEFERRED flags</b> — the run hit its LLM budget cap; raise it in Settings → LLM routing (or re-run
            just that memo). The deterministic results are unaffected.
          </p>
        </div>
      </Card>
    </Panel>
  );
}
