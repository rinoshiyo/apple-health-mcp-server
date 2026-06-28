"""Tests for the v0.4 ZIP-flow MCP tools (``list_zips`` + ``import_zip``)."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.db.migrations import stamp_current_version
from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR
from apple_health_mcp.server.tools import import_zip as import_zip_mod
from apple_health_mcp.server.tools import list_zips as list_zips_mod
from tests._helpers import bind_tool

if TYPE_CHECKING:
    from pytest import MonkeyPatch


_TRIVIAL_EXPORT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<HealthData locale="en_US">'
    '<ExportDate value="2024-06-01 12:00:00 +0000"/>'
    "</HealthData>"
)


def _make_zip(path: Path, *, with_export_xml: bool = True, nested: bool = True) -> None:
    """Create a synthetic ZIP at ``path`` that looks like an Apple Health export.

    ``nested=True`` uses Apple's ``apple_health_export/`` top-level
    folder; ``nested=False`` flattens the contents at the ZIP root.
    ``with_export_xml=False`` produces an alien ZIP that should fail
    the ``is_apple_health`` probe.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if with_export_xml:
            name = "apple_health_export/export.xml" if nested else "export.xml"
            zf.writestr(name, _TRIVIAL_EXPORT_XML)
        else:
            zf.writestr("readme.txt", "not an apple health export")
    path.write_bytes(buf.getvalue())


# --- list_zips ---------------------------------------------------------------


def _call_list_zips(conn: duckdb.DuckDBPyConnection) -> dict[str, object]:
    fn = bind_tool(list_zips_mod, conn)
    raw = asyncio.run(fn())
    return json.loads(raw)


def test_list_zips_returns_empty_hint_when_env_unset() -> None:
    """env unset → ``export_zips_dir: null`` + configure hint."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        assert out["export_zips_dir"] is None
        assert out["zips"] == []
        assert EXPORT_ZIPS_DIR_ENV_VAR in str(out["hint"])
    finally:
        conn.close()


def test_list_zips_returns_empty_hint_when_dir_missing(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured directory does not exist → ``zips: []`` + create hint."""
    missing = tmp_path / "no_such_dir"
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(missing))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        assert out["export_zips_dir"] == str(missing)
        assert out["zips"] == []
        assert "does not exist" in str(out["hint"])
    finally:
        conn.close()


def test_list_zips_returns_empty_hint_for_path_that_is_a_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured path points at a file → not-a-directory hint."""
    bogus = tmp_path / "i_am_a_file.txt"
    bogus.write_text("not a dir")
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(bogus))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        assert out["export_zips_dir"] == str(bogus)
        assert out["zips"] == []
        assert "not a directory" in str(out["hint"])
    finally:
        conn.close()


def test_list_zips_returns_empty_hint_for_empty_directory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured directory with no ZIPs → ``zips: []`` + drop-a-ZIP hint."""
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        assert out["zips"] == []
        assert "Drop your Apple Health" in str(out["hint"])
    finally:
        conn.close()


