"""Tests for importers.gpx.

Fixtures use synthetic coordinates (San Francisco / Tokyo) and timestamps;
no real movement data is replayed here. The connection fixture pins the
session timezone to UTC so timestamp assertions stay stable across the
CI matrix's three operating systems.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers.gpx import (
    _strip_ns,
    import_gpx_files,
    import_single_gpx,
)


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    # Pin the session TZ so TIMESTAMPTZ -> string casts are deterministic
    # regardless of the host's OS local TZ.
    c.execute("SET TimeZone = 'UTC';")
    yield c
    c.close()


def _write_gpx(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- pure-helper tests ------------------------------------------------------


def test_strip_ns_with_and_without_namespace() -> None:
    assert _strip_ns("{http://example}foo") == "foo"
    assert _strip_ns("foo") == "foo"


# --- end-to-end importer tests ----------------------------------------------


_MINIMAL_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="37.7749" lon="-122.4194">
      <ele>10.5</ele>
      <time>2024-01-01T10:00:00Z</time>
      <speed>3.5</speed>
      <course>180.0</course>
      <hAcc>5.0</hAcc>
      <vAcc>3.0</vAcc>
    </trkpt>
    <trkpt lat="37.7750" lon="-122.4195">
      <ele>11.0</ele>
      <time>2024-01-01T10:00:05Z</time>
      <speed>3.6</speed>
      <course>181.0</course>
      <hAcc>4.5</hAcc>
      <vAcc>2.8</vAcc>
    </trkpt>
  </trkseg></trk>
</gpx>"""


