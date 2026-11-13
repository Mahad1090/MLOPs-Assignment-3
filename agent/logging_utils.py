"""Structured (JSON-lines) logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SECRET_KEY = re.compile(r"(pass(word)?|token|secret|api[_-]?key|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(r"(gsk_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]+)")
REDACTED = "***REDACTED***"


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with anything that looks like a secret masked."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(redact(getattr(record, "fields", {})))
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("mlops_agent")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mlops_agent.{name}")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra={"fields": fields})
