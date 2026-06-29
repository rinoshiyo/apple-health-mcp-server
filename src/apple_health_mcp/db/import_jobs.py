"""``import_jobs`` table accessors — async-import job state persistence.

v0.5 (issue #157) layer over the ``import_jobs`` DuckDB table created in
:mod:`apple_health_mcp.db.schema`. ``import_zip`` writes a ``queued`` row
here before spawning a worker thread; the worker updates the row through
``running`` / ``done`` / ``error`` and the new ``get_import_status`` MCP
tool polls it back. Persisting to DuckDB (not an in-process dict) is what
makes the flow survive a server restart mid-import: the boot-time
:func:`sweep_orphan_jobs` rewrites every stuck row to
``error / server_restarted_while_running`` so the multi-launch guard
cannot wedge on a worker that no longer exists.

The module owns nothing about thread-spawning or the importer pipeline —
it is the CRUD seam. Every entry point takes the same
``(conn, lock)`` pair the rest of the server uses, because the DuckDB
Python binding is not thread-safe and the writer-and-readers pattern
relies on serialising every ``conn.execute`` under the shared lock.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb


# Status vocabulary. A finite enumeration kept here as module-level
# strings so callers do not have to import an Enum (the table column is
# a plain VARCHAR; an Enum here would be ornamental).
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


# Reason string written by :func:`sweep_orphan_jobs` when a
# ``queued`` / ``running`` row outlives the worker that owned it
# (= the server process was killed / restarted mid-import). The
# corresponding ``get_import_status`` envelope surfaces this verbatim
# so an agent re-polling an old ``job_id`` after a restart gets a
# definite "import never completed" rather than an indefinite
# "still running" lie.
ORPHAN_REASON = "server_restarted_while_running"
ORPHAN_MESSAGE = "Server restarted before the import worker completed."


@dataclass(frozen=True)
class ImportJob:
    """In-memory view of one ``import_jobs`` row.

    Returned by :func:`get_job` and :func:`find_active_by_sha` so callers
    can pull fields by name instead of remembering the DuckDB column
    order. ``status`` is always one of the ``STATUS_*`` constants above;
    every other field maps directly to the SQL column of the same name
    and is ``None`` when the column is NULL in the row.
    """

    job_id: str
    source_id: str
    source_sha256: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    phase: str | None
    records_added: int | None
    workouts_added: int | None
    ecg_readings_added: int | None
    route_points_added: int | None
    duration_secs: float | None
    already_imported_at: str | None
    error_reason: str | None
    error_message: str | None


# Column order pinned in one place so the ``SELECT *``-style fetch and
# the row-to-:class:`ImportJob` mapping stay in lock-step. Lifting the
# tuple to a module constant means a future schema migration that
# adds a column fails the mapping with a clear IndexError rather than
# silently misaligning fields.
_COLUMNS = (
    "job_id",
    "source_id",
    "source_sha256",
    "status",
    "queued_at",
    "started_at",
    "completed_at",
    "failed_at",
    "phase",
    "records_added",
    "workouts_added",
    "ecg_readings_added",
    "route_points_added",
    "duration_secs",
    "already_imported_at",
    "error_reason",
    "error_message",
)

_SELECT_COLUMNS = ", ".join(_COLUMNS)


def _row_to_job(row: tuple[Any, ...]) -> ImportJob:
    """Map a tuple in :data:`_COLUMNS` order back to an :class:`ImportJob`."""
    already_at = row[14]
    return ImportJob(
        job_id=str(row[0]),
        source_id=str(row[1]),
        source_sha256=str(row[2]),
        status=str(row[3]),
        queued_at=row[4],
        started_at=row[5],
        completed_at=row[6],
        failed_at=row[7],
        phase=row[8],
        records_added=row[9],
        workouts_added=row[10],
        ecg_readings_added=row[11],
        route_points_added=row[12],
        duration_secs=row[13],
        # ``already_imported_at`` is surfaced to ``get_import_status``
        # as a string field so the wire envelope matches the synchronous
        # ``import_zip`` shape (which serialises via str(value)).
        already_imported_at=None if already_at is None else str(already_at),
        error_reason=row[15],
        error_message=row[16],
    )


def generate_job_id(sha256: str, now: datetime | None = None) -> str:
    """Build the ``ij_<UTC timestamp>_<sha-prefix>_<rand>`` job id.

    The format is the spec from issue #157: ``ij_YYYYMMDD_HHMMSS_<8hex>``
    keeps the value sortable by submission order in human eyes
    (logs, ``import_jobs`` table inspection during dogfood) AND
    collision-resistant: the 8 hex chars are the sha256 prefix the
    user already sees in ``list_zips`` output, plus a 4 hex char
    random suffix so two ``import_zip`` calls landing in the same
    wall-clock second on the same source still produce distinct ids.
    """
    moment = now or datetime.now(UTC)
    prefix = sha256[:8]
    # ``secrets.token_hex(2)`` -> 4 hex chars. Random tail is overkill
    # in the steady state but keeps two same-second same-sha attempts
    # from colliding (e.g. a flaky agent retrying inside one second
    # before the queued-job lookup short-circuits).
    suffix = secrets.token_hex(2)
    return f"ij_{moment.strftime('%Y%m%d_%H%M%S')}_{prefix}_{suffix}"


def insert_queued(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    *,
    job_id: str,
    source_id: str,
    source_sha256: str,
    queued_at: datetime | None = None,
) -> datetime:
    """Insert a row with ``status='queued'`` and return the stamped time.

    Returning the queued_at value lets the caller fold it into the
    immediate-response envelope without a follow-up ``SELECT``: the
    timestamp the worker thread will later read back via :func:`get_job`
    matches the one ``import_zip`` reports to the agent on the same call.
    """
    moment = queued_at or datetime.now(UTC)
    with lock:
        conn.execute(
            "INSERT INTO import_jobs (job_id, source_id, source_sha256, "
            "status, queued_at) VALUES (?, ?, ?, ?, ?)",
            [job_id, source_id, source_sha256, STATUS_QUEUED, moment],
        )
    return moment


def mark_running(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
    *,
    started_at: datetime | None = None,
    phase: str | None = None,
) -> None:
    """Transition a ``queued`` row to ``running`` (optionally with a phase)."""
    moment = started_at or datetime.now(UTC)
    with lock:
        conn.execute(
            "UPDATE import_jobs SET status=?, started_at=?, phase=? WHERE job_id=?",
            [STATUS_RUNNING, moment, phase, job_id],
        )


def mark_phase(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
    phase: str,
) -> None:
    """Update the ``phase`` column on a ``running`` row.

    ``get_import_status`` reports this in its ``phase`` envelope key so
    a polling client can show progress more granular than the binary
    ``queued`` / ``running`` flip.
    """
    with lock:
        conn.execute(
            "UPDATE import_jobs SET phase=? WHERE job_id=?",
            [phase, job_id],
        )


def mark_done(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
    *,
    records_added: int,
    workouts_added: int,
    ecg_readings_added: int,
    route_points_added: int,
    duration_secs: float,
    already_imported_at: str | None,
    completed_at: datetime | None = None,
) -> None:
    """Finalise a job's row with the import statistics from the worker."""
    moment = completed_at or datetime.now(UTC)
    with lock:
        conn.execute(
            "UPDATE import_jobs SET status=?, completed_at=?, phase=NULL, "
            "records_added=?, workouts_added=?, ecg_readings_added=?, "
            "route_points_added=?, duration_secs=?, already_imported_at=? "
            "WHERE job_id=?",
            [
                STATUS_DONE,
                moment,
                records_added,
                workouts_added,
                ecg_readings_added,
                route_points_added,
                duration_secs,
                already_imported_at,
                job_id,
            ],
        )


