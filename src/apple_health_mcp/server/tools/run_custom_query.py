"""``run_custom_query`` MCP tool."""

from __future__ import annotations

import logging
from threading import Lock
from typing import TYPE_CHECKING, Annotated

import duckdb
from pydantic import Field

from apple_health_mcp.server.query import (
    build_query_error_envelope,
    query_to_json,
    run_query_payload,
)
from apple_health_mcp.server.query_error import (
    translate_binder_exception,
    translate_catalog_exception,
    translate_parser_exception,
)
from apple_health_mcp.server.safety import (
    MAX_CUSTOM_QUERY_ROWS,
    QueryValidationError,
    validate_query,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_logger = logging.getLogger(__name__)


DESCRIPTION = (
    "Run a read-only SQL query (DuckDB dialect). Must start with SELECT or "
    "WITH. Returns an envelope: ``{rows, row_count, truncated, max_rows, "
    "user_supplied_limit}``. When the caller does NOT pass a LIMIT, the "
    f"server caps the result at max_rows={MAX_CUSTOM_QUERY_ROWS} and sets "
    "``truncated: true`` whenever the underlying result set had more rows; "
    "use ``LIMIT/OFFSET`` (and resubmit) to page through the rest. When "
    "the caller passes their own LIMIT, ``truncated`` is always false and "
    "``user_supplied_limit`` is true. Tables: records (record_hash, "
    "record_type, value, unit, source_name, device, start_date, end_date), "
    "record_metadata (record_hash, key, value), workouts (workout_hash, "
    "activity_type, duration, total_distance, total_energy_burned, "
    "start_date, end_date), workout_events, workout_statistics, "
    "workout_metadata (workout_hash, key, value), workout_routes, "
    "activity_summaries, ecg_readings, ecg_samples, route_points "
    "(latitude, longitude, elevation, timestamp, speed), "
    "heart_rate_samples, correlations, correlation_members "
    "(correlation_hash, record_hash), daily_record_stats (record_type, "
    "date, unit, count, avg_value, min_value, max_value, sum_value), "
    "state_of_mind (record_hash, valence, kind, labels, associations), "
    "imports, export_metadata (import_id, export_date, locale), "
    "me_attributes (import_id, date_of_birth, biological_sex, blood_type, "
    "fitzpatrick_skin_type, cardio_fitness_medications_use). "
    "External access: queries operate only over in-DB relations. The "
    "engine refuses every fs / network function (read_csv, read_parquet, "
    "parquet_scan, parquet_metadata, parquet_schema, sniff_csv, glob, "
    "read_blob, read_text, read_json, read_ndjson, and their _auto "
    "variants) plus ATTACH / COPY / INSTALL / LOAD and any http / https / "
    "s3 / gs / az URL — there is no opt-in. Use list_zips + import_zip "
    "to bring new data in; ad-hoc SQL cannot reach the host filesystem "
    "or the network."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def run_custom_query(
        query: Annotated[
            str,
            Field(
                description="A read-only SQL query (must start with SELECT or WITH)",
                max_length=65536,
            ),
        ],
    ) -> str:
        trimmed = query.strip()
        try:
            stmt = validate_query(trimmed)
        except QueryValidationError as exc:
            return build_query_error_envelope(reason=exc.reason, message=str(exc))
        user_supplied_limit = stmt.args.get("limit") is not None
        # v0.4.1 (issue #159): when the caller omitted LIMIT we probe
        # with ``MAX_CUSTOM_QUERY_ROWS + 1`` so a result set sitting at
        # exactly the cap can be distinguished from one that overflows.
        # The old behaviour silently truncated at the cap and returned
        # the row list without any marker -- callers could not tell
        # whether they got the whole answer or just the first N.
        if user_supplied_limit:
            sql = stmt.sql(dialect="duckdb")
        else:
            sql = stmt.limit(MAX_CUSTOM_QUERY_ROWS + 1).sql(dialect="duckdb")
        # v0.6.1 (issue #273): translate the three DuckDB engine
        # exception classes we know how to describe into typed
        # envelopes with actionable hints. Any other engine exception
        # falls through to the generic ``execution_error`` reason so
        # the wire always carries the same envelope shape.
        try:
            rows = query_to_json(conn, sql, lock=lock)
        except duckdb.CatalogException as exc:
            return translate_catalog_exception(conn, exc, lock=lock)
        except duckdb.BinderException as exc:
            return translate_binder_exception(conn, sql, exc, lock=lock)
        except duckdb.ParserException as exc:  # pragma: no cover - defensive
            # Any SQL that reaches this point already parsed cleanly under
            # sqlglot's duckdb dialect in ``validate_query`` (a genuine
            # syntax typo like "FRM" is rejected there as
            # ``QueryValidationError(reason="syntax_error")`` before
            # ``query_to_json`` ever runs). This branch only fires if a
            # future DuckDB release accepts SQL sqlglot's dialect does not
            # -- kept as defence-in-depth so that divergence still yields a
            # typed envelope instead of an unhandled exception.
            return translate_parser_exception(exc)
        except Exception as exc:
            _logger.debug("query failed: %s", exc)
            return build_query_error_envelope(reason="execution_error", message=str(exc))
        if user_supplied_limit:
            payload = {
                "rows": rows,
                "row_count": len(rows),
                "truncated": False,
                "max_rows": MAX_CUSTOM_QUERY_ROWS,
                "user_supplied_limit": True,
            }
        else:
            truncated = len(rows) > MAX_CUSTOM_QUERY_ROWS
            if truncated:
                rows = rows[:MAX_CUSTOM_QUERY_ROWS]
            payload = {
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "max_rows": MAX_CUSTOM_QUERY_ROWS,
                "user_supplied_limit": False,
            }
        return run_query_payload(payload)