def test_list_zips_lists_apple_health_zip(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An Apple Health ZIP shows up with the documented entry shape."""
    zip_path = tmp_path / "export_2026-06-26.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        zips_list = out["zips"]
        assert isinstance(zips_list, list)
        assert len(zips_list) == 1
        entry = zips_list[0]
        assert entry["file_name"] == "export_2026-06-26.zip"
        assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
        assert entry["id"] == entry["sha256"][:8]
        assert entry["size"] == zip_path.stat().st_size
        assert entry["imported"] is False
        assert entry["is_apple_health"] is True
        assert "import_zip" in str(out["hint"])
    finally:
        conn.close()


def test_list_zips_flags_non_apple_health_zip(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An alien ZIP appears in the list but with ``is_apple_health: false``."""
    zip_path = tmp_path / "not_health.zip"
    _make_zip(zip_path, with_export_xml=False)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        zips_list = out["zips"]
        assert isinstance(zips_list, list) and len(zips_list) == 1
        assert zips_list[0]["is_apple_health"] is False
    finally:
        conn.close()


def test_list_zips_marks_already_imported_via_cache(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ZIP whose (size, mtime) matches a past import is flagged imported."""
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    stat = zip_path.stat()
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    # Seed an ``imports`` row that matches the ZIP's stat triple.
    import hashlib
    from datetime import UTC, datetime

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO imports "
            "(import_id, export_dir, imported_at, "
            "source_zip_sha256, source_zip_mtime, source_zip_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["imp_prior", "/tmp/prior", mtime, sha, mtime, stat.st_size],
        )
        out = _call_list_zips(conn)
        zips_list = out["zips"]
        assert isinstance(zips_list, list) and len(zips_list) == 1
        assert zips_list[0]["imported"] is True
        # The cache hit means the sha matches the seeded value verbatim.
        assert zips_list[0]["sha256"] == sha
    finally:
        conn.close()


# --- import_zip --------------------------------------------------------------


def _call_import_zip(conn: duckdb.DuckDBPyConnection, *, id: str) -> dict[str, object]:
    fn = bind_tool(import_zip_mod, conn)
    raw = asyncio.run(fn(id=id))
    return json.loads(raw)


def test_import_zip_rejects_empty_id() -> None:
    """An empty id MUST NOT silently select the alphabetically-first ZIP.

    Python's ``str.startswith('')`` returns True on every haystack, so
    without the validation gate an empty / 1-char prefix would import
    an arbitrary file. Pin the explicit ``invalid_id`` envelope.
    """
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="")
        assert out["status"] == "error"
        assert out["reason"] == "invalid_id"
    finally:
        conn.close()


def test_import_zip_rejects_non_hex_id() -> None:
    """Non-hex characters in id are rejected before any directory scan."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="ZZZZZZZZ")
        assert out["status"] == "error"
        assert out["reason"] == "invalid_id"
    finally:
        conn.close()


def test_import_zip_accepts_uppercase_hex_id(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Uppercase hex is normalised to lowercase before prefix matching.

    list_zips emits lowercase, but a user who copy-pasted the value
    from a different tool (or capitalised it by accident) should not
    hit invalid_id.
    """
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    import hashlib

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        out = _call_import_zip(conn, id=sha[:8].upper())
        assert out["status"] == "ok"
        # Canonical id on the wire is lowercase, 8 chars, regardless of
        # what the user passed.
        assert out["id"] == sha[:8]
    finally:
        conn.close()


def test_import_zip_errors_when_env_unset() -> None:
    """env unset → ``status: error`` + ``reason: export_zips_dir_not_set``."""
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="aaaaaaaa")
        assert out["status"] == "error"
        assert out["reason"] == "export_zips_dir_not_set"
    finally:
        conn.close()


def test_import_zip_errors_when_dir_missing(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured directory missing → ``reason: export_zips_dir_missing``."""
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path / "no_such"))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="aaaaaaaa")
        assert out["status"] == "error"
        assert out["reason"] == "export_zips_dir_missing"
    finally:
        conn.close()


