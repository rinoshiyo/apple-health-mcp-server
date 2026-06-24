"""Apple Health MCP server package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Source of truth is the installed distribution's metadata
    # (resolved from pyproject's [project].version at build time).
    # Tying __version__ to that prevents the three-way drift we hit
    # between pyproject + manifest.json + this literal that survived
    # through 0.1.1 → 0.1.6.
    __version__ = _pkg_version("apple-health-mcp-server")
except PackageNotFoundError:  # pragma: no cover - editable / source-tree fallback
    __version__ = "0.0.0+unknown"

# Single source of truth for any user-facing link that points at this
# project. A future repo rename or org migration only needs one edit
# here instead of grep-and-replace across the codebase.
REPO_URL = "https://github.com/rinoshiyo/apple-health-mcp-server"
ISSUES_URL = f"{REPO_URL}/issues"

__all__ = ["ISSUES_URL", "REPO_URL", "__version__"]
