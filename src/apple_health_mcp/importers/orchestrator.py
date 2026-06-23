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
from apple_health_mcp.importers.xml import _READ_CHUNK_BYTES, ImportStats, import_xml

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

# Re-use the XML SAX target's 1 MB read chunk size for the sha256
# streaming hash (issue #62 Tier 1). Sharing the constant keeps the
# 'sha256 read keeps the OS page cache warm for the immediately-
# following XML parse' invariant alive across future tuning changes.
_SHA256_READ_CHUNK_BYTES = _READ_CHUNK_BYTES


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
      ``force=True`` bypasses ONLY this check -- the Tier 2 snapshot
      still loads and the handlers still skip on-disk hashes.
    * **Tier 2 incremental hash sets.** When the destination DB already
      holds prior import data, every dedup hash currently on disk is
      snapshotted into Python sets and threaded into the XML / GPX /
      ECG handlers. Each handler checks the freshly-computed hash
      before staging the row, so a re-import contributes only
      genuinely-new rows. Phase 4 dedup auto-skips because the bulk
      staging buffers carry no duplicates -- this avoids the DuckDB
      MVCC tombstones that would otherwise balloon the on-disk file
      on every re-import.

    ``force=True`` is the right call when the on-disk export.xml is
    byte-identical to a prior import BUT the user wants to re-run the
    import (e.g. after deleting some rows from the DB by hand, or to
    verify the importer still produces the same row counts). The hash
    skip in Tier 2 still keeps the re-import cheap; only the sha256
    bail-out is bypassed.
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
        # sets if the DB already holds prior import data. ``--force`` only
        # bypasses the Tier 1 sha256 fast path -- it does NOT disable the
        # incremental hash sets, because there is no useful interpretation
        # of "re-import this data, but pay the on-disk tombstone cost of
        # full Phase 4 dedup". The fresh-install / empty DB case still
        # falls through to the legacy full-insert + Phase 4 dedup path
        # (existing stays ``None`` because the imports table is empty).
        existing: ExistingHashes | None = None
        if _has_prior_imports(conn):
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
        # On a Tier 2 incremental re-import these stats report rows
        # NEWLY INSERTED in this run, not total rows present on disk -- a
        # no-change re-import legitimately reads "0 records, 0 workouts,
        # ..." even though the database still holds the full history.
        # The "Newly inserted" label keeps that distinction visible so a
        # user does not mistake the summary for missing data.
        label = "Newly inserted" if existing is not None else "Imported"
        _logger.info(
            "  %s: %d records, %d workouts, %d activity summaries",
            label,
            stats.records,
            stats.workouts,
            stats.activity_summaries,
        )
        _logger.info(
            "  %s: %d ECG readings, %d route points, %d metadata entries",
            label,
            stats.ecg_readings,
            stats.route_points,
            stats.metadata_entries,
        )
        return stats
    finally:
        conn.close()


def _compute_file_sha256(path: Path) -> str | None:
    """Stream sha256 over ``path``; return the hex digest or ``None`` if absent.

    Returns ``None`` and lets the downstream ``import_xml`` call surface
    the missing-file error in its normal context. Other OSError flavors
    (permission denied, mid-read EIO from a flaky disk) also return
    ``None`` so the orchestrator does not crash here, but the importer
    logs a WARNING in those cases -- a silent fall-through would stamp
    NULL into ``imports.export_xml_sha256`` and the next byte-identical
    re-import could no longer fast-path-skip, masquerading as a
    perf regression.
    """
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(_SHA256_READ_CHUNK_BYTES), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except FileNotFoundError:
        # Expected and handled by ``import_xml`` below; no log needed.
        return None
    except OSError as exc:
        # Anything other than "file absent" is a surprise; log so the
        # next maintainer can see why ``imports.export_xml_sha256``
        # landed NULL on an import that otherwise succeeded.
        _logger.warning(
            "failed to compute sha256 of %s for the Tier 1 fast path "
            "(import will proceed but the fast path is bypassed): %s",
            path,
            exc,
        )
        return None


def _sha256_matches_prior(conn: duckdb.DuckDBPyConnection, export_sha: str) -> bool:
    """Return True when the most recent recorded sha256 matches ``export_sha``.

    ``ORDER BY imported_at DESC, import_id DESC`` makes the ordering
    total even when two imports stamp the same wall-clock second (and
    therefore the same ``CURRENT_TIMESTAMP`` default). ``make_import_id``
    includes microseconds, so the secondary sort breaks every realistic
    tie. The ``imported_at`` field can still be NULL on a pre-#44 DB
    before :func:`repair_legacy_constraints_if_needed` runs; ``DESC``
    sorts NULLs last in DuckDB so the most recent populated row wins
    until the repair fires on the next finalize pass.
    """
    row = conn.execute(
        "SELECT export_xml_sha256 FROM imports "
        "WHERE export_xml_sha256 IS NOT NULL "
        "ORDER BY imported_at DESC, import_id DESC LIMIT 1"
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
