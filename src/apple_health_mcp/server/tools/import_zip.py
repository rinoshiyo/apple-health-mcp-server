"""``import_zip`` MCP tool — drive run_import from a discovered ZIP.

v0.4 (issue #148) companion to ``list_zips``. The agent picks an ``id``
from ``list_zips``'s output and passes it here; this module locates the
matching ZIP under ``APPLE_HEALTH_EXPORT_ZIPS_DIR``, extracts it into a
``tempfile.TemporaryDirectory``, and hands the resulting export
directory to ``run_import`` against the server's live writable handle.

Idempotency lives inside the importer, not at this tool's surface: the
``source_zip_sha256`` lookup inside ``run_import`` makes a byte-identical
re-import a no-op that returns in milliseconds with
``records_added: 0`` + ``already_imported_at`` set.

**Concurrency contract.** The DuckDB Python connection is not thread-
safe and the v0.4 server opens it writable so this tool can reuse the
same handle. The full extract + ``run_import`` body therefore runs
under the same ``lock`` every other MCP tool acquires before touching
``conn`` -- without the wrap, a concurrent read tool's
``conn.execute(...)`` would race the importer's writes. The handler is
also dispatched via ``asyncio.to_thread`` so the event loop stays
responsive to the MCP transport's keepalives during the 1-2 minute
import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR
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
    "Pass the ``id`` value emitted by list_zips (an 8-char sha256 "
    "prefix). The tool resolves the ZIP under "
    "APPLE_HEALTH_EXPORT_ZIPS_DIR, extracts it into a temp directory, "
    "and runs the full XML → ECG → GPX → finalize pipeline. Takes 1-2 "
    "minutes for a typical multi-GB export; the agent should tell the "
    "user it is working synchronously. A byte-identical re-import "
    "no-ops in milliseconds (records_added: 0, "
    "already_imported_at populated). Returns {status: 'ok' | 'error', "
    "id, records_added, workouts_added, ecg_readings_added, "
    "route_points_added, already_imported_at, duration_secs, message} "
    "on success, or {status: 'error', reason, message} on a "
    "configuration / invalid-zip / not-an-Apple-Health-ZIP / "
    "ZIP-not-found / invalid-id failure. The ``invalid_zip`` reason "
    "signals the file is not a valid ZIP archive (corruption, partial "
    "download, an HTML page renamed to .zip) and the user should "
    "re-download; ``not_apple_health_export`` signals a valid ZIP that "
    "is just missing the Apple Health marker and the user should pick "
    "a different file."
)


# Validation for the user-supplied ``id`` argument: hex-only, 4-64 chars.
# Pre-validation is critical because Python's ``str.startswith('')``
# returns True on the empty prefix -- without the gate an empty / 1-char
# id would silently select the alphabetically-first ZIP and import it.
_MIN_ID_LEN = 4
_MAX_ID_LEN = 64
_ID_HEX_RE = re.compile(r"^[0-9a-f]+$")


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def import_zip(
        id: Annotated[
            str,
            Field(
                description=(
                    "Hex sha256 prefix from list_zips (typically the "
                    "8-char form). Validated as 4-64 lowercase hex chars."
                ),
            ),
        ],
    ) -> str:
        # Offload to a worker thread so the asyncio event loop stays
        # responsive during the multi-minute import; the body holds the
        # ``lock`` for the whole conn.execute path, so concurrent tool
        # calls back off cleanly instead of racing the writer.
        return await asyncio.to_thread(_import_zip_sync, conn, lock, id)


def _import_zip_sync(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    target_id: str,
) -> str:
    """Synchronous body of ``import_zip``; split so the asyncio handler
    can offload to ``asyncio.to_thread`` and tests can drive it directly.
    """
    cleaned = target_id.strip().lower()
    if not (_MIN_ID_LEN <= len(cleaned) <= _MAX_ID_LEN and _ID_HEX_RE.fullmatch(cleaned)):
        return run_query_payload(
            {
                "status": "error",
                "reason": "invalid_id",
                "message": (
                    f"id must be {_MIN_ID_LEN}-{_MAX_ID_LEN} lowercase "
                    f"hex characters; got {target_id!r}. Call list_zips "
                    "and use the ``id`` field verbatim."
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

    export_dir = Path(dir_str).expanduser()
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
        # v0.4.1 (issue #160 code-review #4): mirror list_zips's typed
        # envelope when APPLE_HEALTH_EXPORT_ZIPS_DIR points at a file
        # instead of a directory. Pre-fix this raised through to
        # FastMCP as an unstructured MCP error; the sibling list_zips
        # has caught NotADirectoryError since v0.4.0, so import_zip
        # was the only path with the gap.
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

    stat = selected.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    size = stat.st_size

    # Idempotency check before paying the unzip cost: if the importer
    # would no-op anyway, surface the already-imported envelope
    # directly without writing to disk. ``run_import``'s own Tier-1
    # sha256 fast path still covers the same case for completeness;
    # this pre-check just keeps the user's tempdir cleaner and shaves
    # ~0.5s on a multi-GB ZIP that would otherwise extract.
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

    with tempfile.TemporaryDirectory(prefix="apple-health-zip-") as tmpdir:
        extracted_root = Path(tmpdir)
        try:
            with zipfile.ZipFile(selected) as zf:
                zf.extractall(extracted_root)
        except (zipfile.BadZipFile, OSError) as exc:
            _logger.warning("ZIP extraction failed for %s: %s", selected, exc)
            return run_query_payload(
                {
                    "status": "error",
                    "reason": "zip_extract_failed",
                    "message": f"Failed to extract {selected.name}: {exc}",
                }
            )

        # Apple Health ships the export as ``apple_health_export/`` at
        # the top level; some repackagers flatten it. Resolve whichever
        # shape we got into the path the importer expects.
        if (extracted_root / "apple_health_export" / "export.xml").exists():
            import_root = extracted_root / "apple_health_export"
        else:
            import_root = extracted_root

        # Lazy import: pulling in ``apple_health_mcp.importers`` at
        # module-import time would chain into pyarrow + lxml and pay
        # ~30 MB on every server boot, even when the user never calls
        # import_zip. ``test_server_module_does_not_import_pyarrow``
        # pins that invariant.
        from apple_health_mcp.importers import run_import

        # Hold the lock for the whole importer run: the DuckDB Python
        # binding is not thread-safe and ``run_import`` performs many
        # writes against ``conn``. Releasing the lock here would let a
        # concurrent read tool's ``conn.execute`` race the importer
        # mid-write -- cursor corruption, partial reads, or worse.
        # Concurrent agent calls back off on this lock until the
        # import completes; the ``asyncio.to_thread`` wrap in the
        # handler keeps the event loop responsive to the MCP
        # transport's keepalives meanwhile.
        started = time.monotonic()
        with lock:
            stats = run_import(
                import_root,
                conn=conn,
                source_zip=(selected_sha, mtime, size),
            )
        duration_secs = round(time.monotonic() - started, 2)

    return run_query_payload(
        {
            "status": "ok",
            "id": canonical_id,
            "records_added": stats.records,
            "workouts_added": stats.workouts,
            "ecg_readings_added": stats.ecg_readings,
            "route_points_added": stats.route_points,
            "already_imported_at": None,
            "duration_secs": duration_secs,
            "message": (
                f"Imported {selected.name} ({stats.records} records, "
                f"{stats.workouts} workouts) in {duration_secs:.1f}s. "
                "Read tools now return real data."
            ),
        }
    )


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
