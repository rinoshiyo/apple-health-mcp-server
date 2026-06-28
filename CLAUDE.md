# CLAUDE.md

Guidance for human contributors and AI coding agents (Claude Code,
Codex, etc.) working on this repository.

## 1. Project Overview

`apple-health-mcp-server` is a Model Context Protocol server that
ingests an Apple Health export (`export.xml` + the ECG CSVs and GPX
route files Apple ships alongside it) into a local DuckDB database
and exposes 17 read-oriented tools over it. The CLI ships two
subcommands: `apple-health-mcp-server import <dir>` and
`apple-health-mcp-server serve`. Distribution targets are PyPI (uvx)
and Claude Desktop DXT bundles. All data stays local; nothing is
uploaded.

## 2. Architecture

```
src/apple_health_mcp/
  cli.py            Typer entry point, dispatches to importers / server
  exceptions.py     Exception hierarchy rooted at HealthImportError
  logging_config.py logging setup (stderr only, stdout reserved for MCP)
  importers/        XML / ECG / GPX importers + dedup + orchestrator
  db/               DuckDB schema + connection helpers + migrations
  server/           FastMCP server, SQL safety validator, DataState helper,
                    and tool modules (canonical list in
                    server/tools/__init__.py::ALL_TOOLS)
  models/           Pydantic v2 schemas (reserved for future use)
```

`importers/orchestrator.py::run_import` is the single entry point that
drives `XML -> ECG -> GPX -> finalize` and writes one row into the
`imports` table per run. `server/tools/*.py` each export
`register(mcp, conn, lock)`; `server/tools/__init__.py::ALL_TOOLS`
lists them in the order the Rust reference declared them.

## 3. Development Commands

```bash
uv sync                                       # install deps + the dev group
uv run pre-commit install                     # one-time hook setup
uv run pytest --cov-branch --cov-fail-under=100   # full suite + 100% gate
uv run ruff check                             # lint
uv run ruff format --check                    # formatting (no diffs allowed)
uv run mypy                                   # strict type-check
```

**Every pull request must run `/code-review --fix` before merge** and
push the resulting working-tree diffs. The CI matrix (3 OS × 3 Python)
must be fully green; the unit and integration tests share fixtures via
`tests/_helpers.py` and on-disk samples under `tests/fixtures/`.

## 4. Code Conventions

- Code comments and docstrings are **English only**.
- Type hints are required on every public function (mypy `strict`).
- Branch coverage gate is hard 100%. Unreachable code is marked with
  `# pragma: no cover - <one-line reason>`; the reason is the price of
  the exclusion.
- Logging goes through stdlib `logging` and lands on **stderr only** —
  the MCP stdio transport owns stdout.
- Conventional Commits (English) on all commits.

## 5. Apple Health Domain Rules

- **No real export data** is committed to this public repository. The
  fixtures in `tests/fixtures/` are hand-written synthetic stand-ins;
  locale-specific parser quirks are exercised by inline strings in
  unit tests.
- **No real device UUIDs or source names** appear anywhere
  (commit messages, issues, PRs, fixtures, log examples, error
  messages). Use generic placeholders like `Apple Watch` / `iPhone`.
- Apple Health duplicates `Correlation` children at the top level by
  spec; importers must hash both paths identically so dedup collapses
  them to one `records` row.
- GPX timestamps are true UTC (`Z`-suffixed); XML timestamps carry an
  explicit `+HHMM` offset that the importer reshapes to ISO 8601
  `+HH:MM` form. Both feed DuckDB's `TIMESTAMPTZ` parser, which stores
  them as the same canonical UTC instant — no per-workout shift is
  needed. The session timezone (`--tz` / `APPLE_HEALTH_TZ`, defaults to
  the OS local TZ) only affects the render path.

## 6. Language Policy

- **Issues and PRs**: English and Japanese are both first-class. Use
  whichever the contributor is comfortable with; the maintainer reads
  both.
