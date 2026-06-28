"""``get_import_history`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List all data imports. Returns: import_id, export_dir, imported_at, "
    "record_count (Phase-1 parse count of Apple Health <Record> elements, "
    "BEFORE Correlation-child dedup), workout_count, duration_secs, "
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
_SQL = (
    "SELECT import_id, export_dir, imported_at, record_count, "
    "workout_count, duration_secs, export_xml_sha256, records_after_dedup, "
    "dedup_skipped, source_zip_sha256, source_zip_mtime, source_zip_size "
    "FROM imports ORDER BY imported_at DESC"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_import_history() -> str:
        # ``require_data=False`` because "list imports" is the canonical way
        # to confirm the empty-DB state — returning the guidance message
        # would make it impossible to ever observe the empty list.
        return run_query(conn, _SQL, lock=lock, require_data=False)
