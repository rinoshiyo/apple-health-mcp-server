"""End-to-end smoke tests that exercise the on-disk fixtures.

These tests are intentionally coarse: they assemble a minimal Apple Health
``export_dir`` from the synthetic fixtures under ``tests/fixtures/``, run
the full XML -> ECG -> GPX -> finalize pipeline through
:func:`apple_health_mcp.importers.run_import`, and then invoke every one of
the 18 MCP tools to confirm each can return a well-formed JSON payload from
the resulting database. The fine-grained behaviour of each importer and
tool is covered by the unit suites; this module's job is to catch
regressions in the wiring between layers.

Locale-specific parser quirks (Japanese / Spanish CSV variants, etc.) are
exercised by inline strings in the per-importer unit tests; the on-disk
fixtures stay locale-neutral by policy. See ``tests/fixtures/README.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.importers import ImportStats, run_import
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
from tests._helpers import bind_tool, call_tool

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _materialise_export(tmp_path: Path) -> Path:
    """Copy the on-disk fixtures into the layout ``run_import`` expects."""
    export_dir = tmp_path / "apple_health_export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_bytes((_FIXTURES / "sample_export.xml").read_bytes())

    electro_dir = export_dir / "electrocardiograms"
    electro_dir.mkdir()
    (electro_dir / "sample_ecg.csv").write_bytes((_FIXTURES / "sample_ecg.csv").read_bytes())

    routes_dir = export_dir / "workout-routes"
    routes_dir.mkdir()
    (routes_dir / "sample_workout_route.gpx").write_bytes(
        (_FIXTURES / "sample_workout_route.gpx").read_bytes()
    )
    return export_dir


@dataclass(frozen=True)
class ImportedFixture:
    """Outputs of running ``run_import`` on the bundled smoke fixtures."""

    stats: ImportStats
    db_path: Path


@pytest.fixture(scope="module")
def imported_db(tmp_path_factory: pytest.TempPathFactory) -> ImportedFixture:
    """Run the full importer pipeline on the fixtures once per module.

    Module scope keeps the fan-out cheap: every smoke test in this file
    consumes the same import output, so paying for ``run_import`` once is
    enough. The returned ``ImportedFixture`` is immutable so a test cannot
    leak state into a sibling test through it.
    """
    tmp_path = tmp_path_factory.mktemp("smoke")
    export_dir = _materialise_export(tmp_path)
    db_path = tmp_path / "smoke.duckdb"
    stats = run_import(export_dir, db_path, import_id="imp_smoke")
    return ImportedFixture(stats=stats, db_path=db_path)


@contextmanager
def _open_db(db_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a read-write DuckDB connection that is always closed on exit.

    DuckDB holds an exclusive file lock while the connection is open. If a
    test leaks the connection on assertion failure, ``tmp_path`` cleanup
    raises ``PermissionError`` on Windows and masks the real failure.
    ``closing`` guarantees the close call runs regardless of how the
    ``with`` block exits.
    """
    conn = duckdb.connect(str(db_path))
    with closing(conn):
        yield conn


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Return the single scalar value of a one-row, one-column SELECT."""
    row = conn.execute(sql).fetchone()
    assert row is not None, sql
    value = row[0]
    assert isinstance(value, str)
    return value


# --- per-importer smoke ------------------------------------------------------


def test_xml_importer_smoke(imported_db: ImportedFixture) -> None:
    stats = imported_db.stats
    # sample_export.xml emits 6 top-level Record elements (Apple Health
    # duplicates Correlation children at the top level by spec):
    # 2 HeartRate + 1 StepCount + 1 StateOfMind + 2 BloodPressure.
    assert stats.records == 6
    assert stats.workouts == 1
    assert stats.activity_summaries == 1
    assert stats.correlations == 1
    assert stats.correlation_members == 2


def test_ecg_importer_smoke(imported_db: ImportedFixture) -> None:
    assert imported_db.stats.ecg_readings == 1


def test_gpx_importer_smoke(imported_db: ImportedFixture) -> None:
    # sample_workout_route.gpx has 3 trkpts.
    assert imported_db.stats.route_points == 3


# --- MCP-tool smoke ----------------------------------------------------------


def test_all_mcp_tools_smoke(imported_db: ImportedFixture) -> None:
    """Invoke each of the 18 MCP tools against the fixture-imported DB."""
    with _open_db(imported_db.db_path) as conn:
        # Resolve fixture-derived row keys once.
        workout_hash = _scalar(conn, "SELECT workout_hash FROM workouts LIMIT 1")
        hr_record_hash = _scalar(
            conn,
            "SELECT record_hash FROM records "
            "WHERE record_type = 'HKQuantityTypeIdentifierHeartRate' LIMIT 1",
        )
        correlation_hash = _scalar(conn, "SELECT correlation_hash FROM correlations LIMIT 1")
        ecg_hash = _scalar(conn, "SELECT ecg_hash FROM ecg_readings LIMIT 1")

        # 1. list_record_types
        rows = call_tool(bind_tool(list_record_types, conn))
        # Issue #91 (T1): wire field is ``record_type``.
        assert any(r["record_type"] == "HKQuantityTypeIdentifierHeartRate" for r in rows)

        # 2. query_records -- envelope shape (issue #108 / PR-E).
        payload = call_tool(
            bind_tool(query_records, conn),
            record_type="HKQuantityTypeIdentifierHeartRate",
        )
        assert len(payload["items"]) == 2
        assert payload["total"] == 2
        assert payload["next_offset"] is None

        # 3. get_record_statistics
        rows = call_tool(
            bind_tool(get_record_statistics, conn),
            record_type="HKQuantityTypeIdentifierHeartRate",
        )
        assert isinstance(rows, list)

        # 4. list_workouts -- envelope shape.
        payload = call_tool(bind_tool(list_workouts, conn))
        assert any(r["workout_hash"] == workout_hash for r in payload["items"])

        # 5. get_workout_details
        payload = call_tool(bind_tool(get_workout_details, conn), workout_hash=workout_hash)
        assert payload["workout"]["workout_hash"] == workout_hash

        # 6. get_activity_summaries
        rows = call_tool(bind_tool(get_activity_summaries, conn))
        assert rows and rows[0]["date_components"] == "2024-06-15"

        # 7. get_workout_route -- envelope shape (issue #108 / PR-E).
        payload = call_tool(bind_tool(get_workout_route, conn), workout_hash=workout_hash)
        assert payload["total"] == 3
        assert len(payload["items"]) == 3
        assert payload["next_offset"] is None

        # 8. get_heart_rate_samples -- envelope shape (no embedded HR
        # samples in the fixture, but the tool must still wrap them).
        payload = call_tool(bind_tool(get_heart_rate_samples, conn), record_hash=hr_record_hash)
        assert isinstance(payload["items"], list)
        assert payload["next_offset"] is None

        # 9. list_correlations -- envelope shape.
        payload = call_tool(bind_tool(list_correlations, conn))
        assert any(r["correlation_hash"] == correlation_hash for r in payload["items"])

        # 10. get_correlation_details
        payload = call_tool(
            bind_tool(get_correlation_details, conn),
            correlation_hash=correlation_hash,
        )
        assert payload["correlation"]["correlation_hash"] == correlation_hash
        assert len(payload["members"]) == 2

        # 11. list_ecg_readings -- envelope shape.
        payload = call_tool(bind_tool(list_ecg_readings, conn))
        assert payload["items"][0]["ecg_hash"] == ecg_hash

        # 12. get_ecg_data
        payload = call_tool(
            bind_tool(get_ecg_data, conn),
            ecg_hash=ecg_hash,
            include_voltages=True,
        )
        assert payload["reading"]["ecg_hash"] == ecg_hash
        assert payload["stats"]["sample_count"] == 10

        # 13. run_custom_query
        rows = call_tool(
            bind_tool(run_custom_query, conn),
            query="SELECT COUNT(*) AS n FROM records",
        )
        assert int(rows[0]["n"]) == 6

        # 14. list_data_sources
        rows = call_tool(bind_tool(list_data_sources, conn))
        sources = {r["source_name"] for r in rows}
        assert "Apple Watch" in sources

        # 15. get_import_history
        rows = call_tool(bind_tool(get_import_history, conn))
        assert any(r["import_id"] == "imp_smoke" for r in rows)
        # Issue #44: the restored DEFAULT CURRENT_TIMESTAMP on
        # ``imports.imported_at`` must actually fire on a real import.
        # Prior to v0.1.4 ``deduplicate_tables`` stripped the default so
        # this column wrote as NULL on every import.
        smoke_row = next(r for r in rows if r["import_id"] == "imp_smoke")
        assert smoke_row["imported_at"] is not None

        # 16. list_state_of_mind -- envelope shape.
        payload = call_tool(bind_tool(list_state_of_mind, conn))
        assert len(payload["items"]) == 1

        # 17. get_me_attributes — round-trip the <Me> element written into
        # sample_export.xml.
        payload = call_tool(bind_tool(get_me_attributes, conn))
        assert payload["date_of_birth"] == "1990-01-01"
        assert payload["biological_sex"] == "HKBiologicalSexNotSet"

        # 18. get_server_info — the runtime self-diagnosis primitive
        # added in v0.3.0 (issue #137). Smoke confirms the live
        # connection's on-disk DB path is reported (the field's whole
        # purpose is to surface a divergence between resolver output
        # and the actual open handle).
        info = call_tool(bind_tool(get_server_info, conn))
        assert info["db_path"] == str(imported_db.db_path)
        assert info["record_count"] > 0
        assert info["config_source"] in {
            "env:APPLE_HEALTH_DB",
            "env:APPLE_HEALTH_DATA_DIR",
            "platform_default",
        }


# --- date-only end_date inclusive smoke (issue #49) --------------------------


def test_query_records_date_only_end_date_includes_named_day(
    imported_db: ImportedFixture,
) -> None:
    """A date-only ``end_date`` on ``query_records`` returns rows from that day.

    Before #49, DuckDB cast a bare ``YYYY-MM-DD`` to ``TIMESTAMPTZ`` at
    00:00:00, so ``end_date <= 'YYYY-MM-DD'`` silently dropped every
    record on the named day. This smoke confirms the fix end-to-end:
    the fixture's HR samples land on a specific day, and asking
    ``end_date=<that day>`` must return them rather than zero rows.
    """
    with _open_db(imported_db.db_path) as conn:
        # Discover the day the fixture's HR samples land on so the
        # assertion does not depend on the fixture's calendar choices.
        hr_day = _scalar(
            conn,
            "SELECT strftime(start_date, '%Y-%m-%d') FROM records "
            "WHERE record_type = 'HKQuantityTypeIdentifierHeartRate' "
            "ORDER BY start_date LIMIT 1",
        )
        rows = call_tool(
            bind_tool(query_records, conn),
            record_type="HKQuantityTypeIdentifierHeartRate",
            start_date=hr_day,
            end_date=hr_day,
        )
        # Without the fix this would be zero; the fixture seeds 2 HR
        # samples on the same day, so both must survive the upper bound.
        assert len(rows) >= 1


# --- fresh-install gate (issue #38) ------------------------------------------


def test_fresh_install_serves_with_import_required_message(
    tmp_path: Path,
) -> None:
    """``serve`` against a never-imported path boots and surfaces guidance.

    v0.4 (issue #148): the production serve path opens DuckDB
    ``read_only=False`` so the upcoming ``import_zip`` MCP tool can
    drive the importer inline. This integration test now mirrors that
    setting; pre-v0.4 it opened ``read_only=True`` and silently
    diverged from production, so a future regression in the writable
    fresh-install bootstrap path (missing file -> schema-empty file ->
    "Table imports does not exist") would have crashed every Claude
    Desktop fresh install without failing this test.
    """
    import asyncio

    from apple_health_mcp.db.connection import get_connection
    from apple_health_mcp.server.data_state import (
        DataState,
        build_state_error_payload,
    )

    db_path = tmp_path / "fresh" / "health.duckdb"
    assert not db_path.exists()
    conn = get_connection(db_path, read_only=False)
    try:
        # Bootstrap created the file + schema.
        assert db_path.exists()
        fn = bind_tool(list_record_types, conn)
        out = asyncio.run(fn())
        # env unset -> NEEDS_CONFIG envelope (root conftest's autouse
        # fixture clears APPLE_HEALTH_EXPORT_ZIPS_DIR so the assertion
        # is deterministic).
        assert out == build_state_error_payload(DataState.NEEDS_CONFIG)
        # ``get_import_history`` stays callable as the discovery path:
        # an empty list is the canonical "no imports yet" signal.
        rows = call_tool(bind_tool(get_import_history, conn))
        assert rows == []
    finally:
        conn.close()
