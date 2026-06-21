"""Tests for importers._hash."""

from __future__ import annotations

import hashlib

from apple_health_mcp.importers._hash import compute_hash


def test_hash_is_deterministic() -> None:
    assert compute_hash(["a", "b", "c"]) == compute_hash(["a", "b", "c"])


def test_hash_is_order_sensitive() -> None:
    assert compute_hash(["a", "b"]) != compute_hash(["b", "a"])


def test_hash_empty_input_is_well_defined() -> None:
    # Empty input hashes to SHA-256 of the empty string -- the loop body
    # never runs and no separators are emitted.
    assert compute_hash([]) == hashlib.sha256(b"").hexdigest()


def test_hash_distinguishes_empty_vs_double_empty() -> None:
    assert compute_hash([""]) != compute_hash(["", ""])


def test_hash_matches_reference_layout() -> None:
    # SHA-256("a|b|") computed independently of the implementation so a
    # silent change to the separator scheme breaks loudly.
    expected = hashlib.sha256(b"a|b|").hexdigest()
    assert compute_hash(["a", "b"]) == expected


def test_hash_output_is_64_hex_chars() -> None:
    digest = compute_hash(["x"])
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
