"""``get_server_info`` MCP tool — runtime self-diagnosis primitive."""

from __future__ import annotations

import os
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp import __version__
from apple_health_mcp.db.connection import _DATA_DIR_ENV_VAR, _DB_ENV_VAR
from apple_health_mcp.server.query import run_query_payload

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Return the server's runtime state for self-diagnosis. Fields: "
    "db_path (absolute path of the DuckDB file the server has open), "
    "version (server version string), record_count (rows in the records "
    "table, 0 on a fresh DB without an imports table), config_source "
    "(which tier of resolve_db_path() supplied the path: "
    "'env:APPLE_HEALTH_DB', 'env:APPLE_HEALTH_DATA_DIR', or "
    "'platform_default'). Use this when troubleshooting a 'no data' "
    "response to confirm the server opened the same DB file the "
    "importer wrote to — the canonical symptom of the Windows MSIX "
    "AppContainer %LOCALAPPDATA% sandbox redirect."
)


def _resolve_config_source() -> str:
    """Mirror :func:`resolve_db_path` precedence to label the active tier.

    Reads the env vars itself instead of asking the resolver to surface
    the source via a richer return type, so the resolver's call signature
    stays a bare ``-> Path`` (every existing caller — including the
    cache-friendly :data:`default_db_path` alias — keeps its current
    contract). The trade-off is that the labels here must be kept in
    lock-step with the resolver's branch order; the test suite locks
    that pairing in.

    The strip-then-bool check mirrors the resolver's
    blank-after-strip-falls-through rule so a shell that does
    ``export APPLE_HEALTH_DB=`` is reported as ``platform_default``,
    matching what the connection layer actually opened.
    """
    if (os.environ.get(_DB_ENV_VAR) or "").strip():
        return f"env:{_DB_ENV_VAR}"
    if (os.environ.get(_DATA_DIR_ENV_VAR) or "").strip():
        return f"env:{_DATA_DIR_ENV_VAR}"
    return "platform_default"


def _records_count_or_zero(conn: duckdb.DuckDBPyConnection) -> int:
    """Count rows in ``records`` or return 0 on a fresh / wrong DB.

    A bootstrap (schema-only) DB has the table but zero rows -> returns 0.
    A DB that lacks the ``records`` table entirely (the caller pointed
    ``APPLE_HEALTH_DB`` at an unrelated DuckDB file) also returns 0 so
    the diagnostic surface stays a stable shape — the ``db_path`` field
    already gives the user enough to spot a misconfiguration, and a
    raised exception inside the diagnostic tool would defeat its
    purpose. The catch is intentionally broad: DuckDB's
    "Table records does not exist" surfaces as a ``CatalogException``
    today but the binding has changed exception types between minor
    releases more than once.
    """
    try:
        row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
    except Exception:  # pragma: no cover - defensive against alien DBs
        return 0
    return int(row[0]) if row is not None and row[0] is not None else 0


def _open_db_path(conn: duckdb.DuckDBPyConnection) -> str:
    """Report the file path the live connection is reading from.

    Asks DuckDB directly via ``PRAGMA database_list`` rather than
    re-resolving via :func:`resolve_db_path`, because the diagnostic
    contract is "what the server actually has open right now", not
    "what would be resolved on a fresh boot". The two should agree
    on a healthy run, but on a bug they will diverge — and the
    bug-finding value of this tool comes from reporting the open
    handle's truth, not the resolver's restatement of it.

    Result columns are ``(seq, name, file)``. On-disk DBs come back
    with ``file`` populated (the absolute path); in-memory DBs come
    back with ``name = 'memory'`` and ``file = NULL`` — collapse the
    latter to the canonical ``":memory:"`` sentinel that DuckDB
    itself uses as the open string, so a test fixture against an
    in-memory connection still gets a deterministic, human-readable
    value instead of ``"<unknown>"``.
    """
    rows = conn.execute("PRAGMA database_list").fetchall()
    # PRAGMA database_list returns ``(seq, name, file)``. For on-disk
    # connections the ``name`` is the file's stem (NOT the literal
    # ``"main"``); for in-memory it's the sentinel ``"memory"`` with
    # ``file = NULL``. Walk in the order DuckDB returns them and take
    # the first row that looks like a primary DB:
    #
    # - ``name == "memory"`` → return the canonical ``":memory:"``
    #   sentinel so test fixtures using ``get_in_memory_connection``
    #   produce a deterministic, human-readable diagnostic value.
    # - non-empty ``file`` → the path the connection has open.
    #
    # The package itself never ATTACHes, so a single iteration over
    # the (typically one-element) list is enough; the explicit
    # non-empty check on ``file_val`` (rather than a bare truthiness
    # test) guards against a future DuckDB version returning ``""``
    # for an unopened slot — that would silently masquerade as an
    # on-disk path of length zero without this guard.
    for row in rows:
        if len(row) < 3:  # pragma: no cover - DuckDB always returns 3 columns
            continue
        name, file_val = row[1], row[2]
        if name == "memory":
            return ":memory:"
        if file_val is not None and file_val != "":  # pragma: no branch
            return str(file_val)
    return "<unknown>"  # pragma: no cover - DuckDB always lists at least one DB


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_server_info() -> str:
        # Hold the lock across BOTH the DuckDB queries AND the env
        # inspection so the four fields form a consistent snapshot.
        # ``_resolve_config_source`` reads ``os.environ`` directly --
        # in the (rare) case where another thread re-exports an env
        # var mid-call, deciding "snapshot or live read" between
        # config_source and db_path / record_count would break the
        # diagnostic invariant the docstring promises. Holding the
        # lock here gives all four fields the same point-in-time view.
        with lock:
            db_path = _open_db_path(conn)
            record_count = _records_count_or_zero(conn)
            config_source = _resolve_config_source()
        info = {
            "db_path": db_path,
            "version": __version__,
            "record_count": record_count,
            "config_source": config_source,
        }
        # Reuse the package-wide payload serialiser so any future
        # change to indent / ensure_ascii / coercion rules lands in
        # one place (server/query.py::run_query_payload).
        return run_query_payload(info)
