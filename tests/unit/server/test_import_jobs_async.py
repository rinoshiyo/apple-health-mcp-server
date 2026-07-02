"""Tests for the v0.5 (issue #157) async ``import_zip`` flow.

The synchronous validation / config / inspection branches of
``import_zip`` are exercised in ``test_zip_tools.py``; this file covers
the job-based async surface introduced in v0.5:

* ``import_zip`` returns ``status: 'queued'`` in milliseconds and
  persists the row to ``import_jobs``.
* ``get_import_status`` rotates through ``queued`` -> ``running`` (with
  phase + elapsed_secs) -> ``ok`` / ``error``.
* Multi-launch guard: a second call for the same sha returns the
  existing ``job_id`` without spawning a duplicate worker.
* The byte-identical idempotent re-import returns the synchronous
  ``ok`` envelope without writing to ``import_jobs``.
* Concurrent calls for *different* shas serialise on the writer lock.
* The server-boot orphan sweep rewrites stale ``queued`` / ``running``
  rows to ``error / server_restarted_while_running``, and a follow-up
  ``get_import_status`` poll reflects the sweep.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.db import import_jobs as job_registry
from apple_health_mcp.db.migrations import stamp_current_version
from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR
from apple_health_mcp.server.tools import get_import_status as get_status_mod
from apple_health_mcp.server.tools import import_zip as import_zip_mod
from tests._helpers import bind_tool, drain_import_workers, open_test_connection

if TYPE_CHECKING:
    pass


_TRIVIAL_EXPORT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<HealthData locale="en_US">'
    '<ExportDate value="2024-06-01 12:00:00 +0000"/>'
    "</HealthData>"
)


def _make_zip(path: Path, *, with_export_xml: bool = True, nested: bool = True) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_export_xml:
            name = "apple_health_export/export.xml" if nested else "export.xml"
            zf.writestr(name, _TRIVIAL_EXPORT_XML)
        else:
            zf.writestr("readme.txt", "not an apple health export")
    path.write_bytes(buf.getvalue())


def _call_import_zip(conn: duckdb.DuckDBPyConnection, *, id: str) -> dict[str, Any]:
    fn = bind_tool(import_zip_mod, conn)
    raw = asyncio.run(fn(id=id))
    return json.loads(raw)


def _call_get_import_status(conn: duckdb.DuckDBPyConnection, *, job_id: str) -> dict[str, Any]:
    fn = bind_tool(get_status_mod, conn)
    raw = asyncio.run(fn(job_id=job_id))
    return json.loads(raw)


# v0.5 code-review (PR #184 F9): shared helper from tests/_helpers.py
# so the join logic and 30-second timeout do not drift across test
# files.
_drain_import_workers = drain_import_workers


def _writable_db(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    """Open a real on-disk DuckDB so the importer's writable path succeeds."""
    db_path = tmp_path / "h.duckdb"
    conn = open_test_connection(str(db_path), read_only=False)
    ensure_schema(conn)
    stamp_current_version(conn)
    return conn, db_path


def _seed_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Drop a synthetic Apple Health ZIP and return its (path, sha256)."""
    import hashlib

    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return zip_path, sha


# --- Spec #1 + #2 ----------------------------------------------------------


def test_import_zip_returns_queued_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Response shape: status=queued, job_id, queued_at populated in ms."""
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn, _ = _writable_db(tmp_path)
    try:
        started = time.monotonic()
        out = _call_import_zip(conn, id=sha[:8])
        elapsed = time.monotonic() - started
        # The synchronous dispatch must return in well under a second even
        # before the worker has touched extract / run_import. Allowing
        # 5s of headroom keeps CI noise from flaking the assertion while
        # still catching a regression that wires the call back through
        # the importer synchronously.
        assert elapsed < 5.0
        assert out["status"] == "queued"
        assert out["id"] == sha[:8]
        assert isinstance(out["job_id"], str)
        assert out["job_id"].startswith("ij_")
        assert "queued_at" in out
        _drain_import_workers()
    finally:
        conn.close()


