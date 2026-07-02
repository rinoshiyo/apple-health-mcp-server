"""Tests for ``server.query_error``.

Exercises the translator functions in isolation from ``run_custom_query``
so the ``information_schema`` introspection paths (available tables,
column lists, did-you-mean parsing, ANSI stripping) are pinned directly
rather than only indirectly through the tool-level integration tests.
"""

from __future__ import annotations

import json
from typing import cast

import duckdb
import pytest
import sqlglot
from sqlglot import exp as sql_exp

from apple_health_mcp.server import query_error as query_error_module
from apple_health_mcp.server.query_error import (
    strip_ansi,
    translate_binder_exception,
    translate_catalog_exception,
    translate_parser_exception,
)


def _parse(sql: str) -> sql_exp.Query:
    """Parse ``sql`` and return the sqlglot AST node ``run_custom_query``
    would hand to ``translate_binder_exception`` (mirrors the AST
    ``safety.validate_query`` returns during the pre-execute guard)."""
    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return cast(sql_exp.Query, parsed)


def test_strip_ansi_removes_escape_sequences() -> None:
    coloured = "\x1b[4mParser Error\x1b[0m: syntax error"
    assert strip_ansi(coloured) == "Parser Error: syntax error"


def test_strip_ansi_passes_plain_text_through() -> None:
    plain = "Binder Error: Referenced column not found"
    assert strip_ansi(plain) == plain


