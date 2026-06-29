"""FastMCP server bootstrap and transport switching.

The server runs in two modes:

* ``stdio`` (default): the typical Claude Desktop / Codex / Cursor wiring,
  with MCP frames flowing through stdin / stdout. **No other writer may
  touch stdout** — every log line is routed to stderr by
  :func:`apple_health_mcp.logging_config.configure_logging`.
* ``http``: Streamable HTTP on ``http://<host>:<port>/mcp``, opt-in via
  ``apple-health-mcp serve --transport http``.

v0.4 (issue #148) opens the connection in **read-write** mode so the new
``import_zip`` MCP tool can drive ``run_import`` against the live handle
without forcing the server to close-and-reopen. The pre-v0.4 read-only
mode was a defence-in-depth layer on top of :mod:`server.safety`'s SQL
validator (it rejects every DDL / DML / ATTACH / COPY / PRAGMA / quoted-
path-FROM construct LLM-issued ``run_custom_query`` could otherwise
abuse); dropping it leaves the validator as the sole guard. The
trade-off is necessary to make zip-import-tool-from-the-agent a
first-class flow — see ``feedback_db_path_is_not_user_facing.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.db import import_jobs as job_registry
from apple_health_mcp.db.connection import get_connection
from apple_health_mcp.exceptions import ConfigError
from apple_health_mcp.server import tools as tools_pkg

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


def create_server(
    conn: duckdb.DuckDBPyConnection,
    *,
    name: str = "apple-health-mcp",
    host: str = "127.0.0.1",
    port: int = 8080,
) -> FastMCP:
    """Build a :class:`FastMCP` instance with every tool registered.

    ``conn`` is the DuckDB handle the tools query against. v0.4 opens
    it writable so the upcoming ``import_zip`` MCP tool can drive the
    importer inline; LLM-issued SELECT-only enforcement is the
    responsibility of :mod:`server.safety` (see its module docstring for
    the threat model). The connection is wrapped in a process-wide
    ``Lock`` because DuckDB's Python binding is not safe to share
    across coroutines without serialisation.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name, host=host, port=port)
    lock = Lock()
    # v0.5 (issue #157): boot-time orphan sweep. Any ``import_jobs`` row
    # left in ``queued`` / ``running`` from a prior process is owned by
    # a worker thread that no longer exists; rewrite it to ``error``
    # with ``reason='server_restarted_while_running'`` so the
    # multi-launch guard inside ``import_zip`` does not stay wedged on
    # a phantom job and the agent re-polling an old ``job_id`` after a
    # restart gets a definite terminal state.
    #
    # v0.5 code-review (PR #184 F7): guard against transient sweep
    # failure (DuckDB I/O error, file lock contention from an
    # orphaned importer process being cleaned up) so the failure does
    # not bring down server boot for every unrelated read tool too.
    # A logged warning + degraded boot is strictly better than the
    # whole MCP surface failing to come up.
    try:
        swept = job_registry.sweep_orphan_jobs(conn, lock)
    except Exception:  # pragma: no cover - boot-time IO errors are environment-specific
        _logger.warning(
            "Orphan import_jobs sweep failed at server boot; continuing in "
            "degraded mode (stale queued/running rows may persist until "
            "manually cleared).",
            exc_info=True,
        )
    else:
        if swept:
            _logger.info("Swept %d orphan import_jobs row(s) on server boot.", swept)
    for register in tools_pkg.ALL_TOOLS:
        register(mcp, conn, lock)
    return mcp


async def run_server(
    db_path: Path | None,
    transport: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    """Open the DB read-write and serve MCP over ``transport``.

    Valid ``transport`` values are ``"stdio"`` and ``"http"``; anything
    else raises :class:`ConfigError` so the CLI surfaces a clear message
    rather than a confusing AttributeError from FastMCP.

    v0.4 (issue #148) opens the handle writable so the new
    ``import_zip`` MCP tool can run the importer inline. SQL-level
    safety against an LLM-issued ``DELETE`` / ``DROP`` etc. is enforced
    by :func:`server.safety.validate_query` on every
    ``run_custom_query`` call; the connection-level read-only flag is
    no longer the second defence line it was in v0.3.x.
    """
    conn = get_connection(db_path, read_only=False)
    mcp = create_server(conn, host=host, port=port)
    if transport == "stdio":
        _logger.info("MCP server running on stdio")
        await mcp.run_stdio_async()
    elif transport == "http":
        _logger.info("MCP server listening at http://%s:%s/mcp", host, port)
        await mcp.run_streamable_http_async()
    else:
        raise ConfigError(f'Unknown transport: {transport}. Expected "stdio" or "http".')
