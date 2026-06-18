"""JSON-lines logging to output_dir/logs/, UTF-8 everywhere.

Modules log via stdlib `logging.getLogger(__name__)`; print() is reserved
for the CLI layer (rich console). The file handler is always UTF-8 so a
cp1252 Windows console can never corrupt log output (the schema contains
characters like 'Δ' and the memo-ID key glyph).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from pv_extractor.io_guard import assert_write_allowed


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(output_dir: Path, pv_root: str, level: str = "INFO") -> Path:
    """Attach a JSONL file handler under output_dir/logs/. Returns the log
    file path. Safe to call repeatedly (idempotent per path)."""
    logs_dir = Path(output_dir) / "logs"
    log_path = logs_dir / "pv_extractor.jsonl"
    assert_write_allowed(log_path, pv_root)
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    target = str(log_path)
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == target:
            return log_path
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(JsonLinesFormatter())
    root.addHandler(handler)

    # Best-effort UTF-8 console (Windows cp1252 safety).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    return log_path


def log_event(logger: logging.Logger, message: str, **fields) -> None:
    """Structured log helper: extra fields land as JSON keys."""
    logger.info(message, extra={"extra_fields": fields})
