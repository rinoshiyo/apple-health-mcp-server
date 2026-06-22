"""End-to-end smoke tests that exercise the on-disk fixtures.

These tests are intentionally coarse: they assemble a minimal Apple Health
``export_dir`` from the synthetic fixtures under ``tests/fixtures/``, run
the full XML -> ECG -> GPX -> finalize pipeline through
:func:`apple_health_mcp.importers.run_import`, and then invoke every one of
the 16 MCP tools to confirm each can return a well-formed JSON payload from
the resulting database. The fine-grained behaviour of each importer and
tool is covered by the unit suites; this module's job is to catch
regressions in the wiring between layers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Lock
from typing import Any

import duckdb
import pytest

from apple_health_mcp.importers import ImportStats, run_import
from apple_health_mcp.server.tools import (
    get_activity_summaries,
    get_correlation_details,
    get_ecg_data,
    get_heart_rate_samples,
    get_import_history,
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


@pytest.fixture
def imported_db(tmp_path: Path) -> tuple[ImportStats, Path]:
    """Run the full importer pipeline on the fixtures and return ``(stats, db_path)``."""
    export_dir = _materialise_export(tmp_path)
    db_path = tmp_path / "smoke.duckdb"
    stats = run_import(export_dir, db_path, import_id="imp_smoke")
    return stats, db_path


# --- per-importer smoke ------------------------------------------------------


def test_xml_importer_smoke(imported_db: tuple[ImportStats, Path]) -> None:
    stats, _ = imported_db
    # sample_export.xml emits 6 top-level Record elements (Apple Health
    # duplicates Correlation children at the top level by spec):
    # 2 HeartRate + 1 StepCount + 1 StateOfMind + 2 BloodPressure.
    assert stats.records == 6
    assert stats.workouts == 1
    assert stats.activity_summaries == 1
    assert stats.correlations == 1
    assert stats.correlation_members == 2


def test_ecg_importer_smoke(imported_db: tuple[ImportStats, Path]) -> None:
    stats, _ = imported_db
    assert stats.ecg_readings == 1


def test_gpx_importer_smoke(imported_db: tuple[ImportStats, Path]) -> None:
    stats, _ = imported_db
    # sample_workout_route.gpx has 3 trkpts.
    assert stats.route_points == 3


# --- MCP-tool smoke ----------------------------------------------------------


class _StubMCP:
    """Capture the function registered by ``@mcp.tool``."""

    def __init__(self) -> None:
        self.fn: Callable[..., Awaitable[str]] | None = None

    def tool(self, *, description: str = "") -> Callable[..., Any]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.fn = fn
            return fn

        return decorator


def _bind(module: Any, conn: duckdb.DuckDBPyConnection) -> Callable[..., Awaitable[str]]:
    stub = _StubMCP()
    module.register(stub, conn, Lock())
    assert stub.fn is not None
    return stub.fn


def _call(fn: Callable[..., Awaitable[str]], **kwargs: Any) -> Any:
    return json.loads(asyncio.run(fn(**kwargs)))


def _open(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=False)


def test_all_mcp_tools_smoke(imported_db: tuple[ImportStats, Path]) -> None:
    """Invoke each of the 16 MCP tools against the fixture-imported DB."""
    _, db_path = imported_db
    conn = _open(db_path)
    try:
        # Resolve fixture-derived row keys once.
        workout_hash = conn.execute("SELECT workout_hash FROM workouts LIMIT 1").fetchone()
        assert workout_hash is not None
        hr_record_hash = conn.execute(
            "SELECT record_hash FROM records "
            "WHERE record_type = 'HKQuantityTypeIdentifierHeartRate' LIMIT 1"
        ).fetchone()
        assert hr_record_hash is not None
        correlation_hash = conn.execute(
            "SELECT correlation_hash FROM correlations LIMIT 1"
        ).fetchone()
        assert correlation_hash is not None
        ecg_hash = conn.execute("SELECT ecg_hash FROM ecg_readings LIMIT 1").fetchone()
        assert ecg_hash is not None

        # 1. list_record_types
        rows = _call(_bind(list_record_types, conn))
        assert any(r["type"] == "HKQuantityTypeIdentifierHeartRate" for r in rows)

        # 2. query_records
        rows = _call(
            _bind(query_records, conn),
            record_type="HKQuantityTypeIdentifierHeartRate",
        )
        assert len(rows) == 2

        # 3. get_record_statistics
        rows = _call(
            _bind(get_record_statistics, conn),
            record_type="HKQuantityTypeIdentifierHeartRate",
        )
        assert isinstance(rows, list)

        # 4. list_workouts
        rows = _call(_bind(list_workouts, conn))
        assert any(r["workout_hash"] == workout_hash[0] for r in rows)

        # 5. get_workout_details
        payload = _call(_bind(get_workout_details, conn), workout_hash=workout_hash[0])
        assert payload["workout"]["workout_hash"] == workout_hash[0]

        # 6. get_activity_summaries
        rows = _call(_bind(get_activity_summaries, conn))
        assert rows and rows[0]["date_components"] == "2024-06-15"

        # 7. get_workout_route
        rows = _call(_bind(get_workout_route, conn), workout_hash=workout_hash[0])
        assert len(rows) == 3

        # 8. get_heart_rate_samples (no embedded HR samples in the fixture, but
        # the tool must still return a list).
        rows = _call(_bind(get_heart_rate_samples, conn), record_hash=hr_record_hash[0])
        assert isinstance(rows, list)

        # 9. list_correlations
        rows = _call(_bind(list_correlations, conn))
        assert any(r["correlation_hash"] == correlation_hash[0] for r in rows)

        # 10. get_correlation_details
        payload = _call(
            _bind(get_correlation_details, conn),
            correlation_hash=correlation_hash[0],
        )
        assert payload["correlation"]["correlation_hash"] == correlation_hash[0]
        assert len(payload["members"]) == 2

        # 11. list_ecg_readings
        rows = _call(_bind(list_ecg_readings, conn))
        assert rows[0]["ecg_hash"] == ecg_hash[0]

        # 12. get_ecg_data
        payload = _call(
            _bind(get_ecg_data, conn),
            ecg_hash=ecg_hash[0],
            include_voltages=True,
        )
        assert payload["reading"]["ecg_hash"] == ecg_hash[0]
        assert payload["stats"]["sample_count"] == 10

        # 13. run_custom_query
        rows = _call(
            _bind(run_custom_query, conn),
            query="SELECT COUNT(*) AS n FROM records",
        )
        assert int(rows[0]["n"]) == 6

        # 14. list_data_sources
        rows = _call(_bind(list_data_sources, conn))
        sources = {r["source_name"] for r in rows}
        assert "Apple Watch" in sources

        # 15. get_import_history
        rows = _call(_bind(get_import_history, conn))
        assert any(r["import_id"] == "imp_smoke" for r in rows)

        # 16. list_state_of_mind
        rows = _call(_bind(list_state_of_mind, conn))
        assert len(rows) == 1
    finally:
        conn.close()
