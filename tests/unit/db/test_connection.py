"""Tests for db.connection."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest

from apple_health_mcp.db import connection as connection_module
from apple_health_mcp.db.connection import (
    default_db_path,
    get_connection,
    get_in_memory_connection,
    resolve_db_path,
)
from apple_health_mcp.exceptions import ConfigError
from tests._helpers import seed_one_import

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


def _clear_db_env(monkeypatch: MonkeyPatch) -> None:
    """Strip the two env-override knobs that ``resolve_db_path`` consults.

    Without this guard a developer (or CI shell) with ``APPLE_HEALTH_DB``
    or ``APPLE_HEALTH_DATA_DIR`` exported in the ambient environment
    would silently flip every platform-default test into the override
    branch. Explicit ``delenv`` calls keep the platform-default tests
    deterministic regardless of the runner's env.
    """
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)


def test_default_db_path_posix_with_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    result = default_db_path()
    assert result == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_posix_without_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / ".local" / "share" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_with_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_without_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_resolve_db_path_uses_apple_health_db_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """APPLE_HEALTH_DB pins the resolver to that exact file path."""
    target = tmp_path / "custom" / "h.duckdb"
    monkeypatch.setenv("APPLE_HEALTH_DB", str(target))
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    assert resolve_db_path() == target


def test_resolve_db_path_expands_tilde_in_apple_health_db(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``~`` in APPLE_HEALTH_DB expands to the resolved HOME.

    Users who hand-edit the bundle env or set the variable in a shell
    rc typically write ``~/.local/share/...`` rather than the absolute
    expansion; the resolver must honour that the same way every other
    XDG-respecting tool does.
    """
    # ``Path.expanduser`` resolves ``~`` via the HOME env on POSIX and
    # USERPROFILE (then HOMEDRIVE+HOMEPATH) on Windows. Patching only
    # HOME left the Windows runners reading their real USERPROFILE
    # (``C:/Users/runneradmin``) and broke the assertion — patch both
    # so the tilde target lands inside ``tmp_path`` on either OS.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPLE_HEALTH_DB", "~/custom/h.duckdb")
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    assert resolve_db_path() == tmp_path / "custom" / "h.duckdb"


