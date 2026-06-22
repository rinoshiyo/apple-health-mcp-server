"""Tests for ``server.query``."""

from __future__ import annotations

import datetime as _dt
import json
import math
from decimal import Decimal
from threading import Lock

import duckdb
import pytest

from apple_health_mcp.server.query import (
    _coerce,
    query_to_json,
    run_query,
    run_query_payload,
)


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
    out = run_query(conn, "SELECT 1 AS x")
    assert "  " in out  # indented
    parsed = json.loads(out)
    assert parsed == [{"x": 1}]


def test_run_query_returns_error_string_on_failure() -> None:
    conn = duckdb.connect(":memory:")
    out = run_query(conn, "SELECT * FROM does_not_exist")
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
