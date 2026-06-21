"""Tests for importers.gpx.

Fixtures use synthetic coordinates (San Francisco / Tokyo) and timestamps;
no real movement data is replayed here.
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
    clean_timestamp,
    import_gpx_files,
    import_single_gpx,
    shift_utc_to_local,
)


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
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


def test_clean_timestamp_z_suffix() -> None:
    assert clean_timestamp("2020-06-20T16:56:44Z") == "2020-06-20 16:56:44"


def test_clean_timestamp_positive_offset() -> None:
    assert clean_timestamp("2020-06-20T16:56:44+00:00") == "2020-06-20 16:56:44"


def test_clean_timestamp_negative_offset() -> None:
    assert clean_timestamp("2020-06-20T16:56:44-05:00") == "2020-06-20 16:56:44"


def test_clean_timestamp_short_string_passthrough() -> None:
    assert clean_timestamp("12:00") == "12:00"


def test_clean_timestamp_no_tz_just_replaces_t() -> None:
    assert clean_timestamp("2020-06-20T16:56:44") == "2020-06-20 16:56:44"


def test_shift_utc_to_local_jst() -> None:
    assert shift_utc_to_local("2020-06-20T16:56:44Z", 540) == "2020-06-21 01:56:44"


def test_shift_utc_to_local_pst() -> None:
    assert shift_utc_to_local("2024-03-03T15:00:00Z", -480) == "2024-03-03 07:00:00"


def test_shift_utc_to_local_explicit_offset_in_input() -> None:
    assert shift_utc_to_local("2024-03-03T15:00:00+00:00", 540) == "2024-03-04 00:00:00"


def test_shift_utc_to_local_unparseable_falls_back() -> None:
    assert shift_utc_to_local("garbage", 540) == "garbage"


def test_shift_utc_to_local_accepts_space_separator() -> None:
    # Some GPX writers emit "YYYY-MM-DD HH:MM:SS" instead of with 'T'.
    assert shift_utc_to_local("2024-01-01 00:00:00", 60) == "2024-01-01 01:00:00"


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
    count = import_single_gpx(conn, path, "imp", "wh_1", None)
    assert count == 2
    row = conn.execute("SELECT COUNT(*) FROM route_points").fetchone()
    assert row is not None and int(row[0]) == 2
    row = conn.execute(
        "SELECT workout_hash, speed, course, h_accuracy, v_accuracy FROM route_points LIMIT 1"
    ).fetchone()
    assert row == ("wh_1", 3.5, 180.0, 5.0, 3.0)


def test_import_single_gpx_shifts_by_workout_offset(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
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
    import_single_gpx(conn, path, "imp", "wh_jst", 540)
    ts = conn.execute("SELECT CAST(timestamp AS VARCHAR) FROM route_points").fetchone()
    assert ts == ("2024-06-17 13:58:39",)


def test_import_single_gpx_without_offset_uses_legacy_strip(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <trk><trkseg>
    <trkpt lat="35.0" lon="139.0">
      <ele>10.0</ele>
      <time>2024-06-17T04:58:39Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    path = _write_gpx(tmp_path, "orphan.gpx", gpx)
    import_single_gpx(conn, path, "imp", None, None)
    ts = conn.execute("SELECT CAST(timestamp AS VARCHAR) FROM route_points").fetchone()
    assert ts == ("2024-06-17 04:58:39",)


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
    count = import_single_gpx(conn, path, "imp", "wh_p", None)
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
    import_single_gpx(conn, path, "imp", "wh_b", None)
    row = conn.execute("SELECT elevation, speed FROM route_points").fetchone()
    assert row == (None, None)


def test_import_single_gpx_missing_file_raises(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    with pytest.raises(HealthImportError, match="failed to open"):
        import_single_gpx(conn, tmp_path / "nope.gpx", "imp", None, None)


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
        import_single_gpx(conn, path, "imp", None, None)


def test_import_gpx_files_missing_dir(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    count = import_gpx_files(conn, tmp_path / "missing", "imp", {}, {})
    assert count == 0


def test_import_gpx_files_routes_files_to_workout(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    d = tmp_path / "routes"
    d.mkdir()
    (d / "route_2024-01-01.gpx").write_text(_MINIMAL_GPX, encoding="utf-8")
    (d / "irrelevant.txt").write_text("ignored", encoding="utf-8")
    route_map = {"/workout-routes/route_2024-01-01.gpx": "wh_mapped"}
    offset_map = {"wh_mapped": 60}
    total = import_gpx_files(conn, d, "imp", route_map, offset_map)
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
        workout_offset: int | None,
    ) -> int:
        state["n"] += 1
        if state["n"] == 1:
            raise HealthImportError("boom")
        return real(conn, path, import_id, workout_hash, workout_offset)

    monkeypatch.setattr(gpx_module, "import_single_gpx", flaky)
    total = import_gpx_files(conn, d, "imp", {}, {})
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
    count = import_single_gpx(conn, path, "imp", None, None)
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
    count = import_single_gpx(conn, path, "imp", "wh_e", None)
    assert count == 1


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
    total = import_gpx_files(conn, d, "imp", {}, {})
    assert total == 0
    assert any("Failed to read GPX" in rec.message for rec in caplog.records)
