"""Tests for ``server.server``.

Transport switching is asserted by stubbing the FastMCP ``run_*_async``
methods; we don't actually open a stdio handshake or bind a port, but the
dispatch logic (and the error path for unknown transports) is fully covered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.exceptions import ConfigError
from apple_health_mcp.server import create_server, run_server
from apple_health_mcp.server.tools import ALL_TOOLS


@pytest.fixture
def in_memory_conn() -> duckdb.DuckDBPyConnection:
    conn = get_in_memory_connection()
    ensure_schema(conn)
    return conn


# Every name the server is contracted to expose. README's "Tools" table,
# the smoke test's tool-iteration list, and CHANGELOG `[0.1.0]` all enumerate
# the same set; keep them in sync when adding/removing a tool here.
_EXPECTED_TOOL_NAMES = {
    "list_record_types",
    "query_records",
    "get_record_statistics",
    "list_workouts",
    "get_workout_details",
    "get_activity_summaries",
    "get_workout_route",
    "get_heart_rate_samples",
    "list_correlations",
    "get_correlation_details",
    "list_ecg_readings",
    "get_ecg_data",
    "run_custom_query",
    "list_data_sources",
    "get_import_history",
    "list_state_of_mind",
    "get_me_attributes",
    # Issue #137: v0.3.0's 18th tool — runtime self-diagnosis for
    # the MSIX sandbox / env-override troubleshooting flow.
    "get_server_info",
    # v0.4 (issue #148): ZIP-flow tools that let the agent discover +
    # import an Apple Health export ZIP without the user opening a
    # terminal. Tools 19 + 20.
    "list_zips",
    "import_zip",
    # v0.5 (issue #157): companion to the async ``import_zip`` -- the
    # agent polls this every 10-30 seconds after import_zip queued a
    # job. Tool 21.
    "get_import_status",
}


def test_create_server_registers_every_tool(in_memory_conn: duckdb.DuckDBPyConnection) -> None:
    mcp = create_server(in_memory_conn)
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    # Equality (not just subset) so dropping a register from ALL_TOOLS — or
    # silently shadowing one with the wrong import — fails this test rather
    # than the integration smoke, which only re-confirms the tools it
    # explicitly calls by name.
    assert names == _EXPECTED_TOOL_NAMES


def test_all_tools_length_matches_expected_names() -> None:
    # ALL_TOOLS is the single registry the server bootstrap iterates; pin
    # both its length and the membership it produces so an accidental
    # reorder/drop of the tail entry can't slip through.
    assert len(ALL_TOOLS) == len(_EXPECTED_TOOL_NAMES)


def test_run_server_unknown_transport_raises(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "health.duckdb"
    # v0.4: ``get_connection`` (writable in production) materialises an
    # empty schema-only DB when the file is missing, so the unknown-
    # transport branch can be exercised without pre-creating one. We
    # still pre-seed below so the test pins both behaviours (file
    # exists -> probe path; transport check still rejects the bogus
    # name regardless).
    conn = duckdb.connect(str(db_path))
    ensure_schema(conn)
    conn.close()
    with pytest.raises(ConfigError, match="Unknown transport"):
        asyncio.run(run_server(db_path, "carrier-pigeon"))


def test_run_server_dispatches_stdio(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "health.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_schema(conn)
    conn.close()

    calls: list[str] = []

    async def fake_stdio(self: Any) -> None:
        calls.append("stdio")

    async def fake_http(self: Any) -> None:
        calls.append("http")

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run_stdio_async", fake_stdio, raising=False)
    monkeypatch.setattr(
        "mcp.server.fastmcp.FastMCP.run_streamable_http_async",
        fake_http,
        raising=False,
    )
    asyncio.run(run_server(db_path, "stdio"))
    assert calls == ["stdio"]


def test_run_server_dispatches_http(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "health.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_schema(conn)
    conn.close()

    calls: list[str] = []

    async def fake_stdio(self: Any) -> None:
        calls.append("stdio")

    async def fake_http(self: Any) -> None:
        calls.append("http")

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run_stdio_async", fake_stdio, raising=False)
    monkeypatch.setattr(
        "mcp.server.fastmcp.FastMCP.run_streamable_http_async",
        fake_http,
        raising=False,
    )
    asyncio.run(run_server(db_path, "http", host="127.0.0.1", port=18080))
    assert calls == ["http"]


def test_server_module_does_not_import_pyarrow() -> None:
    """``serve`` must not pull pyarrow (~30 MB wheel) into its import graph.

    Issue #50 added pyarrow as a runtime dependency for the importer
    bulk-load path. The serve path has no business touching it -- it
    only reads from DuckDB. A fresh subprocess imports the server
    entry point and asserts ``pyarrow`` is not in ``sys.modules``
    afterwards, guarding the boundary against an accidental import.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, apple_health_mcp.server.server, "
            "apple_health_mcp.server.tools; "
            "import apple_health_mcp.server as _s; "
            "print('pyarrow' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "False", (
        f"pyarrow should not be imported by server.server (stdout: {proc.stdout!r}, "
        f"stderr: {proc.stderr!r})"
    )
