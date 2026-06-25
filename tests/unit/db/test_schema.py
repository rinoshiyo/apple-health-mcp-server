"""Tests for db.schema."""

from __future__ import annotations

from collections.abc import Generator

import duckdb
import pytest

from apple_health_mcp.db import (
    TABLE_COUNT,
    deduplicate_tables,
    ensure_schema,
    get_in_memory_connection,
    populate_workout_vestigial_columns,
    rebuild_daily_stats,
)


def _table_count(conn: duckdb.DuckDBPyConnection) -> int:
    rows = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()
    assert rows is not None
    return int(rows[0])


def _index_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = ?",
        [name],
    ).fetchone()
    return row is not None and int(row[0]) == 1


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    connection = get_in_memory_connection()
    ensure_schema(connection)
    yield connection
    connection.close()


def test_ensure_schema_creates_all_tables(conn: duckdb.DuckDBPyConnection) -> None:
    assert _table_count(conn) == TABLE_COUNT


def test_ensure_schema_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    ensure_schema(conn)
    ensure_schema(conn)
    assert _table_count(conn) == TABLE_COUNT


def test_ensure_schema_creates_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    """ensure_schema must install indexes so callers that never run dedupe
    (read-only consumers, integration dry-runs) still get an indexed DB."""
    for name in (
        "idx_records_type_date",
        "idx_records_source",
        "idx_workouts_type_date",
        "idx_route_points_workout",
        "idx_workout_metadata_hash",
        "idx_workout_routes_hash",
        "idx_heart_rate_samples_parent",
        "idx_correlations_type_date",
        "idx_correlation_members_correlation",
        "idx_correlation_members_record",
    ):
        assert _index_exists(conn, name), f"missing index {name}"


def test_new_tables_present(conn: duckdb.DuckDBPyConnection) -> None:
    for table in ("export_metadata", "me_attributes", "state_of_mind"):
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [table],
        ).fetchone()
        assert row is not None
        assert row[0] == 1, f"missing table {table}"


def test_workout_routes_has_device_column(conn: duckdb.DuckDBPyConnection) -> None:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name='workout_routes' AND column_name='device'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_deduplicate_records(conn: duckdb.DuckDBPyConnection) -> None:
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


def test_deduplicate_picks_newest_import_id(conn: duckdb.DuckDBPyConnection) -> None:
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


def test_deduplicate_workout_statistics_and_events(conn: duckdb.DuckDBPyConnection) -> None:
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


def test_deduplicate_extra_tables(conn: duckdb.DuckDBPyConnection) -> None:
    # Exercise the dedupe branches for every table we own beyond records.
    conn.execute(
        """
        INSERT INTO record_metadata VALUES
          ('h1','k','v'),('h1','k','v');
        INSERT INTO workouts VALUES
          ('wh1','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1'),
          ('wh1','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1');
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
          ('imp1','/tmp/exp','2024-01-01 06:00:00',1,1,1.0,NULL,1),
          ('imp1','/tmp/exp','2024-01-01 06:00:00',1,1,1.0,NULL,1);
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


def test_dedupe_export_metadata_prefers_newest_import(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO export_metadata VALUES
          ('imp1','2024-01-01 00:00:00','en_US'),
          ('imp2','2024-02-01 00:00:00','ja_JP');
        """
    )
    # Same import_id twice with different locales — newest export_date wins.
    conn.execute(
        """
        INSERT INTO export_metadata VALUES
          ('imp2','2024-02-01 00:00:00','ja_JP'),
          ('imp2','2024-02-02 00:00:00','fr_FR');
        """
    )
    deduplicate_tables(conn)
    rows = conn.execute(
        "SELECT import_id, locale FROM export_metadata ORDER BY import_id"
    ).fetchall()
    assert ("imp1", "en_US") in rows
    # For imp2, the later export_date wins thanks to the DESC tie-break.
    imp2_row = next(r for r in rows if r[0] == "imp2")
    assert imp2_row[1] == "fr_FR"


