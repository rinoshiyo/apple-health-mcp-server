"""Server-side ZIP-cache helpers + backward-compat re-exports.

v0.5 (issue #170): the pure-Python ZIP utilities (``stream_sha256``,
``inspect_zip``, ``ZipInspection`` 等) moved to
:mod:`apple_health_mcp._zip_util` so the CLI's ZIP-only ``import``
subcommand can share them without introducing a layering inversion.
This module re-exports the same names so v0.4 import sites
(``server.tools.list_zips``, ``server.tools.import_zip``, and any
external code that pinned the v0.4 path) keep working unchanged.

The DB-touching helpers (``load_sha_cache``, ``find_sha_by_prefix``)
remain here because they only make sense against the server's writable
DuckDB connection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp._zip_util import (
    APPLE_HEALTH_TOP_LEVEL_MARKERS,
    ID_PREFIX_LEN,
    SHA256_READ_CHUNK_BYTES,
    ZipInspection,
    inspect_zip,
    is_apple_health_zip,
    stream_sha256,
)
from apple_health_mcp.server.query import query_to_json

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


def load_sha_cache(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock,
) -> dict[tuple[int, datetime], str]:
    """Build a ``(size, mtime) → sha256`` lookup from ``imports``.

    Returns the canonical sha for every (size, mtime) tuple that
    already appears in ``imports``. ``list_zips`` consults this to
    skip rehashing ZIPs that were imported in a prior session;
    ``import_zip`` also reuses it so the id-resolution loop does not
    pay an O(N x ZIP size) re-hash for ZIPs the cache already covers.
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
        if sha is None or size_raw is None or mtime_raw is None:  # pragma: no cover - defensive
            continue
        cache[(int(size_raw), _parse_iso(mtime_raw))] = str(sha)
    return cache


def find_sha_by_prefix(
    conn: duckdb.DuckDBPyConnection,
    prefix: str,
    *,
    lock: Lock,
) -> str | None:
    """Return the full sha256 of a prior import whose sha starts with ``prefix``.

    Lets ``import_zip`` short-circuit id resolution for ZIPs that were
    imported in a prior session: a single DB lookup beats re-hashing
    every candidate ZIP in the directory until a prefix match. Returns
    ``None`` when no prior import matches.
    """
    rows = query_to_json(
        conn,
        "SELECT source_zip_sha256 FROM imports "
        "WHERE source_zip_sha256 LIKE ? || '%' "
        "ORDER BY imported_at DESC LIMIT 1",
        [prefix],
        lock=lock,
    )
    if not rows:
        return None
    sha = rows[0]["source_zip_sha256"]
    return str(sha) if sha is not None else None  # pragma: no branch


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


__all__ = [
    "APPLE_HEALTH_TOP_LEVEL_MARKERS",
    "ID_PREFIX_LEN",
    "SHA256_READ_CHUNK_BYTES",
    "ZipInspection",
    "find_sha_by_prefix",
    "inspect_zip",
    "is_apple_health_zip",
    "load_sha_cache",
    "stream_sha256",
]
