"""Tests for the shared TZ-suffix normaliser (issue #56 fast path + fallback)."""

from __future__ import annotations

import pytest

from apple_health_mcp.importers._tz import (
    normalize_apple_offset,
    normalize_apple_offset_opt,
)

# --- fast path: "space + +HHMM" (5-char numeric tail) ----------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-01 10:00:00 +0900", "2024-01-01 10:00:00+09:00"),
        ("2024-01-01 10:00:00 -0500", "2024-01-01 10:00:00-05:00"),
        ("2024-12-31 23:59:59 +0000", "2024-12-31 23:59:59+00:00"),
    ],
)
def test_normalize_apple_offset_fast_path_hhmm(raw: str, expected: str) -> None:
    """The +HHMM / -HHMM shape Apple emits hits the fast path verbatim."""
    assert normalize_apple_offset(raw) == expected


# --- fast path: "space + +HH:MM" (6-char colon tail) -----------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-01 10:00:00 +09:00", "2024-01-01 10:00:00+09:00"),
        ("2024-01-01 10:00:00 -05:30", "2024-01-01 10:00:00-05:30"),
    ],
)
def test_normalize_apple_offset_fast_path_hh_colon_mm(raw: str, expected: str) -> None:
    """An already colon-bearing offset still drops the leading space via the fast path."""
    assert normalize_apple_offset(raw) == expected


# --- fast path rejected: tail digits malformed -----------------------------


def test_normalize_apple_offset_falls_back_when_hhmm_digits_invalid() -> None:
    """A non-digit in the +HHMM position falls through to the regex fallback.

    The regex's ``\\d{2}`` anchors only digits, so a bogus tail returns the
    original string unchanged. The behaviour-equivalence check is what
    matters: the fast path must not invent a successful match the regex
    would have rejected.
    """
    raw = "2024-01-01 10:00:00 +09AB"
    # Regex fallback returns the input unchanged (no match on the digit run).
    assert normalize_apple_offset(raw) == raw


def test_normalize_apple_offset_falls_back_when_hh_colon_mm_digits_invalid() -> None:
    """A colon-bearing tail with non-digit minutes falls through to the regex."""
    raw = "2024-01-01 10:00:00 +09:AB"
    assert normalize_apple_offset(raw) == raw


# --- fallback path: no leading space ---------------------------------------


def test_normalize_apple_offset_fallback_handles_no_leading_space() -> None:
    """The regex fallback still normalises an offset attached without a space."""
    assert normalize_apple_offset("2024-01-01 10:00:00+0900") == "2024-01-01 10:00:00+09:00"


def test_normalize_apple_offset_fallback_handles_trailing_whitespace() -> None:
    """``\\s*$`` in the regex absorbs trailing whitespace the fast path skips."""
    assert normalize_apple_offset("2024-01-01 10:00:00 +0900   ") == "2024-01-01 10:00:00+09:00"


def test_normalize_apple_offset_falls_back_on_multi_leading_whitespace_hhmm() -> None:
    """Two spaces before a +HHMM offset must fall through to the regex.

    The fast path only collapses a single leading space; without the
    guard it would leave surplus whitespace in the output and diverge
    from the regex's ``\\s*`` semantics. The regex strips every
    leading whitespace character.
    """
    assert normalize_apple_offset("2024-01-01 10:00:00  +0900") == "2024-01-01 10:00:00+09:00"


def test_normalize_apple_offset_falls_back_on_multi_leading_whitespace_hh_colon_mm() -> None:
    """Two spaces before a +HH:MM offset must also fall through to the regex."""
    assert normalize_apple_offset("2024-01-01 10:00:00  +09:00") == "2024-01-01 10:00:00+09:00"


def test_normalize_apple_offset_falls_back_on_tab_leading_whitespace() -> None:
    """A tab before the offset is whitespace too; the regex's ``\\s`` absorbs it."""
    assert normalize_apple_offset("2024-01-01 10:00:00\t+0900") == "2024-01-01 10:00:00+09:00"


def test_normalize_apple_offset_fast_path_at_minimum_lengths() -> None:
    """The ``n == 7`` / ``n == 6`` short-circuit lets the fast path fire on
    bare offsets (`" +HH:MM"` / `" +HHMM"`) without checking the would-be
    out-of-range ``raw[-8]`` / ``raw[-7]`` position. These inputs are
    pathological in production but the branches must stay covered.
    """
    assert normalize_apple_offset(" +09:00") == "+09:00"
    assert normalize_apple_offset(" +0900") == "+09:00"


def test_normalize_apple_offset_empty_string_passes_through() -> None:
    """Empty input returns empty (both fast-path checks short-circuit on length)."""
    assert normalize_apple_offset("") == ""


def test_normalize_apple_offset_naive_timestamp_passes_through() -> None:
    """A naive (offset-less) timestamp returns unchanged via the regex no-match path."""
    assert normalize_apple_offset("2024-01-01 10:00:00") == "2024-01-01 10:00:00"


# --- Optional wrapper -------------------------------------------------------


def test_normalize_apple_offset_opt_passes_none_through() -> None:
    assert normalize_apple_offset_opt(None) is None


def test_normalize_apple_offset_opt_normalises_strings() -> None:
    assert normalize_apple_offset_opt("2024-01-01 10:00:00 +0900") == "2024-01-01 10:00:00+09:00"
