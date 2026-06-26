"""Suite-wide pytest fixtures.

v0.4 (issue #148): the autouse env-clear fixture lives at the suite
root so EVERY test (unit, integration, and any future top-level
suite) inherits a clean ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` baseline.
Without this, a developer (or CI shell) with the env var exported
would flip ``check_data_state``'s empty-DB tests from ``NEEDS_CONFIG``
(the documented default for a fresh install) to ``NEEDS_IMPORT``, and
the assertions pinning the structured error payload would fail in a
way that reads as a real regression but is actually env contamination.

The smoke test used to monkeypatch the var manually; with this root
fixture in place that duplicated call becomes a no-op.

Tests that need the ``NEEDS_IMPORT`` branch monkeypatch the var back
in explicitly so the choice is visible at the call site.
"""

from __future__ import annotations

import pytest

from apple_health_mcp.server.data_state import EXPORT_ZIPS_DIR_ENV_VAR


@pytest.fixture(autouse=True)
def _clear_export_zips_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXPORT_ZIPS_DIR_ENV_VAR, raising=False)
