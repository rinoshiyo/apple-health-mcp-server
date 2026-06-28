"""Doc-tests pinning README prose to the runtime constants it describes.

These guards exist to catch silent drift between code and docs:

- Env-var constants (issue #122): ``_PROGRESS_INTERVAL_DEFAULT_SECS`` and
  the ``MIN/MAX`` clamp bounds live in ``apple_health_mcp.importers.xml``;
  the README quotes them inline. A widen-the-clamp PR that touches only
  the source would otherwise leave both README locales claiming the old
  numbers.
- DB path SoT (issue #121): the Linux/macOS default DuckDB path string
  appears in both READMEs and the connection module docstring. If
  ``default_db_path()`` ever changes the package's app-subdir name or
  filename, the README mentions need to follow.

The tests are environment-sensitive: ``default_db_path()`` is asserted
only on POSIX hosts (Linux + macOS share the XDG branch). Windows skip
to avoid bringing the platform's ``LOCALAPPDATA`` rules into the doc-
parity assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apple_health_mcp.db.connection import default_db_path
from apple_health_mcp.importers.xml import (
    _PROGRESS_INTERVAL_DEFAULT_SECS,
    _PROGRESS_INTERVAL_MAX_SECS,
    _PROGRESS_INTERVAL_MIN_SECS,
)

_README_PATHS = (
    Path(__file__).resolve().parents[2] / "README.md",
    Path(__file__).resolve().parents[2] / "README.ja.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_progress_interval_clamp_range_matches_xml_constants(readme: Path) -> None:
    """Issue #122: clamp bounds quoted as ``MIN..MAX`` in both READMEs."""
    text = _read(readme)
    expected = f"{_PROGRESS_INTERVAL_MIN_SECS}..{_PROGRESS_INTERVAL_MAX_SECS}"
    assert expected in text, (
        f"{readme.name} lost the clamp range {expected!r}; widen the "
        f"clamp in xml.py and the README in the same PR (issue #122)."
    )


@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_progress_interval_default_matches_xml_constant(readme: Path) -> None:
    """Issue #122: env-vars table cell ``| `10` |`` tracks the default."""
    text = _read(readme)
    expected_cell = f"| `{_PROGRESS_INTERVAL_DEFAULT_SECS}` |"
    assert expected_cell in text, (
        f"{readme.name} env-vars table no longer shows default "
        f"{_PROGRESS_INTERVAL_DEFAULT_SECS!r}; the cell shape "
        f"{expected_cell!r} must appear verbatim (issue #122)."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows resolves under %LOCALAPPDATA%, asserted separately if needed",
)
@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_default_db_path_matches_readme_linux_string(
    readme: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #121: README Linux/macOS path tracks ``default_db_path()``.

    Force the XDG-default branch by clearing ``APPLE_HEALTH_DB`` /
    ``APPLE_HEALTH_DATA_DIR`` / ``XDG_DATA_HOME`` so the resolution
    falls through to ``~/.local/share/apple-health-mcp/health.duckdb``
    regardless of the test host's actual env.
    """
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    resolved = default_db_path()
    expected_suffix = "/.local/share/apple-health-mcp/health.duckdb"
    assert str(resolved).endswith(expected_suffix), (
        f"default_db_path() resolved to {resolved}; expected to end with "
        f"{expected_suffix} after clearing all override env vars."
    )
    text = _read(readme)
    expected_doc = "~/.local/share/apple-health-mcp/health.duckdb"
    assert expected_doc in text, (
        f"{readme.name} no longer mentions {expected_doc!r}; the README's "
        f"§ Database location is the SoT (issue #121) and other mentions "
        f"link back to it."
    )
