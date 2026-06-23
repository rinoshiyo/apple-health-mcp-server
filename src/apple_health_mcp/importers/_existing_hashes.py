"""In-memory hash snapshot for incremental re-imports (issue #62).

When :func:`apple_health_mcp.importers.run_import` is invoked on a
database that already holds prior import data, the orchestrator loads
every dedup-keyed hash (and the ``activity_summaries.date_components``
natural key) into Python sets and threads them into each importer
handler. The handler then checks the freshly-computed hash against the
set BEFORE appending the row to its Arrow flush buffer, so the new
import contributes only the genuinely-new rows. The legacy Phase 4
``deduplicate_tables`` pass is skipped in that case (logged INFO from
:func:`apple_health_mcp.importers.dedup.finalize_import`) because there
is nothing left for it to do.

The sets are constructed once at import start and dropped when
``run_import`` closes its connection -- they exist only for the
duration of the import subcommand and never reach the ``serve``
process. Memory footprint scales with row count: at the current
5.1 M-row real-export size every set fits comfortably in the importer's
1 GB budget; ``project_autonomous_implementation_loop`` and issue #62's
Risks section both call out that a >50 M-row export would need a
disk-backed alternative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


@dataclass
class ExistingHashes:
    """Sets of hash / natural-key strings already present on disk.

    One set per dedup-keyed table. The names mirror the importer column
    they gate (``records.record_hash``, ``workouts.workout_hash``,
    ``route_points.point_hash``, ``ecg_readings.ecg_hash``,
    ``correlations.correlation_hash``,
    ``activity_summaries.date_components``).
    """

    records: set[str] = field(default_factory=set)
    workouts: set[str] = field(default_factory=set)
    route_points: set[str] = field(default_factory=set)
    ecg_readings: set[str] = field(default_factory=set)
    correlations: set[str] = field(default_factory=set)
    activity_summaries: set[str] = field(default_factory=set)


def load_existing_hashes(conn: duckdb.DuckDBPyConnection) -> ExistingHashes:
    """Snapshot every dedup hash currently on disk into Python sets.

    Each set holds the distinct non-NULL hash values for one table; the
    handlers consume them via ``hash in <set>`` which is O(1) amortised.
    ``SELECT DISTINCT`` keeps the working set tight when prior imports
    inserted overlapping rows that the dedup pass has not yet collapsed
    (rare today but possible on a pre-#60 on-disk DB whose
    ``CREATE OR REPLACE TABLE`` dedup left stale tombstones).
    """
    hashes = ExistingHashes()
    hashes.records = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT record_hash FROM records WHERE record_hash IS NOT NULL"
        ).fetchall()
    }
    hashes.workouts = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT workout_hash FROM workouts WHERE workout_hash IS NOT NULL"
        ).fetchall()
    }
    hashes.route_points = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT point_hash FROM route_points WHERE point_hash IS NOT NULL"
        ).fetchall()
    }
    hashes.ecg_readings = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT ecg_hash FROM ecg_readings WHERE ecg_hash IS NOT NULL"
        ).fetchall()
    }
    hashes.correlations = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT correlation_hash FROM correlations WHERE correlation_hash IS NOT NULL"
        ).fetchall()
    }
    # Empty ``date_components`` strings are filtered out as well as NULLs
    # because :meth:`_handle_activity_summary` defaults a missing
    # ``dateComponents`` attribute to ``""``. A stale on-disk row with
    # ``date_components = ''`` from a pre-#62 malformed import would
    # otherwise populate the set with ``""`` and the next import would
    # silently skip every ActivitySummary whose ``dateComponents``
    # attribute Apple happens to drop (an edge seen on iCloud-restored
    # devices). Hash columns can't be empty -- ``compute_hash`` always
    # returns 64-char hex -- so the analogous defense isn't needed for
    # the other five sets.
    hashes.activity_summaries = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT date_components FROM activity_summaries "
            "WHERE date_components IS NOT NULL AND date_components != ''"
        ).fetchall()
    }
    _logger.info(
        "Loaded incremental hash sets: records=%d workouts=%d points=%d "
        "ecg=%d correlations=%d activity=%d",
        len(hashes.records),
        len(hashes.workouts),
        len(hashes.route_points),
        len(hashes.ecg_readings),
        len(hashes.correlations),
        len(hashes.activity_summaries),
    )
    return hashes
