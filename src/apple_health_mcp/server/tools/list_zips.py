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

import hashlib
import logging
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR
from apple_health_mcp.server.query import query_to_json, run_query_payload

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
    "apple_health_export/export.xml or export.xml at the top level). "
    "Use this BEFORE import_zip: pick an entry, then call "
    "import_zip(id=…)."
)


# 1 MB chunk for streaming sha256 — same constant family as the XML
# importer's read chunk; sized to keep the OS page cache warm without
# blowing up memory.
_SHA256_READ_CHUNK_BYTES = 1024 * 1024

# sha256 short-prefix length used as the ``id`` field. 8 hex chars =
# 32 bits of entropy; collision probability is ~negligible for the
# realistic case of ≤100 ZIPs in a user's drop-zone.
_ID_PREFIX_LEN = 8


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_zips() -> str:
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

        export_dir = Path(dir_str).expanduser()
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

        sha_cache = _load_sha_cache(conn, lock=lock)
        imported_set = set(sha_cache.values())

        zips: list[dict[str, Any]] = []
        for path in entries:
            stat = path.stat()
            mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            cache_key = (stat.st_size, mtime_dt)
            sha = sha_cache.get(cache_key) or _stream_sha256(path)
            zips.append(
                {
                    "id": sha[:_ID_PREFIX_LEN],
                    "file_name": path.name,
                    "mtime": mtime_dt.isoformat(),
                    "size": stat.st_size,
                    "sha256": sha,
                    "imported": sha in imported_set,
                    "is_apple_health": _is_apple_health_zip(path),
                }
            )

        if not zips:
            hint = (
                f"No ZIPs found in {export_dir}. Drop your Apple Health "
                "export.zip (the file the Health app produces via "
                "Share → Export) into this directory, then call "
                "list_zips again."
            )
        else:
            hint = (
                "Pick an entry by ``id`` and call import_zip(id=…). The "
                "import takes 1-2 minutes for a typical multi-GB export; "
                "Claude will wait synchronously. Already-imported ZIPs "
                "(imported=true) no-op in milliseconds."
            )

        return run_query_payload(
            {
                "export_zips_dir": str(export_dir),
                "zips": zips,
                "hint": hint,
            }
        )


def _load_sha_cache(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock,
) -> dict[tuple[int, datetime], str]:
    """Build a ``(size, mtime) → sha256`` lookup from ``imports``.

    The ``imports`` table records the source ZIP triple
    (``source_zip_sha256`` / ``source_zip_mtime`` / ``source_zip_size``)
    set by past ``import_zip`` calls. Matching a directory entry's
    (size, mtime) against this cache lets ``list_zips`` skip rehashing
    a 1.2 GB ZIP whose stat has not changed since the last import.
    The original file name is intentionally NOT part of the key: a user
    who renames or duplicates a ZIP between scans should still hit the
    cache, and the sha256 is the canonical content identity anyway.
    """
    rows = query_to_json(
        conn,
        "SELECT source_zip_sha256, source_zip_mtime, source_zip_size "
        "FROM imports WHERE source_zip_sha256 IS NOT NULL",
        lock=lock,
    )
    cache: dict[tuple[int, datetime], str] = {}
    for row in rows:
        sha = row["source_zip_sha256"]
        size_raw = row["source_zip_size"]
        mtime_raw = row["source_zip_mtime"]
        # Defensive: the SELECT already filters sha IS NOT NULL, but
        # the size / mtime columns could land NULL on a pre-v0.4 row
        # that a future migration brings forward without backfilling
        # the triple. The whole row is unusable as a cache entry then,
        # so skip it silently.
        if sha is None or size_raw is None or mtime_raw is None:  # pragma: no cover - defensive
            continue
        mtime = _parse_iso(mtime_raw)
        cache[(int(size_raw), mtime)] = str(sha)
    return cache


def _parse_iso(value: object) -> datetime:
    """Coerce a query-to-json-serialised TIMESTAMPTZ value into a datetime.

    ``query_to_json`` stringifies tz-aware datetimes via
    ``datetime.isoformat(sep=" ")`` -- the expected input here. The
    ``isinstance(value, datetime)`` fast-path keeps the helper robust
    against a future ``_coerce`` change that stops stringifying.
    """
    if isinstance(value, datetime):  # pragma: no cover - future-proofing
        return value
    return datetime.fromisoformat(str(value))


def _stream_sha256(path: Path) -> str:
    """Return the hex sha256 of ``path`` by streaming 1 MB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(_SHA256_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_apple_health_zip(path: Path) -> bool:
    """Detect whether ``path`` looks like an Apple Health export.

    Apple's Health-app share-sheet produces a ZIP whose top-level
    folder is ``apple_health_export/`` containing ``export.xml``. Some
    third-party tools repackage just the inner directory contents at
    the root. Both shapes are accepted; anything else is flagged so
    the agent can ask "did you mean a different ZIP?" before paying
    the import cost.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:  # pragma: no cover - defensive
        _logger.debug("is_apple_health probe failed for %s (%s)", path, exc)
        return False
    return any(name in {"apple_health_export/export.xml", "export.xml"} for name in names)
