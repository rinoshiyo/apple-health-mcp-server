"""Translate DuckDB engine exceptions into typed error envelopes.

v0.6.1 (issue #273) completes the typed-envelope migration for the
``run_custom_query`` MCP tool. v0.6.0 shipped the import-path
translator (``importers.orchestrator._translate_conversion_error``);
the query-path was left returning ``f"Error: {exc}"`` and this module
closes that gap.

Each ``translate_*`` helper maps a specific DuckDB exception class to
the shared envelope shape defined in :func:`query.build_query_error_envelope`:

* ``duckdb.CatalogException`` → ``unknown_table`` / ``unknown_view``
  when the message names a Table / View, else ``execution_error``.
  ``available_tables`` (from ``information_schema.tables``) and
  ``did_you_mean`` (parsed out of DuckDB's own suggestion) are only
  attached to the hint when the classification lands on
  ``unknown_table`` / ``unknown_view`` — otherwise the caller cannot
  tell whether ``did_you_mean`` refers to a table, function, or
  sequence, and would retry against the wrong entity.
* ``duckdb.BinderException`` → ``missing_column`` when the message
  matches ``Referenced column X not found``; every other
  BinderException variant (ambiguous columns, ORDER-term-range errors,
  type mismatches) falls back to ``execution_error`` so an LLM
  branching on ``reason`` does not attempt a nonsensical column-fix
  retry. The hint carries ``referenced_column`` and a full per-table
  column list fetched from ``information_schema.columns`` — DuckDB's
  own ``Candidate bindings`` diagnostic truncates to ~5 entries which
  is not enough to recover a query against ``records`` (12 columns).
* ``duckdb.ParserException`` → ``syntax_error``, with ANSI colour
  escape codes stripped so the message renders cleanly in every
  MCP-client transport.

Every introspection query used to build a hint is wrapped so the
translator never turns an error path into a *second* error — if the
schema probe fails the hint is simply omitted.

The BinderException translator accepts the parsed
``sqlglot.exp.Query`` AST already produced by
``safety.validate_query`` rather than re-parsing the query text on
the error path. Threading the AST eliminates a redundant sqlglot
parse, removes the ``pragma: no cover`` guards for dialect drift
between two sqlglot invocations, and closes a subtle bug where the
LIMIT-injected wire SQL passed by ``run_custom_query`` could
round-trip differently than the original user query.
"""

from __future__ import annotations

import logging
import re
from threading import Lock
from typing import TYPE_CHECKING, Any

from sqlglot import exp as sql_exp

from apple_health_mcp.server.query import build_query_error_envelope, query_to_json

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


# ANSI colour escape sequences (``ESC[<params>m``) leak into DuckDB's
# ParserException / BinderException messages when the process runs
# under a TTY-detected environment. Strip them so the wire message is
# plain UTF-8 regardless of where the server was launched.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI colour escape sequences from ``text``."""
    return _ANSI_ESCAPE_RE.sub("", text)


# Match ``Did you mean "records"?`` / ``Did you mean 'records'?`` — the
# quoting style differs between DuckDB versions.
_DID_YOU_MEAN_RE = re.compile(r"""Did you mean ["']([^"']+)["']""")

# Match ``Table with name X does not exist`` / ``View with name X does
# not exist``. The name may be bare or quoted with ``"`` or ``'``.
_UNKNOWN_TABLE_RE = re.compile(
    r"Table with name (?:[\"'`])?([^\"'`\s!]+)(?:[\"'`])? does not exist"
)
_UNKNOWN_VIEW_RE = re.compile(r"View with name (?:[\"'`])?([^\"'`\s!]+)(?:[\"'`])? does not exist")

# Match ``Referenced column "hearth_rate" not found`` — DuckDB always
# double-quotes the identifier in this diagnostic.
_MISSING_COLUMN_RE = re.compile(r'Referenced column "([^"]+)" not found')


def _available_tables(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock | None,
) -> list[str] | None:
    """Return the list of public table names, or ``None`` on failure.

    Never re-raises: the caller is already on an error path and turning
    a hint-lookup failure into a second exception would only mask the
    original diagnostic.
    """
    try:
        rows = query_to_json(
            conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name",
            lock=lock,
        )
    except Exception as introspect_exc:  # pragma: no cover - defensive
        _logger.debug("available_tables introspection failed: %s", introspect_exc)
        return None
    return [row["table_name"] for row in rows]


def _referenced_tables_from_ast(stmt: sql_exp.Query) -> list[str]:
    """Extract distinct table identifiers referenced in the parsed ``stmt``.

    Walks the sqlglot AST that ``safety.validate_query`` already
    produced during the initial guard check. Threading the AST
    through the error path avoids a second sqlglot parse on every
    BinderException and closes the dialect-drift risk that came from
    re-parsing the LIMIT-injected wire SQL rather than the original
    query text.
    """
    tables: list[str] = []
    for node in stmt.find_all(sql_exp.Table):
        name = node.name
        if name and name not in tables:
            tables.append(name)
    return tables


