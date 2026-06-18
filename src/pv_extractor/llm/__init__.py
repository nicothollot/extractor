"""Phase 3: Claude Code CLI fallback for escalated fields.

The deterministic engine (Phase 2) stays primary. This package executes each
memo's EscalationPlan as a surgical second pass through hidden, non-
interactive local Claude Code sessions (`claude -p --output-format json
--json-schema ...`). There is NO Anthropic SDK, NO `anthropic` import and NO
ANTHROPIC_API_KEY anywhere — authentication is the operator's one-time
`claude auth login`, reused by every subprocess.

LLM_VERSION participates in the response cache key — bump it on any change
that can alter prompts, payload assembly, parsing or merge behavior.
"""

LLM_VERSION = "3.1.0"
