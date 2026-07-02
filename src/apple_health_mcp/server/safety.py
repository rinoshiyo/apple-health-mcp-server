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
        # v0.6 #216: in-DB introspection functions. These are NOT
        # covered by ``enable_external_access = false`` (they read
        # session state, not host files/network), so this denylist is
        # the ONLY guard closing this leak. ``duckdb_settings`` /
        # ``duckdb_databases`` expose internal paths (e.g.
        # ``temp_directory``); ``duckdb_extensions`` happens to also
        # touch the extension directory and is separately blocked by
        # the engine, but it is listed here too for parse-time UX
        # consistency with its introspection siblings.
        "duckdb_settings",
        "duckdb_extensions",
        "duckdb_databases",
        # v0.6 #225: fs-read families missed by the v0.5.1 #190 sweep.
        # Defense-in-depth on top of ``enable_external_access = false``.
        "read_duckdb",
        "read_ndjson_objects",
        "read_json_objects",
        "read_json_objects_auto",
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

    ``reason`` (v0.6.1 / issue #273) is a stable enum that lets
    ``run_custom_query`` build a typed error envelope without
    pattern-matching on the free-form message. Known values:
    ``empty_query``, ``not_select_or_with``, ``multi_statement``,
    ``disallowed_function``, ``syntax_error``. Callers that do not care
    about the enum can catch the base class exactly as before.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# v0.6.1 (issue #273): frozen list of reason strings ``QueryValidationError``
# may carry, exposed for tests that need to iterate over the enum without
# duplicating the literals.
QUERY_VALIDATION_REASONS: frozenset[str] = frozenset(
    {
        "empty_query",
        "not_select_or_with",
        "multi_statement",
        "disallowed_function",
        "syntax_error",
    }
)


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
        raise QueryValidationError(f"SQL parse error: {exc}", reason="syntax_error") from exc

    # ``sqlglot.parse`` returns ``[None]`` for an empty / comment-only input.
    cleaned = [s for s in statements if s is not None]
    if not cleaned:
        raise QueryValidationError("Query is empty", reason="empty_query")
    if len(cleaned) > 1:
        raise QueryValidationError(
            f"Only a single SQL statement is allowed (got {len(cleaned)})",
            reason="multi_statement",
        )

    stmt = cleaned[0]
    # ``exp.Query`` is the base class for SELECT / UNION / WITH-headed reads
    # in sqlglot. DDL / DML statements (Insert, Update, Delete, Create, Drop,
    # Alter, Attach, Copy, Pragma, ...) inherit from other branches of the
    # hierarchy and so fall through the isinstance check below.
    if not isinstance(stmt, exp.Query):
        raise QueryValidationError(
            "Only SELECT / WITH queries are allowed (DDL, DML, ATTACH, COPY, "
            "INSTALL, LOAD, PRAGMA, etc. are rejected)",
            reason="not_select_or_with",
        )

    # Walk the AST looking for two failure modes:
    #
    # 1. Denylisted function calls (scalar ``f(...)``, table-valued
    #    ``FROM read_text(...)``, and the ``FROM LATERAL fn(...)`` form).
    # 2. ``FROM '<path>'`` / ``FROM 'https://...'`` — sqlglot parses these
    #    as a Table whose Identifier is *quoted*, and DuckDB auto-detects
    #    the literal as a file / URL to read. The function-name walker
    #    misses this because there is no Func / Anonymous node.
    # Both failure modes surface as ``disallowed_function`` on the wire —
    # quoted-path references are a DuckDB-side alias for the same
    # fs / URL-read surface the function denylist blocks, so an LLM
    # branching on ``reason`` sees the two as one recoverable category.
    for node in stmt.walk():
        if isinstance(node, exp.Table):
            ident = node.this
            if isinstance(ident, exp.Identifier) and ident.quoted:
                raise QueryValidationError(
                    "Quoted-path table references are not allowed "
                    "(DuckDB auto-detects them as file / URL reads)",
                    reason="disallowed_function",
                )
        name = _node_function_name(node)
        if name is None:
            continue
        if name.lower() in DENIED_FUNCTIONS:
            raise QueryValidationError(
                f"Function '{name}' is not allowed (reads host files or external resources)",
                reason="disallowed_function",
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
