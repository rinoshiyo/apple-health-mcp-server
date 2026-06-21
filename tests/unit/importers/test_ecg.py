"""Tests for importers.ecg.

Synthetic test fixtures only -- no real device IDs or personal data.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers.ecg import (
    _match_header,
    _parse_sample_rate,
    _strip_bom,
    _strip_tz_suffix,
    import_ecg_files,
    import_single_ecg,
)

# A synthetic English-locale ECG export. Voltage samples are illustrative
# (not from a real recording).
_MINIMAL_ECG_CSV = """Name,Test User
Date of Birth,1990-01-01
Recorded Date,2024-06-15 10:30:00 +0000
Classification,Sinus Rhythm
Symptoms,None
Software Version,2.0
Device,"Apple Watch"
Sample Rate,512.000 Hz
Lead,Lead I
Unit,uV

100
200
-50
150
75
"""

# Synthetic Japanese-locale ECG export ("current iOS in JP" variant:
# 記録日 without trailing 時, and "512ヘルツ" with no separator).
_JAPANESE_ECG_CSV = """名前,テスト ユーザー
生年月日,"1990/01/01"
記録日,2024-06-15 10:30:00 +0900
分類,洞調律
症状,
ソフトウェアバージョン,1.90
デバイス,"Apple Watch"
サンプルレート,512ヘルツ
リード,リードI
単位,uV

100
200
"""


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    yield c
    c.close()


# --- pure-helper tests ------------------------------------------------------


def test_match_header_first_match_wins() -> None:
    assert _match_header("Recorded Date,2024-01-01", ("Recorded Date",)) == "2024-01-01"


def test_match_header_requires_comma_separator() -> None:
    # Must not match "Recorded Datetime,..." -- comma is part of the prefix.
    assert _match_header("Recorded Datetime,x", ("Recorded Date",)) is None


def test_match_header_preserves_internal_commas() -> None:
    # The matcher returns the whole slice after the first comma, so commas
    # inside the value are preserved (issue #5 acceptance criterion).
    assert (
        _match_header("Symptoms,Palpitations, Shortness of breath", ("Symptoms",))
        == "Palpitations, Shortness of breath"
    )


def test_match_header_none_when_no_match() -> None:
    assert _match_header("Name,foo", ("Recorded Date",)) is None


def test_strip_bom_handles_both_cases() -> None:
    assert _strip_bom("hello") == "hello"
    assert _strip_bom("﻿hello") == "hello"


def test_strip_tz_suffix_positive_and_negative() -> None:
    assert _strip_tz_suffix("2024-01-01 10:00:00 +0900") == "2024-01-01 10:00:00"
    assert _strip_tz_suffix("2024-01-01 10:00:00 -0500") == "2024-01-01 10:00:00"
    assert _strip_tz_suffix("2024-01-01 10:00:00") == "2024-01-01 10:00:00"


def test_parse_sample_rate_english_and_japanese() -> None:
    assert _parse_sample_rate("513.992 hertz") == 513.992
    assert _parse_sample_rate("512.000 Hz") == 512.0
    assert _parse_sample_rate("512ヘルツ") == 512.0
    assert _parse_sample_rate("512Hz") == 512.0


def test_parse_sample_rate_no_digits_returns_none() -> None:
    assert _parse_sample_rate("ヘルツ") is None
    assert _parse_sample_rate("") is None


def test_parse_sample_rate_bad_numeric_returns_none() -> None:
    # The chars-loop would yield "." alone, which fails the float parse.
    assert _parse_sample_rate(".x") is None


# --- end-to-end tests -------------------------------------------------------


def _write_csv(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_import_single_ecg_minimal(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "ecg_2024.csv", _MINIMAL_ECG_CSV)
    import_single_ecg(conn, path, "imp1")

    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1
    row = conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 5
    row = conn.execute(
        "SELECT classification, device, sample_rate_hz, symptoms FROM ecg_readings"
    ).fetchone()
    assert row == ("Sinus Rhythm", "Apple Watch", 512.0, "None")


def test_import_single_ecg_japanese_locale(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "ecg_ja.csv", _JAPANESE_ECG_CSV)
    import_single_ecg(conn, path, "imp_ja")
    row = conn.execute(
        "SELECT classification, sample_rate_hz, symptoms FROM ecg_readings"
    ).fetchone()
    assert row is not None
    classification, sample_rate, symptoms = row
    assert classification == "洞調律"
    assert sample_rate == 512.0
    # Empty Symptoms value must land as NULL, not empty string.
    assert symptoms is None


def test_import_single_ecg_bom(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    path = tmp_path / "ecg_bom.csv"
    # Write with explicit BOM bytes -- chardet should still produce a UTF-8
    # decoding and the strip_bom helper drops the leading code point.
    path.write_bytes(("﻿" + _MINIMAL_ECG_CSV).encode("utf-8"))
    import_single_ecg(conn, path, "imp_bom")
    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1


def test_import_single_ecg_crlf_line_endings(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    path = _write_csv(tmp_path, "ecg_crlf.csv", _MINIMAL_ECG_CSV.replace("\n", "\r\n"))
    import_single_ecg(conn, path, "imp_crlf")
    row = conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 5


def test_import_single_ecg_missing_date_raises(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    csv = "Name,Test\nClassification,Normal\n\n100\n200\n"
    path = _write_csv(tmp_path, "bad.csv", csv)
    with pytest.raises(HealthImportError, match="no recorded date"):
        import_single_ecg(conn, path, "imp_bad")


def test_import_single_ecg_no_voltages_does_not_insert_samples(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A header-only file (no voltage rows) skips the bulk insert."""
    csv = 'Recorded Date,2024-06-15 10:30:00 +0000\nDevice,"Apple Watch"\nSample Rate,512 Hz\n'
    path = _write_csv(tmp_path, "ecg_no_volts.csv", csv)
    import_single_ecg(conn, path, "imp_empty")
    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1
    row = conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 0


