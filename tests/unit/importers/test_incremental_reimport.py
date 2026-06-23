"""Tests for the issue #62 incremental re-import paths.

Covers the two tiers together because they are intentionally complementary:

* **Tier 1** -- export.xml sha256 fast path on the orchestrator entry.
* **Tier 2** -- in-memory ``ExistingHashes`` threaded into every importer
  handler so re-importing an export that mostly overlaps with what is on
  disk contributes only the genuinely-new rows, AND ``finalize_import``
  skips ``deduplicate_tables`` when the snapshot was active (so the
  DuckDB MVCC tombstone balloon described in the issue body disappears).

The legacy ``--force`` path -- sha256 ignored, no snapshot, Phase 4
dedup runs -- is exercised under :func:`test_force_runs_legacy_dedup`
and :func:`test_force_bypasses_sha256_fast_path` so the regression
guard for the v0.1.5 behaviour stays explicit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from apple_health_mcp import cli
from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.db.migrations import (
    _add_export_xml_sha256_column,
    apply_pending_migrations,
)
from apple_health_mcp.importers import run_import
from apple_health_mcp.importers._existing_hashes import (
    ExistingHashes,
    load_existing_hashes,
)
from apple_health_mcp.importers.dedup import finalize_import
from apple_health_mcp.importers.ecg import import_single_ecg
from apple_health_mcp.importers.gpx import import_single_gpx
from apple_health_mcp.importers.orchestrator import (
    _compute_file_sha256,
    _has_prior_imports,
    _sha256_matches_prior,
)

# ----------------------------------------------------------------------------
# Synthetic export fixtures.
# ----------------------------------------------------------------------------
#
# Each test materialises its own ``export.xml`` (+ optional ECG / GPX
# siblings) under ``tmp_path``. The base body below carries one record,
# one workout with one route reference, one correlation member, and one
# activity summary -- enough to exercise every dedup-keyed handler in
# both fresh and incremental modes.


_BASE_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 12:00:00 +0000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="100" startDate="2024-01-01 09:00:00 +0900" endDate="2024-01-01 09:30:00 +0900">
  <MetadataEntry key="HKMetadataKeyTimeZone" value="Asia/Tokyo"/>
 </Record>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-06-17 04:58:38 +0900" endDate="2024-06-17 05:28:38 +0900">
  <WorkoutEvent type="HKWorkoutEventTypeLap" date="2024-06-17 05:00:00 +0900"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2024-06-17 04:58:38 +0900" endDate="2024-06-17 05:28:38 +0900" average="150" minimum="120" maximum="180" unit="count/min"/>
  <MetadataEntry key="HKWeatherTemperature" value="20"/>
  <WorkoutRoute sourceName="Apple Watch" device="Apple Watch">
   <FileReference path="/workout-routes/route_2024-06-17.gpx"/>
  </WorkoutRoute>
 </Workout>
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="BP" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="BP" unit="mmHg" value="120" startDate="2024-01-01 12:00:00 +0000" endDate="2024-01-01 12:00:00 +0000"/>
 </Correlation>
 <ActivitySummary dateComponents="2024-06-17" activeEnergyBurned="500" activeEnergyBurnedGoal="600" activeEnergyBurnedUnit="kcal" appleExerciseTime="30" appleExerciseTimeGoal="30" appleStandHours="10" appleStandHoursGoal="12"/>
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
    <trkpt lat="35.0001" lon="139.0001">
      <ele>11.0</ele>
      <time>2024-06-17T04:58:40Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""


def _materialise_export(tmp_path: Path, xml_body: str = _BASE_EXPORT_XML) -> Path:
    """Lay out a minimal valid Apple Health export directory under ``tmp_path``."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(xml_body, encoding="utf-8")
    ecg_dir = export_dir / "electrocardiograms"
    ecg_dir.mkdir()
    (ecg_dir / "ecg.csv").write_text(_ECG_CSV, encoding="utf-8")
    routes_dir = export_dir / "workout-routes"
    routes_dir.mkdir()
    (routes_dir / "route_2024-06-17.gpx").write_text(_ROUTE_GPX, encoding="utf-8")
    return export_dir


