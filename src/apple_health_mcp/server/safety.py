"""Read-only SQL validation and bounded-row LIMIT enforcement.

``run_custom_query`` exposes the DuckDB engine to LLM-generated SQL. Even
opening the connection in read-only mode is not sufficient because DuckDB
ships built-in functions that can read host files (``read_text``,
``read_csv``, ...) or hit external networks (``read_json`` with a URL).
Those would leak data through the query result even on a read-only
connection. Mirrors the Rust ``server::mod`` defences (issues #1, #2).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

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


class QueryValidationError(ValueError):
    """Raised when ``validate_query`` rejects a custom SQL statement."""


def validate_query(sql: str) -> None:
    """Reject anything that is not a single read-only SELECT / WITH query.

    The parser is run with the DuckDB dialect so functions, type names, and
    list literals match what ``run_custom_query`` will eventually execute.
    On rejection a :class:`QueryValidationError` is raised whose message is
    the human-readable string to send back to the MCP client.
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

    # Walk the AST looking for denylisted function calls. Covers scalar
    # ``f(...)`` calls, table-valued ``FROM read_text(...)`` references, and
    # the ``FROM LATERAL fn(...)`` form, all of which surface as nodes with
    # an identifier ``name`` attribute.
    for node in stmt.walk():
        name = _node_function_name(node)
        if name is None:
            continue
        if name.lower() in DENIED_FUNCTIONS:
            raise QueryValidationError(
                f"Function '{name}' is not allowed (reads host files or external resources)"
            )


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


def enforce_limit(sql: str, max_rows: int = MAX_CUSTOM_QUERY_ROWS) -> str:
    """Append ``LIMIT max_rows`` unless ``sql`` already has its own LIMIT.

    ``validate_query`` must have already confirmed ``sql`` is a single
    SELECT / WITH so the append is unambiguous. The check parses the AST
    again instead of regex-matching ``LIMIT`` so embedded subqueries with
    their own LIMIT do not fool us into thinking the outer query is bounded.
    """
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.ParseError:
        # ``validate_query`` is the gatekeeper; if it accepted ``sql`` this
        # path should be unreachable. Fall through to the trailing append.
        statements = []
    cleaned = [s for s in statements if s is not None]
    if cleaned:
        stmt = cleaned[0]
        if isinstance(stmt, exp.Query) and stmt.args.get("limit") is not None:
            return sql
    trimmed = sql.rstrip().rstrip(";").rstrip()
    return f"{trimmed} LIMIT {max_rows}"