- **Code and docs** (`README.md`, `CHANGELOG.md`, `CLAUDE.md`): English
  only. `README.ja.md` is the one parallel exception.
- **Commit messages**: English recommended; Japanese accepted as long
  as the Conventional Commits prefix stays.

## 7. Pointers

- [README.md](./README.md) — public entry point (English)
- [README.ja.md](./README.ja.md) — public entry point (Japanese)
- [CHANGELOG.md](./CHANGELOG.md) — Keep a Changelog format
- [tests/fixtures/README.md](./tests/fixtures/README.md) — fixture policy and catalogue
- `.github/workflows/ci.yml` — 3 OS × 3 Python matrix, coverage artifacts
- `.github/workflows/release.yml` — tag-triggered PyPI publish (Trusted Publishing)

## 8. Release Operations

The release workflow (`.github/workflows/release.yml`) publishes to
PyPI via [OIDC Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
— no API tokens are stored in this repository.

### One-time PyPI registration (maintainer)

Done once before the first release tag is pushed.

1. Visit <https://pypi.org/manage/account/publishing/> and add a new
   pending Trusted Publisher:
   - **PyPI Project name**: `apple-health-mcp-server`
   - **Owner**: `rinoshiyo`
   - **Repository name**: `apple-health-mcp-server`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi`
2. In the GitHub repository Settings → Environments, create an
   environment named `pypi` (no secrets required; the OIDC token is
   minted at job time).
3. After the first successful publish, the project switches from
   "pending" to a normal Trusted Publisher entry.

### Cutting a release

0. **Bump `[project] version` in `pyproject.toml` AND `version` in
   `manifest.json`, then run `uv lock` so `uv.lock` picks up the new
   project version.** Merge that change to `main` via a PR. The CI
   `metadata-checks` job in `.github/workflows/ci.yml` runs
   `scripts/check_version_parity.py` and fails the PR if the three
   files disagree, so the drift surfaces at PR time rather than at
   `v*` tag push. The release workflow then re-verifies the tag
   against pyproject (build job) and manifest.json (build_bundle job)
   as a defence-in-depth gate.

1. Pre-flight (run locally before tagging):

   ```bash
   rm -rf dist/
   uv run pytest --cov-branch --cov-fail-under=100
   uv build
   uvx twine check --strict dist/*
   ```

2. Tag and push:

   ```bash
   git tag -a v0.1.0 -m "Release v0.1.0"
   git push origin v0.1.0
   ```

GitHub Actions then verifies the tag matches the pyproject version,
builds the sdist + wheel, runs `twine check --strict`, and uploads
via `pypa/gh-action-pypi-publish@release/v1`.

## 9. LP Copy Conventions

The landing page copy lives in `docs/i18n/ja.json` and
`docs/i18n/en.json`. These files MUST NOT embed an explicit version
literal (`v0.4.1`, `apple-health-mcp-server-v0.4.1.mcpb`, etc.) in
any field except `footer.version`.

- Bundle download URLs and Claude Desktop install steps point to
  GitHub's [`/releases/latest`](https://github.com/rinoshiyo/apple-health-mcp-server/releases/latest)
  redirector. GitHub resolves it to whichever tag is marked
  *Latest*, so the LP never goes stale between releases.
- `footer.version` is the one sanctioned slot. The
  `sync_docs_version` job in `.github/workflows/release.yml`
  rewrites it on every stable tag push (it skips pre-releases,
  matching `if: !contains(github.ref_name, '-')`).
- CI enforces the rule via
  `scripts/check_lp_no_version_literal.py`, invoked from the
  `metadata-checks` job in `ci.yml`. A future contributor who
  pastes a literal back into the JSON gets a PR-time failure
  instead of shipping it to the LP.

The convention was chosen in the 2026-06-25 grill (option c) over
two alternatives: (a) extend the release workflow to rewrite every
mention, and (b) introduce a `{{VERSION}}` placeholder pattern.
Option (c) won because GitHub's redirector already does the work
the LP needs.
