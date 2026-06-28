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
including Claude Desktop — through 20 MCP tools (18 read-oriented + 2 zip-flow) backed by a local
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

> **First-run warmup (recommended).** Before you install the bundle,
> run **once** in a terminal:
>
> ```bash
> uvx --from "apple-health-mcp-server@latest" apple-health-mcp-server --help
> ```
>
> Claude Desktop spawns the MCP server multiple times in parallel on
> its first boot. Each parallel `uvx` invocation tries to install the
> same Python interpreter at the same time and they can race on the
> minor-version-link directory, leaving a half-initialised cache that
> every subsequent launch then trips over (`Missing expected target
> directory for Python minor version link`). One warm-up invocation
> serialises the install and avoids the race. The race has been
> observed reliably on Windows; macOS / Linux filesystems are more
> forgiving but the warmup is still the cheap-and-correct precaution.

Then:

1. Download the latest `apple-health-mcp-server-vX.Y.Z.mcpb` bundle
   from the [latest release](https://github.com/rinoshiyo/apple-health-mcp-server/releases/latest)
   (the page resolves to the current `vX.Y.Z` automatically so the
   link does not rot when a new version ships)
2. Open Claude Desktop's **Settings → Connectors** panel
3. Drag-and-drop the `.mcpb` file onto the panel — Claude Desktop will
   install it and prompt to enable the server
4. The install dialog asks for **Export ZIPs directory** — point it
   at a folder where you keep your Apple Health export ZIPs
   (e.g. `C:\Users\<you>\Documents\AppleHealth` on Windows,
   `~/Documents/AppleHealth` on macOS / Linux). Drop your
   `export.zip` into that folder, then ask Claude to import it:
   "Hey Claude, import the latest Apple Health export." Claude calls
   `list_zips` → `import_zip(id="…")` and the data is queryable
   ~1–2 minutes later — no terminal commands required.
5. **Windows users only — avoid `%LOCALAPPDATA%` subfolders.** Claude
   Desktop on Windows ships as an MSIX package whose child processes
   run inside an AppContainer sandbox that virtualises
   `%LOCALAPPDATA%` to a per-package private path. If the Export ZIPs
   directory falls under that root, `list_zips` will not see ZIPs you
   drop there from Explorer. Pick a path under `%USERPROFILE%`
   (Documents, Desktop, etc.) instead.

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
      "args": ["apple-health-mcp-server", "serve"],
      "env": {
        "APPLE_HEALTH_EXPORT_ZIPS_DIR": "/Users/<you>/Documents/AppleHealth"
      }
    }
  }
}
```

`APPLE_HEALTH_EXPORT_ZIPS_DIR` is the folder where you keep your
Apple Health export ZIPs. v0.4's `list_zips` + `import_zip` MCP
tools read from this directory so Claude can discover + ingest your
export without you ever opening a terminal. Replace the path above
with a real folder you control (e.g. `~/Documents/AppleHealth`
expanded by your shell, or
`C:\Users\<you>\Documents\AppleHealth` on Windows). The MCPB
bundle install dialog promotes the **Export ZIPs directory** field
into this same env var; the JSON example is the same wire shape for
operators who skip the bundle.

Then fully quit Claude Desktop and reopen — the config is only re-read
at startup (closing the window is not enough).

Source: <https://modelcontextprotocol.io/quickstart/user> (fetched
2026-06-22).

### Claude Code

Easiest path is the CLI helper, which writes the entry into the right
scope and survives future schema tweaks:

```bash
claude mcp add --transport stdio --scope user \
  --env APPLE_HEALTH_EXPORT_ZIPS_DIR=$HOME/Documents/AppleHealth \
  apple-health -- uvx apple-health-mcp-server serve
