import { AnimatePresence, motion } from "framer-motion";
import { type MouseEvent as ReactMouseEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button, Card, CardHeader, ConfidenceBar, EmptyState, ErrorState, Field, MethodChip, Panel, SkeletonRows, StatusChip, inputCls } from "../components/ui";
import { MemoIssue, PageWords, ReviewItem, ReviewQueueResponse, RunResult, evidenceUrl, get, post } from "../lib/api";
import { useLoad } from "../lib/hooks";
import { bboxToPercentStyle, memoIssuesForSelected, overlayForPage, selectedHighlight } from "../lib/reviewEvidence";
import { useStickyState } from "../lib/uiState";

export default function ReviewQueue() {
  const { runId } = useParams<{ runId?: string }>();
  const navigate = useNavigate();
  const runs = useLoad<{ runs: RunResult[] }>("/api/runs");

  // No run selected: pick from recent runs.
  if (!runId) {
    return (
      <Panel className="space-y-4 max-w-4xl">
        <h1 className="text-xl font-semibold text-ink-900">Review queue</h1>
        <Card>
          <CardHeader title="Pick a run" />
          {runs.loading && <SkeletonRows rows={4} cols={3} />}
          {runs.error && <ErrorState message={runs.error} onRetry={runs.reload} />}
          {runs.data && runs.data.runs.length === 0 && (
            <EmptyState title="No runs yet" hint="Complete a run first — every flag and low-confidence cell lands here." />
          )}
          <ul>
            {(runs.data?.runs ?? []).filter((r) => !r.dry_run).map((r) => {
              const clients = r.clients ?? [];
              const clientLabel =
                clients.length === 0
                  ? null
                  : clients.slice(0, 3).join(", ") + (clients.length > 3 ? `, +${clients.length - 3}` : "");
              return (
                <li key={r.run_id} className="border-b border-line last:border-0">
                  <button
                    className="w-full text-left px-4 py-3 hover:bg-ink-50 flex justify-between items-center gap-4"
                    onClick={() => navigate(`/review/${r.run_id}`)}
                  >
                    <span className="min-w-0">
                      <span className="font-mono text-[13px] block">{r.run_id}</span>
                      {clientLabel && <span className="text-[12px] text-ink-600 truncate block">{clientLabel}</span>}
                    </span>
                    <span className="text-[12px] text-ink-500 shrink-0 text-right">
                      {r.memos} memo{r.memos === 1 ? "" : "s"} · {r.flags_added ?? "—"} flags
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Card>
      </Panel>
    );
  }
  return <QueueForRun runId={runId} />;
}

function QueueForRun({ runId }: { runId: string }) {
  const queue = useLoad<ReviewQueueResponse>(`/api/runs/${runId}/review`);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useStickyState("review.showResolved", false);
  const [categoryFilter, setCategoryFilter] = useStickyState<string>("review.categoryFilter", "");
  const [editMode, setEditMode] = useState(false);
  const [editValue, setEditValue] = useState("");
  const [editNote, setEditNote] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ id: string; kind: "accept" | "reject" } | null>(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  const [addValueOpen, setAddValueOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const editRef = useRef<HTMLInputElement>(null);

  const items = useMemo(() => {
    let rows = queue.data?.items ?? [];
    if (!showResolved) rows = rows.filter((i) => !i.resolved);
    if (categoryFilter) rows = rows.filter((i) => i.category === categoryFilter);
    return rows;
  }, [queue.data, showResolved, categoryFilter]);

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const i of queue.data?.items ?? []) {
      if (!i.resolved) counts.set(i.category, (counts.get(i.category) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [queue.data]);

  const selected = items.find((i) => i.id === selectedId) ?? items[0] ?? null;
  const selectedIndex = selected ? items.indexOf(selected) : -1;
  const highlight = useMemo(() => selectedHighlight(selected), [selected]);
  const selectedMemoIssues = useMemo(
    () => memoIssuesForSelected(queue.data?.memo_issues ?? [], selected),
    [queue.data?.memo_issues, selected],
  );

  useEffect(() => {
    if (selected && selectedId !== selected.id) setSelectedId(selected.id);
  }, [selected, selectedId]);

  const act = useCallback(
    async (item: ReviewItem, action: "accept" | "edit" | "unresolvable", value?: string, note?: string) => {
      setActionError(null);
      try {
        const body: Record<string, unknown> = { action, note: note || null };
        if (action === "edit") {
          const trimmed = (value ?? "").trim();
          const asNumber = Number(trimmed.replace(/,/g, ""));
          body.value = trimmed !== "" && !Number.isNaN(asNumber) && /^[\d.,()%xX$\s-]+$/.test(trimmed) ? asNumber : trimmed;
        }
        await post(`/api/runs/${runId}/review/${encodeURIComponent(item.id)}/action`, body);
        setFlash({ id: item.id, kind: action === "unresolvable" ? "reject" : "accept" });
        const next = items[items.indexOf(item) + 1];
        setSelectedId(next?.id ?? null);
        setEditMode(false);
        setEditValue("");
        setEditNote("");
        queue.reload();
      } catch (e) {
        setActionError((e as Error).message);
      }
    },
    [items, queue, runId],
  );

  const addValue = useCallback(
    async (
      item: ReviewItem,
      payload: { value: unknown; field: string | null; page: number | null; bbox: number[] | null; evidence: string; note: string },
    ) => {
      setActionError(null);
      await post(`/api/runs/${runId}/review/${encodeURIComponent(item.id)}/action`, {
        action: "add_value",
        value: payload.value,
        field: payload.field,
        page: payload.page,
        bbox: payload.bbox,
        evidence: payload.evidence,
        note: payload.note || null,
      });
      setAddValueOpen(false);
      setFlash({ id: item.id, kind: "accept" });
      const next = items[items.indexOf(item) + 1];
      setSelectedId(next?.id ?? item.id);
      queue.reload();
    },
    [items, queue, runId],
  );

  const bulkAccept = async (category: string | null) => {
    setActionError(null);
    try {
      await post(`/api/runs/${runId}/review/bulk-accept`, category ? { category } : {});
      queue.reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  const pendingCount = useMemo(
    () => (queue.data?.items ?? []).filter((i) => !i.resolved).length,
    [queue.data],
  );

  // keyboard-first: j/k navigate, a accept, e edit, u unresolvable
  useEffect(() => {
    const handler = (ev: KeyboardEvent) => {
      const target = ev.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
        if (ev.key === "Escape") setEditMode(false);
        return;
      }
      if (!selected) return;
      if (ev.key === "j") setSelectedId(items[Math.min(items.length - 1, selectedIndex + 1)]?.id ?? selected.id);
      else if (ev.key === "k") setSelectedId(items[Math.max(0, selectedIndex - 1)]?.id ?? selected.id);
      else if (ev.key === "a") act(selected, "accept");
      else if (ev.key === "u") act(selected, "unresolvable");
      else if (ev.key === "v") {
        if (selected.reader === "pdf" && selected.source_page_count > 0) {
          ev.preventDefault();
          setAddValueOpen(true);
        }
      } else if (ev.key === "e") {
        ev.preventDefault();
        setEditMode(true);
        setEditValue(selected.value === null ? "" : String(selected.value));
        window.setTimeout(() => editRef.current?.focus(), 50);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [selected, selectedIndex, items, act]);

  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-id="${selected?.id}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [selected?.id]);

  return (
    <Panel className="space-y-3 max-w-[1400px]">
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <h1 className="text-xl font-semibold text-ink-900">Review queue</h1>
          <span className="font-mono text-[12px] text-ink-500">{runId}</span>
          <span className="text-[12px] text-ink-500">
            {items.length} open item{items.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[12px] text-ink-500">
          <kbd className="px-1.5 py-0.5 border border-line rounded bg-surface font-mono">j/k</kbd> navigate
          <kbd className="px-1.5 py-0.5 border border-line rounded bg-surface font-mono">a</kbd> accept
          <kbd className="px-1.5 py-0.5 border border-line rounded bg-surface font-mono">e</kbd> edit
          <kbd className="px-1.5 py-0.5 border border-line rounded bg-surface font-mono">v</kbd> add value
          <kbd className="px-1.5 py-0.5 border border-line rounded bg-surface font-mono">u</kbd> unresolvable
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <select className={`${inputCls} w-auto`} value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
          <option value="">all categories</option>
          {categories.map(([cat, n]) => (
            <option key={cat} value={cat}>
              {cat} ({n})
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-[12.5px] text-ink-600">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} /> show resolved
        </label>
        {categoryFilter && (
          <Button kind="secondary" onClick={() => bulkAccept(categoryFilter)}>
            Bulk accept “{categoryFilter}”
          </Button>
        )}
        {pendingCount > 0 && (
          <Button
            kind="secondary"
            onClick={() => {
              if (window.confirm(`Accept all ${pendingCount} pending item${pendingCount === 1 ? "" : "s"}? Each acceptance is recorded in the audit files.`)) {
                bulkAccept(null);
              }
            }}
          >
            Accept all pending ({pendingCount})
          </Button>
        )}
        {actionError && <span className="text-[12px] text-err">{actionError}</span>}
      </div>

      {queue.loading && <Card><SkeletonRows rows={8} cols={5} /></Card>}
      {queue.error && <Card><ErrorState message={queue.error} onRetry={queue.reload} /></Card>}
      {queue.data && items.length === 0 && (
        <Card>
          <EmptyState title="Queue is clear" hint={showResolved ? "No items at all for this run." : "Everything is resolved — toggle ‘show resolved’ to audit past decisions."} />
        </Card>
      )}

      {items.length > 0 && (
        <>
        {selectedMemoIssues.length > 0 && (
          <MemoIssueSummary issues={selectedMemoIssues} />
        )}
        <div className="grid grid-cols-[minmax(380px,1fr)_minmax(480px,1.2fr)] gap-4 items-start">
          <Card>
            <div ref={listRef} className="max-h-[70vh] overflow-auto">
              <AnimatePresence initial={false}>
                {items.map((item) => (
                  <motion.button
                    key={item.id}
                    data-id={item.id}
                    layout="position"
                    initial={false}
                    animate={
                      flash?.id === item.id
                        ? { backgroundColor: flash.kind === "accept" ? "var(--hl-success-soft)" : "var(--hl-error-soft)" }
                        : {}
                    }
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.18 }}
                    className={`w-full text-left px-3 py-2.5 border-b border-line last:border-0 block ${
                      selected?.id === item.id ? "bg-info-soft" : "hover:bg-ink-50"
                    }`}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[12.5px] font-medium text-ink-800 truncate">
                        {item.field ?? item.category}
                      </span>
                      <span className="flex items-center gap-1.5 shrink-0">
                        {item.reviewer_attention && <span title="reviewer attention" className="text-warn">⚑</span>}
                        {item.resolved && <span className="text-[10px] uppercase text-ok font-semibold">resolved</span>}
                        <MethodChip method={item.method} />
                      </span>
                    </div>
                    <p className="text-[11.5px] text-ink-500 truncate mt-0.5">{item.description}</p>
                    <p className="text-[11px] text-ink-400 font-mono mt-0.5">
                      {item.row_memo_id} · {item.client} / {item.deal}
                    </p>
                  </motion.button>
                ))}
              </AnimatePresence>
            </div>
          </Card>

          {selected && (
            <Card className="sticky top-4">
              <CardHeader
                title={selected.field ?? selected.category}
                sub={`${selected.client} / ${selected.deal} · ${selected.row_memo_id}`}
                right={<StatusChip value={selected.qa_status || selected.severity} />}
              />
              <div className="px-4 pb-4 space-y-3">
                <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px]">
                  <div>
                    <p className="text-[11px] uppercase tracking-wide text-ink-400">Extracted value</p>
                    <p className="font-mono text-ink-900">
                      {selected.value === null ? "—" : String(selected.value)}
                      {selected.unit && <span className="text-ink-400 ml-1">{selected.unit}</span>}
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] uppercase tracking-wide text-ink-400">Method · confidence</p>
                    <p className="flex items-center gap-2 flex-wrap">
                      <MethodChip method={selected.method} />
                      <ConfidenceBar value={selected.confidence} />
                      <GroundingChip item={selected} />
                    </p>
                  </div>
                  <div className="col-span-2">
                    <p className="text-[11px] uppercase tracking-wide text-ink-400">Flag</p>
                    <p className="text-ink-700">{selected.description}</p>
                  </div>
                  <div className="col-span-2">
                    <p className="text-[11px] uppercase tracking-wide text-ink-400">
                      Verbatim evidence {selected.page && <span>· page {selected.page}</span>}
                    </p>
                    <p className="font-mono text-[12px] bg-surface border border-line rounded px-2 py-1.5 mt-1 whitespace-pre-wrap break-words">
                      {selected.evidence || selected.raw_text || "—"}
                    </p>
                    {selected.grounding_status === "page_only" && (
                      <p className="text-[11.5px] text-warn mt-1">{selected.grounding_reason || "page evidence available, exact box unavailable"}</p>
                    )}
                  </div>
                  {selected.conflicts.length > 0 && (
                    <div className="col-span-2">
                      <p className="text-[11px] uppercase tracking-wide text-ink-400">Conflicting candidates</p>
                      <ul className="text-[12px] text-ink-600 font-mono">
                        {selected.conflicts.map((c, i) => (
                          <li key={i}>
                            {String(c.value)} (p{String(c.page ?? "?")}, conf {Number(c.confidence ?? 0).toFixed(2)})
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>

                {selected.has_page_image && highlight.highlightPage && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-[11px] uppercase tracking-wide text-ink-400">
                        Source page {highlight.highlightPage} {highlight.highlightBbox ? "(evidence region highlighted)" : ""}
                      </p>
                      {selected.reader === "pdf" && selected.source_page_count > 0 && (
                        <button
                          type="button"
                          className="text-[11.5px] text-[var(--hl-blue)] underline"
                          onClick={() => setViewerOpen(true)}
                        >
                          View full document ({selected.source_page_count} pages)
                        </button>
                      )}
                    </div>
                    <EvidencePageImage
                      runId={selected.run_id}
                      memoId={selected.memo_id}
                      fileName={selected.source_filename}
                      page={highlight.highlightPage}
                      bbox={highlight.highlightBbox}
                      className="max-h-[45vh]"
                    />
                    {highlight.fallbackMessage && (
                      <p className="text-[11.5px] text-warn mt-1">{highlight.fallbackMessage}</p>
                    )}
                  </div>
                )}
                {!selected.has_page_image && selected.reader === "pdf" && selected.source_page_count > 0 && (
                  <div>
                    <Button kind="secondary" onClick={() => setViewerOpen(true)}>
                      View full document ({selected.source_page_count} pages)
                    </Button>
                  </div>
                )}

                {!selected.resolved && (
                  <div className="pt-2 border-t border-line space-y-2">
                    {editMode ? (
                      <div className="space-y-2">
                        <Field label={`New value for “${selected.field}”`}>
                          <input
                            ref={editRef}
                            className={inputCls}
                            value={editValue}
                            onChange={(e) => setEditValue(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") act(selected, "edit", editValue, editNote);
                              if (e.key === "Escape") setEditMode(false);
                            }}
                          />
                        </Field>
                        <Field label="Note (audit trail)">
                          <input className={inputCls} value={editNote} onChange={(e) => setEditNote(e.target.value)} placeholder="why this correction is right" />
                        </Field>
                        <div className="flex gap-2">
                          <Button kind="primary" onClick={() => act(selected, "edit", editValue, editNote)} disabled={selected.field === null}>
                            Save to workbook
                          </Button>
                          <Button kind="ghost" onClick={() => setEditMode(false)}>
                            Cancel
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="flex gap-2">
                        <Button kind="primary" onClick={() => act(selected, "accept")}>
                          Accept (a)
                        </Button>
                        <Button
                          kind="secondary"
                          onClick={() => {
                            setEditMode(true);
                            setEditValue(selected.value === null ? "" : String(selected.value));
                            window.setTimeout(() => editRef.current?.focus(), 50);
                          }}
                          disabled={selected.field === null}
                          title={selected.field === null ? "No linked schema field to edit" : undefined}
                        >
                          Edit value (e)
                        </Button>
                        <Button
                          kind="secondary"
                          onClick={() => setAddValueOpen(true)}
                          disabled={selected.reader !== "pdf" || selected.source_page_count <= 0}
                          title={
                            selected.reader !== "pdf"
                              ? "Add-from-document is available for PDF sources"
                              : "Find the value in the document, highlight it, and enter it"
                          }
                        >
                          Add value (v)
                        </Button>
                        <Button kind="danger" onClick={() => act(selected, "unresolvable")}>
                          Unresolvable (u)
                        </Button>
                      </div>
                    )}
                  </div>
                )}
                {selected.resolved && selected.resolution && (
                  <p className="text-[12px] text-ink-500 border-t border-line pt-2">
                    {String(selected.resolution.action)} · {String(selected.resolution.ts)}{" "}
                    {selected.resolution.note ? `— ${String(selected.resolution.note)}` : ""}
                  </p>
                )}
              </div>
            </Card>
          )}
        </div>
        </>
      )}

      {viewerOpen && selected && (
        <FullDocViewer
          runId={selected.run_id}
          memoId={selected.memo_id}
          fileName={selected.source_filename}
          pageCount={selected.source_page_count}
          startPage={highlight.highlightPage ?? 1}
          highlightPage={highlight.highlightPage}
          highlightBbox={highlight.highlightBbox}
          evidenceMeta={highlight}
          onClose={() => setViewerOpen(false)}
        />
      )}

      {addValueOpen && selected && (
        <AddValueModal
          item={selected}
          onClose={() => setAddValueOpen(false)}
          onSave={(payload) => addValue(selected, payload)}
        />
      )}
    </Panel>
  );
}

function MemoIssueSummary({ issues }: { issues: MemoIssue[] }) {
  const descriptions = issues.flatMap((issue) => issue.descriptions);
  return (
    <div className="rounded border border-err/40 bg-err-soft px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-err font-semibold">Memo QA summary</p>
      <ul className="mt-1 text-[12.5px] text-ink-800 list-disc list-inside space-y-0.5">
        {descriptions.map((reason, i) => (
          <li key={`${reason}-${i}`}>{reason}</li>
        ))}
      </ul>
    </div>
  );
}

function GroundingChip({ item }: { item: ReviewItem }) {
  const label =
    item.grounding_status === "box"
      ? "grounded box"
      : item.grounding_status === "page_only"
        ? "page only"
        : "no grounding";
  const title =
    item.evidence_ref?.match_method || item.grounding_reason
      ? `${item.evidence_ref?.match_method ?? "page_only"}${item.evidence_ref?.match_score !== null && item.evidence_ref?.match_score !== undefined ? ` · ${(item.evidence_ref.match_score * 100).toFixed(0)}%` : ""}${item.grounding_reason ? ` · ${item.grounding_reason}` : ""}`
      : undefined;
  return (
    <span
      title={title}
      className={`text-[10.5px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${
        item.grounding_status === "box"
          ? "border-ok/30 text-ok bg-ok/10"
          : item.grounding_status === "page_only"
            ? "border-warn/40 text-warn bg-warn/10"
            : "border-line text-ink-400 bg-surface"
      }`}
    >
      {label}
    </span>
  );
}

function EvidencePageImage({
  runId,
  memoId,
  fileName,
  page,
  bbox,
  className = "",
}: {
  runId: string;
  memoId: string;
  fileName: string;
  page: number;
  bbox: number[] | null;
  className?: string;
}) {
  const [pageInfo, setPageInfo] = useState<PageWords | null>(null);

  useEffect(() => {
    let live = true;
    setPageInfo(null);
    get<PageWords>(`/api/runs/${runId}/page-words/${memoId}?page=${page}`)
      .then((info) => live && setPageInfo(info))
      .catch(() => live && setPageInfo(null));
    return () => {
      live = false;
    };
  }, [runId, memoId, page]);

  const style = pageInfo ? bboxToPercentStyle(bbox, { width: pageInfo.width, height: pageInfo.height }) : null;

  return (
    <div className={`border border-line rounded overflow-auto bg-ink-100 ${className}`}>
      <div className="relative inline-block min-w-full">
        <img
          src={evidenceUrl(runId, memoId, page, null)}
          alt={`page ${page} of ${fileName}`}
          className="w-full block"
          loading="lazy"
        />
        {style && (
          <div
            data-testid="selected-evidence-highlight"
            className="absolute border-2 border-[var(--hl-blue)] bg-[var(--hl-blue)]/20 pointer-events-none"
            style={style}
          />
        )}
      </div>
    </div>
  );
}

/** Add a value by finding it in the document: page through the source, drag a
    highlight over the value (on text pages the overlapped words are captured as
    the verbatim evidence; on scanned/image pages the box is a pure marker), then
    type the value. The box is sent as a PDF-point bbox so the evidence viewer can
    re-highlight it later. */
function AddValueModal({
  item,
  onClose,
  onSave,
}: {
  item: ReviewItem;
  onClose: () => void;
  onSave: (payload: {
    value: unknown;
    field: string | null;
    page: number | null;
    bbox: number[] | null;
    evidence: string;
    note: string;
  }) => Promise<void>;
}) {
  const pageCount = item.source_page_count;
  const [page, setPage] = useState(Math.min(Math.max(1, item.page ?? 1), pageCount));
  const [words, setWords] = useState<PageWords | null>(null);
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null);
  const [value, setValue] = useState("");
  const [evidence, setEvidence] = useState("");
  const [field, setField] = useState(item.field ?? "");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  // Load this page's word boxes (text pages) for text capture.
  useEffect(() => {
    let live = true;
    setWords(null);
    setBox(null);
    get<PageWords>(`/api/runs/${item.run_id}/page-words/${item.memo_id}?page=${page}`)
      .then((w) => live && setWords(w))
      .catch(() => live && setWords(null));
    return () => {
      live = false;
    };
  }, [item.run_id, item.memo_id, page]);

  const scale = useCallback(() => {
    const img = imgRef.current;
    if (!img || !words || words.width === 0 || words.height === 0) return null;
    return { sx: words.width / img.clientWidth, sy: words.height / img.clientHeight };
  }, [words]);

  const finishBox = useCallback(
    (px: { x0: number; y0: number; x1: number; y1: number }) => {
      setBox(px);
      const s = scale();
      if (!s || !words) return;
      const bb = {
        x0: Math.min(px.x0, px.x1) * s.sx,
        y0: Math.min(px.y0, px.y1) * s.sy,
        x1: Math.max(px.x0, px.x1) * s.sx,
        y1: Math.max(px.y0, px.y1) * s.sy,
      };
      // Words whose center falls inside the box, in reading order.
      const hit = words.words
        .filter((w) => {
          const cx = (w.x0 + w.x1) / 2;
          const cy = (w.y0 + w.y1) / 2;
          return cx >= bb.x0 && cx <= bb.x1 && cy >= bb.y0 && cy <= bb.y1;
        })
        .sort((a, b) => (Math.abs(a.y0 - b.y0) > 3 ? a.y0 - b.y0 : a.x0 - b.x0))
        .map((w) => w.text)
        .join(" ");
      if (hit) {
        setEvidence(hit);
        if (!value.trim()) setValue(hit);
      }
    },
    [scale, words, value],
  );

  const pxToBbox = (): number[] | null => {
    const s = scale();
    if (!box || !s) return null;
    return [
      Math.min(box.x0, box.x1) * s.sx,
      Math.min(box.y0, box.y1) * s.sy,
      Math.max(box.x0, box.x1) * s.sx,
      Math.max(box.y0, box.y1) * s.sy,
    ];
  };

  const onMouseDown = (e: ReactMouseEvent) => {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setDrag({ x: e.clientX - rect.left, y: e.clientY - rect.top });
    setBox(null);
  };
  const onMouseMove = (e: ReactMouseEvent) => {
    if (!drag) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setBox({ x0: drag.x, y0: drag.y, x1: e.clientX - rect.left, y1: e.clientY - rect.top });
  };
  const onMouseUp = () => {
    if (drag && box) finishBox(box);
    setDrag(null);
  };

  const save = async () => {
    const trimmed = value.trim();
    if (!trimmed) {
      setError("Enter a value.");
      return;
    }
    const target = (field || item.field || "").trim();
    if (!target) {
      setError("Enter the target field (column header).");
      return;
    }
    const asNumber = Number(trimmed.replace(/,/g, ""));
    const coerced =
      trimmed !== "" && !Number.isNaN(asNumber) && /^[\d.,()%xX$\s-]+$/.test(trimmed) ? asNumber : trimmed;
    setBusy(true);
    setError(null);
    try {
      await onSave({
        value: coerced,
        field: target,
        page,
        bbox: pxToBbox(),
        evidence,
        note,
      });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const hasWords = (words?.words.length ?? 0) > 0;

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-[var(--hl-radius)] shadow-lift w-[1080px] max-w-[97vw] max-h-[94vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-line flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[13px] font-semibold text-ink-900 truncate">
              Add value — {item.field ?? "choose a field"}
            </p>
            <p className="text-[11.5px] text-ink-500 truncate">
              {item.source_filename} · {item.row_memo_id}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button kind="ghost" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>
              ← Prev
            </Button>
            <span className="text-[12.5px] text-ink-600">
              page
              <input
                type="number"
                min={1}
                max={pageCount}
                value={page}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (!Number.isNaN(n)) setPage(Math.min(pageCount, Math.max(1, n)));
                }}
                className="mx-1 w-14 px-1.5 py-0.5 text-[12.5px] text-center bg-surface border border-line-strong rounded focus:outline-none focus:border-accent"
              />
              of {pageCount}
            </span>
            <Button kind="ghost" onClick={() => setPage((p) => Math.min(pageCount, p + 1))} disabled={page >= pageCount}>
              Next →
            </Button>
            <button className="text-ink-400 hover:text-ink-700 text-[14px] ml-1" onClick={onClose}>
              ✕
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-[1.5fr_minmax(280px,1fr)]">
          {/* document with drag-to-highlight overlay */}
          <div className="overflow-auto bg-ink-100 border-r border-line">
            <div
              className="relative inline-block select-none cursor-crosshair"
              onMouseDown={onMouseDown}
              onMouseMove={onMouseMove}
              onMouseUp={onMouseUp}
              onMouseLeave={() => setDrag(null)}
            >
              <img
                ref={imgRef}
                key={page}
                src={evidenceUrl(item.run_id, item.memo_id, page, null)}
                alt={`page ${page}`}
                className="block max-w-full pointer-events-none"
                draggable={false}
              />
              {box && (
                <div
                  className="absolute border-2 border-[var(--hl-blue)] bg-[var(--hl-blue)]/15 pointer-events-none"
                  style={{
                    left: Math.min(box.x0, box.x1),
                    top: Math.min(box.y0, box.y1),
                    width: Math.abs(box.x1 - box.x0),
                    height: Math.abs(box.y1 - box.y0),
                  }}
                />
              )}
            </div>
          </div>

          {/* value entry */}
          <div className="p-4 space-y-3 overflow-auto">
            <p className="text-[11.5px] text-ink-500">
              {hasWords
                ? "Drag a box over the value — the highlighted text is captured as evidence."
                : words === null
                  ? "Loading page…"
                  : "Scanned/image page: drag a box to mark the region (no text to capture)."}
            </p>
            <Field label="Target field (column header)">
              <input className={inputCls} value={field} onChange={(e) => setField(e.target.value)} placeholder="e.g. EBITDA ($M)" />
            </Field>
            <Field label="Value">
              <input
                className={inputCls}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder="the value to write to the workbook"
                autoFocus
              />
            </Field>
            <Field label="Evidence (verbatim from the page)">
              <textarea
                className={`${inputCls} h-20 resize-none`}
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                placeholder="highlight the source text, or type the supporting quote"
              />
            </Field>
            <Field label="Note (audit trail)">
              <input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} placeholder="why this value is right" />
            </Field>
            {box && <p className="text-[11px] text-ink-400">region marked on page {page}</p>}
            {error && <p className="text-[12px] text-err">{error}</p>}
            <div className="flex gap-2 pt-1">
              <Button kind="primary" onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save value to workbook"}
              </Button>
              <Button kind="ghost" onClick={onClose} disabled={busy}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Page through the entire source PDF. Only the currently-viewed page is
    materialized: a single <img> is keyed on the page number so navigating
    discards (releases) the previous page rather than holding every rendered
    page in memory. Pages are rendered on demand by the evidence endpoint. */
function FullDocViewer({
  runId,
  memoId,
  fileName,
  pageCount,
  startPage,
  highlightPage,
  highlightBbox,
  evidenceMeta,
  onClose,
}: {
  runId: string;
  memoId: string;
  fileName: string;
  pageCount: number;
  startPage: number;
  highlightPage: number | null;
  highlightBbox: number[] | null;
  evidenceMeta: ReturnType<typeof selectedHighlight>;
  onClose: () => void;
}) {
  const [page, setPage] = useState(Math.min(Math.max(1, startPage), pageCount));
  const go = useCallback(
    (delta: number) => setPage((p) => Math.min(pageCount, Math.max(1, p + delta))),
    [pageCount],
  );

  useEffect(() => {
    if (highlightPage !== null) {
      setPage(Math.min(Math.max(1, highlightPage), pageCount));
    }
  }, [highlightPage, pageCount]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" || e.key === "PageDown") go(1);
      else if (e.key === "ArrowLeft" || e.key === "PageUp") go(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, onClose]);

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-[var(--hl-radius)] shadow-lift w-[820px] max-w-[95vw] max-h-[92vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-line flex items-center justify-between gap-3">
          <p className="text-[13px] font-semibold text-ink-900 truncate" title={fileName}>
            {fileName}
          </p>
          <div className="flex items-center gap-2 shrink-0">
            <Button kind="ghost" onClick={() => go(-1)} disabled={page <= 1}>
              ← Prev
            </Button>
            <span className="text-[12.5px] text-ink-600">
              page
              <input
                type="number"
                min={1}
                max={pageCount}
                value={page}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (!Number.isNaN(n)) setPage(Math.min(pageCount, Math.max(1, n)));
                }}
                className="mx-1 w-14 px-1.5 py-0.5 text-[12.5px] text-center bg-surface border border-line-strong rounded focus:outline-none focus:border-accent"
              />
              of {pageCount}
            </span>
            <Button kind="ghost" onClick={() => go(1)} disabled={page >= pageCount}>
              Next →
            </Button>
            <button className="text-ink-400 hover:text-ink-700 text-[14px] ml-1" onClick={onClose}>
              ✕
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-auto bg-ink-100 min-h-[300px] flex justify-center">
          <EvidencePageImage
            key={page}
            runId={runId}
            memoId={memoId}
            fileName={fileName}
            page={page}
            bbox={overlayForPage(page, highlightPage, highlightBbox)}
            className="border-0 rounded-none max-w-full self-start"
          />
        </div>
        <div className="px-4 py-2 text-[10.5px] text-ink-400 border-t border-line flex items-center justify-between gap-3">
          <span>← / → to page · only the current page is loaded · Esc to close</span>
          {evidenceMeta.fallbackMessage && page === evidenceMeta.highlightPage && (
            <span className="text-warn">{evidenceMeta.fallbackMessage}</span>
          )}
        </div>
      </div>
    </div>
  );
}
