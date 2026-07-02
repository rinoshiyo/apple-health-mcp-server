"""``get_import_history`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import run_query
from apple_health_mcp.server.tools._gates import schema_gated_tool

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List all data imports. Returns: import_id, export_dir, imported_at, "
    "record_count (Phase-1 parse count of Apple Health <Record> elements, "
    "BEFORE Correlation-child dedup), workout_count, processing_secs "
    "(run_import body wall-clock: Phase-1 XML parse + Phase-2 ECG + "
    "Phase-3 GPX + Phase-4 finalize; ZIP extraction time is NOT included. "
    "For the running → ok worker wall-clock -- including ZIP "
    "extraction -- consult the matching ``import_jobs`` row via "
    "``run_custom_query`` (``SELECT job_id, duration_secs FROM "
    "import_jobs WHERE source_sha256 = (SELECT source_zip_sha256 FROM "
    "imports WHERE import_id = ?)``); the live polling tool "
    "``get_import_status(job_id=...)`` exposes the same number only "
    "while a job_id from the current session is in hand. The two "
    "values can differ by several seconds on large ZIPs.), "
    "export_xml_sha256 (hex sha256 of the source export.xml; NULL on rows "
    "finalized before the column was introduced), records_after_dedup "
    "(rows surviving in the records table after Phase 4 Correlation "
    "dedup; record_count - records_after_dedup is the number of "
    "Correlation duplicates collapsed -- Apple duplicates Correlation "
    "children at the top level by spec. NULL on rows finalized before "
    "v0.3.0 #129 AND on Tier-2 incremental re-imports where the dedup "
    "pass was skipped -- treat NULL as 'no dedup measurement available' "
    "rather than computing a misleading delta), dedup_skipped (v0.5 #163; "
    "true on Tier-2 incremental re-imports where the Phase-4 dedup "
    "pass was skipped on purpose -- records_after_dedup IS NULL by "
    "design; false on Tier-1 fresh imports where measurement happened, "
    "including the zero-collapse case where records_after_dedup == "
    "record_count. Always non-NULL on v=6 DBs under the fresh-reset "
    "contract), source_zip_sha256 / source_zip_mtime / "
    "source_zip_size (identity of the source ZIP for re-import dedup; "
    "NULL on rows produced by the CLI `import <dir>` path because the "
    "source artefact was a directory)."
)

# Explicit column list (rather than ``SELECT *``) mirrors the audit-batch
# principle applied to T5 / T6 / T12: future ``ALTER TABLE imports ADD
# COLUMN`` work cannot leak into the wire shape without a deliberate
# description / schema bump. The column order matches the description
# above so the LLM-facing prose and SQL projection stay in sync.
#
# ``duration_secs AS processing_secs`` (v0.5.1 #189): the underlying
# column captures only the run_import body wall-clock, while the v0.5
# ``get_import_status.duration_secs`` field reports the full worker
# wall-clock including ZIP extraction. Same column name on the wire
# returning different values was a documented confusion source from
# v0.5.0 dogfood; the alias splits the two on the wire while leaving
# the DB shape unchanged (so ``run_custom_query`` on
# ``imports.duration_secs`` continues to work).
_SQL = (
    "SELECT import_id, export_dir, imported_at, record_count, "
    "workout_count, duration_secs AS processing_secs, export_xml_sha256, "
    "records_after_dedup, dedup_skipped, source_zip_sha256, "
    "source_zip_mtime, source_zip_size "
    "FROM imports ORDER BY imported_at DESC"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    # v0.5.1 #188 (post-#195 code-review Angle C; issue #198): the
    # SELECT references ``dedup_skipped`` (added in v=6 / v0.5 #163),
    # which is absent from v=5-or-earlier ``imports`` shapes. The
    # schema_outdated gate injected by ``schema_gated_tool`` fires before the
    # SELECT so a pre-v0.5 DB surfaces the typed envelope instead of a
    # raw DuckDB column-missing error. ``require_data=False`` is
    # preserved on the READY/NEEDS_CONFIG/NEEDS_IMPORT path so "empty
    # imports list" stays observable.
    @schema_gated_tool(mcp, conn, lock, description=DESCRIPTION)
    async def get_import_history() -> str:
        return run_query(conn, _SQL, lock=lock, require_data=False)
