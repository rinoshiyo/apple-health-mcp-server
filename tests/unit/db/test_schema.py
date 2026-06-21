"""Tests for db.schema."""

from __future__ import annotations

import pytest

from apple_health_mcp.db import (
    TABLE_COUNT,
    deduplicate_tables,
    ensure_schema,
    get_in_memory_connection,
    populate_workout_vestigial_columns,
    rebuild_daily_stats,
)


def _table_count(conn: object) -> int:
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()
    assert rows is not None
    return int(rows[0])


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    connection = get_in_memory_connection()
    ensure_schema(connection)
    yield connection
    connection.close()


def test_ensure_schema_creates_all_tables(conn) -> None:  # type: ignore[no-untyped-def]
    assert _table_count(conn) == TABLE_COUNT


def test_ensure_schema_is_idempotent(conn) -> None:  # type: ignore[no-untyped-def]
    ensure_schema(conn)
    ensure_schema(conn)
    assert _table_count(conn) == TABLE_COUNT


def test_new_tables_present(conn) -> None:  # type: ignore[no-untyped-def]
    for table in ("export_metadata", "me_attributes", "state_of_mind"):
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [table],
        ).fetchone()
        assert row is not None
        assert row[0] == 1, f"missing table {table}"


def test_workout_routes_has_device_column(conn) -> None:  # type: ignore[no-untyped-def]
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name='workout_routes' AND column_name='device'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_deduplicate_records(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO records VALUES
          ('h1','HeartRate',72.0,NULL,'count/min','Watch','1.0',NULL,
           '2024-01-01 00:00:00','2024-01-01 00:00:00','2024-01-01 00:01:00','imp1'),
          ('h1','HeartRate',72.0,NULL,'count/min','Watch','1.0',NULL,
           '2024-01-01 00:00:00','2024-01-01 00:00:00','2024-01-01 00:01:00','imp1'),
          ('h2','StepCount',100.0,NULL,'count','Phone','1.0',NULL,
           '2024-01-01 00:00:00','2024-01-01 00:00:00','2024-01-01 00:01:00','imp1');
        """
    )
    deduplicate_tables(conn)
    row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
    assert row is not None
    assert row[0] == 2


def test_deduplicate_picks_newest_import_id(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO records VALUES
          ('h1','HeartRate',72.0,NULL,'count/min','Watch','1.0',NULL,
           '2024-01-01 00:00:00','2024-01-01 00:00:00','2024-01-01 00:01:00','imp1'),
          ('h1','HeartRate',72.0,NULL,'count/min','Watch','2.0',NULL,
           '2024-01-01 00:00:00','2024-01-01 00:00:00','2024-01-01 00:01:00','imp2');
        """
    )
    deduplicate_tables(conn)
    row = conn.execute("SELECT source_version FROM records").fetchone()
    assert row is not None
    assert row[0] == "2.0"


