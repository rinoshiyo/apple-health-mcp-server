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
import re
import sys
from pathlib import Path

import duckdb

from apple_health_mcp.exceptions import ConfigError

_logger = logging.getLogger(__name__)

_APP_DIR_NAME = "apple-health-mcp"
_DB_FILE_NAME = "health.duckdb"
_DEFAULT_THREADS = 4
_TZ_ENV_VAR = "APPLE_HEALTH_TZ"
# IANA TZ names are alphanumerics plus '/', '_', '+', '-'. DuckDB's
# `SET TimeZone = '...'` cannot be parameterised, so we validate against
# this whitelist before interpolating to keep the surface free of SQL
# injection even when the value comes from an env var.
_TZ_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-/]*$")


def _apply_session_tz(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply ``APPLE_HEALTH_TZ`` to the connection's session TZ when set.

    When the env var is empty/unset DuckDB keeps its own default (OS local
    TZ), which is what we want for the common single-machine case. The
    override is for users on globally-mobile or DST-active data who need
    a stable rendering TZ across imports.
    """
    tz = os.environ.get(_TZ_ENV_VAR, "").strip()
    if not tz:
        return
    if not _TZ_NAME_RE.fullmatch(tz):
        raise ConfigError(
            f"invalid {_TZ_ENV_VAR}={tz!r}: expected an IANA timezone like 'Asia/Tokyo'"
        )
    conn.execute(f"SET TimeZone = '{tz}';")


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
            # A fresh install hasn't run `apple-health-mcp-server import`
            # yet, but the MCP client still expects to connect and list
            # tools. Materialise an empty schema-only DB so the read-only
            # open below succeeds; the tools then return
            # ``IMPORT_REQUIRED_MESSAGE`` because the ``imports`` table is
            # empty (see ``server/query.py``).
            _materialise_empty_db(resolved)
    else:
        _ensure_parent_dir(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    if not read_only:
        conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    _apply_session_tz(conn)
    return conn


def _materialise_empty_db(db_path: Path) -> None:
    """Create ``db_path`` as a schema-only DuckDB file.

    Imported lazily to avoid a top-level circular import between
    ``db.connection`` and ``db.schema``. The writable connection is closed
    immediately so the caller can re-open in read-only mode without DuckDB
    rejecting the second handle as conflicting.
    """
    from apple_health_mcp.db.schema import ensure_schema

    _ensure_parent_dir(db_path)
    bootstrap = duckdb.connect(str(db_path), read_only=False)
    try:
        bootstrap.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
        ensure_schema(bootstrap)
    finally:
        bootstrap.close()


def get_in_memory_connection() -> duckdb.DuckDBPyConnection:
    """Open an ephemeral in-memory DuckDB connection.

    Used by the test suite and any caller that wants schema isolation without
    touching the filesystem.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    _apply_session_tz(conn)
    return conn
