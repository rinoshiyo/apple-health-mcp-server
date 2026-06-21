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
from datetime import UTC, datetime
from typing import Final

_HUMAN_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Minimal structured JSON formatter for production logs.

    Timestamps are emitted in UTC with the ``Z`` suffix so downstream log
    consumers (Loki, Datadog, etc.) always see a valid RFC 3339 value, even
    on Windows / minimal containers where ``%z`` from ``time.strftime`` can
    be empty.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        payload: dict[str, str] = {
            "timestamp": timestamp,
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
