"""DuckDB connection management with XDG-compliant default paths.

Default location resolution follows project convention:

* Linux / macOS: ``${XDG_DATA_HOME:-~/.local/share}/apple-health-mcp/health.duckdb``
* Windows: ``%LOCALAPPDATA%\\apple-health-mcp\\health.duckdb``

The parent directory is created on demand with mode ``0700`` on POSIX so
local health data is not world-readable by default.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import duckdb

_logger = logging.getLogger(__name__)

_APP_DIR_NAME = "apple-health-mcp"
_DB_FILE_NAME = "health.duckdb"


def default_db_path() -> Path:
    """Return the platform-appropriate default DuckDB path.

    On Windows we honour ``LOCALAPPDATA`` and fall back to ``~/AppData/Local``
    when the environment variable is unset (unlikely outside of stripped CI
    images, but the fallback keeps the call total).
    """
    if sys.platform == "win32":
        base_env = os.environ.get("LOCALAPPDATA")
        base = Path(base_env) if base_env else Path.home() / "AppData" / "Local"
    else:
        base_env = os.environ.get("XDG_DATA_HOME")
        base = Path(base_env) if base_env else Path.home() / ".local" / "share"
    return base / _APP_DIR_NAME / _DB_FILE_NAME


def _ensure_parent_dir(db_path: Path) -> None:
    parent = db_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # Tighten only when we own the directory; ignore failures on shared
        # mounts where the user lacks chmod rights.
        try:
            parent.chmod(0o700)
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            _logger.debug("could not chmod %s to 0700: %s", parent, exc)


def get_connection(
    db_path: Path | None = None,
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open (or create) a DuckDB connection at ``db_path``.

    When ``db_path`` is ``None`` the XDG-compliant default is used. The
    parent directory is created on demand. ``read_only=True`` opens the
    database without acquiring a write lock; the ``serve`` subcommand uses
    that mode so MCP queries never block ``import`` runs.
    """
    resolved = db_path if db_path is not None else default_db_path()
    _ensure_parent_dir(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    conn.execute("PRAGMA threads=4;")
    return conn


def get_in_memory_connection() -> duckdb.DuckDBPyConnection:
    """Open an ephemeral in-memory DuckDB connection.

    Used by the test suite and any caller that wants schema isolation without
    touching the filesystem.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("PRAGMA threads=4;")
    return conn