def mark_error(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
    *,
    reason: str,
    message: str,
    failed_at: datetime | None = None,
) -> None:
    """Move a row to ``status='error'`` with the failure reason + message."""
    moment = failed_at or datetime.now(UTC)
    with lock:
        conn.execute(
            "UPDATE import_jobs SET status=?, failed_at=?, "
            "error_reason=?, error_message=? WHERE job_id=?",
            [STATUS_ERROR, moment, reason, message, job_id],
        )


def get_job(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
) -> ImportJob | None:
    """Fetch one row by ``job_id`` or return ``None`` if absent."""
    with lock:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM import_jobs WHERE job_id=?",
            [job_id],
        ).fetchone()
    if row is None:
        return None
    return _row_to_job(row)


def find_active_by_sha(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    source_sha256: str,
) -> ImportJob | None:
    """Return the newest ``queued`` / ``running`` job for ``source_sha256``.

    Powers the multi-launch guard inside ``import_zip``: if there is
    already an in-flight worker for this exact ZIP (byte-identical
    sha256), return its ``job_id`` instead of spawning a duplicate. A
    rare race is possible if two ``import_zip`` calls land between
    each other's INSERT and SELECT, but the worker thread itself
    serialises on the writable conn lock once it starts importing, so
    the second worker would simply observe the ``imports.source_zip_sha256``
    row the first one just wrote and no-op.
    """
    with lock:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM import_jobs "
            "WHERE source_sha256=? AND status IN (?, ?) "
            "ORDER BY queued_at DESC LIMIT 1",
            [source_sha256, STATUS_QUEUED, STATUS_RUNNING],
        ).fetchone()
    if row is None:
        return None
    return _row_to_job(row)


def sweep_orphan_jobs(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    *,
    failed_at: datetime | None = None,
) -> int:
    """Rewrite leftover ``queued`` / ``running`` rows to ``error``.

    Called once at server boot. Any job whose worker thread was killed
    by the server process exiting is necessarily orphaned now: the
    worker is gone and nobody will ever flip the status. Returning
    those rows to a terminal state ensures the multi-launch guard does
    not stay tripped (= the next ``import_zip`` for the same sha would
    otherwise loop back the dead ``job_id``).

    Returns the row count that was swept, both as a smoke value for
    tests and so server boot can log "swept N stale import jobs" for
    forensics.
    """
    moment = failed_at or datetime.now(UTC)
    with lock:
        conn.execute(
            "UPDATE import_jobs SET status=?, failed_at=?, "
            "error_reason=?, error_message=? "
            "WHERE status IN (?, ?)",
            [
                STATUS_ERROR,
                moment,
                ORPHAN_REASON,
                ORPHAN_MESSAGE,
                STATUS_QUEUED,
                STATUS_RUNNING,
            ],
        )
        # ``conn.rowcount`` is unreliable across DuckDB versions; query the
        # one cheap counter that is stable: the number of rows that NOW
        # carry the orphan reason at this exact ``failed_at`` instant.
        row = conn.execute(
            "SELECT COUNT(*) FROM import_jobs WHERE error_reason=? AND failed_at=?",
            [ORPHAN_REASON, moment],
        ).fetchone()
    # ``COUNT(*)`` always returns a single row; the assert is for the
    # static type checker (``fetchone() -> tuple | None``).
    assert row is not None
    return int(row[0])


__all__ = [
    "ORPHAN_MESSAGE",
    "ORPHAN_REASON",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "ImportJob",
    "find_active_by_sha",
    "generate_job_id",
    "get_job",
    "insert_queued",
    "mark_done",
    "mark_error",
    "mark_phase",
    "mark_running",
    "sweep_orphan_jobs",
]
