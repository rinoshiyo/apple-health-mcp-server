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
    block_if_schema_outdated,
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
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = require_ready_or_state_error(conn)
        assert out == build_state_error_payload(DataState.NEEDS_CONFIG)
    finally:
        conn.close()


# --- v0.4.1 (issue #156): NEEDS_REIMPORT --------------------------


def test_check_data_state_returns_needs_reimport_when_schema_stale() -> None:
    """v0.4.1 (issue #156): a DB whose schema_version trails CURRENT
    surfaces ``NEEDS_REIMPORT`` so the agent triggers the re-import
    recovery flow.

    Takes precedence over the READY tier: an existing ``imports`` row
    is meaningless when the schema is stale because the row's column
    set may not match the package's current expectations.
    """
    from apple_health_mcp.db.migrations import CURRENT_SCHEMA_VERSION, set_current_version

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        # Seed an imports row to confirm the schema-staleness check
        # short-circuits BEFORE the READY tier.
        seed_one_import(conn)
        set_current_version(conn, CURRENT_SCHEMA_VERSION - 1)
        assert check_data_state(conn) == DataState.NEEDS_REIMPORT
    finally:
        conn.close()


def test_build_state_error_payload_for_needs_reimport_has_documented_shape() -> None:
    """``NEEDS_REIMPORT`` envelope steers the agent at ``list_zips``."""
    raw = build_state_error_payload(DataState.NEEDS_REIMPORT)
    payload = json.loads(raw)
    assert payload["state"] == "NEEDS_REIMPORT"
    assert payload["suggested_action"] == "call_list_zips"
    assert "human_message" in payload
    assert "list_zips" in payload["human_message"]
    # v0.5.1 #188: reason was tightened from a free-form prose sentence
    # to a stable enum-style identifier so MCP agents can branch on
    # ``payload["reason"] == "schema_outdated"`` without a fragile
    # substring match. The descriptive sentence moved to human_message.
    assert payload["reason"] == "schema_outdated"
    # The descriptive wording (schema_version trails / import_jobs
    # missing) now lives on the human_message so the user-facing prose
    # still names the failure mode -- the next assertion pins that.
    assert "schema_version" in payload["human_message"]


def test_check_data_state_falls_through_when_stale_probe_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """A surprise from the stale-schema probe is logged + treated as fresh.

    Defensive mirror of the ``_imports_table_has_rows`` catch-all: a
    SQL exception from the probe must not crash the tool layer; it
    falls through to the friendlier NEEDS_CONFIG / NEEDS_IMPORT
    tiers so the user still gets actionable guidance.
    """
    from apple_health_mcp.db import migrations as migrations_module

    def _boom(_conn: object) -> bool:
        raise RuntimeError("probe boom")

    # ``_safe_schema_stale_probe`` re-reads
    # ``migrations_module.schema_version_is_stale`` per call (lazy
    # import), so patching the source name there is enough to make
    # the helper observe the failure.
    monkeypatch.setattr(migrations_module, "schema_version_is_stale", _boom)

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        # The probe raises → fall-through → NEEDS_CONFIG (env unset).
        assert check_data_state(conn) == DataState.NEEDS_CONFIG
    finally:
        conn.close()


# --- v0.5.1 (issue #188): schema_outdated -------------------------------


def test_check_data_state_flags_populated_db_with_missing_import_jobs() -> None:
    """A populated v=5-or-earlier-shaped DB → NEEDS_REIMPORT.

    A pre-v0.5 DB carries ``imports`` rows but no ``import_jobs`` table.
    Without the v0.5.1 #188 detection, ``check_data_state`` would
    happily report READY and the next ``import_zip`` call would surface
    a raw ``Catalog Error: Table import_jobs does not exist``.
    """
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        conn.execute("DROP TABLE import_jobs;")
        assert check_data_state(conn) == DataState.NEEDS_REIMPORT
    finally:
        conn.close()


def test_check_data_state_import_jobs_probe_handles_alien_db(
    monkeypatch: MonkeyPatch,
) -> None:
    """A probe raising on the ``import_jobs`` presence check reads as missing.

    Defensive mirror of the other catch-alls in this module: an alien
    DB (no ``duckdb_tables()`` metadata, an extreme catalog corruption)
    must not crash the tool layer. The helper treats the failure as
    "missing" so the downstream NEEDS_REIMPORT branch fires whenever
    we cannot prove the table is present.
    """
    from apple_health_mcp.server import data_state as ds

    real_execute = duckdb.DuckDBPyConnection.execute

    def _boom(self: duckdb.DuckDBPyConnection, sql: str, *args: object, **kw: object):  # type: ignore[no-untyped-def]
        if "import_jobs" in sql and "duckdb_tables" in sql:
            raise RuntimeError("alien-db boom")
        return real_execute(self, sql, *args, **kw)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", _boom)
    conn = get_in_memory_connection()
    try:
        # _import_jobs_table_missing returns True on probe failure.
        assert ds._import_jobs_table_missing(conn) is True
    finally:
        conn.close()


def test_block_if_schema_outdated_returns_envelope_on_stale_db() -> None:
    """Write-side helper surfaces the schema_outdated envelope on stale DBs."""
    from apple_health_mcp.db.migrations import set_current_version

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        # Force the persisted version to a v=5 baseline so
        # schema_version_is_stale fires.
        set_current_version(conn, 5)
        envelope = block_if_schema_outdated(conn)
        assert envelope is not None
        payload = json.loads(envelope)
        assert payload["state"] == "NEEDS_REIMPORT"
        assert payload["reason"] == "schema_outdated"
    finally:
        conn.close()


def test_block_if_schema_outdated_returns_none_on_healthy_db() -> None:
    """A READY / NEEDS_CONFIG / NEEDS_IMPORT DB → no envelope, caller proceeds."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        # NEEDS_CONFIG (env unset, no imports) is not a schema problem,
        # so the helper returns None and the tool runs its normal flow.
        assert block_if_schema_outdated(conn) is None
        seed_one_import(conn)
        # READY (imports populated, schema current) is also None.
        assert block_if_schema_outdated(conn) is None
    finally:
        conn.close()