def test_deduplicate_workout_statistics_and_events(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO workout_statistics VALUES
          ('wh1','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2024-01-01 10:00:00','2024-01-01 10:30:00',NULL,NULL,NULL,300.0,'kcal'),
          ('wh1','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2024-01-01 10:00:00','2024-01-01 10:30:00',NULL,NULL,NULL,300.0,'kcal');
        INSERT INTO workout_events VALUES
          ('wh1','HKWorkoutEventTypeLap','2024-01-01 10:15:00',NULL,NULL),
          ('wh1','HKWorkoutEventTypeLap','2024-01-01 10:15:00',NULL,NULL),
          ('wh1','HKWorkoutEventTypePause','2024-01-01 10:20:00',5.0,'min');
        """
    )
    deduplicate_tables(conn)
    stats_row = conn.execute("SELECT COUNT(*) FROM workout_statistics").fetchone()
    assert stats_row is not None
    assert stats_row[0] == 1
    events_row = conn.execute("SELECT COUNT(*) FROM workout_events").fetchone()
    assert events_row is not None
    assert events_row[0] == 2


def test_deduplicate_extra_tables(conn) -> None:  # type: ignore[no-untyped-def]
    # Exercise the dedupe branches for every table we own beyond records.
    conn.execute(
        """
        INSERT INTO record_metadata VALUES
          ('h1','k','v'),('h1','k','v');
        INSERT INTO workouts VALUES
          ('wh1','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,'imp1'),
          ('wh1','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,'imp1');
        INSERT INTO activity_summaries VALUES
          ('2024-01-01',100,500,'kcal',30,30,30,30,12,12,'imp1'),
          ('2024-01-01',100,500,'kcal',30,30,30,30,12,12,'imp1');
        INSERT INTO ecg_readings VALUES
          ('e1','2024-01-01 00:00:00','Sinus','Watch',512.0,NULL,'10.4','imp1'),
          ('e1','2024-01-01 00:00:00','Sinus','Watch',512.0,NULL,'10.4','imp1');
        INSERT INTO ecg_samples VALUES
          ('e1',0,0.0),('e1',0,0.0);
        INSERT INTO route_points VALUES
          ('p1','wh1',35.0,135.0,10.0,'2024-01-01 06:00:00',1.0,90.0,5.0,5.0,'imp1'),
          ('p1','wh1',35.0,135.0,10.0,'2024-01-01 06:00:00',1.0,90.0,5.0,5.0,'imp1');
        INSERT INTO workout_metadata VALUES
          ('wh1','k','v','imp1'),('wh1','k','v','imp1');
        INSERT INTO workout_routes VALUES
          ('wh1','/workout-routes/r1.gpx','Workouts','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1'),
          ('wh1','/workout-routes/r1.gpx','Workouts','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1');
        INSERT INTO heart_rate_samples VALUES
          ('h1',0,72.0,'10','imp1'),('h1',0,72.0,'10','imp1');
        INSERT INTO correlations VALUES
          ('c1','HKCorrelationTypeIdentifierBloodPressure','iPhone','11',NULL,
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:01:00','imp1'),
          ('c1','HKCorrelationTypeIdentifierBloodPressure','iPhone','11',NULL,
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:01:00','imp1');
        INSERT INTO correlation_members VALUES
          ('c1','h1','imp1'),('c1','h1','imp1');
        INSERT INTO imports VALUES
          ('imp1','/tmp/exp','2024-01-01 06:00:00',1,1,1.0),
          ('imp1','/tmp/exp','2024-01-01 06:00:00',1,1,1.0);
        INSERT INTO export_metadata VALUES
          ('imp1','2024-01-01 06:00:00','ja_JP'),
          ('imp1','2024-01-01 06:00:00','ja_JP');
        INSERT INTO me_attributes VALUES
          ('imp1','1990-01-01','HKBiologicalSexMale','HKBloodTypeAPositive',
           'HKFitzpatrickSkinTypeIII','HKCardioFitnessMedicationsUseNone'),
          ('imp1','1990-01-01','HKBiologicalSexMale','HKBloodTypeAPositive',
           'HKFitzpatrickSkinTypeIII','HKCardioFitnessMedicationsUseNone');
        INSERT INTO state_of_mind VALUES
          ('rh1',0.5,'Momentary','Happy','Family','imp1'),
          ('rh1',0.5,'Momentary','Happy','Family','imp1');
        """
    )
    deduplicate_tables(conn)
    expected = {
        "record_metadata": 1,
        "workouts": 1,
        "activity_summaries": 1,
        "ecg_readings": 1,
        "ecg_samples": 1,
        "route_points": 1,
        "workout_metadata": 1,
        "workout_routes": 1,
        "heart_rate_samples": 1,
        "correlations": 1,
        "correlation_members": 1,
        "imports": 1,
        "export_metadata": 1,
        "me_attributes": 1,
        "state_of_mind": 1,
    }
    for table, want in expected.items():
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert row is not None
        assert row[0] == want, f"{table} expected {want} got {row[0]}"


def test_populate_vestigial_columns(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_run','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,'imp1');
        INSERT INTO workout_statistics VALUES
          ('wh_run','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,240.5,'kcal'),
          ('wh_run','HKQuantityTypeIdentifierDistanceWalkingRunning',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,3.2,'km');
        """
    )
    populate_workout_vestigial_columns(conn)
    row = conn.execute(
        "SELECT total_energy_burned, total_energy_unit, "
        "total_distance, total_distance_unit FROM workouts"
    ).fetchone()
    assert row is not None
    energy, energy_unit, distance, distance_unit = row
    assert energy == pytest.approx(240.5)
    assert energy_unit == "kcal"
    assert distance == pytest.approx(3.2)
    assert distance_unit == "km"


def test_populate_vestigial_columns_preserves_legacy_values(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_legacy','HKWorkoutActivityTypeRunning',1800,'s',
           5.0,'mi',300.0,'kcal','Watch','10','iPhone',
           '2020-01-01 06:00:00','2020-01-01 06:00:00','2020-01-01 06:30:00',NULL,'imp1');
        INSERT INTO workout_statistics VALUES
          ('wh_legacy','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2020-01-01 06:00:00','2020-01-01 06:30:00',NULL,NULL,NULL,999.0,'kcal'),
          ('wh_legacy','HKQuantityTypeIdentifierDistanceWalkingRunning',
           '2020-01-01 06:00:00','2020-01-01 06:30:00',NULL,NULL,NULL,99.0,'km');
        """
    )
    populate_workout_vestigial_columns(conn)
    row = conn.execute(
        "SELECT total_energy_burned, total_distance, total_distance_unit FROM workouts"
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(300.0)
    assert row[1] == pytest.approx(5.0)
    assert row[2] == "mi"


def test_dedup_then_vestigial_is_idempotent(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_run','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,'imp1');
        INSERT INTO workout_statistics VALUES
          ('wh_run','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,240.5,'kcal'),
          ('wh_run','HKQuantityTypeIdentifierActiveEnergyBurned',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,240.5,'kcal'),
          ('wh_run','HKQuantityTypeIdentifierDistanceWalkingRunning',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,3.2,'km'),
          ('wh_run','HKQuantityTypeIdentifierDistanceWalkingRunning',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,3.2,'km');
        """
    )
    deduplicate_tables(conn)
    populate_workout_vestigial_columns(conn)
    row = conn.execute("SELECT total_energy_burned, total_distance FROM workouts").fetchone()
    assert row is not None
    assert row[0] == pytest.approx(240.5)
    assert row[1] == pytest.approx(3.2)


def test_rebuild_daily_stats(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        """
        INSERT INTO records VALUES
          ('h1','HeartRate',72.0,NULL,'count/min','Watch',NULL,NULL,NULL,
           '2024-01-01 08:00:00','2024-01-01 08:01:00','imp1'),
          ('h2','HeartRate',80.0,NULL,'count/min','Watch',NULL,NULL,NULL,
           '2024-01-01 09:00:00','2024-01-01 09:01:00','imp1'),
          ('h3','HeartRate',65.0,NULL,'count/min','Watch',NULL,NULL,NULL,
           '2024-01-02 08:00:00','2024-01-02 08:01:00','imp1');
        """
    )
    rebuild_daily_stats(conn)
    row = conn.execute("SELECT COUNT(*) FROM daily_record_stats").fetchone()
    assert row is not None
    assert row[0] == 2
    avg = conn.execute(
        "SELECT avg_value FROM daily_record_stats WHERE date = '2024-01-01'"
    ).fetchone()
    assert avg is not None
    assert avg[0] == pytest.approx(76.0)
