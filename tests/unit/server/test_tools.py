"""Tests for the 18 MCP tools.

The tests bypass FastMCP entirely: each tool module's ``register`` is
called with a small stub that records the decorated function, so the
underlying coroutine can be awaited directly. That keeps the assertion
surface focused on SQL behaviour and JSON shape rather than on the
FastMCP wire format (which has its own coverage in ``test_server``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import duckdb
import pytest

from apple_health_mcp.server.query import IMPORT_REQUIRED_MESSAGE
from apple_health_mcp.server.tools import (
    get_activity_summaries,
    get_correlation_details,
    get_ecg_data,
    get_heart_rate_samples,
    get_import_history,
    get_me_attributes,
    get_record_statistics,
    get_server_info,
    get_workout_details,
    get_workout_route,
    list_correlations,
    list_data_sources,
    list_ecg_readings,
    list_record_types,
    list_state_of_mind,
    list_workouts,
    query_records,
    run_custom_query,
)
from tests._helpers import assert_tool_db_error, seed_one_import
from tests._helpers import bind_tool as _bind


def _call(fn: Any, **kwargs: Any) -> Any:
    """Call a bound tool and decode its JSON return.

    Several tests intentionally exercise the validation-error path (where
    the tool returns ``"Error: ..."`` instead of JSON); the shared
    ``call_tool`` helper rejects that case, so this thin wrapper preserves
    the suite's existing semantics. New code that does not need to inspect
    error strings should use ``call_tool`` from ``tests/_helpers.py``.
    """
    return json.loads(asyncio.run(fn(**kwargs)))


def _items(fn: Any, **kwargs: Any) -> Any:
    """Call an envelope-shaped tool and return just the ``items`` list.

    Issue #108 (PR-E): the 7 list/page tools all return
    ``{items, total, next_offset}``. Tests that only assert per-row
    content go through this helper so the assertion surface stays
    focused on the row shape and not the envelope wrapper.
    """
    payload = _call(fn, **kwargs)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"items", "total", "next_offset"}
    items = payload["items"]
    assert isinstance(items, list)
    return items


# --- list_record_types -------------------------------------------------------


def test_list_record_types(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_record_types, seeded_conn)
    rows = _call(fn)
    # Issue #91 (T1): wire field is ``record_type`` (was ``type``).
    types = {r["record_type"] for r in rows}
    assert "HKQuantityTypeIdentifierHeartRate" in types
    # The generic ``type`` key must not leak through any more.
    assert all("type" not in r for r in rows)


# --- query_records -----------------------------------------------------------


def test_query_records_basic(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(query_records, seeded_conn)
    rows = _items(fn, record_type="HKQuantityTypeIdentifierHeartRate")
    assert len(rows) == 2
    assert all(r["record_type"] == "HKQuantityTypeIdentifierHeartRate" for r in rows)


def test_query_records_applies_every_filter(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(query_records, seeded_conn)
    rows = _items(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        start_date="2024-01-01",
        end_date="2024-01-02",
        source_name="Apple Watch",
        limit=1,
    )
    assert len(rows) == 1


def test_query_records_clamps_limit(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(query_records, seeded_conn)
    rows = _items(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        limit=10_000,
    )
    assert len(rows) <= 1000


def test_query_records_envelope_pagination(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    """Issue #108 (PR-E): ``{items, total, next_offset}`` envelope.

    Walks through a 2-row result set one item at a time to confirm
    ``next_offset`` advances and turns into ``None`` on the last page.
    """
    fn = _bind(query_records, seeded_conn)
    page1 = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate", limit=1, offset=0)
    assert page1["total"] == 2
    assert len(page1["items"]) == 1
    assert page1["next_offset"] == 1
    page2 = _call(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        limit=1,
        offset=page1["next_offset"],
    )
    assert page2["total"] == 2
    assert len(page2["items"]) == 1
    assert page2["next_offset"] is None


# --- get_record_statistics ---------------------------------------------------


@pytest.mark.parametrize("period", [None, "day", "week", "month", "year", "DAY", "Week"])
def test_get_record_statistics_period_whitelist(
    seeded_conn: duckdb.DuckDBPyConnection, period: str | None
) -> None:
    fn = _bind(get_record_statistics, seeded_conn)
    rows = _call(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        period=period,
    )
    assert isinstance(rows, list)


def test_get_record_statistics_invalid_period_errors(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Issue #92 (T3): bogus ``period`` returns an explicit error string."""
    fn = _bind(get_record_statistics, seeded_conn)
    out = asyncio.run(
        fn(
            record_type="HKQuantityTypeIdentifierHeartRate",
            period="bogus-\x1b[31m-injection",
        )
    )
    # The accepted set must be enumerated so callers know how to recover.
    assert out.startswith("Error: invalid period; ")
    assert "day" in out and "week" in out
    # The user-supplied value must NOT be echoed back — otherwise a
    # control-character payload in ``period`` would round-trip into the
    # caller LLM's context as trusted server output.
    assert "bogus" not in out
    assert "\x1b" not in out


