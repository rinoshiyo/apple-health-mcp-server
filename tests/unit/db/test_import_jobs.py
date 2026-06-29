"""Tests for ``apple_health_mcp.db.import_jobs`` — registry CRUD seam.

v0.5 (issue #157) introduced ``import_jobs`` as the persistence layer
that backs the new async ``import_zip`` MCP tool and its
``get_import_status`` companion. These unit tests pin the CRUD surface
on a clean in-memory schema so the higher-level tool tests
(``tests/unit/server/test_import_jobs_async.py``) can lean on the
registry behaviour without re-asserting every detail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.db import import_jobs as job_registry


def _seed_conn() -> tuple:
    conn = get_in_memory_connection()
    ensure_schema(conn)
    return conn, Lock()


def test_generate_job_id_uses_utc_timestamp_and_sha_prefix() -> None:
    """``generate_job_id`` format: ``ij_YYYYMMDD_HHMMSS_<sha-prefix>_<rand>``."""
    fixed = datetime(2026, 6, 28, 6, 23, 0, tzinfo=UTC)
    job_id = job_registry.generate_job_id("a3f9d2c1" + "0" * 56, now=fixed)
    assert job_id.startswith("ij_20260628_062300_a3f9d2c1_")
    # 4 random hex chars at the tail keep two same-second same-sha calls
    # distinct; the prefix is deterministic, the suffix is not.
    assert len(job_id) == len("ij_20260628_062300_a3f9d2c1_") + 4


def test_insert_queued_persists_row_and_returns_timestamp() -> None:
    """``insert_queued`` writes the row and returns the queued_at it stamped."""
    conn, lock = _seed_conn()
    stamp = datetime(2026, 6, 28, 6, 23, 0, tzinfo=UTC)
    returned = job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_test_1",
        source_id="deadbeef",
        source_sha256="deadbeef" + "0" * 56,
        queued_at=stamp,
    )
    assert returned == stamp
    job = job_registry.get_job(conn, lock, "ij_test_1")
    assert job is not None
    assert job.status == job_registry.STATUS_QUEUED
    assert job.source_id == "deadbeef"
    assert job.queued_at == stamp


def test_mark_running_then_done_records_full_lifecycle() -> None:
    """Queued -> running (with phase) -> done writes every result field."""
    conn, lock = _seed_conn()
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_test_2",
        source_id="cafebabe",
        source_sha256="cafebabe" + "0" * 56,
    )
    job_registry.mark_running(conn, lock, "ij_test_2", phase="extracting")
    job_registry.mark_phase(conn, lock, "ij_test_2", "xml_parsing")
    job_registry.mark_done(
        conn,
        lock,
        "ij_test_2",
        records_added=100,
        workouts_added=5,
        ecg_readings_added=2,
        route_points_added=10,
        duration_secs=1.5,
        already_imported_at=None,
    )
    job = job_registry.get_job(conn, lock, "ij_test_2")
    assert job is not None
    assert job.status == job_registry.STATUS_DONE
    assert job.records_added == 100
    assert job.workouts_added == 5
    assert job.ecg_readings_added == 2
    assert job.route_points_added == 10
    assert job.duration_secs == 1.5
    assert job.already_imported_at is None
    # done clears phase (the wire envelope is "ok" not "running", so
    # phase must not leak into the polled status).
    assert job.phase is None


def test_mark_error_records_reason_and_message() -> None:
    """The error path stamps reason / message and a non-NULL failed_at."""
    conn, lock = _seed_conn()
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_test_3",
        source_id="deadbabe",
        source_sha256="deadbabe" + "0" * 56,
    )
    job_registry.mark_error(
        conn,
        lock,
        "ij_test_3",
        reason="zip_extract_failed",
        message="archive corruption",
    )
    job = job_registry.get_job(conn, lock, "ij_test_3")
    assert job is not None
    assert job.status == job_registry.STATUS_ERROR
    assert job.error_reason == "zip_extract_failed"
    assert job.error_message == "archive corruption"
    assert job.failed_at is not None


def test_find_active_by_sha_returns_newest_active_job() -> None:
    """The lookup picks the newest queued / running row for the same sha."""
    conn, lock = _seed_conn()
    sha = "feedface" + "0" * 56
    older = datetime(2026, 6, 28, 5, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 28, 6, 0, 0, tzinfo=UTC)
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_old",
        source_id="feedface",
        source_sha256=sha,
        queued_at=older,
    )
    job_registry.mark_running(conn, lock, "ij_old")
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_new",
        source_id="feedface",
        source_sha256=sha,
        queued_at=newer,
    )
    hit = job_registry.find_active_by_sha(conn, lock, sha)
    assert hit is not None
    assert hit.job_id == "ij_new"


def test_find_active_by_sha_ignores_terminal_jobs() -> None:
    """Done / error rows do not satisfy the multi-launch guard."""
    conn, lock = _seed_conn()
    sha = "bea2f00d" + "0" * 56
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_done",
        source_id="bea2f00d",
        source_sha256=sha,
    )
    job_registry.mark_done(
        conn,
        lock,
        "ij_done",
        records_added=0,
        workouts_added=0,
        ecg_readings_added=0,
        route_points_added=0,
        duration_secs=0.1,
        already_imported_at=None,
    )
    assert job_registry.find_active_by_sha(conn, lock, sha) is None


def test_sweep_orphan_jobs_rewrites_queued_and_running() -> None:
    """The boot sweep flips every non-terminal row to error / orphan reason."""
    conn, lock = _seed_conn()
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_orphan_q",
        source_id="11111111",
        source_sha256="1" * 64,
    )
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_orphan_r",
        source_id="22222222",
        source_sha256="2" * 64,
    )
    job_registry.mark_running(conn, lock, "ij_orphan_r")
    # A finished row stays put.
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_done",
        source_id="33333333",
        source_sha256="3" * 64,
    )
    job_registry.mark_done(
        conn,
        lock,
        "ij_done",
        records_added=10,
        workouts_added=1,
        ecg_readings_added=0,
        route_points_added=0,
        duration_secs=0.1,
        already_imported_at=None,
    )

    swept = job_registry.sweep_orphan_jobs(
        conn,
        lock,
        failed_at=datetime(2026, 6, 28, 7, 0, 0, tzinfo=UTC),
    )
    assert swept == 2
    for jid in ("ij_orphan_q", "ij_orphan_r"):
        job = job_registry.get_job(conn, lock, jid)
        assert job is not None
        assert job.status == job_registry.STATUS_ERROR
        assert job.error_reason == job_registry.ORPHAN_REASON
        assert job.error_message == job_registry.ORPHAN_MESSAGE
    done = job_registry.get_job(conn, lock, "ij_done")
    assert done is not None and done.status == job_registry.STATUS_DONE


def test_sweep_orphan_jobs_is_no_op_on_clean_table() -> None:
    """The sweep returns 0 on an empty / all-terminal table without raising."""
    conn, lock = _seed_conn()
    assert job_registry.sweep_orphan_jobs(conn, lock) == 0


def test_get_job_returns_none_for_unknown_id() -> None:
    conn, lock = _seed_conn()
    assert job_registry.get_job(conn, lock, "ij_nope") is None


def test_insert_queued_defaults_queued_at_to_now() -> None:
    """When ``queued_at`` is omitted the helper stamps UTC now."""
    conn, lock = _seed_conn()
    before = datetime.now(UTC) - timedelta(seconds=1)
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_now",
        source_id="44444444",
        source_sha256="4" * 64,
    )
    after = datetime.now(UTC) + timedelta(seconds=1)
    job = job_registry.get_job(conn, lock, "ij_now")
    assert job is not None
    assert before <= job.queued_at <= after


def test_mark_running_defaults_started_at_to_now() -> None:
    conn, lock = _seed_conn()
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_mr",
        source_id="55555555",
        source_sha256="5" * 64,
    )
    before = datetime.now(UTC) - timedelta(seconds=1)
    job_registry.mark_running(conn, lock, "ij_mr")
    after = datetime.now(UTC) + timedelta(seconds=1)
    job = job_registry.get_job(conn, lock, "ij_mr")
    assert job is not None
    assert job.started_at is not None
    assert before <= job.started_at <= after


def test_mark_done_defaults_completed_at_and_mark_error_defaults_failed_at() -> None:
    """Both terminal helpers fall back to ``datetime.now(UTC)`` when arg omitted."""
    conn, lock = _seed_conn()
    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_md",
        source_id="66666666",
        source_sha256="6" * 64,
    )
    job_registry.mark_done(
        conn,
        lock,
        "ij_md",
        records_added=1,
        workouts_added=0,
        ecg_readings_added=0,
        route_points_added=0,
        duration_secs=0.1,
        already_imported_at=None,
    )
    job_done = job_registry.get_job(conn, lock, "ij_md")
    assert job_done is not None and job_done.completed_at is not None

    job_registry.insert_queued(
        conn,
        lock,
        job_id="ij_me",
        source_id="77777777",
        source_sha256="7" * 64,
    )
    job_registry.mark_error(conn, lock, "ij_me", reason="x", message="y")
    job_err = job_registry.get_job(conn, lock, "ij_me")
    assert job_err is not None and job_err.failed_at is not None


def test_generate_job_id_defaults_now_argument() -> None:
    """Omitting ``now`` falls back to ``datetime.now(UTC)`` without crashing."""
    job_id = job_registry.generate_job_id("c" * 64)
    assert job_id.startswith("ij_")
    # The prefix block is 8 hex chars (sha prefix); confirm the spec
    # shape independently of the timestamp.
    parts = job_id.split("_")
    # ij / YYYYMMDD / HHMMSS / <8-hex sha> / <4-hex rand>
    assert len(parts) == 5
    assert parts[3] == "c" * 8
    assert len(parts[4]) == 4