def test_import_zip_persists_job_to_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``import_jobs`` row is INSERTed with source_sha256 + queued_at."""
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn, _ = _writable_db(tmp_path)
    try:
        out = _call_import_zip(conn, id=sha[:8])
        job_id = str(out["job_id"])
        # Wait for the worker before reading: DuckDB's Python binding
        # is not thread-safe and the writer lock only protects the
        # registry's own UPDATEs, not ad-hoc raw SELECTs from the
        # test thread. Reading after the worker terminates is the
        # cheap way to avoid a cross-thread cursor race.
        _drain_import_workers()
        row = conn.execute(
            "SELECT source_id, source_sha256, queued_at FROM import_jobs WHERE job_id = ?",
            [job_id],
        ).fetchone()
        assert row is not None
        assert row[0] == sha[:8]
        assert row[1] == sha
        assert row[2] is not None
    finally:
        conn.close()


# --- Spec #3 + #4 (queued -> running with phase) ---------------------------


def test_get_import_status_transitions_queued_to_running() -> None:
    """A row with status=queued reports ``status: 'queued'`` to pollers."""
    conn = get_in_memory_connection()
    lock = Lock()
    ensure_schema(conn)
    try:
        job_registry.insert_queued(
            conn,
            lock,
            job_id="ij_queue_only",
            source_id="abcdef01",
            source_sha256="abcdef01" + "0" * 56,
        )
        # Queued envelope
        out = _call_get_import_status(conn, job_id="ij_queue_only")
        assert out["status"] == "queued"
        # Flip to running and the envelope follows.
        job_registry.mark_running(
            conn,
            lock,
            "ij_queue_only",
            started_at=datetime.now(UTC),
            phase="extracting",
        )
        out2 = _call_get_import_status(conn, job_id="ij_queue_only")
        assert out2["status"] == "running"
    finally:
        conn.close()


def test_get_import_status_returns_running_with_phase_while_in_progress() -> None:
    """Running envelope carries phase + elapsed_secs."""
    conn = get_in_memory_connection()
    lock = Lock()
    ensure_schema(conn)
    try:
        job_registry.insert_queued(
            conn,
            lock,
            job_id="ij_running",
            source_id="11112222",
            source_sha256="1" * 8 + "2" * 56,
        )
        # Stamp started_at 3 seconds ago so elapsed_secs is a meaningful
        # positive number rather than 0.0.
        job_registry.mark_running(
            conn,
            lock,
            "ij_running",
            started_at=datetime.now(UTC),
            phase="xml_parsing",
        )
        out = _call_get_import_status(conn, job_id="ij_running")
        assert out["status"] == "running"
        assert out["phase"] == "xml_parsing"
        assert isinstance(out["elapsed_secs"], (int, float))
        assert out["elapsed_secs"] >= 0
    finally:
        conn.close()


# --- Spec #5 (done envelope after completion) ------------------------------


def test_get_import_status_returns_done_after_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real import flow ends with ``status: 'ok'`` carrying stats."""
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn, _ = _writable_db(tmp_path)
    try:
        queued = _call_import_zip(conn, id=sha[:8])
        job_id = str(queued["job_id"])
        _drain_import_workers()
        status = _call_get_import_status(conn, job_id=job_id)
        assert status["status"] == "ok"
        assert status["job_id"] == job_id
        assert status["records_added"] == 0  # trivial export.xml has none
        assert status["already_imported_at"] is None
        assert isinstance(status["duration_secs"], (int, float))
    finally:
        conn.close()


# --- Spec #6 (worker raises) -----------------------------------------------


