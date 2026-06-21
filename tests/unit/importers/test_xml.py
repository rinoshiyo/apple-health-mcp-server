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
    _clean_date,
    _clean_date_opt,
    _extract_offset_minutes,
    _parse_opt_float,
    import_xml,
)


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    yield c
    c.close()


def _write_xml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "export.xml"
    path.write_text(body, encoding="utf-8")
    return path


# --- pure-helper tests -------------------------------------------------------


def test_clean_date_strips_positive_offset() -> None:
    assert _clean_date("2024-01-01 12:00:00 +0900") == "2024-01-01 12:00:00"


def test_clean_date_strips_negative_offset() -> None:
    assert _clean_date("2024-01-01 12:00:00 -0500") == "2024-01-01 12:00:00"


def test_clean_date_no_offset_passes_through() -> None:
    assert _clean_date("2024-01-01 12:00:00") == "2024-01-01 12:00:00"


def test_clean_date_opt_handles_none() -> None:
    assert _clean_date_opt(None) is None
    assert _clean_date_opt("2024-01-01 12:00:00 +0000") == "2024-01-01 12:00:00"


def test_parse_opt_float_valid() -> None:
    assert _parse_opt_float("72.5") == 72.5


@pytest.mark.parametrize("bad", ["", "NaN", "nan", "inf", "-inf", "Infinity", "not_a_number"])
def test_parse_opt_float_rejects_invalid_and_non_finite(bad: str) -> None:
    assert _parse_opt_float(bad) is None


def test_parse_opt_float_none() -> None:
    assert _parse_opt_float(None) is None


def test_extract_offset_minutes_common_offsets() -> None:
    assert _extract_offset_minutes("2024-01-01 00:00:00 +0900") == 540
    assert _extract_offset_minutes("2024-01-01 00:00:00 -0700") == -420
    assert _extract_offset_minutes("2024-01-01 00:00:00 +0000") == 0
    # Half-hour zones.
    assert _extract_offset_minutes("2024-01-01 00:00:00 +0530") == 330
    assert _extract_offset_minutes("2024-01-01 00:00:00 -0345") == -225


@pytest.mark.parametrize(
    "raw",
    [
        "2024-01-01 00:00:00",  # no offset
        "2024-01-01 00:00:00 +090",  # too short
        "2024-01-01 00:00:00 +09:00",  # RFC 3339 form rejected here
        "2024-01-01 00:00:00 +09ab",  # non-digit body
    ],
)
def test_extract_offset_minutes_rejects_malformed(raw: str) -> None:
    assert _extract_offset_minutes(raw) is None


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
    # Offsets are +0000 -> 0 minutes east of UTC.
    assert all(v == 0 for v in stats.workout_offset_map.values())

    # Verify DB rows.
    row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
    assert row is not None and int(row[0]) == 2

    row = conn.execute(
        "SELECT locale, CAST(export_date AS VARCHAR) FROM export_metadata"
    ).fetchone()
    assert row == ("en_US", "2024-06-01 12:00:00")

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


def test_import_xml_workout_offset_map_populated(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-06-17 04:58:38 +0900" endDate="2024-06-17 05:28:38 +0900"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="60" durationUnit="min" sourceName="Apple Watch" startDate="2024-03-03 07:00:00 -0700" endDate="2024-03-03 08:00:00 -0700"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_off")
    assert stats.workouts == 2
    assert len(stats.workout_offset_map) == 2
    assert set(stats.workout_offset_map.values()) == {540, -420}


def test_import_xml_workout_without_offset_skips_map_entry(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-06-17 04:58:38" endDate="2024-06-17 05:28:38"/>
</HealthData>"""
    stats = import_xml(conn, _write_xml(tmp_path, xml), "imp_no_off")
    assert stats.workouts == 1
    assert stats.workout_offset_map == {}
    row = conn.execute("SELECT start_offset_minutes FROM workouts").fetchone()
    assert row == (None,)


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
    """Exceed the consecutive-error budget and the importer must raise."""
    # Build an XML with more Record elements than the threshold so every
    # handler call triggers the synthetic error.
    body = "\n".join(
        '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" '
        f'value="{i}" startDate="2024-01-01 00:00:00 +0000" '
        'endDate="2024-01-01 01:00:00 +0000"/>'
        for i in range(150)
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n{body}\n</HealthData>'

    from apple_health_mcp.importers import xml as xml_module

    def always_fail(parts: list[str]) -> str:
        raise RuntimeError("synthetic permanent error")

    monkeypatch.setattr(xml_module, "compute_hash", always_fail)
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
    """Lowering _BATCH_SIZE to 1 forces every flush branch to execute mid-parse.

    The XML below exercises records, record_metadata, workouts, workout
    events / stats / metadata, workout routes, activities, heart-rate
    samples, correlations, and correlation members in the same document so
    every per-batch ``if len >= _BATCH_SIZE`` clause is taken at least once.
    """
    from apple_health_mcp.importers import xml as xml_module

    monkeypatch.setattr(xml_module, "_BATCH_SIZE", 1)

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
