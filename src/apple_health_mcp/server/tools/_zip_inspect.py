"""Shared helpers for the v0.4 (issue #148) ZIP-flow tools.

``list_zips`` and ``import_zip`` both need to (a) stream sha256 over a
ZIP file, (b) decide whether a ZIP looks like an Apple Health export,
and (c) consult the ``imports`` table to cheaply skip work for ZIPs
the importer has already ingested. Hosting these in one module keeps
the "what counts as a ZIP we care about" rule a single source of
truth — pre-extraction the two tools had byte-identical copies of
each helper and were drifting on the chunk-size constant in
particular.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Final

from apple_health_mcp.server.query import query_to_json

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


# 1 MB chunk for streaming sha256 — matches the constant family the
# importer uses (``importers.xml._READ_CHUNK_BYTES``) so a future
# tuning change lands in one place.
SHA256_READ_CHUNK_BYTES: Final[int] = 1024 * 1024

# sha256 short-prefix length used as the ``id`` field on the wire. 8
# hex chars = 32 bits of entropy; collision probability is ~negligible
# for the realistic case of ≤100 ZIPs in a user's drop-zone.
ID_PREFIX_LEN: Final[int] = 8

# Top-level ZIP entries that mark a ZIP as an Apple Health export.
# Apple's Health-app share-sheet always emits ``apple_health_export/``;
# some third-party repackagers flatten the contents at the root. Both
# shapes are accepted; anything else is flagged so the agent can ask
# "did you mean a different ZIP?" before paying the import cost.
APPLE_HEALTH_TOP_LEVEL_MARKERS: Final[frozenset[str]] = frozenset(
    {"apple_health_export/export.xml", "export.xml"}
)


def stream_sha256(path: Path) -> str:
    """Return the hex sha256 of ``path`` by streaming 1 MB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(SHA256_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class ZipInspection(StrEnum):
    """Three-state classification of a candidate ``*.zip`` path (v0.4.1, issue #158).

    Replaces the prior binary ``is_apple_health_zip`` view that conflated
    "the file is not a valid ZIP archive at all" with "the file is a
    valid ZIP archive that just doesn't contain Apple Health data". The
    two failure modes need different recovery actions: an INVALID_ZIP
    means the user should re-download the file (corruption, partial
    transfer, an HTML page renamed to ``.zip``), while a
    VALID_NON_APPLE_HEALTH means the user picked the wrong file. The
    legacy helper still returns a single bool for backward compatibility
    with the rest of the v0.4 import pipeline.
    """

    VALID_APPLE_HEALTH = "valid_apple_health"
    VALID_NON_APPLE_HEALTH = "valid_non_apple_health"
    INVALID_ZIP = "invalid_zip"


def inspect_zip(path: Path) -> ZipInspection:
    """Classify ``path`` into one of the :class:`ZipInspection` states.

    Returns ``INVALID_ZIP`` (not an exception) when the file is not a
    valid ZIP archive at all (corruption, partial transfer, an HTML
    error page renamed to ``.zip``, etc.). Returns
    ``VALID_NON_APPLE_HEALTH`` for a parseable ZIP that lacks the
    expected top-level marker file, and ``VALID_APPLE_HEALTH`` when
    the marker is present. The single-pass parse keeps the discovery
    surface uniform; downstream callers branch on the enum.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        _logger.debug("zip inspect: %s is not a valid ZIP (%s)", path, exc)
        return ZipInspection.INVALID_ZIP
    if any(name in APPLE_HEALTH_TOP_LEVEL_MARKERS for name in names):
        return ZipInspection.VALID_APPLE_HEALTH
    return ZipInspection.VALID_NON_APPLE_HEALTH


def is_apple_health_zip(path: Path) -> bool:
    """Backward-compatible thin wrapper over :func:`inspect_zip`.

    Returns ``True`` only when the file is a valid ZIP archive that
    contains the expected Apple Health top-level marker; every other
    state (invalid ZIP, valid ZIP without the marker) reads as
    ``False``. The richer :class:`ZipInspection` enum is the
    preferred entry point for new callers, but ``list_zips`` keeps
    emitting the boolean alongside the new ``zip_status`` field for
    wire compatibility with v0.4.0 consumers.
    """
    return inspect_zip(path) == ZipInspection.VALID_APPLE_HEALTH


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