def test_translate_catalog_exception_unknown_table(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    exc = duckdb.CatalogException(
        'Catalog Error: Table with name record does not exist!\nDid you mean "records"?'
    )
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["state"] == "error"
    assert payload["reason"] == "unknown_table"
    assert payload["hint"]["did_you_mean"] == "records"
    assert "records" in payload["hint"]["available_tables"]


def test_translate_catalog_exception_unknown_view(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    exc = duckdb.CatalogException(
        'Catalog Error: View with name nonexistent_view does not exist!\nDid you mean "v"?'
    )
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "unknown_view"
    assert payload["hint"]["did_you_mean"] == "v"


def test_translate_binder_exception_missing_column(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    exc = duckdb.BinderException(
        'Binder Error: Referenced column "hearth_rate" not found in FROM clause!'
    )
    out = translate_binder_exception(
        seeded_conn,
        _parse("SELECT hearth_rate FROM records LIMIT 1"),
        exc,
        lock=None,
    )
    payload = json.loads(out)
    assert payload["state"] == "error"
    assert payload["reason"] == "missing_column"
    assert payload["hint"]["referenced_column"] == "hearth_rate"
    # The full 12-column ``records`` schema must be present, proving the
    # information_schema fallback fills in what DuckDB's own "Candidate
    # bindings" diagnostic truncates to ~5 entries.
    columns = payload["hint"]["available_columns"]["records"]
    assert len(columns) == 12
    assert "record_hash" in columns
    assert "unit" in columns


def test_translate_parser_exception_strips_ansi() -> None:
    exc = duckdb.ParserException("\x1b[4mParser Error\x1b[0m: syntax error near 'FRM'")
    out = translate_parser_exception(exc)
    payload = json.loads(out)
    assert payload["state"] == "error"
    assert payload["reason"] == "syntax_error"
    assert "\x1b" not in payload["message"]
    assert "Parser Error" in payload["message"]


def test_translate_catalog_exception_survives_introspection_failure(
    seeded_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hint-lookup failure must not turn the error path into a second
    exception -- the envelope still returns cleanly, just hint-less."""

    def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("introspection boom")

    monkeypatch.setattr(query_error_module, "query_to_json", _boom)
    exc = duckdb.CatalogException(
        'Catalog Error: Table with name record does not exist!\nDid you mean "records"?'
    )
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "unknown_table"
    # did_you_mean is still parsed from the message text (no DB access
    # required); available_tables is omitted because the introspection
    # query failed.
    assert payload["hint"] == {"did_you_mean": "records"}


def test_translate_catalog_exception_unrelated_message(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A CatalogException that is neither an unknown-table nor an
    unknown-view message falls through to ``execution_error`` with no
    hint (neither branch of the classification matches, and there is no
    "Did you mean" suggestion to parse)."""
    exc = duckdb.CatalogException("Catalog Error: Sequence with name x does not exist!")
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "execution_error"
    assert "hint" not in payload


def test_translate_catalog_exception_did_you_mean_not_attached_outside_table_view(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """v0.6.1 (issue #273 code-review A1): DuckDB emits "Did you mean X?"
    for scalar-function CatalogExceptions too (e.g. ``SELECT foo(1)`` →
    ``Scalar Function with name foo does not exist! Did you mean
    'floor'?``). The suggestion must NOT be attached to the envelope
    when the reason is ``execution_error`` — otherwise an LLM sees a
    generic engine failure with a table-shaped ``did_you_mean`` hint
    and retries against the wrong entity (``FROM floor``)."""
    exc = duckdb.CatalogException(
        'Catalog Error: Scalar Function with name foo does not exist!\nDid you mean "floor"?'
    )
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "execution_error"
    assert "hint" not in payload


def test_translate_catalog_exception_unknown_table_without_suggestion(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """Older DuckDB releases omit the "Did you mean" line when the
    engine has no close-enough match. The envelope must still classify
    as ``unknown_table`` and populate ``available_tables`` without
    adding a ``did_you_mean`` key."""
    exc = duckdb.CatalogException(
        "Catalog Error: Table with name totally_fabricated_name does not exist!"
    )
    out = translate_catalog_exception(seeded_conn, exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "unknown_table"
    assert "did_you_mean" not in payload["hint"]
    assert "records" in payload["hint"]["available_tables"]


def test_translate_binder_exception_missing_column_no_referenced_tables(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A ``SELECT hearth_rate`` with no FROM clause parses cleanly but
    references zero tables — the ``available_columns`` hint must be
    omitted rather than emitting an empty ``information_schema`` probe
    against an empty ``IN (...)`` clause."""
    exc = duckdb.BinderException(
        'Binder Error: Referenced column "hearth_rate" not found in FROM clause!'
    )
    out = translate_binder_exception(
        seeded_conn,
        _parse("SELECT hearth_rate"),
        exc,
        lock=None,
    )
    payload = json.loads(out)
    assert payload["reason"] == "missing_column"
    assert payload["hint"] == {"referenced_column": "hearth_rate"}


def test_translate_binder_exception_non_missing_column_falls_back(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """v0.6.1 (issue #273 code-review B1): a BinderException whose
    message does not match ``Referenced column X not found`` (e.g.
    ambiguous column, ``ORDER term out of range``, type mismatch) must
    NOT be hard-labeled ``missing_column`` — otherwise an LLM branching
    on the reason enum enters a nonsensical column-fix retry loop for
    what is really a different binder failure."""
    exc = duckdb.BinderException("Binder Error: some other binder failure")
    out = translate_binder_exception(seeded_conn, _parse("SELECT 1"), exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "execution_error"
    assert "hint" not in payload


def test_translate_binder_exception_deduplicates_repeated_table_reference(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A self-join references ``records`` twice; the table list -- and
    therefore the ``available_columns`` hint -- must not duplicate it."""
    exc = duckdb.BinderException(
        'Binder Error: Referenced column "hearth_rate" not found in FROM clause!'
    )
    out = translate_binder_exception(
        seeded_conn,
        _parse(
            "SELECT a.hearth_rate FROM records a JOIN records b ON a.record_hash = b.record_hash"
        ),
        exc,
        lock=None,
    )
    payload = json.loads(out)
    assert list(payload["hint"]["available_columns"].keys()) == ["records"]


def test_translate_binder_exception_survives_introspection_failure(
    seeded_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("introspection boom")

    monkeypatch.setattr(query_error_module, "query_to_json", _boom)
    exc = duckdb.BinderException(
        'Binder Error: Referenced column "hearth_rate" not found in FROM clause!'
    )
    out = translate_binder_exception(
        seeded_conn,
        _parse("SELECT hearth_rate FROM records LIMIT 1"),
        exc,
        lock=None,
    )
    payload = json.loads(out)
    assert payload["reason"] == "missing_column"
    assert payload["hint"] == {"referenced_column": "hearth_rate"}
