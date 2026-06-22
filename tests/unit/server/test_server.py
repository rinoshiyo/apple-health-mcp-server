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
    # Create an empty DB so get_connection(read_only=True) succeeds.
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
