"""Command-line interface for the Apple Health MCP server.

This module currently exposes ``import`` and ``serve`` stub subcommands.
Concrete behavior is implemented in later milestones (importers / server).
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from apple_health_mcp.logging_config import configure_logging

app = typer.Typer(
    name="apple-health-mcp",
    help="Apple Health MCP server command-line interface.",
    no_args_is_help=True,
)

_logger = logging.getLogger(__name__)


@app.command(name="import")
def import_cmd(
    export_path: Path = typer.Argument(
        ...,
        exists=False,
        help="Path to the Apple Health export.zip or extracted directory.",
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="DuckDB path override (default: XDG_DATA_HOME/apple-health-mcp/health.duckdb).",
    ),
) -> None:
    """Import an Apple Health export into the local DuckDB database."""
    configure_logging()
    _logger.info("import stub invoked: export=%s db=%s", export_path, db)


@app.command()
def serve(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="Transport: 'stdio' (default) or 'http'.",
    ),
    port: int = typer.Option(8080, "--port", help="HTTP port when --transport=http."),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="DuckDB path override (default: XDG_DATA_HOME/apple-health-mcp/health.duckdb).",
    ),
) -> None:
    """Run the Apple Health MCP server."""
    configure_logging()
    _logger.info("serve stub invoked: transport=%s port=%s db=%s", transport, port, db)


def main() -> None:
    """Console-script entry point."""
    app()
