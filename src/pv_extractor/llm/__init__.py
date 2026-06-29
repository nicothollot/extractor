"""Phase 3: local CLI LLM assist for escalated fields.

The deterministic engine (Phase 2) stays primary. This package executes each
memo's EscalationPlan as a surgical second pass through hidden, non-interactive
local provider CLI sessions. There is NO hosted LLM API call from Python;
authentication is the operator's one-time local CLI login, reused by every
subprocess.

LLM_VERSION participates in the response cache key — bump it on any change
that can alter prompts, payload assembly, parsing or merge behavior.
"""

# 3.4.0: schema property keys sanitized to the API's ^[A-Za-z0-9_.-]{1,64}$
# rule (raw headers/band names were rejected with HTTP 400); prompt now
# annotates each band/field with its JSON key and responses are mapped back.
# 3.5.0: small-doc collapse — payloads <= llm.single_call_max_pages make ONE
# call over the whole doc + all fields instead of band-batching (changes
# per-call field/page grouping).
# 3.6.0: not_found is RESOLVED by default (llm.retry_not_found=False) — no
# expensive-tier retry and no NOT_EXTRACTABLE flag for confirmed-absent fields.
# 3.7.0: ungrounded values surfaced as low-confidence flagged hits by default
# (llm.surface_ungrounded_values) instead of discarded; wholesale call failure
# emits ONE LLM_PASS_FAILED error instead of a flag per field.
# 3.8.0: small-doc collapse chunks by max_fields_per_call so the INLINE
# --json-schema arg stays under the Windows ~32 KB command-line limit
# (200-field one-shot schema was failing to launch with [WinError 206]).
# 3.9.0: response schema uses $defs/$ref (~10x smaller) so ~200 fields fit ONE
# call (max_fields_per_call 50->200); cleanly-OCR'd SCANNED pages sent as TEXT
# instead of slow page images (prefer_ocr_text_over_image).
# 4.1.0: provider-neutral structured-extraction seam; cache key includes provider
# identity; llm.one_call_per_deal is deprecated in favor of
# llm.combine_deal_documents and no longer implies force-assist/cache bypass.
# 4.2.0: bounded adaptive AssistanceTask planner, sparse schema-v2 output,
# selected-page cache keys, structured grounding, scoped retries/timeouts and
# finalization rescue wave.
# 5.0.0: sparse schema-v5 candidates with numeric model_confidence, one primary
# deal/document pass, model extraction profiles, and local confidence
# arbitration with repair disabled by default.
# 5.1.0: extended thinking forced on for every model/effort
# (llm.always_enable_thinking) — the model reasons before answering, which can
# change extracted values, so prior non-thinking cache entries are invalidated.
# 5.2.0: direct_document_read — the source document is copied into the call dir
# and the model Reads it itself (small prompt) instead of consuming a
# pre-rendered/embedded page payload; different prompt + inputs, so cache reset.
# 5.3.0: file-based output — the model WRITES its answer to answers.json (Write
# tool) instead of the --json-schema StructuredOutput tool; the prompt gains an
# output-format override, the TEXT-page quote-grounding floor was relaxed from
# 0.98 to text_quote_match_threshold, and ungrounded values now surface instead
# of being rejected in arbitration. All change prompts/parsing/merging.
# 5.4.0: confidence_selection toggle (off = accept LLM values at the model's own
# confidence, no grounding cap/arbitration gate), per-call answer filename +
# pretty-print + per-field guidance in the output instruction (prompt change).
# 5.5.0: file-based output instruction now tells the model to reply with only
# `done` and never restate/print the extracted values in text (cuts output-token
# cost — the answers.json file is the only output read). Prompt change.
LLM_VERSION = "5.5.0"
