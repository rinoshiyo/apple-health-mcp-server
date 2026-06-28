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
    # would always return True and the "fresh-install bootstrap" /
    # "pre-v0.4 legacy-DB probe" branches below would never fire on
    # the writable path.
    file_existed_before_open = resolved.exists()
    if not read_only:
        _ensure_parent_dir(resolved)
    # Bootstrap + legacy probe are the same on both transports. v0.4
    # (issue #148) drove this symmetry: writable serve now opens the
    # same file the read-only path used to, so they share the
    # fresh-install schema bootstrap (otherwise a fresh writable open
    # would land an empty file and the first tool call would crash with
    # "Table imports does not exist") AND the probe-via-fresh-read-only-
    # handle path (otherwise opening a pre-v0.4 DB writable would
    # trigger DuckDB's internal storage-format upgrade -- mutating a
    # file the package is about to refuse -- which is exactly what the
    # v0.3.0 read-only-probe contract was designed to prevent).
    if not file_existed_before_open:
        _materialise_empty_db(resolved)
    else:
        _migrate_if_needed_via_separate_probe(resolved)
    conn = duckdb.connect(str(resolved), read_only=read_only)
    if not read_only:
        conn.execute(f"PRAGMA threads={_DEFAULT_THREADS};")
        # v0.4 (issue #148): preserve_insertion_order=false used to be
        # set inside ``run_import`` for the duration of one CLI-owned
        # connection. With ``run_import(conn=...)`` reusing the server's
        # live handle, setting the PRAGMA inside the importer would
        # leak the override onto every subsequent serve query that
        # passes through the same connection (DuckDB session-scopes
        # PRAGMA). Set it once at writable-open time so the override is
        # connection-stable for the serve's whole lifetime, matching
        # the load-bearing comment the orchestrator carries.
        conn.execute("PRAGMA preserve_insertion_order = false;")
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
    """Log when ``db_path``'s ``schema_version`` trails CURRENT but never raise.

    v0.4.1 (issue #156) behaviour change: the helper used to raise
    :class:`ConfigError` so ``serve`` startup refused a pre-v0.5 DB and
    asked the user to ``rm`` the file + re-run the CLI. That contract
    broke the v0.4 terminal-zero install pitch -- Claude Desktop on
    Windows hides the default DB path inside the MSIX AppContainer
    sandbox, so the user could not even find the file to delete. We
    now keep the connection open at startup and let the read path
    surface ``NEEDS_REIMPORT`` (handled by
    :func:`server.data_state.check_data_state`); the next
    ``import_zip`` call lands in
    :func:`importers.orchestrator.run_import` and triggers
    :func:`db.schema.reset_db_for_fresh_import` before rebuilding the
    canonical schema.

    The probe still exists so a stale DB is *visible* in the logs at
    debug level -- callers grepping for the legacy ConfigError
    fingerprint will find a single DEBUG line pointing at the new
    state-machine recovery path.

    v0.4 (issue #148): the helper accepts the caller's handle so the
    writable serve path can re-use the just-opened writable connection
    (DuckDB rejects same-process concurrent opens of one file when
    either side is writable, so a second probe handle would fail).
    """
    from apple_health_mcp.db.migrations import CURRENT_SCHEMA_VERSION

    # Defer to the tool-level error path when the DB pre-dates the
    # ``imports`` table; the friendly read-path guidance lands on the
    # ``check_data_state`` empty-DB branch, not here.
    if not _table_exists_in_main_conn(conn, "imports"):
        return
    if not _table_exists_in_main_conn(conn, "schema_version"):
        return
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    if 0 < current < CURRENT_SCHEMA_VERSION:
        _logger.debug(
            "DB %s carries schema_version=%d (CURRENT=%d); deferring to "
            "the NEEDS_REIMPORT state-machine envelope and the orchestrator's "
            "fresh-reset path",
            db_path,
            current,
            CURRENT_SCHEMA_VERSION,
        )


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
