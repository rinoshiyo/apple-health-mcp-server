"""Apple Health timezone-suffix normalization shared by the XML and ECG importers.

Apple emits date attributes as ``"YYYY-MM-DD HH:MM:SS +HHMM"`` (XML)
and the same shape inside ECG CSV headers. DuckDB's ``TIMESTAMPTZ``
parser only accepts ISO 8601's colon-bearing ``+HH:MM`` form and treats
``" +HHMM"`` as an unknown timezone *name*. This module owns the one
regex that reshapes the trailing offset so both importers feed the same
canonical form to DuckDB and a JST-tagged row from XML and the same JST
ECG row both round-trip to the identical UTC instant.
"""

from __future__ import annotations

import re

# Collapses any trailing UTC offset of the form ``" +HHMM"``, ``"+HHMM"``,
# ``" +HH:MM"``, or ``"+HH:MM"`` (with optional trailing whitespace) into
# the colon-bearing, space-less ``"+HH:MM"`` ISO 8601 form. ``re.ASCII``
# pins ``\d`` to plain ASCII so a third-party HealthKit producer cannot
# slip full-width / Arabic-Indic digits past validation and trigger a
# downstream DuckDB ConversionException with an opaque message.
_OFFSET_TAIL_RE = re.compile(r"\s*([+-])(\d{2}):?(\d{2})\s*$", re.ASCII)


def normalize_apple_offset(raw: str) -> str:
    """Return ``raw`` with any Apple-style trailing TZ suffix normalised.

    Strings without a recognised offset suffix (naive timestamps Apple
    occasionally emits for legacy fields) pass through unchanged. Note
    that DuckDB then interprets the naive form under the session TZ
    *at insert time* — the resulting UTC instant is baked in, not
    deferred to read time. Operators importing rare naive-timestamp
    fields under a non-UTC session TZ should be aware that a later
    re-import under a different TZ would store a different instant.
    """
    # Hot-path fast path (issue #56): py-spy attributed ~15% of Phase 1
    # to the regex below on a 1.2 GB export (8M+ calls). The overwhelming
    # majority of Apple-emitted timestamps fit one of two fixed shapes at
    # the tail: " +HHMM" (space + 5 chars) or " +HH:MM" (space + 6 chars).
    # Detecting them via index lookups skips the regex engine entirely
    # for the common case and falls back to ``_OFFSET_TAIL_RE`` for
    # anything else so the original behaviour (trailing whitespace,
    # offset without leading space, etc.) stays intact.
    n = len(raw)
    if n >= 7 and raw[-7] == " " and raw[-6] in "+-" and raw[-3] == ":":
        tail = raw[-6:]
        if tail[1:3].isdigit() and tail[4:6].isdigit():
            return raw[:-7] + tail
    if n >= 6 and raw[-6] == " " and raw[-5] in "+-":
        tail = raw[-5:]
        if tail[1:3].isdigit() and tail[3:5].isdigit():
            return raw[:-6] + tail[:3] + ":" + tail[3:]
    return _OFFSET_TAIL_RE.sub(r"\1\2:\3", raw)


def normalize_apple_offset_opt(raw: str | None) -> str | None:
    """``None``-tolerant wrapper around :func:`normalize_apple_offset`."""
    if raw is None:
        return None
    return normalize_apple_offset(raw)
