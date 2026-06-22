"""``get_me_attributes`` MCP tool (new in the Python port).

Apple Health writes a single ``<Me ...>`` element at the root of every
export, carrying five fixed characteristic attributes: date of birth,
biological sex, blood type, Fitzpatrick skin type, and cardio-fitness
medications use. The XML importer already lands them in the dedicated
``me_attributes`` table (one row per ``import_id``); this tool exposes
that row so an LLM consumer asking "what's my blood type?" does not have
to guess the table name through ``run_custom_query``.

Same pattern as ``list_state_of_mind`` — break a structured Apple Health
element out of the generic record stream into a first-class tool.
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import query_to_json, run_query_payload

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Return the Apple Health 'Me' characteristic attributes for the most "
    "recent import: import_id, date_of_birth, biological_sex, blood_type, "
    "fitzpatrick_skin_type, cardio_fitness_medications_use. Each field is "
    "null if the export omitted it. Returns an empty object {} when no "
    "import has populated the me_attributes table yet."
)

# Match the dedupe ORDER BY in db/schema.py (``import_id DESC``): "most
# recent" is the lexicographically-greatest import_id, the same convention
# used everywhere else in this codebase for picking a winner across
# multiple imports.
_SQL = (
    "SELECT import_id, date_of_birth, biological_sex, blood_type, "
    "fitzpatrick_skin_type, cardio_fitness_medications_use "
    "FROM me_attributes ORDER BY import_id DESC LIMIT 1"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_me_attributes() -> str:
        try:
            rows = query_to_json(conn, _SQL, lock=lock)
        except Exception as exc:
            return f"Error: {exc}"
        return run_query_payload(rows[0] if rows else {})
