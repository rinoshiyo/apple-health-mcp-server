<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

> **Probably the most complete Apple Health MCP server.**
>
> Read this in [English](README.md) / [日本語](README.ja.md).

`apple-health-mcp-server` exposes the contents of your Apple Health export
(`export.xml` plus the ECG CSV and GPX route files Apple ships alongside it)
to any [Model Context Protocol](https://modelcontextprotocol.io/) client —
including Claude Desktop — through 17 read-oriented tools backed by a local
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

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: Claude Desktop is not yet released on Linux; use **Claude Code**
  below instead.

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

Then fully quit Claude Desktop and reopen — the config is only re-read
at startup (closing the window is not enough).

Source: <https://modelcontextprotocol.io/quickstart/user> (fetched
2026-06-22).

### Claude Code

Easiest path is the CLI helper, which writes the entry into the right
scope and survives future schema tweaks:

```bash
claude mcp add --transport stdio --scope user apple-health -- uvx apple-health-mcp-server serve
```

- `--scope user` registers the server for every project (writes to
  `~/.claude.json`). Use `--scope project` to share via a
  version-controlled `.mcp.json` at the repo root, or `--scope local`
  (the default) for the current project only.
- The `--` separator is mandatory when the server command takes its own
  arguments — without it Claude Code would try to parse `serve` as one
  of its own flags.

Equivalent manual entry inside the chosen JSON file:

```json
{
  "mcpServers": {
    "apple-health": {
      "type": "stdio",
      "command": "uvx",
      "args": ["apple-health-mcp-server", "serve"],
      "env": {}
    }
  }
}
```

A running session does not auto-reload `.mcp.json` edits; restart
Claude Code to pick them up. Stdio servers are not automatically
reconnected after a crash either — restart the session if the server
goes away mid-conversation.

Source: <https://code.claude.com/docs/en/mcp> (fetched 2026-06-22).

### Codex CLI

Codex CLI stores MCP servers in **TOML**, not JSON. The simplest path
is the helper command, which writes into `~/.codex/config.toml`:

```bash
codex mcp add apple-health -- uvx apple-health-mcp-server serve
```

Equivalent manual entry in `~/.codex/config.toml` (override the path
with `CODEX_HOME=` if needed):

```toml
[mcp_servers.apple-health]
command = "uvx"
args = ["apple-health-mcp-server", "serve"]
```

Edits to `config.toml` take effect on the next `codex` invocation —
restart any running session to apply them. The CLI also exposes
`codex mcp list` / `codex mcp get <name>` / `codex mcp remove <name>`
for inspection and cleanup.

Source: <https://developers.openai.com/codex/mcp> (fetched 2026-06-22).

### Importing your export

Before any tool returns data you have to ingest your export once.
Apple gives you a directory containing `export.xml`, an
`electrocardiograms/` folder, and a `workout-routes/` folder; point
the importer at the directory itself:

```bash
uvx apple-health-mcp-server import /path/to/apple_health_export
```

The import is idempotent — re-running it with a newer export merges
the new rows into the existing database via the `import_id` column.

### Database location

By default the database lands at the XDG-resolved data directory:

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

Override with `--db /custom/path/health.duckdb` on either subcommand.

## Tools

17 tools are registered with FastMCP, grouped by family:

| Family | Tools |
|---|---|
| Record types & data | `list_record_types`, `query_records`, `get_record_statistics` |
| Workouts | `list_workouts`, `get_workout_details`, `get_workout_route` |
| Activity summaries | `get_activity_summaries` |
| Heart rate | `get_heart_rate_samples` |
| Correlations | `list_correlations`, `get_correlation_details` |
| ECG | `list_ecg_readings`, `get_ecg_data` |
| State of mind | `list_state_of_mind` |
| Me characteristics | `get_me_attributes` |
| Metadata & ops | `list_data_sources`, `get_import_history` |
| Escape hatch | `run_custom_query` (read-only validated SQL) |

## Updating

`uvx` caches the package on first run and re-uses that cached copy on
subsequent invocations, so a new release does **not** install itself
automatically. Pick one:

- **Always run the latest** — pass `--refresh` once whenever you want
  to pull a newer version:

  ```bash
  uvx --refresh apple-health-mcp-server serve
  ```

- **Pin a specific version** — write the version directly in your
  Claude Desktop / Codex / Cursor config so an unrelated `uvx` cache
  eviction cannot move you off it:

  ```jsonc
  {
    "mcpServers": {
      "apple-health": {
        "command": "uvx",
        "args": ["apple-health-mcp-server==0.1.0", "serve"]
      }
    }
  }
  ```

See [CHANGELOG.md](./CHANGELOG.md) for the per-release notes.

## Development

```bash
uv sync
uv run pytest
```

See [CLAUDE.md](./CLAUDE.md) for the full command list, conventions, and
the mandatory `/code-review --fix` policy on every pull request.

## Contributing

Issues and pull requests in **English or Japanese** are both first class;
see [CLAUDE.md §6](./CLAUDE.md#6-language-policy) for the full language
policy.

## License

[MIT](./LICENSE)
