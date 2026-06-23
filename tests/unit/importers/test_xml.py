"""Tests for importers.xml.

Fixtures use synthetic dates and generic device names ("Apple Watch",
"iPhone") -- no real device UUIDs, hostnames, or personal data ever lands
in the test corpus.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest
from lxml import etree

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers.xml import (
    ImportStats,
    _parse_opt_float,
    import_xml,
)


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    # Pin the session TZ so TIMESTAMPTZ -> string casts in assertions stay
    # deterministic regardless of the host's OS local TZ.
    c.execute("SET TimeZone = 'UTC';")
    yield c
    c.close()


def _write_xml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "export.xml"
    path.write_text(body, encoding="utf-8")
    return path


# --- pure-helper tests -------------------------------------------------------


def test_parse_opt_float_valid() -> None:
    assert _parse_opt_float("72.5") == 72.5


@pytest.mark.parametrize("bad", ["", "NaN", "nan", "inf", "-inf", "Infinity", "not_a_number"])
def test_parse_opt_float_rejects_invalid_and_non_finite(bad: str) -> None:
    assert _parse_opt_float(bad) is None


def test_parse_opt_float_none() -> None:
    assert _parse_opt_float(None) is None


# --- end-to-end importer tests ----------------------------------------------


def test_import_xml_minimal_happy_path(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 12:00:00 +0000"/>
 <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01"
     HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"
     HKCharacteristicTypeIdentifierBloodType="HKBloodTypeNotSet"
     HKCharacteristicTypeIdentifierFitzpatrickSkinType="HKFitzpatrickSkinTypeNotSet"
     HKCharacteristicTypeIdentifierCardioFitnessMedicationsUse="None"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="72" startDate="2024-01-01 08:00:00 +0000" endDate="2024-01-01 08:01:00 +0000">
  <MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="1"/>
 </Record>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="100" startDate="2024-01-01 09:00:00 +0000" endDate="2024-01-01 09:30:00 +0000"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30.5" durationUnit="min" totalDistance="5.0" totalDistanceUnit="km" totalEnergyBurned="300" totalEnergyBurnedUnit="kcal" sourceName="Apple Watch" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000">
  <WorkoutEvent type="HKWorkoutEventTypeLap" date="2024-01-01 10:15:00 +0000"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000" average="150" minimum="120" maximum="180" unit="count/min"/>
  <WorkoutRoute sourceName="Apple Watch" device="Apple Watch">
   <FileReference path="/workout-routes/route_2024-01-01.gpx"/>
  </WorkoutRoute>
 </Workout>
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="BP" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="BP" unit="mmHg" value="120" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
 </Correlation>
 <ActivitySummary dateComponents="2024-01-01" activeEnergyBurned="500" activeEnergyBurnedGoal="600" activeEnergyBurnedUnit="kcal" appleExerciseTime="30" appleExerciseTimeGoal="30" appleStandHours="10" appleStandHoursGoal="12"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp1")
    assert isinstance(stats, ImportStats)
    assert stats.records == 2
    assert stats.workouts == 1
    assert stats.activity_summaries == 1
    assert stats.correlations == 1
    assert stats.correlation_members == 1
    assert stats.metadata_entries == 1
    assert stats.workout_events == 1
    assert stats.workout_statistics == 1
    assert stats.workout_routes == 1
    assert stats.export_metadata_rows == 1
    assert stats.me_rows == 1

    # Map keys: WorkoutRoute file_path -> workout_hash.
    assert "/workout-routes/route_2024-01-01.gpx" in stats.workout_route_map

    # Verify DB rows.
    row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
    assert row is not None and int(row[0]) == 2

    row = conn.execute(
        "SELECT locale, CAST(export_date AS VARCHAR) FROM export_metadata"
    ).fetchone()
    # Session TZ is UTC (see fixture); DuckDB renders TIMESTAMPTZ with the
    # ``+00`` suffix once the offset has been normalised to UTC.
    assert row == ("en_US", "2024-06-01 12:00:00+00")

    row = conn.execute("SELECT date_of_birth, biological_sex FROM me_attributes").fetchone()
    assert row == ("1990-01-01", "HKBiologicalSexNotSet")

    # WorkoutRoute.device must be preserved (the Rust version dropped it).
    row = conn.execute("SELECT device FROM workout_routes").fetchone()
    assert row == ("Apple Watch",)

    # ActivitySummary unit column is populated.
    row = conn.execute("SELECT active_energy_burned_unit FROM activity_summaries").fetchone()
    assert row == ("kcal",)


def test_import_xml_text_value_preserves_categorical(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch"
         value="HKCategoryValueSleepAnalysisAsleepDeep"
         startDate="2024-01-01 00:00:00 +0000" endDate="2024-01-01 01:00:00 +0000"/>
</HealthData>"""
    import_xml(conn, _write_xml(tmp_path, xml), "imp_cat")
    row = conn.execute("SELECT value, text_value FROM records").fetchone()
    assert row == (None, "HKCategoryValueSleepAnalysisAsleepDeep")


