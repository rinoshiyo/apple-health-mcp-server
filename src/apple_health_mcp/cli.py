"""Command-line interface for the Apple Health MCP server.

Two subcommands:

* ``import <export>`` -- ingest an Apple Health export into the local DB
  via :func:`apple_health_mcp.importers.run_import`.
* ``serve`` -- run the FastMCP server. Defaults to stdio (so it drops into
  Claude Desktop / Codex / Cursor as-is); HTTP is opt-in via
  ``--transport http --port 8080``.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path

import typer

from apple_health_mcp.logging_config import configure_logging

app = typer.Typer(
    name="apple-health-mcp-server",
    help="Apple Health MCP server command-line interface.",
    no_args_is_help=True,
)

_logger = logging.getLogger(__name__)


class Transport(StrEnum):
    """Supported MCP transports. Default is stdio per project conventions."""

    STDIO = "stdio"
    HTTP = "http"


@app.callback()
def _root(
    ctx: typer.Context,
    db: Path | None = typer.Option(
        None,
        "--db",
        help="DuckDB path override (default: XDG_DATA_HOME/apple-health-mcp/health.duckdb).",
    ),
) -> None:
    """Configure logging once and stash global options on the context."""
    configure_logging()
    ctx.obj = {"db": db}


@app.command(name="import")
def import_cmd(
    ctx: typer.Context,
    export_path: Path = typer.Argument(
        ...,
        help="Path to the Apple Health extracted export directory.",
    ),
) -> None:
    """Import an Apple Health export into the local DuckDB database."""
    # Imported lazily so `apple-health-mcp-server serve` does not pay the
    # importer / lxml import cost on every CLI invocation.
    from apple_health_mcp.exceptions import AppleHealthMCPError
    from apple_health_mcp.importers import run_import

    db: Path | None = ctx.obj["db"]
    _logger.info("import invoked: export=%s db=%s", export_path, db)
    try:
        stats = run_import(export_path, db)
    except AppleHealthMCPError as exc:
        _logger.error("import failed: %s", exc)
        raise typer.Exit(code=1) from exc
    _logger.info(
        "import complete: records=%d workouts=%d ecg_readings=%d route_points=%d",
        stats.records,
        stats.workouts,
        stats.ecg_readings,
        stats.route_points,
    )


@app.command()
def serve(
    ctx: typer.Context,
    transport: Transport = typer.Option(
        Transport.STDIO,
        "--transport",
        case_sensitive=False,
        help="Transport: 'stdio' (default) or 'http'.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="HTTP bind host when --transport=http.",
    ),
    port: int = typer.Option(
        8080, "--port", min=1, max=65535, help="HTTP port when --transport=http."
    ),
) -> None:
    """Run the Apple Health MCP server."""
    # Imported lazily so `apple-health-mcp import ...` does not pay the
    # FastMCP / mcp import cost on every CLI invocation.
    from apple_health_mcp.exceptions import AppleHealthMCPError
    from apple_health_mcp.server import run_server

    db: Path | None = ctx.obj["db"]
    _logger.info(
        "serve invoked: transport=%s host=%s port=%s db=%s",
        transport.value,
        host,
        port,
        db,
    )
    try:
        asyncio.run(run_server(db, transport.value, host=host, port=port))
    except AppleHealthMCPError as exc:
        # Surface a typed exit instead of a raw traceback. The most common
        # cause is ``DatabaseError`` from a fresh install that hasn't run
        # ``apple-health-mcp import`` yet.
        _logger.error("server failed to start: %s", exc)
        raise typer.Exit(code=1) from exc


def main() -> None:
    """Console-script entry point."""
    app()
