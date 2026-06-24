"""Provider-neutral structured extraction contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from pv_extractor.models import LlmUsage


class LlmProviderCapabilities(BaseModel):
    structured_output: bool = True
    image_input: bool = False
    output_schema_file: bool = False
    output_last_message_file: bool = False
    json_telemetry: bool = False


class LlmCliResult(BaseModel):
    """Outcome of one local CLI structured extraction call."""

    provider: str
    job_id: str
    ok: bool
    exit_code: int | None = None
    duration_seconds: float = 0.0
    session_id: str | None = None
    structured: dict | None = None
    usage: LlmUsage | None = None
    total_cost_usd: float | None = None
    error: str | None = None
    safe_error_summary: str | None = None
    stdout_sha256: str = ""
    argv: list[str] = Field(default_factory=list)
    command_metadata: dict[str, object] = Field(default_factory=dict)


class StructuredExtractionProvider(Protocol):
    provider_name: str

    def binary_path(self) -> str | None: ...

    def check_available(self) -> tuple[bool, str]: ...

    def capabilities(self) -> LlmProviderCapabilities: ...

    def extract_structured(
        self,
        *,
        job_id: str,
        prompt: str,
        schema: dict,
        images: list[Path] | None,
        timeout: int | None,
        model: str | None,
        effort: str | None,
        cwd: Path,
    ) -> LlmCliResult: ...
