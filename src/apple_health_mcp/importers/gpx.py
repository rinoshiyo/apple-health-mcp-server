"""Importer for Apple Health GPX route files.

Apple Health stores per-workout GPS tracks under ``<export>/workout-routes/``
as standard GPX 1.1 files with Apple extensions (``speed``, ``course``,
``hAcc``, ``vAcc`` inside ``<extensions>``). We stream with
``lxml.etree.iterparse`` keyed on ``end`` events for the ``trkpt`` element
since that is the only block we materialize. Each route file joins back to
the parent workout via the ``workout_route_map`` produced by the XML
importer.

Time-zone handling: GPX timestamps are true UTC (``...Z``); we feed them
straight through to DuckDB's ``TIMESTAMPTZ`` parser, which keeps them as
UTC instants alongside the offset-bearing strings the XML importer emits.
The pre-TIMESTAMPTZ implementation had to shift them by the owning
workout's offset because the rest of the schema was naive ``TIMESTAMP``
holding wall-clock; that workaround is gone.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers._bulk_arrow import bulk_load_via_arrow
from apple_health_mcp.importers._hash import compute_hash

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

# GPX 1.1 default namespace; Apple emits it.
_GPX_NS = "http://www.topografix.com/GPX/1/1"


def _strip_ns(tag: str) -> str:
    """Return the local part of an lxml-style namespaced tag."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def import_single_gpx(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    import_id: str,
    workout_hash: str | None,
) -> int:
    """Parse one GPX file and insert its track points; return the count."""
    try:
        context = etree.iterparse(
            str(path),
            events=("start", "end"),
            recover=True,
            huge_tree=True,
        )
    except OSError as exc:
        raise HealthImportError(f"failed to open GPX file at {path}: {exc}") from exc

    rows: list[tuple[object, ...]] = []
    in_trkpt = False
    lat: float | None = None
    lon: float | None = None
    ele: float | None = None
    timestamp: str | None = None
    speed: float | None = None
    course: float | None = None
    h_acc: float | None = None
    v_acc: float | None = None

    try:
        for event, elem in context:
            tag = _strip_ns(elem.tag)
            if event == "start":
                if tag == "trkpt":
                    in_trkpt = True
                    lat = _parse_float(elem.get("lat"))
                    lon = _parse_float(elem.get("lon"))
                    ele = None
                    timestamp = None
                    speed = None
                    course = None
                    h_acc = None
                    v_acc = None
            else:  # end
                if in_trkpt:
                    if tag == "ele":
                        ele = _parse_float(elem.text)
                    elif tag == "time":
                        timestamp = elem.text
                    elif tag == "speed":
                        speed = _parse_float(elem.text)
                    elif tag == "course":
                        course = _parse_float(elem.text)
                    elif tag == "hAcc":
                        h_acc = _parse_float(elem.text)
                    elif tag == "vAcc":
                        v_acc = _parse_float(elem.text)
                    elif tag == "trkpt":
                        if lat is not None and lon is not None and timestamp is not None:
                            wh = workout_hash or ""
                            point_hash = compute_hash(
                                [wh, timestamp, _rust_float_repr(lat), _rust_float_repr(lon)]
                            )
                            rows.append(
                                (
                                    point_hash,
                                    workout_hash,
                                    lat,
                                    lon,
                                    ele,
                                    timestamp,
                                    speed,
                                    course,
                                    h_acc,
                                    v_acc,
                                    import_id,
                                )
                            )
                        in_trkpt = False
                elem.clear()
    except etree.XMLSyntaxError as exc:
        raise HealthImportError(f"unrecoverable GPX syntax error: {exc}") from exc

    bulk_load_via_arrow(conn, "route_points", rows)
    return len(rows)


def _parse_float(raw: str | None) -> float | None:
    """Parse ``raw`` as a finite float, returning ``None`` on failure.

    Rejects NaN / Infinity — a single non-finite value poisons every
    downstream aggregate because DuckDB propagates NaN through SUM/AVG.
    Mirrors :func:`apple_health_mcp.importers.xml._parse_opt_float`.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _rust_float_repr(x: float) -> str:
    """Format ``x`` to match Rust's ``f64::to_string`` byte-for-byte.

    Python's ``str(35.0)`` is ``'35.0'``; Rust's ``35.0_f64.to_string()`` is
    ``'35'``. The point_hash composition must use the Rust formatting so a
    DB built by the Rust binary stays compatible with the Python importer
    after migration.
    """
    if x.is_integer() and math.isfinite(x):
        return str(int(x))
    return repr(x)


def import_gpx_files(
    conn: duckdb.DuckDBPyConnection,
    routes_dir: Path,
    import_id: str,
    workout_route_map: dict[str, str],
) -> int:
    """Import every ``*.gpx`` under ``routes_dir``; return total point count.

    A missing directory is not an error. Individual file failures are
    logged and skipped so one corrupt GPX cannot abort the batch.
    """
    if not routes_dir.exists():
        _logger.info("No workout-routes directory found, skipping GPX import")
        return 0

    entries = sorted(p for p in routes_dir.iterdir() if p.suffix.lower() == ".gpx")
    _logger.info("Found %d GPX route files", len(entries))

    total = 0
    unmatched = 0
    for path in entries:
        # Map keys are the verbatim FileReference path Apple emits, e.g.
        # "/workout-routes/route_2020-05-21_1.14pm.gpx".
        route_key = f"/workout-routes/{path.name}"
        workout_hash = workout_route_map.get(route_key)
        if workout_hash is None:
            # The GPX file exists on disk but no Workout in the XML registered
            # this file path. Common causes: case-mismatched extraction on
            # case-insensitive filesystems, a Workout whose XML start handler
            # raised mid-element, or a third-party producer using a different
            # prefix. The points still land (with NULL workout_hash) so the
            # data is not lost, but warn so the orphan is surfaced.
            unmatched += 1
            _logger.warning(
                "GPX file %s has no matching workout in the XML map "
                "(route_key=%s); inserting points with NULL workout_hash",
                path.name,
                route_key,
            )
        try:
            total += import_single_gpx(conn, path, import_id, workout_hash)
        except HealthImportError as exc:
            _logger.warning("Failed to import GPX file %s: %s", path, exc)
        except OSError as exc:
            _logger.warning("Failed to read GPX file %s: %s", path, exc)
    if unmatched:
        _logger.warning(
            "Imported %d GPX file(s) with no matching workout out of %d total",
            unmatched,
            len(entries),
        )
    _logger.info("Imported %d route points from GPX files", total)
    return total
