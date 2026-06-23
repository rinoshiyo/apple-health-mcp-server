"""Top-level pipeline that runs XML, ECG, GPX importers in order.

Mirrors the Rust ``import::run_import``: parse ``export.xml`` first
because its scan produces the route map the GPX phase needs, then ECG
(independent), then GPX (depends on the XML output), then the dedupe /
backfill / daily-stats finalize phase. Each phase logs at INFO level for
human-readable progress.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from apple_health_mcp.db.connection import get_connection
from apple_health_mcp.db.migrations import apply_pending_migrations
from apple_health_mcp.db.schema import ensure_schema
from apple_health_mcp.importers._existing_hashes import (
    ExistingHashes,
    load_existing_hashes,
)
from apple_health_mcp.importers.dedup import finalize_import
from apple_health_mcp.importers.ecg import import_ecg_files
from apple_health_mcp.importers.gpx import import_gpx_files
from apple_health_mcp.importers.xml import ImportStats, import_xml

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

# Chunk size for the sha256 streaming hash (issue #62 Tier 1). 1 MB
# is the same value the XML SAX target reads in, so the OS page
# cache stays warm if the importer goes on to parse the file
# immediately after hashing it.
_SHA256_READ_CHUNK_BYTES = 1 << 20


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
    force: bool = False,
) -> ImportStats:
    """Run the full XML -> ECG -> GPX -> finalize pipeline on ``export_dir``.

    Returns the :class:`ImportStats` from the XML phase, augmented with the
    ECG / GPX counts in fields the dataclass already exposes
    (``ecg_readings``, ``route_points``).

    ``db_path`` defaults to the XDG-resolved location; pass ``Path(":memory:")``
    -- or an explicit file path under ``tmp_path`` in tests -- to override.

    Two re-import optimisations from issue #62 fire here:

    * **Tier 1 sha256 fast path.** The orchestrator streams sha256 over
      ``export.xml`` once and compares it against the most recent
      ``imports.export_xml_sha256`` row. A byte-identical export exits
      in roughly one disk-read of wall-clock without parsing the file.
      ``force=True`` bypasses the check.
    * **Tier 2 incremental hash sets.** When the destination DB already
      holds prior import data (and ``force`` is False), every dedup hash
      currently on disk is snapshotted into Python sets and threaded
      into the XML / GPX / ECG handlers. Each handler checks the
      freshly-computed hash before staging the row, so a re-import
      contributes only genuinely-new rows. Phase 4 dedup auto-skips
      because the bulk staging buffers carry no duplicates -- this
      avoids the DuckDB MVCC tombstones that would otherwise balloon
      the on-disk file on every re-import.

    ``force=True`` falls back to the legacy full-insert + Phase 4 dedup
    path (the same code v0.1.5 took unconditionally) so a user who
    suspects on-disk drift can re-import from scratch over the existing
    DB without first deleting it.
    """
    start = time.monotonic()
    actual_import_id = import_id or make_import_id()
    _logger.info("Starting import %s from %s", actual_import_id, export_dir)

    xml_path = export_dir / "export.xml"
    export_sha = _compute_file_sha256(xml_path)

    conn = _open_db(db_path)
    try:
        ensure_schema(conn)
        # Tier 1 requires the ``imports.export_xml_sha256`` column. The
        # migration is idempotent on a fresh DB (``ADD COLUMN IF NOT
        # EXISTS`` no-ops because ``ensure_schema`` already declared it)
        # and patches a pre-#62 on-disk DB to v2.
        apply_pending_migrations(conn)

        # Tier 1: sha256 fast path. Skip the whole import when the
        # incoming export.xml is byte-identical to the last successful
        # one. ``--force`` bypasses; a missing ``export.xml`` falls
        # through so import_xml below can raise the proper error.
        if not force and export_sha is not None and _sha256_matches_prior(conn, export_sha):
            _logger.info(
                "Skipping import: export.xml is byte-identical to the most recent "
                "successful import (sha256=%s...). Pass --force to re-import.",
                export_sha[:12],
            )
            return ImportStats()

        # Tier 2: load every dedup-keyed hash currently on disk into Python
        # sets if the DB already holds prior import data AND --force is not
        # set. A fresh-install / empty DB skips this and runs the legacy
        # full-insert + Phase 4 dedup path; ``--force`` does the same so a
        # user can re-run the dedup pass over an existing DB.
        existing: ExistingHashes | None = None
        if not force and _has_prior_imports(conn):
            existing = load_existing_hashes(conn)

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
        stats = import_xml(conn, xml_path, actual_import_id, existing=existing)

        _logger.info("Phase 2: Parsing ECG files")
        stats.ecg_readings = import_ecg_files(
            conn, export_dir / "electrocardiograms", actual_import_id, existing=existing
        )

        _logger.info("Phase 3: Parsing GPX route files")
        stats.route_points = import_gpx_files(
            conn,
            export_dir / "workout-routes",
            actual_import_id,
            stats.workout_route_map,
            existing=existing,
        )

        _logger.info("Phase 4: Finalize (dedupe, backfill, daily stats)")
        finalize_import(conn, skip_dedup=existing is not None)

        duration_secs = time.monotonic() - start
        conn.execute(
            """
            INSERT INTO imports (
                import_id, export_dir, record_count, workout_count, duration_secs,
                export_xml_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                actual_import_id,
                str(export_dir),
                stats.records,
                stats.workouts,
                duration_secs,
                export_sha,
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


def _compute_file_sha256(path: Path) -> str | None:
    """Stream sha256 over ``path``; return the hex digest or ``None`` if absent.

    ``None`` signals "no sha256 to record / compare" so the Tier 1 fast
    path falls through and the regular ``import_xml`` call surfaces the
    missing-file error with its normal message. Any other OSError
    (permission denied, mid-read I/O failure) also produces ``None``
    rather than crashing the import here -- the downstream parse will
    hit the same fault and report it in context.
    """
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(_SHA256_READ_CHUNK_BYTES), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _sha256_matches_prior(conn: duckdb.DuckDBPyConnection, export_sha: str) -> bool:
    """Return True when the most recent recorded sha256 matches ``export_sha``.

    ``ORDER BY imported_at DESC`` reflects the issue #44 fix that made
    ``imported_at`` consistently populated; the column was sometimes
    NULL on pre-#44 DBs but the migration that landed alongside this
    function repairs that schema in place so the ordering remains
    well-defined across upgrade paths.
    """
    row = conn.execute(
        "SELECT export_xml_sha256 FROM imports "
        "WHERE export_xml_sha256 IS NOT NULL "
        "ORDER BY imported_at DESC LIMIT 1"
    ).fetchone()
    return row is not None and row[0] == export_sha


def _has_prior_imports(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True when the ``imports`` table already holds at least one row.

    Used to gate the Tier 2 existing-hash snapshot load: on a fresh DB
    the sets would be empty anyway, so we skip the cost of issuing six
    ``SELECT DISTINCT`` round-trips and the orchestrator's later
    ``skip_dedup=False`` keeps the legacy Phase 4 path lit.
    """
    row = conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone()
    return row is not None


def _open_db(db_path: Path | None) -> duckdb.DuckDBPyConnection:
    """Open the destination DuckDB connection.

    Wraps :func:`get_connection` so tests can substitute via monkeypatch
    without touching the orchestrator's main path.
    """
    return get_connection(db_path)
