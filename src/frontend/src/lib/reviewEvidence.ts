export interface EvidenceSelectionLike {
  page: number | null;
  bbox: number[] | null;
  evidence_ref?: {
    display_page: number | null;
    source_file?: string | null;
    bbox: number[] | null;
    match_method: string;
    match_score: number | null;
    no_geometry_reason: string | null;
  } | null;
  evidence_mode?: string;
  grounding_status?: string;
  grounding_reason?: string;
  method?: string | null;
  confidence?: number | null;
}

export interface MemoIssueLike {
  memo_id: string;
}

export interface SelectedHighlight {
  highlightPage: number | null;
  highlightBbox: number[] | null;
  sourceFile: string | null;
  fallbackMessage: string | null;
  method: string | null;
  confidence: number | null;
  matchMethod: string | null;
  matchScore: number | null;
}

export interface PageSize {
  width: number;
  height: number;
}

export interface PercentBboxStyle {
  left: string;
  top: string;
  width: string;
  height: string;
}

export function selectedHighlight(item: EvidenceSelectionLike | null): SelectedHighlight {
  if (!item) {
    return {
      highlightPage: null,
      highlightBbox: null,
      sourceFile: null,
      fallbackMessage: null,
      method: null,
      confidence: null,
      matchMethod: null,
      matchScore: null,
    };
  }
  const ref = item.evidence_ref ?? null;
  const page = ref?.display_page ?? item.page ?? null;
  const bbox = normalizeBbox(ref?.bbox ?? item.bbox ?? null);
  const evidenceMode = item.evidence_mode ?? (ref?.match_method === "llm_reasoned" ? "reasoned" : "quote");
  const hasGeometry = page !== null && bbox !== null;
  let fallback: string | null = null;
  if (evidenceMode === "reasoned") {
    fallback =
      item.grounding_reason ||
      ref?.no_geometry_reason ||
      "reasoned value has no highlightable source region";
  } else if (page !== null && !hasGeometry) {
    fallback =
      item.grounding_reason ||
      ref?.no_geometry_reason ||
      "page evidence available, exact box unavailable";
  }
  return {
    highlightPage: page,
    highlightBbox: hasGeometry ? bbox : null,
    sourceFile: ref?.source_file ?? null,
    fallbackMessage: fallback,
    method: item.method ?? null,
    confidence: item.confidence ?? null,
    matchMethod: ref?.match_method ?? null,
    matchScore: ref?.match_score ?? null,
  };
}

export function overlayForPage(
  currentPage: number,
  highlightPage: number | null,
  highlightBbox: number[] | null,
): number[] | null {
  if (highlightPage === null || currentPage !== highlightPage) return null;
  return normalizeBbox(highlightBbox);
}

export function bboxToPercentStyle(bbox: number[] | null, page: PageSize): PercentBboxStyle | null {
  const normalized = normalizeBbox(bbox);
  if (!normalized || page.width <= 0 || page.height <= 0) return null;
  const [x0, y0, x1, y1] = normalized;
  const left = clamp((x0 / page.width) * 100, 0, 100);
  const top = clamp((y0 / page.height) * 100, 0, 100);
  const right = clamp((x1 / page.width) * 100, 0, 100);
  const bottom = clamp((y1 / page.height) * 100, 0, 100);
  if (right <= left || bottom <= top) return null;
  return {
    left: percent(left),
    top: percent(top),
    width: percent(right - left),
    height: percent(bottom - top),
  };
}

export function memoIssuesForSelected<T extends MemoIssueLike>(
  issues: T[],
  selected: { memo_id: string } | null,
): T[] {
  if (!selected) return [];
  return issues.filter((issue) => issue.memo_id === selected.memo_id);
}

function normalizeBbox(bbox: number[] | null | undefined): number[] | null {
  if (!bbox || bbox.length !== 4 || bbox.some((value) => !Number.isFinite(value))) return null;
  const [x0, y0, x1, y1] = bbox;
  const left = Math.min(x0, x1);
  const top = Math.min(y0, y1);
  const right = Math.max(x0, x1);
  const bottom = Math.max(y0, y1);
  if (right - left <= 0 || bottom - top <= 0) return null;
  return [left, top, right, bottom];
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function percent(value: number): string {
  return `${Number(value.toFixed(6))}%`;
}
