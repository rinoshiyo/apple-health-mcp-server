<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Landing page](https://img.shields.io/badge/landing_page-rinoshiyo.github.io-10b981)](https://rinoshiyo.github.io/apple-health-mcp-server/)

> **Make Claude your personal health AI — locally.**
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
- **One-click Claude Desktop install.** Drag-and-drop the MCPB bundle
  attached to every GitHub Release; see the *Installation → Claude
  Desktop (MCPB bundle)* section below.
- **100% branch-tested.** Every release gates on full coverage with
  `pytest --cov-branch --cov-fail-under=100`.

## Installation

The recommended entry point is [uvx](https://docs.astral.sh/uv/), which
fetches a one-shot virtualenv on demand and never pollutes the system
Python:

```bash
uvx apple-health-mcp-server --help
```

### Claude Desktop (one-click via MCPB bundle)

The easiest path on Claude Desktop is the **MCPB bundle** attached to
each [GitHub Release](https://github.com/rinoshiyo/apple-health-mcp-server/releases).

> **Prerequisite:** the bundle wraps `uvx apple-health-mcp-server serve`,
> so install [`uv`](https://docs.astral.sh/uv/) first (`brew install uv`
> on macOS, the official installer on Windows). Without `uv` on `PATH`
> Claude Desktop will fail with a generic spawn error after install.

Then:

1. Download the latest `apple-health-mcp-server-vX.Y.Z.mcpb` from the
   release assets
2. Open Claude Desktop's **Settings → Connectors** panel
3. Drag-and-drop the `.mcpb` file onto the panel — Claude Desktop will
   install it and prompt to enable the server

The MCPB format is documented at <https://github.com/anthropics/mcpb>;
both `.dxt` (legacy) and `.mcpb` extensions are accepted by Claude
Desktop.

### Claude Desktop (manual JSON config)

If you prefer to wire the server up by hand, edit
`claude_desktop_config.json`:

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

Phase 1 (XML parse) emits a single-line progress entry every
10 seconds (`INFO progress: xml NN% (X / Y MB, ~Z min remaining)`)
so a streaming agent or human can confirm forward motion during a
multi-minute parse. Tune the cadence via
`APPLE_HEALTH_IMPORT_PROGRESS_SECS` (positive integer, clamped to
1..600); set it to `60` for quiet runs or `1` for debugging. Exports
smaller than 1 MB skip the emitter entirely.

### Database location

By default the database lands at the XDG-resolved data directory:

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

Override with `--db /custom/path/health.duckdb` on either subcommand.

### Locales

Apple Health localises the ECG CSV header labels to the iPhone language
setting (the `export.xml` itself is locale-neutral). The importer
recognises:

- **Verified**: English, Japanese (both `記録日` and `記録日時` variants)
- **Best-effort**: Chinese Simplified, Chinese Traditional, Korean — header
  strings are educated guesses and have not been confirmed against real
  exports from those locales

The authoritative source of truth for which locales the parser supports
is the `_VERIFIED_LOCALES` and `_BEST_EFFORT_LOCALES` tuples in
`src/apple_health_mcp/importers/ecg.py` (alongside the per-header
`_*_LABELS` tuples they describe). Add a locale by extending both that
file and the tuples above; this README section reflects them.

When the parser fails to match any locale, the warning log points to the
GitHub issue tracker and asks for the first ten lines of the CSV so the
locale can be added. The full guidance is emitted once per import run
(further files in the same run get a short reference back to it). There
is no privacy concern in those header lines — the importer skips `Name`
and `Date of Birth` by design.

Distance and energy units (`km`, `mi`, `kcal`) come straight from the
underlying HealthKit identifiers and are not localised; the
`total_distance_unit` column on the `workouts` table records them
faithfully.

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

## Compatibility

`apple-health-mcp-server` follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from v1.0.0
onward. While the project remains in the v0.x.y series, breaking
changes can land in any minor release; the project minimises them but
does not formally guarantee against them yet.

### Public API surface

The following are considered part of the **public API** under SemVer:

- **MCP tool names, parameter signatures (including defaults), and
  top-level response field names** — adding a new tool, parameter, or
  response field is a minor bump; renaming, removing, or changing the
  type of an existing one is a major bump. Tool responses are consumed
  by downstream LLM prompt templates, so renaming a returned key is as
  breaking as renaming a parameter.
- **DuckDB schema table names, column names, types, and NOT NULL
  constraints** — adding a column is a minor bump; renaming, removing,
  retyping, or relaxing a NOT NULL on an existing column (or renaming a
  table) is a major bump. Relevant for `run_custom_query` consumers
  building SQL against the tables — the v0.1.4 `imports.imported_at`
  regression showed constraints are user-visible too, not just types.
- **CLI subcommand names and their required parameters** (positional
  arguments and required flags alike) — same versioning rules apply.
- **Top-level Python identifiers exported via `__all__` from the
  package root** (`apple_health_mcp`) — e.g. `__version__`, `REPO_URL`,
  `ISSUES_URL`. Removing one of these or changing its type is a major
  bump.
- **Environment variables** the server and importer read from the
  process environment. The current set:

  | Name | Purpose | Default |
  |---|---|---|
  | `APPLE_HEALTH_TZ` | DuckDB session timezone used to render `TIMESTAMPTZ` columns. Overridden by `--tz` on the CLI when both are set. | OS timezone |
  | `APPLE_HEALTH_IMPORT_PROGRESS_SECS` | Cadence of the Phase 1 progress emitter on `import`. Integer seconds; out-of-range integers are clamped to 1..600, non-integer strings fall back to the default with a warning. Exports smaller than 1 MB skip the emitter entirely. | `10` |
  | `LOG_LEVEL` | stdlib `logging` level applied to the root logger (`DEBUG`/`INFO`/`WARNING`/`ERROR`). All logs land on stderr; stdout is reserved for the MCP stdio transport. | `INFO` |
  | `LOG_FORMAT` | Log formatter shape. `human` is plain text; `json` emits one JSON object per line for log aggregators. | `human` |

  The server also honours the OS-standard `XDG_DATA_HOME` (Linux/macOS) and `LOCALAPPDATA` (Windows) when resolving the default DB path; those are part of the platform contract, not project-specific.

  Renaming, removing, or changing the parsing rules of any of these is a major bump. Adding a new env var is a minor bump.
- **CLI parameters** — used by callers that pipe `apple-health-mcp-server` into shell scripts, service supervisors, or wire it into Claude Desktop / Claude Code configs:

  - **Subcommands**: `import <export-dir>`, `serve`
  - **Top-level flags**: `--db <path>` (DB path override, applies to both subcommands), `--tz <name>` (overrides `APPLE_HEALTH_TZ`)
  - **`serve` flags**: `--transport stdio|http` (default `stdio`), `--host <addr>` (HTTP bind host), `--port <int>` (HTTP port)

  Renaming a subcommand or flag, removing one, or changing the semantics of an existing one is a major bump. Adding a new optional flag or subcommand is a minor bump.
- **CLI exit codes** — observed by shell-script callers:

  | Code | Meaning |
  |---|---|
  | `0` | Success |
  | `1` | Any `AppleHealthMCPError` from the import or serve path (missing export, malformed DB, importer failure, server startup failure) |
  | `2` | Usage error from the CLI argument parser (unknown subcommand, missing required argument, bad flag value) |

  Adding a new specific exit code (e.g. carving off `3` for "DB locked by another process") is a minor bump; collapsing or repurposing an existing code is a major bump.
- **DuckDB database file path conventions** (see [Database location](#database-location)) — the default XDG-resolved paths on each OS are part of the contract because users back them up, point monitoring at them, or symlink them across machines. Changing where the default DB lands is a major bump; supporting an additional override mechanism is a minor bump.

Anything not enumerated above — helper modules without an MCP-tool /
CLI / DuckDB-schema / `__all__` / env-var / exit-code / DB-path
surface, identifiers prefixed with `_` (private constants, helpers,
internal exceptions), and module-internal constants — is **not** part
of the public API and may change in any release.

### Deprecation policy

(Applies from v1.0.0 onward — during v0.x.y, breaking changes can land
in any minor release without going through this cadence; see the
headline above.)

When something in the public API is scheduled for removal or rename:

1. The deprecated item is marked in the CHANGELOG.md `Deprecated`
   section of the release that announces it, alongside the planned
   replacement and removal version
2. The deprecated item keeps working for **at least one minor release**
   before being removed (e.g. `1.5.0` announces deprecation, `1.6.x`
   continues to ship the old name, `2.0.0` removes it)
3. The actual removal lands in the next major version bump

### Security exception

A CVE-grade flaw inside a deprecated surface (e.g. a `run_custom_query`
parameter that turns out to leak data, or a tool whose response shape
exposes something it shouldn't) may break the deprecation cadence
above: the fix can ship as a removal or breaking change in **any**
release, including a patch. Such breaks are called out under a
`Security` heading in CHANGELOG.md so downstream consumers can spot
them in a single scan, and a security advisory is published on the
GitHub repository's Security tab. Without this carve-out, the
deprecation policy would bind the maintainer to keep a known-bad
surface alive for a full minor cycle, which is worse than the surprise
break it would prevent.

## Updating

`uvx` caches the package on first run and re-uses that cached copy on
subsequent invocations, so a new release does **not** install itself
automatically. Pick one:

- **Always run the latest** — use the `@latest` suffix whenever you
  want the newest published version:

  ```bash
  uvx apple-health-mcp-server@latest serve
  ```

  > **Why not `--refresh`?** `--refresh` revalidates PyPI metadata but
  > does not always rebuild the cached tool environment, so a freshly
  > published release can be silently shadowed by the previously-cached
  > version (see [astral-sh/uv#16991](https://github.com/astral-sh/uv/pull/16991)).
  > `@latest` is the method
  > [recommended by the uv docs](https://docs.astral.sh/uv/concepts/tools/)
  > and is unambiguous.

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

## Troubleshooting

**Every tool returns "No Apple Health data has been imported yet."**

The MCP server boots even when the local DuckDB file is empty so the
client still sees the full tool list, but every tool that needs data
returns this guidance string until you run the importer:

```bash
apple-health-mcp-server import /path/to/apple_health_export
```

After the import finishes, **restart the MCP server** (quit and reopen
Claude Desktop / Claude Code / Codex, or stop and re-run the `serve`
process). The server keeps a read-only DuckDB snapshot for the lifetime
of the process; new rows only become visible to a fresh connection.

`get_import_history` is the one tool that stays callable on an empty
DB — it returns an empty list, which is how you confirm "no imports
yet" from the client side.

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
