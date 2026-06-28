"""Tests for ``server.query``."""

from __future__ import annotations

import datetime as _dt
import json
import math
from decimal import Decimal
from threading import Lock
from typing import Any

import duckdb
import pytest

from apple_health_mcp.server.data_state import (
    DataState,
    build_state_error_payload,
)
from apple_health_mcp.server.query import (
    OFFSET_DESCRIPTION,
    _coerce,
    _count_sql_from_page_sql,
    imports_present,
    normalise_end_date,
    normalise_pagination,
    query_to_json,
    require_imports_or_message,
    run_query,
    run_query_envelope,
    run_query_payload,
)
from tests._helpers import seed_one_import


def test_coerce_none() -> None:
    assert _coerce(None) is None


def test_coerce_bool_before_int() -> None:
    # ``bool`` must be coerced as bool, not the int 1.
    assert _coerce(True) is True
    assert _coerce(False) is False


def test_coerce_datetime() -> None:
    dt = _dt.datetime(2024, 1, 2, 3, 4, 5)
    assert _coerce(dt) == "2024-01-02 03:04:05"


def test_coerce_date() -> None:
    assert _coerce(_dt.date(2024, 1, 2)) == "2024-01-02"


def test_coerce_time() -> None:
    assert _coerce(_dt.time(3, 4, 5)) == "03:04:05"


def test_coerce_timedelta() -> None:
    td = _dt.timedelta(hours=1, minutes=2, seconds=3)
    assert _coerce(td) == "1:02:03"


def test_coerce_finite_float() -> None:
    assert _coerce(1.5) == 1.5


def test_coerce_nan_falls_back_to_string() -> None:
    out = _coerce(float("nan"))
    assert isinstance(out, str)
    assert out.lower() == "nan"


def test_coerce_inf_falls_back_to_string() -> None:
    out = _coerce(math.inf)
    assert isinstance(out, str)


def test_coerce_int() -> None:
    assert _coerce(42) == 42


def test_coerce_str() -> None:
    assert _coerce("hello") == "hello"


def test_coerce_decimal_stringifies() -> None:
    assert _coerce(Decimal("1.5")) == "1.5"


def test_coerce_bytes_hex() -> None:
    assert _coerce(b"\x00\xff") == "00ff"


def test_coerce_list_recurses() -> None:
    assert _coerce([1, _dt.date(2024, 1, 1), True]) == [1, "2024-01-01", True]


def test_coerce_tuple_recurses() -> None:
    assert _coerce((1, 2)) == [1, 2]


def test_coerce_dict_recurses() -> None:
    assert _coerce({"a": True, 1: _dt.date(2024, 1, 1)}) == {
        "a": True,
        "1": "2024-01-01",
    }


def test_coerce_unknown_falls_back_to_str() -> None:
    class _Marker:
        def __str__(self) -> str:
            return "marker"

    assert _coerce(_Marker()) == "marker"


def test_query_to_json_basic() -> None:
    conn = duckdb.connect(":memory:")
    rows = query_to_json(conn, "SELECT 1 AS x, 'hi' AS y")
    assert rows == [{"x": 1, "y": "hi"}]


def test_query_to_json_with_params() -> None:
    conn = duckdb.connect(":memory:")
    rows = query_to_json(conn, "SELECT ? AS v", [7])
    assert rows == [{"v": 7}]


def test_query_to_json_uses_lock_when_supplied() -> None:
    conn = duckdb.connect(":memory:")
    lock = Lock()
    rows = query_to_json(conn, "SELECT 1 AS x", lock=lock)
    assert rows == [{"x": 1}]
    # Lock should be released after the call.
    assert lock.acquire(blocking=False)
    lock.release()


def test_run_query_returns_pretty_json() -> None:
    conn = duckdb.connect(":memory:")
    # ``require_data=False`` so the wire-format assertion does not race the
    # empty-DB gate (this test verifies pretty-printing, not the gate).
    out = run_query(conn, "SELECT 1 AS x", require_data=False)
    assert "  " in out  # indented
    parsed = json.loads(out)
    assert parsed == [{"x": 1}]


def test_run_query_returns_error_string_on_failure() -> None:
    conn = duckdb.connect(":memory:")
    out = run_query(conn, "SELECT * FROM does_not_exist", require_data=False)
    assert out.startswith("Error: ")


def test_run_query_payload_pretty_prints() -> None:
    payload = {"a": 1, "b": [2, 3]}
    out = run_query_payload(payload)
    assert json.loads(out) == payload