def test_dedupe_me_attributes_prefers_newest_import(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO me_attributes VALUES
          ('imp1','1980-01-01','HKBiologicalSexMale','HKBloodTypeAPositive',
           'HKFitzpatrickSkinTypeIII','HKCardioFitnessMedicationsUseNone'),
          ('imp2','1990-12-31','HKBiologicalSexFemale','HKBloodTypeBNegative',
           'HKFitzpatrickSkinTypeIV','HKCardioFitnessMedicationsUseSingleUse');
        """
    )
    deduplicate_tables(conn)
    row = conn.execute("SELECT date_of_birth FROM me_attributes WHERE import_id='imp2'").fetchone()
    assert row is not None
    assert row[0] == "1990-12-31"


def test_dedupe_state_of_mind_uses_deterministic_tiebreak(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Same record_hash twice within one import (replayed import) must pick
    deterministically via the valence tertiary key."""
    conn.execute(
        """
        INSERT INTO state_of_mind VALUES
          ('rh1',0.7,'Momentary','Happy','Family','imp1'),
          ('rh1',0.3,'Momentary','Calm','Work','imp1');
        """
    )
    deduplicate_tables(conn)
    rows = conn.execute(
        "SELECT valence, labels FROM state_of_mind WHERE record_hash='rh1'"
    ).fetchall()
    assert len(rows) == 1
    # The lower valence wins because ORDER BY ... valence is ASC.
    assert rows[0][0] == pytest.approx(0.3)
    assert rows[0][1] == "Calm"


def test_populate_vestigial_columns(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_run','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1');
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


def test_populate_vestigial_columns_skips_mixed_unit_distance(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A workout with multiple distance units (e.g. swim metres + cycle km)
    must NOT receive a backfilled total_distance, because summing across
    incommensurable units would silently store a nonsense value."""
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_tri','HKWorkoutActivityTypeSwimBikeRun',7200,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 08:00:00','imp1');
        INSERT INTO workout_statistics VALUES
          ('wh_tri','HKQuantityTypeIdentifierDistanceSwimming',
           '2024-01-01 06:00:00','2024-01-01 06:30:00',NULL,NULL,NULL,1500.0,'m'),
          ('wh_tri','HKQuantityTypeIdentifierDistanceCycling',
           '2024-01-01 06:30:00','2024-01-01 07:30:00',NULL,NULL,NULL,40.0,'km'),
          ('wh_tri','HKQuantityTypeIdentifierDistanceWalkingRunning',
           '2024-01-01 07:30:00','2024-01-01 08:00:00',NULL,NULL,NULL,10.0,'km');
        """
    )
    populate_workout_vestigial_columns(conn)
    row = conn.execute(
        "SELECT total_distance, total_distance_unit FROM workouts WHERE workout_hash='wh_tri'"
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_populate_vestigial_columns_preserves_legacy_values(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_legacy','HKWorkoutActivityTypeRunning',1800,'s',
           5.0,'mi',300.0,'kcal','Watch','10','iPhone',
           '2020-01-01 06:00:00','2020-01-01 06:00:00','2020-01-01 06:30:00','imp1');
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


def test_dedup_then_vestigial_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO workouts VALUES
          ('wh_run','HKWorkoutActivityTypeRunning',1800,'s',
           NULL,NULL,NULL,NULL,'Watch','11','iPhone',
           '2024-01-01 06:00:00','2024-01-01 06:00:00','2024-01-01 06:30:00','imp1');
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


# Source-schema-declared NOT NULL columns per table, derived directly from
# ``_CREATE_TABLES_SQL``. This duplicates the SQL on purpose: the test fails
# loudly if anyone adds/removes a NOT NULL declaration without updating the
# ``_RESTORE_CONSTRAINTS_SQL`` block. Coupled, not generated.
_EXPECTED_NOT_NULL: dict[str, frozenset[str]] = {
    "records": frozenset({"record_type", "start_date", "end_date", "import_id"}),
    "record_metadata": frozenset({"record_hash", "key"}),
    "workouts": frozenset({"activity_type", "start_date", "end_date", "import_id"}),
    "workout_events": frozenset({"workout_hash", "event_type"}),
    "workout_statistics": frozenset({"workout_hash", "stat_type"}),
    "activity_summaries": frozenset({"import_id"}),
    "ecg_readings": frozenset({"recorded_date", "import_id"}),
    "ecg_samples": frozenset({"ecg_hash", "sample_idx", "voltage_uv"}),
    "route_points": frozenset({"latitude", "longitude", "timestamp", "import_id"}),
    "workout_metadata": frozenset({"workout_hash", "key", "import_id"}),
    "workout_routes": frozenset({"workout_hash", "file_path", "import_id"}),
    "heart_rate_samples": frozenset({"parent_record_hash", "sample_idx", "import_id"}),
    "correlations": frozenset(
        {"correlation_hash", "correlation_type", "start_date", "end_date", "import_id"}
    ),
    "correlation_members": frozenset({"correlation_hash", "record_hash", "import_id"}),
    "imports": frozenset({"export_dir", "imported_at"}),
    "export_metadata": frozenset({"import_id"}),
    "me_attributes": frozenset({"import_id"}),
    "state_of_mind": frozenset({"record_hash", "import_id"}),
}


def _pragma_table_info(
    conn: duckdb.DuckDBPyConnection, table: str
) -> list[tuple[str, bool, str | None]]:
    """Return ``(column_name, not_null, dflt_value)`` triples for ``table``.

    ``PRAGMA table_info`` columns are ``cid, name, type, notnull, dflt_value,
    pk``. The connection-mode equivalent ``duckdb_columns()`` would also
    work but PRAGMA matches what the issue spec references.
    """
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [(str(r[1]), bool(r[3]), None if r[4] is None else str(r[4])) for r in rows]


def test_deduplicate_restores_not_null_constraints(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """After ``deduplicate_tables`` the schema's NOT NULL columns are
    restored on every dedup-target table.

    ``CREATE OR REPLACE TABLE ... AS SELECT ...`` in DuckDB strips column
    constraints. This regression test pins ``_RESTORE_CONSTRAINTS_SQL`` to
    the source-schema declaration so a future schema change cannot silently
    let the post-dedup tables drift away from their contract.
    """
    deduplicate_tables(conn)
    for table, expected in _EXPECTED_NOT_NULL.items():
        info = _pragma_table_info(conn, table)
        observed_not_null = {name for name, not_null, _ in info if not_null}
        assert observed_not_null == expected, (
            f"{table}: NOT NULL set drifted; "
            f"expected={sorted(expected)} observed={sorted(observed_not_null)}"
        )


def test_deduplicate_restores_imports_imported_at_default(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``imports.imported_at`` keeps its ``CURRENT_TIMESTAMP`` default so
    the orchestrator INSERT can keep omitting the column."""
    deduplicate_tables(conn)
    info = _pragma_table_info(conn, "imports")
    dflt = next(d for name, _, d in info if name == "imported_at")
    assert dflt is not None
    assert "CURRENT_TIMESTAMP" in dflt.upper()

    # Functional check: omit imported_at on INSERT and confirm the default
    # actually fires (PRAGMA dflt_value is metadata; the runtime behaviour
    # is the contract callers depend on).
    conn.execute("INSERT INTO imports (import_id, export_dir) VALUES ('imp_x', '/tmp')")
    row = conn.execute("SELECT imported_at FROM imports WHERE import_id='imp_x'").fetchone()
    assert row is not None
    assert row[0] is not None


def test_legacy_schema_needs_constraint_repair_false_on_fresh_schema(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A freshly ``ensure_schema``-ed DB already has the NOT NULL on
    ``imports.imported_at`` so the gate returns False and the one-shot
    constraint-repair block stays skipped (DuckDB would raise
    ``DependencyException`` against the indexes otherwise).
    """
    from apple_health_mcp.db.schema import _legacy_schema_needs_constraint_repair

    assert _legacy_schema_needs_constraint_repair(conn) is False


def test_legacy_schema_needs_constraint_repair_true_when_imported_at_nullable(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Simulate a pre-#44 finalize by dropping the NOT NULL flag on
    ``imports.imported_at`` and assert the gate flips to True so the
    one-shot ``_RESTORE_CONSTRAINTS_SQL`` migration would fire on the
    next ``deduplicate_tables`` call. PRAGMA ``notnull`` is the proxy
    column the gate inspects.
    """
    from apple_health_mcp.db.schema import _legacy_schema_needs_constraint_repair

    conn.execute("ALTER TABLE imports ALTER COLUMN imported_at DROP NOT NULL")
    assert _legacy_schema_needs_constraint_repair(conn) is True


def test_rebuild_daily_stats(conn: duckdb.DuckDBPyConnection) -> None:
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
