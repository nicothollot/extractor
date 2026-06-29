import assert from "node:assert/strict";
import test from "node:test";

import {
  bboxToPercentStyle,
  memoIssuesForSelected,
  overlayForPage,
  selectedHighlight,
} from "../src/lib/reviewEvidence.ts";

test("selecting item A exposes only A's page and box", () => {
  const itemA = {
    page: 2,
    bbox: [10, 20, 110, 60],
    evidence_ref: null,
    method: "deterministic",
    confidence: 0.7,
  };
  const state = selectedHighlight(itemA);
  assert.equal(state.highlightPage, 2);
  assert.deepEqual(state.highlightBbox, [10, 20, 110, 60]);
  assert.deepEqual(overlayForPage(2, state.highlightPage, state.highlightBbox), [10, 20, 110, 60]);
  assert.equal(overlayForPage(1, state.highlightPage, state.highlightBbox), null);
});

test("selecting item B replaces A's box and jumps pages", () => {
  const itemA = { page: 2, bbox: [10, 20, 110, 60], evidence_ref: null };
  const itemB = { page: 5, bbox: [30, 40, 80, 90], evidence_ref: null };
  const stateA = selectedHighlight(itemA);
  const stateB = selectedHighlight(itemB);
  assert.notDeepEqual(stateA.highlightBbox, stateB.highlightBbox);
  assert.equal(stateB.highlightPage, 5);
  assert.equal(overlayForPage(2, stateB.highlightPage, stateB.highlightBbox), null);
  assert.deepEqual(overlayForPage(5, stateB.highlightPage, stateB.highlightBbox), [30, 40, 80, 90]);
});

test("opening full document preserves the selected highlight state", () => {
  const state = selectedHighlight({
    page: null,
    bbox: null,
    evidence_ref: {
      display_page: 4,
      bbox: [100, 120, 200, 180],
      match_method: "native_text",
      match_score: 1,
      no_geometry_reason: null,
    },
  });
  assert.equal(state.highlightPage, 4);
  assert.deepEqual(overlayForPage(4, state.highlightPage, state.highlightBbox), [100, 120, 200, 180]);
});

test("null geometry returns page-only fallback message", () => {
  const state = selectedHighlight({
    page: 3,
    bbox: null,
    evidence_ref: {
      display_page: 3,
      bbox: null,
      match_method: "page_only",
      match_score: 0.92,
      no_geometry_reason: "quote matched page text but no bbox was resolved",
    },
  });
  assert.equal(state.highlightPage, 3);
  assert.equal(state.highlightBbox, null);
  assert.match(state.fallbackMessage ?? "", /no bbox/);
});

test("memo QA issues are looked up separately from field items", () => {
  const selected = { memo_id: "MEMO_001" };
  const issues = [
    { memo_id: "MEMO_001", descriptions: ["no valuation value found"] },
    { memo_id: "MEMO_002", descriptions: ["as-of mismatch"] },
  ];
  assert.deepEqual(memoIssuesForSelected(issues, selected), [issues[0]]);
});

test("bbox conversion documents PDF points to percent overlay coordinates", () => {
  assert.deepEqual(bboxToPercentStyle([10, 20, 110, 70], { width: 200, height: 100 }), {
    left: "5%",
    top: "20%",
    width: "50%",
    height: "50%",
  });
});