@pytest.mark.parametrize("v", [1, 2, 3])
def test_query_to_json_int_types(v: int) -> None:
    conn = duckdb.connect(":memory:")
    rows = query_to_json(conn, f"SELECT CAST({v} AS BIGINT) AS x")
    assert rows == [{"x": v}]


def test_normalise_end_date_expands_date_only() -> None:
    """A bare ``YYYY-MM-DD`` becomes end-of-day microsecond precision."""
    assert normalise_end_date("2026-06-22") == "2026-06-22 23:59:59.999999"


def test_normalise_end_date_passes_iso_timestamp_through() -> None:
    """ISO 8601 timestamps with a time component are untouched."""
    full = "2026-06-22T10:00:00+09:00"
    assert normalise_end_date(full) == full


def test_normalise_end_date_passes_other_lengths_through() -> None:
    """Strings the heuristic cannot recognise round-trip unchanged.

    DuckDB will reject them at bind time with its own diagnostic; we
    intentionally do not pre-validate so the surface error stays the
    same as without the helper.
    """
    assert normalise_end_date("not-a-date") == "not-a-date"


def test_normalise_end_date_requires_dashes_at_positions_4_and_7() -> None:
    """A 10-char string without the date shape stays unchanged."""
    # Same length as YYYY-MM-DD but the separators are colons -- not a
    # date, so the helper does not expand it.
    assert normalise_end_date("12:34:56XY") == "12:34:56XY"


def test_imports_present_returns_false_when_imports_table_missing() -> None:
    """A DB opened against a non-apple-health file returns False, not raise.

    Without this, the surrounding gate would either propagate the
    CatalogException (crashing the tool call) or swallow it as
    ``"Error: Table imports does not exist"``, defeating the point of
    surfacing the structured state-error payload.
    """
    conn = duckdb.connect(":memory:")
    assert imports_present(conn) is False


def test_require_imports_or_message_returns_state_payload_when_empty() -> None:
    """v0.4 (issue #148): the empty-DB path returns the structured
    NEEDS_CONFIG payload (env var is cleared by the conftest autouse
    fixture) instead of the pre-v0.4 plain-string IMPORT_REQUIRED_MESSAGE.
    """
    conn = duckdb.connect(":memory:")
    assert require_imports_or_message(conn) == build_state_error_payload(DataState.NEEDS_CONFIG)


def test_require_imports_or_message_returns_none_when_imports_exist() -> None:
    """Once the gate sees data, the helper returns ``None`` so the caller proceeds."""
    from apple_health_mcp.db.schema import ensure_schema

    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    seed_one_import(conn)
    assert require_imports_or_message(conn) is None


# --- normalise_pagination ----------------------------------------------------


def test_normalise_pagination_defaults() -> None:
    assert normalise_pagination(None, None, default_limit=100, max_limit=1000) == (100, 0)


def test_normalise_pagination_caps_limit_at_max() -> None:
    assert normalise_pagination(5000, 0, default_limit=100, max_limit=1000) == (1000, 0)


def test_normalise_pagination_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        normalise_pagination(0, None, default_limit=100, max_limit=1000)


def test_normalise_pagination_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        normalise_pagination(-3, None, default_limit=100, max_limit=1000)


def test_normalise_pagination_clamps_negative_offset_to_zero() -> None:
    assert normalise_pagination(50, -7, default_limit=100, max_limit=1000) == (50, 0)


# --- OFFSET_DESCRIPTION -------------------------------------------------------


def test_offset_description_is_a_non_empty_string() -> None:
    """Constant used by every envelope tool's ``offset`` field annotation."""
    assert isinstance(OFFSET_DESCRIPTION, str)
    assert OFFSET_DESCRIPTION
    assert "Skip" in OFFSET_DESCRIPTION


# --- _count_sql_from_page_sql -------------------------------------------------


def test_count_sql_strips_limit_offset_tail() -> None:
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t WHERE x > ? LIMIT 50 OFFSET 100"
    out = _count_sql_from_page_sql(sql)
    assert "LIMIT" not in out.upper().split("FROM (", 1)[0]
    assert "COUNT(*)" in out
    # The inner subquery still contains the original WHERE clause.
    assert "WHERE x > ?" in out


def test_count_sql_strips_limit_only_tail() -> None:
    """A SELECT without OFFSET still has the trailing LIMIT clause removed."""
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t LIMIT 10"
    out = _count_sql_from_page_sql(sql)
    # Tail LIMIT is gone from the outer wrap; the wrap itself is bare COUNT.
    assert out.startswith("SELECT COUNT(*)")


