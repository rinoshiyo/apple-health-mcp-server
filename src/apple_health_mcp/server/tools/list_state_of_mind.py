"""``list_state_of_mind`` MCP tool (new in the Python port).

iOS 17 introduced ``HKStateOfMind``; Apple emits it through the export XML
as ``HKCategoryTypeIdentifierStateOfMind`` Category records carrying a
``valence`` (-1.0 .. +1.0), a ``kind`` (momentary / daily), and free-form
``labels`` / ``associations`` lists in metadata. The Rust port lumped those
in with the generic Category path and lost the structure; the Python
importer breaks them out into the ``state_of_mind`` table so this tool can
return them as first-class fields.
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from apple_health_mcp.server.query import run_query

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "List Apple Health StateOfMind (iOS 17+) entries with their valence, "
    "kind (momentary / daily), labels (e.g. Joy, Calm), and associations "
    "(e.g. Work, Family). Returns: record_hash, start_date, end_date, "
    "valence, kind, labels, associations, source_name. Use this for "
    'natural-language queries about mood over time ("show my mood over '
    'the past week").'
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def list_state_of_mind(
        start_date: Annotated[
            str | None,
            Field(description="Start date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="End date filter (ISO 8601 / YYYY-MM-DD)"),
        ] = None,
        limit: Annotated[
            int | None,
            Field(description="Maximum number of results (default 100, max 1000)"),
        ] = None,
    ) -> str:
        effective_limit = min(limit or _DEFAULT_LIMIT, _MAX_LIMIT)
        # The join surfaces the timestamp / source through the parent record
        # so callers can query mood over time without a follow-up lookup.
        sql_parts = [
            "SELECT s.record_hash, r.start_date, r.end_date, s.valence, "
            "s.kind, s.labels, s.associations, r.source_name "
            "FROM state_of_mind s "
            "JOIN records r ON r.record_hash = s.record_hash WHERE 1=1"
        ]
        params: list[Any] = []
        if start_date is not None:
            sql_parts.append("AND r.start_date >= ?")
            params.append(start_date)
        if end_date is not None:
            sql_parts.append("AND r.end_date <= ?")
            params.append(end_date)
        sql_parts.append(f"ORDER BY r.start_date DESC LIMIT {effective_limit}")
        return run_query(conn, " ".join(sql_parts), params, lock=lock)
