"""Importer for Apple Health GPX route files.

Apple Health stores per-workout GPS tracks under ``<export>/workout-routes/``
as standard GPX 1.1 files with Apple extensions (``speed``, ``course``,
``hAcc``, ``vAcc`` inside ``<extensions>``). We stream with
``lxml.etree.iterparse`` keyed on ``end`` events for the ``trkpt`` element
since that is the only block we materialize. Each route file joins back to
the parent workout via the ``workout_route_map`` produced by the XML
importer.

Time-zone handling: GPX timestamps are true UTC (``...Z``) while the rest
of the database stores local wall-clock time as naive ``TIMESTAMP``. We
shift route points by the owning workout's offset (carried in
``workout_offset_map``) so a route joins cleanly against its workout's
``start_date``. When the offset is unknown the importer falls back to
strip-only behavior; the row still lands, but its wall-clock will be UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from apple_health_mcp.exceptions import HealthImportError
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


def clean_timestamp(ts: str) -> str:
    """Strip TZ info from an ISO 8601 timestamp and use space as separator.

    ``"2020-06-20T16:56:44Z"`` -> ``"2020-06-20 16:56:44"``. Note that this
    preserves the original UTC instant in the emitted string, which only
    matches the rest of the database if the rest is also interpreted as
    UTC. The XML/ECG importers store local wall-clock time; the GPX
    importer normally calls :func:`shift_utc_to_local` to align with that
    convention, and this helper is only the fallback when the owning
    workout's offset is unknown.
    """
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if len(s) > 6:
        tail = s[-6:]
        if (tail.startswith("+") or tail.startswith("-")) and ":" in tail:
            s = s[:-6]
    return s.replace("T", " ")


def shift_utc_to_local(ts: str, offset_minutes: int) -> str:
    """Shift a true-UTC GPX timestamp by ``offset_minutes`` into naive local form.

    ``"2020-06-20T16:56:44Z"`` plus 540 (i.e. ``+09:00``) ->
    ``"2020-06-21 01:56:44"``. Falls back to :func:`clean_timestamp` when
    the input cannot be parsed as ISO 8601 so a malformed ``<time>`` element
    degrades gracefully instead of dropping the point.
    """
    trimmed = ts.strip()
    dt: datetime | None = None
    # Python 3.11+ understands trailing 'Z' on fromisoformat.
    for candidate in (trimmed, trimmed.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            dt = parsed.replace(tzinfo=None) - parsed.utcoffset()  # type: ignore[operator]
        else:
            dt = parsed
        break
    if dt is None:
        return clean_timestamp(ts)
    shifted = dt + timedelta(minutes=offset_minutes)
    return shifted.strftime("%Y-%m-%d %H:%M:%S")


def import_single_gpx(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    import_id: str,
    workout_hash: str | None,
    workout_offset_minutes: int | None,
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
                            point_hash = compute_hash([wh, timestamp, str(lat), str(lon)])
                            if workout_offset_minutes is not None:
                                clean_ts = shift_utc_to_local(timestamp, workout_offset_minutes)
                            else:
                                clean_ts = clean_timestamp(timestamp)
                            rows.append(
                                (
                                    point_hash,
                                    workout_hash,
                                    lat,
                                    lon,
                                    ele,
                                    clean_ts,
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

    if rows:
        conn.executemany(
            "INSERT INTO route_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def _parse_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def import_gpx_files(
    conn: duckdb.DuckDBPyConnection,
    routes_dir: Path,
    import_id: str,
    workout_route_map: dict[str, str],
    workout_offset_map: dict[str, int],
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
    for path in entries:
        # Map keys are the verbatim FileReference path Apple emits, e.g.
        # "/workout-routes/route_2020-05-21_1.14pm.gpx".
        route_key = f"/workout-routes/{path.name}"
        workout_hash = workout_route_map.get(route_key)
        workout_offset: int | None = None
        if workout_hash is not None:
            workout_offset = workout_offset_map.get(workout_hash)
        try:
            total += import_single_gpx(conn, path, import_id, workout_hash, workout_offset)
        except HealthImportError as exc:
            _logger.warning("Failed to import GPX file %s: %s", path, exc)
        except OSError as exc:
            _logger.warning("Failed to read GPX file %s: %s", path, exc)
    _logger.info("Imported %d route points from GPX files", total)
    return total
