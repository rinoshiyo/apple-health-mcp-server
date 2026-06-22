"""``get_ecg_data`` MCP tool."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from apple_health_mcp.server.query import query_to_json, run_query_payload

if TYPE_CHECKING:
    import duckdb
    from mcp.server.fastmcp import FastMCP


DESCRIPTION = (
    "Get ECG data by ecg_hash. Returns: reading (metadata), stats "
    "(sample_count, mean_uv, min_uv, max_uv, stddev_uv -- STDDEV_SAMP), "
    "downsample_factor, and voltages_uv (empty by default). Set "
    "include_voltages=true to get the waveform array; pair with "
    "downsample_factor (e.g. 10) to thin it out so the LLM context doesn't "
    "get overwhelmed. Downsampling is naive every-Nth-sample decimation (no "
    "anti-alias filter) -- fine for waveform visualization, avoid for "
    "spectral analysis. NOTE: in earlier versions sample_count was a "
    "top-level field; it is now under stats. Get ecg_hash from list_ecg_readings."
)


def register(mcp: FastMCP, conn: duckdb.DuckDBPyConnection, lock: Lock) -> None:
    @mcp.tool(description=DESCRIPTION)
    async def get_ecg_data(
        ecg_hash: Annotated[
            str,
            Field(description="The ECG hash identifier"),
        ],
        include_voltages: Annotated[
            bool | None,
            Field(
                description="If true, also return the voltage_uv samples "
                "array (potentially tens of thousands of values -- use "
                "sparingly through an LLM, or pair with downsample_factor). "
                "Defaults to false: only metadata + summary stats are returned.",
            ),
        ] = None,
        downsample_factor: Annotated[
            int | None,
            Field(
                description="Keep every Nth voltage sample when "
                "include_voltages=true. e.g. 10 returns ~1.5k samples from a "
                "30s/512Hz recording. Defaults to 1 (no downsampling). "
                "Clamped to >= 1.",
            ),
        ] = None,
    ) -> str:
        downsample = max(downsample_factor or 1, 1)
        include = bool(include_voltages)
        try:
            metadata = query_to_json(
                conn,
                "SELECT * FROM ecg_readings WHERE ecg_hash = ?",
                [ecg_hash],
                lock=lock,
            )
            stats_rows = query_to_json(
                conn,
                "SELECT COUNT(*) AS sample_count, "
                "COALESCE(AVG(voltage_uv), 0.0) AS mean_uv, "
                "COALESCE(MIN(voltage_uv), 0.0) AS min_uv, "
                "COALESCE(MAX(voltage_uv), 0.0) AS max_uv, "
                "COALESCE(STDDEV_SAMP(voltage_uv), 0.0) AS stddev_uv "
                "FROM ecg_samples WHERE ecg_hash = ?",
                [ecg_hash],
                lock=lock,
            )
            if include:
                samples = query_to_json(
                    conn,
                    "SELECT voltage_uv FROM ecg_samples WHERE ecg_hash = ? ORDER BY sample_idx",
                    [ecg_hash],
                    lock=lock,
                )
                voltages = [s["voltage_uv"] for s in samples[::downsample]]
            else:
                voltages = []
        except Exception as exc:
            return f"Error: {exc}"
        payload = {
            "reading": metadata[0] if metadata else None,
            "stats": stats_rows[0] if stats_rows else None,
            "downsample_factor": downsample,
            "voltages_uv": voltages,
        }
        return run_query_payload(payload)