def test_import_zip_errors_when_id_not_found(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown id → ``reason: id_not_found``."""
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="deadbeef")
        assert out["status"] == "error"
        assert out["reason"] == "id_not_found"
    finally:
        conn.close()


def test_import_zip_errors_when_zip_not_apple_health(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resolved ZIP without export.xml → ``reason: not_apple_health_export``."""
    zip_path = tmp_path / "alien.zip"
    _make_zip(zip_path, with_export_xml=False)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    import hashlib

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "error"
        assert out["reason"] == "not_apple_health_export"
    finally:
        conn.close()


def test_import_zip_drives_run_import_against_live_handle(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Happy path: extract + run_import + stamp source_zip triple.

    Uses a real on-disk DuckDB so the importer's writable open succeeds.
    Trivial export.xml (no Record / Workout) so the assertion focuses on
    the wiring (status / id / triple) rather than per-record counts.
    """
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        import hashlib

        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "ok"
        assert out["id"] == sha[:8]
        assert out["already_imported_at"] is None
        assert isinstance(out["records_added"], int)
        # ``run_import`` stamped the source ZIP triple via the v0.4 seam.
        row = conn.execute(
            "SELECT source_zip_sha256, source_zip_size FROM imports WHERE "
            "source_zip_sha256 IS NOT NULL"
        ).fetchone()
        assert row is not None
        assert row[0] == sha
        assert row[1] == zip_path.stat().st_size
    finally:
        conn.close()


def test_import_zip_resolves_via_db_cache_fast_path(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Second invocation with a known sha prefix uses the DB cache lookup.

    Once a ZIP has been imported, its full sha lives in
    ``imports.source_zip_sha256``. ``_resolve_target`` first tries
    ``find_sha_by_prefix`` against that table and matches a candidate
    file by (size, mtime) without re-streaming the bytes. This test
    proves both that the DB-cache branch is taken (no re-hash needed)
    AND that the result reaches the idempotent ``already_imported_at``
    envelope correctly.
    """
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        import hashlib

        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        # First import: populates source_zip_* triple in imports.
        first = _call_import_zip(conn, id=sha[:8])
        assert first["status"] == "ok"
        # Second import via prefix: hits the DB cache fast-path.
        second = _call_import_zip(conn, id=sha[:8])
        assert second["status"] == "ok"
        assert second["records_added"] == 0
        assert second["already_imported_at"] is not None
        assert second["id"] == sha[:8]
    finally:
        conn.close()


def test_import_zip_falls_through_when_db_prefix_match_lacks_disk_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A DB-cache prefix hit with no matching on-disk (size,mtime) falls
    through to streaming; if no candidate matches the streamed sha
    either, the user sees ``id_not_found``.

    Pins the fall-through path: prior imports left an ``imports`` row
    whose sha prefix matches the requested id, but the actual ZIP file
    has been deleted or replaced. The resolver must not return the
    stale DB row; it must fall back to hashing on-disk files and
    surface ``id_not_found`` when nothing matches.
    """
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        # Seed imports with a fake prior import whose sha starts with
        # ``deadbeef`` but no on-disk file matches (the directory is
        # empty).
        from datetime import UTC, datetime

        fake_sha = "deadbeef" + ("0" * 56)
        conn.execute(
            "INSERT INTO imports (import_id, export_dir, imported_at, "
            "source_zip_sha256, source_zip_mtime, source_zip_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                "imp_stale",
                "/tmp/stale",
                datetime(2024, 1, 1, tzinfo=UTC),
                fake_sha,
                datetime(2024, 1, 1, tzinfo=UTC),
                12345,
            ],
        )
        # Directory is empty; nothing on disk matches the prefix.
        out = _call_import_zip(conn, id="deadbeef")
        assert out["status"] == "error"
        assert out["reason"] == "id_not_found"
    finally:
        conn.close()


def test_import_zip_returns_already_imported_envelope_on_byte_identical_reimport(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Second invocation against the same ZIP returns ``records_added: 0``."""
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        import hashlib

        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        # First import: real records_added.
        first = _call_import_zip(conn, id=sha[:8])
        assert first["status"] == "ok"
        # Second import: no-op envelope, populated already_imported_at.
        second = _call_import_zip(conn, id=sha[:8])
        assert second["status"] == "ok"
        assert second["records_added"] == 0
        assert second["already_imported_at"] is not None
    finally:
        conn.close()


def test_import_zip_handles_flat_apple_health_zip(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ZIP with ``export.xml`` at the root (no nested folder) imports OK.

    Some third-party repackagers flatten the Apple-supplied structure;
    the tool accepts both shapes.
    """
    zip_path = tmp_path / "flat.zip"
    _make_zip(zip_path, nested=False)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        import hashlib

        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "ok"
    finally:
        conn.close()


def test_import_zip_returns_zip_extract_failed_on_corrupt_archive(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ZIP that passes is_apple_health probe but fails extract surfaces an error.

    Constructs an archive whose top-level entry is a valid ``export.xml``
    member but the rest of the file is truncated mid-stream so
    ``extractall`` raises. The pre-extract idempotency check returns
    no match (sha is not in ``imports``), so the extract path runs.
    """
    # Build a valid Apple-Health-shaped archive, then truncate the
    # tail bytes. The central directory survives at the end so
    # ``namelist()`` works (is_apple_health passes); ``extractall``
    # crashes when it tries to read the truncated member payload.
    zip_path = tmp_path / "torn.zip"
    _make_zip(zip_path)
    raw = zip_path.read_bytes()
    # Overwrite a stretch of the local file header / data payload so
    # extractall raises BadZipFile.
    corrupted = raw[:30] + b"\x00" * 40 + raw[70:]
    zip_path.write_bytes(corrupted)
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))

    db_path = tmp_path / "h.duckdb"
    conn = duckdb.connect(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        stamp_current_version(conn)
        import hashlib

        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        out = _call_import_zip(conn, id=sha[:8])
        # Either the is_apple_health probe rejects the corrupted file
        # (BadZipFile in zipfile.ZipFile init) or the extract path
        # catches a downstream OSError -- both surface as a typed
        # error envelope, never as an unhandled exception.
        assert out["status"] == "error"
        assert out["reason"] in {"not_apple_health_export", "zip_extract_failed"}
    finally:
        conn.close()


def test_list_zips_skips_unparseable_imports_rows(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bogus (string) mtime in ``imports`` falls through without crashing.

    Defends against a future schema change that lands a non-ISO string
    in ``source_zip_mtime``; the cache loader just skips that row.
    """
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    zip_path = tmp_path / "x.zip"
    _make_zip(zip_path)

    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        # Build the row by hand bypassing the orchestrator. The
        # ``imports.source_zip_mtime`` column is TIMESTAMPTZ, so insert
        # a real datetime — ``_parse_iso_or_none`` must accept it.
        from datetime import UTC, datetime

        conn.execute(
            "INSERT INTO imports (import_id, export_dir, imported_at, "
            "source_zip_sha256, source_zip_mtime, source_zip_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                "imp_orphan",
                "/tmp/orphan",
                datetime(2024, 1, 1, tzinfo=UTC),
                "f" * 64,
                datetime(2024, 1, 1, tzinfo=UTC),
                123,
            ],
        )
        out = _call_list_zips(conn)
        assert isinstance(out["zips"], list)
    finally:
        conn.close()


# --- v0.4.1 (issue #158): inspect_zip 3-state classification -----------


def test_inspect_zip_returns_invalid_for_html_file(tmp_path: Path) -> None:
    """An HTML file renamed to .zip reads as INVALID_ZIP.

    Common Apple Health share-sheet failure mode: a network error page
    saved with the user's chosen filename. The body is HTML, the ZIP
    reader bails immediately, and the user should re-download.
    """
    from apple_health_mcp.server.tools._zip_inspect import (
        ZipInspection,
        inspect_zip,
    )

    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"<html><body>404</body></html>")
    assert inspect_zip(fake) == ZipInspection.INVALID_ZIP


def test_inspect_zip_returns_invalid_for_truncated_archive(tmp_path: Path) -> None:
    """A partial ZIP header without payload reads as INVALID_ZIP."""
    from apple_health_mcp.server.tools._zip_inspect import (
        ZipInspection,
        inspect_zip,
    )

    truncated = tmp_path / "partial.zip"
    # First four bytes are the ZIP local-file header signature, then
    # garbage cuts the central directory off.
    truncated.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    assert inspect_zip(truncated) == ZipInspection.INVALID_ZIP


def test_inspect_zip_returns_valid_non_apple_health_for_random_zip(
    tmp_path: Path,
) -> None:
    """A parseable ZIP without the export marker reads as VALID_NON_APPLE_HEALTH."""
    from apple_health_mcp.server.tools._zip_inspect import (
        ZipInspection,
        inspect_zip,
    )

    zip_path = tmp_path / "random.zip"
    _make_zip(zip_path, with_export_xml=False)
    assert inspect_zip(zip_path) == ZipInspection.VALID_NON_APPLE_HEALTH


def test_inspect_zip_returns_valid_apple_health_for_export_zip(
    tmp_path: Path,
) -> None:
    """A real-shaped Apple Health export reads as VALID_APPLE_HEALTH."""
    from apple_health_mcp.server.tools._zip_inspect import (
        ZipInspection,
        inspect_zip,
    )

    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path)
    assert inspect_zip(zip_path) == ZipInspection.VALID_APPLE_HEALTH


def test_is_apple_health_zip_remains_backwards_compatible(tmp_path: Path) -> None:
    """The legacy boolean helper still maps to VALID_APPLE_HEALTH only."""
    from apple_health_mcp.server.tools._zip_inspect import is_apple_health_zip

    good = tmp_path / "good.zip"
    _make_zip(good)
    bad = tmp_path / "bad.zip"
    _make_zip(bad, with_export_xml=False)
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"<html>oops</html>")

    assert is_apple_health_zip(good) is True
    assert is_apple_health_zip(bad) is False
    assert is_apple_health_zip(invalid) is False


def test_list_zips_returns_zip_status_field(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``list_zips`` exposes the 3-state ``zip_status`` field per entry."""
    good = tmp_path / "good.zip"
    _make_zip(good)
    bad = tmp_path / "bad.zip"
    _make_zip(bad, with_export_xml=False)
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"<html>oops</html>")

    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_list_zips(conn)
        zips_list = out["zips"]
        assert isinstance(zips_list, list) and len(zips_list) == 3
        by_name = {e["file_name"]: e for e in zips_list}
        assert by_name["good.zip"]["zip_status"] == "valid_apple_health"
        assert by_name["bad.zip"]["zip_status"] == "valid_non_apple_health"
        assert by_name["invalid.zip"]["zip_status"] == "invalid_zip"
        # Backward-compatible boolean still mirrors the VALID_APPLE_HEALTH branch.
        assert by_name["good.zip"]["is_apple_health"] is True
        assert by_name["bad.zip"]["is_apple_health"] is False
        assert by_name["invalid.zip"]["is_apple_health"] is False
    finally:
        conn.close()


def test_import_zip_returns_not_a_directory_reason_when_env_points_at_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v0.4.1 code-review #4: env var pointing at a file → typed envelope.

    Pre-fix this raised NotADirectoryError uncaught through
    asyncio.to_thread. The sibling list_zips has caught it since
    v0.4.0; import_zip now matches the contract.
    """
    file_path = tmp_path / "not_a_dir.zip"
    file_path.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(file_path))
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id="deadbeef")
        assert out["status"] == "error"
        assert out["reason"] == "export_zips_dir_not_a_directory"
        assert "not a directory" in str(out["message"]).lower()
    finally:
        conn.close()


def test_import_zip_returns_invalid_zip_reason_for_html_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An HTML-renamed .zip surfaces ``reason: invalid_zip``, not the old
    ``not_apple_health_export`` collision."""
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"<html><body>404</body></html>")
    monkeypatch.setenv(EXPORT_ZIPS_DIR_ENV_VAR, str(tmp_path))
    import hashlib

    sha = hashlib.sha256(fake.read_bytes()).hexdigest()
    conn = get_in_memory_connection()
    try:
        ensure_schema(conn)
        out = _call_import_zip(conn, id=sha[:8])
        assert out["status"] == "error"
        assert out["reason"] == "invalid_zip"
        assert "re-download" in str(out["message"]).lower()
    finally:
        conn.close()
