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

import logging
from threading import Lock
from typing import TYPE_CHECKING

from apple_health_mcp.server.query import (
    query_to_json,
    require_imports_or_message,
    run_query_payload,
)

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


DESCRIPTION = (
    "Return the Apple Health 'Me' characteristic attributes for the most "
    "recent import: import_id, date_of_birth, biological_sex, blood_type, "
    "fitzpatrick_skin_type, cardio_fitness_medications_use. Each field is "
    "null if the export omitted it. Returns an empty object {} when an "
    "import has happened but the export did not include a Me element. "
    "Before any import has been run, returns the standard "
    "'No Apple Health data has been imported yet.' guidance string "
    "instead of an empty object so the LLM has an actionable next step."
)

# "Most recent" follows the same definition `get_import_history` exposes to
# clients: the import with the latest wall-clock `imported_at`. Joining
# through `imports` keeps the two tools aligned even when the caller
# supplies a non-timestamp `import_id` to `run_import` (in which case a
# plain `ORDER BY import_id DESC` would silently flip to lexicographic
# order and disagree with `get_import_history`'s ranking). The
# `m.import_id DESC` tie-break mirrors the dedupe ORDER BY in
# db/schema.py for the rare case where `imported_at` collides.
_SQL = (
    "SELECT m.import_id, m.date_of_birth, m.biological_sex, m.blood_type, "
    "m.fitzpatrick_skin_type, m.cardio_fitness_medications_use "
    "FROM me_attributes m JOIN imports i USING (import_id) "
    "ORDER BY i.imported_at DESC, m.import_id DESC LIMIT 1"
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_me_attributes() -> str:
        if msg := require_imports_or_message(conn, lock=lock):
            return msg
        try:
            rows = query_to_json(conn, _SQL, lock=lock)
        except Exception as exc:
            # Match `server/query.py::run_query` observability: the wire
            # response carries the error string but stderr also gets a debug
            # line so operators can correlate one with the other.
            _logger.debug("get_me_attributes query failed: %s", exc)
            return f"Error: {exc}"
        return run_query_payload(rows[0] if rows else {})
