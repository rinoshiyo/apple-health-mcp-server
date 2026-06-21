"""Import pipelines (XML / GPX / ECG / dedup)."""

from __future__ import annotations

from apple_health_mcp.importers.dedup import finalize_import
from apple_health_mcp.importers.ecg import import_ecg_files, import_single_ecg
from apple_health_mcp.importers.gpx import (
    clean_timestamp,
    import_gpx_files,
    import_single_gpx,
    shift_utc_to_local,
)
from apple_health_mcp.importers.orchestrator import make_import_id, run_import
from apple_health_mcp.importers.xml import ImportStats, import_xml

__all__ = [
    "ImportStats",
    "clean_timestamp",
    "finalize_import",
    "import_ecg_files",
    "import_gpx_files",
    "import_single_ecg",
    "import_single_gpx",
    "import_xml",
    "make_import_id",
    "run_import",
    "shift_utc_to_local",
]
