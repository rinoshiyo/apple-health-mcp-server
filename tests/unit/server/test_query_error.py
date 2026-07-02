"""Tests for ``server.query_error``.

Exercises the translator functions in isolation from ``run_custom_query``
so the ``information_schema`` introspection paths (available tables,
column lists, did-you-mean parsing, ANSI stripping) are pinned directly
rather than only indirectly through the tool-level integration tests.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from apple_health_mcp.server import query_error as query_error_module
from apple_health_mcp.server.query_error import (
    strip_ansi,
    translate_binder_exception,
    translate_catalog_exception,
    translate_parser_exception,
)


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
        "SELECT hearth_rate FROM records LIMIT 1",
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


def test_translate_binder_exception_message_without_column_name(
    seeded_conn: duckdb.DuckDBPyConnection,
) -> None:
    """A BinderException whose message does not match the "Referenced
    column" pattern still resolves to ``missing_column`` with no
    ``referenced_column`` hint key."""
    exc = duckdb.BinderException("Binder Error: some other binder failure")
    out = translate_binder_exception(seeded_conn, "SELECT 1", exc, lock=None)
    payload = json.loads(out)
    assert payload["reason"] == "missing_column"
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
        "SELECT a.hearth_rate FROM records a JOIN records b ON a.record_hash = b.record_hash",
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
        "SELECT hearth_rate FROM records LIMIT 1",
        exc,
        lock=None,
    )
    payload = json.loads(out)
    assert payload["reason"] == "missing_column"
    assert payload["hint"] == {"referenced_column": "hearth_rate"}
