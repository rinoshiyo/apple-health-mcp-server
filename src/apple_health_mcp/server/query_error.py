"""Translate DuckDB engine exceptions into typed error envelopes.

v0.6.1 (issue #273) completes the typed-envelope migration for the
``run_custom_query`` MCP tool. v0.6.0 shipped the import-path
translator (``importers.orchestrator._translate_conversion_error``);
the query-path was left returning ``f"Error: {exc}"`` and this module
closes that gap.

Each ``translate_*`` helper maps a specific DuckDB exception class to
the shared envelope shape defined in :func:`query.build_query_error_envelope`:

* ``duckdb.CatalogException`` → ``unknown_table`` / ``unknown_view``,
  with ``available_tables`` (fetched from ``information_schema.tables``)
  and ``did_you_mean`` (parsed out of DuckDB's own suggestion) filled
  in when the classification succeeds.
* ``duckdb.BinderException`` → ``missing_column``, with a
  ``referenced_column`` field and a full per-table column list fetched
  from ``information_schema.columns`` — DuckDB's own ``Candidate
  bindings`` list truncates to ~5 entries which is not enough to
  recover a query against ``records`` (12 columns).
* ``duckdb.ParserException`` → ``syntax_error``, with ANSI colour
  escape codes stripped so the message renders cleanly in every
  MCP-client transport.

Every introspection query used to build a hint is wrapped so the
translator never turns an error path into a *second* error — if the
schema probe fails the hint is simply omitted.
"""

from __future__ import annotations

import logging
import re
from threading import Lock
from typing import TYPE_CHECKING, Any

import sqlglot
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
_UNKNOWN_VIEW_RE = re.compile(
    r"View with name (?:[\"'`])?([^\"'`\s!]+)(?:[\"'`])? does not exist"
)

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


def _referenced_tables_from_sql(sql: str) -> list[str]:
    """Extract distinct table identifiers referenced in ``sql`` via sqlglot.

    Returns an empty list if parsing fails — a BinderException already
    means DuckDB accepted the parse, but if sqlglot's dialect diverges
    we prefer no hint over an exception.
    """
    tables: list[str] = []
    try:
        parsed = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as parse_exc:  # pragma: no cover - defensive
        _logger.debug("sqlglot parse failed while building hint: %s", parse_exc)
        return tables
    if parsed is None:
        return tables
    for node in parsed.find_all(sql_exp.Table):
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
    sql: str,
    exc: BaseException,
    *,
    lock: Lock | None = None,
) -> str:
    """Translate a ``duckdb.BinderException`` into a typed envelope.

    All BinderException variants surface on the wire as
    ``missing_column`` since the class fires for missing / ambiguous
    columns during logical planning. The hint carries the referenced
    column (when parseable) and the full column list of every table
    referenced in ``sql``.
    """
    message = strip_ansi(str(exc))
    hint: dict[str, Any] = {}
    m = _MISSING_COLUMN_RE.search(message)
    if m:
        hint["referenced_column"] = m.group(1)
    tables = _referenced_tables_from_sql(sql)
    grouped = _columns_by_table(conn, tables, lock=lock)
    if grouped:
        hint["available_columns"] = grouped
    return build_query_error_envelope(
        reason="missing_column",
        message=message,
        hint=hint or None,
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
