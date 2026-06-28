"""Tests for the typer CLI skeleton."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apple_health_mcp import cli

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "import" in result.stdout
    assert "serve" in result.stdout


_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _materialise_export_zip(tmp_path: Path, *, nested: bool = True) -> Path:
    """Build a ZIP at tmp_path/export.zip carrying the synthetic fixtures.

    v0.5 (issue #170): the CLI ``import`` subcommand accepts a ZIP path
    only -- the previously-used directory fixture is no longer wired
    into the entry point. ``nested=True`` mirrors Apple's
    ``apple_health_export/`` top-level shape; ``nested=False`` flattens
    the contents at the ZIP root so the importer's "either shape is
    accepted" code path stays covered.
    """
    zip_path = tmp_path / "export.zip"
    prefix = "apple_health_export/" if nested else ""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(_FIXTURES / "sample_export.xml", arcname=f"{prefix}export.xml")
        zf.write(
            _FIXTURES / "sample_ecg.csv",
            arcname=f"{prefix}electrocardiograms/sample_ecg.csv",
        )
        zf.write(
            _FIXTURES / "sample_workout_route.gpx",
            arcname=f"{prefix}workout-routes/sample_workout_route.gpx",
        )
    return zip_path


def test_import_happy_path(tmp_path: Path) -> None:
    zip_path = _materialise_export_zip(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 0, result.output
    assert db.exists()


def test_import_accepts_flattened_zip_shape(tmp_path: Path) -> None:
    """A ZIP whose contents are at the root (no apple_health_export/ prefix) is accepted."""
    zip_path = _materialise_export_zip(tmp_path, nested=False)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 0, result.output


def test_import_rejects_directory_argument(tmp_path: Path) -> None:
    """v0.5 (issue #170): the CLI no longer accepts a directory argument.

    A user upgrading from v0.4 who runs ``import <dir>`` gets a typed
    exit with a CHANGELOG pointer rather than the old behaviour of
    silently proceeding (and stamping a NULL ``source_zip_*`` triple).
    """
    a_dir = tmp_path / "apple_health_export"
    a_dir.mkdir()
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(a_dir)])
    assert result.exit_code == 1
    assert "directory" in result.output.lower()


def test_import_rejects_missing_path(tmp_path: Path) -> None:
    """A missing ZIP file surfaces as a clean exit-1 with a 'does not exist' note."""
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(tmp_path / "nope.zip")])
    assert result.exit_code == 1
    assert "does not exist" in result.output.lower()


def test_import_rejects_invalid_zip(tmp_path: Path) -> None:
    """A file that is not a valid ZIP (e.g. HTML renamed) surfaces as exit-1."""
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"<html><body>404</body></html>")
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(fake)])
    assert result.exit_code == 1
    assert "not a valid zip" in result.output.lower()


def test_import_surfaces_extract_race_as_typer_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.5 (PR #172 code-review #2): an extraction-phase failure
    surfaces as a clean exit-1 with a "failed to extract" message.

    ``zip_extract`` re-raises any OSError from ``extractall`` as
    BadZipFile so the two extraction-time failure modes share one
    recovery path. Importer-phase OSError bypasses this branch and
    falls through to the AppleHealthMCPError handler.
    """
    import zipfile as _zipfile

    def fake_extract(*args: object, **kwargs: object) -> object:
        raise _zipfile.BadZipFile("simulated extraction failure: file vanished mid-extract")

    monkeypatch.setattr(
        "apple_health_mcp.importers.zip_extract.extract_zip_and_import",
        fake_extract,
        raising=True,
    )
    zip_path = _materialise_export_zip(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 1
    assert "failed to extract" in result.output.lower()


def test_import_surfaces_health_error_as_typer_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline-level ``AppleHealthMCPError`` surfaces as a clean exit-1."""
    from apple_health_mcp.exceptions import DatabaseError

    def fake_extract(*args: object, **kwargs: object) -> object:
        raise DatabaseError("simulated DB write failure")

    monkeypatch.setattr(
        "apple_health_mcp.importers.zip_extract.extract_zip_and_import",
        fake_extract,
        raising=True,
    )
    zip_path = _materialise_export_zip(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 1


def test_import_rejects_valid_zip_without_apple_health_marker(tmp_path: Path) -> None:
    """A parseable ZIP that lacks export.xml at the top level surfaces as exit-1."""
    zip_path = tmp_path / "alien.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "not an apple health export")
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 1
    assert "does not contain" in result.output.lower()


def test_serve_defaults_to_stdio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_server(
        db_path: Path | None,
        transport: str,
        *,
        host: str,
        port: int,
    ) -> None:
        captured["transport"] = transport
        captured["host"] = host
        captured["port"] = port
        captured["db"] = db_path

    monkeypatch.setattr("apple_health_mcp.server.run_server", fake_run_server, raising=False)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "serve"])
    assert result.exit_code == 0, result.output
    assert captured["transport"] == "stdio"


def test_serve_accepts_http_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_server(
        db_path: Path | None,
        transport: str,
        *,
        host: str,
        port: int,
    ) -> None:
        captured["transport"] = transport
        captured["port"] = port

    monkeypatch.setattr("apple_health_mcp.server.run_server", fake_run_server, raising=False)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(
        cli.app,
        ["--db", str(db), "serve", "--transport", "http", "--port", "9090"],
    )
    assert result.exit_code == 0, result.output
    assert captured["transport"] == "http"
    assert captured["port"] == 9090


def test_serve_rejects_unknown_transport() -> None:
    result = runner.invoke(cli.app, ["serve", "--transport", "bogus"])
    assert result.exit_code != 0


def test_serve_surfaces_apple_health_error_as_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: serve must convert AppleHealthMCPError into a typer exit
    rather than letting asyncio.run propagate a raw traceback (the failure
    mode on a fresh install where ``import`` was never run)."""
    from apple_health_mcp.exceptions import DatabaseError

    async def boom(
        db_path: Path | None,
        transport: str,
        *,
        host: str,
        port: int,
    ) -> None:
        raise DatabaseError("simulated missing DB")

    monkeypatch.setattr("apple_health_mcp.server.run_server", boom, raising=False)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "serve"])
    assert result.exit_code == 1, result.output


def test_main_entry_point_invokes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["apple-health-mcp-server", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0


def test_tz_flag_promotes_to_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--tz`` populates APPLE_HEALTH_TZ so the connection layer picks it up."""
    import os as _os

    monkeypatch.delenv("APPLE_HEALTH_TZ", raising=False)
    zip_path = _materialise_export_zip(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(
        cli.app,
        ["--db", str(db), "--tz", "Asia/Tokyo", "import", str(zip_path)],
    )
    assert result.exit_code == 0, result.output
    assert _os.environ.get("APPLE_HEALTH_TZ") == "Asia/Tokyo"


def test_db_flag_promotes_to_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--db`` populates APPLE_HEALTH_DB so resolve_db_path() picks it up.

    Mirrors the ``--tz`` -> APPLE_HEALTH_TZ pattern. Without this
    promotion a future caller that resolves through resolve_db_path()
    (a new subcommand, a plugin, a diagnostic helper like
    get_server_info) would silently ignore the ``--db`` the user
    typed on the CLI, because the env-only resolver would never see
    it. The resolver's "single source of truth" docstring would then
    be a lie.
    """
    import os as _os

    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    zip_path = _materialise_export_zip(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(zip_path)])
    assert result.exit_code == 0, result.output
    # The promotion stores an absolute path so resolve_db_path() does
    # not later reject it as "relative" — the CWD-stable invariant the
    # env-resolver enforces must match what the CLI promotes.
    assert _os.environ.get("APPLE_HEALTH_DB") == str(db.expanduser().resolve())


def test_zip_extract_translates_oserror_to_badzipfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.5 (PR #172 code-review #1): ``zip_extract`` re-raises any OSError
    from ``extractall`` as ``BadZipFile`` so the caller's narrow
    extract-phase handler treats it uniformly with corruption failures.
    Importer-phase OSError stays unwrapped (covered by other tests).
    """
    import zipfile

    from apple_health_mcp.importers.zip_extract import extract_zip_and_import

    zip_path = _materialise_export_zip(tmp_path)

    def fake_extractall(self: zipfile.ZipFile, path: object) -> None:
        raise OSError("simulated ENOSPC during extraction")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", fake_extractall)
    from datetime import UTC, datetime

    stat = zip_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    with pytest.raises(zipfile.BadZipFile, match="extraction failed"):
        extract_zip_and_import(
            zip_path,
            ("00" * 32, mtime, stat.st_size),
            db_path=tmp_path / "h.duckdb",
        )