def _columns_by_table(
    conn: duckdb.DuckDBPyConnection,
    tables: list[str],
    *,
    lock: Lock | None,
) -> dict[str, list[str]]:
    """Return ``{table_name: [column, ...]}`` for the given tables.

    Uses ``information_schema.columns`` which surfaces the FULL column
    list (unlike DuckDB's Candidate bindings diagnostic that caps at
    around 5 entries). Silently returns an empty dict on introspection
    failure so the caller can fall back to a hint-less envelope.
    """
    if not tables:
        return {}
    try:
        placeholders = ",".join(["?"] * len(tables))
        rows = query_to_json(
            conn,
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE table_schema = 'main' AND table_name IN ({placeholders}) "
            "ORDER BY table_name, ordinal_position",
            list(tables),
            lock=lock,
        )
    except Exception as introspect_exc:  # pragma: no cover - defensive
        _logger.debug("columns_by_table introspection failed: %s", introspect_exc)
        return {}
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["table_name"], []).append(row["column_name"])
    return grouped


def translate_catalog_exception(
    conn: duckdb.DuckDBPyConnection,
    exc: BaseException,
    *,
    lock: Lock | None = None,
) -> str:
    """Translate a ``duckdb.CatalogException`` into a typed envelope."""
    message = strip_ansi(str(exc))
    hint: dict[str, Any] = {}
    reason = "execution_error"
    if _UNKNOWN_TABLE_RE.search(message):
        reason = "unknown_table"
    elif _UNKNOWN_VIEW_RE.search(message):
        reason = "unknown_view"
    # ``did_you_mean`` and ``available_tables`` are only meaningful when
    # the classification lands on a table / view lookup. DuckDB emits
    # "Did you mean X?" for scalar-function CatalogExceptions too (e.g.
    # ``SELECT foo(1)`` → "Scalar Function with name foo does not
    # exist! Did you mean 'floor'?") — attaching those suggestions to
    # an ``execution_error`` envelope would let an LLM retry with a
    # function name where it expected a table name.
    if reason in ("unknown_table", "unknown_view"):
        tables = _available_tables(conn, lock=lock)
        if tables is not None:
            hint["available_tables"] = tables
        did_you_mean_match = _DID_YOU_MEAN_RE.search(message)
        if did_you_mean_match:
            hint["did_you_mean"] = did_you_mean_match.group(1)
    return build_query_error_envelope(
        reason=reason,
        message=message,
        hint=hint or None,
    )


def translate_binder_exception(
    conn: duckdb.DuckDBPyConnection,
    stmt: sql_exp.Query,
    exc: BaseException,
    *,
    lock: Lock | None = None,
) -> str:
    """Translate a ``duckdb.BinderException`` into a typed envelope.

    ``duckdb.BinderException`` fires for several distinct planning-time
    failures — missing columns are only one of them. Ambiguous column
    references, ORDER-term-range errors, and type mismatches all share
    the same exception class, so classifying every BinderException as
    ``missing_column`` would let an LLM enter a nonsensical column-fix
    retry loop for what is really e.g. an ambiguous reference. Only
    messages that match ``Referenced column "X" not found`` map to
    ``missing_column``; every other variant falls back to
    ``execution_error`` with the raw diagnostic preserved in the
    ``message`` field.

    The ``stmt`` argument is the parsed AST that
    ``safety.validate_query`` already produced during the pre-execute
    guard check — threaded through so the translator can enumerate
    referenced tables without a second sqlglot parse of the wire SQL.
    """
    message = strip_ansi(str(exc))
    m = _MISSING_COLUMN_RE.search(message)
    if not m:
        # Non-``missing_column`` BinderException (ambiguous column, ORDER
        # term out of range, type mismatch, ...). No structured hint is
        # available — surface as generic execution_error so the caller
        # does not misinterpret the diagnostic.
        return build_query_error_envelope(
            reason="execution_error",
            message=message,
        )
    hint: dict[str, Any] = {"referenced_column": m.group(1)}
    tables = _referenced_tables_from_ast(stmt)
    grouped = _columns_by_table(conn, tables, lock=lock)
    if grouped:
        hint["available_columns"] = grouped
    return build_query_error_envelope(
        reason="missing_column",
        message=message,
        hint=hint,
    )


def translate_parser_exception(exc: BaseException) -> str:
    """Translate a ``duckdb.ParserException`` into a typed envelope.

    No connection is needed — the failure happened before DuckDB
    reached any catalog / binder work. ANSI escape codes are stripped
    so the message stays readable across every MCP-client transport.
    """
    return build_query_error_envelope(
        reason="syntax_error",
        message=strip_ansi(str(exc)),
    )
