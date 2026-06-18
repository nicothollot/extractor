"""Phase 2: deterministic extraction engine.

EXTRACTOR_VERSION participates in the memo result cache key — bump it on any
change that can alter extraction output, so stale cache entries self-expire.
"""

EXTRACTOR_VERSION = "2.1.0"
