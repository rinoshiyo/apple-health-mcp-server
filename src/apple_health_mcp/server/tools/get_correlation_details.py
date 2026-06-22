"""``get_correlation_details`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import (
    IMPORT_REQUIRED_MESSAGE,
    imports_present,
    query_to_json,
    run_query_payload,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get the full member set of a Correlation (e.g. all records in a "
    "blood-pressure reading or a meal). Returns: correlation object "
    "(correlation_hash, correlation_type, source_name, start_date, end_date), "
    "and members array of {record_hash, record_type, value, unit, start_date, "
    "end_date}. For a blood-pressure correlation the members will include "
    "both the Systolic and Diastolic Records joined from the records table."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_correlation_details(
        correlation_hash: Annotated[
            str,
            Field(description="The correlation hash identifier"),
        ],
    ) -> str:
        try:
            if not imports_present(conn, lock=lock):
                return IMPORT_REQUIRED_MESSAGE
            correlation_rows = query_to_json(
                conn,
                "SELECT correlation_hash, correlation_type, source_name, "
                "source_version, creation_date, start_date, end_date "
                "FROM correlations WHERE correlation_hash = ?",
                [correlation_hash],
                lock=lock,
            )
            members = query_to_json(
                conn,
                "SELECT r.record_hash, r.record_type, r.value, r.unit, "
                "r.start_date, r.end_date FROM correlation_members cm "
                "JOIN records r ON r.record_hash = cm.record_hash "
                "WHERE cm.correlation_hash = ? ORDER BY r.record_type",
                [correlation_hash],
                lock=lock,
            )
        except Exception as exc:
            return f"Error: {exc}"
        payload = {
            "correlation": correlation_rows[0] if correlation_rows else None,
            "members": members,
        }
        return run_query_payload(payload)
