"""Shared fixtures for ``apple_health_mcp.server`` tests.

The in-memory DuckDB connection is seeded with the same synthetic shape the
Rust reference implementation used so the Python tools can be diffed
against the Rust contract at the JSON level.
"""

from __future__ import annotations

from collections.abc import Generator

import duckdb
import pytest

from apple_health_mcp.db import (
    ensure_schema,
    get_in_memory_connection,
    rebuild_daily_stats,
)
from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR


@pytest.fixture(autouse=True)
def _clear_export_zips_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` from every server-test process.

    v0.4 (issue #148): ``check_data_state`` returns ``NEEDS_CONFIG`` vs
    ``NEEDS_IMPORT`` depending on whether this env var is set. Without
    this fixture a developer (or CI shell) with the var exported would
    flip every empty-DB test from NEEDS_CONFIG (the documented default
    for a fresh install) to NEEDS_IMPORT, and the assertions pinning
    the structured error payload would fail in a way that read as a
    real regression but was actually env contamination.

    Tests that need the NEEDS_IMPORT branch monkeypatch the var back
    in explicitly so the choice is visible at the call site.
    """
    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)


_SEED_SQL = """
INSERT INTO records VALUES
    ('rh1', 'HKQuantityTypeIdentifierHeartRate', 72.0, NULL, 'count/min',
     'Apple Watch', '10.0', NULL, NULL,
     TIMESTAMP '2024-01-01 08:00:00', TIMESTAMP '2024-01-01 08:01:00', 'imp1');
INSERT INTO records VALUES
    ('rh2', 'HKQuantityTypeIdentifierHeartRate', 80.0, NULL, 'count/min',
     'Apple Watch', '10.0', NULL, NULL,
     TIMESTAMP '2024-01-01 09:00:00', TIMESTAMP '2024-01-01 09:01:00', 'imp1');
INSERT INTO records VALUES
    ('rh3', 'HKQuantityTypeIdentifierStepCount', 1500.0, NULL, 'count',
     'iPhone', '17.0', NULL, NULL,
     TIMESTAMP '2024-01-01 00:00:00', TIMESTAMP '2024-01-01 23:59:59', 'imp1');
INSERT INTO record_metadata VALUES
    ('rh1', 'HKMetadataKeyHeartRateMotionContext', '1');
INSERT INTO workouts VALUES
    ('wh1', 'HKWorkoutActivityTypeRunning', 1800.0, 'sec', 5000.0, 'm',
     300.0, 'kcal', 'Apple Watch', '10.0', NULL, NULL,
     TIMESTAMP '2024-01-01 10:00:00', TIMESTAMP '2024-01-01 10:30:00',
     'imp1');
INSERT INTO workout_events VALUES
    ('wh1', 'HKWorkoutEventTypeLap', TIMESTAMP '2024-01-01 10:15:00', NULL, NULL);
INSERT INTO workout_statistics VALUES
    ('wh1', 'HKQuantityTypeIdentifierHeartRate',
     TIMESTAMP '2024-01-01 10:00:00', TIMESTAMP '2024-01-01 10:30:00',
     150.0, 120.0, 180.0, NULL, 'count/min');
INSERT INTO activity_summaries VALUES
    ('2024-01-01', 500.0, 600.0, 'kcal', 45.0, 30.0, 30.0, 30.0, 10.0, 12.0, 'imp1');
INSERT INTO ecg_readings VALUES
    ('ecg1', TIMESTAMP '2024-01-01 12:00:00', 'Sinus Rhythm', 'Apple Watch',
     512.0, NULL, '2.0', 'imp1');
INSERT INTO ecg_samples VALUES ('ecg1', 0, 100.0);
INSERT INTO ecg_samples VALUES ('ecg1', 1, 200.0);
INSERT INTO ecg_samples VALUES ('ecg1', 2, -50.0);
INSERT INTO route_points VALUES
    ('rp1', 'wh1', 37.7749, -122.4194, 10.5, TIMESTAMP '2024-01-01 10:00:00',
     3.5, 180.0, 5.0, 3.0, 'imp1');
INSERT INTO route_points VALUES
    ('rp2', 'wh1', 37.7750, -122.4195, 11.0, TIMESTAMP '2024-01-01 10:00:05',
     3.6, 181.0, 4.5, 2.8, 'imp1');
INSERT INTO workout_metadata VALUES ('wh1', 'HKIndoorWorkout', '0', 'imp1');
INSERT INTO workout_metadata VALUES ('wh1', 'HKAverageMETs', '7.2 kcal/hr', 'imp1');
INSERT INTO workout_routes VALUES
    ('wh1', '/workout-routes/route_2024-01-01.gpx', 'Apple Watch', '10.0',
     NULL, TIMESTAMP '2024-01-01 10:30:00', TIMESTAMP '2024-01-01 10:00:00',
     TIMESTAMP '2024-01-01 10:30:00', 'imp1');
-- Issue #109 (PR-F): ``sample_time`` is stored DOUBLE (seconds-of-day)
-- since 00:00 local. 28800.0 / 28801.5 / 28803.0 = 08:00:00.000 /
-- 08:00:01.500 / 08:00:03.000 -- same wall-clock values as before, now
-- pre-normalised to match the post-PR-F storage shape.
INSERT INTO heart_rate_samples VALUES ('rh1', 0, 70.0, 28800.0, 'imp1');
INSERT INTO heart_rate_samples VALUES ('rh1', 1, 72.5, 28801.5, 'imp1');
INSERT INTO heart_rate_samples VALUES ('rh1', 2, 75.0, 28803.0, 'imp1');
INSERT INTO records VALUES
    ('rbp_s', 'HKQuantityTypeIdentifierBloodPressureSystolic', 130.0, NULL,
     'mmHg', 'BP', '1.0', NULL, NULL,
     TIMESTAMP '2024-01-02 07:00:00', TIMESTAMP '2024-01-02 07:00:00', 'imp1');
INSERT INTO records VALUES
    ('rbp_d', 'HKQuantityTypeIdentifierBloodPressureDiastolic', 80.0, NULL,
     'mmHg', 'BP', '1.0', NULL, NULL,
     TIMESTAMP '2024-01-02 07:00:00', TIMESTAMP '2024-01-02 07:00:00', 'imp1');
INSERT INTO correlations VALUES
    ('cor_bp', 'HKCorrelationTypeIdentifierBloodPressure', 'BP', '1.0', NULL, NULL,
     TIMESTAMP '2024-01-02 07:00:00', TIMESTAMP '2024-01-02 07:00:00', 'imp1');
INSERT INTO correlation_members VALUES ('cor_bp', 'rbp_s', 'imp1');
INSERT INTO correlation_members VALUES ('cor_bp', 'rbp_d', 'imp1');
INSERT INTO imports VALUES
    ('imp1', '/tmp/export', TIMESTAMP '2024-01-01 00:00:00', 3, 1, 5.0,
     NULL, 3, NULL, NULL, NULL);
-- StateOfMind seed (record + dedicated row) so list_state_of_mind has data
INSERT INTO records VALUES
    ('som1', 'HKCategoryTypeIdentifierStateOfMind', 0.5, NULL, NULL,
     'iPhone', '17.0', NULL, NULL,
     TIMESTAMP '2024-01-03 09:00:00', TIMESTAMP '2024-01-03 09:00:00', 'imp1');
INSERT INTO state_of_mind VALUES
    ('som1', 0.5, 'momentary', 'Joy,Calm', 'Family', 'imp1');
-- Me characteristic attributes seed (one row per import_id).
INSERT INTO me_attributes VALUES
    ('imp1', '1990-01-01', 'HKBiologicalSexNotSet', 'HKBloodTypeNotSet',
     'HKFitzpatrickSkinTypeNotSet', 'None');
"""


@pytest.fixture
def seeded_conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """In-memory DuckDB connection populated with synthetic Apple Health rows."""
    conn = get_in_memory_connection()
    ensure_schema(conn)
    # Pin the session TZ so naive TIMESTAMP seed literals land at the
    # same UTC instant regardless of the host OS local TZ. The seeds
    # below intentionally use bare ``TIMESTAMP '...'`` literals because
    # they read as "Apple Watch / iPhone wall-clock"; pinning UTC here
    # keeps that mental model deterministic across the CI matrix.
    conn.execute("SET TimeZone = 'UTC';")
    conn.execute(_SEED_SQL)
    rebuild_daily_stats(conn)
    yield conn
    conn.close()


@pytest.fixture
def empty_conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """In-memory DuckDB connection with schema only -- no rows."""
    conn = get_in_memory_connection()
    ensure_schema(conn)
    conn.execute("SET TimeZone = 'UTC';")
    rebuild_daily_stats(conn)
    yield conn
    conn.close()
