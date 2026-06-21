"""Tests for ``apple_health_mcp.logging_config``."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from apple_health_mcp.logging_config import JsonFormatter, configure_logging


def _seed_existing_handler() -> None:
    """Attach a dummy handler so configure_logging exercises its removal branch."""
    logging.getLogger().addHandler(logging.NullHandler())


def test_configure_logging_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    _seed_existing_handler()

    configure_logging()

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert root.handlers[0].stream is sys.stderr  # type: ignore[attr-defined]
    assert isinstance(root.handlers[0].formatter, logging.Formatter)
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_json_and_unknown_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "NOPE")

    configure_logging()

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_json_formatter_plain_message() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["name"] == "test"
    assert payload["message"] == "hello world"
    assert "exc_info" not in payload


def test_json_formatter_with_exception() -> None:
    formatter = JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=None,
        exc_info=exc_info,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "ERROR"
    assert "RuntimeError: boom" in payload["exc_info"]
