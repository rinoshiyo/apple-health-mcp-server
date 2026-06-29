"""SQL validation and bounded-row LIMIT enforcement.

**Threat model (v0.4, issue #148).** v0.4 opened the server's DuckDB
handle ``read_only=False`` so the new ``import_zip`` MCP tool can drive
the importer inline on the live connection. That dropped the
OS-file-lock-level read-only barrier the v0.3.x server relied on as a
second line of defence; this validator is now the SOLE wire-side
guard between an LLM-issued ``run_custom_query`` statement and the
DuckDB engine. Concretely, ``validate_query`` must reject every
non-SELECT/WITH construct (DDL: CREATE / ALTER / DROP; DML: INSERT /
UPDATE / DELETE / MERGE; admin / I/O: ATTACH / COPY / INSTALL / LOAD /
PRAGMA; the quoted-path ``FROM '<path>'`` and ``FROM '<url>'`` shortcuts
DuckDB auto-detects as file / URL reads) and every built-in function
that exfiltrates host content via the query result (``read_text`` /
``read_csv`` / ``read_parquet`` / ``read_json`` / ``glob`` and their
``_auto`` variants).

Any future code path that hands LLM-controlled SQL straight to
``conn.execute`` without going through ``validate_query`` first loses
the only guard. New tools that accept SQL from the agent MUST funnel
through ``run_custom_query`` (or call ``validate_query`` directly) so
the guard remains the single chokepoint.

The validator mirrors the Rust ``server::mod`` defences (issues #1,
#2) plus the quoted-path bypass discovered during the Python port
review.

``run_custom_query`` exposes the DuckDB engine to LLM-generated SQL.
``MAX_CUSTOM_QUERY_ROWS`` caps result-set size so an unbounded SELECT
cannot blow up the LLM context window.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from apple_health_mcp.exceptions import ValidationError

# Built-in DuckDB functions that can read host files or hit external
# networks. Even a read-only connection must reject these because the
# data leaves through the query result.
#
# v0.5.1 #190 defense-in-depth: the engine-level lockdown in
# ``db.connection._set_engine_safety_pragmas`` (= SET
# enable_external_access = false) is the root-cause fix that closes
# every fs / network surface DuckDB exposes — including aliases that
# this denylist could never enumerate exhaustively. This list remains
# as a secondary guard:
#
# * It produces a friendlier ``Function 'X' is not allowed`` error
#   than DuckDB's downstream ``IO Error`` / ``Permission Error`` on
#   the same call, which matters for the LLM-facing UX of
#   ``run_custom_query``.
# * It catches the call at parse time inside ``validate_query``
#   instead of at execute time, so a CTE or subquery hiding the
#   denylisted function is rejected before any work runs.
# * If ``enable_external_access`` is ever re-enabled (a deliberate
#   future opt-in or an accidental rollback), this list still blocks
#   the most dangerous functions.
#
# The v0.5.0 adversarial test (tmp/v0-5-0-adversarial-results_1.md
# §2-2) flagged the missing parquet_scan / parquet_metadata /
# parquet_schema / sniff_csv aliases — added below alongside the
# pre-existing Rust-reference set.
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
        # v0.5.1 #190: aliases / near-relatives of the Rust-reference
        # set that bypassed the v0.5.0 denylist on adversarial probes.
        "parquet_scan",
        "parquet_metadata",
        "parquet_schema",
        "sniff_csv",
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

    NOTE (v0.4.1 / issue #159): no production caller invokes this
    helper any more -- ``run_custom_query`` builds its SQL inline via
    ``stmt.limit(MAX + 1).sql(...)`` so it can probe for overflow
    truncation. The helper remains as a test-only API to keep the
    historic ``test_enforce_limit_survives_trailing_line_comment``
    pin alive; the equivalent invariant on the live ``run_custom_query``
    path is covered by
    ``test_run_custom_query_caps_unbounded_select_despite_trailing_comment``
    in ``tests/unit/server/test_tools.py``.
    """
    if stmt.args.get("limit") is not None:
        return stmt.sql(dialect="duckdb")
    return stmt.limit(max_rows).sql(dialect="duckdb")
