import { useEffect, useMemo, useState } from "react";
import { JobEvent } from "../lib/api";
import { fmtDuration } from "../lib/hooks";
import { Button, Card, CardHeader, StatusChip } from "./ui";

type LlmStatus = "started" | "message" | "finished" | "failed" | "cache_hit" | "deferred" | string;

interface LlmMessage {
  ts: string;
  stream: string;
  message: string;
}

interface LlmCall {
  id: string;
  firstSeq: number;
  startedAt: string | null;
  finishedAt: string | null;
  status: LlmStatus;
  payload: Record<string, unknown>;
  messages: LlmMessage[];
}

const terminalStatuses = new Set(["finished", "failed", "cache_hit", "deferred"]);

const asString = (value: unknown, fallback = "") => (typeof value === "string" ? value : fallback);
const asNumber = (value: unknown, fallback = 0) => (typeof value === "number" && Number.isFinite(value) ? value : fallback);
const asArray = (value: unknown): Record<string, unknown>[] => (Array.isArray(value) ? value.filter((v) => v && typeof v === "object") as Record<string, unknown>[] : []);

function statusTone(status: string) {
  if (status === "finished" || status === "cache_hit") return "bg-ok";
  if (status === "failed" || status === "deferred") return "bg-err";
  if (status === "started" || status === "message") return "bg-info";
  return "bg-ink-300";
}

function buildCalls(events: JobEvent[]): LlmCall[] {
  const calls = new Map<string, LlmCall>();
  for (const event of events) {
    if (event.type !== "llm_activity") continue;
    const payload = event.payload;
    const callId = asString(payload.call_id, `llm-${event.seq}`);
    const status = asString(payload.status, "message");
    const existing = calls.get(callId);
    const call: LlmCall = existing ?? {
      id: callId,
      firstSeq: event.seq,
      startedAt: null,
      finishedAt: null,
      status,
      payload: {},
      messages: [],
    };
    call.status = status;
    call.payload = { ...call.payload, ...payload };
    if (status === "started" && call.startedAt === null) call.startedAt = event.ts;
    if (status === "cache_hit" && call.startedAt === null) call.startedAt = event.ts;
    if (terminalStatuses.has(status)) call.finishedAt = event.ts;
    if (status === "message") {
      call.messages.push({
        ts: event.ts,
        stream: asString(payload.stream, "provider"),
        message: asString(payload.message, ""),
      });
    }
    calls.set(callId, call);
  }
  return [...calls.values()].sort((a, b) => a.firstSeq - b.firstSeq);
}

function elapsedSeconds(call: LlmCall, now: number) {
  const start = call.startedAt ? Date.parse(call.startedAt) : null;
  if (start === null || Number.isNaN(start)) return 0;
  const end = call.finishedAt ? Date.parse(call.finishedAt) : now;
  return Math.max(0, (end - start) / 1000);
}

