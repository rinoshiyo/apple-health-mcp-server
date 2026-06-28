"""Shared ZIP-handling utilities used by both the CLI and MCP server.

v0.5 (issue #170) consolidates the previously-duplicated ZIP
classification / sha256 streaming code so the CLI's ZIP-only ``import``
subcommand and the MCP ``import_zip`` tool share a single source of
truth. The :mod:`apple_health_mcp.server.tools._zip_inspect` module
re-exports these symbols for backward compatibility with v0.4 callers.

This module intentionally lives at the package root (not under
``server/`` or ``importers/``) because both subtrees need it and a
one-way ``importers → server`` or ``server → importers`` dependency
would introduce a layering inversion.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from enum import StrEnum
from pathlib import Path
from typing import Final

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


class ZipInspection(StrEnum):
    """Three-state classification of a candidate ``*.zip`` path (v0.4.1, issue #158).

    The two failure modes need different recovery actions: an
    INVALID_ZIP means the user should re-download the file (corruption,
    partial transfer, an HTML page renamed to ``.zip``), while a
    VALID_NON_APPLE_HEALTH means the user picked the wrong file.
    """

    VALID_APPLE_HEALTH = "valid_apple_health"
    VALID_NON_APPLE_HEALTH = "valid_non_apple_health"
    INVALID_ZIP = "invalid_zip"


def stream_sha256(path: Path) -> str:
    """Return the hex sha256 of ``path`` by streaming 1 MB chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(SHA256_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def inspect_zip(path: Path) -> ZipInspection:
    """Classify ``path`` into one of the :class:`ZipInspection` states.

    Returns ``INVALID_ZIP`` (not an exception) when the file is not a
    valid ZIP archive at all (corruption, partial transfer, an HTML
    error page renamed to ``.zip``). Returns ``VALID_NON_APPLE_HEALTH``
    for a parseable ZIP that lacks the expected top-level marker file,
    and ``VALID_APPLE_HEALTH`` when the marker is present.
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
    """Backward-compatible thin wrapper over :func:`inspect_zip`."""
    return inspect_zip(path) == ZipInspection.VALID_APPLE_HEALTH


__all__ = [
    "APPLE_HEALTH_TOP_LEVEL_MARKERS",
    "ID_PREFIX_LEN",
    "SHA256_READ_CHUNK_BYTES",
    "ZipInspection",
    "inspect_zip",
    "is_apple_health_zip",
    "stream_sha256",
]
