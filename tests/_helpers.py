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


def seed_one_import(conn: duckdb.DuckDBPyConnection) -> None:
    """Insert a single placeholder row into ``imports``.

    Several DB-error-path tests need the empty-DB gate to pass so the tool
    actually reaches the SQL it is about to fail on. Hoisting the literal
    here keeps the placeholder columns in one place, matching the synthetic
    fixture pattern documented in ``tests/fixtures/README.md`` (no real
    device UUIDs or source names anywhere in the repo).
    """
    conn.execute(
        "INSERT INTO imports VALUES "
        "('imp1', '/tmp/x', TIMESTAMPTZ '2024-01-01 00:00:00+00', "
        "0, 0, 0, NULL, 0, NULL, NULL, NULL)"
    )


def assert_tool_db_error(fn: Callable[..., Awaitable[str]], **kwargs: Any) -> str:
    """Call ``fn`` and assert it produced an ``Error: ...`` string.

    Returns the error string so individual tests can layer additional
    assertions on the message body when they need to (e.g. checking that a
    specific exception class name leaked through).
    """
    out = asyncio.run(fn(**kwargs))
    assert out.startswith("Error: "), f"expected an Error: response, got: {out!r}"
    return out