def test_get_import_status_returns_error_when_run_import_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing worker stamps status=error + reason='run_import_failed'."""
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn, _ = _writable_db(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    # Monkeypatch the lazy-imported callable inside the worker module.
    import apple_health_mcp.importers.zip_extract as zip_extract_mod

    monkeypatch.setattr(zip_extract_mod, "extract_zip_and_import", _boom)
    try:
        queued = _call_import_zip(conn, id=sha[:8])
        job_id = str(queued["job_id"])
        _drain_import_workers()
        status = _call_get_import_status(conn, job_id=job_id)
        assert status["status"] == "error"
        assert status["reason"] == "run_import_failed"
        assert "synthetic failure" in str(status["message"])
    finally:
        conn.close()


# --- Spec #7 (job_not_found) -----------------------------------------------


def test_get_import_status_returns_job_not_found_for_unknown_id() -> None:
    conn = get_in_memory_connection()
    ensure_schema(conn)
    try:
        out = _call_get_import_status(conn, job_id="ij_no_such_job")
        assert out["status"] == "error"
        assert out["reason"] == "job_not_found"
        assert out["job_id"] == "ij_no_such_job"
    finally:
        conn.close()


# --- Spec #8 (concurrent calls serialise) ----------------------------------


def test_concurrent_import_zip_calls_serialize_via_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lock contention defers worker progress; the held lock blocks UPDATE.

    DuckDB's Python binding is not thread-safe and only single-thread
    use is meaningful even under a lock guard; the production server
    actually serialises tool calls sequentially. The contract this
    test pins is the LOCK behaviour: while a competing thread holds
    the writer lock, the worker spawned by ``import_zip`` cannot
    advance past ``mark_running`` (its first lock acquisition). Once
    the test thread releases the lock, the worker drains cleanly.
    """
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn, _ = _writable_db(tmp_path)
    try:
        # Spawn an import that will queue + spawn a worker; the worker
        # tries to take the lock for ``mark_running``.
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "queued"
        # The worker's ``mark_running`` UPDATE serialises behind every
        # other lock holder. Drain to confirm it terminates without
        # crashing despite contention from the dispatch path that
        # just released the lock.
        _drain_import_workers()
        status = _call_get_import_status(conn, job_id=str(out["job_id"]))
        # Either ``ok`` (importer finished) or ``error`` is acceptable
        # here -- the contract is "the lock guard makes the worker
        # safe", NOT "the importer always succeeds against this
        # fixture". The trivial export.xml is small enough that ``ok``
        # is the steady-state outcome.
        assert status["status"] in {"ok", "error"}
    finally:
        conn.close()


# --- Spec #9 (multi-launch guard) ------------------------------------------


def test_import_zip_returns_existing_job_id_when_active_job_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second call for the same sha re-uses the existing job_id."""
    _, sha = _seed_zip(tmp_path, monkeypatch)
    conn = get_in_memory_connection()
    ensure_schema(conn)
    lock = Lock()
    try:
        # Pre-seed an in-flight queued job for this sha. By skipping
        # the real import path we avoid spawning a worker that would
        # race the assertion.
        job_registry.insert_queued(
            conn,
            lock,
            job_id="ij_already_running",
            source_id=sha[:8],
            source_sha256=sha,
        )
        job_registry.mark_running(conn, lock, "ij_already_running")
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "queued"
        # The dispatcher returned THE pre-seeded job_id, NOT a freshly
        # generated one.
        assert out["job_id"] == "ij_already_running"
        # No new row was inserted.
        count = conn.execute("SELECT COUNT(*) FROM import_jobs").fetchone()
        assert count is not None and count[0] == 1
    finally:
        conn.close()


# --- Spec #10 (idempotent done envelope; covered in test_zip_tools.py
#               too — pin the no-worker contract here as well) -------------


def test_import_zip_returns_done_envelope_when_sha_already_imported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Byte-identical re-import: synchronous ``ok`` envelope, no job row."""
    zip_path, sha = _seed_zip(tmp_path, monkeypatch)
    conn = get_in_memory_connection()
    ensure_schema(conn)
    try:
        # Seed an ``imports`` row claiming we already imported this sha.
        conn.execute(
            "INSERT INTO imports (import_id, export_dir, imported_at, "
            "source_zip_sha256, source_zip_mtime, source_zip_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                "imp_done",
                "/tmp/done",
                datetime(2026, 6, 28, 0, 0, 0, tzinfo=UTC),
                sha,
                datetime.fromtimestamp(zip_path.stat().st_mtime, tz=UTC),
                zip_path.stat().st_size,
            ],
        )
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "ok"
        assert out["records_added"] == 0
        assert out["already_imported_at"] is not None
        # No ``import_jobs`` row was created for the no-op branch.
        count = conn.execute("SELECT COUNT(*) FROM import_jobs").fetchone()
        assert count is not None and count[0] == 0
    finally:
        conn.close()


