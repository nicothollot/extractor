"""Smart Search (Search & Selection Revamp, Phase B).

Free-text document search over the file index. The package is rule-first and
fully self-sufficient WITHOUT the LLM: `intent.resolve_intent` turns a query
into a `DocTypeSpec` from a deterministic rule/synonym lexicon, `rank.rank_files`
scores indexed files against that spec with a transparent additive model
(lexical relevance + folder/extension/period evidence + learned nudges), and
`doc_type_spec` is the editable profile store (builtins migrated from the
locator's doc-type keyword lists, plus analyst-defined slugs). An optional
Claude Code CLI layer only AUGMENTS the rule spec — it never blocks and any
failure degrades cleanly to the rule-only result.
"""

from __future__ import annotations

from pv_extractor.search import doc_type_spec, intent, rank

__all__ = ["doc_type_spec", "intent", "rank"]
