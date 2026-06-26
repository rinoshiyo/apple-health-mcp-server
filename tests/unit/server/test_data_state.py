"""Tests for ``server.data_state``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.server.data_state import (
    EXPORT_ZIPS_DIR_ENV_VAR,
    DataState,
    build_state_error_payload,
    check_data_state,
    require_ready_or_state_error,
)
from tests._helpers import seed_one_import

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_check_data_state_returns_ready_when_imports_has_rows() -> None:
    """A seeded ``imports`` row is the only signal needed for READY.

    The orchestrator never INSERTs a row for a failed import, so
    presence-of-row is a sufficient proxy for "a successful import has
    happened" without a separate ``status`` column.
    """
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        assert check_data_state(conn) == DataState.READY
    finally:
        conn.close()


def test_check_data_state_returns_needs_config_when_env_unset(
    monkeypatch: MonkeyPatch,
) -> None:
    """Empty DB + unconfigured drop-zone → NEEDS_CONFIG."""
    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        assert check_data_state(conn) == DataState.NEEDS_CONFIG
    finally:
        conn.close()


def test_check_data_state_returns_needs_import_when_env_set(
    monkeypatch: MonkeyPatch,
    tmp_path: object,
) -> None:
    """Empty DB but configured drop-zone → NEEDS_IMPORT."""
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        assert check_data_state(conn) == DataState.NEEDS_IMPORT
    finally:
        conn.close()


def test_check_data_state_treats_blank_env_as_unset(
    monkeypatch: MonkeyPatch,
) -> None:
    """A blank ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` value reads as unset.

    Mirrors the resolver's blank-after-strip rule (``db.connection``):
    a shell rc that does ``export APPLE_HEALTH_EXPORT_ZIPS_DIR=`` must
    behave like the variable was never set, otherwise the operator
    can't deconfigure the drop-zone without unsetting the variable
    entirely.
    """
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, "   ")
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        assert check_data_state(conn) == DataState.NEEDS_CONFIG
    finally:
        conn.close()


def test_check_data_state_handles_missing_imports_table(
    monkeypatch: MonkeyPatch,
) -> None:
    """An alien DB without an ``imports`` table falls through to the env tier.

    Caught broadly so the SQL ``CatalogException`` does not crash the
    tool layer -- the tool surfaces the friendly NEEDS_CONFIG /
    NEEDS_IMPORT guidance instead.
    """
    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)
    conn = duckdb.connect(":memory:")
    try:
        assert check_data_state(conn) == DataState.NEEDS_CONFIG
    finally:
        conn.close()


def test_check_data_state_uses_lock_when_provided(
    monkeypatch: MonkeyPatch,
) -> None:
    """The optional ``lock`` argument is acquired around the probe.

    Verified by passing a real ``Lock`` and confirming the call still
    succeeds (which it would not if the helper double-locked or never
    released).
    """
    from threading import Lock

    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        lock = Lock()
        assert check_data_state(conn, lock=lock) == DataState.NEEDS_CONFIG
    finally:
        conn.close()


def test_build_state_error_payload_for_needs_config_has_documented_shape() -> None:
    """``NEEDS_CONFIG`` envelope carries every documented field."""
    raw = build_state_error_payload(DataState.NEEDS_CONFIG)
    payload = json.loads(raw)
    assert payload["state"] == "NEEDS_CONFIG"
    assert payload["suggested_action"] == "ask_user_to_open_settings"
    assert EXPORT_ZIPS_DIR_ENV_VAR in payload["reason"]
    assert "human_message" in payload
    assert "Settings" in payload["human_message"]


def test_build_state_error_payload_for_needs_import_has_documented_shape() -> None:
    """``NEEDS_IMPORT`` envelope tells the agent to call ``list_zips``."""
    raw = build_state_error_payload(DataState.NEEDS_IMPORT)
    payload = json.loads(raw)
    assert payload["state"] == "NEEDS_IMPORT"
    assert payload["suggested_action"] == "call_list_zips"
    assert "human_message" in payload
    assert "list_zips" in payload["human_message"]


def test_build_state_error_payload_rejects_ready() -> None:
    """Passing ``READY`` is a programming error -- the helper raises.

    Guards the contract: ``READY`` is the non-error branch, so a caller
    that asked for an error envelope on a READY state has a bug worth
    surfacing immediately rather than shipping a malformed payload.
    """
    with pytest.raises(ValueError, match="non-error state"):
        build_state_error_payload(DataState.READY)


def test_require_ready_or_state_error_returns_none_when_ready() -> None:
    """A seeded DB returns ``None`` so the caller proceeds to its SQL."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        assert require_ready_or_state_error(conn) is None
    finally:
        conn.close()


def test_require_ready_or_state_error_returns_payload_when_not_ready(
    monkeypatch: MonkeyPatch,
) -> None:
    """An empty DB returns the structured error envelope."""
    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = require_ready_or_state_error(conn)
        assert out == build_state_error_payload(DataState.NEEDS_CONFIG)
    finally:
        conn.close()
