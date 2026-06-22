"""Tests for the typer CLI skeleton."""

from __future__ import annotations

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


def _materialise_export(tmp_path: Path) -> Path:
    """Lay out the synthetic fixtures into the directory shape run_import expects."""
    export_dir = tmp_path / "apple_health_export"
    export_dir.mkdir()
    (export_dir / "export.xml").write_bytes((_FIXTURES / "sample_export.xml").read_bytes())
    electro = export_dir / "electrocardiograms"
    electro.mkdir()
    (electro / "sample_ecg.csv").write_bytes((_FIXTURES / "sample_ecg.csv").read_bytes())
    routes = export_dir / "workout-routes"
    routes.mkdir()
    (routes / "sample_workout_route.gpx").write_bytes(
        (_FIXTURES / "sample_workout_route.gpx").read_bytes()
    )
    return export_dir


def test_import_happy_path(tmp_path: Path) -> None:
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(export_dir)])
    assert result.exit_code == 0, result.output
    assert db.exists()


def test_import_surfaces_health_error_as_typer_exit(tmp_path: Path) -> None:
    """A missing export.xml must surface as a clean exit-1, not a traceback."""
    empty_export = tmp_path / "apple_health_export"
    empty_export.mkdir()
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(empty_export)])
    assert result.exit_code == 1


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
    export_dir = _materialise_export(tmp_path)
    db = tmp_path / "health.duckdb"
    result = runner.invoke(
        cli.app,
        ["--db", str(db), "--tz", "Asia/Tokyo", "import", str(export_dir)],
    )
    assert result.exit_code == 0, result.output
    assert _os.environ.get("APPLE_HEALTH_TZ") == "Asia/Tokyo"
