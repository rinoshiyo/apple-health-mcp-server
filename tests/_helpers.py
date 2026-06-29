"""Shared test helpers reused across the unit and integration suites.

The MCP tool modules expose a ``register(mcp, conn, lock)`` callable that
attaches the underlying coroutine to a FastMCP instance via a
``@mcp.tool`` decorator. Tests want to call the coroutine directly without
spinning up FastMCP, so they pass a stub MCP whose ``tool`` decorator just
captures the registered function. The capture/bind/call trio used to live
inline in both ``tests/unit/server/test_tools.py`` and
``tests/integration/test_smoke.py``; consolidating them here keeps the two
suites locked to the same registration contract when it evolves.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Any

import duckdb


class StubMCP:
    """Capture the function a tool module registers via ``@mcp.tool``."""

    def __init__(self) -> None:
        self.fn: Callable[..., Awaitable[str]] | None = None
        self.description: str = ""

    def tool(self, *, description: str = "") -> Callable[..., Any]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            # Tool modules register exactly one coroutine each; a second
            # registration would silently shadow the first, masking a
            # regression that this helper is supposed to catch. Fail loud
            # instead.
            assert self.fn is None, (
                "StubMCP captured more than one @mcp.tool registration; "
                "each tool module is expected to register exactly one."
            )
            self.fn = fn
            self.description = description
            return fn

        return decorator


def bind_tool(module: Any, conn: duckdb.DuckDBPyConnection) -> Callable[..., Awaitable[str]]:
    """Run ``module.register`` against a fresh ``StubMCP`` and return the captured fn."""
    stub = StubMCP()
    module.register(stub, conn, Lock())
    assert stub.fn is not None
    assert stub.description
    return stub.fn


def call_tool(fn: Callable[..., Awaitable[str]], **kwargs: Any) -> Any:
    """Invoke a bound tool coroutine and decode its JSON return value.

    MCP tools return their payload as a JSON-encoded string by contract, or
    a literal ``"Error: ..."`` string when input validation rejects the
    request. ``json.loads`` would crash on the latter; raise an explicit
    ``AssertionError`` instead so test failures point at the error message
    rather than at a JSONDecodeError stack.
    """
    raw = asyncio.run(fn(**kwargs))
    assert not raw.startswith("Error: "), f"tool returned a validation error: {raw}"
    return json.loads(raw)


def seed_one_import(
    conn: duckdb.DuckDBPyConnection,
    *,
    import_id: str = "imp1",
) -> None:
    """Insert a single placeholder row into ``imports``.

    Several DB-error-path tests need the empty-DB gate to pass so the tool
    actually reaches the SQL it is about to fail on. Hoisting the literal
    here keeps the placeholder columns in one place, matching the synthetic
    fixture pattern documented in ``tests/fixtures/README.md`` (no real
    device UUIDs or source names anywhere in the repo).

    Uses a column-list INSERT so future nullable column adds to ``imports``
    do not require an N-place rewrite across the test suite: a new column
    that schema declares NULL-able lands NULL implicitly here without any
    test edit. The same pattern is already used by
    ``tests/unit/importers/test_incremental_reimport.py``; the first
    v0.4 (issue #148) PR promotes it to the single seed helper so each
    future schema-add bump touches only schema + this file.

    ``import_id`` is overridable for the few sites that pin a specific
    value; the default ``"imp1"`` matches the previous inline literal.
    """
    conn.execute(
        "INSERT INTO imports (import_id, export_dir, imported_at) "
        "VALUES (?, '/tmp/x', TIMESTAMPTZ '2024-01-01 00:00:00+00')",
        [import_id],
    )


def drain_import_workers(timeout: float = 30.0) -> None:
    """Wait for every ``import-zip-*`` daemon thread spawned by ``import_zip``.

    v0.5 (issue #157) async ``import_zip`` returns ``status: 'queued'``
    immediately and runs the importer in a daemon thread named
    ``import-zip-<job_id>``. Tests that assert on the final ``import_jobs``
    state must join those threads first, but the dispatcher does not
    surface the ``Thread`` handle; this helper walks
    ``threading.enumerate()`` and joins each match.

    Per-thread timeout. Synthetic fixtures finish in tens of
    milliseconds; production-scale would take much longer (the entire
    point of the async refactor in issue #157).
    """
    deadline = time.monotonic() + timeout
    for thread in list(threading.enumerate()):
        if thread.name.startswith("import-zip-") and thread.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            if thread.is_alive():  # pragma: no cover - defensive
                raise TimeoutError(f"import worker {thread.name} did not finish in {timeout}s")


def assert_tool_db_error(fn: Callable[..., Awaitable[str]], **kwargs: Any) -> str:
    """Call ``fn`` and assert it produced an ``Error: ...`` string.

    Returns the error string so individual tests can layer additional
    assertions on the message body when they need to (e.g. checking that a
    specific exception class name leaked through).
    """
    out = asyncio.run(fn(**kwargs))
    assert out.startswith("Error: "), f"expected an Error: response, got: {out!r}"
    return out
