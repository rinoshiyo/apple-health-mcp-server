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
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR
from apple_health_mcp.server.query import query_to_json, run_query_payload

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
    "configuration / not-an-Apple-Health-ZIP / ZIP-not-found failure."
)


_SHA256_READ_CHUNK_BYTES = 1024 * 1024
_ID_PREFIX_LEN = 8


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def import_zip(
        id: Annotated[
            str,
            Field(
                description=(
                    "8-char sha256 prefix from list_zips — uniquely identifies which ZIP to import."
                ),
            ),
        ],
    ) -> str:
        return _import_zip_sync(conn, lock, id)


def _import_zip_sync(
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    target_id: str,
) -> str:
    """Synchronous body of ``import_zip``; split out so it is reusable in tests."""
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

    # Match by hashing each candidate; ``list_zips``'s cache is the
    # happy path but does not store ``id``-keyed entries, so the
    # cheapest correct resolution is to recompute sha256 here. For a
    # directory with ≤10 ZIPs (the realistic shape) this is on the
    # order of single-digit seconds total even on first call; subsequent
    # ``import_zip`` invocations against the same ZIP hit the importer's
    # own sha256-fast-path and return without re-reading anything.
    selected: Path | None = None
    selected_sha: str | None = None
    for path in candidates:
        sha = _stream_sha256(path)
        if sha.startswith(target_id):
            selected = path
            selected_sha = sha
            break

    if selected is None:
        return run_query_payload(
            {
                "status": "error",
                "reason": "id_not_found",
                "message": (
                    f"No ZIP in {export_dir} has id starting with "
                    f"{target_id!r}. Call list_zips to refresh the "
                    "discovery list."
                ),
            }
        )

    assert selected_sha is not None
    if not _is_apple_health_zip(selected):
        return run_query_payload(
            {
                "status": "error",
                "reason": "not_apple_health_export",
                "message": (
                    f"{selected.name} does not contain "
                    "apple_health_export/export.xml or export.xml at "
                    "the top level. Did you mean a different ZIP?"
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
                "id": target_id,
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
        # shape we got into the path the importer expects (the directory
        # that holds ``export.xml`` + ``electrocardiograms/`` +
        # ``workout-routes/``).
        if (extracted_root / "apple_health_export" / "export.xml").exists():
            import_root = extracted_root / "apple_health_export"
        else:
            import_root = extracted_root

        # Lazy import: pulling in ``apple_health_mcp.importers`` at
        # module import would chain into pyarrow / lxml and pay a
        # ~30 MB import cost on every server boot, even when the user
        # never calls import_zip. The smoke test ``test_server_module_
        # does_not_import_pyarrow`` pins that invariant; keep the
        # importer load on the function-call path only.
        from apple_health_mcp.importers import run_import

        stats = run_import(
            import_root,
            conn=conn,
            source_zip=(selected_sha, mtime, size),
        )

    return run_query_payload(
        {
            "status": "ok",
            "id": target_id,
            "records_added": stats.records,
            "workouts_added": stats.workouts,
            "ecg_readings_added": stats.ecg_readings,
            "route_points_added": stats.route_points,
            "already_imported_at": None,
            "duration_secs": None,
            "message": (
                f"Imported {selected.name} ({stats.records} records, "
                f"{stats.workouts} workouts). Read tools now return real "
                "data."
            ),
        }
    )


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


def _stream_sha256(path: Path) -> str:
    """Return the hex sha256 of ``path`` by streaming 1 MB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(_SHA256_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ``_is_apple_health_zip`` lives in ``list_zips`` (the discovery tool
# that emits the flag); ``import_zip`` re-uses the same predicate so
# the "what counts as an Apple Health ZIP" rule has a single source of
# truth.
from apple_health_mcp.server.tools.list_zips import _is_apple_health_zip  # noqa: E402

__all__ = ["DESCRIPTION", "register"]