# --- run_query_envelope ------------------------------------------------------


def _envelope_sql(table: str) -> str:
    return f"SELECT x, COUNT(*) OVER () AS _total FROM {table} ORDER BY x LIMIT 1 OFFSET 5"


def _seed_envelope_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2), (3)")


def test_run_query_envelope_recovers_total_when_offset_past_end() -> None:
    """F1: ``offset > total`` must still surface the true ``total``."""
    conn = duckdb.connect(":memory:")
    _seed_envelope_table(conn)
    out = run_query_envelope(conn, _envelope_sql("t"), [], offset=5, require_data=False)
    payload = json.loads(out)
    assert payload == {
        "items": [],
        "total": 3,
        "next_offset": None,
        "truncated_by_size": False,
        "size_budget_bytes": 950_000,
    }


def test_run_query_envelope_returns_zero_total_on_empty_table_no_offset() -> None:
    """An empty result set at offset=0 still wires ``total=0`` (no fallback)."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t ORDER BY x LIMIT 1 OFFSET 0"
    out = run_query_envelope(conn, sql, [], offset=0, require_data=False)
    assert json.loads(out) == {
        "items": [],
        "total": 0,
        "next_offset": None,
        "truncated_by_size": False,
        "size_budget_bytes": 950_000,
    }


def test_run_query_envelope_first_page_uses_window_total() -> None:
    """Non-empty pages keep the single-query total path."""
    conn = duckdb.connect(":memory:")
    _seed_envelope_table(conn)
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t ORDER BY x LIMIT 1 OFFSET 0"
    out = run_query_envelope(conn, sql, [], offset=0, require_data=False)
    payload = json.loads(out)
    assert payload["total"] == 3
    assert payload["next_offset"] == 1
    assert payload["items"] == [{"x": 1}]


def test_run_query_envelope_returns_error_string_on_failure() -> None:
    conn = duckdb.connect(":memory:")
    out = run_query_envelope(
        conn,
        "SELECT * FROM does_not_exist",
        [],
        offset=0,
        require_data=False,
    )
    assert out.startswith("Error: ")


def test_run_query_envelope_row_transform_applies_before_total_strip() -> None:
    """F4: ``row_transform`` runs per item before ``_total`` is dropped."""
    conn = duckdb.connect(":memory:")
    _seed_envelope_table(conn)
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t ORDER BY x LIMIT 100 OFFSET 0"
    captured: list[dict[str, Any]] = []

    def _transform(row: dict[str, Any]) -> dict[str, Any]:
        # Confirm the transform sees ``_total`` (it has not been popped yet)
        # and is allowed to mutate the wire-facing columns.
        captured.append(dict(row))
        row["x"] = row["x"] * 10
        return row

    out = run_query_envelope(conn, sql, [], offset=0, require_data=False, row_transform=_transform)
    payload = json.loads(out)
    assert [item["x"] for item in payload["items"]] == [10, 20, 30]
    assert all("_total" not in item for item in payload["items"])
    # ``_total`` is present at the moment the transform sees the row.
    assert captured and all("_total" in c for c in captured)


def test_run_query_envelope_gate_short_circuits_on_empty_db() -> None:
    """An empty DB returns the structured state-error payload.

    v0.4 (issue #148): the env-cleared conftest fixture forces the
    NEEDS_CONFIG branch so the assertion is deterministic regardless
    of the developer's local environment.
    """
    conn = duckdb.connect(":memory:")
    out = run_query_envelope(conn, "SELECT 1 AS x", [], offset=0)
    assert out == build_state_error_payload(DataState.NEEDS_CONFIG)


def test_run_query_envelope_accepts_explicit_size_budget() -> None:
    """v0.5 (issue #171): an explicit ``size_budget_bytes`` overrides the default."""
    conn = duckdb.connect(":memory:")
    _seed_envelope_table(conn)
    sql = "SELECT x, COUNT(*) OVER () AS _total FROM t ORDER BY x LIMIT 10 OFFSET 0"
    out = run_query_envelope(conn, sql, [], offset=0, require_data=False, size_budget_bytes=10)
    payload = json.loads(out)
    # Budget of 10 bytes cannot fit any item -- the clamp drops everything.
    assert payload["truncated_by_size"] is True
    assert payload["size_budget_bytes"] == 10
    assert payload["items"] == []
