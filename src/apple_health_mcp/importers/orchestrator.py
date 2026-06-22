"""Top-level pipeline that runs XML, ECG, GPX importers in order.

Mirrors the Rust ``import::run_import``: parse ``export.xml`` first
because its scan produces the route map the GPX phase needs, then ECG
(independent), then GPX (depends on the XML output), then the dedupe /
backfill / daily-stats finalize phase. Each phase logs at INFO level for
human-readable progress.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from apple_health_mcp.db.connection import get_connection
from apple_health_mcp.db.schema import ensure_schema
from apple_health_mcp.importers.dedup import finalize_import
from apple_health_mcp.importers.ecg import import_ecg_files
from apple_health_mcp.importers.gpx import import_gpx_files
from apple_health_mcp.importers.xml import ImportStats, import_xml

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


def make_import_id(now: datetime | None = None) -> str:
    """Return a unique import identifier.

    Includes microseconds so two imports launched in the same wall-clock
    second (CI, scripted batches) cannot collide and dedupe into one
    ``imports`` row.
    """
    moment = now or datetime.now(UTC)
    return f"import_{moment.strftime('%Y%m%d_%H%M%S')}_{moment.microsecond:06d}"


def run_import(
    export_dir: Path,
    db_path: Path | None = None,
    *,
    import_id: str | None = None,
) -> ImportStats:
    """Run the full XML -> ECG -> GPX -> finalize pipeline on ``export_dir``.

    Returns the :class:`ImportStats` from the XML phase, augmented with the
    ECG / GPX counts in fields the dataclass already exposes
    (``ecg_readings``, ``route_points``).

    ``db_path`` defaults to the XDG-resolved location; pass ``Path(":memory:")``
    -- or an explicit file path under ``tmp_path`` in tests -- to override.
    """
    start = time.monotonic()
    actual_import_id = import_id or make_import_id()
    _logger.info("Starting import %s from %s", actual_import_id, export_dir)

    conn = _open_db(db_path)
    try:
        ensure_schema(conn)

        # DuckDB defaults to preserving insertion order during checkpoint,
        # which costs an extra sort over millions of imported rows. The
        # bulk-load path (issue #41) is unordered by design — Appender / COPY
        # FROM CSV both write in the order rows arrive at the buffer, and
        # downstream queries always re-sort via ORDER BY anyway — so we tell
        # DuckDB to skip the preservation work.
        #
        # Scope: this is a session-scoped PRAGMA on the connection ``_open_db``
        # just opened above; we close that connection in the ``finally`` at
        # the bottom of ``run_import``. The override therefore lives for the
        # lifetime of THIS import only, and cannot leak to a future caller
        # that opens its own connection. A future refactor that calls
        # ``run_import`` with an externally-owned conn would inherit the
        # PRAGMA for that conn's remaining lifetime — if that ever happens,
        # move the PRAGMA into ``get_connection(read_only=False)`` instead.
        conn.execute("PRAGMA preserve_insertion_order = false;")

        _logger.info("Phase 1: Parsing export.xml")
        stats = import_xml(conn, export_dir / "export.xml", actual_import_id)

        _logger.info("Phase 2: Parsing ECG files")
        stats.ecg_readings = import_ecg_files(
            conn, export_dir / "electrocardiograms", actual_import_id
        )

        _logger.info("Phase 3: Parsing GPX route files")
        stats.route_points = import_gpx_files(
            conn,
            export_dir / "workout-routes",
            actual_import_id,
            stats.workout_route_map,
        )

        _logger.info("Phase 4: Finalize (dedupe, backfill, daily stats)")
        finalize_import(conn)

        duration_secs = time.monotonic() - start
        conn.execute(
            """
            INSERT INTO imports (
                import_id, export_dir, record_count, workout_count, duration_secs
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                actual_import_id,
                str(export_dir),
                stats.records,
                stats.workouts,
                duration_secs,
            ],
        )

        _logger.info("Import complete in %.1fs", duration_secs)
        _logger.info(
            "  Records: %d, Workouts: %d, Activity Summaries: %d",
            stats.records,
            stats.workouts,
            stats.activity_summaries,
        )
        _logger.info(
            "  ECG readings: %d, Route points: %d, Metadata entries: %d",
            stats.ecg_readings,
            stats.route_points,
            stats.metadata_entries,
        )
        return stats
    finally:
        conn.close()


def _open_db(db_path: Path | None) -> duckdb.DuckDBPyConnection:
    """Open the destination DuckDB connection.

    Wraps :func:`get_connection` so tests can substitute via monkeypatch
    without touching the orchestrator's main path.
    """
    return get_connection(db_path)
