"""Tests for db.connection."""

from __future__ import annotations

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
    assert (tmp_path / "data" / "apple-health-mcp" / "health.duckdb").exists()


def test_get_connection_creates_parent_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dirs" / "h.duckdb"
    conn = get_connection(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.exists()
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


def test_get_connection_read_only_requires_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ro.duckdb"
    # Seed the file with a writable connection first; DuckDB cannot open
    # read-only when the file does not yet exist.
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


def test_get_in_memory_connection() -> None:
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()