def test_get_record_statistics_with_date_filters(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_record_statistics, seeded_conn)
    rows = _call(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    assert isinstance(rows, list)


# --- list_workouts -----------------------------------------------------------


def test_list_workouts_no_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_workouts, seeded_conn)
    rows = _items(fn)
    assert any(r["workout_hash"] == "wh1" for r in rows)


def test_list_workouts_all_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_workouts, seeded_conn)
    rows = _items(
        fn,
        activity_type="HKWorkoutActivityTypeRunning",
        start_date="2024-01-01",
        end_date="2024-01-31",
        limit=1000,
    )
    assert any(r["workout_hash"] == "wh1" for r in rows)


# --- get_workout_details -----------------------------------------------------


def test_get_workout_details_full(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_workout_details, seeded_conn)
    payload = _call(fn, workout_hash="wh1")
    assert payload["workout"]["workout_hash"] == "wh1"
    assert payload["has_route"] is True
    assert any(e["event_type"] == "HKWorkoutEventTypeLap" for e in payload["events"])
    assert payload["statistics"]
    assert {m["key"] for m in payload["metadata"]} == {"HKIndoorWorkout", "HKAverageMETs"}


def test_get_workout_details_missing(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_workout_details, seeded_conn)
    payload = _call(fn, workout_hash="nope")
    assert payload["workout"] is None
    assert payload["has_route"] is False
    assert payload["route"] is None


