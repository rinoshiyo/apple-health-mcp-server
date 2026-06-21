"""DuckDB connection management with XDG-compliant default paths.

Default location resolution follows project convention:

* Linux / macOS: ``${XDG_DATA_HOME:-~/.local/share}/apple-health-mcp/health.duckdb``
* Windows: ``%LOCALAPPDATA%\\apple-health-mcp\\health.duckdb``

When the database is opened at the default path, the auto-created app
subdirectory is tightened to mode ``0700`` on POSIX so local health data is
not world-readable. User-supplied ``db_path`` values never have their parent
directory's permissions touched (the parent may be ``$HOME`` or ``/tmp``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import duckdb

from apple_health_mcp.exceptions import DatabaseError

_logger = logging.getLogger(__name__)

_APP_DIR_NAME = "apple-health-mcp"
_DB_FILE_NAME = "health.duckdb"
_DEFAULT_THREADS = 4


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
    """Create ``db_path.parent`` if missing, tightening it only when safe.

    The chmod 0700 only applies when the parent directory's basename matches
    the package's app directory (``apple-health-mcp``). User-supplied paths
    whose parent is ``$HOME``, ``/tmp``, a project dir, etc. are left alone
    — chmod-ing them would silently break sshd ``StrictModes`` and other
    tools that rely on conventional home-directory permissions.
    """
    parent = db_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32" and parent.name == _APP_DIR_NAME:
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

    When ``db_path`` is ``None`` the XDG-compliant default is used. For
    writable opens the parent directory is created on demand and the thread
    pool is tuned via ``PRAGMA threads``. For ``read_only=True`` we never
    create directories (the file is expected to already exist; raise a
    clear error if it does not) and skip the PRAGMA so a read-only MCP
    connection cannot perturb another process's thread-pool tuning.
    """
    resolved = db_path if db_path is not None else default_db_path()
    if read_only:
        if not resolved.exists():
            raise DatabaseError(
                f"cannot open read-only: database does not exist at {resolved} "
                "(run `apple-health-mcp import` first)"
            )
    else:
        _ensure_parent_dir(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    if not read_only:
        conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    return conn


def get_in_memory_connection() -> duckdb.DuckDBPyConnection:
    """Open an ephemeral in-memory DuckDB connection.

    Used by the test suite and any caller that wants schema isolation without
    touching the filesystem.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    return conn
