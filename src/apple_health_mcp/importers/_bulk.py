"""Bulk-load helper for the importer flush path.

The Rust reference uses DuckDB's columnar Appender API; the Rust schema is
even commented to mark that constraint
("Create tables without PRIMARY KEY constraints so Appender can bulk-load",
``rust/src/db.rs:19``). The DuckDB Python binding does NOT expose the
columnar Appender — only ``DuckDBPyConnection.append(table, df)`` which
requires a pandas DataFrame.

Routing every flush through ``executemany("INSERT INTO ... VALUES (?, ...)")``
(the original implementation) dispatches per row through the SQL planner;
measured throughput on a real 1.2 GB ``export.xml`` was ~300 rows/s, so the
import never finished within 20 minutes. ``COPY FROM CSV`` is the fastest
bulk-load path the Python binding supports without adding pandas / pyarrow
as runtime dependencies — measured at ~100 000 rows/s in the same harness,
a ~325x speedup. See issue #41.

Each flush writes its batch to a per-invocation tempfile and runs DuckDB's
native ``COPY ... FROM '...' (FORMAT CSV, ...)``; the tempfile is removed
in a ``finally`` that wraps the entire body so a writer-raised exception
mid-batch (e.g. a UnicodeEncodeError on a corrupted metadata blob) still
unlinks the partial file instead of orphaning it.

The ``\\N`` NULL sentinel (PostgreSQL convention) is used rather than the
empty string so columns whose legitimate value is an empty VARCHAR (e.g.
``text_value`` on a Category record without a label) are not collapsed to
NULL. A pre-flight scan asserts that no input value equals the sentinel
literal — Apple Health's ``MetadataEntry.value`` is free-form text from
third-party apps, so the assertion guards against silent data corruption
in the rare case a real value happens to be ``"\\N"`` (we'd rather fail
loudly with a clear message than write a wrong row).
"""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apple_health_mcp.exceptions import HealthImportError

if TYPE_CHECKING:
    import duckdb


# PostgreSQL convention. Apple Health first-party data does not contain
# this byte sequence, but ``MetadataEntry.value`` written by third-party
# apps is free-form — the pre-flight scan in :func:`bulk_load_via_csv`
# rejects rows that would collide so we never silently NULL real data.
_NULL_SENTINEL = "\\N"

# Allowlist of tables the importer is permitted to bulk-load into. The
# helper interpolates ``table`` into the ``COPY`` SQL via f-string (DuckDB
# rejects ``COPY ? FROM ?`` — only the file path can be parameterised),
# so guarding the input is the only defense against a future caller
# accidentally passing user-tainted input. Every entry matches a table
# created by :mod:`apple_health_mcp.db.schema`.
_ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "records",
        "record_metadata",
        "workouts",
        "workout_events",
        "workout_statistics",
        "workout_metadata",
        "workout_routes",
        "activity_summaries",
        "heart_rate_samples",
        "correlations",
        "correlation_members",
        "state_of_mind",
        "route_points",
        "ecg_samples",
    }
)


