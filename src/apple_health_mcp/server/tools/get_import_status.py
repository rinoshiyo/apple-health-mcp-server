"""``get_import_status`` MCP tool — companion to v0.5 async ``import_zip``.

Poll the state of an import job spawned by ``import_zip``. Mirrors the
job state machine in :mod:`apple_health_mcp.db.import_jobs`:

* ``queued`` — row INSERTed, worker thread not yet picked it up.
* ``running`` — worker active; ``phase`` column reports the current
  importer stage (``extracting`` / ``xml_parsing`` / ``ecg`` / ``gpx``
  / ``finalize``) and ``elapsed_secs`` is wall-clock since the worker
  flipped to running.
* ``ok`` — worker finished; row carries the same statistics the v0.4
  synchronous ``import_zip`` returned (records_added, workouts_added,
  ecg_readings_added, route_points_added, duration_secs).
* ``error`` — worker raised (or boot-sweep flagged the row as orphaned
  by a server restart); ``reason`` / ``message`` describe the failure.

Issue #157 reasoning: pure poll, no waiting. The agent's natural
cadence (~30s) gives the importer time to make visible progress
between calls; the tool itself returns in microseconds (one indexed
SELECT against ``import_jobs``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.db import import_jobs as job_registry
from apple_health_mcp.server.query import run_query_payload

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Poll the status of an import_zip job. Pass the ``job_id`` returned "
    "by import_zip (an ``ij_YYYYMMDD_HHMMSS_<sha-prefix>_<rand>`` string). "
    "Returns one of: {status: 'queued', job_id, queued_at, message} -- "
    "row inserted, worker not yet started; {status: 'running', job_id, "
    "phase, elapsed_secs, message} -- worker active, ``phase`` is one of "
    "'extracting' / 'xml_parsing' / 'ecg' / 'gpx' / 'finalize'; "
    "{status: 'ok', job_id, records_added, workouts_added, "
    "ecg_readings_added, route_points_added, duration_secs, "
    "already_imported_at, message} -- import succeeded, same shape as "
    "the legacy synchronous import_zip envelope; or {status: 'error', "
    "job_id, reason, message} -- worker raised, or the server restarted "
    "mid-import and the orphan sweep flagged this row "
    "(reason='server_restarted_while_running'). Unknown job_id surfaces "
    "as {status: 'error', reason: 'job_not_found', message}. Polling "
    "cost is one indexed SELECT (microseconds); no client back-off "
    "required, but ~30s between polls is a sensible default since "
    "imports take 45s-several minutes depending on hardware."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_import_status(
        job_id: Annotated[
            str,
            Field(
                description=(
                    "The ``job_id`` returned by import_zip when it queued the background worker."
                ),
            ),
        ],
    ) -> str:
        return _get_import_status_dispatch(conn, lock, job_id)


def _get_import_status_dispatch(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
) -> str:
    """Synchronous body; split so tests can drive it directly."""
    job = job_registry.get_job(conn, lock, job_id)
    if job is None:
        return run_query_payload(
            {
                "status": "error",
                "job_id": job_id,
                "reason": "job_not_found",
                "message": (
                    f"No import job with job_id={job_id!r}. The id may "
                    "be mis-typed, or the job may pre-date a fresh "
                    "DuckDB file. Call import_zip again to start a "
                    "new import."
                ),
            }
        )

    if job.status == job_registry.STATUS_QUEUED:
        return run_query_payload(
            {
                "status": "queued",
                "job_id": job.job_id,
                "queued_at": str(job.queued_at),
                "message": (
                    "Job queued; worker thread has not yet picked it "
                    "up. Poll again in 5-10 seconds."
                ),
            }
        )

    if job.status == job_registry.STATUS_RUNNING:
        elapsed = _elapsed_seconds_since(job.started_at)
        return run_query_payload(
            {
                "status": "running",
                "job_id": job.job_id,
                "phase": job.phase,
                "elapsed_secs": elapsed,
                "message": (
                    f"Import in progress (phase={job.phase}, "
                    f"elapsed={elapsed:.1f}s). Poll again in 10-30 "
                    "seconds."
                ),
            }
        )

    if job.status == job_registry.STATUS_DONE:
        return run_query_payload(
            {
                "status": "ok",
                "job_id": job.job_id,
                "records_added": job.records_added,
                "workouts_added": job.workouts_added,
                "ecg_readings_added": job.ecg_readings_added,
                "route_points_added": job.route_points_added,
                "duration_secs": job.duration_secs,
                "already_imported_at": job.already_imported_at,
                "message": (
                    f"Imported {job.records_added} records / "
                    f"{job.workouts_added} workouts in "
                    f"{job.duration_secs:.1f}s. Read tools now return "
                    "real data."
                ),
            }
        )

    # status == STATUS_ERROR
    return run_query_payload(
        {
            "status": "error",
            "job_id": job.job_id,
            "reason": job.error_reason,
            "message": job.error_message,
        }
    )


def _elapsed_seconds_since(started_at: datetime | None) -> float:
    """Compute wall-clock seconds since the worker flipped to ``running``.

    Returns ``0.0`` defensively when ``started_at`` is absent. The
    invariant is that ``status='running'`` rows always carry a non-NULL
    ``started_at`` (set in the same UPDATE that flipped the status); the
    None branch only fires under a write that violated the invariant
    (e.g. an external tool fiddling the DB) and shrinking the report
    to 0.0 is the safer fallback than raising or fabricating a value.
    """
    if started_at is None:  # pragma: no cover - invariant: running implies started_at
        return 0.0
    delta = datetime.now(UTC) - started_at
    return round(delta.total_seconds(), 1)


__all__ = ["DESCRIPTION", "register"]
