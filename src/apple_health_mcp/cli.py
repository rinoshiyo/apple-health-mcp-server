"""Command-line interface for the Apple Health MCP server.

This module currently exposes ``import`` and ``serve`` stub subcommands.
Concrete behavior is implemented in later milestones (importers / server).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

import typer

from apple_health_mcp.logging_config import configure_logging

app = typer.Typer(
    name="apple-health-mcp",
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
        help="Path to the Apple Health export.zip or extracted directory.",
    ),
) -> None:
    """Import an Apple Health export into the local DuckDB database."""
    db: Path | None = ctx.obj["db"]
    _logger.info("import stub invoked: export=%s db=%s", export_path, db)


@app.command()
def serve(
    ctx: typer.Context,
    transport: Transport = typer.Option(
        Transport.STDIO,
        "--transport",
        case_sensitive=False,
        help="Transport: 'stdio' (default) or 'http'.",
    ),
    port: int = typer.Option(
        8080, "--port", min=1, max=65535, help="HTTP port when --transport=http."
    ),
) -> None:
    """Run the Apple Health MCP server."""
    db: Path | None = ctx.obj["db"]
    _logger.info("serve stub invoked: transport=%s port=%s db=%s", transport.value, port, db)


def main() -> None:
    """Console-script entry point."""
    app()
