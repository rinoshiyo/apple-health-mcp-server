"""Tests for ``server.safety``."""

from __future__ import annotations

import pytest

from apple_health_mcp.server.safety import (
    DENIED_FUNCTIONS,
    MAX_CUSTOM_QUERY_ROWS,
    QueryValidationError,
    enforce_limit,
    validate_query,
)


def test_validate_query_accepts_plain_select() -> None:
    validate_query("SELECT 1")


def test_validate_query_accepts_with_cte() -> None:
    validate_query("WITH t AS (SELECT 1 AS x) SELECT * FROM t")


def test_validate_query_accepts_union() -> None:
    validate_query("SELECT 1 UNION SELECT 2")


def test_validate_query_rejects_empty_input() -> None:
    with pytest.raises(QueryValidationError, match="empty"):
        validate_query("   ")


def test_validate_query_rejects_comment_only_input() -> None:
    with pytest.raises(QueryValidationError, match="empty"):
        validate_query("-- only a comment")


def test_validate_query_rejects_multiple_statements() -> None:
    with pytest.raises(QueryValidationError, match="single SQL statement"):
        validate_query("SELECT 1; SELECT 2")


def test_validate_query_rejects_insert() -> None:
    with pytest.raises(QueryValidationError, match="SELECT / WITH"):
        validate_query("INSERT INTO records VALUES (1)")


def test_validate_query_rejects_update() -> None:
    with pytest.raises(QueryValidationError, match="SELECT / WITH"):
        validate_query("UPDATE records SET value = 0")


def test_validate_query_rejects_attach() -> None:
    with pytest.raises(QueryValidationError, match="SELECT / WITH"):
        validate_query("ATTACH '/tmp/x.duckdb'")


def test_validate_query_rejects_parse_error() -> None:
    with pytest.raises(QueryValidationError, match="parse error"):
        validate_query("SELECT FROM WHERE !!!")


@pytest.mark.parametrize("fn", sorted(DENIED_FUNCTIONS))
def test_validate_query_rejects_denied_scalar_call(fn: str) -> None:
    with pytest.raises(QueryValidationError, match=fn):
        validate_query(f"SELECT {fn}('/etc/passwd')")


def test_validate_query_rejects_denied_table_function() -> None:
    with pytest.raises(QueryValidationError, match="read_csv"):
        validate_query("SELECT * FROM read_csv('/etc/passwd')")


def test_validate_query_rejects_denied_lateral_function() -> None:
    with pytest.raises(QueryValidationError, match="read_text"):
        validate_query("SELECT * FROM records, LATERAL read_text('/etc/passwd')")


def test_validate_query_rejects_case_insensitively() -> None:
    with pytest.raises(QueryValidationError, match="READ_TEXT"):
        validate_query("SELECT READ_TEXT('/etc/passwd')")


def test_validate_query_accepts_known_safe_functions() -> None:
    # Walks the Func branch (sql_name path) without raising -- COUNT and AVG
    # are built-in Func subclasses in sqlglot and must continue the loop.
    validate_query("SELECT COUNT(*) AS c, AVG(value) AS a FROM records")


@pytest.mark.parametrize(
    "path",
    [
        "foo.csv",
        "/etc/passwd.csv",
        "https://attacker.example/x.parquet",
        "s3://bucket/x.json",
    ],
)
def test_validate_query_rejects_quoted_path_table_references(path: str) -> None:
    """Regression: FROM '<path>' lets DuckDB auto-detect the literal as a
    file / URL to read, bypassing the function-name denylist entirely."""
    with pytest.raises(QueryValidationError, match="Quoted-path"):
        validate_query(f"SELECT * FROM '{path}'")


def test_validate_query_returns_parsed_query() -> None:
    """The parsed AST is returned so ``enforce_limit`` can render from it."""
    import sqlglot
    from sqlglot import exp

    stmt = validate_query("SELECT 1")
    assert isinstance(stmt, exp.Query)
    # Re-rendering should round-trip.
    rendered = stmt.sql(dialect="duckdb")
    sqlglot.parse_one(rendered, dialect="duckdb")


def _limit_for(sql: str, max_rows: int = MAX_CUSTOM_QUERY_ROWS) -> str:
    """Helper: validate then enforce_limit so tests assert end-to-end."""
    return enforce_limit(validate_query(sql), max_rows)


def test_enforce_limit_appends_when_missing() -> None:
    assert f"LIMIT {MAX_CUSTOM_QUERY_ROWS}" in _limit_for("SELECT * FROM records")


def test_enforce_limit_passes_through_existing_limit() -> None:
    out = _limit_for("SELECT * FROM records LIMIT 10")
    assert "LIMIT 10" in out
    # The hard cap must NOT be re-applied when the caller already set one.
    assert f"LIMIT {MAX_CUSTOM_QUERY_ROWS}" not in out


def test_enforce_limit_ignores_subquery_limit() -> None:
    # Inner LIMIT in a derived table should not bypass the outer cap.
    out = _limit_for("SELECT * FROM (SELECT * FROM records LIMIT 10) t")
    assert f"LIMIT {MAX_CUSTOM_QUERY_ROWS}" in out


def test_enforce_limit_survives_trailing_line_comment() -> None:
    # Regression: appending text after a `-- ...` line comment would swallow
    # the LIMIT. The fix renders the AST instead, so the LIMIT is materialised
    # in the SQL regardless of trailing comments.
    out = _limit_for("SELECT * FROM records -- trailing comment")
    assert f"LIMIT {MAX_CUSTOM_QUERY_ROWS}" in out
    # Re-parsing the rendered SQL must surface the LIMIT in the AST.
    import sqlglot

    reparsed = sqlglot.parse_one(out, dialect="duckdb")
    assert reparsed.args.get("limit") is not None
