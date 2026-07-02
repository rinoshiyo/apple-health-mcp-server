"""End-to-end pins for the ``run_custom_query`` typed error envelopes.

Completes issue #273: v0.6.0 shipped the import-path translator
(``importers.orchestrator._translate_conversion_error``) but left the
query path returning raw ``Error: {exc}`` strings for every failure
mode. This module drives the tool through a real, schema-bootstrapped
in-memory connection (no full importer pipeline needed -- the failure
modes below only depend on the DDL schema, not on seeded rows) and
pins the wire ``reason`` enum end-to-end.

``unknown_view`` is exercised only at the unit level
(``tests/unit/server/test_query_error.py``) -- DuckDB's own
``CatalogException`` wording says "Table with name X does not exist"
even when the closest candidate is a view, so there is no SELECT-only
SQL that naturally produces the "View with name X" wording through
this tool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.server.tools import run_custom_query
from tests._helpers import bind_tool


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = get_in_memory_connection()
    ensure_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _run(conn: duckdb.DuckDBPyConnection, query: str) -> dict[str, Any]:
    fn = bind_tool(run_custom_query, conn)
    out = asyncio.run(fn(query=query))
    return json.loads(out)  # type: ignore[no-any-return]


def test_unknown_table_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "SELECT * FROM record")
    assert payload["state"] == "error"
    assert payload["reason"] == "unknown_table"
    assert "records" in payload["hint"]["available_tables"]


def test_missing_column_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "SELECT hearth_rate FROM records LIMIT 1")
    assert payload["state"] == "error"
    assert payload["reason"] == "missing_column"
    columns = payload["hint"]["available_columns"]["records"]
    assert len(columns) == 12
    assert "record_hash" in columns


def test_syntax_error_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "SELECT * FRM records")
    assert payload["state"] == "error"
    assert payload["reason"] == "syntax_error"


def test_empty_query_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "   ")
    assert payload["state"] == "error"
    assert payload["reason"] == "empty_query"


def test_not_select_or_with_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "DROP TABLE records")
    assert payload["state"] == "error"
    assert payload["reason"] == "not_select_or_with"


def test_multi_statement_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "SELECT 1; SELECT 2")
    assert payload["state"] == "error"
    assert payload["reason"] == "multi_statement"


def test_disallowed_function_envelope(conn: duckdb.DuckDBPyConnection) -> None:
    payload = _run(conn, "SELECT * FROM read_csv('/etc/passwd')")
    assert payload["state"] == "error"
    assert payload["reason"] == "disallowed_function"
    assert "not allowed" in payload["message"]
