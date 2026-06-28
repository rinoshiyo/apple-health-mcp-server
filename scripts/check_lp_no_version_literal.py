#!/usr/bin/env python3
"""Fail if LP copy embeds an explicit version literal outside the footer slot.

The 2026-06-25 grill picked option (c) — every LP version pointer renders
through ``/releases/latest`` so the maintainer never has to touch
``docs/i18n/*.json`` for a release. The only sanctioned slot for a hard-coded
``vN.M.K`` string is ``footer.version``, which the ``sync_docs_version`` job
in release.yml rewrites on every stable tag. Anywhere else, a literal is a
ticking time-bomb that goes stale at the next release.

This script walks both locale JSONs and flags any other string that matches
``vN.M.K`` (semver-shaped). CI runs it on every PR.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_VERSION_LITERAL = re.compile(r"v\d+\.\d+\.\d+")
_ALLOWED_PATH = ("footer", "version")
_LOCALES = ("docs/i18n/ja.json", "docs/i18n/en.json")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _walk(node: object, breadcrumb: tuple[str, ...]) -> list[tuple[tuple[str, ...], str]]:
    """Yield (path, value) for every string leaf in ``node``."""
    out: list[tuple[tuple[str, ...], str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.extend(_walk(value, (*breadcrumb, str(key))))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.extend(_walk(value, (*breadcrumb, str(index))))
    elif isinstance(node, str):
        out.append((breadcrumb, node))
    return out


def main() -> int:
    root = _project_root()
    violations: list[str] = []

    for rel_path in _LOCALES:
        path = root / rel_path
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for breadcrumb, value in _walk(data, ()):
            if breadcrumb == _ALLOWED_PATH:
                continue
            if _VERSION_LITERAL.search(value):
                violations.append(f"{rel_path}: {'.'.join(breadcrumb)} = {value!r}")

    if violations:
        print(
            "LP copy contains a vN.M.K literal outside the sanctioned footer.version slot:",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nUse a /releases/latest link for download URLs and rely on "
            "release.yml's sync_docs_version job for footer.version. "
            "See CLAUDE.md §9 (LP Copy Conventions).",
            file=sys.stderr,
        )
        return 1

    print(f"LP version-literal scan OK across {len(_LOCALES)} locales.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
