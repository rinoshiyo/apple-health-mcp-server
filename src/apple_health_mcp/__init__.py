"""Apple Health MCP server package."""

from __future__ import annotations

__version__ = "0.1.0"

# Single source of truth for any user-facing link that points at this
# project. A future repo rename or org migration only needs one edit
# here instead of grep-and-replace across the codebase.
REPO_URL = "https://github.com/rinoshiyo/apple-health-mcp-server"
ISSUES_URL = f"{REPO_URL}/issues"

__all__ = ["ISSUES_URL", "REPO_URL", "__version__"]
