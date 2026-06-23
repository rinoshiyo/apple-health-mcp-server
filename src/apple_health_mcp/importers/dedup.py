"""Deduplication entry point for the import pipeline.

The actual SQL lives in :mod:`apple_health_mcp.db.schema` (``deduplicate_tables``
plus ``populate_workout_vestigial_columns`` and ``rebuild_daily_stats``). This
module composes them into the post-import phase the orchestrator runs so each
importer module stays focused on parsing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apple_health_mcp.db.schema import (
    deduplicate_tables,
    populate_workout_vestigial_columns,
    rebuild_daily_stats,
)

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


def finalize_import(conn: duckdb.DuckDBPyConnection, *, skip_dedup: bool = False) -> None:
    """Run the standard post-import pipeline.

    Order matters: deduplication first so the vestigial-column backfill sees
    one row per ``(workout_hash, stat_type)`` instead of the duplicated bulk
    set, then the backfill, then the daily-stats rebuild that reads the
    finalized ``records`` table.

    ``skip_dedup`` short-circuits :func:`deduplicate_tables` (Tier 2 of issue
    #62). When the orchestrator primed every importer with an existing-hash
    snapshot the per-element handlers already dropped every row that was on
    disk, so the bulk staging buffers carry only genuinely-new rows --
    Phase 4 dedup has nothing to do, and skipping its per-table DELETE
    pass also avoids the DuckDB MVCC tombstones that would otherwise
    balloon the on-disk file on every re-import. The vestigial backfill
    and daily-stats rebuild still run because they materialise per-row
    derived columns that need the newly-added rows.
    """
    if skip_dedup:
        _logger.info("Finalizing import: skip dedup (incremental) -> backfill -> daily stats")
    else:
        _logger.info("Finalizing import: deduplicate -> backfill -> daily stats")
        deduplicate_tables(conn)
    populate_workout_vestigial_columns(conn)
    rebuild_daily_stats(conn)
    _logger.info("Finalize complete")
