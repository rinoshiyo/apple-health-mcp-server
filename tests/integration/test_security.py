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

import duckdb
import pytest

from apple_health_mcp.db import get_in_memory_connection


@pytest.mark.parametrize(
    "function_call",
    [
        # The aliases the v0.5.0 adversarial review surfaced. Each one
        # would have surfaced as a usable fs-read or URL-fetch path
        # before the v0.5.1 lockdown.
        "parquet_scan('/etc/passwd')",
        "parquet_metadata('/etc/passwd')",
        "parquet_schema('/etc/passwd')",
        "sniff_csv('/etc/passwd')",
        # The Rust-reference denylist set, re-pinned here so a future
        # accidental removal from ``DENIED_FUNCTIONS`` is caught by
        # the engine-level safety net.
        "read_csv('/etc/passwd')",
        "read_csv_auto('/etc/passwd')",
        "read_parquet('/etc/passwd')",
        "read_text('/etc/passwd')",
        "read_blob('/etc/passwd')",
        "read_json('/etc/passwd')",
        "read_ndjson('/etc/passwd')",
        "glob('/etc/*')",
    ],
)
def test_engine_rejects_filesystem_table_functions(function_call: str) -> None:
    """``SET enable_external_access = false`` blocks every fs table function.

    Bypasses the validator and goes straight to ``conn.execute`` so the
    test pins the ENGINE-level guarantee. A regression in
    ``_set_engine_safety_pragmas`` (e.g. the setting silently failing
    to apply on a future DuckDB release) would surface here even if
    the validator's denylist still listed all the right names.
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error) as exc_info:
            conn.execute(f"SELECT * FROM {function_call}").fetchall()
        # Engine surfaces these as "Permission" / "IO" / "Catalog"
        # errors depending on the function family. The contract is
        # "engine refuses", not the exact wording — pin breadth rather
        # than a brittle string match.
        msg = str(exc_info.value).lower()
        assert any(
            keyword in msg
            for keyword in (
                "enable_external_access",
                "permission",
                "io error",
                "invalid input",
                "extension",
                "not enabled",
                "disabled",
            )
        ), f"engine error was {exc_info.value!r}; expected an external-access-related message"
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
    """
    conn = get_in_memory_connection()
    try:
        with pytest.raises(duckdb.Error):
            conn.execute(f"SELECT * FROM parquet_scan('{url}')").fetchall()
    finally:
        conn.close()


def test_engine_rejects_file_attach_install_load() -> None:
    """File-backed ATTACH, INSTALL, and LOAD all go through the same lockdown.

    DuckDB's ``enable_external_access`` covers extension loading
    (INSTALL / LOAD) and file-backed cross-DB attachment in addition
    to file reading; this test pins the symmetry so a future
    contributor cannot accidentally widen the safety contract to
    "only file reads". ``ATTACH ':memory:'`` is deliberately NOT
    pinned -- it does not touch the filesystem and DuckDB
    intentionally allows it under the locked-down setting.
    """
    conn = get_in_memory_connection()
    try:
        for stmt in (
            "ATTACH '/tmp/attacker.db' AS f",
            "INSTALL httpfs",
            "LOAD httpfs",
        ):
            with pytest.raises(duckdb.Error):
                conn.execute(stmt)
    finally:
        conn.close()


def test_enable_external_access_is_off_after_open() -> None:
    """Invariant probe: the setting flipped to false and stays there.

    A regression test for the ``_set_engine_safety_pragmas`` call site
    -- if a future PR accidentally drops the SET statement (e.g. by
    refactoring the connection-open helper into a path that forgets
    the safety pragmas), the value reverts to true and every other
    safety test in this file would still pass on the validator path
    while leaving the engine wide open. This probe pins the actual
    DuckDB session setting.
    """
    conn = get_in_memory_connection()
    try:
        row = conn.execute(
            "SELECT current_setting('enable_external_access')"
        ).fetchone()
        assert row is not None
        # DuckDB returns the setting value as text; "false" both for
        # SET enable_external_access = false and for the PRAGMA spelling.
        assert str(row[0]).lower() == "false"
    finally:
        conn.close()
