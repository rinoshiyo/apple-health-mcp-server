"""Tests for the 17 MCP tools.

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

from apple_health_mcp.server.tools import (
    get_activity_summaries,
    get_correlation_details,
    get_ecg_data,
    get_heart_rate_samples,
    get_import_history,
    get_me_attributes,
    get_record_statistics,
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


# --- list_record_types -------------------------------------------------------


def test_list_record_types(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_record_types, seeded_conn)
    rows = _call(fn)
    types = {r["type"] for r in rows}
    assert "HKQuantityTypeIdentifierHeartRate" in types


# --- query_records -----------------------------------------------------------


def test_query_records_basic(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(query_records, seeded_conn)
    rows = _call(fn, record_type="HKQuantityTypeIdentifierHeartRate")
    assert len(rows) == 2
    assert all(r["record_type"] == "HKQuantityTypeIdentifierHeartRate" for r in rows)


def test_query_records_applies_every_filter(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(query_records, seeded_conn)
    rows = _call(
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
    rows = _call(
        fn,
        record_type="HKQuantityTypeIdentifierHeartRate",
        limit=10_000,
    )
    assert len(rows) <= 1000


# --- get_record_statistics ---------------------------------------------------


@pytest.mark.parametrize("period", [None, "day", "week", "month", "year", "bogus"])
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
    rows = _call(fn)
    assert any(r["workout_hash"] == "wh1" for r in rows)


def test_list_workouts_all_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_workouts, seeded_conn)
    rows = _call(
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


def test_get_workout_details_db_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the DB raises, the tool returns ``Error: ...`` instead of crashing."""
    fn = _bind(get_workout_details, duckdb.connect(":memory:"))
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
    fn = _bind(get_workout_route, seeded_conn)
    rows = _call(fn, workout_hash="wh1")
    assert len(rows) == 2


def test_get_workout_route_pagination(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_workout_route, seeded_conn)
    rows = _call(fn, workout_hash="wh1", limit=1, offset=1)
    assert len(rows) == 1


def test_get_workout_route_negative_offset(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_workout_route, seeded_conn)
    rows = _call(fn, workout_hash="wh1", limit=100, offset=-10)
    assert len(rows) == 2


# --- get_heart_rate_samples --------------------------------------------------


def test_get_heart_rate_samples(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_heart_rate_samples, seeded_conn)
    rows = _call(fn, record_hash="rh1")
    assert len(rows) == 3


def test_get_heart_rate_samples_limit(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(get_heart_rate_samples, seeded_conn)
    rows = _call(fn, record_hash="rh1", limit=2)
    assert len(rows) == 2


# --- list_correlations -------------------------------------------------------


def test_list_correlations_no_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_correlations, seeded_conn)
    rows = _call(fn)
    assert any(r["correlation_hash"] == "cor_bp" for r in rows)


def test_list_correlations_all_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_correlations, seeded_conn)
    rows = _call(
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


def test_get_correlation_details_db_error() -> None:
    fn = _bind(get_correlation_details, duckdb.connect(":memory:"))
    out = asyncio.run(fn(correlation_hash="x"))
    assert out.startswith("Error: ")


# --- list_ecg_readings -------------------------------------------------------


def test_list_ecg_readings_no_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _call(fn)
    assert rows[0]["ecg_hash"] == "ecg1"


def test_list_ecg_readings_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_ecg_readings, seeded_conn)
    rows = _call(fn, start_date="2024-01-01", end_date="2024-01-31")
    assert rows[0]["ecg_hash"] == "ecg1"


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


def test_get_ecg_data_db_error() -> None:
    fn = _bind(get_ecg_data, duckdb.connect(":memory:"))
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


# --- list_state_of_mind ------------------------------------------------------


def test_list_state_of_mind_returns_seeded_row(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _call(fn)
    assert rows[0]["record_hash"] == "som1"
    assert rows[0]["valence"] == 0.5
    assert rows[0]["kind"] == "momentary"


def test_list_state_of_mind_filters(seeded_conn: duckdb.DuckDBPyConnection) -> None:
    fn = _bind(list_state_of_mind, seeded_conn)
    rows = _call(
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
    rows = _call(fn, start_date="2030-01-01")
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


def test_get_me_attributes_returns_empty_when_no_row(
    empty_conn: duckdb.DuckDBPyConnection,
) -> None:
    fn = _bind(get_me_attributes, empty_conn)
    payload = _call(fn)
    assert payload == {}


def test_get_me_attributes_db_error() -> None:
    fn = _bind(get_me_attributes, duckdb.connect(":memory:"))
    out = asyncio.run(fn())
    assert out.startswith("Error: ")