function CallRow({
  call,
  selected,
  onSelect,
  now,
}: {
  call: LlmCall;
  selected: boolean;
  onSelect: () => void;
  now: number;
}) {
  const elapsed = elapsedSeconds(call, now);
  const timeout = asNumber(call.payload.timeout_seconds, 0);
  const pct = terminalStatuses.has(call.status)
    ? 100
    : timeout > 0
      ? Math.max(4, Math.min(98, (elapsed / timeout) * 100))
      : 12;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left px-3 py-2 border-b border-line last:border-b-0 hover:bg-ink-50 ${selected ? "bg-info-soft" : ""}`}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] text-ink-800 truncate">
            <span className="font-medium">{asString(call.payload.provider, "llm")}</span>{" "}
            <span className="text-ink-500">{asString(call.payload.model, "model")} / {asString(call.payload.effort, "effort")}</span>
          </p>
          <p className="text-[11px] text-ink-500 truncate">
            {asString(call.payload.memo_id, call.id)} · {asNumber(call.payload.fields_requested, 0)} fields ·{" "}
            {asNumber(call.payload.selected_page_count, 0)} pages
          </p>
        </div>
        <span className="shrink-0 text-[11px] font-mono text-ink-500">{fmtDuration(elapsed)}</span>
      </div>
      <div className="mt-2 h-1.5 rounded bg-ink-100 overflow-hidden">
        <div className={`h-full ${statusTone(call.status)}`} style={{ width: `${pct}%` }} />
      </div>
    </button>
  );
}

function CallDetails({ call, now }: { call: LlmCall; now: number }) {
  const docs = asArray(call.payload.documents);
  const pages = asArray(call.payload.payload_pages);
  const imagePaths = Array.isArray(call.payload.image_paths) ? call.payload.image_paths.map(String) : [];
  const usage = call.payload.usage && typeof call.payload.usage === "object" ? call.payload.usage as Record<string, unknown> : null;
  return (
    <div className="px-4 pb-4 space-y-4">
      <div className="grid grid-cols-2 gap-3 text-[12px]">
        <div>
          <p className="text-ink-500">Call</p>
          <p className="font-mono text-ink-800 break-all">{call.id}</p>
        </div>
        <div>
          <p className="text-ink-500">Elapsed</p>
          <p className="font-mono text-ink-800">{fmtDuration(elapsedSeconds(call, now))}</p>
        </div>
        <div>
          <p className="text-ink-500">Prompt</p>
          <p className="font-mono text-ink-800 break-all">{asString(call.payload.prompt_path, "not written")}</p>
        </div>
        <div>
          <p className="text-ink-500">Result</p>
          <p className="font-mono text-ink-800">
            {usage ? `${asNumber(usage.input_tokens)} in / ${asNumber(usage.output_tokens)} out` : "pending"}
            {typeof call.payload.cost_usd === "number" && <> · ${call.payload.cost_usd.toFixed(4)}</>}
          </p>
        </div>
      </div>

      {call.payload.error != null && (
        <div className="rounded-[var(--hl-radius)] border border-err bg-err-soft px-3 py-2 text-[12px] text-err">
          {String(call.payload.error)}
        </div>
      )}

      <div>
        <p className="text-[12px] font-semibold text-ink-700 mb-1">Prompt preview</p>
        <pre className="max-h-64 overflow-auto rounded-[var(--hl-radius)] bg-navy-deep px-3 py-2 text-[11.5px] leading-5 text-ink-200 whitespace-pre-wrap">
          {asString(call.payload.prompt_preview, "No prompt preview captured.")}
        </pre>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <p className="text-[12px] font-semibold text-ink-700 mb-1">Documents</p>
          <div className="space-y-1 text-[12px]">
            {docs.length === 0 && <p className="text-ink-400">none recorded</p>}
            {docs.map((doc, index) => (
              <div key={index} className="rounded border border-line px-2 py-1.5">
                <p className="font-medium text-ink-800">{asString(doc.name, `D${index + 1}`)}</p>
                <p className="font-mono text-[11px] text-ink-500 break-all">{asString(doc.path)}</p>
              </div>
            ))}
          </div>
        </div>
        <div>
          <p className="text-[12px] font-semibold text-ink-700 mb-1">Attached pages</p>
          <div className="max-h-36 overflow-auto rounded border border-line text-[12px]">
            {pages.length === 0 && <p className="px-2 py-2 text-ink-400">none recorded</p>}
            {pages.map((page, index) => (
              <div key={index} className="flex justify-between gap-2 border-b border-line last:border-b-0 px-2 py-1">
                <span className="font-mono">p{asNumber(page.page)}</span>
                <span className="text-ink-600">{asString(page.kind)} · {asString(page.page_class)}</span>
              </div>
            ))}
          </div>
          {imagePaths.length > 0 && (
            <p className="mt-1 text-[11px] text-ink-500">{imagePaths.length} image attachment(s)</p>
          )}
        </div>
      </div>

      <div>
        <p className="text-[12px] font-semibold text-ink-700 mb-1">Provider messages</p>
        <div className="max-h-36 overflow-auto rounded-[var(--hl-radius)] bg-ink-50 border border-line px-2 py-1 text-[11.5px]">
          {call.messages.length === 0 && <p className="text-ink-400 py-1">No interim provider messages yet.</p>}
          {call.messages.map((message, index) => (
            <p key={index} className="font-mono text-ink-700 py-0.5">
              <span className="text-ink-400">{message.ts.slice(11, 19)}</span>{" "}
              <span className="text-info">{message.stream}</span>{" "}
              <span>{message.message}</span>
            </p>
          ))}
        </div>
      </div>
    </div>
  );
}

export function LlmActivityView({ events }: { events: JobEvent[] }) {
  const [open, setOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const calls = useMemo(() => buildCalls(events), [events]);
  const active = calls.filter((call) => !terminalStatuses.has(call.status));
  const selected = calls.find((call) => call.id === selectedId) ?? calls[calls.length - 1] ?? null;

  useEffect(() => {
    if (calls.length && selectedId === null) setSelectedId(calls[calls.length - 1].id);
  }, [calls, selectedId]);

  useEffect(() => {
    if (active.length === 0) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active.length]);

  return (
    <Card>
      <CardHeader
        title="LLM view"
        sub={`${active.length} running · ${calls.length} total call${calls.length === 1 ? "" : "s"}`}
        right={<Button kind="secondary" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</Button>}
      />
      {open && (
        <div className="px-4 pb-4 space-y-4">
          {calls.length === 0 ? (
            <p className="text-[12px] text-ink-400 py-4">No LLM calls have started for this run.</p>
          ) : (
            <>
              <div className="flex items-center gap-1 h-3">
                {calls.map((call) => (
                  <button
                    key={call.id}
                    type="button"
                    title={`${call.id} ${call.status}`}
                    onClick={() => setSelectedId(call.id)}
                    className={`h-2 min-w-6 flex-1 rounded-sm ${statusTone(call.status)} ${selected?.id === call.id ? "ring-2 ring-offset-1 ring-accent" : ""}`}
                  />
                ))}
              </div>
              <div className="grid grid-cols-[minmax(260px,0.9fr)_minmax(0,1.4fr)] gap-4">
                <div className="rounded-[var(--hl-radius)] border border-line overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 border-b border-line bg-ink-50">
                    <span className="text-[12px] font-semibold text-ink-700">Calls</span>
                    {selected && <StatusChip value={selected.status} />}
                  </div>
                  <div className="max-h-[34rem] overflow-auto">
                    {calls.map((call) => (
                      <CallRow
                        key={call.id}
                        call={call}
                        selected={selected?.id === call.id}
                        onSelect={() => setSelectedId(call.id)}
                        now={now}
                      />
                    ))}
                  </div>
                </div>
                {selected ? <CallDetails call={selected} now={now} /> : null}
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
