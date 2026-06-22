# CLAUDE.md

Guidance for human contributors and AI coding agents (Claude Code,
Codex, etc.) working on this repository.

## 1. Project Overview

`apple-health-mcp-server` is a Model Context Protocol server that
ingests an Apple Health export (`export.xml` + the ECG CSVs and GPX
route files Apple ships alongside it) into a local DuckDB database
and exposes 16 read-oriented tools over it. The CLI ships two
subcommands: `apple-health-mcp import <dir>` and
`apple-health-mcp serve`. Distribution targets are PyPI (uvx) and
Claude Desktop DXT bundles. All data stays local; nothing is uploaded.

## 2. Architecture

```
src/apple_health_mcp/
  cli.py            Typer entry point, dispatches to importers / server
  exceptions.py     Exception hierarchy rooted at HealthImportError
  logging_config.py logging setup (stderr only, stdout reserved for MCP)
  importers/        XML / ECG / GPX importers + dedup + orchestrator
  db/               DuckDB schema + connection helpers + migrations
  server/           FastMCP server, query validation, and 16 tool modules
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
- GPX timestamps are true UTC; XML timestamps are local wall-clock
  time. The GPX importer shifts route points by the parent workout's
  offset (from `workout_offset_map`) so joins are clean.

## 6. Language Policy

- **Issues and PRs**: English and Japanese are both first-class. Use
  whichever the contributor is comfortable with; the maintainer reads
  both.
- **Code and docs** (`README.md`, `CHANGELOG.md`, `CLAUDE.md`,
  `SECURITY.md`, `docs/`): English only. `README.ja.md` is the one
  parallel exception.
- **Commit messages**: English recommended; Japanese accepted as long
  as the Conventional Commits prefix stays.

## 7. Pointers

- [README.md](./README.md) — public entry point (English)
- [README.ja.md](./README.ja.md) — public entry point (Japanese)
- [CHANGELOG.md](./CHANGELOG.md) — Keep a Changelog format
- `tests/fixtures/README.md` — fixture policy and catalogue
- `.github/workflows/ci.yml` — 3 OS × 3 Python matrix, coverage artifacts
- Memory dir (Claude Code): contains the implementation-decision log
  and the autonomous-loop work playbook; consult before starting a
  large change.
