"""``list_zips`` MCP tool — discover Apple Health export ZIPs.

v0.4 (issue #148) entry-point of the agent-driven import flow: lists every
``*.zip`` in ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` so the agent can show the
user what's available and pass the chosen ``id`` to ``import_zip``.

Returns a single dict regardless of directory state (empty / mixed /
populated) so callers always get the same shape. ``imports`` table is
consulted for the (mtime, size) → sha256 cache so a 1.2 GB ZIP is not
rehashed on every directory scan.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from apple_health_mcp.server.data_state import (
    EXPORT_ZIPS_DIR_ENV_VAR,
    block_if_schema_outdated,
    resolve_export_zips_dir,
)
from apple_health_mcp.server.query import run_query_payload
from apple_health_mcp.server.tools._async_blurb import (
    IMPORT_POLL_BLURB,
    IMPORT_RUNTIME_BLURB,
)
from apple_health_mcp.server.tools._zip_inspect import (
    ID_PREFIX_LEN,
    ZipInspection,
    inspect_zip,
    load_sha_cache,
    stream_sha256,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


DESCRIPTION = (
    "List Apple Health export ZIPs in the configured directory. Returns "
    "{export_zips_dir, zips, hint}. Each ``zips`` entry carries: id "
    "(8-char sha256 prefix used by import_zip), file_name, mtime "
    "(ISO 8601), size (bytes), sha256 (full hex), imported (bool — true "
    "when this exact ZIP has already been imported into the local DB), "
    "is_apple_health (bool — true when the ZIP contains "
    "apple_health_export/export.xml or export.xml at the top level), "
    "zip_status (one of 'valid_apple_health', 'valid_non_apple_health', "
    "'invalid_zip' — v0.4.1 / issue #158, lets the agent skip a corrupt "
    "or HTML-renamed file without paying the import cost). Use this "
    "BEFORE import_zip: pick an entry, then call import_zip(id=...)."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_zips() -> str:
        # v0.5.1 #188: short-circuit on an outdated DB before the
        # imports-cache load, which would otherwise hit DuckDB's
        # ``Catalog Error: Table source_zip_sha256 does not exist`` on
        # the legacy ``imports`` shape that lacks the v0.4 #148 columns.
        if (envelope := block_if_schema_outdated(conn, lock=lock)) is not None:
            return envelope
        dir_str = (os.environ.get(EXPORT_ZIPS_DIR_ENV_VAR) or "").strip()
        if not dir_str:
            return run_query_payload(
                {
                    "export_zips_dir": None,
                    "zips": [],
                    "hint": (
                        f"{EXPORT_ZIPS_DIR_ENV_VAR} is not set. Configure "
                        "Claude Desktop → Settings → MCP → "
                        "apple-health-mcp-server → Export ZIPs directory, "
                        "or set the env var directly, then call list_zips "
                        "again."
                    ),
                }
            )

        export_dir = resolve_export_zips_dir(dir_str)
        try:
            entries = sorted(p for p in export_dir.iterdir() if p.suffix.lower() == ".zip")
        except FileNotFoundError:
            return run_query_payload(
                {
                    "export_zips_dir": str(export_dir),
                    "zips": [],
                    "hint": (
                        f"Directory {export_dir} does not exist. Create it "
                        "and drop your Apple Health export ZIP into it, "
                        "then call list_zips again."
                    ),
                }
            )
        except NotADirectoryError:
            return run_query_payload(
                {
                    "export_zips_dir": str(export_dir),
                    "zips": [],
                    "hint": (
                        f"Path {export_dir} is not a directory. Point "
                        f"{EXPORT_ZIPS_DIR_ENV_VAR} at a folder, not a "
                        "file."
                    ),
                }
            )

        # ``imports`` is monotonic (rows only appended, never deleted),
        # so taking the cache snapshot under the lock and then walking
        # the directory unlocked is consistency-safe: a fresh import
        # landing mid-scan can only flip a future ``list_zips`` entry
        # from ``imported=false`` to ``imported=true``, never the other
        # way; the stale answer self-corrects on the next call.
        sha_cache = load_sha_cache(conn, lock=lock)
        imported_set = set(sha_cache.values())

        zips: list[dict[str, Any]] = []
        for path in entries:
            entry = _describe_zip(path, sha_cache, imported_set)
            if entry is not None:  # pragma: no branch - None only on rare TOCTOU
                zips.append(entry)

        if not zips:
            hint = (
                f"No ZIPs found in {export_dir}. Drop your Apple Health "
                "export.zip (the file the Health app produces via "
                "Share → Export) into this directory, then call "
                "list_zips again."
            )
        else:
            hint = (
                "Pick an entry by ``id`` and call import_zip(id=...). "
                "Branch on the returned envelope: an entry with "
                "``imported: true`` short-circuits synchronously and "
                "returns ``{status: 'ok', records_added: 0, "
                "already_imported_at, ...}`` in milliseconds without a "
                "``job_id`` -- do NOT poll get_import_status on this "
                "branch; just read the synchronous payload. A fresh "
                "import returns ``{status: 'queued', job_id, ...}``. "
                f"{IMPORT_POLL_BLURB}. {IMPORT_RUNTIME_BLURB}."
            )

        return run_query_payload(
            {
                "export_zips_dir": str(export_dir),
                "zips": zips,
                "hint": hint,
            }
        )


def _describe_zip(
    path: Path,
    sha_cache: dict[tuple[int, datetime], str],
    imported_set: set[str],
) -> dict[str, Any] | None:
    """Build one ``zips`` entry, or ``None`` if the file vanished mid-scan.

    The TOCTOU window between ``iterdir`` and ``stat`` is real (companion
    apps may rotate ZIPs into / out of the drop-zone). Catching the
    expected ``FileNotFoundError`` / ``PermissionError`` per entry keeps
    a transient directory mutation from crashing the entire ``list_zips``
    call -- the agent retries on the next scan.
    """
    try:
        stat = path.stat()
    except (FileNotFoundError, PermissionError) as exc:  # pragma: no cover - rare
        _logger.debug("skipping %s during list_zips scan (%s)", path, exc)
        return None
    mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    cache_key = (stat.st_size, mtime_dt)
    cached = sha_cache.get(cache_key)
    try:
        sha = cached or stream_sha256(path)
    except (FileNotFoundError, PermissionError) as exc:  # pragma: no cover - rare
        _logger.debug("skipping %s while hashing during list_zips (%s)", path, exc)
        return None
    inspection = inspect_zip(path)
    return {
        "id": sha[:ID_PREFIX_LEN],
        "file_name": path.name,
        "mtime": mtime_dt.isoformat(),
        "size": stat.st_size,
        "sha256": sha,
        "imported": sha in imported_set,
        # Backward-compatible boolean for v0.4.0 consumers. New callers
        # should branch on ``zip_status`` for the three-state view.
        "is_apple_health": inspection == ZipInspection.VALID_APPLE_HEALTH,
        "zip_status": inspection.value,
    }
