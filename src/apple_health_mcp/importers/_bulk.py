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
native ``COPY ... FROM '...' (FORMAT CSV, HEADER FALSE, NULLSTR '\\N')``;
the tempfile is removed in a ``finally`` so a partial flush leaves no
on-disk residue. The ``\\N`` NULL sentinel (PostgreSQL convention) is used
rather than the empty string so columns whose legitimate value is an
empty VARCHAR (e.g. ``text_value`` on a Category record without a label)
are not collapsed to NULL.
"""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb


# PostgreSQL convention. Apple Health export data never contains this
# byte sequence, so collisions are not a concern.
_NULL_SENTINEL = "\\N"


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
    """
    if not rows:
        return
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".csv",
        delete=False,
        newline="",
        encoding="utf-8",
    ) as f:
        # ``QUOTE_ALL`` so every field is wrapped in ``"..."`` regardless of
        # content. ``QUOTE_MINIMAL`` only quotes fields with embedded
        # separators / quotes / newlines — which means the first 20k rows of
        # a real Apple Health export (largely ASCII numerics with no quoting
        # needed) leave DuckDB's auto-sniffer convinced the file has no
        # quote character, and a later row containing e.g. a metadata blob
        # with embedded commas is then misparsed (issue #41 follow-up: a
        # 5h+ hang on real export.xml was traced to this dialect mismatch).
        writer = csv.writer(
            f, quoting=csv.QUOTE_ALL, quotechar='"', lineterminator="\n"
        )
        for row in rows:
            writer.writerow([_NULL_SENTINEL if v is None else v for v in row])
        csv_path = f.name
    try:
        # Pin the dialect explicitly rather than relying on DuckDB's
        # auto-sniffer (same reason as the QUOTE_ALL choice above). ``ESCAPE
        # '"'`` matches Python ``csv``'s default doubled-quote escaping.
        conn.execute(
            f"COPY {table} FROM ? (FORMAT CSV, HEADER FALSE, NULLSTR '\\N', "
            f"QUOTE '\"', ESCAPE '\"', AUTO_DETECT FALSE)",
            [csv_path],
        )
    finally:
        # The tempfile was just created above; the only way it could be
        # missing here is a separate process racing to remove it, which the
        # importer does not do. ``missing_ok=True`` keeps cleanup defensive
        # without obscuring the contract.
        Path(csv_path).unlink(missing_ok=True)