def test_import_single_ecg_skips_blank_lines_in_voltage_section(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Blank lines interspersed in the voltage section are tolerated."""
    csv = 'Recorded Date,2024-06-15 10:30:00 +0000\nDevice,"Apple Watch"\n\n100\n\n200\n\n300\n'
    path = _write_csv(tmp_path, "ecg_blanks.csv", csv)
    import_single_ecg(conn, path, "imp_blank")
    row = conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 3


def test_import_single_ecg_voltage_section_terminates_on_non_numeric(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # A trailing "END" line stops the voltage parse but the preceding
    # samples are still recorded.
    csv = 'Recorded Date,2024-06-15 10:30:00 +0000\nDevice,"Apple Watch"\n\n10\n20\nEND\n30\n'
    path = _write_csv(tmp_path, "ecg_terminate.csv", csv)
    import_single_ecg(conn, path, "imp_term")
    row = conn.execute("SELECT COUNT(*) FROM ecg_samples").fetchone()
    assert row is not None and int(row[0]) == 2


def test_import_ecg_files_missing_dir_returns_zero(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    count = import_ecg_files(conn, tmp_path / "missing", "imp")
    assert count == 0


def test_import_ecg_files_imports_csv_and_skips_others(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    d = tmp_path / "ecgs"
    d.mkdir()
    (d / "a.csv").write_text(_MINIMAL_ECG_CSV, encoding="utf-8")
    (d / "b.txt").write_text("ignored", encoding="utf-8")
    count = import_ecg_files(conn, d, "imp")
    assert count == 1


def test_import_ecg_files_logs_and_continues_on_per_file_error(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    d = tmp_path / "ecgs"
    d.mkdir()
    (d / "good.csv").write_text(_MINIMAL_ECG_CSV, encoding="utf-8")
    (d / "bad.csv").write_text("Name,Foo\n", encoding="utf-8")  # no recorded date
    count = import_ecg_files(conn, d, "imp")
    assert count == 1
    assert any("Failed to import ECG file" in rec.message for rec in caplog.records)


def test_import_ecg_files_logs_oserror(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = tmp_path / "ecgs"
    d.mkdir()
    (d / "boom.csv").write_text(_MINIMAL_ECG_CSV, encoding="utf-8")

    from apple_health_mcp.importers import ecg as ecg_module

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(ecg_module, "import_single_ecg", boom)
    count = import_ecg_files(conn, d, "imp")
    assert count == 0
    assert any("Failed to read ECG file" in rec.message for rec in caplog.records)


def test_read_text_falls_back_when_chardet_returns_none(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-UTF-8 bytes + chardet returning None falls through to UTF-8 with replace."""
    path = tmp_path / "ecg_undetected.csv"
    # Insert a raw 0xFF byte (invalid UTF-8) so the strict UTF-8 attempt
    # fails and the chardet fallback path runs.
    body = _MINIMAL_ECG_CSV.encode("utf-8") + b"\xff"
    path.write_bytes(body)

    from apple_health_mcp.importers import ecg as ecg_module

    monkeypatch.setattr(ecg_module.chardet, "detect", lambda _raw: {"encoding": None})
    import_single_ecg(conn, path, "imp_fallback")
    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1


def test_read_text_handles_unknown_encoding(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chardet-claimed encoding that Python does not know triggers the LookupError fallback."""
    path = tmp_path / "ecg_funky.csv"
    body = _MINIMAL_ECG_CSV.encode("utf-8") + b"\xff"
    path.write_bytes(body)

    from apple_health_mcp.importers import ecg as ecg_module

    monkeypatch.setattr(
        ecg_module.chardet, "detect", lambda _raw: {"encoding": "definitely-not-a-real-encoding"}
    )
    import_single_ecg(conn, path, "imp_funky")
    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1


def test_read_text_decodes_shift_jis_via_chardet(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Non-UTF-8 (Shift_JIS) Japanese ECG CSV decodes through the chardet fallback."""
    # Japanese localized header in Shift_JIS — not valid UTF-8, forces the
    # chardet sniffing path.
    csv_text = (
        "記録日,2024-01-01 12:00:00 +0900\n"
        "分類,Sinus Rhythm\n"
        "デバイス,Apple Watch\n"
        "ソフトウェアバージョン,10.4\n"
        "サンプリングレート,512 Hz\n"
        "症状,なし\n"
        "\n"
        "電圧,\n"
        "0.001\n"
        "0.002\n"
    )
    path = tmp_path / "ecg_jp.csv"
    path.write_bytes(csv_text.encode("shift_jis"))
    import_single_ecg(conn, path, "imp_sjis")
    row = conn.execute("SELECT COUNT(*) FROM ecg_readings").fetchone()
    assert row is not None and int(row[0]) == 1
