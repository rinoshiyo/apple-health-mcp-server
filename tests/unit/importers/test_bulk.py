"""Tests for ``importers._bulk.bulk_load_via_csv`` (issue #41)."""

from __future__ import annotations

import math
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers import _bulk
from apple_health_mcp.importers._bulk import bulk_load_via_csv


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with a synthetic 4-column table named ``t``.

    The helper's table allowlist is widened for the test's lifetime so the
    test fixture can use a short table name; production callers always pass
    one of the real importer tables.
    """
    monkeypatch.setattr(_bulk, "_ALLOWED_TABLES", _bulk._ALLOWED_TABLES | {"t"})
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE t (
            a VARCHAR,
            b VARCHAR,
            c DOUBLE,
            d TIMESTAMPTZ
        )
        """
    )
    return conn


def test_bulk_load_basic_row(conn: duckdb.DuckDBPyConnection) -> None:
    bulk_load_via_csv(
        conn,
        "t",
        [("hello", "world", 1.5, "2024-01-01 10:00:00+0900")],
    )
    rows = conn.execute("SELECT a, b, c FROM t").fetchall()
    assert rows == [("hello", "world", 1.5)]


def test_bulk_load_empty_batch_is_noop(conn: duckdb.DuckDBPyConnection) -> None:
    """Empty batch must short-circuit so callers can stay symmetric."""
    bulk_load_via_csv(conn, "t", [])
    rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    assert rows is not None
    assert rows[0] == 0


