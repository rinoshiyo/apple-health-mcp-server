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
    pool is tuned via ``PRAGMA threads``. For ``read_only=True`` we still
    open the file even if it does not yet exist: a fresh install bootstraps
    an empty schema-only DB at the requested path via
    :func:`_materialise_empty_db` so the MCP client can list tools and each
    tool can surface the standard "run import first" guidance. A WARNING
    is logged when the bootstrap fires so a typo'd ``--db`` does not
    silently masquerade as a successful install.

    Issue #109 follow-up (PR-F /code-review F1): when ``read_only=True``
    against an existing file, probe :func:`_migrate_if_needed` first.
    Read-only handles cannot run ALTER TABLE, so a v0.2.x DB upgraded to
    a v0.3.0+ package would otherwise serve the old shape (VARCHAR
    ``heart_rate_samples.sample_time``) instead of the new one (DOUBLE).
    The probe opens the file briefly in writable mode, runs any pending
    migrations, then closes and re-opens read-only.
    """
    resolved = db_path if db_path is not None else default_db_path()
    if read_only:
        if not resolved.exists():
            _materialise_empty_db(resolved)
        else:
            _migrate_if_needed(resolved)
    else:
        _ensure_parent_dir(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    if not read_only:
        conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    _apply_session_tz(conn)
    return conn


def _migrate_if_needed(db_path: Path) -> None:
    """Run pending migrations on ``db_path`` when the on-disk schema is behind.

    Read-only ``serve`` callers cannot ALTER TABLE in-place, so a DB that
    was last touched by an older package version would otherwise keep
    serving the old schema (e.g. VARCHAR ``heart_rate_samples.sample_time``
    after a v0.2.x → v0.3.0 upgrade). We briefly open ``db_path`` in
    writable mode, probe ``imports.schema_version`` via the migration
    layer, and apply pending steps only when the persisted version trails
    :data:`CURRENT_SCHEMA_VERSION`. Already-current DBs avoid the write
    open entirely.

    Skip cases:

    * Very-pre-v0.1.4 DBs that lack the ``imports`` table fall through to
      the existing tool-level error handling rather than crash here. The
      probe is also a useful guard against opening a writable handle on
      a file that is not actually our DB (DuckDB raises clearly if the
      magic does not match, but skipping the probe early keeps the
      common case cheap).
    * Already-current DBs (``current == CURRENT_SCHEMA_VERSION``) skip
      the writable reopen entirely.

    Imported lazily to avoid a top-level circular import between
    ``db.connection`` and ``db.migrations``.
    """
    from apple_health_mcp.db.migrations import (
        CURRENT_SCHEMA_VERSION,
        apply_pending_migrations,
        get_current_version,
    )

    probe = duckdb.connect(str(db_path), read_only=False)
    try:
        # Defer to the tool-level error path when the DB pre-dates the
        # ``imports`` table; a probe-time crash here would hide the
        # better "run import first" guidance the tool layer would give.
        if not _table_exists_in_main_conn(probe, "imports"):
            return
        current = get_current_version(probe)
        if current >= CURRENT_SCHEMA_VERSION:
            return
        _logger.info(
            "migrating existing DB from schema v%d to v%d before opening read-only",
            current,
            CURRENT_SCHEMA_VERSION,
        )
        apply_pending_migrations(probe)
    finally:
        probe.close()


def _table_exists_in_main_conn(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Return True when ``name`` exists as a table in the connection's ``main`` schema.

    Local duplicate of :func:`db.migrations._table_exists_in_main` so the
    ``connection`` module can probe without importing the migrations
    module at parse time (the lazy import inside
    :func:`_migrate_if_needed` is the load-bearing one; this helper runs
    on every read-only open).
    """
    row = conn.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main' LIMIT 1",
        [name],
    ).fetchone()
    return row is not None


def _materialise_empty_db(db_path: Path) -> None:
    """Bootstrap ``db_path`` as a schema-only DuckDB file, atomically.

    Writes the schema to a per-process temporary file alongside the final
    path and atomically renames it into place at the end. The all-or-nothing
    rename guarantees that:

    * A crash partway through ``ensure_schema`` (KeyboardInterrupt, disk
      full, schema error) leaves no half-initialised file at ``db_path`` —
      the next ``serve`` invocation will hit the missing-file branch again
      and re-bootstrap cleanly. Without this, an aborted bootstrap would
      leave a real file on disk that the next run's ``exists()`` check
      mistakes for a complete DB, then every tool errors with
      ``Error: Table imports does not exist`` instead of returning
      ``IMPORT_REQUIRED_MESSAGE``.
    * Two concurrent ``serve`` processes (Claude Desktop + Claude Code
      launched together against the same default XDG path before any
      import) each write to a distinct ``<pid>``-suffixed temp file; the
      first ``os.replace`` wins and the loser's temp file is removed.
      Neither process crashes at startup, and only one bootstrap survives.
    * If a legitimate ``import`` lands real data at ``db_path`` between
      our ``exists()`` check and the rename, ``os.replace`` is skipped so
      we never clobber user data with our empty scaffold.

    The schema is built via ``ensure_schema`` + ``apply_pending_migrations``
    so the bootstrap path stamps the same ``schema_version`` row the import
    path would; otherwise a future v2 migration would re-run v1's ALTERs
    against tables that already carry the v2 shape.

    Imported lazily to avoid a top-level circular import between
    ``db.connection`` and ``db.schema`` / ``db.migrations``.
    """
    from apple_health_mcp.db.migrations import apply_pending_migrations
    from apple_health_mcp.db.schema import ensure_schema

    _logger.warning(
        "no DuckDB file at %s — bootstrapping an empty schema-only DB so the "
        "MCP server can start. If this path is wrong (typo in --db, missing "
        "APPLE_HEALTH_TZ env, etc.), the server will keep returning the "
        "'run import first' guidance until the path matches your real import.",
        db_path,
    )
    _ensure_parent_dir(db_path)
    tmp_path = db_path.with_name(f"{db_path.name}.bootstrap.{os.getpid()}")
    if tmp_path.exists():
        # Stale leftover from a previous crash in the same PID slot.
        tmp_path.unlink()
    try:
        bootstrap = duckdb.connect(str(tmp_path), read_only=False)
        try:
            bootstrap.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
            ensure_schema(bootstrap)
            apply_pending_migrations(bootstrap)
        finally:
            bootstrap.close()
        if not db_path.exists():
            os.replace(str(tmp_path), str(db_path))
        else:  # pragma: no cover - timing-dependent concurrent race
            tmp_path.unlink()
    except BaseException:
        # ``missing_ok=True`` collapses the "did the tmp file ever get
        # materialised before the crash?" branch into one cleanup call;
        # the answer doesn't change what we do, only whether unlink
        # would otherwise raise.
        tmp_path.unlink(missing_ok=True)
        raise


def get_in_memory_connection() -> duckdb.DuckDBPyConnection:
    """Open an ephemeral in-memory DuckDB connection.

    Used by the test suite and any caller that wants schema isolation without
    touching the filesystem.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
    _apply_session_tz(conn)
    return conn