# ----------------------------------------------------------------------------
# ExistingHashes loader.
# ----------------------------------------------------------------------------


@pytest.fixture
def fresh_conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    yield c
    c.close()


def test_load_existing_hashes_on_fresh_db_returns_empty_sets(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    snapshot = load_existing_hashes(fresh_conn)
    assert isinstance(snapshot, ExistingHashes)
    assert snapshot.records == set()
    assert snapshot.workouts == set()
    assert snapshot.route_points == set()
    assert snapshot.ecg_readings == set()
    assert snapshot.correlations == set()
    assert snapshot.activity_summaries == set()


def test_load_existing_hashes_populates_each_set_from_disk(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Seed every dedup-keyed table once and confirm the snapshot picks up the hashes."""
    fresh_conn.execute(
        "INSERT INTO records (record_hash, record_type, start_date, end_date, import_id)"
        " VALUES ('rec_h', 't', '2024-01-01', '2024-01-01', 'imp')"
    )
    fresh_conn.execute(
        "INSERT INTO workouts (workout_hash, activity_type, start_date, end_date, import_id)"
        " VALUES ('wo_h', 't', '2024-01-01', '2024-01-01', 'imp')"
    )
    fresh_conn.execute(
        "INSERT INTO route_points "
        "(point_hash, workout_hash, latitude, longitude, timestamp, import_id) "
        "VALUES ('p_h', 'wo_h', 35.0, 139.0, '2024-01-01', 'imp')"
    )
    fresh_conn.execute(
        "INSERT INTO ecg_readings (ecg_hash, recorded_date, import_id)"
        " VALUES ('ecg_h', '2024-01-01', 'imp')"
    )
    fresh_conn.execute(
        "INSERT INTO correlations "
        "(correlation_hash, correlation_type, start_date, end_date, import_id) "
        "VALUES ('cor_h', 't', '2024-01-01', '2024-01-01', 'imp')"
    )
    fresh_conn.execute(
        "INSERT INTO activity_summaries (date_components, import_id) VALUES ('2024-01-01', 'imp')"
    )

    snapshot = load_existing_hashes(fresh_conn)
    assert snapshot.records == {"rec_h"}
    assert snapshot.workouts == {"wo_h"}
    assert snapshot.route_points == {"p_h"}
    assert snapshot.ecg_readings == {"ecg_h"}
    assert snapshot.correlations == {"cor_h"}
    assert snapshot.activity_summaries == {"2024-01-01"}


# ----------------------------------------------------------------------------
# finalize_import skip_dedup branch.
# ----------------------------------------------------------------------------


def test_finalize_import_skip_dedup_keeps_duplicates(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    """``skip_dedup=True`` runs backfill + daily stats but leaves duplicates alone.

    Duplicates here are synthetic -- the orchestrator never lets the Tier 2
    path enter ``finalize_import`` with duplicates in the bulk staging
    buffers because every handler short-circuits on a hit. The point of
    this test is the ``skip_dedup`` branch itself: we seed two identical
    rows by hand and confirm the dedup pass did NOT run.
    """
    for _ in range(2):
        fresh_conn.execute(
            "INSERT INTO records (record_hash, record_type, start_date, end_date, import_id)"
            " VALUES (?, ?, ?, ?, ?)",
            ["h_dup", "HKQuantityTypeIdentifierStepCount", "2024-01-01", "2024-01-01", "imp"],
        )
    finalize_import(fresh_conn, skip_dedup=True)
    # Both rows survive because dedup did not fire.
    row = fresh_conn.execute("SELECT COUNT(*) FROM records").fetchone()
    assert row is not None and int(row[0]) == 2


# ----------------------------------------------------------------------------
# orchestrator helper functions.
# ----------------------------------------------------------------------------


def test_compute_file_sha256_returns_known_digest(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    payload = b"abc123"
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert _compute_file_sha256(path) == expected


def test_compute_file_sha256_missing_file_returns_none(tmp_path: Path) -> None:
    """A non-existent file returns ``None`` so the Tier 1 fast path falls through."""
    assert _compute_file_sha256(tmp_path / "absent.xml") is None


def test_compute_file_sha256_non_missing_oserror_logs_warning_and_returns_none(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-FileNotFoundError OSError (e.g. mid-read EIO) is logged so it does not silently stamp NULL.

    Regression guard for the code-review finding: ``_compute_file_sha256``
    used to swallow every OSError silently, so a flaky disk that hit
    EIO mid-read produced a NULL ``imports.export_xml_sha256`` row with
    no log trace, masquerading as a perf regression on the next
    re-import (NULL row never matches the Tier 1 fast path).
    """
    import logging as _logging

    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")

    real_open = Path.open

    def boom(
        self: Path, *args: object, **kwargs: object
    ) -> object:  # pragma: no cover - signature placeholder
        if self == path:
            raise PermissionError("simulated permission denied")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", boom)
    caplog.set_level(_logging.WARNING, logger="apple_health_mcp.importers.orchestrator")
    result = _compute_file_sha256(path)
    assert result is None
    assert any(
        "failed to compute sha256" in rec.message and "Tier 1" in rec.message
        for rec in caplog.records
    )


def test_sha256_matches_prior_returns_false_on_empty_imports_table(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    assert _sha256_matches_prior(fresh_conn, "anything") is False


def test_sha256_matches_prior_ignores_null_rows(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Pre-#62 rows (NULL sha256) must not be matched against."""
    fresh_conn.execute(
        "INSERT INTO imports (import_id, export_dir, export_xml_sha256) "
        "VALUES ('legacy', '/tmp/x', NULL)"
    )
    assert _sha256_matches_prior(fresh_conn, "abc") is False


def test_sha256_matches_prior_returns_true_on_byte_identical(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    sha = "a" * 64
    fresh_conn.execute(
        "INSERT INTO imports (import_id, export_dir, export_xml_sha256) VALUES ('p', '/tmp/p', ?)",
        [sha],
    )
    assert _sha256_matches_prior(fresh_conn, sha) is True


def test_has_prior_imports_distinguishes_empty_from_seeded(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    assert _has_prior_imports(fresh_conn) is False
    fresh_conn.execute("INSERT INTO imports (import_id, export_dir) VALUES ('p', '/tmp/p')")
    assert _has_prior_imports(fresh_conn) is True


# ----------------------------------------------------------------------------
# Migration upgrade path.
# ----------------------------------------------------------------------------


def test_migration_adds_export_xml_sha256_to_pre_62_db(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A pre-#62 schema (no sha256 column) gets the column via the migration."""
    fresh_conn.execute("DROP TABLE imports")
    fresh_conn.execute(
        """
        CREATE TABLE imports (
            import_id     VARCHAR,
            export_dir    VARCHAR NOT NULL,
            imported_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            record_count  BIGINT,
            workout_count BIGINT,
            duration_secs DOUBLE
        );
        """
    )
    fresh_conn.execute(
        "INSERT INTO imports (import_id, export_dir) VALUES ('legacy', '/tmp/legacy')"
    )

    _add_export_xml_sha256_column(fresh_conn)
    # Column now exists; existing row backfilled to NULL.
    row = fresh_conn.execute(
        "SELECT export_xml_sha256 FROM imports WHERE import_id = 'legacy'"
    ).fetchone()
    assert row == (None,)


def test_migration_noop_on_post_62_db(
    fresh_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Already-current schemas survive the idempotent ALTER without error."""
    # The fixture ran ensure_schema, so the column already exists.
    _add_export_xml_sha256_column(fresh_conn)
    # Second invocation must also be safe.
    _add_export_xml_sha256_column(fresh_conn)


def test_migration_noop_when_imports_table_absent() -> None:
    """An ALTER on a missing table would crash; the guard turns it into a no-op.

    apply_pending_migrations is callable on a freshly-opened connection
    that has not yet run ``ensure_schema``; the version sentinel only
    needs the ``schema_version`` table. The migration's empty-DB guard
    keeps that contract intact.
    """
    c = get_in_memory_connection()
    try:
        _add_export_xml_sha256_column(c)  # no exception
        # apply_pending_migrations still stamps the version sentinel.
        result = apply_pending_migrations(c)
        assert result >= 2
    finally:
        c.close()


# ----------------------------------------------------------------------------
# End-to-end orchestrator paths.
# ----------------------------------------------------------------------------


def test_fresh_import_stamps_sha256_in_imports_row(tmp_path: Path) -> None:
    """A first import records the export.xml sha256 for future fast-path checks."""
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    stats = run_import(export_dir, db, import_id="imp_first")
    # Real data landed, no skip.
    assert stats.records >= 1

    sha = hashlib.sha256((export_dir / "export.xml").read_bytes()).hexdigest()
    conn = duckdb.connect(str(db), read_only=True)
    try:
        row = conn.execute(
            "SELECT export_xml_sha256 FROM imports WHERE import_id = 'imp_first'"
        ).fetchone()
        assert row == (sha,)
    finally:
        conn.close()


def test_reimport_with_identical_xml_skips_via_sha256_fast_path(
    tmp_path: Path,
) -> None:
    """A second import of the same export.xml exits early; imports stays at one row."""
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    stats = run_import(export_dir, db, import_id="imp_second")
    # Tier 1 returns a default-constructed ImportStats: zero everywhere.
    assert stats.records == 0
    assert stats.workouts == 0
    assert stats.ecg_readings == 0
    assert stats.route_points == 0

    conn = duckdb.connect(str(db), read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
        # imp_first only; the skipped second import did not write its row.
        assert row is not None and int(row[0]) == 1
    finally:
        conn.close()


def test_force_bypasses_sha256_fast_path(tmp_path: Path) -> None:
    """``force=True`` re-runs the full pipeline even on a byte-identical export."""
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    stats = run_import(export_dir, db, import_id="imp_forced", force=True)
    # Full pipeline ran; the forced re-import re-parsed every row.
    assert stats.records >= 1
    # The new imports row landed too.
    conn = duckdb.connect(str(db), read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
        assert row is not None and int(row[0]) == 2
    finally:
        conn.close()


def test_incremental_reimport_only_adds_new_record(tmp_path: Path) -> None:
    """Second import of a slightly modified export adds only the new Record row."""
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    # Append one extra Record so the file's sha256 differs and Tier 1 falls
    # through. The original Record / Workout / Correlation / ActivitySummary
    # all hash-match the on-disk rows and are skipped by Tier 2.
    appended = _BASE_EXPORT_XML.replace(
        "</HealthData>",
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" '
        'unit="count" value="200" startDate="2024-01-02 09:00:00 +0900" '
        'endDate="2024-01-02 09:30:00 +0900"/>\n</HealthData>',
    )
    (export_dir / "export.xml").write_text(appended, encoding="utf-8")

    stats = run_import(export_dir, db, import_id="imp_second")
    # Exactly one new Record contributed; the other 1 row was already on disk.
    assert stats.records == 1
    # Workout / ActivitySummary / Correlation skipped because their hashes hit.
    assert stats.workouts == 0
    assert stats.activity_summaries == 0
    assert stats.correlations == 0

    conn = duckdb.connect(str(db), read_only=True)
    try:
        # records table now holds two rows (the original + the new one).
        row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        assert row is not None and int(row[0]) == 2
        # workouts / activity_summaries still hold exactly one row each --
        # the Tier 2 skip prevented a duplicate insert AND the dedup pass
        # was auto-skipped so no MVCC tombstones were emitted.
        row = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()
        assert row is not None and int(row[0]) == 1
        # The ECG and GPX point hashes were also already on disk so the
        # incremental skip path covered them too.
        row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM route_points").fetchone()
        assert row is not None and int(row[0]) == 2
    finally:
        conn.close()


def test_force_reimport_runs_legacy_dedup_and_keeps_one_row(tmp_path: Path) -> None:
    """``force=True`` re-imports every row and relies on Phase 4 dedup to collapse.

    The post-import row count for each dedup-keyed table is the same as
    fresh-import / incremental-reimport -- proving the legacy path still
    converges. The point of the test is to exercise the dedup-runs branch
    inside ``finalize_import`` so the v0.1.5 contract stays alive.
    """
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    run_import(export_dir, db, import_id="imp_forced", force=True)
    conn = duckdb.connect(str(db), read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM correlations").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()
        assert row is not None and int(row[0]) == 1
    finally:
        conn.close()


def test_incremental_skipped_workout_preserves_route_map_for_gpx(
    tmp_path: Path,
) -> None:
    """A skipped Workout still updates ``workout_route_map`` so GPX point_hashes match.

    Regression guard: if a re-import dropped the route map entry for a
    skipped workout, the GPX importer would compute point_hashes with an
    empty workout component and miss the existing-point set, re-inserting
    every route point and ballooning ``route_points``.
    """
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    # Re-import with one extra new record but the original workout / GPX
    # untouched -- the workout hash will hit ``existing.workouts``.
    appended = _BASE_EXPORT_XML.replace(
        "</HealthData>",
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" '
        'unit="count" value="200" startDate="2024-01-02 09:00:00 +0900" '
        'endDate="2024-01-02 09:30:00 +0900"/>\n</HealthData>',
    )
    (export_dir / "export.xml").write_text(appended, encoding="utf-8")

    run_import(export_dir, db, import_id="imp_second")

    conn = duckdb.connect(str(db), read_only=True)
    try:
        # Exactly the same two route points as after the first import --
        # the route map carried the workout hash forward, so the GPX
        # importer computed the same point_hashes and skipped both.
        row = conn.execute("SELECT COUNT(*) FROM route_points").fetchone()
        assert row is not None and int(row[0]) == 2
    finally:
        conn.close()


def test_import_single_gpx_skips_points_already_on_disk(
    fresh_conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    """A point_hash hit drops the point from the batch before staging."""
    # Pre-load a synthetic point that matches the GPX fixture's first trkpt.
    from apple_health_mcp.importers._hash import compute_hash
    from apple_health_mcp.importers.gpx import _rust_float_repr

    workout_hash = "wo_h"
    point_hash = compute_hash(
        [workout_hash, "2024-06-17T04:58:39Z", _rust_float_repr(35.0), _rust_float_repr(139.0)]
    )
    fresh_conn.execute(
        "INSERT INTO route_points "
        "(point_hash, workout_hash, latitude, longitude, timestamp, import_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [point_hash, workout_hash, 35.0, 139.0, "2024-06-17T04:58:39Z", "imp_old"],
    )

    snapshot = load_existing_hashes(fresh_conn)
    gpx_path = tmp_path / "route.gpx"
    gpx_path.write_text(_ROUTE_GPX, encoding="utf-8")

    inserted = import_single_gpx(fresh_conn, gpx_path, "imp_new", workout_hash, existing=snapshot)
    # First point hit; only the second (different lat/lon) was inserted.
    assert inserted == 1


def test_import_single_ecg_skips_csv_already_on_disk(
    fresh_conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    """An ecg_hash hit returns False BEFORE parsing the voltage section."""
    from apple_health_mcp.importers._hash import compute_hash
    from apple_health_mcp.importers._tz import normalize_apple_offset

    recorded_date = normalize_apple_offset("2024-06-15 10:30:00 +0900")
    ecg_hash = compute_hash([recorded_date, "Apple Watch"])
    fresh_conn.execute(
        "INSERT INTO ecg_readings (ecg_hash, recorded_date, device, import_id) VALUES (?, ?, ?, ?)",
        [ecg_hash, recorded_date, "Apple Watch", "imp_old"],
    )

    snapshot = load_existing_hashes(fresh_conn)
    csv_path = tmp_path / "ecg.csv"
    csv_path.write_text(_ECG_CSV, encoding="utf-8")

    inserted = import_single_ecg(fresh_conn, csv_path, "imp_new", existing=snapshot)
    assert inserted is False

    # ecg_samples must NOT have been touched -- the voltage parse was bypassed.
    row = fresh_conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 0


def test_import_single_ecg_inserts_when_hash_misses(
    fresh_conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    """A miss returns True so ``import_ecg_files`` increments the count."""
    snapshot = load_existing_hashes(fresh_conn)
    csv_path = tmp_path / "ecg.csv"
    csv_path.write_text(_ECG_CSV, encoding="utf-8")
    inserted = import_single_ecg(fresh_conn, csv_path, "imp_new", existing=snapshot)
    assert inserted is True


def test_ecg_files_skipped_log_line_fires_when_every_csv_is_skip(
    fresh_conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``skipped N already on disk`` summary log fires when at least one skip happens."""
    from apple_health_mcp.importers._hash import compute_hash
    from apple_health_mcp.importers._tz import normalize_apple_offset
    from apple_health_mcp.importers.ecg import import_ecg_files

    recorded_date = normalize_apple_offset("2024-06-15 10:30:00 +0900")
    ecg_hash = compute_hash([recorded_date, "Apple Watch"])
    fresh_conn.execute(
        "INSERT INTO ecg_readings (ecg_hash, recorded_date, device, import_id) VALUES (?, ?, ?, ?)",
        [ecg_hash, recorded_date, "Apple Watch", "imp_old"],
    )
    snapshot = load_existing_hashes(fresh_conn)

    ecg_dir = tmp_path / "electrocardiograms"
    ecg_dir.mkdir()
    (ecg_dir / "ecg.csv").write_text(_ECG_CSV, encoding="utf-8")

    import logging as _logging

    caplog.set_level(_logging.INFO, logger="apple_health_mcp.importers.ecg")
    count = import_ecg_files(fresh_conn, ecg_dir, "imp_new", existing=snapshot)
    assert count == 0
    assert any("skipped 1 already on disk" in rec.message for rec in caplog.records)


# ----------------------------------------------------------------------------
# Tier 2 inside the XML importer -- targeted handler skips.
# ----------------------------------------------------------------------------


def test_incremental_metadata_does_not_route_to_workout_when_record_skipped(
    tmp_path: Path,
) -> None:
    """A Record nested inside a Workout that hash-hits must drop its MetadataEntry too.

    Regression guard for the ``_skipping_record`` flag: without it, the
    metadata of a skipped Record would fall through to the
    ``_in_workout`` elif branch and be mis-routed into ``workout_metadata``.
    """
    xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000">
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="150" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:01:00 +0000">
   <MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="1"/>
  </Record>
 </Workout>
</HealthData>"""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(xml_body, encoding="utf-8")
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    # Modify export.xml so sha256 differs (force Tier 2 path).
    altered = xml_body.replace(
        "</HealthData>",
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" '
        'unit="count" value="50" startDate="2024-01-02 09:00:00 +0900" '
        'endDate="2024-01-02 09:30:00 +0900"/>\n</HealthData>',
    )
    (export_dir / "export.xml").write_text(altered, encoding="utf-8")

    run_import(export_dir, db, import_id="imp_second")

    # The nested-Record MetadataEntry from the first import is still
    # exactly one row in ``record_metadata`` -- not duplicated and not
    # incorrectly routed to ``workout_metadata``.
    conn = duckdb.connect(str(db), read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM record_metadata WHERE key = 'HKMetadataKeyHeartRateMotionContext'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute(
            "SELECT COUNT(*) FROM workout_metadata WHERE key = 'HKMetadataKeyHeartRateMotionContext'"
        ).fetchone()
        # The skipped record's metadata must NOT leak into workout_metadata.
        assert row is not None and int(row[0]) == 0
    finally:
        conn.close()


def test_incremental_fresh_record_nested_in_skipped_workout_keeps_metadata(
    tmp_path: Path,
) -> None:
    """A new Record inside a skipped Workout must still route its MetadataEntry to record_metadata.

    Regression guard for the issue #62 code-review finding: the original
    ``if _skipping_record or _skipping_workout: return`` guard at the top
    of ``_handle_metadata_entry`` fired before the ``_in_record`` priority
    branch, so a fresh nested Record inside a skipped Workout silently
    lost every MetadataEntry. The fix routes on ``_in_record`` first.
    """
    # First import: one Workout with one nested Record (no metadata).
    first_xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000">
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="150" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:01:00 +0000"/>
 </Workout>
</HealthData>"""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(first_xml, encoding="utf-8")
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    # Second import: SAME Workout (hash hits), SAME Record (hash hits)
    # BUT a NEW Record nested inside the workout that carries a
    # MetadataEntry. The new Record's record_hash misses
    # existing.records so it must be inserted; its MetadataEntry must
    # land in record_metadata (NOT silently dropped because the outer
    # Workout was skipped).
    second_xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" sourceName="Apple Watch" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:30:00 +0000">
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch" unit="count/min" value="150" startDate="2024-01-01 10:00:00 +0000" endDate="2024-01-01 10:01:00 +0000"/>
  <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" sourceName="Apple Watch" unit="kcal" value="42" startDate="2024-01-01 10:05:00 +0000" endDate="2024-01-01 10:06:00 +0000">
   <MetadataEntry key="HKMetadataKeyMetabolicEquivalentTask" value="6.5"/>
  </Record>
 </Workout>
</HealthData>"""
    (export_dir / "export.xml").write_text(second_xml, encoding="utf-8")
    run_import(export_dir, db, import_id="imp_second")

    conn = duckdb.connect(str(db), read_only=True)
    try:
        # The new Record landed.
        row = conn.execute(
            "SELECT COUNT(*) FROM records "
            "WHERE record_type = 'HKQuantityTypeIdentifierActiveEnergyBurned'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
        # The new Record's MetadataEntry landed in record_metadata,
        # NOT in workout_metadata.
        row = conn.execute(
            "SELECT COUNT(*) FROM record_metadata "
            "WHERE key = 'HKMetadataKeyMetabolicEquivalentTask'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute(
            "SELECT COUNT(*) FROM workout_metadata "
            "WHERE key = 'HKMetadataKeyMetabolicEquivalentTask'"
        ).fetchone()
        assert row is not None and int(row[0]) == 0
    finally:
        conn.close()


def test_incremental_skipped_workout_does_not_double_count_events(
    tmp_path: Path,
) -> None:
    """Re-importing an export whose Workout hash-hits must NOT insert events / stats / metadata."""
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "h.duckdb"
    run_import(export_dir, db, import_id="imp_first")

    # Append one extra Record so sha256 differs; the Workout block is
    # untouched and its hash still hits existing.workouts.
    appended = _BASE_EXPORT_XML.replace(
        "</HealthData>",
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" '
        'unit="count" value="200" startDate="2024-01-02 09:00:00 +0900" '
        'endDate="2024-01-02 09:30:00 +0900"/>\n</HealthData>',
    )
    (export_dir / "export.xml").write_text(appended, encoding="utf-8")

    run_import(export_dir, db, import_id="imp_second")

    conn = duckdb.connect(str(db), read_only=True)
    try:
        # Each child table holds exactly the rows from the first import.
        row = conn.execute("SELECT COUNT(*) FROM workout_events").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute("SELECT COUNT(*) FROM workout_statistics").fetchone()
        assert row is not None and int(row[0]) == 1
        row = conn.execute(
            "SELECT COUNT(*) FROM workout_metadata WHERE key = 'HKWeatherTemperature'"
        ).fetchone()
        assert row is not None and int(row[0]) == 1
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# CLI --force flag.
# ----------------------------------------------------------------------------


_runner = CliRunner()


def test_cli_force_flag_threads_through_to_run_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import --force`` reaches ``run_import(force=True)``."""
    captured: dict[str, object] = {}

    def fake_run_import(export_path: Path, db: Path | None, *, force: bool = False) -> object:
        captured["force"] = force

        class _Stats:
            records = 0
            workouts = 0
            ecg_readings = 0
            route_points = 0

        return _Stats()

    monkeypatch.setattr("apple_health_mcp.importers.run_import", fake_run_import, raising=False)

    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_text(
        '<?xml version="1.0"?><HealthData locale="en_US"/>', encoding="utf-8"
    )

    db = tmp_path / "h.duckdb"
    result = _runner.invoke(cli.app, ["--db", str(db), "import", "--force", str(export_dir)])
    assert result.exit_code == 0, result.output
    assert captured["force"] is True