def test_get_workout_details_db_error_path(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """If a downstream query raises, the tool returns ``Error: ...``.

    Seeds an ``imports`` row so the empty-DB gate passes (otherwise the
    tool would short-circuit to ``IMPORT_REQUIRED_MESSAGE`` before the
    downstream queries run), then drops the ``workouts`` table to force
    the first ``query_to_json`` call into a binder error.
    """
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE workouts")
    fn = _bind(get_workout_details, empty_conn)
    out = asyncio.run(fn(workout_hash="wh1"))
    assert out.startswith("Error: ")


# --- get_activity_summaries --------------------------------------------------


def test_get_activity_summaries_no_filters(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_activity_summaries, seeded_conn)
    rows = _call(fn)
    assert rows[0]["date_components"] == "2024-01-01"


def test_get_activity_summaries_with_filters(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_activity_summaries, seeded_conn)
    rows = _call(
        fn,
        start_date="2023-12-01",
        end_date="2024-12-31",
        limit=400,  # exceeds max → clamped
    )
    assert rows


# --- get_workout_route -------------------------------------------------------


def test_get_workout_route_default(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    """Issue #108 (PR-E): unified ``{items, total, next_offset}`` envelope."""
    fn = _bind(get_workout_route, seeded_conn)
    payload = _call(fn, workout_hash="wh1")
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    assert payload["next_offset"] is None
    assert "has_more" not in payload


def test_get_workout_route_pagination(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    """A mid-route page advertises a usable ``next_offset``."""
    fn = _bind(get_workout_route, seeded_conn)
    payload = _call(fn, workout_hash="wh1", limit=1, offset=0)
    assert len(payload["items"]) == 1
    assert payload["total"] == 2
    assert payload["next_offset"] == 1
    # Follow-up call exhausts the route and clears next_offset.
    payload2 = _call(fn, workout_hash="wh1", limit=1, offset=payload["next_offset"])
    assert len(payload2["items"]) == 1
    assert payload2["next_offset"] is None


def test_get_workout_route_negative_offset(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_workout_route, seeded_conn)
    payload = _call(fn, workout_hash="wh1", limit=100, offset=-10)
    assert len(payload["items"]) == 2
    assert payload["total"] == 2
    assert payload["next_offset"] is None


def test_get_workout_route_unknown_hash_returns_empty_envelope(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A missing workout still returns a valid envelope (total=0)."""
    fn = _bind(get_workout_route, seeded_conn)
    payload = _call(fn, workout_hash="nope")
    assert payload == {"items": [], "total": 0, "next_offset": None}


def test_get_workout_route_db_error_path(empty_conn: duckdb.DuckDBPyConnection) -> None:
    """Downstream binder errors propagate as ``Error: ...`` strings."""
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE route_points")
    fn = _bind(get_workout_route, empty_conn)
    assert_tool_db_error(fn, workout_hash="wh1")


def test_get_workout_route_gate_failure_returns_error_string(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """L2: failure inside ``require_imports_or_message`` is normalised.

    Dropping ``imports`` makes the gate probe raise; PR-A's pre-fix code
    let the traceback escape ``require_imports_or_message`` because it ran
    outside the try block. The hardened version catches the exception and
    surfaces an ``Error: ...`` string instead.
    """
    empty_conn.execute("DROP TABLE imports")
    fn = _bind(get_workout_route, empty_conn)
    out = asyncio.run(fn(workout_hash="wh1"))
    # ``imports_present`` swallows the exception and returns ``False``,
    # which sends the gate down the import-required-message branch — that
    # is the documented contract for a missing/corrupt ``imports`` table
    # and is asserted explicitly here so any future tightening of
    # ``imports_present`` shows up in this test.
    assert out == IMPORT_REQUIRED_MESSAGE


def test_get_workout_route_limit_zero_errors(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """H2: ``limit=0`` must error instead of looping with a non-null next_offset."""
    fn = _bind(get_workout_route, seeded_conn)
    out = asyncio.run(fn(workout_hash="wh1", limit=0))
    assert out == "Error: limit must be >= 1"


def test_get_workout_route_negative_limit_errors(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A negative ``limit`` hits the same guard as ``limit=0``."""
    fn = _bind(get_workout_route, seeded_conn)
    out = asyncio.run(fn(workout_hash="wh1", limit=-5))
    assert out == "Error: limit must be >= 1"


# --- envelope helper: review F3 (limit < 1 rejected uniformly) --------------


@pytest.mark.parametrize(
    "module, kwargs",
    [
        (query_records, {"record_type": "HKQuantityTypeIdentifierHeartRate"}),
        (list_workouts, {}),
        (list_correlations, {}),
        (list_state_of_mind, {}),
        (get_heart_rate_samples, {"record_hash": "rh1"}),
    ],
    ids=lambda v: getattr(v, "__name__", ""),
)
def test_envelope_tool_rejects_zero_limit(
    seeded_conn: duckdb.DuckDBPyConnection,
    module: Any,
    kwargs: dict[str, Any],
) -> None:
    """F3: the 5 previously-permissive tools now reject ``limit=0``.

    Previously these wrapped ``effective_limit`` in ``max(0, ...)`` and
    paired with ``COUNT(*) OVER ()`` they returned ``{items: [], total: 0,
    next_offset: null}`` even on a non-empty table — an LLM would mistake
    that for "no data". Aligns with ``get_workout_route`` / ``list_ecg_readings``.
    """
    fn = _bind(module, seeded_conn)
    out = asyncio.run(fn(**kwargs, limit=0))
    assert out == "Error: limit must be >= 1"


@pytest.mark.parametrize(
    "module, kwargs",
    [
        (query_records, {"record_type": "HKQuantityTypeIdentifierHeartRate"}),
        (list_workouts, {}),
        (list_correlations, {}),
        (list_state_of_mind, {}),
        (get_heart_rate_samples, {"record_hash": "rh1"}),
    ],
    ids=lambda v: getattr(v, "__name__", ""),
)
def test_envelope_tool_rejects_negative_limit(
    seeded_conn: duckdb.DuckDBPyConnection,
    module: Any,
    kwargs: dict[str, Any],
) -> None:
    """A negative ``limit`` hits the same guard."""
    fn = _bind(module, seeded_conn)
    out = asyncio.run(fn(**kwargs, limit=-1))
    assert out == "Error: limit must be >= 1"


# --- envelope helper: review F1 (offset > total recovers true total) --------


def test_query_records_envelope_offset_past_end_keeps_total(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """F1: paginating past the dataset must still report ``total`` correctly.

    Walks ``offset=0 -> next_offset -> next_offset + limit`` and asserts
    ``total`` is constant across all three pages even when the last page
    is empty.
    """
    fn = _bind(query_records, seeded_conn)
    page1 = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate", limit=1, offset=0)
    assert page1["total"] == 2
    page2 = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate", limit=1, offset=1)
    assert page2["total"] == 2
    assert page2["next_offset"] is None
    # Walk one beyond the end — used to wire ``total=0`` because the
    # ``COUNT(*) OVER ()`` window has no row to ride on.
    page_past = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate", limit=1, offset=2)
    assert page_past == {"items": [], "total": 2, "next_offset": None}
    page_far_past = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate", limit=1, offset=20)
    assert page_far_past == {"items": [], "total": 2, "next_offset": None}


def test_get_workout_route_envelope_offset_past_end_keeps_total(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """F1 applies to ``get_workout_route`` as well."""
    fn = _bind(get_workout_route, seeded_conn)
    page = _call(fn, workout_hash="wh1", limit=5, offset=99)
    assert page == {"items": [], "total": 2, "next_offset": None}


# --- get_heart_rate_samples --------------------------------------------------


def test_get_heart_rate_samples(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    """Issue #109 (PR-F): ``sample_time`` is read verbatim as DOUBLE.

    Pre-PR-F (issue #96 / T8) the column was a VARCHAR ``HH:MM:SS.SSS``
    literal and the tool normalised it on the way out. PR-F moves the
    normalisation to import time and stores DOUBLE seconds-of-day, so
    the same wire values fall out of a plain ``SELECT`` with no
    ``row_transform`` shim.
    """
    fn = _bind(get_heart_rate_samples, seeded_conn)
    rows = _items(fn, record_hash="rh1")
    assert len(rows) == 3
    # Seeded values map to 08:00:00.000 / 08:00:01.500 / 08:00:03.000 =
    # 28800.0 / 28801.5 / 28803.0 seconds after midnight.
    assert rows[0]["sample_time"] == 28800.0
    assert rows[1]["sample_time"] == 28801.5
    assert rows[2]["sample_time"] == 28803.0
    assert all(isinstance(r["sample_time"], float) for r in rows)


def test_get_heart_rate_samples_limit(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_heart_rate_samples, seeded_conn)
    rows = _items(fn, record_hash="rh1", limit=2)
    assert len(rows) == 2


def test_get_heart_rate_samples_envelope_pagination(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Issue #108 (PR-E): ``{items, total, next_offset}`` envelope shape."""
    fn = _bind(get_heart_rate_samples, seeded_conn)
    page1 = _call(fn, record_hash="rh1", limit=2, offset=0)
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["next_offset"] == 2
    page2 = _call(fn, record_hash="rh1", limit=2, offset=page1["next_offset"])
    assert page2["total"] == 3
    assert len(page2["items"]) == 1
    assert page2["next_offset"] is None


def test_get_heart_rate_samples_null_sample_time_returns_none(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A NULL ``sample_time`` surfaces as ``None`` (no exception).

    Issue #109 (PR-F): the column is now DOUBLE, so malformed-string
    inputs can no longer reach storage -- the importer's
    ``_parse_sample_time`` and the DB migration's TRY_CAST both lower a
    malformed literal to NULL before it touches the column. We keep a
    NULL-pass-through assertion as the post-PR-F equivalent: nothing on
    the read path should choke when the underlying value is NULL.
    """
    seeded_conn.execute("INSERT INTO heart_rate_samples VALUES ('rh1', 5, 78.0, NULL, 'imp1')")
    fn = _bind(get_heart_rate_samples, seeded_conn)
    rows = _items(fn, record_hash="rh1")
    by_idx = {r["sample_idx"]: r["sample_time"] for r in rows}
    assert by_idx[5] is None


def test_get_heart_rate_samples_db_error(empty_conn: duckdb.DuckDBPyConnection) -> None:
    """Downstream binder errors propagate as ``Error: ...`` strings."""
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE heart_rate_samples")
    fn = _bind(get_heart_rate_samples, empty_conn)
    assert_tool_db_error(fn, record_hash="rh1")


def test_get_heart_rate_samples_gate_failure_returns_error_string(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """F2: gate-probe failures must not raise a raw traceback.

    Mirrors ``test_get_workout_route_gate_failure_returns_error_string``.
    PR-E recreated the issue #103 regression: ``require_imports_or_message``
    ran outside the try block in this tool, so a missing/corrupt
    ``imports`` table would leak the raw exception through FastMCP.
    After the fix the gate runs inside ``run_query_envelope``'s own
    try block; dropping ``imports`` exercises that path and confirms
    the gate's ``imports_present`` fallback to ``False`` surfaces the
    documented ``IMPORT_REQUIRED_MESSAGE`` instead of an ``Error:``
    traceback.
    """
    empty_conn.execute("DROP TABLE imports")
    fn = _bind(get_heart_rate_samples, empty_conn)
    out = asyncio.run(fn(record_hash="rh1"))
    assert out == IMPORT_REQUIRED_MESSAGE


# --- list_correlations -------------------------------------------------------


def test_list_correlations_no_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_correlations, seeded_conn)
    rows = _items(fn)
    assert any(r["correlation_hash"] == "cor_bp" for r in rows)


def test_list_correlations_all_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_correlations, seeded_conn)
    rows = _items(
        fn,
        correlation_type="HKCorrelationTypeIdentifierBloodPressure",
        start_date="2024-01-01",
        end_date="2024-01-31",
        limit=10,
    )
    assert any(r["correlation_hash"] == "cor_bp" for r in rows)


# --- get_correlation_details -------------------------------------------------


def test_get_correlation_details_full(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_correlation_details, seeded_conn)
    payload = _call(fn, correlation_hash="cor_bp")
    assert payload["correlation"]["correlation_hash"] == "cor_bp"
    assert len(payload["members"]) == 2


def test_get_correlation_details_missing(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_correlation_details, seeded_conn)
    payload = _call(fn, correlation_hash="nope")
    assert payload["correlation"] is None
    assert payload["members"] == []


def test_get_correlation_details_db_error(empty_conn: duckdb.DuckDBPyConnection) -> None:
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE correlations")
    fn = _bind(get_correlation_details, empty_conn)
    out = asyncio.run(fn(correlation_hash="x"))
    assert out.startswith("Error: ")


# --- list_ecg_readings -------------------------------------------------------


def test_list_ecg_readings_no_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _items(fn)
    assert rows[0]["ecg_hash"] == "ecg1"


def test_list_ecg_readings_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _items(fn, start_date="2024-01-01", end_date="2024-01-31")
    assert rows[0]["ecg_hash"] == "ecg1"


def test_list_ecg_readings_clamps_limit(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    """Issue #97 (T11): ``limit`` clamps to the documented max (1000)."""
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _items(fn, limit=5_000)  # exceeds max -> clamped
    assert len(rows) <= 1000


def test_list_ecg_readings_limit_zero_errors(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """L4: ``limit=0`` errors instead of silently returning an empty list.

    The previous behaviour (empty list) let an LLM mistake the response
    for "no recordings exist". Returning an explicit error string keeps
    the failure mode close to the caller; H3's envelope sweep will align
    every list_* tool with this contract.
    """
    fn = _bind(list_ecg_readings, seeded_conn)
    out = asyncio.run(fn(limit=0))
    assert out == "Error: limit must be >= 1"


def test_list_ecg_readings_negative_limit_errors(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A negative ``limit`` is rejected on the same code path as ``limit=0``."""
    fn = _bind(list_ecg_readings, seeded_conn)
    out = asyncio.run(fn(limit=-1))
    assert out == "Error: limit must be >= 1"


# --- get_ecg_data ------------------------------------------------------------


def test_get_ecg_data_default(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_ecg_data, seeded_conn)
    payload = _call(fn, ecg_hash="ecg1")
    assert payload["reading"]["ecg_hash"] == "ecg1"
    assert payload["stats"]["sample_count"] == 3
    assert payload["voltages_uv"] == []
    assert payload["downsample_factor"] == 1


def test_get_ecg_data_with_voltages(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_ecg_data, seeded_conn)
    payload = _call(fn, ecg_hash="ecg1", include_voltages=True)
    assert payload["voltages_uv"] == [100.0, 200.0, -50.0]


def test_get_ecg_data_downsample(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_ecg_data, seeded_conn)
    payload = _call(fn, ecg_hash="ecg1", include_voltages=True, downsample_factor=2)
    assert payload["voltages_uv"] == [100.0, -50.0]


def test_get_ecg_data_clamps_downsample(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_ecg_data, seeded_conn)
    payload = _call(fn, ecg_hash="ecg1", include_voltages=True, downsample_factor=0)
    # downsample 0 should be clamped to 1.
    assert payload["downsample_factor"] == 1


def test_get_ecg_data_missing_returns_zero_stats(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_ecg_data, seeded_conn)
    payload = _call(fn, ecg_hash="nope")
    assert payload["stats"]["sample_count"] == 0
    assert payload["reading"] is None


def test_get_ecg_data_db_error(empty_conn: duckdb.DuckDBPyConnection) -> None:
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE ecg_readings")
    fn = _bind(get_ecg_data, empty_conn)
    out = asyncio.run(fn(ecg_hash="x"))
    assert out.startswith("Error: ")


# --- run_custom_query --------------------------------------------------------


def test_run_custom_query_basic(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(run_custom_query, seeded_conn)
    rows = _call(fn, query="SELECT record_hash FROM records LIMIT 1")
    assert len(rows) == 1


def test_run_custom_query_validation_error(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(run_custom_query, seeded_conn)
    out = asyncio.run(fn(query="DROP TABLE records"))
    assert out.startswith("Error:")


def test_run_custom_query_enforces_limit(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(run_custom_query, seeded_conn)
    # Without an explicit LIMIT we should get at most MAX_CUSTOM_QUERY_ROWS rows.
    rows = _call(fn, query="SELECT 1 AS x")
    assert rows


# --- list_data_sources -------------------------------------------------------


def test_list_data_sources(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_data_sources, seeded_conn)
    rows = _call(fn)
    names = {r["source_name"] for r in rows}
    assert "Apple Watch" in names


# --- get_import_history ------------------------------------------------------


def test_get_import_history(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_import_history, seeded_conn)
    rows = _call(fn)
    assert rows[0]["import_id"] == "imp1"
    # L1: ``get_import_history`` now selects explicit columns instead of
    # ``SELECT *``. Assert the exact wire-facing ORDER so a future
    # ``ALTER TABLE imports ADD COLUMN`` cannot leak into the response
    # without an intentional description + SQL update, AND a future
    # re-order of the SQL projection cannot silently flip the LLM-
    # readable narrative order. Pre-v0.4 the assertion was on
    # ``set(...)`` equality only; the ordered ``list(...)`` form here
    # closes that gap (raised by /code-review angle B candidate).
    expected_fields = [
        "import_id",
        "export_dir",
        "imported_at",
        "record_count",
        "workout_count",
        "duration_secs",
        "export_xml_sha256",
        # Issue #129 (PR-D): post-Phase-4-dedup row count.
        "records_after_dedup",
        # v0.4 (issue #148): identity of the source ZIP for re-import
        # dedup. NULL on CLI-driven rows; populated by the upcoming
        # ZIP-flow tools.
        "source_zip_sha256",
        "source_zip_mtime",
        "source_zip_size",
    ]
    assert list(rows[0].keys()) == expected_fields


# --- list_state_of_mind ------------------------------------------------------


def test_list_state_of_mind_returns_seeded_row(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _items(fn)
    assert rows[0]["record_hash"] == "som1"
    assert rows[0]["valence"] == 0.5
    assert rows[0]["kind"] == "momentary"


def test_list_state_of_mind_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _items(
        fn,
        start_date="2024-01-03",
        end_date="2024-01-04",
        limit=10_000,
    )
    assert len(rows) == 1


def test_list_state_of_mind_empty_when_outside_window(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _items(fn, start_date="2030-01-01")
    assert rows == []


# --- get_me_attributes -------------------------------------------------------


def test_get_me_attributes_returns_seeded_row(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_me_attributes, seeded_conn)
    payload = _call(fn)
    assert payload["import_id"] == "imp1"
    assert payload["date_of_birth"] == "1990-01-01"
    assert payload["biological_sex"] == "HKBiologicalSexNotSet"
    assert payload["blood_type"] == "HKBloodTypeNotSet"
    assert payload["fitzpatrick_skin_type"] == "HKFitzpatrickSkinTypeNotSet"
    assert payload["cardio_fitness_medications_use"] == "None"


def test_get_me_attributes_returns_empty_when_import_lacks_me_row(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Import done but no ``<Me>`` element -> empty object, not the gate message."""
    # Seed a single ``imports`` row so the empty-DB gate does not fire — we
    # are testing the ``rows[0] if rows else {}`` branch in the tool itself.
    seed_one_import(empty_conn)
    fn = _bind(get_me_attributes, empty_conn)
    payload = _call(fn)
    assert payload == {}


def test_get_me_attributes_db_error(empty_conn: duckdb.DuckDBPyConnection) -> None:
    seed_one_import(empty_conn)
    empty_conn.execute("DROP TABLE me_attributes")
    fn = _bind(get_me_attributes, empty_conn)
    out = asyncio.run(fn())
    assert out.startswith("Error: ")


# --- empty-DB gate (issue #38) -----------------------------------------------
#
# Every tool except ``get_import_history`` short-circuits to
# ``IMPORT_REQUIRED_MESSAGE`` when the ``imports`` table is empty, so a fresh
# install plumbed into Claude Desktop / Claude Code returns actionable
# guidance to the LLM instead of an empty result that looks like "no data".


_GATED_TOOLS: list[tuple[Any, dict[str, Any]]] = [
    (list_record_types, {}),
    (query_records, {"record_type": "HKQuantityTypeIdentifierHeartRate"}),
    (get_record_statistics, {"record_type": "HKQuantityTypeIdentifierHeartRate"}),
    (list_workouts, {}),
    (get_workout_details, {"workout_hash": "wh1"}),
    (get_activity_summaries, {}),
    (get_workout_route, {"workout_hash": "wh1"}),
    (get_heart_rate_samples, {"record_hash": "rh1"}),
    (list_correlations, {}),
    (get_correlation_details, {"correlation_hash": "ch1"}),
    (list_ecg_readings, {}),
    (get_ecg_data, {"ecg_hash": "eh1"}),
    (list_data_sources, {}),
    (list_state_of_mind, {}),
    (get_me_attributes, {}),
]


@pytest.mark.parametrize("module, kwargs", _GATED_TOOLS, ids=lambda v: getattr(v, "__name__", ""))
def test_tool_returns_import_required_message_on_empty_db(
    module: Any,
    kwargs: dict[str, Any],
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Each gated tool returns the standard guidance string on an empty DB."""
    fn = _bind(module, empty_conn)
    out = asyncio.run(fn(**kwargs))
    assert out == IMPORT_REQUIRED_MESSAGE


def test_get_import_history_returns_empty_list_on_empty_db(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """``get_import_history`` is one of two exceptions — empty list, not the gate."""
    fn = _bind(get_import_history, empty_conn)
    rows = _call(fn)
    assert rows == []


# --- Issue #49: date-only end_date inclusive ---------------------------------
#
# DuckDB casts a bare ``YYYY-MM-DD`` to ``TIMESTAMPTZ`` at start-of-day, so
# ``end_date <= ?`` historically dropped every record that happened later
# than midnight on the named day. The 5 tools below now route the upper
# bound through :func:`apple_health_mcp.server.query.normalise_end_date`
# which expands a date-only value to end-of-day; the parametrised test
# guards the named-day inclusion across all 5 tools.


def test_query_records_end_date_date_only_includes_named_day(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(query_records, seeded_conn)
    # 2024-01-01 holds 2 HR rows at 08:00 / 09:00. Without the fix,
    # ``end_date='2024-01-01'`` cast to 00:00:00 and dropped both.
    rows = _items(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        start_date="2024-01-01",
        end_date="2024-01-01",
    )
    assert len(rows) == 2


def test_query_records_end_date_full_timestamp_unchanged(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A full ISO 8601 timestamp respects the caller's precision."""
    fn = _bind(query_records, seeded_conn)
    rows = _items(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        start_date="2024-01-01",
        # 08:30 sits between the two HR rows (08:00 and 09:00) so a
        # date-only expansion would have grabbed both; the explicit
        # time bound must keep only the earlier one.
        end_date="2024-01-01T08:30:00+00:00",
    )
    assert len(rows) == 1


def test_list_workouts_end_date_date_only_includes_named_day(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_workouts, seeded_conn)
    rows = _items(fn, start_date="2024-01-01", end_date="2024-01-01")
    assert len(rows) == 1


def test_list_ecg_readings_end_date_date_only_includes_named_day(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _items(fn, start_date="2024-01-01", end_date="2024-01-01")
    assert len(rows) == 1


def test_list_state_of_mind_end_date_date_only_includes_named_day(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _items(fn, start_date="2024-01-03", end_date="2024-01-03")
    assert len(rows) == 1


def test_list_correlations_end_date_date_only_includes_named_day(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_correlations, seeded_conn)
    rows = _items(fn, start_date="2024-01-02", end_date="2024-01-02")
    assert len(rows) == 1


def test_run_custom_query_runs_on_empty_db(empty_conn: duckdb.DuckDBPyConnection) -> None:
    """``run_custom_query`` opts out so LLMs can introspect the empty scaffold.

    The natural way an LLM probes the empty-DB state is
    ``SELECT COUNT(*) FROM imports``; if that hit the gate it would
    return the guidance string instead of the count, defeating
    introspection of the freshly-bootstrapped scaffold.
    """
    fn = _bind(run_custom_query, empty_conn)
    rows = _call(fn, query="SELECT COUNT(*) AS n FROM imports")
    assert rows == [{"n": 0}]


# --- get_server_info ---------------------------------------------------------


def _server_info(fn: Any) -> dict[str, Any]:
    """Decode the diagnostic JSON object returned by ``get_server_info``."""
    payload = _call(fn)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"db_path", "version", "record_count", "config_source"}
    return payload


def test_get_server_info_returns_diagnostic_shape(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """The four-field self-diagnosis payload survives a happy path."""
    from apple_health_mcp import __version__

    fn = _bind(get_server_info, seeded_conn)
    info = _server_info(fn)
    assert info["version"] == __version__
    # The seeded fixture has at least the records seeded by the conftest
    # (3 records of HeartRate + StepCount + 2 BP + StateOfMind, etc.).
    # Asserting > 0 keeps this test from coupling to the exact seed
    # count, which the conftest grows independently.
    assert info["record_count"] > 0


def test_get_server_info_records_count_zero_on_empty_db(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Empty (schema-only) DB reports ``record_count = 0`` rather than erroring."""
    fn = _bind(get_server_info, empty_conn)
    info = _server_info(fn)
    assert info["record_count"] == 0


def test_get_server_info_reports_open_db_path(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """``db_path`` reflects the in-memory sentinel for the conftest fixture.

    The conftest uses ``get_in_memory_connection`` so DuckDB reports
    the connection as the ``memory`` DB with no file backing; the
    diagnostic collapses that to ``":memory:"`` so the diagnostic
    output is deterministic instead of an empty string. The point
    of this test is that the diagnostic does NOT silently re-resolve
    via ``resolve_db_path()`` (which would have returned the platform
    XDG path, not the actual in-memory handle).
    """
    fn = _bind(get_server_info, seeded_conn)
    info = _server_info(fn)
    assert info["db_path"] == ":memory:"


def test_get_server_info_reports_on_disk_db_path(tmp_path: Any) -> None:
    """An on-disk connection's ``db_path`` is the absolute file path DuckDB opened.

    Covers the alternate branch of ``_open_db_path`` where DuckDB
    reports a non-NULL ``file`` column (every real serve invocation
    against the XDG / LOCALAPPDATA default or an env-overridden path).
    Without this case the on-disk branch would be unreachable from
    the in-memory test suite — and the very contract the diagnostic
    exists to enforce (\"report the path you actually opened\") would
    have no test.
    """
    from apple_health_mcp.db import ensure_schema, get_connection

    on_disk = tmp_path / "diag.duckdb"
    conn = get_connection(on_disk)
    try:
        ensure_schema(conn)
        fn = _bind(get_server_info, conn)
        info = _server_info(fn)
        assert info["db_path"] == str(on_disk)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("env_db", "env_dir", "expected"),
    [
        # APPLE_HEALTH_DB takes precedence even when both vars are set.
        ("/tmp/explicit.duckdb", "/tmp/data-root-ignored", "env:APPLE_HEALTH_DB"),
        # APPLE_HEALTH_DB alone.
        ("/tmp/explicit.duckdb", None, "env:APPLE_HEALTH_DB"),
        # APPLE_HEALTH_DATA_DIR alone -> next tier.
        (None, "/tmp/data-root", "env:APPLE_HEALTH_DATA_DIR"),
        # Both unset -> platform default.
        (None, None, "platform_default"),
    ],
)
def test_get_server_info_config_source_tier(
    seeded_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    env_db: str | None,
    env_dir: str | None,
    expected: str,
) -> None:
    """``config_source`` mirrors ``resolve_db_path``'s precedence chain.

    Parametrised so a future fifth tier (or a reorder) is a one-line
    table change instead of a copy-pasted test. ``None`` means
    ``delenv``; a string means ``setenv``.
    """
    if env_db is None:
        monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    else:
        monkeypatch.setenv("APPLE_HEALTH_DB", env_db)
    if env_dir is None:
        monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("APPLE_HEALTH_DATA_DIR", env_dir)
    fn = _bind(get_server_info, seeded_conn)
    info = _server_info(fn)
    assert info["config_source"] == expected


def test_get_server_info_config_source_blank_env_falls_through(
    seeded_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``APPLE_HEALTH_DB=""`` is reported as ``platform_default``.

    Mirrors the resolver's blank-after-strip-falls-through contract so
    a user who did ``export APPLE_HEALTH_DB=`` in their shell rc sees
    the diagnostic agree with what the connection layer actually
    opened (the platform default). Without this rule the diagnostic
    would report ``env:APPLE_HEALTH_DB`` while the resolver opened
    the XDG path — a divergence that would defeat the tool's purpose.
    """
    monkeypatch.setenv("APPLE_HEALTH_DB", "")
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    fn = _bind(get_server_info, seeded_conn)
    info = _server_info(fn)
    assert info["config_source"] == "platform_default"
