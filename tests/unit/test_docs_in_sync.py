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
  filename, the README mentions need to follow. The test derives the
  expected path suffix from ``_APP_DIR_NAME`` and ``_DB_FILE_NAME`` so a
  rename of either constant fails the test in one place (here) rather
  than dropping silent stale prose into both READMEs.
- Anchor SoT: ``database-location-ja`` is the JA README's only stable
  fragment for cross-refs (the auto-generated heading slug uses the
  Japanese title, which is awkward to link from English-language
  copy). A future contributor deleting the explicit anchor would
  break every existing cross-ref; the test asserts it stays.

The tests are environment-sensitive: ``default_db_path()`` is asserted
only on POSIX hosts (Linux + macOS share the XDG branch). Windows skip
to avoid bringing the platform's ``LOCALAPPDATA`` rules into the doc-
parity assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apple_health_mcp.db.connection import (
    _APP_DIR_NAME,
    _DB_FILE_NAME,
    default_db_path,
)
from apple_health_mcp.importers.xml import (
    _PROGRESS_INTERVAL_DEFAULT_SECS,
    _PROGRESS_INTERVAL_MAX_SECS,
    _PROGRESS_INTERVAL_MIN_SECS,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README_PATHS = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "README.ja.md",
)


@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_progress_interval_clamp_range_matches_xml_constants(readme: Path) -> None:
    """Issue #122: clamp bounds quoted as ``MIN..MAX`` in both READMEs."""
    text = readme.read_text(encoding="utf-8")
    expected = f"{_PROGRESS_INTERVAL_MIN_SECS}..{_PROGRESS_INTERVAL_MAX_SECS}"
    assert expected in text, (
        f"{readme.name} lost the clamp range {expected!r}; widen the "
        f"clamp in xml.py and the README in the same PR (issue #122)."
    )


@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_progress_interval_default_matches_xml_constant(readme: Path) -> None:
    """Issue #122: env-vars table cell ``| `10` |`` tracks the default."""
    text = readme.read_text(encoding="utf-8")
    expected_cell = f"| `{_PROGRESS_INTERVAL_DEFAULT_SECS}` |"
    assert expected_cell in text, (
        f"{readme.name} env-vars table no longer shows default "
        f"{_PROGRESS_INTERVAL_DEFAULT_SECS!r}; the cell shape "
        f"{expected_cell!r} must appear verbatim (issue #122)."
    )


def test_default_db_path_resolves_to_xdg_default_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #121: ``default_db_path()`` falls through to the XDG default.

    Asserts the runtime resolution in isolation from any README; a
    failure here points at ``_APP_DIR_NAME`` / ``_DB_FILE_NAME`` /
    ``_platform_default_dir`` rather than at the docs.
    """
    if sys.platform == "win32":
        pytest.skip("Windows resolves under %LOCALAPPDATA%, exercised separately")
    monkeypatch.delenv("APPLE_HEALTH_DB", raising=False)
    monkeypatch.delenv("APPLE_HEALTH_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    resolved = default_db_path()
    expected_suffix = f"/.local/share/{_APP_DIR_NAME}/{_DB_FILE_NAME}"
    assert str(resolved).endswith(expected_suffix), (
        f"default_db_path() resolved to {resolved}; expected suffix "
        f"{expected_suffix} (derived from _APP_DIR_NAME / _DB_FILE_NAME)."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows resolves under %LOCALAPPDATA%, asserted separately if needed",
)
@pytest.mark.parametrize("readme", _README_PATHS, ids=lambda p: p.name)
def test_readme_quotes_xdg_default_db_path(readme: Path) -> None:
    """Issue #121: README mentions the canonical POSIX default."""
    text = readme.read_text(encoding="utf-8")
    expected_doc = f"~/.local/share/{_APP_DIR_NAME}/{_DB_FILE_NAME}"
    assert expected_doc in text, (
        f"{readme.name} no longer mentions {expected_doc!r}; § Database "
        f"location is the SoT (issue #121) and other mentions link back to it."
    )


def test_ja_readme_database_location_anchor_present() -> None:
    """Issue #121: ``database-location-ja`` anchor is the JA cross-ref SoT.

    The JA README's H3 ``データベースの場所`` auto-generates a slug
    containing Japanese characters that is awkward for English-language
    cross-refs and inconsistent across GitHub vs. local Markdown
    renderers. The explicit anchor is the stable fragment every other
    section links to; deleting it would silently 404 every cross-ref.
    """
    text = (_REPO_ROOT / "README.ja.md").read_text(encoding="utf-8")
    assert '<a id="database-location-ja"></a>' in text, (
        "README.ja.md lost the `database-location-ja` anchor; existing "
        "cross-refs depend on it (issue #121). Re-add the anchor "
        "immediately before `### データベースの場所`."
    )
