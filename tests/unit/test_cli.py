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


def test_import_stub_exits_zero(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["import", str(tmp_path)])
    assert result.exit_code == 0


def test_import_stub_accepts_global_db(tmp_path: Path) -> None:
    db = tmp_path / "health.duckdb"
    result = runner.invoke(cli.app, ["--db", str(db), "import", str(tmp_path)])
    assert result.exit_code == 0


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
    monkeypatch.setattr("sys.argv", ["apple-health-mcp", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
