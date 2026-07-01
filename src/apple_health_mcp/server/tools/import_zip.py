"""``import_zip`` MCP tool — job-based async import driver (v0.5, issue #157).

v0.4 (issue #148) introduced this tool as a *synchronous* importer that
returned a ``done`` envelope after the full XML → ECG → GPX → finalize
pipeline finished. Dogfood on a fast workstation (Intel 14 + NVMe) ran
in 44-106s, but the implicit MCP tool-call timeout on slower clients
made the synchronous shape unshippable — a 200-400s import on mid-range
hardware deterministically tripped the client's timeout and left the
user with a half-imported DB and no progress signal.

v0.5 splits the surface into two tools:

* ``import_zip(id=...)`` — validates the id, resolves the ZIP under
  ``APPLE_HEALTH_EXPORT_ZIPS_DIR``, inspects it for the Apple Health
  marker, then inserts an ``import_jobs`` row, spawns a worker thread,
  and returns a ``queued`` envelope in ms. Validation / config /
  invalid-zip / not-an-Apple-Health-ZIP failures still surface
  synchronously through ``error`` envelopes — a job_id is only minted
  once we have a real importer to run.
* ``get_import_status(job_id=...)`` — the new companion tool the agent
  polls every 10-30 seconds to retrieve ``running`` / ``done`` / ``error``
  state from the same ``import_jobs`` row.

**Idempotency.** A byte-identical re-import still no-ops in ms: the
sha256 lookup against ``imports.source_zip_sha256`` runs BEFORE any
job is inserted, so the agent receives a ``done`` envelope with
``records_added: 0`` and ``already_imported_at`` populated, exactly
matching the v0.4 wire shape. No row is written to ``import_jobs`` for
the no-op case.

**Multi-launch guard.** If a worker is already in flight for the same
sha256 (``status IN ('queued','running')``), the second call returns
the EXISTING ``job_id`` instead of spawning a duplicate worker that
would queue on the writer lock and then no-op against the same
``imports.source_zip_sha256`` row anyway.

**Orphan recovery.** Server boot runs
:func:`apple_health_mcp.db.import_jobs.sweep_orphan_jobs` to flip every
``queued`` / ``running`` row to ``error`` with
``reason='server_restarted_while_running'``. Without the sweep, the
multi-launch guard would wedge on a worker the OS killed mid-import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.db import import_jobs as job_registry
from apple_health_mcp.server.data_state import (
    EXPORT_ZIPS_DIR_ENV_VAR,
    block_if_schema_outdated,
    resolve_export_zips_dir,
)
from apple_health_mcp.server.query import query_to_json, run_query_payload
from apple_health_mcp.server.tools._zip_inspect import (
    ID_PREFIX_LEN,
    ZipInspection,
    find_sha_by_prefix,
    inspect_zip,
    load_sha_cache,
    stream_sha256,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


DESCRIPTION = (
    "Import an Apple Health export ZIP into the local DuckDB database. "
    "Pass the ``id`` value emitted by list_zips (typically an 8-char "
    "sha256 prefix; 4-64 hex chars are accepted, leading/trailing "
    "whitespace is trimmed and uppercase is normalised to lowercase "
    "before lookup). The tool resolves the ZIP under "
    "APPLE_HEALTH_EXPORT_ZIPS_DIR, inspects it, and -- on success -- "
    "kicks off the importer in a background worker thread. **Returns "
    "immediately with a ``job_id``** so the call cannot trip the MCP "
    "client's tool-call timeout on slow hardware. Poll "
    "get_import_status(job_id=...) every 10-30 seconds to track "
    "progress and retrieve the final result; total import time depends "
    "on the user's machine (~45s on a fast NVMe + recent CPU, several "
    "minutes on slower hardware). A byte-identical re-import returns "
    "a synchronous ``status: 'ok'`` envelope (records_added: 0, "
    "already_imported_at populated) without spawning a worker -- the "
    "wire shape matches the pre-v0.5 synchronous import_zip and never "
    "carries a job_id, so the agent must NOT poll get_import_status on "
    "this branch. Returns {status: 'queued', job_id, id, queued_at, "
    "message} on a new import; {status: 'ok', id, records_added: 0, "
    "already_imported_at, ...} on the idempotent no-op; or "
    "{status: 'error', reason, message} on a configuration / "
    "invalid-zip / not-an-Apple-Health-ZIP / ZIP-not-found / "
    "invalid-id failure. The ``invalid_zip`` reason signals the file "
    "is not a valid ZIP archive (corruption, partial download, an "
    "HTML page renamed to .zip) and the user should re-download; "
    "``not_apple_health_export`` signals a valid ZIP that is just "
    "missing the Apple Health marker and the user should pick a "
    "different file."
)


# Validation for the user-supplied ``id`` argument: hex-only, 4-64 chars.
# Pre-validation is critical because Python's ``str.startswith('')``
# returns True on the empty prefix -- without the gate an empty / 1-char
# id would silently select the alphabetically-first ZIP and import it.
_MIN_ID_LEN = 4
_MAX_ID_LEN = 64
_ID_HEX_RE = re.compile(r"^[0-9a-f]+$")

# Cap for echoing the caller-supplied ``id`` back inside the
# ``invalid_id`` error message (issue #228). Real MCP calls cannot reach
# this code with an oversized id: the ``max_length=64`` Field constraint
# on the tool argument (added in #235) rejects them at the FastMCP
# boundary before dispatch. The truncation is defense-in-depth for the
# paths that bypass that gate -- direct ``_import_zip_dispatch`` calls
# (unit tests) and any future regression of the Field constraint.
_ID_ECHO_MAX_CHARS = _MAX_ID_LEN


def _truncate_id_for_echo(value: str) -> str:
    """Truncate ``value`` to ``_ID_ECHO_MAX_CHARS`` with a ``...`` suffix."""
    if len(value) <= _ID_ECHO_MAX_CHARS:
        return value
    return f"{value[:_ID_ECHO_MAX_CHARS]}..."


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def import_zip(
        id: Annotated[
            str,
            Field(
                description=(
                    "Hex sha256 prefix from list_zips (typically the "
                    "8-char form). Validated as 4-64 hex characters; "
                    "leading/trailing whitespace is trimmed and "
                    "uppercase is normalised to lowercase before lookup."
                ),
                max_length=64,
            ),
        ],
    ) -> str:
        # v0.5 code-review (PR #184 F2): wrap dispatch in
        # ``asyncio.to_thread`` so multi-GB sha256 streaming (when
        # ``_resolve_target`` misses the cache on a first import) does
        # not block the asyncio event loop. The dispatch body is
        # usually milliseconds, but the worst-case cache-miss path
        # streams a 1-2 GB ZIP synchronously — exactly the kind of
        # event-loop block the v0.5 split was designed to avoid on
        # first-time users.
        return await asyncio.to_thread(_import_zip_dispatch, conn, lock, id)


def _import_zip_dispatch(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    target_id: str,
) -> str:
    """Synchronous body of ``import_zip``; split so tests can drive it directly.

    Validation and short-circuit branches return their envelopes inline
    (no job row is created). Only the "new import to run" branch mints
    a ``job_id``, INSERTs the queued row, and spawns the worker thread
    that drives :func:`apple_health_mcp.importers.zip_extract.extract_zip_and_import`.
    """
    # v0.5.1 #188: short-circuit on an outdated DB before the writer
    # hits ``INSERT INTO import_jobs`` (the table did not exist before
    # v=6). The schema_outdated envelope routes the agent at the
    # fresh-reset recovery path instead of surfacing a raw DuckDB
    # ``Catalog Error``.
    #
    # Intentionally placed BEFORE id-validation: an agent on a stale
    # DB cannot recover by fixing its id; surfacing schema_outdated
    # tells them the right next step. The pre-#188 behaviour of
    # returning ``invalid_id`` for a malformed id on a stale DB is a
    # tolerable wire-shape change at pre-1.0 (no production consumer
    # branches on it, per post-#195 code-review Angle A/B).
    if (envelope := block_if_schema_outdated(conn, lock=lock)) is not None:
        return envelope

    cleaned = target_id.strip().lower()
    if not (_MIN_ID_LEN <= len(cleaned) <= _MAX_ID_LEN and _ID_HEX_RE.fullmatch(cleaned)):
        echoed_id = _truncate_id_for_echo(target_id)
        return run_query_payload(
            {
                "status": "error",
                "reason": "invalid_id",
                "message": (
                    f"id must be {_MIN_ID_LEN}-{_MAX_ID_LEN} hex "
                    f"characters (case-insensitive, surrounding "
                    f"whitespace ignored); got {echoed_id!r}. Call "
                    "list_zips and pass the ``id`` field."
                ),
            }
        )

    dir_str = (os.environ.get(EXPORT_ZIPS_DIR_ENV_VAR) or "").strip()
    if not dir_str:
        return run_query_payload(
            {
                "status": "error",
                "reason": "export_zips_dir_not_set",
                "message": (
                    f"{EXPORT_ZIPS_DIR_ENV_VAR} is not set. Configure "
                    "the Export ZIPs directory and call list_zips first."
                ),
            }
        )

    export_dir = resolve_export_zips_dir(dir_str)
    try:
        candidates = sorted(p for p in export_dir.iterdir() if p.suffix.lower() == ".zip")
    except FileNotFoundError:
        return run_query_payload(
            {
                "status": "error",
                "reason": "export_zips_dir_missing",
                "message": (
                    f"Directory {export_dir} does not exist. Create it "
                    "and drop your Apple Health export ZIP into it."
                ),
            }
        )
    except NotADirectoryError:
        return run_query_payload(
            {
                "status": "error",
                "reason": "export_zips_dir_not_a_directory",
                "message": (
                    f"Path {export_dir} is not a directory. Point "
                    f"{EXPORT_ZIPS_DIR_ENV_VAR} at a folder containing "
                    "your Apple Health export ZIP, not a file."
                ),
            }
        )

    selected, selected_sha = _resolve_target(conn, lock, candidates, cleaned)
    if selected is None:
        return run_query_payload(
            {
                "status": "error",
                "reason": "id_not_found",
                "message": (
                    f"No ZIP in {export_dir} has id starting with "
                    f"{cleaned!r}. Call list_zips to refresh the "
                    "discovery list."
                ),
            }
        )

    assert selected_sha is not None
    canonical_id = selected_sha[:ID_PREFIX_LEN]
    inspection = inspect_zip(selected)
    if inspection == ZipInspection.INVALID_ZIP:
        return run_query_payload(
            {
                "status": "error",
                "reason": "invalid_zip",
                "message": (
                    f"{selected.name} is not a valid ZIP archive. The file "
                    "may be corrupted, partially downloaded, or have a "
                    ".zip extension by mistake (e.g. an HTML page renamed). "
                    "Re-download or re-export your Apple Health data and "
                    "try again."
                ),
            }
        )
    if inspection == ZipInspection.VALID_NON_APPLE_HEALTH:
        return run_query_payload(
            {
                "status": "error",
                "reason": "not_apple_health_export",
                "message": (
                    f"{selected.name} is a valid ZIP but does not contain "
                    "apple_health_export/export.xml or export.xml at the "
                    "top level. Did you mean a different ZIP?"
                ),
            }
        )

    # Idempotency check: byte-identical re-import returns the legacy
    # ``ok`` envelope without writing to ``import_jobs``. The pre-v0.5
    # synchronous wire shape (status: 'ok', records_added: 0,
    # already_imported_at populated) is preserved on this no-op branch.
    already = _find_existing_import(conn, lock, selected_sha)
    if already is not None:
        return run_query_payload(
            {
                "status": "ok",
                "id": canonical_id,
                "records_added": 0,
                "workouts_added": 0,
                "ecg_readings_added": 0,
                "route_points_added": 0,
                "already_imported_at": already,
                "duration_secs": 0.0,
                "message": (
                    f"{selected.name} was already imported at {already}. "
                    "No changes made; existing data remains queryable."
                ),
            }
        )

    stat = selected.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    size = stat.st_size
    source_zip = (selected_sha, mtime, size)

    # v0.5 code-review (PR #184 F3): atomic claim-or-get-active closes
    # the TOCTOU window between the multi-launch guard SELECT and the
    # INSERT. Two near-simultaneous calls for the same sha used to
    # both pass the guard and both spawn workers; ``claim_or_get_active``
    # now holds the writer lock across both the read AND the INSERT, so
    # the second caller deterministically gets the first call's
    # ``job_id`` back via ``freshly_inserted=False``.
    new_job_id = job_registry.generate_job_id(selected_sha)
    claimed, freshly_inserted = job_registry.claim_or_get_active(
        conn,
        lock,
        job_id=new_job_id,
        source_id=canonical_id,
        source_sha256=selected_sha,
    )
    if not freshly_inserted:
        return run_query_payload(
            {
                "status": "queued",
                "job_id": claimed.job_id,
                "id": canonical_id,
                "queued_at": str(claimed.queued_at),
                "message": (
                    f"Import for {selected.name} is already in flight "
                    f"(job_id={claimed.job_id}). Poll "
                    "get_import_status(job_id=...) every 10-30 seconds "
                    "until status reaches 'done' or 'error'."
                ),
            }
        )

    worker = threading.Thread(
        target=_run_import_in_background,
        args=(conn, lock, selected, source_zip, claimed.job_id),
        daemon=True,
        name=f"import-zip-{claimed.job_id}",
    )
    worker.start()

    return run_query_payload(
        {
            "status": "queued",
            "job_id": claimed.job_id,
            "id": canonical_id,
            "queued_at": str(claimed.queued_at),
            "message": (
                f"Import of {selected.name} started in background "
                f"(job_id={claimed.job_id}). Poll get_import_status(job_id=...) "
                "every 10-30 seconds to track progress. Total runtime "
                "depends on your machine (~45s on fast NVMe + recent CPU, "
                "several minutes on slower hardware)."
            ),
        }
    )


def _run_import_in_background(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    selected: Path,
    source_zip: tuple[str, datetime, int],
    job_id: str,
) -> None:
    """Worker-thread body: run extract + import + record terminal state.

    Catches every Exception so the daemon thread cannot die silently --
    a swallowed traceback here would leave the ``import_jobs`` row stuck
    in ``running`` until the next server boot's orphan sweep, which is
    the wrong story for an in-process error the agent should see now.
    """
    # Lazy import: ``extract_zip_and_import`` pulls the importer / lxml /
    # pyarrow graph; the server boot path must stay clean of it
    # (``test_server_module_does_not_import_pyarrow``).
    from apple_health_mcp.importers.zip_extract import extract_zip_and_import

    # Phase callback runs INSIDE the writer-lock context
    # ``extract_zip_and_import`` opens around ``run_import``. Re-acquiring
    # the same ``threading.Lock`` would deadlock, so the callback issues
    # the UPDATE directly. Safe because we are guaranteed to own the
    # lock for the duration of the callback's lifetime.
    def _phase_cb(phase: str) -> None:
        conn.execute(
            "UPDATE import_jobs SET phase=? WHERE job_id=?",
            [phase, job_id],
        )

    # v0.5 code-review (PR #184 F5): wrap mark_running inside the same
    # try block as extract_zip_and_import. Pre-fix, a failing
    # mark_running (transient DuckDB error, file lock contention) would
    # escape the daemon thread silently and leave the row stuck at
    # status='queued' until the next boot sweep — wedging the
    # multi-launch guard against this sha indefinitely.
    started = time.monotonic()
    try:
        job_registry.mark_running(conn, lock, job_id, phase="extracting")
        stats = extract_zip_and_import(
            selected,
            source_zip=source_zip,
            conn=conn,
            lock=lock,
            phase_callback=_phase_cb,
        )
    except zipfile.BadZipFile as exc:
        _logger.warning("ZIP extraction failed for %s: %s", selected, exc)
        _safe_mark_error(
            conn,
            lock,
            job_id,
            reason="zip_extract_failed",
            message=f"Failed to extract {selected.name}: {exc}",
        )
        return
    except Exception as exc:  # pragma: no cover - covered via injected failure test
        _logger.exception("Background import failed for %s", selected)
        _safe_mark_error(
            conn,
            lock,
            job_id,
            reason="run_import_failed",
            message=str(exc),
        )
        return
    duration_secs = round(time.monotonic() - started, 2)

    try:
        job_registry.mark_done(
            conn,
            lock,
            job_id,
            records_added=stats.records,
            workouts_added=stats.workouts,
            ecg_readings_added=stats.ecg_readings,
            route_points_added=stats.route_points,
            duration_secs=duration_secs,
            already_imported_at=None,
        )
    except Exception:  # pragma: no cover - defensive: mark_done is the last write
        _logger.exception("mark_done failed for %s after successful import", job_id)


def _safe_mark_error(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    job_id: str,
    *,
    reason: str,
    message: str,
) -> None:
    """Best-effort ``mark_error`` that never re-raises out of the worker.

    The terminal-state write is the last opportunity to flip a job to
    a terminal status; if it raises, the row is stuck in ``running``
    forever (the boot sweep's only safety net) and the agent's poll
    keeps returning ``running`` indefinitely. Swallowing the exception
    here loses the failure detail but keeps the agent UX honest — the
    underlying error is already logged with full traceback above.
    """
    try:
        job_registry.mark_error(conn, lock, job_id, reason=reason, message=message)
    except Exception:  # pragma: no cover - DB unreachable during error reporting
        _logger.exception("mark_error failed for %s; row left in running state", job_id)


def _resolve_target(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    candidates: list[Path],
    target_id: str,
) -> tuple[Path | None, str | None]:
    """Find the on-disk ZIP whose sha256 starts with ``target_id``.

    Resolution order (cheapest first):

    1. **DB cache by sha prefix.** A prior import already stamped the
       full sha into ``imports.source_zip_sha256``; if the prefix hits,
       look up the (size, mtime) tuple in the same cache, then confirm
       a candidate's ``stat()`` matches. No hashing required.
    2. **DB cache by (size, mtime).** When the prefix lookup misses,
       any candidate whose (size, mtime) tuple is already in the
       imports cache short-circuits hashing to the cached sha.
    3. **Stream sha256.** Last resort: hash the candidate. Only fires
       for ZIPs the user has never imported.

    Steps 1-2 collapse the O(N x ZIP-size) re-hash storm that an
    earlier draft paid every time an agent re-asked for an already-
    imported ZIP. The cost on the agent's hot path drops from
    multi-second to one ``stat()`` per candidate plus, at most, one
    fresh sha256.
    """
    sha_cache = load_sha_cache(conn, lock=lock)

    full_sha = find_sha_by_prefix(conn, target_id, lock=lock)
    if full_sha is not None:
        for size_key, mtime_key in (k for k, v in sha_cache.items() if v == full_sha):
            for path in candidates:
                try:
                    stat = path.stat()
                except (FileNotFoundError, PermissionError):  # pragma: no cover - rare
                    continue
                if stat.st_size != size_key:  # pragma: no cover - prefix collision is rare
                    continue
                mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if mtime_dt == mtime_key:  # pragma: no branch - same-stat is the happy path
                    return path, full_sha
        # The prefix matched the DB but no on-disk ZIP carries that
        # (size, mtime). Fall through to the streaming path -- the
        # user may have re-saved the ZIP, changing mtime.

    for path in candidates:
        try:
            stat = path.stat()
        except (FileNotFoundError, PermissionError):  # pragma: no cover - rare
            continue
        mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        cached = sha_cache.get((stat.st_size, mtime_dt))
        sha = cached or stream_sha256(path)
        if sha.startswith(target_id):
            return path, sha
    return None, None


def _find_existing_import(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    sha: str,
) -> str | None:
    """Return the ``imported_at`` timestamp for a prior import of ``sha``, or None."""
    rows = query_to_json(
        conn,
        "SELECT imported_at FROM imports WHERE source_zip_sha256 = ? "
        "ORDER BY imported_at DESC LIMIT 1",
        [sha],
        lock=lock,
    )
    if not rows:
        return None
    value = rows[0]["imported_at"]
    if value is None:  # pragma: no cover - imports.imported_at is NOT NULL
        return None
    return str(value)


__all__ = ["DESCRIPTION", "register"]
