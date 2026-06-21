"""Tests for db.migrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db import migrations as migrations_module
from apple_health_mcp.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    apply_pending_migrations,
    get_current_version,
    set_current_version,
)
from apple_health_mcp.exceptions import DatabaseError

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_fresh_database_reports_version_zero() -> None:
    conn = get_in_memory_connection()
    try:
        assert get_current_version(conn) == 0
    finally:
        conn.close()


def test_apply_pending_migrations_stamps_baseline_on_fresh_db() -> None:
    conn = get_in_memory_connection()
    try:
        result = apply_pending_migrations(conn)
        assert result == CURRENT_SCHEMA_VERSION
        assert get_current_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_pending_migrations_is_idempotent_when_already_current() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, CURRENT_SCHEMA_VERSION)
        result = apply_pending_migrations(conn)
        assert result == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_pending_migrations_runs_registered_steps(monkeypatch: MonkeyPatch) -> None:
    calls: list[int] = []

    def _step_two(conn: object) -> None:  # type: ignore[no-untyped-def]
        calls.append(2)

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((2, _step_two),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 1)
        result = apply_pending_migrations(conn)
        assert result == 2
        assert calls == [2]
        assert get_current_version(conn) == 2
    finally:
        conn.close()


def test_database_newer_than_supported_raises() -> None:
    conn = get_in_memory_connection()
    try:
        set_current_version(conn, CURRENT_SCHEMA_VERSION + 1)
        with pytest.raises(DatabaseError, match="newer than"):
            apply_pending_migrations(conn)
    finally:
        conn.close()


def test_migration_target_not_greater_than_applied_raises(monkeypatch: MonkeyPatch) -> None:
    def _bogus(conn: object) -> None:  # type: ignore[no-untyped-def]
        pass  # pragma: no cover - never invoked

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((1, _bogus),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 2)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 1)
        with pytest.raises(DatabaseError, match="not greater"):
            apply_pending_migrations(conn)
    finally:
        conn.close()


def test_migration_target_exceeds_current_supported_raises(monkeypatch: MonkeyPatch) -> None:
    def _bogus(conn: object) -> None:  # type: ignore[no-untyped-def]
        pass  # pragma: no cover - never invoked

    monkeypatch.setattr(migrations_module, "MIGRATIONS", ((5, _bogus),))
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 1)

    conn = get_in_memory_connection()
    try:
        set_current_version(conn, 0)
        with pytest.raises(DatabaseError, match="exceeds CURRENT_SCHEMA_VERSION"):
            apply_pending_migrations(conn)
    finally:
        conn.close()