def bulk_load_via_csv(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    """Bulk-load ``rows`` into ``table`` via DuckDB ``COPY FROM`` a tempfile.

    The caller owns the batch buffer and clears it after the call returns;
    on an empty batch this is a no-op so the caller can stay symmetric with
    the original ``executemany`` shape.

    ``rows`` is a sequence of column-aligned tuples in declaration order —
    the helper does NOT name columns, so any reordering must happen at the
    call site. ``None`` values become the NULL sentinel; everything else is
    handed to ``csv.writer`` which quotes embedded commas / newlines /
    quote characters per RFC 4180 (DuckDB's CSV reader handles the same
    dialect).

    Raises:
        HealthImportError: if ``table`` is not in :data:`_ALLOWED_TABLES`
            (defense-in-depth against future callers passing user-tainted
            input), or if any string value in ``rows`` equals
            :data:`_NULL_SENTINEL` (would silently corrupt to SQL NULL).
    """
    if not rows:
        return
    if table not in _ALLOWED_TABLES:
        raise HealthImportError(
            f"bulk_load_via_csv: table {table!r} is not on the importer "
            f"allowlist (allowed: {sorted(_ALLOWED_TABLES)})"
        )
    _assert_no_sentinel_collision(table, rows)

    csv_path: str | None = None
    try:
        # ``NamedTemporaryFile(delete=False)`` so the file survives the
        # ``with`` close, and our ``finally`` (rather than the context
        # manager) owns cleanup. This shape guarantees the unlink runs
        # even when ``writer.writerow`` raises mid-batch.
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            newline="",
            encoding="utf-8",
        ) as f:
            csv_path = f.name
            # ``QUOTE_ALL`` so every field is wrapped in ``"..."`` regardless
            # of content. ``QUOTE_MINIMAL`` only quotes fields with embedded
            # separators, which means the first 20k rows of a real Apple
            # Health export (largely ASCII numerics with no quoting needed)
            # leave DuckDB's auto-sniffer convinced the file has no quote
            # character, and a later row containing a metadata blob with
            # embedded commas is then misparsed (issue #41 follow-up: a 5h+
            # hang on real ``export.xml`` was traced to this dialect
            # mismatch). ``quotechar='"'`` is the csv.excel default and
            # therefore omitted; ``lineterminator='\n'`` is load-bearing
            # (default is ``'\r\n'``) so DuckDB and Python agree on the
            # newline shape across platforms.
            writer = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n")
            for row in rows:
                writer.writerow([_NULL_SENTINEL if v is None else v for v in row])
        # Pin the dialect explicitly rather than relying on DuckDB's
        # auto-sniffer (same reason as the QUOTE_ALL choice above).
        # ``DELIMITER ','`` is pinned even though comma is DuckDB's
        # default — making the contract total against a future default
        # change. ``ESCAPE '"'`` matches Python ``csv``'s default
        # doubled-quote escaping.
        conn.execute(
            f"COPY {table} FROM ? (FORMAT CSV, HEADER FALSE, "
            f"DELIMITER ',', QUOTE '\"', ESCAPE '\"', "
            f"NULLSTR '\\N', AUTO_DETECT FALSE)",
            [csv_path],
        )
    finally:
        # ``csv_path is None`` only when ``NamedTemporaryFile()`` itself
        # raised before ``f.name`` was bound — pragma keeps the defensive
        # guard out of the branch coverage report.
        if csv_path is not None:  # pragma: no branch
            # ``missing_ok=True`` is defensive — the file was created above
            # and only this process owns it, but cleanup must not raise.
            Path(csv_path).unlink(missing_ok=True)


def _assert_no_sentinel_collision(table: str, rows: Sequence[Sequence[Any]]) -> None:
    """Raise if any string value equals :data:`_NULL_SENTINEL`.

    Apple Health's ``MetadataEntry.value`` is free-form text written by
    third-party apps (a Workout note, a regex pattern stored as metadata,
    a developer debug tag). A value that happens to equal the literal
    two-character string ``\\N`` would be serialised verbatim by
    ``csv.writer`` and then read back by DuckDB's ``NULLSTR`` matcher as
    SQL NULL, silently corrupting the row. Failing loudly here surfaces
    the corruption-causing value to the operator (with table + value
    snippet) instead of producing a quietly-wrong DB.

    The scan is per-cell over already-allocated strings; on a 2.6M-row
    import it costs well under a second.
    """
    for row in rows:
        for value in row:
            if isinstance(value, str) and value == _NULL_SENTINEL:
                raise HealthImportError(
                    f"bulk_load_via_csv: cannot import row into {table!r} — "
                    f"value equals the NULL sentinel {_NULL_SENTINEL!r} "
                    "which would silently corrupt to SQL NULL. Source row "
                    "needs sanitising upstream."
                )
