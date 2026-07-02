"""Tests for ``importers._bulk_arrow.bulk_load_via_arrow`` (issue #50)."""

from __future__ import annotations

import math

import duckdb
import pyarrow as pa
import pytest

from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers import _bulk_arrow
from apple_health_mcp.importers._bulk_arrow import SCHEMAS, bulk_load_via_arrow
from tests._helpers import open_test_memory_connection


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with a synthetic 4-column table named ``t``.

    The helper looks up the schema by table name, so a dedicated test
    schema is registered for the test's lifetime. Production callers
    always pass one of the real importer tables (which live in
    :data:`SCHEMAS`).
    """
    schema = pa.schema(
        [
            ("a", pa.string()),
            ("b", pa.string()),
            ("c", pa.float64()),
            ("d", pa.string()),  # TIMESTAMPTZ -- DuckDB casts on INSERT
        ]
    )
    monkeypatch.setitem(SCHEMAS, "t", schema)
    monkeypatch.setattr(_bulk_arrow, "_ALLOWED_TABLES", frozenset(SCHEMAS.keys()) | {"t"})
    conn = open_test_memory_connection()
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
    bulk_load_via_arrow(
        conn,
        "t",
        [("hello", "world", 1.5, "2024-01-01 10:00:00+0900")],
    )
    rows = conn.execute("SELECT a, b, c FROM t").fetchall()
    assert rows == [("hello", "world", 1.5)]


def test_bulk_load_empty_batch_is_noop(conn: duckdb.DuckDBPyConnection) -> None:
    """Empty batch must short-circuit so callers can stay symmetric."""
    bulk_load_via_arrow(conn, "t", [])
    row = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    assert row is not None
    assert row[0] == 0


def test_bulk_load_none_becomes_null_distinct_from_empty_string(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``None`` writes Arrow ``null`` (SQL NULL); ``""`` stays empty string.

    Without the distinction, columns whose legitimate value is an empty
    VARCHAR (e.g. ``text_value`` on a Category record with no label) would
    silently collapse to NULL. Arrow's native null representation makes
    the historical CSV-era ``\\N`` sentinel collision check obsolete.
    """
    bulk_load_via_arrow(
        conn,
        "t",
        [
            ("empty-not-null", "", 0.0, "2024-01-01 10:00:00+0900"),
            ("real-null", None, None, None),
        ],
    )
    rows = conn.execute("SELECT a, b, c, d FROM t ORDER BY a").fetchall()
    assert rows[0][0] == "empty-not-null"
    assert rows[0][1] == ""  # empty string, NOT NULL
    assert rows[0][2] == 0.0
    assert rows[1][0] == "real-null"
    assert rows[1][1] is None
    assert rows[1][2] is None
    assert rows[1][3] is None


