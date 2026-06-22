<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-blue.svg)](https://modelcontextprotocol.io/)

> **Probably the most complete Apple Health MCP server.**
>
> Read this in [English](README.md) / [日本語](README.ja.md).

`apple-health-mcp-server` exposes the contents of your Apple Health export
(`export.xml` plus the ECG CSV and GPX route files Apple ships alongside it)
to any [Model Context Protocol](https://modelcontextprotocol.io/) client —
including Claude Desktop — through 16 read-oriented tools backed by a local
[DuckDB](https://duckdb.org/) database.

## Features

- **Comprehensive ingestion.** Imports `Record`, `Workout` (with
  `WorkoutEvent`, `WorkoutStatistics`, `WorkoutRoute`, and
  `WorkoutMetadataEntry`), `ActivitySummary`, `Correlation`, `Me`,
  `ExportDate`, ECG voltage samples, and GPX route points. Categorical
  state-of-mind entries (iOS 17+) land in a dedicated table.
- **All data stays local — no external transmission.** The importer reads
  files from disk, the server speaks MCP over stdio (HTTP is opt-in), and
  the only network artefact is whatever the client itself decides to send.
- **DuckDB-backed.** Re-imports are idempotent thanks to deterministic
  deduplication; ad-hoc analysis through `run_custom_query` runs at native
  DuckDB speed.
- **Time-zone aware.** GPX route timestamps are aligned to each parent
  workout's local offset so joins against XML-derived rows are clean.
- **Cross-platform.** Tested on Ubuntu, macOS, and Windows against Python
  3.12 / 3.13 / 3.14.
- **100% branch-tested.** Every release gates on full coverage with
  `pytest --cov-branch --cov-fail-under=100`.

## Installation

The recommended entry point is [uvx](https://docs.astral.sh/uv/), which
fetches a one-shot virtualenv on demand and never pollutes the system
Python:

```bash
uvx apple-health-mcp-server --help
```

### Claude Desktop

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "apple-health": {
      "command": "uvx",
      "args": ["apple-health-mcp-server", "serve"]
    }
  }
}
```

Restart Claude Desktop. Before the tools return anything useful, import
your data once:

```bash
uvx apple-health-mcp-server import /path/to/apple_health_export
```

The expected directory is the one Apple Health unzips for you (it
contains `export.xml`, an `electrocardiograms/` folder, and a
`workout-routes/` folder).

### Database location

By default the database lands at the XDG-resolved data directory:

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

Override with `--db /custom/path/health.duckdb` on either subcommand.

## Tools

The 16 tools registered with FastMCP cover the common slices of an Apple
Health export. Inspect `apple_health_mcp.server.tools` (or call the
client's tool list) for the full set; broadly they fall into
record / workout / activity-summary / correlation / ECG / route / custom
SQL / metadata families.

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest --cov-branch --cov-fail-under=100
uv run ruff check
uv run ruff format --check
uv run mypy
```

See [CLAUDE.md](./CLAUDE.md) for the development conventions used in this
repository, including the mandatory `/code-review --fix` policy on every
pull request.

## Contributing

Issues and pull requests in **English or Japanese** are both first class.
Code comments and the documents under `docs/`, `README.md`,
`CHANGELOG.md`, `CLAUDE.md`, and `SECURITY.md` stay in English so the
codebase reads uniformly. `README.ja.md` is the one parallel exception.

## License

[MIT](./LICENSE)
