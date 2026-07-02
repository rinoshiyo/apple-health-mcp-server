"""Registration helpers that inject data-state gates at tool registration.

Every MCP tool that touches ``imports`` / ``import_jobs`` needs to
short-circuit before its own SQL runs when the DB is not READY
(the ``require_ready_or_state_error`` surface) or when the schema
is outdated (the ``block_if_schema_outdated`` surface). Before
issue #198 each tool hand-rolled the gate check as the first
statement in its dispatch, which invited two regressions:

* Adding a new schema-gated tool without the gate call surfaces
  the raw ``Catalog Error: Table import_jobs does not exist`` on
  a v=5-or-earlier DB (the v0.5.0 dogfood observation that
  motivated ``block_if_schema_outdated`` in the first place).
* Adding a new READY-gated tool without ``require_ready_or_state_error``
  surfaces the same catalog error family on any pre-import DB.

The two decorators below move the gate to tool-registration time
so a new tool inherits the correct gate by picking the correct
decorator, not by remembering to write the guard clause.

The gate wrappers are ``functools.wraps``-transparent, so
FastMCP's signature-driven schema generation still sees each
tool's original parameter list.

Design notes:

* The gate probe runs on the event loop (before ``asyncio.to_thread``
  where applicable). This is intentional: the schema-freshness
  cache (issue #197 / ``_SCHEMA_FRESH_DECIDED``) makes the hot-
  path check a lock-free set-membership probe (microseconds); the
  cache-miss path is at most two DuckDB roundtrips (sub-
  millisecond). Neither cost warrants scheduling on a worker
  thread. On a stale DB, running the gate before ``to_thread``
  ALSO skips the thread-pool spawn entirely — a net win.
* Naming reflects the gate function, not SQL semantics. Three of
  the four ``schema_gated_tool`` callers (``list_zips``,
  ``get_import_status``, ``get_import_history``) are pure reads
  that just have to tolerate ``NEEDS_CONFIG`` / ``NEEDS_IMPORT``;
  only ``import_zip`` mutates. Read/write verbs would mis-label
  three of four sites — the gate-name convention keeps the
  decorator honest.
* Presets, not a composable ``gates=[...]`` list. Issue #198
  sketched a composable primitive; the surface today is 2x2 (2
  gates x 2 wrappers), so presets pay for themselves. Revisit
  when a third gate lands (auth, rate limit, ...); a follow-up
  issue tracks the migration path.
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
    GateFn = Callable[..., str | None]


def _build_gated_registrar(
    gate_fn: GateFn,
) -> Callable[..., Callable[[ToolFn], ToolFn]]:
    """Return a registration decorator that runs ``gate_fn`` before the tool body.

    ``gate_fn`` is expected to take ``(conn, *, lock=...)`` and return
    the JSON envelope string when the tool should short-circuit,
    ``None`` otherwise. Every gate wrapper in the codebase follows the
    same shape, so both public decorators below thread the differing
    gate through this common structure and add no bespoke wrapping
    logic — a fix to ``functools.wraps`` handling, error mapping, or
    schema-generation compatibility only needs to be applied here.
    """

    def registrar(
        mcp: FastMCP,
        conn: duckdb.DuckDBPyConnection,
        lock: Lock,
        *,
        description: str,
    ) -> Callable[[ToolFn], ToolFn]:
        def decorator(tool_fn: ToolFn) -> ToolFn:
            @functools.wraps(tool_fn)
            async def gated(*args: Any, **kwargs: Any) -> str:
                if (envelope := gate_fn(conn, lock=lock)) is not None:
                    return envelope
                return await tool_fn(*args, **kwargs)

            return mcp.tool(description=description)(gated)

        return decorator

    return registrar


schema_gated_tool = _build_gated_registrar(block_if_schema_outdated)
"""Register a tool guarded by ``block_if_schema_outdated``.

The gate returns the ``NEEDS_REIMPORT`` envelope on a stale DB and
lets ``READY`` / ``NEEDS_CONFIG`` / ``NEEDS_IMPORT`` through — used
by the four ZIP-flow entry points that need to tolerate the
recover-from states but must refuse to run against a stale schema:
``list_zips``, ``import_zip``, ``get_import_status``,
``get_import_history``.
"""


ready_gated_tool = _build_gated_registrar(require_ready_or_state_error)
"""Register a tool guarded by ``require_ready_or_state_error``.

The gate returns an error envelope for any state other than
``READY`` — used by tools that would otherwise surface a raw
DuckDB catalog error against an empty / not-yet-imported DB.
The four current callers (``get_correlation_details``,
``get_workout_details``, ``get_me_attributes``, ``get_ecg_data``)
previously hand-rolled ``require_imports_or_message`` at the top
of their bodies. Tools that go through ``run_query(...,
require_data=True)`` already inherit the same gate inside
``run_query`` and do not need this wrapper — the bifurcation is
tracked at #TBD-gate-unification for a follow-up refactor.
"""