def test_import_xml_workout_metadata_persisted(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-02-02 06:00:00 +0000" endDate="2024-02-02 06:30:00 +0000">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <MetadataEntry key="HKAverageMETs" value="7.5 kcal/hr·kg"/>
 </Workout>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_meta")
    assert stats.workouts == 1
    assert stats.workout_metadata_entries == 2
    assert stats.metadata_entries == 0
    row = conn.execute("SELECT COUNT(*) FROM workout_metadata").fetchone()
    assert row is not None and int(row[0]) == 2


def test_import_xml_workout_route_without_file_reference_dropped(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-04-04 06:00:00 +0000" endDate="2024-04-04 06:30:00 +0000">
  <WorkoutRoute sourceName="Apple Watch"/>
 </Workout>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_no_ref")
    assert stats.workout_routes == 0


def test_import_xml_route_hash_matches_workout(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="60" durationUnit="min" sourceName="Apple Watch" startDate="2024-03-03 07:00:00 +0000" endDate="2024-03-03 08:00:00 +0000">
  <WorkoutRoute sourceName="Apple Watch">
   <FileReference path="/workout-routes/route_2024-03-03.gpx"/>
  </WorkoutRoute>
 </Workout>
</HealthData>"""
    import_xml(conn, _write_xml(tmp_path, xml), "imp_route")
    row = conn.execute(
        "SELECT w.workout_hash, wr.workout_hash FROM workouts w "
        "JOIN workout_routes wr ON wr.workout_hash = w.workout_hash"
    ).fetchone()
    assert row is not None
    assert row[0] == row[1]


def test_import_xml_instantaneous_bpm_under_hr_record(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="68" startDate="2024-02-02 08:00:00 +0000" endDate="2024-02-02 08:00:30 +0000">
  <InstantaneousBeatsPerMinute bpm="66" time="08:00:00.000"/>
  <InstantaneousBeatsPerMinute bpm="68" time="08:00:10.000"/>
  <InstantaneousBeatsPerMinute bpm="70" time="08:00:20.000"/>
 </Record>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_hr")
    assert stats.heart_rate_samples == 3
    row = conn.execute("SELECT MIN(sample_idx), MAX(sample_idx) FROM heart_rate_samples").fetchone()
    assert row == (0, 2)


def test_import_xml_hrv_metadata_list_samples(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="Apple Watch" unit="ms" value="42.5" startDate="2024-03-03 09:00:00 +0000" endDate="2024-03-03 09:01:00 +0000">
  <HeartRateVariabilityMetadataList>
   <InstantaneousBeatsPerMinute bpm="60" time="09:00:00.000"/>
   <InstantaneousBeatsPerMinute bpm="62" time="09:00:15.000"/>
  </HeartRateVariabilityMetadataList>
 </Record>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_hrv")
    assert stats.heart_rate_samples == 2


def test_import_xml_offset_bearing_workouts_normalise_to_utc(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """``+HHMM``-bearing ``startDate`` values land as their UTC equivalents.

    JST ``04:58:38 +0900`` is ``19:58:38 UTC`` on the previous day; PDT
    ``07:00:00 -0700`` is ``14:00:00 UTC``. We assert against the session-TZ
    pinned to UTC (see fixture) so the host TZ never enters the comparison.
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-06-17 04:58:38 +0900" endDate="2024-06-17 05:28:38 +0900"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="60" durationUnit="min" sourceName="Apple Watch" startDate="2024-03-03 07:00:00 -0700" endDate="2024-03-03 08:00:00 -0700"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_off")
    assert stats.workouts == 2
    rows = conn.execute(
        "SELECT activity_type, CAST(start_date AS VARCHAR) FROM workouts ORDER BY start_date"
    ).fetchall()
    assert rows == [
        ("HKWorkoutActivityTypeCycling", "2024-03-03 14:00:00+00"),
        ("HKWorkoutActivityTypeRunning", "2024-06-16 19:58:38+00"),
    ]


def test_import_xml_activity_summary_without_unit_stays_null(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ActivitySummary dateComponents="2024-02-02" activeEnergyBurned="400" appleExerciseTime="25"/>
</HealthData>"""
    import_xml(conn, _write_xml(tmp_path, xml), "imp_no_unit")
    row = conn.execute("SELECT active_energy_burned_unit FROM activity_summaries").fetchone()
    assert row == (None,)


def test_import_xml_missing_file_raises(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.xml"
    with pytest.raises(HealthImportError, match="failed to open"):
        import_xml(conn, missing, "imp_missing")


def test_import_xml_unrecoverable_syntax_error_raises(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An XMLSyntaxError raised mid-iteration is translated to HealthImportError."""
    # lxml's recover mode swallows most malformed input, so simulate the
    # unrecoverable case directly by patching iterparse to yield once and
    # then raise XMLSyntaxError on the next pull.
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US"/>"""
    path = _write_xml(tmp_path, xml)

    from apple_health_mcp.importers import xml as xml_module

    class _BrokenIter:
        def __init__(self) -> None:
            self._yielded = False

        def __iter__(self) -> _BrokenIter:
            return self

        def __next__(self) -> object:
            if not self._yielded:
                self._yielded = True
                # iterparse yields (event, element); a HealthData element
                # so the start handler runs cleanly the first time.
                from lxml.etree import Element

                return ("start", Element("HealthData"))
            raise etree.XMLSyntaxError("simulated", 0, 0, 0)

    def fake_iterparse(*_args: object, **_kwargs: object) -> _BrokenIter:
        return _BrokenIter()

    monkeypatch.setattr(xml_module.etree, "iterparse", fake_iterparse)
    with pytest.raises(HealthImportError, match="unrecoverable XML syntax error"):
        import_xml(conn, path, "imp_bad")


def test_import_xml_handler_error_does_not_abort_below_threshold(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small number of handler errors are logged and the import continues."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="10" startDate="2024-01-01 00:00:00 +0000" endDate="2024-01-01 01:00:00 +0000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="20" startDate="2024-01-01 01:00:00 +0000" endDate="2024-01-01 02:00:00 +0000"/>
</HealthData>"""

    # Monkey-patch the helper invoked from inside _handle_record so the
    # first Record start raises but the second succeeds.
    from apple_health_mcp.importers import xml as xml_module

    real_compute_hash = xml_module.compute_hash
    calls = {"n": 0}

    def flaky_hash(parts: list[str]) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic transient error")
        return real_compute_hash(parts)

    monkeypatch.setattr(xml_module, "compute_hash", flaky_hash)
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_flaky")
    # Second Record landed; first was logged and skipped.
    assert stats.records == 1


def test_import_xml_handler_error_aborts_above_threshold(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceed the consecutive-error budget and the importer must raise.

    The counter resets on any successful event (start OR end), so to trip
    the abort we need both handlers to fail. We patch ``_XmlImporter._on_end``
    to also raise, simulating a class of malformed elements that fail every
    handler call. With both events failing per Record, 51 records produce
    102 consecutive errors — above the 100 budget.
    """
    body = "\n".join(
        '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" '
        f'value="{i}" startDate="2024-01-01 00:00:00 +0000" '
        'endDate="2024-01-01 01:00:00 +0000"/>'
        for i in range(60)
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n{body}\n</HealthData>'

    from apple_health_mcp.importers import xml as xml_module

    def always_fail_hash(parts: list[str]) -> str:
        raise RuntimeError("synthetic start failure")

    def always_fail_end(self: object, elem: object) -> None:
        raise RuntimeError("synthetic end failure")

    monkeypatch.setattr(xml_module, "compute_hash", always_fail_hash)
    monkeypatch.setattr(xml_module._XmlImporter, "_on_end", always_fail_end)
    with pytest.raises(HealthImportError, match="consecutive errors"):
        import_xml(conn, _write_xml(tmp_path, xml), "imp_die")


def test_import_xml_metadata_outside_record_or_workout_ignored(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A stray MetadataEntry at the root must not crash or land a row."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <MetadataEntry key="orphan" value="ignored"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_stray")
    assert stats.metadata_entries == 0
    assert stats.workout_metadata_entries == 0


def test_import_xml_correlation_records_link_only(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A correlation child Record must not double-count into records."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="BP" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="BP" unit="mmHg" value="120" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
  <Record type="HKQuantityTypeIdentifierBloodPressureDiastolic" sourceName="BP" unit="mmHg" value="80" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
 </Correlation>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_corr")
    # Both child Records appear only in correlation_members, not records.
    assert stats.records == 0
    assert stats.correlations == 1
    assert stats.correlation_members == 2


def test_import_xml_workout_event_outside_workout_noop(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A WorkoutEvent / WorkoutStatistics / FileReference outside a Workout is silently ignored."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <WorkoutEvent type="HKWorkoutEventTypeLap" date="2024-01-01 10:15:00 +0000"/>
 <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"/>
 <FileReference path="/workout-routes/orphan.gpx"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_orphans")
    assert stats.workout_events == 0
    assert stats.workout_statistics == 0


def test_import_xml_file_reference_missing_path_attribute(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A FileReference with no path attribute leaves the route unjoinable."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-04-04 06:00:00 +0000" endDate="2024-04-04 06:30:00 +0000">
  <WorkoutRoute sourceName="Apple Watch">
   <FileReference/>
  </WorkoutRoute>
 </Workout>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_nopath")
    assert stats.workout_routes == 0


def test_import_xml_instantaneous_bpm_without_parent_record_is_noop(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """An InstantaneousBeatsPerMinute at the root without a parent Record is dropped."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <InstantaneousBeatsPerMinute bpm="60" time="00:00:00.000"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_orphan_hr")
    assert stats.heart_rate_samples == 0


def test_import_xml_batch_flush_thresholds_fire(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowering both batch thresholds to 1 forces every flush branch to fire mid-parse.

    The XML below exercises records, record_metadata, workouts, workout
    events / stats / metadata, workout routes, activities, heart-rate
    samples, correlations, and correlation members in the same document so
    every per-batch ``if len >= _BATCH_SIZE`` clause is taken at least once.
    ``records`` / ``record_metadata`` / ``heart_rate_samples`` were
    promoted to ``_BATCH_SIZE_HOT`` for #56 — patch both so the hot-table
    flush branches still execute under the test.
    """
    from apple_health_mcp.importers import xml as xml_module

    monkeypatch.setattr(xml_module, "_BATCH_SIZE", 1)
    monkeypatch.setattr(xml_module, "_BATCH_SIZE_HOT", 1)

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="72" startDate="2024-01-01 08:00:00 +0000" endDate="2024-01-01 08:01:00 +0000">
  <MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="1"/>
  <InstantaneousBeatsPerMinute bpm="70" time="08:00:00.000"/>
  <InstantaneousBeatsPerMinute bpm="71" time="08:00:01.000"/>
 </Record>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="100" startDate="2024-01-01 09:00:00 +0000" endDate="2024-01-01 09:30:00 +0000">
  <MetadataEntry key="k" value="v"/>
 </Record>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <WorkoutEvent type="HKWorkoutEventTypeLap" date="2024-01-01 10:15:00 +0000"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="150"/>
  <WorkoutRoute sourceName="Apple Watch">
   <FileReference path="/workout-routes/a.gpx"/>
  </WorkoutRoute>
 </Workout>
 <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="60" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-02 10:00:00 +0000" endDate="2024-01-02 11:00:00 +0000">
  <WorkoutRoute sourceName="Apple Watch">
   <FileReference path="/workout-routes/b.gpx"/>
  </WorkoutRoute>
 </Workout>
 <ActivitySummary dateComponents="2024-01-01" activeEnergyBurned="500"/>
 <ActivitySummary dateComponents="2024-01-02" activeEnergyBurned="600"/>
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="BP" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="BP" unit="mmHg" value="120" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
  <Record type="HKQuantityTypeIdentifierBloodPressureDiastolic" sourceName="BP" unit="mmHg" value="80" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
 </Correlation>
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="BP" startDate="2024-01-02 12:00:00 +0000" endDate="2024-01-02 12:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="BP" unit="mmHg" value="121" startDate="2024-01-02 12:00:00 +0000" endDate="2024-01-02 12:00:00 +0000"/>
 </Correlation>
</HealthData>"""

    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_batched")
    assert stats.records == 2
    assert stats.workouts == 2
    assert stats.workout_routes == 2
    assert stats.activity_summaries == 2
    assert stats.correlations == 2
    assert stats.correlation_members == 3
    assert stats.heart_rate_samples == 2
    assert stats.metadata_entries == 2
    assert stats.workout_metadata_entries == 1
    assert stats.workout_events == 1
    assert stats.workout_statistics == 1


def test_import_xml_clears_iterated_elements(tmp_path: Path) -> None:
    """Smoke check that the iterparse loop calls elem.clear() / drops siblings.

    We can't directly observe memory, but we can confirm that after a parse
    the root element has no surviving children -- which is what the
    prev-sibling cleanup is supposed to achieve.
    """
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        body = "\n".join(
            '<Record type="X" sourceName="iPhone" unit="count" value="1"'
            ' startDate="2024-01-01 00:00:00 +0000"'
            ' endDate="2024-01-01 00:00:00 +0000"/>'
            for _ in range(10)
        )
        xml = f"<?xml version='1.0'?><HealthData locale='en_US'>{body}</HealthData>"
        path = tmp_path / "export.xml"
        path.write_text(xml, encoding="utf-8")
        import_xml(conn, path, "imp_clear")
        # Parse the file ourselves to confirm the structure that was scanned
        # actually had 10 records (sanity check on the fixture).
        tree = etree.parse(str(path))
        assert len(tree.getroot().findall("Record")) == 10
    finally:
        conn.close()


def test_import_xml_export_date_without_healthdata_inserts_standalone(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If HealthData's INSERT fails (or is missing), ExportDate must still land
    a row instead of being silently dropped by a zero-row UPDATE."""
    from apple_health_mcp.importers import xml as xml_module

    # Make the HealthData handler raise so no row is inserted. The
    # consecutive-error budget absorbs the single failure.
    real_handler = xml_module._XmlImporter._handle_health_data

    def broken(self: object, elem: object) -> None:
        raise RuntimeError("synthetic HealthData failure")

    monkeypatch.setattr(xml_module._XmlImporter, "_handle_health_data", broken)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 12:00:00 +0000"/>
</HealthData>"""
    import_xml(conn, _write_xml(tmp_path, xml), "imp_no_root")
    row = conn.execute(
        "SELECT locale, CAST(export_date AS VARCHAR) FROM export_metadata WHERE import_id=?",
        ["imp_no_root"],
    ).fetchone()
    assert row == (None, "2024-06-01 12:00:00+00")
    assert any("without a preceding HealthData" in rec.message for rec in caplog.records)
    monkeypatch.setattr(xml_module._XmlImporter, "_handle_health_data", real_handler)


def test_import_xml_nested_record_in_workout_routes_metadata_to_record(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A <Record> nested under a <Workout> must route its <MetadataEntry> to
    record_metadata (inner context wins), not workout_metadata."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-01 06:00:00 +0000" endDate="2024-01-01 06:30:00 +0000">
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="150" startDate="2024-01-01 06:10:00 +0000" endDate="2024-01-01 06:10:00 +0000">
   <MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="2"/>
  </Record>
 </Workout>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_nested")
    # MetadataEntry must land in record_metadata, NOT workout_metadata.
    assert stats.metadata_entries == 1
    assert stats.workout_metadata_entries == 0
    row = conn.execute("SELECT COUNT(*) FROM record_metadata").fetchone()
    assert row is not None and int(row[0]) == 1
    row = conn.execute("SELECT COUNT(*) FROM workout_metadata").fetchone()
    assert row is not None and int(row[0]) == 0


# --- Issue #51: Phase-1 progress emitter --------------------------------------


def test_resolve_progress_interval_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apple_health_mcp.importers.xml import (
        _PROGRESS_INTERVAL_DEFAULT_SECS,
        _resolve_progress_interval,
    )

    monkeypatch.delenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", raising=False)
    assert _resolve_progress_interval() == _PROGRESS_INTERVAL_DEFAULT_SECS


def test_resolve_progress_interval_empty_string_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apple_health_mcp.importers.xml import (
        _PROGRESS_INTERVAL_DEFAULT_SECS,
        _resolve_progress_interval,
    )

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "")
    assert _resolve_progress_interval() == _PROGRESS_INTERVAL_DEFAULT_SECS


def test_resolve_progress_interval_respects_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apple_health_mcp.importers.xml import _resolve_progress_interval

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "30")
    assert _resolve_progress_interval() == 30


def test_resolve_progress_interval_clamps_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apple_health_mcp.importers.xml import (
        _PROGRESS_INTERVAL_MIN_SECS,
        _resolve_progress_interval,
    )

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "0")
    assert _resolve_progress_interval() == _PROGRESS_INTERVAL_MIN_SECS


def test_resolve_progress_interval_clamps_too_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apple_health_mcp.importers.xml import (
        _PROGRESS_INTERVAL_MAX_SECS,
        _resolve_progress_interval,
    )

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "100000")
    assert _resolve_progress_interval() == _PROGRESS_INTERVAL_MAX_SECS


def test_resolve_progress_interval_non_integer_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo at the command line warns once and falls back to the default."""
    from apple_health_mcp.importers.xml import (
        _PROGRESS_INTERVAL_DEFAULT_SECS,
        _resolve_progress_interval,
    )

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "ten")
    caplog.set_level("WARNING", logger="apple_health_mcp.importers.xml")
    assert _resolve_progress_interval() == _PROGRESS_INTERVAL_DEFAULT_SECS
    assert any("is not an integer" in r.message for r in caplog.records)


def test_progress_emitter_fires_for_large_import(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beyond the minimum-size gate, at least one progress line lands.

    Construct an XML file > 1 MB so the gate trips, pin the cadence to
    1 second (the clamped minimum) and stub ``time.monotonic`` so the
    first event-loop iteration already exceeds the interval. One INFO
    line of the documented shape must appear in caplog.
    """
    from apple_health_mcp.importers import xml as xml_module

    monkeypatch.setenv("APPLE_HEALTH_IMPORT_PROGRESS_SECS", "1")

    # Stretch the body with filler Record elements until the file goes
    # past the 1 MB gate.
    filler_record = (
        '<Record type="HKQuantityTypeIdentifierHeartRate" '
        'sourceName="Apple Watch" unit="count/min" value="72" '
        'startDate="2024-06-15 08:00:00 +0000" '
        'endDate="2024-06-15 08:01:00 +0000"/>'
    )
    # Aim well above the minimum so wrapper overhead does not undershoot.
    target_size = xml_module._PROGRESS_MIN_BYTES * 2
    count = target_size // len(filler_record) + 1
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<HealthData locale="en_US">' + (filler_record * count) + "</HealthData>"
    )
    xml_path = tmp_path / "big.xml"
    xml_path.write_text(body, encoding="utf-8")
    assert xml_path.stat().st_size >= xml_module._PROGRESS_MIN_BYTES

    # Stub monotonic so every iterparse event yields elapsed >= interval.
    ticks = iter([0.0] + [10.0] * (count * 4))
    monkeypatch.setattr(xml_module.time, "monotonic", lambda: next(ticks))

    caplog.set_level("INFO", logger="apple_health_mcp.importers.xml")
    import_xml(conn, xml_path, "imp_progress")
    progress_lines = [r.message for r in caplog.records if r.message.startswith("progress: xml")]
    assert progress_lines
    # Shape contract: percent, MB consumed/total, ETA fragment.
    sample = progress_lines[0]
    assert "%" in sample
    assert "MB" in sample
    assert "min remaining" in sample or "ETA unknown" in sample


def test_progress_emitter_suppressed_for_tiny_files(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sub-megabyte exports must emit zero progress lines.

    The CI smoke fixture is sub-second; an emitted line would be noise.
    """
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<HealthData locale="en_US">'
        '<Record type="HKQuantityTypeIdentifierHeartRate" '
        'sourceName="Apple Watch" unit="count/min" value="72" '
        'startDate="2024-06-15 08:00:00 +0000" '
        'endDate="2024-06-15 08:01:00 +0000"/>'
        "</HealthData>"
    )
    xml_path = _write_xml(tmp_path, xml)
    caplog.set_level("INFO", logger="apple_health_mcp.importers.xml")
    import_xml(conn, xml_path, "imp_tiny")
    assert not [r for r in caplog.records if r.message.startswith("progress: xml")]
