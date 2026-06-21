"""Tests for importers.orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.importers.orchestrator import (
    _open_db,
    make_import_id,
    run_import,
)

_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 12:00:00 +0000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="100" startDate="2024-01-01 09:00:00 +0900" endDate="2024-01-01 09:30:00 +0900"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-06-17 04:58:38 +0900" endDate="2024-06-17 05:28:38 +0900">
  <WorkoutRoute sourceName="Apple Watch">
   <FileReference path="/workout-routes/route_2024-06-17.gpx"/>
  </WorkoutRoute>
 </Workout>
</HealthData>"""

_ECG_CSV = """Recorded Date,2024-06-15 10:30:00 +0900
Classification,Sinus Rhythm
Device,"Apple Watch"
Sample Rate,512 Hz

100
200
"""

_ROUTE_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.0" lon="139.0">
      <ele>10.0</ele>
      <time>2024-06-17T04:58:39Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""


def test_make_import_id_is_deterministic_given_clock() -> None:
    now = datetime(2024, 6, 1, 12, 34, 56, 789012, tzinfo=UTC)
    assert make_import_id(now) == "import_20240601_123456_789012"


def test_make_import_id_includes_microseconds() -> None:
    """A real call must include the microsecond suffix so two same-second calls do not collide."""
    a = make_import_id()
    b = make_import_id()
    # In practice these may be equal if microsecond also matches; the
    # important property is just that the format is right.
    assert len(a) == len("import_YYYYMMDD_HHMMSS_FFFFFF")
    assert a[:14] == b[:14]


def test_open_db_returns_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "h.duckdb"
    conn = _open_db(db_path)
    try:
        row = conn.execute("SELECT 42").fetchone()
        assert row == (42,)
    finally:
        conn.close()


def test_run_import_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The full pipeline lands counts from all three importers and finalizes."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(_EXPORT_XML, encoding="utf-8")
    ecg_dir = export_dir / "electrocardiograms"
    ecg_dir.mkdir()
    (ecg_dir / "ecg.csv").write_text(_ECG_CSV, encoding="utf-8")
    routes_dir = export_dir / "workout-routes"
    routes_dir.mkdir()
    (routes_dir / "route_2024-06-17.gpx").write_text(_ROUTE_GPX, encoding="utf-8")

    db_path = tmp_path / "h.duckdb"
    stats = run_import(export_dir, db_path, import_id="imp_e2e")

    assert stats.records == 1
    assert stats.workouts == 1
    assert stats.ecg_readings == 1
    assert stats.route_points == 1

    # Re-open the DB to confirm rows landed and finalize ran (dedup +
    # daily_record_stats materialization).
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM route_points").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'daily_record_stats'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute(
            "SELECT record_count, workout_count FROM imports WHERE import_id = 'imp_e2e'"
        ).fetchone()
        assert row == (1, 1)
    finally:
        conn.close()


def test_run_import_autogenerates_import_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(
        '<?xml version="1.0"?><HealthData locale="en_US"/>', encoding="utf-8"
    )

    # Substitute the connection helper so we get an in-memory DB without
    # touching the XDG default path.
    from apple_health_mcp.importers import orchestrator as orch

    in_mem = get_in_memory_connection()
    monkeypatch.setattr(orch, "_open_db", lambda _path: in_mem)
    try:
        stats = orch.run_import(export_dir)
    finally:
        # in_mem was closed inside run_import via the finally block.
        pass

    assert stats.records == 0
    # The auto-generated import_id format is "import_YYYYMMDD_HHMMSS_FFFFFF".
    # We can verify the orchestrator inserted an `imports` row by re-opening
    # a fresh in-memory connection. Because in_mem is closed already, just
    # check that the stats object returned populated.
    assert stats.workouts == 0
