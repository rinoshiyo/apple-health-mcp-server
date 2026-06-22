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


def test_enforce_limit_appends_when_missing() -> None:
    assert enforce_limit("SELECT * FROM records") == (
        f"SELECT * FROM records LIMIT {MAX_CUSTOM_QUERY_ROWS}"
    )


def test_enforce_limit_strips_trailing_semicolon_before_appending() -> None:
    assert enforce_limit("SELECT * FROM records;   ") == (
        f"SELECT * FROM records LIMIT {MAX_CUSTOM_QUERY_ROWS}"
    )


def test_enforce_limit_passes_through_existing_limit() -> None:
    sql = "SELECT * FROM records LIMIT 10"
    assert enforce_limit(sql) == sql


def test_enforce_limit_ignores_subquery_limit() -> None:
    # Inner LIMIT in a derived table should not bypass the outer cap.
    sql = "SELECT * FROM (SELECT * FROM records LIMIT 10) t"
    out = enforce_limit(sql)
    assert out.endswith(f"LIMIT {MAX_CUSTOM_QUERY_ROWS}")


def test_enforce_limit_falls_through_on_parse_failure() -> None:
    # Malformed SQL should still get a LIMIT appended; the trailing append
    # is a belt-and-suspenders fallback.
    out = enforce_limit("SELECT FROM WHERE")
    assert out.endswith(f"LIMIT {MAX_CUSTOM_QUERY_ROWS}")
