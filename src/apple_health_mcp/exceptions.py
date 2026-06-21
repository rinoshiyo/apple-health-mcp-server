"""Exception hierarchy for the Apple Health MCP server."""

from __future__ import annotations


class AppleHealthMCPError(Exception):
    """Base class for all Apple Health MCP errors."""


class ImportError(AppleHealthMCPError):
    """Raised when an Apple Health export cannot be imported."""


class ValidationError(AppleHealthMCPError):
    """Raised when input or parsed data fails validation."""


class DatabaseError(AppleHealthMCPError):
    """Raised when a DuckDB operation fails."""


class ConfigError(AppleHealthMCPError):
    """Raised when configuration or environment is invalid."""
