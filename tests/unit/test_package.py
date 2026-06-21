"""Smoke tests for the package skeleton."""

from __future__ import annotations

import apple_health_mcp
from apple_health_mcp import (
    db,
    exceptions,
    importers,
    models,
    server,
)
from apple_health_mcp.server import tools


def test_version_exposed() -> None:
    assert apple_health_mcp.__version__ == "0.1.0"


def test_subpackages_importable() -> None:
    for module in (importers, db, server, tools, models, exceptions):
        assert module.__name__.startswith("apple_health_mcp")


def test_dunder_main_module_importable() -> None:
    import apple_health_mcp.__main__ as entry

    assert entry.main is not None
