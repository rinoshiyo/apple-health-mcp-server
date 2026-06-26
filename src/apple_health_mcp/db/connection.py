"""DuckDB connection management with XDG-compliant default paths.

Path resolution (:func:`resolve_db_path`) precedence, most → least specific:

1. ``APPLE_HEALTH_DB`` — file path (``~`` expansion). Highest priority.
2. ``APPLE_HEALTH_DATA_DIR`` — directory path (``~`` expansion); default
   file name (``health.duckdb``) is appended directly under it (no
   ``apple-health-mcp/`` subdir).
3. Platform default:

   * Linux / macOS: ``${XDG_DATA_HOME:-~/.local/share}/apple-health-mcp/health.duckdb``
   * Windows: ``%LOCALAPPDATA%\\apple-health-mcp\\health.duckdb``

Both env vars are ``.strip()``-ed and rejected if blank-after-strip;
relative paths and obvious-misuse forms (``APPLE_HEALTH_DB`` pointing at
an existing directory, ``APPLE_HEALTH_DATA_DIR`` pointing at a
``*.duckdb`` file) raise :class:`ConfigError` so the user gets a
copy-pasteable hint instead of an opaque DuckDB I/O error downstream.

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
_DB_ENV_VAR = "APPLE_HEALTH_DB"
_DATA_DIR_ENV_VAR = "APPLE_HEALTH_DATA_DIR"
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


def resolve_db_path() -> Path:
    """Resolve the DuckDB path with environment-variable override precedence.

    Precedence (most → least specific):

    1. ``APPLE_HEALTH_DB`` — file path, ``~`` is expanded. Takes precedence
       over every other source. Use this when the caller wants the
       server / CLI to open a specific file (e.g. the MCPB bundle injects
       this from ``user_config.db_path`` to escape the Windows MSIX
       AppContainer redirect of ``%LOCALAPPDATA%``).
    2. ``APPLE_HEALTH_DATA_DIR`` — directory path, ``~`` is expanded, and the
       default file name (``health.duckdb``) is appended. Use this when
       you want a custom data root but keep the package's file name.

       NOTE: unlike the platform default, ``APPLE_HEALTH_DATA_DIR`` is
       treated as the FINAL parent — the DB sits directly under it,
       without the ``apple-health-mcp/`` subdir. The env var is a
       deliberate opt-in override, so the caller owns the layout (and
       therefore also the responsibility for permissions; the auto
       ``chmod 0700`` in :func:`_ensure_parent_dir` only fires when
       the parent's basename matches the package).
    3. Platform default — XDG_DATA_HOME on POSIX, LOCALAPPDATA on
       Windows; both nested under ``apple-health-mcp/`` so the
       auto-chmod still applies.

    The server and CLI share this single resolver so any future env
    or launcher hook can only drift in one place.

    Validation: blank-after-strip env values fall through to the next
    tier so a shell rc that does ``export APPLE_HEALTH_DB=`` behaves
    the same as "unset". Relative paths, paths pointing at an existing
    directory (for ``APPLE_HEALTH_DB``), and ``*.duckdb`` file paths
    (for ``APPLE_HEALTH_DATA_DIR``) are rejected with
    :class:`ConfigError` so an obvious typo surfaces as actionable
    guidance instead of an opaque DuckDB I/O error downstream.
    """
    env_db_raw = os.environ.get(_DB_ENV_VAR)
    env_db = env_db_raw.strip() if env_db_raw else ""
    if env_db:
        candidate = Path(env_db).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(
                f"invalid {_DB_ENV_VAR}={env_db!r}: must be an absolute path "
                "(relative paths would resolve against the process working "
                "directory, which differs between CLI and server boots)"
            )
        if candidate.is_dir():
            raise ConfigError(
                f"invalid {_DB_ENV_VAR}={env_db!r}: points at an existing "
                "directory; expected a DuckDB file path (e.g. "
                f"{env_db.rstrip('/')}/{_DB_FILE_NAME})"
            )
        return candidate
    env_dir_raw = os.environ.get(_DATA_DIR_ENV_VAR)
    env_dir = env_dir_raw.strip() if env_dir_raw else ""
    if env_dir:
        if env_dir.lower().endswith(".duckdb"):
            raise ConfigError(
                f"invalid {_DATA_DIR_ENV_VAR}={env_dir!r}: ends in '.duckdb' "
                f"(looks like a file path); use {_DB_ENV_VAR} for file paths "
                f"or pass a directory that does not end in '.duckdb' here"
            )
        candidate = Path(env_dir).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(
                f"invalid {_DATA_DIR_ENV_VAR}={env_dir!r}: must be an "
                "absolute path (relative paths would resolve against the "
                "process working directory, which differs between CLI and "
                "server boots)"
            )
        return candidate / _DB_FILE_NAME
    return _platform_default_dir() / _DB_FILE_NAME


def _platform_default_dir() -> Path:
    """Return the package's platform-appropriate app data directory.

    Extracted from the historic :func:`default_db_path` so
    :func:`resolve_db_path` can reuse the platform-default lookup
    without inlining the OS branching. The returned directory always
    ends in the package's ``apple-health-mcp/`` subdir; this keeps
    :func:`_ensure_parent_dir`'s name-based ``chmod 0700`` guard
    firing for the package-owned case while leaving user-supplied
    or env-supplied paths untouched.

    On Windows we honour ``LOCALAPPDATA`` and fall back to
    ``~/AppData/Local`` when the environment variable is unset
    (unlikely outside of stripped CI images, but the fallback keeps
    the call total).
    """
    if sys.platform == "win32":
        base_env = os.environ.get("LOCALAPPDATA")
        base = Path(base_env) if base_env else Path.home() / "AppData" / "Local"
    else:
        base_env = os.environ.get("XDG_DATA_HOME")
        base = Path(base_env) if base_env else Path.home() / ".local" / "share"
    return base / _APP_DIR_NAME


def default_db_path() -> Path:
    """Return the resolved DuckDB path (backward-compatible alias).

    Identical to :func:`resolve_db_path`; kept so external callers and
    docs that reference ``default_db_path`` continue to work. New code
    should call :func:`resolve_db_path` directly so the env-override
    precedence is visible at the call site.
    """
    return resolve_db_path()


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

    Issue #124 (v0.3.0): when ``read_only=True`` against an existing
    file, probe :func:`_migrate_if_needed` first so a pre-v0.3.0 DB
    surfaces the canonical "please re-import" :class:`ConfigError` at
    server start instead of letting the tool layer return malformed
    data from an old-shape table (e.g. VARCHAR
    ``heart_rate_samples.sample_time``). v0.3.0 dropped automatic
    in-place upgrades; the probe either silently confirms the DB is
    current or raises :class:`ConfigError` carrying the re-import
    guidance.

    v0.4 (issue #148): the same legacy-DB probe also fires on the
    writable serve path (``read_only=False``, used by the new
    import-from-the-agent flow) so the `serve` startup contract
    matches across both transport modes. The writable probe uses the
    just-opened handle directly instead of taking a second read-only
    open of the same file (DuckDB rejects concurrent same-process
    opens of the same on-disk file when one of them is writable).
    """
    resolved = db_path if db_path is not None else resolve_db_path()
    # Snapshot existence BEFORE ``duckdb.connect`` runs: the writable
    # open creates the file on the spot, so a post-open ``exists()``
    # would always return True and the "fresh-install skip the
    # legacy-DB probe" branch would never fire.
    file_existed_before_open = resolved.exists()
    if read_only:
        if not file_existed_before_open:
            _materialise_empty_db(resolved)
        else:
            _migrate_if_needed_via_separate_probe(resolved)
    else:
        _ensure_parent_dir(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    if not read_only:
        conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
        # v0.4 writable serve path: probe the legacy-DB guard on the
        # live handle when the file already existed pre-open. A
        # pre-v0.4 DB raises ConfigError carrying the re-import command;
        # the connection is closed before re-raising so the file lock
        # does not survive a failed startup.
        if file_existed_before_open:
            try:
                _migrate_if_needed_on_handle(conn, resolved)
            except BaseException:
                conn.close()
                raise
    _apply_session_tz(conn)
    return conn


def _migrate_if_needed_via_separate_probe(db_path: Path) -> None:
    """Validate ``db_path``'s schema_version using a fresh read-only probe.

    Used by the read-only serve path (``read_only=True``). Opens a
    short-lived read-only handle, delegates to
    :func:`_migrate_if_needed_on_handle`, then closes the probe.

    The probe is opened ``read_only=True`` -- before v0.3.0 it was
    writable so the deleted in-place migration could ALTER, but the
    v0.3.0 path only reads ``schema_version`` and raises. Holding the
    writer lock just to read one integer serialised serve startup
    behind any concurrent importer or other serve process, and worse,
    a writable open on a refused DB could trigger DuckDB's internal
    storage-format upgrade -- mutating a file the package is about to
    refuse. The read-only probe avoids both.
    """
    probe = duckdb.connect(str(db_path), read_only=True)
    try:
        _migrate_if_needed_on_handle(probe, db_path)
    finally:
        probe.close()


def _migrate_if_needed_on_handle(
    conn: duckdb.DuckDBPyConnection,
    db_path: Path,
) -> None:
    """Shared schema_version validation for any caller-owned handle.

    v0.3.0 dropped automatic in-place schema upgrades from pre-v0.3.0
    DBs (:issue:`124`). Two outcomes only:

    * Current DB (``schema_version == CURRENT_SCHEMA_VERSION``) -> return
      silently. The serve path proceeds to its real open.
    * Pre-v0.3.0 / pre-v0.4 DB (``0 < schema_version <
      CURRENT_SCHEMA_VERSION`` AND no in-place migration can close
      the gap) -> raise :class:`ConfigError` carrying the canonical
      re-import guidance, with ``db_path`` interpolated so the user
      can copy-paste the ``rm`` / ``import`` commands verbatim.

    Skip case: very-pre-v0.1.4 DBs that lack the ``imports`` table
    fall through to the existing tool-level error handling rather
    than crash here.

    Imported lazily to avoid a top-level circular import between
    ``db.connection`` and ``db.migrations``.

    v0.4 (issue #148): the helper accepts the caller's handle so the
    writable serve path can re-use the just-opened writable connection
    (DuckDB rejects same-process concurrent opens of one file when
    either side is writable, so a second probe handle would fail).
    """
    from apple_health_mcp.db.migrations import (
        CURRENT_SCHEMA_VERSION,
        _reimport_required_message,
    )
    from apple_health_mcp.exceptions import ConfigError

    # Defer to the tool-level error path when the DB pre-dates the
    # ``imports`` table; a probe-time crash here would hide the
    # better "run import first" guidance the tool layer would give.
    if not _table_exists_in_main_conn(conn, "imports"):
        return
    # ``get_current_version`` cannot be called on a read-only
    # handle because it idempotently CREATE TABLE IF NOT EXISTS
    # the sentinel; that helper is for the writable
    # apply_pending_migrations path. We probe the sentinel
    # ourselves: very-pre-v0.1.4 DBs lack the table and are
    # treated as version 0 (the tool-level error path picks them
    # up); newer DBs read the persisted integer.
    if not _table_exists_in_main_conn(conn, "schema_version"):
        return
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    if current >= CURRENT_SCHEMA_VERSION:
        return
    # v0.3.0 (#124): the DB is behind and the registry cannot bring
    # it forward in place (apply_pending_migrations would raise the
    # exact same ConfigError, but we can't call it on the read-only
    # probe because future migrations would need ALTER). Re-raise
    # the canonical message directly so behaviour stays bit-identical
    # to the writable path.
    raise ConfigError(_reimport_required_message(current, db_path))


# Backwards-compatible alias for the v0.3.x function name. New code
# should call ``_migrate_if_needed_via_separate_probe`` (read-only
# probe path) or ``_migrate_if_needed_on_handle`` (writable-handle
# path) directly so the choice is visible at the call site.
_migrate_if_needed = _migrate_if_needed_via_separate_probe


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
        "MCP server can start. If this path is wrong (typo in --db, mismatched "
        "APPLE_HEALTH_DB / APPLE_HEALTH_DATA_DIR env, MCPB user_config drift, "
        "etc.), the server will keep returning the 'run import first' "
        "guidance until the path matches your real import.",
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
            # Fresh DB (current == 0) so the v0.3.0 (#124) re-import
            # ConfigError guard never fires; ``db_path`` is passed for
            # signature consistency only.
            apply_pending_migrations(bootstrap, db_path=db_path)
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