def test_bulk_load_none_becomes_null_distinct_from_empty_string(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``None`` writes NULL via the ``\\N`` sentinel; ``""`` stays empty string.

    Without the distinction, columns whose legitimate value is an empty
    VARCHAR (e.g. ``text_value`` on a Category record with no label) would
    silently collapse to NULL.
    """
    bulk_load_via_csv(
        conn,
        "t",
        [
            ("empty-not-null", "", 0.0, "2024-01-01 10:00:00+0900"),
            ("real-null", None, None, None),
        ],
    )
    rows = conn.execute("SELECT a, b, c, d FROM t ORDER BY a").fetchall()
    # Row order ASC by `a`: "empty-not-null" < "real-null".
    assert rows[0][0] == "empty-not-null"
    assert rows[0][1] == ""  # empty string, NOT NULL
    assert rows[0][2] == 0.0
    assert rows[1][0] == "real-null"
    assert rows[1][1] is None
    assert rows[1][2] is None
    assert rows[1][3] is None


def test_bulk_load_quotes_commas_newlines_in_strings(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """CSV quoting must round-trip values containing special characters."""
    tricky = 'has "quotes", commas\nand newlines'
    bulk_load_via_csv(
        conn,
        "t",
        [("k", tricky, 0.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT b FROM t").fetchone()
    assert row is not None
    assert row[0] == tricky


def test_bulk_load_unicode_round_trips(conn: duckdb.DuckDBPyConnection) -> None:
    """UTF-8 strings survive the CSV → DuckDB round trip."""
    bulk_load_via_csv(
        conn,
        "t",
        [("k", "ヘルスケア / 心拍数 — ❤", 0.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT b FROM t").fetchone()
    assert row is not None
    assert row[0] == "ヘルスケア / 心拍数 — ❤"


def test_bulk_load_tempfile_cleaned_up_after_success(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `.csv` files survive in the configured temp dir after a clean call."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    bulk_load_via_csv(
        conn,
        "t",
        [("k", "v", 1.0, "2024-01-01 10:00:00+0900")],
    )
    leftover = list(tmp_path.glob("*.csv"))
    assert leftover == []


def test_bulk_load_tempfile_cleaned_up_on_copy_failure_pre_copy(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A COPY-prepare failure (column-count mismatch) still removes the tempfile.

    This exercises the early-validation path: DuckDB rejects the CSV before
    streaming any rows because the column count does not match the table.
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    with pytest.raises(duckdb.Error):
        bulk_load_via_csv(
            conn,
            "t",
            # 5 columns supplied against a 4-column table; COPY raises.
            [("k", "v", 1.0, "2024-01-01 10:00:00+0900", "extra")],
        )
    leftover = list(tmp_path.glob("*.csv"))
    assert leftover == []


def test_bulk_load_tempfile_cleaned_up_on_copy_runtime_failure(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A COPY-runtime failure (bad TIMESTAMPTZ value) still removes the tempfile.

    The CSV is structurally valid — same column count, no bare quote
    characters — but a value in the TIMESTAMPTZ column cannot be parsed.
    DuckDB raises after streaming has started, which is the failure path
    the helper's ``try/finally`` was designed for. Without this test the
    cleanup contract could regress unnoticed because the column-count
    sibling test fires its error before the COPY actually opens the file.
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    # ``"not-a-number"`` into the DOUBLE column triggers a CSV-stream
    # ConversionException AFTER DuckDB opens the file.
    with pytest.raises(duckdb.Error):
        bulk_load_via_csv(
            conn,
            "t",
            [("k", "v", "not-a-number", "2024-01-01 10:00:00+0900")],
        )
    leftover = list(tmp_path.glob("*.csv"))
    assert leftover == []


def test_bulk_load_tempfile_cleaned_up_on_writer_failure(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer-raised exception mid-batch still removes the tempfile.

    Pre-fix the ``try/finally`` was OUTSIDE the ``with NamedTemporaryFile``
    block, so a ``csv.writer.writerow`` exception orphaned the on-disk
    file. The new shape moves the ``finally`` to cover everything after
    ``csv_path`` is assigned, so this path stays clean.
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    # An object whose `__str__` raises will explode inside csv.writer.
    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        bulk_load_via_csv(conn, "t", [(Boom(), "v", 1.0, "2024-01-01 10:00:00+0900")])
    leftover = list(tmp_path.glob("*.csv"))
    assert leftover == []


def test_bulk_load_large_batch_round_trips_row_count(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Sanity check that the COPY path handles batches the size the importer flushes at."""
    n = 10_000
    rows = [(f"k{i}", f"v{i}", float(i), "2024-01-01 10:00:00+0900") for i in range(n)]
    bulk_load_via_csv(conn, "t", rows)
    count_row = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    assert count_row is not None
    assert count_row[0] == n


def test_bulk_load_nan_float_round_trips_as_nan(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``float('nan')`` survives the CSV path as NaN (not NULL).

    DuckDB's CSV reader recognises 'nan' / 'NaN' as the IEEE NaN, so the
    distinction matters for any downstream aggregate the user runs against
    a column whose source values legitimately include NaN.
    """
    bulk_load_via_csv(
        conn,
        "t",
        [("k", "v", float("nan"), "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT c FROM t").fetchone()
    assert row is not None
    assert row[0] is not None
    assert math.isnan(row[0])


def test_bulk_load_rejects_unknown_table(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The allowlist guards against an unintended ``table`` argument.

    Production callers pass literal table names that match the schema,
    but the helper's API does not enforce that statically — defense in
    depth catches a future caller mid-refactor.
    """
    with pytest.raises(HealthImportError, match="not on the importer allowlist"):
        bulk_load_via_csv(conn, "not_an_apple_health_table", [("k", "v", 0.0, None)])


def test_bulk_load_rejects_sentinel_collision(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A string value equal to the NULL sentinel raises rather than silently NULLing.

    Apple Health metadata is free-form user/third-party text; a value of
    the literal two characters ``\\N`` would otherwise be misread as SQL
    NULL by the DuckDB CSV reader.
    """
    with pytest.raises(HealthImportError, match="NULL sentinel"):
        bulk_load_via_csv(
            conn,
            "t",
            [("k", "\\N", 0.0, "2024-01-01 10:00:00+0900")],
        )
