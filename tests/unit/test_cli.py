"""Tests for the typer CLI skeleton."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from apple_health_mcp import cli

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "import" in result.stdout
    assert "serve" in result.stdout


def test_import_stub_exits_zero(tmp_path: object) -> None:
    result = runner.invoke(cli.app, ["import", str(tmp_path)])
    assert result.exit_code == 0


def test_serve_stub_defaults_to_stdio() -> None:
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0


def test_serve_stub_accepts_http_transport() -> None:
    result = runner.invoke(cli.app, ["serve", "--transport", "http", "--port", "9090"])
    assert result.exit_code == 0


def test_main_entry_point_invokes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["apple-health-mcp", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
