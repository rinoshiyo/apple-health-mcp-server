"""Tests for the exception hierarchy."""

from __future__ import annotations

import pytest

from apple_health_mcp.exceptions import (
    AppleHealthMCPError,
    ConfigError,
    DatabaseError,
    HealthImportError,
    ValidationError,
)


@pytest.mark.parametrize(
    "exc_cls",
    [HealthImportError, ValidationError, DatabaseError, ConfigError],
)
def test_subclasses_inherit_from_base(exc_cls: type[AppleHealthMCPError]) -> None:
    assert issubclass(exc_cls, AppleHealthMCPError)
    with pytest.raises(AppleHealthMCPError):
        raise exc_cls("boom")