```

- `--scope user` registers the server for every project (writes to
  `~/.claude.json`). Use `--scope project` to share via a
  version-controlled `.mcp.json` at the repo root, or `--scope local`
  (the default) for the current project only.
- `--env APPLE_HEALTH_EXPORT_ZIPS_DIR=…` points the v0.4 ZIP-flow
  tools (`list_zips` / `import_zip`) at the folder where you keep
  your Apple Health export ZIPs. Without it those tools surface a
  ``NEEDS_CONFIG`` envelope and the agent has to ask you to
  configure the path before it can import anything.
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
      "env": {
        "APPLE_HEALTH_EXPORT_ZIPS_DIR": "/Users/<you>/Documents/AppleHealth"
      }
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
env = { APPLE_HEALTH_EXPORT_ZIPS_DIR = "/Users/<you>/Documents/AppleHealth" }
```

`APPLE_HEALTH_EXPORT_ZIPS_DIR` points the v0.4 ZIP-flow tools
(`list_zips` / `import_zip`) at your Apple Health export drop-zone;
without it those tools return a ``NEEDS_CONFIG`` envelope and the
agent has to ask you to configure the path before it can import.

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

Override precedence (most → least specific):

1. `--db /custom/path/health.duckdb` on either subcommand.
2. `APPLE_HEALTH_DB` env var (file path) — same precedence as
   `--db` because the CLI promotes `--db` into this env so any
   downstream caller resolving through `resolve_db_path()` agrees
   with what the connection layer actually opened. v0.4 dropped the
   MCPB GUI field for this; Claude Desktop users who need a custom
   DB location now edit `claude_desktop_config.json` directly (see
   the manual JSON config section above) and add `APPLE_HEALTH_DB`
   to the server's `env` map.
3. `APPLE_HEALTH_DATA_DIR` env var (directory path) — useful when
   you want a custom root but the package's default file name.
4. The XDG / `LOCALAPPDATA` platform default above.

Use the `get_server_info` MCP tool to confirm at any time which path
the running server actually opened (and which override tier
resolved it), e.g. when troubleshooting the Windows MSIX sandbox
redirect described in the Claude Desktop install section above.

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