def test_bulk_load_null_sentinel_literal_passes_through(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The literal ``\\N`` no longer triggers a sentinel-collision error.

    The CSV path needed a pre-flight scan that rejected rows whose
    string value equalled the NULL sentinel. The Arrow path expresses
    null natively, so the literal two-character string can ride
    through without being misread.
    """
    bulk_load_via_arrow(
        conn,
        "t",
        [("k", "\\N", 0.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT b FROM t").fetchone()
    assert row is not None
    assert row[0] == "\\N"


def test_bulk_load_unicode_round_trips(conn: duckdb.DuckDBPyConnection) -> None:
    """UTF-8 strings survive the Arrow → DuckDB round trip."""
    bulk_load_via_arrow(
        conn,
        "t",
        [("k", "ヘルスケア / 心拍数 — ❤", 0.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT b FROM t").fetchone()
    assert row is not None
    assert row[0] == "ヘルスケア / 心拍数 — ❤"


def test_bulk_load_strings_with_quotes_and_newlines_round_trip(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Embedded special characters survive the Arrow path verbatim.

    CSV needed RFC 4180 quoting to ferry these safely; Arrow carries
    raw bytes and DuckDB stores them unchanged.
    """
    tricky = 'has "quotes", commas\nand newlines'
    bulk_load_via_arrow(
        conn,
        "t",
        [("k", tricky, 0.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT b FROM t").fetchone()
    assert row is not None
    assert row[0] == tricky


def test_bulk_load_nan_float_round_trips_as_nan(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``float('nan')`` survives the Arrow path as NaN (not NULL).

    Arrow stores IEEE NaN as a real value (distinct from null), and
    DuckDB preserves the distinction on INSERT.
    """
    bulk_load_via_arrow(
        conn,
        "t",
        [("k", "v", float("nan"), "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT c FROM t").fetchone()
    assert row is not None
    assert row[0] is not None
    assert math.isnan(row[0])


def test_bulk_load_large_batch_round_trips_row_count(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Sanity check the Arrow path handles batches at the importer's flush size."""
    n = 10_000
    rows = [(f"k{i}", f"v{i}", float(i), "2024-01-01 10:00:00+0900") for i in range(n)]
    bulk_load_via_arrow(conn, "t", rows)
    count_row = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    assert count_row is not None
    assert count_row[0] == n


def test_bulk_load_rejects_unknown_table(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The allowlist guards against an unintended ``table`` argument.

    The helper interpolates ``table`` into the ``INSERT`` SQL via
    f-string; defense in depth catches a future caller mid-refactor.
    """
    with pytest.raises(HealthImportError, match="not on the importer allowlist"):
        bulk_load_via_arrow(conn, "not_an_apple_health_table", [("k", "v", 0.0, None)])


def test_bulk_load_rejects_arity_mismatch(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A wrong-shape row buffer surfaces with the offending arity."""
    # 5 columns supplied against a 4-column schema.
    with pytest.raises(HealthImportError, match="row arity 5 does not match"):
        bulk_load_via_arrow(
            conn,
            "t",
            [("k", "v", 1.0, "2024-01-01 10:00:00+0900", "extra")],
        )


def test_bulk_load_rejects_ragged_rows(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Mid-batch arity mismatch surfaces with the table name, not IndexError.

    The zip(*rows, strict=True) transpose validates every row's arity
    in the same pass; a ragged buffer whose first row matches the schema
    but a later row is short would otherwise fall through into an opaque
    iterator-length error mid-transpose.
    """
    # Row 0 matches the 4-column schema; row 1 is short by one column.
    rows: list[tuple[object, ...]] = [
        ("k1", "v1", 1.0, "2024-01-01 10:00:00+0900"),
        ("k2", "v2", 2.0),
    ]
    with pytest.raises(HealthImportError, match="row arity"):
        bulk_load_via_arrow(conn, "t", rows)


def test_bulk_load_unregisters_after_failure(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``__bulk_arrow`` view does not survive an INSERT failure.

    The helper's ``try/finally`` unregisters even when DuckDB raises
    during the INSERT, so a subsequent call against the same connection
    sees a clean namespace.
    """
    # Force the INSERT to fail by feeding a value the TIMESTAMPTZ parser
    # cannot read; DuckDB raises after registering the view.
    with pytest.raises(duckdb.Error):
        bulk_load_via_arrow(
            conn,
            "t",
            [("k", "v", 1.0, "not-a-timestamp")],
        )
    # The cleanup pass must have run; a second call against the same
    # connection succeeds.
    bulk_load_via_arrow(
        conn,
        "t",
        [("k2", "v2", 2.0, "2024-01-01 10:00:00+0900")],
    )
    row = conn.execute("SELECT a FROM t").fetchone()
    assert row is not None
    assert row[0] == "k2"


def test_real_schema_records_round_trip() -> None:
    """A real-schema ``records`` row round-trips with NULL preserved.

    Guards the per-table Arrow schemas wired up for the production
    flush helpers: the column order must match the table definition in
    ``db.schema`` (a silent mismatch would write into the wrong column).
    """
    from apple_health_mcp.db import ensure_schema, get_in_memory_connection

    # v0.6 (issues #222/#223): pin the session TZ via the ``tz`` kwarg --
    # a post-hoc ``SET TimeZone`` is now rejected once
    # ``lock_configuration = true`` fires inside
    # ``get_in_memory_connection``.
    conn = get_in_memory_connection(tz="UTC")
    ensure_schema(conn)
    rows = [
        (
            "rh1",
            "HKQuantityTypeIdentifierHeartRate",
            72.0,
            None,
            "count/min",
            "Apple Watch",
            "10.0",
            None,
            None,
            "2024-01-01 08:00:00+00:00",
            "2024-01-01 08:01:00+00:00",
            "imp1",
        )
    ]
    bulk_load_via_arrow(conn, "records", rows)
    out = conn.execute("SELECT record_hash, value, source_name FROM records").fetchone()
    assert out == ("rh1", 72.0, "Apple Watch")
    conn.close()
