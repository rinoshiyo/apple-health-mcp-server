#!/usr/bin/env python3
"""Fail if pyproject.toml, manifest.json, and uv.lock disagree on the project version.

CI invokes this on every PR so a one-file bump (the v0.4.x cycle had to bump
three files by hand) surfaces at PR time instead of on the v* tag push, where
the only recourse is a hot-fix tag.

PEP 440 normalises ``0.5.0-rc1`` and ``0.5.0rc1`` to the same release, so the
three files are allowed to spell pre-releases with or without the dash; the
comparison runs through ``packaging.version.Version``.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_pyproject_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def _read_manifest_version(root: Path) -> str:
    with (root / "manifest.json").open("r", encoding="utf-8") as f:
        return str(json.load(f)["version"])


def _read_uv_lock_version(root: Path, package: str) -> str:
    """Walk uv.lock until the [[package]] table for ``package`` is found.

    uv.lock is TOML but the per-package ``version`` lines have no header
    prefix once the parser is inside a ``[[package]]`` array element, so use
    tomllib for the full document and look the package up by name.
    """
    with (root / "uv.lock").open("rb") as f:
        data = tomllib.load(f)
    for entry in data.get("package", []):
        if entry.get("name") == package:
            return str(entry["version"])
    raise SystemExit(f"uv.lock: no [[package]] entry for {package!r}")


def _normalise(value: str, source: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise SystemExit(f"{source}: {value!r} is not a PEP 440 version: {exc}") from exc


def main() -> int:
    root = _project_root()
    pyproject_raw = _read_pyproject_version(root)
    manifest_raw = _read_manifest_version(root)
    uv_lock_raw = _read_uv_lock_version(root, "apple-health-mcp-server")

    pyproject_v = _normalise(pyproject_raw, "pyproject.toml [project].version")
    manifest_v = _normalise(manifest_raw, "manifest.json version")
    uv_lock_v = _normalise(uv_lock_raw, "uv.lock [[package]] apple-health-mcp-server.version")

    mismatches = []
    if manifest_v != pyproject_v:
        mismatches.append(f"manifest.json version {manifest_raw!r} != pyproject {pyproject_raw!r}")
    if uv_lock_v != pyproject_v:
        mismatches.append(
            f"uv.lock apple-health-mcp-server.version {uv_lock_raw!r} != "
            f"pyproject {pyproject_raw!r}"
        )

    if mismatches:
        print("Version drift detected:", file=sys.stderr)
        for line in mismatches:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nBump pyproject.toml [project].version, manifest.json version, and re-run "
            "`uv lock` so uv.lock picks up the new project version.",
            file=sys.stderr,
        )
        return 1

    print(f"Version parity OK: pyproject == manifest == uv.lock == {pyproject_raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
