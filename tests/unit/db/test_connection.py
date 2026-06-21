"""Tests for db.connection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest

from apple_health_mcp.db.connection import (
    default_db_path,
    get_connection,
    get_in_memory_connection,
)
from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_default_db_path_posix_with_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    result = default_db_path()
    assert result == tmp_path / "xdg" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_posix_without_xdg(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / ".local" / "share" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_with_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_default_db_path_windows_without_localappdata(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    result = default_db_path()
    assert result == tmp_path / "AppData" / "Local" / "apple-health-mcp" / "health.duckdb"


def test_get_connection_uses_default_when_not_provided(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
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


def test_get_connection_read_only_raises_when_missing(tmp_path: Path) -> None:
    """Read-only open against a missing path must raise without side effects.

    Without this guard, ``serve`` invoked before ``import`` would silently
    create the parent directory and then surface an opaque DuckDB error.
    """
    db_path = tmp_path / "missing" / "ro.duckdb"
    with pytest.raises(DatabaseError, match="cannot open read-only"):
        get_connection(db_path, read_only=True)
    # Parent dir must NOT have been created as a side effect.
    assert not db_path.parent.exists()


def test_get_in_memory_connection() -> None:
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()
