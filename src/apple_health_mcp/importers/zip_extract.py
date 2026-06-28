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

import logging
import tempfile
import zipfile
from pathlib import Path
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
    """
    with tempfile.TemporaryDirectory(prefix="apple-health-zip-") as tmpdir:
        extracted_root = Path(tmpdir)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted_root)
        # Apple Health ships the export as ``apple_health_export/`` at
        # the top level; some repackagers flatten it. Resolve whichever
        # shape we got into the path the importer expects.
        if (extracted_root / "apple_health_export" / "export.xml").exists():
            import_root = extracted_root / "apple_health_export"
        else:
            import_root = extracted_root
        return run_import(
            import_root,
            db_path=db_path,
            conn=conn,
            import_id=import_id,
            force=force,
            source_zip=source_zip,
        )


__all__ = ["extract_zip_and_import"]
