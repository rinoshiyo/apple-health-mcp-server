"""Registration helpers that inject data-state gates at tool registration.

Every MCP tool that touches ``imports`` / ``import_jobs`` needs to
short-circuit before its own SQL runs when the DB is not READY
(read tools) or when the schema is outdated (write tools). Before
issue #198 each tool hand-rolled the gate check as the first
statement in its dispatch:

.. code-block:: python

    @mcp.tool(description=DESCRIPTION)
    async def list_zips() -> str:
        if (envelope := block_if_schema_outdated(conn, lock=lock)) is not None:
            return envelope
        ...

Which invited two regressions:

* Adding a new write tool without the gate call surfaces the raw
  ``Catalog Error: Table import_jobs does not exist`` on a v=5-or-
  earlier DB (the v0.5.0 dogfood observation that motivated
  ``block_if_schema_outdated`` in the first place).
* Adding a new read tool without ``require_imports_or_message``
  surfaces the same catalog error family on any pre-import DB.

The two helpers here move the gate to tool-registration time so
new tools inherit the correct gate by picking the correct
decorator, not by remembering to write the guard clause.

The gate wrappers are ``functools.wraps``-transparent, so
FastMCP's signature-driven schema generation still sees each
tool's original parameter list.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from apple_health_mcp.server.data_state import (
    block_if_schema_outdated,
    require_ready_or_state_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from threading import Lock

    import duckdb
    from mcp.server.fastmcp import FastMCP

    ToolFn = Callable[..., Awaitable[str]]


def write_tool(
    mcp: FastMCP,
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    *,
    description: str,
) -> Callable[[ToolFn], ToolFn]:
    """Register a write-side MCP tool with ``block_if_schema_outdated`` injected.

    Use for tools that can tolerate ``NEEDS_CONFIG`` / ``NEEDS_IMPORT``
    (the ZIP-flow entry points: ``list_zips``, ``import_zip``,
    ``get_import_status``, ``get_import_history``) but must NOT proceed
    against a stale schema. The injected gate returns the
    ``NEEDS_REIMPORT`` envelope on stale DBs and lets everything else
    through.
    """

    def decorator(tool_fn: ToolFn) -> ToolFn:
        @functools.wraps(tool_fn)
        async def gated(*args: Any, **kwargs: Any) -> str:
            if (envelope := block_if_schema_outdated(conn, lock=lock)) is not None:
                return envelope
            return await tool_fn(*args, **kwargs)

        return mcp.tool(description=description)(gated)

    return decorator


def read_tool(
    mcp: FastMCP,
    conn: duckdb.DuckDBPyConnection,
    lock: Lock,
    *,
    description: str,
) -> Callable[[ToolFn], ToolFn]:
    """Register a read-side MCP tool with ``require_ready_or_state_error`` injected.

    Use for tools that must not run against a DB that is not READY
    (the pre-v0.4 ``require_imports_or_message`` surface: the four
    tools currently rewriting the guard clause manually --
    ``get_correlation_details``, ``get_workout_details``,
    ``get_me_attributes``, ``get_ecg_data``). Tools that go through
    ``run_query(..., require_data=True)`` already inherit the same
    gate inside ``run_query`` and do not need this wrapper.
    """

    def decorator(tool_fn: ToolFn) -> ToolFn:
        @functools.wraps(tool_fn)
        async def gated(*args: Any, **kwargs: Any) -> str:
            if msg := require_ready_or_state_error(conn, lock=lock):
                return msg
            return await tool_fn(*args, **kwargs)

        return mcp.tool(description=description)(gated)

    return decorator
