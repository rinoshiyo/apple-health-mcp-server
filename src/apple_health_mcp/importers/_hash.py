"""Deterministic SHA-256 hashing for importer row keys.

Matches the Rust reference implementation in ``src/models.rs::compute_hash``:
SHA-256 over each part's UTF-8 bytes followed by a literal ``|`` byte, then
hex-encoded. The trailing separator after the last part is intentional and
preserved so the Python and Rust hashes are byte-for-byte identical.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

_SEP = b"|"


def compute_hash(parts: Iterable[str]) -> str:
    """Return the lowercase hex SHA-256 of ``parts`` joined by ``|`` bytes.

    Each part is followed by a single ``|`` byte (including the last), which
    keeps the digest distinct between ``["", ""]`` and ``[""]``. The Rust
    reference implementation has the same trailing-separator behavior; do not
    change this without coordinating with that code.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(_SEP)
    return hasher.hexdigest()
