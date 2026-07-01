"""Adversarial pins for the v0.6 phase:1 input-validation hardening (#224 / #229).

These tests exercise the *real* FastMCP argument-validation boundary --
``mcp.call_tool(name, arguments)`` -- rather than calling the bound tool
coroutine directly (as ``tests/_helpers.py::call_tool`` does for the happy
path). The pydantic ``Field(max_length=..., ge=..., le=...)`` constraints
added on every tool's ``Annotated[...]`` signature are only enforced by
FastMCP's own argument-binding layer (``fn_metadata.call_fn_with_arg_
validation``), which builds and validates a pydantic model from the
signature before the coroutine body ever runs. Calling the coroutine
directly (the ``StubMCP`` pattern) bypasses that layer entirely, so a
regression here would go undetected by the rest of the suite.

Test functions stay synchronous and drive the async ``mcp.call_tool``
calls through ``asyncio.run`` -- matching the existing suite convention
in ``tests/_helpers.py::call_tool`` -- since the project does not
register an async pytest plugin (pytest-asyncio / pytest-anyio).

Separate file from ``test_security.py`` by design (see PR description):
that file is being edited concurrently by another in-flight PR that
hardens the SQL-safety denylist, and both files would otherwise collide
on the same lines.

Historical context:

* #224 -- ``query_records(source_name="A" * 12000)`` hung the server
  (v0.5.1 dogfood Phase 3 defect #4): a 12,000-char VARCHAR bind
  parameter drove pathological DuckDB memory/CPU use. The fix caps
  every free-form string parameter at the tool-schema level so the
  request is rejected before it ever reaches DuckDB.
* #229 -- ``query_records(offset=2**63)`` (one past INT64 max) leaked a
  raw DuckDB ``Conversion Error`` (v0.5.1 dogfood Phase 3 UX #5). The
  fix bounds every ``offset`` parameter to the valid INT64 range so an
  out-of-range value is rejected as a typed validation error instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.server.server import create_server

# INT64 max is the DuckDB conversion boundary that #229 pinned; one past
# it is the smallest value that must be rejected.
_INT64_MAX = 2**63 - 1
_OVER_INT64_MAX = _INT64_MAX + 1


@contextmanager
def _live_server() -> Iterator[FastMCP]:
    """Boot a real ``FastMCP`` instance over a schema-less in-memory DB.

    Every case below is rejected by pydantic argument validation before
    the tool coroutine body runs (and therefore before any DB access),
    so the connection does not need the full schema bootstrap the
    smoke-test fixtures pay for.
    """
    conn = get_in_memory_connection()
    try:
        mcp = create_server(conn)
        yield mcp
    finally:
        conn.close()


def _expect_validation_error(mcp: FastMCP, tool: str, **kwargs: object) -> str:
    """Call ``tool`` and assert it was rejected at the argument-binding layer.

    Returns the stringified :class:`ToolError` so callers can pin the
    offending parameter name in the message when useful.
    """
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(mcp.call_tool(tool, dict(kwargs)))
    return str(excinfo.value)


@pytest.fixture(autouse=True)
def _quiet_boot_sweep_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Silence the expected boot-time orphan-sweep warning noise.

    ``create_server`` sweeps ``import_jobs`` for orphaned rows at boot;
    against the schema-less in-memory connection used here that always
    misses (no ``import_jobs`` table yet) and logs a WARNING. That is
    orthogonal to what this module tests, so caplog is raised to ERROR
    to keep pytest's captured-log output focused on real failures.
    """
    caplog.set_level(logging.ERROR)


def test_source_name_too_long_rejected() -> None:
    """#224: a 12,000-char ``source_name`` is rejected before it reaches DuckDB."""
    with _live_server() as mcp:
        message = _expect_validation_error(
            mcp,
            "query_records",
            record_type="HKQuantityTypeIdentifierHeartRate",
            source_name="A" * 12_000,
        )
        assert "source_name" in message
        # The server process must stay alive and answer a normal call
        # right after the adversarial one -- the whole point of #224 was
        # that the old behaviour hung the process instead of rejecting.
        result = asyncio.run(
            mcp.call_tool("query_records", {"record_type": "HKQuantityTypeIdentifierHeartRate"})
        )
        assert result


def test_record_type_too_long_rejected() -> None:
    """#224: the required ``record_type`` string is also length-capped."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "query_records", record_type="H" * 300)
        assert "record_type" in message


def test_offset_out_of_range_rejected() -> None:
    """#229: ``offset`` past INT64 max is a typed validation error, not a raw

    DuckDB ``Conversion Error``."""
    with _live_server() as mcp:
        message = _expect_validation_error(
            mcp,
            "query_records",
            record_type="HKQuantityTypeIdentifierHeartRate",
            offset=_OVER_INT64_MAX,
        )
        assert "offset" in message
        # DuckDB's raw error text must not leak through the validation
        # boundary -- that is exactly the UX regression #229 reported.
        assert "Conversion Error" not in message


def test_offset_negative_rejected() -> None:
    """#229: negative ``offset`` is rejected the same way as out-of-range."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "list_workouts", offset=-1)
        assert "offset" in message


def test_hash_too_long_rejected() -> None:
    """#224: hash-family identifiers are capped at 64 chars (sha256 hex length)."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "get_workout_details", workout_hash="a" * 1000)
        assert "workout_hash" in message


def test_ecg_hash_too_long_rejected() -> None:
    """#224: ``get_ecg_data``'s hash parameter is capped the same way."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "get_ecg_data", ecg_hash="b" * 1000)
        assert "ecg_hash" in message


def test_correlation_hash_too_long_rejected() -> None:
    """#224: ``get_correlation_details``'s hash parameter is capped."""
    with _live_server() as mcp:
        message = _expect_validation_error(
            mcp, "get_correlation_details", correlation_hash="c" * 1000
        )
        assert "correlation_hash" in message


def test_import_zip_id_too_long_rejected() -> None:
    """Retest for #224: ``import_zip.id`` keeps its pre-existing 64-char cap.

    The runtime hex/length check inside ``import_zip`` already covered
    this at the body level; this pins the *schema-level* pydantic
    ``max_length=64`` companion so an oversized ``id`` is rejected at
    the argument-binding layer before the tool body even runs.
    """
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "import_zip", id="a" * 100)
        assert "id" in message


def test_run_custom_query_oversized_query_rejected() -> None:
    """#224: ``run_custom_query`` caps the SQL text at 64 KiB.

    A legitimate multi-table analytical query is well under this
    ceiling; only a pathological payload (e.g. a huge IN-list) trips it.
    """
    with _live_server() as mcp:
        message = _expect_validation_error(
            mcp,
            "run_custom_query",
            query="SELECT 1 " + "-- padding\n" * 10_000,
        )
        assert "query" in message


def test_activity_type_too_long_rejected() -> None:
    """#224: ``list_workouts.activity_type`` is capped like the other health fields."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "list_workouts", activity_type="X" * 500)
        assert "activity_type" in message


def test_job_id_too_long_rejected() -> None:
    """#224: ``get_import_status.job_id`` is capped at 64 chars."""
    with _live_server() as mcp:
        message = _expect_validation_error(mcp, "get_import_status", job_id="z" * 1000)
        assert "job_id" in message
