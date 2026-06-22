"""DB row -> JSON conversion shared by every MCP tool.

The Rust reference implementation hand-rolled the row -> JSON conversion to
guarantee a stable response shape: NULL and unconvertible values become
JSON ``null`` (not a missing key), TIMESTAMP / DATE / TIME come back as
ISO-style strings rather than the integer counters DuckDB exposes natively.

DuckDB's Python binding already decodes most of those types into Python
objects (``datetime.datetime`` / ``datetime.date`` / ``datetime.time``) so
the work is simpler here, but we still need to format them as strings so
clients see the same wire format the Rust server produced.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from decimal import Decimal
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


# Wire string returned to the MCP client whenever a tool is invoked against
# a database that has never been seeded by ``apple-health-mcp-server import``.
# Exposed as a module-level constant so both the server and the test suite
# anchor on the same exact text; consumers parsing tool responses should
# match this prefix rather than the trailing URL (which may change between
# minor versions).
IMPORT_REQUIRED_MESSAGE = (
    "Error: No Apple Health data has been imported yet. "
    "Run `apple-health-mcp-server import <export-dir>` to ingest your "
    "export, then restart this MCP server. "
    "See https://github.com/rinoshiyo/apple-health-mcp-server#usage for details."
)


def imports_present(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock | None = None,
) -> bool:
    """Return ``True`` when at least one row exists in the ``imports`` table.

    Used by every tool whose contract assumes at least one import has
    happened; ``get_import_history`` is the single exception and skips this
    check so callers can confirm the empty-DB state without seeing the
    guidance message.

    The check is cheap (single aggregate over a tiny table) and runs on every
    tool call rather than being cached, so a freshly-imported DB starts
    returning real data on the very next call without restarting the server.
    """
    rows = query_to_json(conn, "SELECT COUNT(*) AS n FROM imports", lock=lock)
    return bool(rows) and int(rows[0]["n"]) > 0


def _coerce(value: object) -> Any:
    """Convert a DuckDB-returned Python object into a JSON-safe value.

    The order matters: ``bool`` is a subclass of ``int`` in Python, so it
    must be tested first to avoid ``True`` rendering as ``1``. ``datetime``
    is a subclass of ``date`` for the same reason.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, _dt.datetime):
        # TIMESTAMPTZ columns surface as tz-aware ``datetime``; isoformat
        # keeps the offset in the wire payload so a downstream LLM can
        # render in any zone without consulting the server's session TZ.
        # Naive datetimes (legacy TIMESTAMP columns the schema no longer
        # declares; defensive) fall through to the same path and serialise
        # without an offset suffix.
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, _dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, _dt.timedelta):
        # DuckDB INTERVAL columns surface as timedelta; ISO-ish representation
        # keeps the result JSON-serialisable while preserving the duration.
        return str(value)
    if isinstance(value, float):
        # JSON has no NaN / Infinity; fall back to the textual repr so
        # the column still appears with a deterministic value rather than
        # silently dropping out of the row.
        if not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        # ``json.dumps`` rejects Decimal; stringify rather than lose precision
        # by converting to float.
        return str(value)
    if isinstance(value, bytes):
        # BLOB columns are rare in this schema (route_points / ecg_samples
        # are numeric) but handle them defensively.
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    # Unknown types fall back to ``str`` so the column never disappears.
    return str(value)


def query_to_json(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | tuple[Any, ...] = (),
    *,
    lock: Lock | None = None,
) -> list[dict[str, Any]]:
    """Execute ``sql`` against ``conn`` and return the rows as dicts.

    ``lock`` is required when the connection is shared between coroutines so
    the asynchronous tool handlers don't race on the underlying cursor (the
    DuckDB Python connection is not thread-safe). Tests bypass the lock by
    passing ``None``.
    """
    if lock is None:
        return _execute(conn, sql, params)
    with lock:
        return _execute(conn, sql, params)


def _execute(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | tuple[Any, ...],
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, list(params))
    description = cursor.description or []
    columns = [d[0] for d in description]
    rows = cursor.fetchall()
    return [{col: _coerce(val) for col, val in zip(columns, row, strict=False)} for row in rows]


def run_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | tuple[Any, ...] = (),
    *,
    lock: Lock | None = None,
    require_data: bool = True,
) -> str:
    """Execute ``sql`` and return a pretty-printed JSON array string.

    Mirrors the Rust contract: errors come back as ``"Error: <msg>"`` rather
    than raising, so the MCP client always gets a string body. Logged at
    debug level because the SQL may include sensitive values; production
    logging defaults to INFO, so query bodies stay out of the standard log
    stream unless the operator opts in.

    When ``require_data`` is ``True`` (the default), an empty ``imports``
    table short-circuits with :data:`IMPORT_REQUIRED_MESSAGE` so a freshly
    installed server returns actionable guidance instead of an empty list
    that an LLM might interpret as "the user has no heart-rate data".
    ``get_import_history`` is the single tool that opts out.
    """
    try:
        if require_data and not imports_present(conn, lock=lock):
            return IMPORT_REQUIRED_MESSAGE
        rows = query_to_json(conn, sql, params, lock=lock)
    except Exception as exc:
        _logger.debug("query failed: %s", exc)
        return f"Error: {exc}"
    return json.dumps(rows, indent=2, ensure_ascii=False)


def run_query_payload(payload: object) -> str:
    """Pretty-print an already-built tool response payload."""
    return json.dumps(payload, indent=2, ensure_ascii=False)