def test_import_single_gpx_minimal(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    path = _write_gpx(tmp_path, "route.gpx", _MINIMAL_GPX)
    count = import_single_gpx(conn, path, "imp", "wh_1")
    assert count == 2
    row = conn.execute("SELECT COUNT(*) FROM route_points").fetchone()
    assert row is not None and int(row[0]) == 2
    row = conn.execute(
        "SELECT workout_hash, speed, course, h_accuracy, v_accuracy FROM route_points LIMIT 1"
    ).fetchone()
    assert row == ("wh_1", 3.5, 180.0, 5.0, 3.0)


def test_import_single_gpx_preserves_utc_instant(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A ``Z``-suffixed GPX timestamp lands as a true UTC instant in TIMESTAMPTZ."""
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.0" lon="139.0">
      <ele>10.0</ele>
      <time>2024-06-17T04:58:39Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "jst.gpx", gpx)
    import_single_gpx(conn, path, "imp", "wh_jst")
    # Session TZ is UTC (see fixture) so CAST renders as the UTC instant.
    ts = conn.execute("SELECT CAST(timestamp AS VARCHAR) FROM route_points").fetchone()
    assert ts == ("2024-06-17 04:58:39+00",)


def test_import_single_gpx_skips_trkpt_missing_required(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # The first trkpt lacks lat; the second lacks time. Both must be dropped
    # rather than inserted with NULL values that would break joins.
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lon="139.0"><time>2024-01-01T00:00:00Z</time></trkpt>
    <trkpt lat="35.0" lon="139.0"></trkpt>
    <trkpt lat="35.0" lon="139.0"><time>2024-01-01T00:00:01Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "partial.gpx", gpx)
    count = import_single_gpx(conn, path, "imp", "wh_p")
    assert count == 1


def test_import_single_gpx_invalid_numeric_falls_back_to_none(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.0" lon="139.0">
      <ele>not-a-number</ele>
      <time>2024-01-01T00:00:00Z</time>
      <speed>also-bad</speed>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "bad_numeric.gpx", gpx)
    import_single_gpx(conn, path, "imp", "wh_b")
    row = conn.execute("SELECT elevation, speed FROM route_points").fetchone()
    assert row == (None, None)


def test_import_single_gpx_missing_file_raises(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    with pytest.raises(HealthImportError, match="failed to open"):
        import_single_gpx(conn, tmp_path / "nope.gpx", "imp", None)


def test_import_single_gpx_unrecoverable_syntax_error(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An XMLSyntaxError raised mid-iteration is translated to HealthImportError."""
    from lxml import etree

    from apple_health_mcp.importers import gpx as gpx_module

    class _Boom:
        def __iter__(self) -> _Boom:
            return self

        def __next__(self) -> object:
            raise etree.XMLSyntaxError("simulated", 0, 0, 0)

    monkeypatch.setattr(gpx_module.etree, "iterparse", lambda *_a, **_kw: _Boom())
    path = _write_gpx(tmp_path, "ok.gpx", _MINIMAL_GPX)
    with pytest.raises(HealthImportError, match="unrecoverable GPX"):
        import_single_gpx(conn, path, "imp", None)


def test_import_gpx_files_missing_dir(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    count = import_gpx_files(conn, tmp_path / "missing", "imp", {})
    assert count == 0


def test_import_gpx_files_routes_files_to_workout(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    d = tmp_path / "routes"
    d.mkdir()
    (d / "route_2024-01-01.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")
    (d / "irrelevant.txt").write_text("ignored", encoding="utf-8")
    route_map = {"/workout-routes/route_2024-01-01.gpx": "wh_mapped"}
    total = import_gpx_files(conn, d, "imp", route_map)
    assert total == 2
    row = conn.execute("SELECT DISTINCT workout_hash FROM route_points").fetchone()
    assert row == ("wh_mapped",)


def test_import_gpx_files_handles_per_file_error(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = tmp_path / "routes"
    d.mkdir()
    (d / "good.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")
    (d / "bad.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")

    from apple_health_mcp.importers import gpx as gpx_module

    real = gpx_module.import_single_gpx
    state = {"n": 0}

    def flaky(
        conn: duckdb.DuckDBPyConnection,
        path: Path,
        import_id: str,
        workout_hash: str | None,
        **kwargs: object,
    ) -> int:
        state["n"] += 1
        if state["n"] == 1:
            raise HealthImportError("boom")
        return real(conn, path, import_id, workout_hash, **kwargs)

    monkeypatch.setattr(gpx_module, "import_single_gpx", flaky)
    total = import_gpx_files(conn, d, "imp", {})
    # Two files; one raised so only the second contributed 2 points.
    assert total == 2
    assert any("Failed to import GPX" in rec.message for rec in caplog.records)


def test_import_single_gpx_empty_file_returns_zero(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A GPX with no trkpt elements skips the executemany and returns 0."""
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg></trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "empty.gpx", gpx)
    count = import_single_gpx(conn, path, "imp", None)
    assert count == 0


def test_import_single_gpx_unknown_child_inside_trkpt_ignored(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """An unrecognized child element inside trkpt is silently skipped."""
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.0" lon="139.0">
      <extensions><customField>ignored</customField></extensions>
      <time>2024-01-01T00:00:00Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "extn.gpx", gpx)
    count = import_single_gpx(conn, path, "imp", "wh_e")
    assert count == 1


def test_import_gpx_files_warns_on_unmatched_workout(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A GPX file with no entry in workout_route_map logs a warning and inserts
    points with NULL workout_hash, rather than dropping the data silently."""
    d = tmp_path / "routes"
    d.mkdir()
    (d / "orphan.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")
    # Empty map → no match for the file.
    total = import_gpx_files(conn, d, "imp_orphan", {})
    assert total == 2
    assert any("has no matching workout" in rec.message for rec in caplog.records)
    assert any("Imported 1 GPX file(s) with no matching" in rec.message for rec in caplog.records)
    row = conn.execute("SELECT DISTINCT workout_hash FROM route_points").fetchone()
    assert row == (None,)


def test_parse_float_rejects_non_finite() -> None:
    """NaN / Infinity must be dropped so they cannot poison downstream aggregates."""
    from apple_health_mcp.importers.gpx import _parse_float, _rust_float_repr

    assert _parse_float("NaN") is None
    assert _parse_float("Infinity") is None
    assert _parse_float("-Infinity") is None
    assert _parse_float("3.14") == 3.14
    # _rust_float_repr formats whole-number floats without trailing .0 for
    # byte-for-byte parity with the Rust importer's hash composition.
    assert _rust_float_repr(35.0) == "35"
    assert _rust_float_repr(-139.0) == "-139"
    assert _rust_float_repr(35.5) == "35.5"


def test_import_single_gpx_skips_non_finite_elevation(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A trkpt with NaN elevation must record the point but leave elevation NULL."""
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.5" lon="139.5">
      <ele>NaN</ele>
      <time>2024-01-01T00:00:00Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "nan.gpx", gpx)
    count = import_single_gpx(conn, path, "imp_nan", "wh")
    assert count == 1
    row = conn.execute("SELECT elevation FROM route_points").fetchone()
    assert row is not None
    assert row[0] is None


def test_import_gpx_files_oserror_in_loop_is_logged(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = tmp_path / "routes"
    d.mkdir()
    (d / "one.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")

    from apple_health_mcp.importers import gpx as gpx_module

    def boom(*_a: object, **_kw: object) -> int:
        raise OSError("disk failure")

    monkeypatch.setattr(gpx_module, "import_single_gpx", boom)
    total = import_gpx_files(conn, d, "imp", {})
    assert total == 0
    assert any("Failed to read GPX" in rec.message for rec in caplog.records)
