"""Command-line interface for the Apple Health MCP server.

Two subcommands:

* ``import <export.zip>`` -- ingest an Apple Health export ZIP into the
  local DB. The CLI extracts the ZIP into a temp directory and drives
  the same pipeline the MCP ``import_zip`` tool uses, so the two
  entry points stamp the same ``imports.source_zip_sha256`` triple
  and idempotency works across CLI / MCP boundaries (v0.5, issue #170).
* ``serve`` -- run the FastMCP server. Defaults to stdio (so it drops into
  Claude Desktop / Codex / Cursor as-is); HTTP is opt-in via
  ``--transport http --port 8080``.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
        help=(
            "DuckDB path override (default: XDG_DATA_HOME/apple-health-mcp/health.duckdb "
            "on POSIX, %LOCALAPPDATA%/apple-health-mcp/health.duckdb on Windows). "
            "Overrides APPLE_HEALTH_DB / APPLE_HEALTH_DATA_DIR."
        ),
    ),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help=(
            "Session timezone for rendering TIMESTAMPTZ columns (e.g. 'Asia/Tokyo'). "
            "Overrides APPLE_HEALTH_TZ. When neither is set, DuckDB renders in the "
            "operating system's local timezone."
        ),
    ),
) -> None:
    """Configure logging once and stash global options on the context."""
    configure_logging()
    # The DB connection layer reads APPLE_HEALTH_TZ when it opens a
    # connection (see db/connection.py::_apply_session_tz). Promote the
    # flag into the env so import / serve / any nested caller picks it up
    # without having to thread it through every function signature.
    if tz is not None:
        os.environ["APPLE_HEALTH_TZ"] = tz
    # Mirror the --tz / APPLE_HEALTH_TZ promotion pattern for the DB
    # override: a caller that resolves through resolve_db_path() (e.g.
    # a future subcommand, a plugin, a get_server_info diagnostic
    # helper) would otherwise see the env-only view and ignore the
    # ``--db`` the user typed on the CLI -- the resolver claims to be
    # the single source of truth, but two parallel paths would drift
    # without this promotion. ``.expanduser().resolve()`` pins to an
    # absolute path so the env value the resolver picks up is
    # CWD-independent (relative paths there would round-trip back into
    # the same ConfigError the resolver raises for relative env input).
    if db is not None:
        os.environ["APPLE_HEALTH_DB"] = str(db.expanduser().resolve())
    ctx.obj = {"db": db}


@app.command(name="import")
def import_cmd(
    ctx: typer.Context,
    zip_path: Path = typer.Argument(
        ...,
        help=(
            "Path to the Apple Health export ZIP file (the file the Health "
            "app produces via Share → Export). v0.5 dropped directory "
            "acceptance — the CLI now extracts the ZIP internally so the "
            "user never has to unzip manually."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Bypass the export.xml sha256 fast path so the importer still "
            "runs even when the file is byte-identical to the last import. "
            "The incremental hash-set skip (Tier 2) remains active, so the "
            "re-import stays cheap and the DB does not balloon with "
            "tombstones. Use when you need to re-verify the importer "
            "against an unchanged export."
        ),
    ),
) -> None:
    """Import an Apple Health export ZIP into the local DuckDB database.

    v0.5 (issue #170): the subcommand accepts a ``.zip`` file path
    only -- directory acceptance was removed so the CLI and the MCP
    ``import_zip`` tool stamp the same ``imports.source_zip_*`` triple
    and idempotency works uniformly across both entry points.
    """
    # Imported lazily so `apple-health-mcp-server serve` does not pay the
    # importer / lxml / zipfile import cost on every CLI invocation.
    import zipfile
    from datetime import UTC, datetime

    from apple_health_mcp._zip_util import (
        ZipInspection,
        inspect_zip,
        stream_sha256,
    )
    from apple_health_mcp.exceptions import AppleHealthMCPError
    from apple_health_mcp.importers.zip_extract import extract_zip_and_import

    db: Path | None = ctx.obj["db"]
    _logger.info("import invoked: zip=%s db=%s force=%s", zip_path, db, force)

    # Validate path shape early so the user gets a clear error before
    # the importer modules load lxml / pyarrow.
    if not zip_path.exists():
        _logger.error("import failed: %s does not exist", zip_path)
        raise typer.Exit(code=1)
    if zip_path.is_dir():
        _logger.error(
            "import failed: %s is a directory. v0.5 requires a .zip file path; "
            "the CLI extracts the ZIP internally (see CHANGELOG and issue #170).",
            zip_path,
        )
        raise typer.Exit(code=1)

    inspection = inspect_zip(zip_path)
    if inspection == ZipInspection.INVALID_ZIP:
        _logger.error(
            "import failed: %s is not a valid ZIP archive. The file may be "
            "corrupted, partially downloaded, or have a .zip extension by "
            "mistake. Re-download or re-export your Apple Health data.",
            zip_path,
        )
        raise typer.Exit(code=1)
    if inspection == ZipInspection.VALID_NON_APPLE_HEALTH:
        _logger.error(
            "import failed: %s is a valid ZIP but does not contain "
            "apple_health_export/export.xml or export.xml at the top level. "
            "Did you mean a different ZIP?",
            zip_path,
        )
        raise typer.Exit(code=1)

    stat = zip_path.stat()
    sha = stream_sha256(zip_path)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    source_zip = (sha, mtime, stat.st_size)

    try:
        stats = extract_zip_and_import(zip_path, source_zip, db_path=db, force=force)
    except zipfile.BadZipFile as exc:
        # Extraction-phase failure: ``zip_extract`` re-raises any OSError
        # from ``extractall`` as BadZipFile so the two corruption / IO
        # failure modes share one recovery path. Importer-phase OSError
        # (DuckDB writes, ECG / GPX file IO) bypasses this branch and
        # falls through to the AppleHealthMCPError handler below — or
        # propagates uncaught when it isn't a typed AppleHealthMCPError
        # (rare; would indicate an importer bug worth surfacing).
        _logger.error("import failed: failed to extract %s: %s", zip_path, exc)
        raise typer.Exit(code=1) from exc
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