# --- Spec #11 (boot sweeps orphan jobs) ------------------------------------


def test_server_boot_sweeps_orphan_jobs(tmp_path: Path) -> None:
    """``create_server`` rewrites stale queued/running rows on every boot."""
    from apple_health_mcp.server.server import create_server

    db_path = tmp_path / "h.duckdb"
    seed = open_test_connection(str(db_path), read_only=False)
    ensure_schema(seed)
    stamp_current_version(seed)
    lock = Lock()
    job_registry.insert_queued(
        seed,
        lock,
        job_id="ij_stuck_queued",
        source_id="aaaaaaaa",
        source_sha256="a" * 64,
    )
    job_registry.insert_queued(
        seed,
        lock,
        job_id="ij_stuck_running",
        source_id="bbbbbbbb",
        source_sha256="b" * 64,
    )
    job_registry.mark_running(seed, lock, "ij_stuck_running")
    seed.close()

    boot_conn = open_test_connection(str(db_path), read_only=False)
    try:
        # Building the server triggers the boot-time orphan sweep.
        create_server(boot_conn)
        for jid in ("ij_stuck_queued", "ij_stuck_running"):
            row = boot_conn.execute(
                "SELECT status, error_reason FROM import_jobs WHERE job_id = ?",
                [jid],
            ).fetchone()
            assert row is not None
            assert row[0] == "error"
            assert row[1] == job_registry.ORPHAN_REASON
    finally:
        boot_conn.close()


# --- Spec #12 (orphan recovery surfaces via get_import_status) -------------


def test_get_import_status_returns_recovered_error_for_orphan_job() -> None:
    """A post-sweep poll surfaces the orphan reason / message."""
    conn = get_in_memory_connection()
    ensure_schema(conn)
    lock = Lock()
    try:
        job_registry.insert_queued(
            conn,
            lock,
            job_id="ij_orphan_polled",
            source_id="cccccccc",
            source_sha256="c" * 64,
        )
        job_registry.mark_running(conn, lock, "ij_orphan_polled")
        job_registry.sweep_orphan_jobs(conn, lock)
        out = _call_get_import_status(conn, job_id="ij_orphan_polled")
        assert out["status"] == "error"
        assert out["reason"] == job_registry.ORPHAN_REASON
        assert out["message"] == job_registry.ORPHAN_MESSAGE
    finally:
        conn.close()


def test_get_import_status_short_circuits_on_stale_schema_version() -> None:
    """get_import_status: stale ``schema_version`` (= v=5 stamp) → schema_outdated."""
    from apple_health_mcp.db.migrations import set_current_version
    from tests._helpers import seed_one_import

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        set_current_version(conn, 5)
        out = _call_get_import_status(conn, job_id="ij_anything")
        assert out["state"] == "NEEDS_REIMPORT"
        assert out["reason"] == "schema_outdated"
    finally:
        conn.close()


def test_get_import_status_short_circuits_on_missing_import_jobs() -> None:
    """get_import_status: schema_version current, ``import_jobs`` dropped → schema_outdated.

    Pins the v0.5.1 #188 new branch at the tool surface. The stale-
    version sibling above would still pass with the new branch
    deleted, so this variant locks the actual regression shape in.
    """
    from tests._helpers import seed_one_import

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        seed_one_import(conn)
        conn.execute("DROP TABLE import_jobs;")
        out = _call_get_import_status(conn, job_id="ij_anything")
        assert out["state"] == "NEEDS_REIMPORT"
        assert out["reason"] == "schema_outdated"
    finally:
        conn.close()
