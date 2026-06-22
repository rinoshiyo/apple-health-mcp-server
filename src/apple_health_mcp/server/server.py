"""FastMCP server bootstrap and transport switching.

The server runs in two modes:

* ``stdio`` (default): the typical Claude Desktop / Codex / Cursor wiring,
  with MCP frames flowing through stdin / stdout. **No other writer may
  touch stdout** — every log line is routed to stderr by
  :func:`apple_health_mcp.logging_config.configure_logging`.
* ``http``: Streamable HTTP on ``http://<host>:<port>/mcp``, opt-in via
  ``apple-health-mcp serve --transport http``.

The DB connection is opened read-only so a misbehaving LLM-issued query
cannot mutate the local DuckDB file (and ``server/safety`` adds a second
layer of validation on top of read-only mode).
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

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

    ``conn`` is the (read-only) DuckDB handle the tools query against. The
    connection is wrapped in a process-wide ``Lock`` because DuckDB's Python
    binding is not safe to share across coroutines without serialisation.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name, host=host, port=port)
    lock = Lock()
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
    """Open the DB read-only and serve MCP over ``transport``.

    Valid ``transport`` values are ``"stdio"`` and ``"http"``; anything else
    raises :class:`ConfigError` so the CLI surfaces a clear message rather
    than a confusing AttributeError from FastMCP.
    """
    conn = get_connection(db_path, read_only=True)
    mcp = create_server(conn, host=host, port=port)
    if transport == "stdio":
        _logger.info("MCP server running on stdio")
        await mcp.run_stdio_async()
    elif transport == "http":
        _logger.info("MCP server listening at http://%s:%s/mcp", host, port)
        await mcp.run_streamable_http_async()
    else:
        raise ConfigError(f'Unknown transport: {transport}. Expected "stdio" or "http".')