def test_resolve_db_path_uses_apple_health_data_dir_when_db_unset(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """APPLE_HEALTH_DATA_DIR appends the default file name directly under it.

    The env var is treated as the FINAL parent (no ``apple-health-mcp/``
    subdir) — that's the documented contract; if the test ever flips to
    requiring the package subdir, both the docstring and the README
    need to move first.
    """
    target_dir = tmp_path / "data-root"
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", str(target_dir))
    assert resolve_db_path() == target_dir / "health.duckdb"


def test_resolve_db_path_expands_tilde_in_apple_health_data_dir(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``~`` in APPLE_HEALTH_DATA_DIR expands to the resolved HOME on either OS."""
    # See companion test above for the HOME / USERPROFILE rationale —
    # Windows runners read USERPROFILE first.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", "~/data-root")
    assert resolve_db_path() == tmp_path / "data-root" / "health.duckdb"


def test_resolve_db_path_prefers_apple_health_db_over_data_dir(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """APPLE_HEALTH_DB wins when both env vars are set simultaneously."""
    file_target = tmp_path / "explicit" / "h.duckdb"
    monkeypatch.setenv("APPLE_HEALTH_DB", str(file_target))
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", str(tmp_path / "should-be-ignored"))
    assert resolve_db_path() == file_target


def test_resolve_db_path_falls_back_to_platform_default(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Both env vars unset → POSIX XDG default path under ``apple-health-mcp/``."""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_db_path() == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_is_alias_of_resolve_db_path(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``default_db_path`` keeps working as a backward-compatible alias.

    The env precedence must flow through the alias as well — otherwise
    callers that still reference ``default_db_path`` would silently
    bypass the override. The assertion compares both functions on the
    same monkeypatched env to lock that in.
    """
    target = tmp_path / "alias" / "h.duckdb"
    monkeypatch.setenv("APPLE_HEALTH_DB", str(target))
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    assert default_db_path() == resolve_db_path() == target


def test_get_connection_uses_resolve_db_path_with_apple_health_db_env(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``get_connection(db_path=None)`` honours APPLE_HEALTH_DB end-to-end.

    Locks in the server / CLI one-path-resolver contract: any caller
    that omits ``db_path`` (server boot, CLI without ``--db``) must
    pick up the env override, otherwise a future regression that
    inlined the platform default in ``get_connection`` would silently
    bypass the MCPB-injected ``user_config.db_path`` and re-open the
    sandbox-redirected file (issue #128).
    """
    target = tmp_path / "via-env" / "h.duckdb"
    monkeypatch.setenv("APPLE_HEALTH_DB", str(target))
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    conn = get_connection()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    assert target.exists()


def test_get_connection_explicit_db_path_overrides_env(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``db_path=`` argument wins over APPLE_HEALTH_DB.

    Locks in the resolver chain precedence: explicit caller arg > env
    > platform default. A future regression that flipped
    ``get_connection`` to ALWAYS consult the env (instead of the
    explicit arg) would silently override callers that already know
    where their DB lives — e.g. a future migration helper that wants
    to operate on a specific file regardless of the user's env.
    """
    explicit = tmp_path / "explicit" / "h.duckdb"
    decoy = tmp_path / "decoy" / "h.duckdb"
    monkeypatch.setenv("APPLE_HEALTH_DB", str(decoy))
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    conn = get_connection(explicit)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    assert explicit.exists()
    assert not decoy.exists()


def test_resolve_db_path_treats_blank_env_db_as_unset(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``APPLE_HEALTH_DB=""`` falls through to the next tier silently.

    A shell rc that does ``export APPLE_HEALTH_DB=`` (set-then-clear
    in a single line) leaves the var present-but-empty in os.environ.
    Treating that as "the user wants the empty-string path" would
    produce nonsense; treating it as "unset" matches every other
    XDG-respecting tool's convention.
    """
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPLE_HEALTH_DB", "")
    assert resolve_db_path() == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_resolve_db_path_treats_whitespace_only_env_db_as_unset(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``APPLE_HEALTH_DB="   "`` strips to empty and falls through.

    Without ``.strip()``, ``Path(" ").expanduser()`` would yield a
    relative path with a leading space — a silent foot-gun. The
    resolver instead treats whitespace-only values the same as the
    bare-empty case.
    """
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPLE_HEALTH_DB", "   ")
    assert resolve_db_path() == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_resolve_db_path_treats_blank_env_data_dir_as_unset(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``APPLE_HEALTH_DATA_DIR=""`` falls through to the platform default."""
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", "")
    assert resolve_db_path() == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_resolve_db_path_rejects_relative_apple_health_db(
    monkeypatch: MonkeyPatch,
) -> None:
    """Relative APPLE_HEALTH_DB raises ConfigError instead of CWD-dependent open.

    The historic ``default_db_path`` always returned an absolute path;
    losing that invariant silently when the env var is relative would
    re-open DIFFERENT files between CLI and server invocations (the
    MCPB launcher CWD differs from the user's terminal CWD).
    """
    monkeypatch.setenv("APPLE_HEALTH_DB", "health.duckdb")
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    with pytest.raises(ConfigError, match=r"APPLE_HEALTH_DB.*absolute"):
        resolve_db_path()


def test_resolve_db_path_rejects_apple_health_db_pointing_at_directory(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``APPLE_HEALTH_DB=/some/existing/dir`` is a misuse and must error early.

    Without the check, ``get_connection`` would proceed to
    ``duckdb.connect('/some/existing/dir')`` and surface an opaque
    ``IO Error`` from DuckDB; the ConfigError instead names the env
    var and hints at the file-suffix the user almost certainly meant.
    """
    a_dir = tmp_path / "i_am_a_directory"
    a_dir.mkdir()
    monkeypatch.setenv("APPLE_HEALTH_DB", str(a_dir))
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    with pytest.raises(ConfigError, match=r"APPLE_HEALTH_DB.*directory"):
        resolve_db_path()


def test_resolve_db_path_rejects_relative_apple_health_data_dir(
    monkeypatch: MonkeyPatch,
) -> None:
    """Relative APPLE_HEALTH_DATA_DIR raises ConfigError, same reason as DB."""
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", "data-root")
    with pytest.raises(ConfigError, match=r"APPLE_HEALTH_DATA_DIR.*absolute"):
        resolve_db_path()


def test_resolve_db_path_rejects_data_dir_pointing_at_duckdb_file(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """``APPLE_HEALTH_DATA_DIR`` ending in ``.duckdb`` is the var-swap typo.

    Users who set ``APPLE_HEALTH_DATA_DIR=~/health/db.duckdb`` likely
    meant ``APPLE_HEALTH_DB``; without this guard the resolver would
    return ``~/health/db.duckdb/health.duckdb`` and DuckDB would error
    with a cryptic ``Cannot open file`` because the parent does not
    exist as a directory.
    """
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", str(tmp_path / "db.duckdb"))
    with pytest.raises(ConfigError, match=r"APPLE_HEALTH_DATA_DIR.*duckdb"):
        resolve_db_path()


def test_get_connection_uses_default_when_not_provided(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _clear_db_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()
    db_path = tmp_path / "data" / "apple-health-mcp" / "health.duckdb"
    assert db_path.exists()
    # We auto-create the default path's app subdir at 0700 because we own it;
    # a more permissive mode means the chmod tightening regressed. Skip the
    # POSIX-mode check on real Windows (Path.chmod is ACL-only there and the
    # mode bits do not reflect what we asked for).
    if os.name == "posix":
        assert (db_path.parent.stat().st_mode & 0o777) == 0o700


def test_get_connection_creates_parent_dir_without_chmod_on_user_path(tmp_path: Path) -> None:
    """User-supplied paths must NOT have their parent dir chmod-ed.

    Locking down ``$HOME`` / ``/tmp`` / a project dir to 0700 would silently
    break sshd StrictModes and any tool that expects 0755 home permissions.
    """
    db_path = tmp_path / "nested" / "dirs" / "h.duckdb"
    pre_existing_mode = (tmp_path / "nested").exists() or db_path.parent.exists()
    assert not pre_existing_mode
    conn = get_connection(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.exists()
        # Parent dir basename is "dirs", not "apple-health-mcp", so chmod must
        # not have fired. mkdir's default umask gives 0755 (or whatever the
        # ambient umask permits) — assert the chmod did NOT lock it down.
        assert (db_path.parent.stat().st_mode & 0o777) != 0o700
    finally:
        conn.close()


def test_get_connection_skips_chmod_on_windows(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    db_path = tmp_path / "win" / "h.duckdb"
    conn = get_connection(db_path)
    try:
        assert db_path.parent.is_dir()
    finally:
        conn.close()


def test_get_connection_read_only_opens_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ro.duckdb"
    seeder = get_connection(db_path)
    seeder.execute("CREATE TABLE t(x INTEGER);")
    seeder.execute("INSERT INTO t VALUES (1);")
    seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute("SELECT x FROM t").fetchone()
        assert row is not None
        assert row[0] == 1
        with pytest.raises(duckdb.Error):
            conn.execute("INSERT INTO t VALUES (2);")
    finally:
        conn.close()


def test_get_connection_read_only_materialises_empty_db_when_missing(
    tmp_path: Path,
) -> None:
    """Read-only open against a missing path bootstraps a schema-only DB.

    Before issue #38 this raised ``DatabaseError`` and ``serve`` exited, so
    the MCP client saw no tools at all and could not even surface the
    "run import first" guidance. Now we materialise an empty schema, open
    read-only against it, and let each tool return ``IMPORT_REQUIRED_MESSAGE``
    from a live MCP session.
    """
    db_path = tmp_path / "missing" / "ro.duckdb"
    conn = get_connection(db_path, read_only=True)
    try:
        # Parent dir auto-created during the bootstrap.
        assert db_path.parent.is_dir()
        assert db_path.exists()
        # ``imports`` table exists (schema was applied) but is empty.
        row = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
        assert row is not None
        assert row[0] == 0
        # The handle is genuinely read-only — writes still fail. The INSERT
        # supplies a fully-valid row (matching the imports schema) so the
        # only possible cause of duckdb.Error is the read-only refusal: if
        # we passed NULL for the NOT NULL imported_at column, a constraint
        # failure could be mistaken for read-only enforcement and the test
        # would keep passing if RO was silently regressed.
        with pytest.raises(duckdb.Error):
            # Named-column form is churn-resistant against future ADD COLUMN
            # bumps; positional form had to be churned in PRs #62, #129, #148, #163.
            conn.execute(
                "INSERT INTO imports ("
                "  import_id, export_dir, imported_at, "
                "  record_count, workout_count, duration_secs, "
                "  records_after_dedup, dedup_skipped"
                ") VALUES ("
                "  'x', '/tmp/x', TIMESTAMPTZ '2024-01-01 00:00:00+00', "
                "  0, 0, 0, "
                "  0, FALSE"
                ")"
            )
    finally:
        conn.close()


def test_materialise_empty_db_cleans_up_stale_bootstrap_tempfile(
    tmp_path: Path,
) -> None:
    """A leftover .bootstrap.<pid> from a previous crash is removed before retry.

    Without this guard, two successive bootstrap attempts from the same
    process (CLI invoked twice, second time after a crash mid-DDL on the
    first) would hit ``duckdb.Error`` opening the tmp path that already
    exists with a half-written DuckDB header.
    """
    db_path = tmp_path / "fresh" / "h.duckdb"
    tmp_marker = db_path.with_name(f"{db_path.name}.bootstrap.{os.getpid()}")
    tmp_marker.parent.mkdir(parents=True, exist_ok=True)
    tmp_marker.write_bytes(b"stale leftover from a previous crash")
    assert tmp_marker.exists()

    conn = get_connection(db_path, read_only=True)
    try:
        assert db_path.exists()
        # Stale tmp marker was removed before the bootstrap re-used the slot,
        # and the final atomic-rename consumed the new temp file too.
        assert not tmp_marker.exists()
    finally:
        conn.close()


def test_materialise_empty_db_removes_tempfile_when_bootstrap_raises(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A crash mid-DDL leaves no half-initialised file at the final path.

    Without the atomic-rename strategy, an aborted ensure_schema would
    leave a real DuckDB file at ``db_path`` that the next ``serve`` run
    would mistake for a complete DB and skip the bootstrap; every tool
    would then error with ``Error: Table imports does not exist``
    instead of returning ``IMPORT_REQUIRED_MESSAGE``.
    """
    from apple_health_mcp.db import schema as schema_mod

    boom = RuntimeError("simulated DDL crash")

    def _explode(_conn: object) -> None:
        raise boom

    monkeypatch.setattr(schema_mod, "ensure_schema", _explode)

    db_path = tmp_path / "fresh" / "h.duckdb"
    tmp_marker = db_path.with_name(f"{db_path.name}.bootstrap.{os.getpid()}")

    with pytest.raises(RuntimeError, match="simulated DDL crash"):
        get_connection(db_path, read_only=True)
    # Neither the final path nor the per-pid temp file remain on disk.
    assert not db_path.exists()
    assert not tmp_marker.exists()


def test_get_connection_read_only_preserves_existing_data_after_bootstrap(
    tmp_path: Path,
) -> None:
    """Bootstrap fires only when the file is missing — pre-existing rows survive."""
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "ro.duckdb"
    seeder = get_connection(db_path)
    ensure_schema(seeder)
    seed_one_import(seeder)
    seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute("SELECT import_id FROM imports").fetchone()
        assert row is not None
        assert row[0] == "imp1"
    finally:
        conn.close()


def _seed_legacy_v2_db(db_path: Path) -> None:
    """Build a v0.2.x-shaped DB file at ``db_path`` for the issue #124 tests.

    Builds the canonical schema, then drops ``heart_rate_samples`` and
    recreates it with the legacy VARCHAR ``sample_time`` column so the
    file plausibly represents what a user who imported under v0.2.x and
    then upgraded the package to v0.3.0 would have on disk. Stamps
    ``schema_version=2`` so :func:`apply_pending_migrations` -- and
    therefore :func:`_migrate_if_needed` -- raises the canonical
    re-import :class:`ConfigError` instead of silently bumping the
    sentinel.

    Each phase runs on its own DuckDB connection + CHECKPOINT so the
    next read-only open does not inherit a stale catalog snapshot from
    the v=3 ensure_schema -> v=2 downgrade rewrite (DuckDB's MVCC
    otherwise treats the legacy DROP as a conflict against the
    seeder's ensure_schema on the same logical table).
    """
    from apple_health_mcp.db.migrations import (
        set_current_version,
    )
    from apple_health_mcp.db.schema import ensure_schema

    # Phase 1: build the canonical schema (v=3 shape, including
    # imports.export_xml_sha256 and a DOUBLE sample_time) without
    # applying migrations -- the v=3 column is already DOUBLE here so
    # the migration registry would be a no-op anyway, but skipping the
    # stamp keeps schema_version at 0 ready for the downgrade.
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()

    # Phase 2: tear ``heart_rate_samples`` back down to the v0.2.x
    # shape on a fresh connection so the next probe's MVCC view sees a
    # clean post-CHECKPOINT baseline. Doing the DROP/CREATE on the
    # same connection as the migration probe is what triggered the
    # "another transaction has altered this table" failure.
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        seeder.execute("DROP TABLE heart_rate_samples;")
        seeder.execute(
            """
            CREATE TABLE heart_rate_samples (
                parent_record_hash  VARCHAR NOT NULL,
                sample_idx          INTEGER NOT NULL,
                bpm                 DOUBLE,
                sample_time         VARCHAR,
                import_id           VARCHAR NOT NULL
            );
            """
        )
        seeder.execute(
            "INSERT INTO heart_rate_samples VALUES ('rh', 0, 70.0, '08:00:00.000', 'imp')"
        )
        set_current_version(seeder, 2)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()


def test_get_connection_read_only_opens_legacy_v2_db_without_raising(
    tmp_path: Path,
) -> None:
    """v0.4.1 (issue #156): a v0.2.x DB opened by ``serve`` is allowed through.

    The pre-v0.4.1 contract raised :class:`ConfigError` so the server
    refused to boot against a stale DB, but that broke the v0.4
    terminal-zero install pitch on Claude Desktop (Windows) where the
    canonical DB path lives behind the MSIX AppContainer sandbox
    redirect and is invisible to Explorer / PowerShell. The new
    contract: open the DB anyway and rely on
    :func:`server.data_state.check_data_state` to surface
    ``NEEDS_REIMPORT`` so the agent triggers the
    ``list_zips`` + ``import_zip`` recovery path.
    """
    db_path = tmp_path / "legacy_v2.duckdb"
    _seed_legacy_v2_db(db_path)

    conn = get_connection(db_path, read_only=True)
    try:
        # The legacy DB still opens cleanly; the read path now relies
        # on ``check_data_state`` to surface NEEDS_REIMPORT.
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == 2
    finally:
        conn.close()


def test_get_connection_read_only_opens_legacy_v4_db_without_raising(
    tmp_path: Path,
) -> None:
    """v0.4.1 (issue #156): a v=4 DB no longer makes ``serve`` startup raise.

    Mirrors the v0.4 test that asserted the legacy ConfigError path; the
    refusal is gone so the data-state machine can surface
    ``NEEDS_REIMPORT`` on the next tool call. ``import_zip`` then drives
    the orchestrator's fresh-reset path.
    """
    from apple_health_mcp.db.migrations import (
        set_current_version,
    )
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "legacy_v4.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        set_current_version(seeder, 4)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == 4
    finally:
        conn.close()


def test_get_connection_writable_opens_legacy_v4_db_without_raising(
    tmp_path: Path,
) -> None:
    """v0.4.1 (issue #156): the writable serve path also accepts stale DBs.

    The v0.4 server opens the connection ``read_only=False`` so the new
    ``import_zip`` MCP tool can drive the importer inline. The new
    contract: a stale DB opens cleanly; the next ``import_zip`` call
    lands in the orchestrator's fresh-reset path and rebuilds the
    schema before re-ingesting.
    """
    from apple_health_mcp.db.migrations import (
        set_current_version,
    )
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "legacy_v4_writable.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        set_current_version(seeder, 4)
        seeder.execute("CHECKPOINT;")
    finally:
        seeder.close()

    conn = get_connection(db_path, read_only=False)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row is not None and row[0] == 4
    finally:
        conn.close()


def test_get_connection_writable_skips_probe_for_missing_file(
    tmp_path: Path,
) -> None:
    """A missing-file path does NOT run the legacy-DB probe.

    Mirrors the read-only path's empty-file bootstrap: the writable
    path creates the file via DuckDB's normal first-open behaviour
    without trying to read a non-existent ``schema_version`` table.
    """
    db_path = tmp_path / "writable_fresh.duckdb"
    assert not db_path.exists()
    conn = get_connection(db_path, read_only=False)
    try:
        assert db_path.exists()
    finally:
        conn.close()


def test_get_connection_read_only_returns_quietly_on_current_db(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    """Already-current DBs reach the read-only handle without log noise.

    Pre-/code-review this asserted absence of a literal "migrating
    existing DB" log line that the v0.3.0 cleanup deleted; the
    assertion became a tautology that always passed. The post-fix
    invariant is positive: ``_migrate_if_needed`` returns silently
    on a current DB, so the connection module's logger emits NO
    INFO/WARNING records at all when ``read_only=True`` is opened
    against a current DB. A future contributor that adds a noisy
    startup log will fail this test.
    """
    from apple_health_mcp.db.migrations import apply_pending_migrations
    from apple_health_mcp.db.schema import ensure_schema

    db_path = tmp_path / "current.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(seeder)
        apply_pending_migrations(seeder)
    finally:
        seeder.close()

    with caplog.at_level(logging.INFO, logger=connection_module.__name__):
        conn = get_connection(db_path, read_only=True)
    try:
        # Sanity check: handle is usable and on the new schema.
        type_row = conn.execute(
            "SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'"
        ).fetchone()
        assert type_row is not None
        assert str(type_row[0]).upper() == "DOUBLE"
    finally:
        conn.close()

    # Positive invariant: NO INFO/WARNING from the connection module
    # on the current-DB happy path. A future contributor that adds a
    # noisy probe log (the kind that crept in pre-/code-review and
    # was deleted) will trip this assertion.
    noise = [
        r
        for r in caplog.records
        if r.name == connection_module.__name__ and r.levelno >= logging.INFO
    ]
    assert noise == [], (
        "current-DB read-only open should be silent; "
        f"got {[(r.levelname, r.getMessage()) for r in noise]}"
    )


def test_get_connection_read_only_skips_migration_when_imports_table_missing(
    tmp_path: Path,
) -> None:
    """Very-pre-v0.1.4 DBs lack the ``imports`` table; the probe must
    defer to tool-level error handling instead of crashing.

    The bare file is still openable read-only and the migration probe
    simply returns without trying to stamp a version.
    """
    db_path = tmp_path / "pre_imports.duckdb"
    seeder = duckdb.connect(str(db_path), read_only=False)
    try:
        seeder.execute("CREATE TABLE _placeholder (x INTEGER);")
    finally:
        seeder.close()

    conn = get_connection(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = 'imports' LIMIT 1"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_get_in_memory_connection() -> None:
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()


def test_get_in_memory_connection_applies_session_tz_from_env(
    monkeypatch: MonkeyPatch,
) -> None:
    """APPLE_HEALTH_TZ flows through to ``SET TimeZone`` on the new connection."""
    monkeypatch.setenv("APPLE_HEALTH_TZ", "Asia/Tokyo")
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT current_setting('TimeZone')").fetchone()
        assert row is not None
        assert row[0] == "Asia/Tokyo"
    finally:
        conn.close()


def test_get_in_memory_connection_rejects_invalid_session_tz(
    monkeypatch: MonkeyPatch,
) -> None:
    """Garbage in the env var is rejected before the SET TimeZone interpolation."""
    # A semicolon would be a SQL-injection vector if the connection layer
    # interpolated the env value directly; the validation regex rejects it.
    monkeypatch.setenv("APPLE_HEALTH_TZ", "Asia/Tokyo'; DROP TABLE x;--")
    with pytest.raises(ConfigError, match="invalid APPLE_HEALTH_TZ"):
        get_in_memory_connection()
