#!/usr/bin/env python3
"""Fail if pyproject.toml, manifest.json, and uv.lock disagree on the project version.

CI invokes this on every PR so a one-file bump (the v0.4.x cycle had to bump
three files by hand) surfaces at PR time instead of on the v* tag push, where
the only recourse is a hot-fix tag. release.yml's build job invokes the same
script before the tag-vs-pyproject inline check so a uv.lock drift introduced
between merge and tag-push is caught before PyPI publish.

Stdlib only — runs against the bare ubuntu-latest python3 without an env
install. PEP 440 normalises ``0.5.0-rc1`` and ``0.5.0rc1`` to the same release,
so a tiny inline regex collapses the optional dash before pre-release / dev /
post markers before string comparison; full PEP 440 parsing is not needed
because all three readers feed off the same input distribution (project's own
version string).
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

_PRE_RELEASE_DASH = re.compile(r"-(?=(?:rc|a|b|c|alpha|beta|dev|post)\d)")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _normalise(version: str) -> str:
    """Collapse the optional dash before PEP 440 pre-release / dev / post markers."""
    return _PRE_RELEASE_DASH.sub("", version)


def _read_pyproject(root: Path) -> tuple[str, str]:
    """Return (project_name, project_version) from pyproject.toml."""
    with (root / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    return str(project["name"]), str(project["version"])


def _read_manifest_version(root: Path) -> str:
    with (root / "manifest.json").open("r", encoding="utf-8") as f:
        return str(json.load(f)["version"])


def _read_uv_lock_version(root: Path, package: str) -> str:
    with (root / "uv.lock").open("rb") as f:
        data = tomllib.load(f)
    for entry in data.get("package", []):
        if entry.get("name") == package:
            return str(entry["version"])
    raise SystemExit(f"uv.lock: no [[package]] entry for {package!r}")


def main() -> int:
    root = _project_root()
    project_name, pyproject_raw = _read_pyproject(root)
    manifest_raw = _read_manifest_version(root)
    uv_lock_raw = _read_uv_lock_version(root, project_name)

    pyproject = _normalise(pyproject_raw)
    if _normalise(manifest_raw) != pyproject:
        print(
            f"Version drift detected: manifest.json {manifest_raw!r} != "
            f"pyproject {pyproject_raw!r}.\n"
            "Bump pyproject.toml [project].version, manifest.json version, then run "
            "`uv lock --upgrade-package <project>` so uv.lock picks up the new "
            "project version without touching transitives.",
            file=sys.stderr,
        )
        return 1
    if _normalise(uv_lock_raw) != pyproject:
        print(
            f"Version drift detected: uv.lock {project_name} version {uv_lock_raw!r} "
            f"!= pyproject {pyproject_raw!r}.\n"
            "Run `uv lock --upgrade-package "
            f"{project_name}` to refresh uv.lock without pulling in unrelated "
            "transitive bumps.",
            file=sys.stderr,
        )
        return 1

    print(f"Version parity OK: pyproject == manifest == uv.lock == {pyproject_raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