> **Cross-locale merging is not supported in v0.3.x (issue #131).**
> Apple Health stores some field VALUES (not just CSV headers) in the
> iPhone's display language: a Japanese-locale export surfaces ECG
> `classification` as `洞調律` where an English-locale export writes
> `SinusRhythm`, and `source_name` ships as `ヘルスケア` /
> `血中酸素ウェルネス` instead of `Health` / `Blood Oxygen`. The
> importer stores those values verbatim; there is no name-normalisation
> layer yet, so importing exports from **different** iPhone locales
> into the **same** DB will produce two parallel sets of rows that
> downstream tools cannot reconcile. Keep one DB per locale until
> normalisation lands (please thumbs-up #131 if you need this).

## Tools

20 tools are registered with FastMCP, grouped by family:

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

### Two-tier contract

The public surface is split into two tiers so that internal storage
choices can keep evolving without dragging the wire-facing contract
into major-bump territory every time.

**Layer 1 — Wire-facing contract (strict; changes require a major bump):**

- **MCP tool names, parameter signatures (including defaults), and
  top-level response field names** — adding a new tool, parameter, or
  response field is a minor bump; renaming, removing, or changing the
  type of an existing one is a major bump. Tool responses are consumed
  by downstream LLM prompt templates, so renaming a returned key is as
  breaking as renaming a parameter.
- **CLI subcommand names and their required parameters** (positional
  arguments and required flags alike), and **environment variable
  names and parsing rules** — same major-bump rules apply to
  renames / removals / semantic changes.
- **CLI exit codes** (see the table below).
- **Top-level Python identifiers exported via `__all__` from the
  package root** (`apple_health_mcp`) — e.g. `__version__`, `REPO_URL`,
  `ISSUES_URL`. Removing one of these or changing its type is a major
  bump.

**Layer 2 — Internal escape hatch (best-effort; changes ride a minor
bump and are called out under `Changed` in CHANGELOG.md):**

- **DuckDB schema** — table names, column names, types, and NOT NULL
  constraints. `run_custom_query` users read against these directly,
  so the project will not break them lightly, but the schema is a
  storage detail rather than the wire contract: a column rename or a
  type widening can ship in a minor release as long as the CHANGELOG
  flags it under `Changed`. Layer 1 still gates the tool responses
  built on top, so a schema migration that does not affect any tool's
  output stays invisible to non-`run_custom_query` callers.
- **Default DuckDB file path conventions** (see [Database location](#database-location)).
  The XDG-resolved defaults on each OS are stable in practice — users
  back them up, point monitoring at them, or symlink them across
  machines — but reserving them as Layer 2 leaves room to add an
  override mechanism or shift the default in response to an OS
  convention change without forcing a major bump.
- **Module-internal helpers** — anything not re-exported through
  `apple_health_mcp.__all__`. These are documented inline for
  contributors but are not part of the SemVer contract at any tier.

`run_custom_query` callers depend on Layer 2 by construction. The
project treats their stability as best-effort: the goal is to avoid
breaking the schema between minor versions whenever possible, and to
document any change that does land under `Changed` in CHANGELOG.md so
existing custom queries can be updated in one pass.

Schema migrations are forward-only. Downgrading to a prior version
after a schema bump (e.g. v0.3.0-rc2 → v0.2.x) requires re-importing
from `export.xml` or restoring a pre-bump DB backup.

#### Layer 1 reference tables

**Environment variables** the server and importer read from the
process environment. The current set:

| Name | Purpose | Default |
|---|---|---|
| `APPLE_HEALTH_TZ` | DuckDB session timezone used to render `TIMESTAMPTZ` columns. Overridden by `--tz` on the CLI when both are set. | OS timezone |
| `APPLE_HEALTH_IMPORT_PROGRESS_SECS` | Cadence of the Phase 1 progress emitter on `import`. Integer seconds; out-of-range integers are clamped to 1..600, non-integer strings fall back to the default with a warning. Exports smaller than 1 MB skip the emitter entirely. | `10` |
| `APPLE_HEALTH_LOG_LEVEL` | stdlib `logging` level applied to the root logger (`DEBUG`/`INFO`/`WARNING`/`ERROR`). All logs land on stderr; stdout is reserved for the MCP stdio transport. | `INFO` |
| `APPLE_HEALTH_LOG_FORMAT` | Log formatter shape. `human` is plain text; `json` emits one JSON object per line for log aggregators. | `human` |

The server also honours the OS-standard `XDG_DATA_HOME` (Linux/macOS) and `LOCALAPPDATA` (Windows) when resolving the default DB path; those are part of the platform contract, not project-specific.

Renaming, removing, or changing the parsing rules of any of these is a major bump. Adding a new env var is a minor bump.

**CLI parameters** — used by callers that pipe `apple-health-mcp-server` into shell scripts, service supervisors, or wire it into Claude Desktop / Claude Code configs:

- **Subcommands**: `import <export-dir>`, `serve`
- **Top-level flags**: `--db <path>` (DB path override, applies to both subcommands), `--tz <name>` (overrides `APPLE_HEALTH_TZ`)
- **`serve` flags**: `--transport stdio|http` (default `stdio`), `--host <addr>` (HTTP bind host), `--port <int>` (HTTP port)

Renaming a subcommand or flag, removing one, or changing the semantics of an existing one is a major bump. Adding a new optional flag or subcommand is a minor bump.

**CLI exit codes** — observed by shell-script callers:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Any `AppleHealthMCPError` from the import or serve path (missing export, malformed DB, importer failure, server startup failure) |
| `2` | Usage error from the CLI argument parser (unknown subcommand, missing required argument, bad flag value) |

Adding a new specific exit code (e.g. carving off `3` for "DB locked by another process") is a minor bump; collapsing or repurposing an existing code is a major bump.

#### Outside both layers

Anything not enumerated in Layer 1 or Layer 2 — helper modules without
an MCP-tool / CLI / `__all__` / env-var / exit-code surface,
identifiers prefixed with `_` (private constants, helpers, internal
exceptions), and module-internal constants — is **not** part of the
public API at any tier and may change in any release. In particular:

- **Log-line format** (e.g. `progress: xml NN% (X / Y MB, ~Z min remaining)`)
  is not part of the public API contract; the human-readable shape may
  change between releases without a SemVer bump. `APPLE_HEALTH_LOG_FORMAT=json`
  currently wraps the same human-readable string inside a JSON
  envelope's `message` field — it doesn't currently emit
  per-progress-event structured fields. If you need machine-parseable
  progress, please open an issue describing the use case; until a
  structured progress contract is published, treat all progress output
  as informational only.
- **MCP tool description text** (the LLM-facing prose embedded in each
  tool registration) is not part of the public API contract;
  descriptions may be tightened, reworded, or reorganised without a
  SemVer bump as long as the parameter and return shape stay stable.
  Clients that rely on a tool should lock onto its name and signature,
  not its description prose.

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

### Upgrading from < v0.3.0

v0.3.0 dropped automatic in-place schema upgrades from pre-v0.3.0
databases (see [issue #124](https://github.com/rinoshiyo/apple-health-mcp-server/issues/124)).
The first `apple-health-mcp-server serve` against an older DB now exits
with a `ConfigError` that names the path and shows the recovery command;
your data on disk is left untouched.

Recovery is a one-time re-import:

```bash
# Remove the pre-v0.3.0 DB (the default location, override with --db).
rm ~/.local/share/apple-health-mcp/health.duckdb

# Re-import from the latest Apple Health export.zip you extracted.
uvx apple-health-mcp-server@latest import /path/to/apple_health_export
```

The importer takes a couple of minutes on a multi-GB `export.xml` and
the data never leaves your machine. After the re-import, every
subsequent `serve` invocation runs against the v0.3.0 schema and the
ConfigError no longer fires.

## Troubleshooting

**Every tool returns a structured `{"state": "NEEDS_CONFIG" | "NEEDS_IMPORT", ...}` envelope**

The MCP server boots even when the local DuckDB file is empty so the
client still sees the full tool list, but every read tool short-circuits
with a structured JSON envelope until a successful import lands:

```json
{
  "state": "NEEDS_CONFIG",
  "reason": "APPLE_HEALTH_EXPORT_ZIPS_DIR is not set",
  "suggested_action": "ask_user_to_open_settings",
  "human_message": "Set the APPLE_HEALTH_EXPORT_ZIPS_DIR ... "
}
```

The `state` is one of:

- `NEEDS_CONFIG` — the `APPLE_HEALTH_EXPORT_ZIPS_DIR` env var (the
  drop-zone the v0.4 `list_zips` / `import_zip` MCP tools read from)
  is not configured. Claude Desktop users set it via Settings → MCP →
  apple-health-mcp-server → Export ZIPs directory; other MCP clients
  set the env var directly.
- `NEEDS_IMPORT` — the drop-zone is configured but no successful
  import has happened yet. Ask Claude to call `list_zips` followed by
  `import_zip(id="…")`.

For the CLI import flow:

```bash
apple-health-mcp-server import /path/to/apple_health_export
```

**Stop the MCP server first** (quit Claude Desktop, kill the `serve`
process, etc.) before running the CLI importer. v0.4 opens the serve
handle writable so the upcoming `import_zip` tool can drive the
importer inline; DuckDB holds an exclusive file lock for the lifetime
of the writable handle, so a concurrent `apple-health-mcp-server
import` from another shell would fail with a lock-conflict error.
After the CLI import finishes, restart the server so the tools query
against the fresh data.

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
