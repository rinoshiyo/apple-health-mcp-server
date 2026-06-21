"""Exception hierarchy for the Apple Health MCP server."""

from __future__ import annotations


class AppleHealthMCPError(Exception):
    """Base class for all Apple Health MCP errors."""


class HealthImportError(AppleHealthMCPError):
    """Raised when an Apple Health export cannot be imported.

    Named ``HealthImportError`` (not ``ImportError``) to avoid shadowing the
    builtin and breaking ``try / except ImportError`` for optional deps.
    """


class ValidationError(AppleHealthMCPError):
    """Raised when input or parsed data fails validation."""


class DatabaseError(AppleHealthMCPError):
    """Raised when a DuckDB operation fails."""


class ConfigError(AppleHealthMCPError):
    """Raised when configuration or environment is invalid."""
