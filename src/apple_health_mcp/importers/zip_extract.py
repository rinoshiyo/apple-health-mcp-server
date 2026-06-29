"""Extract + import-from-ZIP helper shared by the CLI and ``import_zip`` tool.

v0.5 (issue #170) consolidates the previously-duplicated
"extract ZIP into tempdir, resolve apple_health_export/ nesting,
delegate to run_import" sequence so the CLI ``import <zip>`` and the
MCP ``import_zip(id=...)`` tool go through the same code path. The
caller computes the ``source_zip`` triple itself so id-driven callers
(MCP tool) can reuse the sha they already hashed during id
resolution instead of paying for a second multi-GB sha pass.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.importers.orchestrator import run_import
from apple_health_mcp.importers.xml import ImportStats

if TYPE_CHECKING:
    from datetime import datetime

    import duckdb

_logger = logging.getLogger(__name__)


def extract_zip_and_import(
    zip_path: Path,
    source_zip: tuple[str, datetime, int],
    *,
    db_path: Path | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
    import_id: str | None = None,
    force: bool = False,
    lock: Lock | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> ImportStats:
    """Extract ``zip_path`` into a tempdir and run the full import pipeline.

    The caller MUST have already verified the ZIP shape via
    :func:`apple_health_mcp._zip_util.inspect_zip` (returning
    ``VALID_APPLE_HEALTH``) before calling this helper. Extraction
    failures (``BadZipFile``, ``OSError``) propagate to the caller so
    each entry point can frame the user-facing message in its own
    idiom (typed envelope for the MCP tool, exit-with-error for the
    CLI).

    ``source_zip`` is the ``(sha256_hex, mtime, size_bytes)`` triple
    that ``run_import`` stamps into the matching ``imports`` row.
    Passed by the caller so id-driven callers (the MCP tool) can hand
    over the sha they already streamed during id resolution; the CLI
    streams a fresh one.

    ``conn`` / ``db_path`` are mutually-exclusive forwards to
    ``run_import`` (it raises ``ValueError`` when both are passed).
    Tempdir cleanup is automatic via ``TemporaryDirectory``; the
    extracted files do NOT survive beyond the ``run_import`` call.

    ``lock`` (v0.5, issue #173) is held ONLY around the ``run_import``
    call, NOT during the multi-second ZIP extraction. Pre-v0.5 the
    MCP ``import_zip`` tool wrapped the whole call in ``with lock:``,
    so concurrent read tools were blocked for the full extract +
    import window. The helper now acquires the lock at the importer
    boundary so concurrent reads only wait for the run_import phase.
    ``None`` is fine for single-thread callers (CLI: no shared
    connection, so no lock needed).
    """
    if phase_callback is not None:
        phase_callback("extracting")
    with tempfile.TemporaryDirectory(prefix="apple-health-zip-") as tmpdir:
        extracted_root = Path(tmpdir)
        # v0.5 (PR #172 code-review #1/#2): scope the extraction-phase
        # try block tightly around ``extractall``. The caller's broad
        # ``except (BadZipFile, OSError)`` used to wrap the full
        # run_import body too, so a DuckDB OSError (disk full, EIO,
        # permission denied) would dress up as "zip_extract_failed"
        # and the agent / CLI would tell the user to re-download the
        # ZIP. Narrowing the wrap here keeps the misclassification
        # contained: extraction-time errors stay BadZipFile / OSError,
        # importer-time errors raise as AppleHealthMCPError /
        # database-flavored exceptions for the caller to surface
        # under their own envelope.
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extracted_root)
        except OSError as exc:
            # OS-level IO from ``extractall`` (ENOSPC mid-write, EACCES,
            # truncated archive surfaced through a read error, etc.).
            # Re-raise as ``BadZipFile`` so the caller's narrow
            # extract-phase handler treats it uniformly with corruption
            # failures -- both cases share the "this ZIP cannot be
            # unpacked; re-download or pick another file" recovery
            # action. Keeps OSError flavors from inside ``run_import``
            # (DuckDB writes, ECG/GPX file IO) separable at the caller.
            raise zipfile.BadZipFile(f"extraction failed before run_import: {exc}") from exc
        # Apple Health ships the export as ``apple_health_export/`` at
        # the top level; some repackagers flatten it. Resolve whichever
        # shape we got into the path the importer expects.
        if (extracted_root / "apple_health_export" / "export.xml").exists():
            import_root = extracted_root / "apple_health_export"
        else:
            import_root = extracted_root
        # v0.5 (issue #173): hold the lock only around the importer
        # call so concurrent read tools do not pay the multi-second
        # extract phase.
        lock_ctx = lock if lock is not None else contextlib.nullcontext()
        with lock_ctx:
            return run_import(
                import_root,
                db_path=db_path,
                conn=conn,
                import_id=import_id,
                force=force,
                source_zip=source_zip,
                phase_callback=phase_callback,
            )


__all__ = ["extract_zip_and_import"]
