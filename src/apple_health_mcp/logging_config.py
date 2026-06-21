"""Logging configuration for the Apple Health MCP server.

All log output goes to stderr because the stdio MCP transport owns stdout.
``LOG_LEVEL`` controls the verbosity (default ``INFO``).
``LOG_FORMAT`` switches between ``human`` (default) and ``json`` formatters.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Final

_HUMAN_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Minimal structured JSON formatter for production logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Configure the root logger from ``LOG_LEVEL`` / ``LOG_FORMAT`` env vars."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    use_json = os.environ.get("LOG_FORMAT", "human").lower() == "json"

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter(_HUMAN_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
