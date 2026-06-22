"""Read-only SQL validation and bounded-row LIMIT enforcement.

``run_custom_query`` exposes the DuckDB engine to LLM-generated SQL. Even
opening the connection in read-only mode is not sufficient because DuckDB
ships built-in functions that can read host files (``read_text``,
``read_csv``, ...) or hit external networks (``read_json`` with a URL),
plus a ``FROM '<path>.csv'`` / ``FROM 'https://...'`` auto-detect shortcut
that reads files / URLs without naming any function. Those would leak data
through the query result even on a read-only connection. Mirrors the Rust
``server::mod`` defences (issues #1, #2) plus the quoted-path bypass
discovered during the Python port review.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from apple_health_mcp.exceptions import ValidationError

# Built-in DuckDB functions that can read host files or hit external networks.
# Even a read-only connection must reject these because the data leaves
# through the query result. Match the Rust reference list verbatim.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "read_text",
        "read_text_auto",
        "read_blob",
        "read_blob_auto",
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "glob",
    }
)

# Hard cap applied when the caller did not specify a LIMIT. Keeps unbounded
# SELECTs from blowing up the LLM context window.
MAX_CUSTOM_QUERY_ROWS = 1000


class QueryValidationError(ValidationError):
    """Raised when ``validate_query`` rejects a custom SQL statement.

    Inherits from the project's :class:`ValidationError` so callers catching
    the shared :class:`AppleHealthMCPError` base also catch SQL-validation
    failures.
    """


def validate_query(sql: str) -> exp.Query:
    """Reject anything that is not a single read-only SELECT / WITH query.

    The parser runs with the DuckDB dialect so functions, type names, and
    list literals match what ``run_custom_query`` will eventually execute.
    On rejection a :class:`QueryValidationError` is raised whose message is
    the human-readable string to send back to the MCP client. The parsed
    AST node is returned so :func:`enforce_limit` can apply the row cap
    without re-parsing the same string.
    """
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise QueryValidationError(f"SQL parse error: {exc}") from exc

    # ``sqlglot.parse`` returns ``[None]`` for an empty / comment-only input.
    cleaned = [s for s in statements if s is not None]
    if not cleaned:
        raise QueryValidationError("Query is empty")
    if len(cleaned) > 1:
        raise QueryValidationError(f"Only a single SQL statement is allowed (got {len(cleaned)})")

    stmt = cleaned[0]
    # ``exp.Query`` is the base class for SELECT / UNION / WITH-headed reads
    # in sqlglot. DDL / DML statements (Insert, Update, Delete, Create, Drop,
    # Alter, Attach, Copy, Pragma, ...) inherit from other branches of the
    # hierarchy and so fall through the isinstance check below.
    if not isinstance(stmt, exp.Query):
        raise QueryValidationError(
            "Only SELECT / WITH queries are allowed (DDL, DML, ATTACH, COPY, "
            "INSTALL, LOAD, PRAGMA, etc. are rejected)"
        )

    # Walk the AST looking for two failure modes:
    #
    # 1. Denylisted function calls (scalar ``f(...)``, table-valued
    #    ``FROM read_text(...)``, and the ``FROM LATERAL fn(...)`` form).
    # 2. ``FROM '<path>'`` / ``FROM 'https://...'`` — sqlglot parses these
    #    as a Table whose Identifier is *quoted*, and DuckDB auto-detects
    #    the literal as a file / URL to read. The function-name walker
    #    misses this because there is no Func / Anonymous node.
    for node in stmt.walk():
        if isinstance(node, exp.Table):
            ident = node.this
            if isinstance(ident, exp.Identifier) and ident.quoted:
                raise QueryValidationError(
                    "Quoted-path table references are not allowed "
                    "(DuckDB auto-detects them as file / URL reads)"
                )
        name = _node_function_name(node)
        if name is None:
            continue
        if name.lower() in DENIED_FUNCTIONS:
            raise QueryValidationError(
                f"Function '{name}' is not allowed (reads host files or external resources)"
            )

    return stmt


def _node_function_name(node: exp.Expr) -> str | None:
    """Extract the function-like name from an AST node, or ``None``.

    sqlglot routes function calls through two distinct node families:

    * Unknown / non-built-in functions land on :class:`exp.Anonymous`, whose
      ``this`` attribute carries the raw identifier text.
    * Known DuckDB built-ins (``read_csv``, ``read_parquet``, ...) get a
      dedicated :class:`exp.Func` subclass; the canonical SQL name comes
      from :meth:`exp.Func.sql_name`.

    Table-valued calls in the ``FROM`` clause (including the
    ``FROM LATERAL fn(...)`` form) also walk through the inner Func / Anonymous
    node, so we do not need a separate ``exp.Table`` branch.
    """
    if isinstance(node, exp.Anonymous):
        name = node.this
        return name if isinstance(name, str) else None
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def enforce_limit(stmt: exp.Query, max_rows: int = MAX_CUSTOM_QUERY_ROWS) -> str:
    """Render ``stmt`` back to SQL, applying ``LIMIT max_rows`` if it has none.

    Takes the parsed AST that :func:`validate_query` already produced (one
    parse per custom query instead of two) and uses sqlglot's serializer so
    the LIMIT is materialised inside the AST rather than appended as text.
    Appending text after a trailing ``-- ...`` line comment would otherwise
    push the LIMIT into the comment and bypass the row cap.
    """
    if stmt.args.get("limit") is not None:
        return stmt.sql(dialect="duckdb")
    return stmt.limit(max_rows).sql(dialect="duckdb")
