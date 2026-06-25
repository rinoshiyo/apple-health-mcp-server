"""PyArrow-backed bulk-load helper for the importer flush path (issue #50).

The DuckDB Python binding does not expose the columnar Appender API
that the Rust reference uses, but it does expose first-class Arrow
integration. Building a ``pyarrow.Table`` and routing it through
``conn.register(...)`` + ``INSERT INTO ... SELECT * FROM __bulk_arrow``
hands DuckDB a single contiguous columnar buffer per batch, skipping
the per-row CSV serialise+tempfile+COPY round-trip the previous
``_bulk.py`` helper paid on every flush.

Measured wall-clock on the maintainer's real 1.2 GB ``export.xml``
(2.6 M records / 350 workouts / 325 k GPX route points / 7 ECGs /
1.5 M metadata): v0.1.4's CSV path took 240 s end-to-end (Phase 1 =
176 s). The Arrow path drops that into the ≤ 130 s target the issue
sets.

The 14 Arrow schemas defined below pin the column layout for every
importer-writable table. All TIMESTAMPTZ columns are declared as
``pa.string()`` -- DuckDB applies the same ISO 8601 → TIMESTAMPTZ
parser the CSV path relied on during the implicit cast that fires on
``INSERT INTO <table> SELECT * FROM __bulk_arrow``. Keeping the wire
format as a string avoids building 10+ million ``datetime`` objects
in Python per import (which would re-introduce the per-row Python-CPU
cost Arrow is supposed to escape).

The historic ``_NULL_SENTINEL`` collision check is gone -- Arrow
distinguishes ``null`` from the literal two-character string ``"\\N"``
natively, so no preflight scan is needed. The allowlist stays as a
defense-in-depth guard against a future caller passing an unintended
table name (the helper interpolates ``table`` into the ``INSERT`` SQL
via f-string).

Importing this module is what pulls ``pyarrow`` (≈ 30 MB wheel) into
the runtime, so it MUST stay out of the ``serve`` import graph. A unit
test under ``tests/unit/server/`` asserts that importing
``apple_health_mcp.server.server`` does not transitively load
``pyarrow``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from apple_health_mcp.exceptions import HealthImportError

if TYPE_CHECKING:
    import duckdb


# Type shorthands so the per-table schemas stay readable.
_str = pa.string()
_f64 = pa.float64()
_i32 = pa.int32()
# TIMESTAMPTZ columns ride the wire as ISO 8601 strings; DuckDB casts
# them to TIMESTAMPTZ on INSERT using the same parser the CSV path used.
_ts = pa.string()


SCHEMAS: dict[str, pa.Schema] = {
    "records": pa.schema(
        [
            ("record_hash", _str),
            ("record_type", _str),
            ("value", _f64),
            ("text_value", _str),
            ("unit", _str),
            ("source_name", _str),
            ("source_version", _str),
            ("device", _str),
            ("creation_date", _ts),
            ("start_date", _ts),
            ("end_date", _ts),
            ("import_id", _str),
        ]
    ),
    "record_metadata": pa.schema(
        [
            ("record_hash", _str),
            ("key", _str),
            ("value", _str),
        ]
    ),
    "workouts": pa.schema(
        [
            ("workout_hash", _str),
            ("activity_type", _str),
            ("duration", _f64),
            ("duration_unit", _str),
            ("total_distance", _f64),
            ("total_distance_unit", _str),
            ("total_energy_burned", _f64),
            ("total_energy_unit", _str),
            ("source_name", _str),
            ("source_version", _str),
            ("device", _str),
            ("creation_date", _ts),
            ("start_date", _ts),
            ("end_date", _ts),
            ("import_id", _str),
        ]
    ),
    "workout_events": pa.schema(
        [
            ("workout_hash", _str),
            ("event_type", _str),
            ("date", _ts),
            ("duration", _f64),
            ("duration_unit", _str),
        ]
    ),
    "workout_statistics": pa.schema(
        [
            ("workout_hash", _str),
            ("stat_type", _str),
            ("start_date", _ts),
            ("end_date", _ts),
            ("average", _f64),
            ("minimum", _f64),
            ("maximum", _f64),
            ("sum", _f64),
            ("unit", _str),
        ]
    ),
    "workout_metadata": pa.schema(
        [
            ("workout_hash", _str),
            ("key", _str),
            ("value", _str),
            ("import_id", _str),
        ]
    ),
    "workout_routes": pa.schema(
        [
            ("workout_hash", _str),
            ("file_path", _str),
            ("source_name", _str),
            ("source_version", _str),
            ("device", _str),
            ("creation_date", _ts),
            ("start_date", _ts),
            ("end_date", _ts),
            ("import_id", _str),
        ]
    ),
    "activity_summaries": pa.schema(
        [
            ("date_components", _str),
            ("active_energy_burned", _f64),
            ("active_energy_burned_goal", _f64),
            ("active_energy_burned_unit", _str),
            ("apple_move_time", _f64),
            ("apple_move_time_goal", _f64),
            ("apple_exercise_time", _f64),
            ("apple_exercise_time_goal", _f64),
            ("apple_stand_hours", _f64),
            ("apple_stand_hours_goal", _f64),
            ("import_id", _str),
        ]
    ),
    "heart_rate_samples": pa.schema(
        [
            ("parent_record_hash", _str),
            ("sample_idx", _i32),
            ("bpm", _f64),
            # Issue #109 (PR-F): seconds-of-day since 00:00 local,
            # normalised at import time from Apple's raw ``HH:MM:SS.SSS``.
            ("sample_time", _f64),
            ("import_id", _str),
        ]
    ),
    "correlations": pa.schema(
        [
            ("correlation_hash", _str),
            ("correlation_type", _str),
            ("source_name", _str),
            ("source_version", _str),
            ("device", _str),
            ("creation_date", _ts),
            ("start_date", _ts),
            ("end_date", _ts),
            ("import_id", _str),
        ]
    ),
    "correlation_members": pa.schema(
        [
            ("correlation_hash", _str),
            ("record_hash", _str),
            ("import_id", _str),
        ]
    ),
    "state_of_mind": pa.schema(
        [
            ("record_hash", _str),
            ("valence", _f64),
            ("kind", _str),
            ("labels", _str),
            ("associations", _str),
            ("import_id", _str),
        ]
    ),
    "route_points": pa.schema(
        [
            ("point_hash", _str),
            ("workout_hash", _str),
            ("latitude", _f64),
            ("longitude", _f64),
            ("elevation", _f64),
            ("timestamp", _ts),
            ("speed", _f64),
            ("course", _f64),
            ("h_accuracy", _f64),
            ("v_accuracy", _f64),
            ("import_id", _str),
        ]
    ),
    "ecg_samples": pa.schema(
        [
            ("ecg_hash", _str),
            ("sample_idx", _i32),
            ("voltage_uv", _f64),
        ]
    ),
}


_ALLOWED_TABLES: frozenset[str] = frozenset(SCHEMAS.keys())


def bulk_load_via_arrow(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    """Bulk-load ``rows`` into ``table`` via a registered ``pyarrow.Table``.

    The caller owns the batch buffer and clears it after the call returns;
    on an empty batch this is a no-op so the caller can stay symmetric
    with the historical ``executemany`` / CSV shape.

    ``rows`` is a sequence of column-aligned tuples in the declaration
    order pinned by :data:`SCHEMAS`. ``None`` values become Arrow
    ``null`` (and therefore SQL ``NULL`` on read).

    Raises:
        HealthImportError: if ``table`` is not in :data:`_ALLOWED_TABLES`
            (defense-in-depth against a future caller passing an
            unintended table name -- the helper interpolates ``table``
            into the ``INSERT`` SQL via f-string).
    """
    if not rows:
        return
    schema = SCHEMAS.get(table)
    if schema is None:
        raise HealthImportError(
            f"bulk_load_via_arrow: table {table!r} is not on the importer "
            f"allowlist (allowed: {sorted(_ALLOWED_TABLES)})"
        )

    n_cols = len(schema)
    # ``zip(strict=True)`` does the column-major transpose in a single
    # C-level loop AND validates every row's arity matches the schema in
    # the same pass. The nested-comprehension transpose did N_columns
    # full sweeps over ``rows`` and only spot-checked ``rows[0]``; the
    # zip version is ~10x faster on a 12-column / 100 000-row flush and
    # surfaces a ragged row with table context instead of an opaque
    # mid-transpose IndexError.
    try:
        column_tuples = list(zip(*rows, strict=True))
    except ValueError as exc:
        # ``zip(..., strict=True)`` raises when row lengths disagree, but
        # so does a single-row buffer whose arity does not match the
        # schema. Re-check ``rows[0]`` here to attach the table name +
        # offending arity (the underlying ValueError otherwise carries
        # only the abstract iterator-length mismatch).
        raise HealthImportError(
            f"bulk_load_via_arrow: row arity {len(rows[0])} does not match "
            f"the {table!r} schema arity {n_cols} (or rows are ragged)"
        ) from exc
    if column_tuples and len(column_tuples) != n_cols:
        # Every row was uniform but did not match the schema width.
        raise HealthImportError(
            f"bulk_load_via_arrow: row arity {len(column_tuples)} does not match "
            f"the {table!r} schema arity {n_cols}"
        )
    # ``array_per_col`` builds one ``pa.array`` per column and feeds them
    # to ``Table.from_arrays`` instead of materialising the intermediate
    # ``dict[str, list]`` ``Table.from_pydict`` would build. The
    # microbench under ``tmp/perf-probe/arrow_microbench.py`` (issue #56)
    # measures ~11% higher build throughput on a 100k-row records flush
    # and skips the per-column ``list(...)`` copy that ``from_pydict``
    # otherwise forces.
    arrays = [
        pa.array(col, type=field.type) for field, col in zip(schema, column_tuples, strict=True)
    ]
    tbl = pa.Table.from_arrays(arrays, schema=schema)

    # The importer runs single-threaded per process (the orchestrator
    # serialises XML → ECG → GPX → finalize), so a fixed registration
    # name is safe and the unregister in ``finally`` keeps the namespace
    # clean across batches.
    conn.register("__bulk_arrow", tbl)
    try:
        conn.execute(f"INSERT INTO {table} SELECT * FROM __bulk_arrow")
    finally:
        conn.unregister("__bulk_arrow")
