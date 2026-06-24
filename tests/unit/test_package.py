"""Smoke tests for the package skeleton."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

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
    """``__version__`` is sourced from the installed distribution.

    Pinning to a hard-coded literal here led to a multi-release drift
    (0.1.0 frozen while pyproject went 0.1.1 → 0.1.6). The test now
    asserts the runtime exposure matches the metadata, so any future
    bump in pyproject is picked up automatically.
    """
    assert apple_health_mcp.__version__ == _pkg_version("apple-health-mcp-server")
    # And that the package shipped a real (non-fallback) version
    # — the editable / source-tree fallback would surface as the
    # ``0.0.0+unknown`` sentinel below.
    assert apple_health_mcp.__version__ != "0.0.0+unknown"


def test_subpackages_importable() -> None:
    for module in (importers, db, server, tools, models, exceptions):
        assert module.__name__.startswith("apple_health_mcp")


def test_dunder_main_module_importable() -> None:
    import apple_health_mcp.__main__ as entry

    assert entry.main is not None
