# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from v1.0.0 onward — see the README's "Compatibility" section for the
v0.x.y disclaimer and the public-API scope.

## [Unreleased]

### Breaking

- **Pagination envelope unified across the 7 paginated list/page tools**
  (issue #108, PR-E). The following 7 tools now return
  `{items, total, next_offset}`: `query_records`, `list_workouts`,
  `list_correlations`, `list_state_of_mind`, `list_ecg_readings`,
  `get_heart_rate_samples`, and `get_workout_route`. The 6 that
  previously returned bare arrays gain the envelope wrapper;
  `get_workout_route` renames its `points` key to `items` and the
  `has_more` flag is dropped everywhere — `next_offset is null` is
  now the canonical "last page" marker. Each tool also gains an
  `offset` parameter so callers can paginate via the returned
  `next_offset`. `total` is computed via `COUNT(*) OVER ()` in the
  same SELECT as the page rows so each request still takes one DB
  round trip in the common case; an `offset` past the end of the
  dataset triggers a second targeted `COUNT(*)` so `total` never
  reads as `0` when the underlying table actually has rows. Clients
  reading the raw array (`json.loads(response)`) must switch to
  `json.loads(response)["items"]`; clients matching `has_more` must
  switch to `next_offset is None`. Aggregates and static catalogues
  — `get_activity_summaries`, `get_import_history`, `list_record_types`,
  `list_data_sources` — keep returning bare arrays because they
  paginate by domain key (date range, import id, record-type
  identifier, source identifier), not by row offset; their shapes stay
  stable across v0.3.0 → v1.0.0.

### Changed

- **DB schema: `heart_rate_samples.sample_time` is now `DOUBLE`** (issue
  #109, Layer 2 schema bump). Previously stored as VARCHAR
  `HH:MM:SS.SSS` and parsed to float on every wire response; now stored
  directly as seconds-of-day since 00:00 local and read without
  conversion. Existing v0.1.x / v0.2.x databases are migrated in-place
  by `db/migrations.py` (idempotent, malformed rows become NULL with a
  single warning). Original VARCHAR `HH:MM:SS.SSS` literals are not
  preserved through the migration; re-import from `export.xml` if you
  need literal-form fidelity. `run_custom_query` callers no longer see
  the raw VARCHAR. Per the SemVer Layer 1/2 split (PR #107), DB schema
  changes ship as minor / pre-release bumps rather than majors.
- **LP install step now points at the GitHub Releases latest page**
  (issue #111). The Claude Desktop install step previously hard-coded
  the v0.2.0 bundle filename, which would 404 the moment the v0.3.0
  stable tag lands and the LP footer auto-syncs. The step now links
  to <https://github.com/rinoshiyo/apple-health-mcp-server/releases/latest>
  in both `docs/i18n/en.json` and `docs/i18n/ja.json` so the install
  link never goes stale, regardless of release cadence. Note that
  GitHub's `/releases/latest` skips pre-releases by design, so during
  rc windows the LP install step keeps pointing at the previous
  stable release. Layer 2 (LP copy / install ergonomics), not a
  wire-contract change.

## [0.3.0-rc1] - 2026-06-25

First pre-release of the v0.3.0 cycle. v0.3.0 batches the breaking
changes that came out of the v1.0.0 commitment audit so the eventual
v1.0.0 release can freeze the public API surface (MCP tools / CLI /
env vars / DB schema / exit codes) under SemVer with no further
breakage required. Treat this rc as the dogfood baseline; the final
v0.3.0 will ship after stability is confirmed.

### Breaking

- **`list_record_types` response field `type` is now `record_type`**
  (issue #91, audit T1). The new name matches the canonical key used
  across every other tool that exposes a record-type identifier.
  Clients reading `row["type"]` must switch to `row["record_type"]`.
- **`get_record_statistics` rejects unknown `period` values** (issue
  #92, audit T3). Previously an unrecognised string silently fell back
  to `day`, masking client typos. Invalid values now produce an
  explicit error listing the accepted set (`day`, `week`, `month`,
  `year`).
- **`get_workout_details.workout` and `get_activity_summaries` no
  longer return `SELECT *` rows** (issues #93 / #94, audit T5 / T6).
  The internal `import_id` column has been dropped from both
  responses; the remaining columns are listed explicitly so future
  schema additions cannot leak through.
- **`get_workout_route` now returns a pagination envelope** (issue #95,
  audit T7). The bare array `[{latitude, ...}, ...]` is replaced by
  `{points: [...], total: N, has_more: bool, next_offset: int | null}`
  so clients can detect the end of the route without polling for an
  empty response.
- **`get_heart_rate_samples.sample_time` is now a float-seconds offset**
  (issue #96, audit T8). The raw `HH:MM:SS.SSS` VARCHAR is preserved
  in storage for round-trippability, but the tool normalises it to
  seconds-from-the-parent-record's-start on the way out so LLMs do not
  have to reason about Apple's wall-clock formatting.
- **`get_ecg_data.reading` returns explicit columns** (issue #98,
  audit T12). The internal `import_id` is dropped from the wire format
  and the legacy "earlier versions had sample_count at top level"
  description note is removed (v0.3.0 is the SemVer baseline; pre-0.3
  callers are not supported).
- **Logging environment variables now carry the `APPLE_HEALTH_`
  prefix** (issue #101, audit ENV1). `LOG_LEVEL` and `LOG_FORMAT` are
  replaced by `APPLE_HEALTH_LOG_LEVEL` and `APPLE_HEALTH_LOG_FORMAT`
  so multiple MCP servers sharing one shell environment cannot
  clobber each other's settings.

### Added

- **`list_ecg_readings` accepts an optional `limit` parameter** (issue
  #97, audit T11). Defaults to 100, capped at 1000, matching the
  signatures of every other `list_*` tool.
- **`SECURITY.md`** (issue #88, mandatory PR-B). Introduces a private
  vulnerability reporting channel via GitHub Security Advisory; the
  README's existing "Security exception" pointer now resolves to a
  concrete intake.
- **Compatibility exclusions for log-line format and MCP tool
  description text** (issues #89 / #90, mandatory PR-B). README.md and
  README.ja.md Compatibility section explicitly carve these
  human-facing surfaces out of the SemVer contract so future
  improvements to progress prose or LLM-facing tool descriptions do
  not trigger major bumps.
- **Release workflow pre-release detection** (issue #87, PR-C). Tags
  carrying a `-` suffix (e.g. `v0.3.0-rc1`) are now flagged as
  GitHub Release "Pre-release" and skip the LP footer version sync.
  Stable tags (`v1.0.0`) keep the previous "Latest" + LP-sync
  behaviour.
- **MCPB bundle args pin** (issue #78, mandatory PR-B). The release
  workflow rewrites the bundled manifest's `mcp_config.args` to
  `["--from", "apple-health-mcp-server==<tag-version>",
  "apple-health-mcp-server", "serve"]` so a downloaded `.mcpb`
  stays faithful to its tag and is not silently upgraded by uvx's
  cache-refresh path. The `--from` form is the spelling that
  forces uvx to honour the pin (see project memory entry on the
  2026-06-23 cache-drift incident).

### Changed

- **`run_custom_query` and `get_import_history` descriptions match
  the current schema** (issues #99 / #100, audit T13 / T15). The
  table list in `run_custom_query` now includes `workout_metadata`,
  `correlation_members`, `me_attributes`, and `export_metadata`; the
  `get_import_history` description now mentions `export_xml_sha256`
  (added in #62). These are description-only edits; the wire shapes
  were already correct.
- **DB schema gains explanatory SQL comments** (issue #102, audit
  DB1+DB2). `records.value` / `records.text_value` are annotated with
  the numeric-vs-text duality rule; `workouts.total_distance` /
  `total_energy_burned` are annotated with the iOS 10 vs iOS 11+
  back-fill behaviour. Comments only — no schema mutation.
- **Compatibility section reorganised into a two-tier contract**
  (PR-A post-merge follow-up). The DuckDB schema (table / column /
  type / NOT NULL constraints) and the default DuckDB file path are
  reclassified as **Layer 2 (best-effort, may change in a minor
  release when called out under `Changed` in CHANGELOG.md)**; the
  wire-facing surfaces (MCP tool names / signatures / response
  fields, CLI subcommands and flags, env var names and parsing rules,
  exit codes, `__all__` exports) keep the strict **Layer 1** SemVer
  contract. `run_custom_query` users are explicitly noted as Layer 2
  consumers. This is a documentation-only re-framing — no behavioural
  change ships under this entry — and it preserves room to evolve
  storage internals without major bumps that would otherwise be
  forced by treating every schema rename as a wire-breaking change.
- **`get_import_history` now selects explicit columns** (PR-A
  post-merge follow-up). Matches the audit-batch principle already
  applied to `get_workout_details` (T5), `get_activity_summaries`
  (T6), and `get_ecg_data` (T12): a future
  `ALTER TABLE imports ADD COLUMN` cannot leak into the wire shape
  without a deliberate description + projection update. The response
  fields stay identical to v0.3.0-rc1.
- **`get_heart_rate_samples.sample_time` description fixed** (PR-A
  post-merge follow-up). The tool's description now accurately
  documents that `sample_time` is wall-clock seconds since 00:00
  local (e.g. `28800.0` = 08:00:00), not a relative offset from the
  parent record's `start_date`. The numeric value already matched
  this contract; only the description was wrong, which would have
  caused HRV calculations (RMSSD, pNN50) to be off by the parent
  record's wall-clock offset.
- **`get_workout_route` and `list_ecg_readings` reject `limit < 1`**
  (PR-A post-merge follow-up). Previously `limit=0` clamped to 0 and
  returned an empty page; for `get_workout_route` that combined with
  `has_more=True` to create an infinite pagination loop, and for
  `list_ecg_readings` it returned an empty list a client could mistake
  for "no recordings". Both now return `Error: limit must be >= 1`.
- **`get_record_statistics` no longer echoes the rejected `period`
  value in its error string** (PR-A post-merge follow-up). The error
  now reads `Error: invalid period; accepted values: day, week, month, year`
  without interpolating the user-supplied value, closing a small
  prompt-injection vector where a control-character `period` would
  round-trip into the caller LLM's context as trusted server output.
- **`get_workout_route` normalises gate-probe failures to `Error:`
  strings** (PR-A post-merge follow-up). `require_imports_or_message`
  now runs inside the tool's `try` block so a lock contention or DB
  read failure on the empty-DB gate cannot leak a raw traceback up
  through FastMCP. Brings the tool in line with the 16 other tools
  that funnel through `run_query`.

### Verification needed at first stable v0.3.0 push

- LP install snippet in `docs/i18n/{ja,en}.json#desktop_step1`
  hardcodes `apple-health-mcp-server-v0.2.0.mcpb`; `sync_docs_version`
  currently only rewrites `footer.version`. v0.3.0-rc1 sidesteps this
  because pre-release tags skip the sync job entirely, but the
  v0.3.0 final tag will surface the drift if not fixed beforehand.
  Tracked as a dogfood-period follow-up.

## [0.2.0] - 2026-06-24

### Added

- **One-click Claude Desktop install via MCPB bundle (issue #71).**
  Each GitHub Release now attaches an `apple-health-mcp-server-vX.Y.Z.mcpb`
  bundle (Model Context Protocol Bundle, the successor to DXT) alongside
  the existing PyPI wheel/sdist. Users can drag-and-drop the `.mcpb`
  onto Claude Desktop's Connectors panel instead of editing
  `claude_desktop_config.json` by hand. The bundle wraps the same
  `uvx apple-health-mcp-server serve` invocation as the manual JSON
  path, so it still requires `uv` on `PATH`.
- **README "Locales" and "Compatibility" sections (issue #71).** Documents
  ECG header locale coverage (English + Japanese verified;
  Chinese/Korean best-effort) and the SemVer-from-v1.0.0 public-API
  contract that will govern future releases. Includes a deprecation
  cadence (CHANGELOG `Deprecated` heading → at-least-one-minor grace
  period → next-major removal) so the 1.0 promise has operational rules.

### Changed

- **`apple_health_mcp.__version__` now reads from
  `importlib.metadata.version("apple-health-mcp-server")`** instead of
  a hard-coded literal, eliminating the drift that had it stuck at
  `0.1.0` through six releases. Consumers who imported `__version__`
  to gate behaviour on the running release see the real version
  starting with this release.
- **ECG importer raises `LocaleUnrecognisedError` (subclass of
  `HealthImportError`) when no locale headers match**, with an
  actionable message that lists supported locales and points at the
  issue tracker. `import_ecg_files` rate-limits the verbose guidance
  to one full emission per import run; subsequent failing files in
  the same batch get a short reference back.

## [0.1.6] - 2026-06-24

### Changed

- **`apply_pending_migrations` is now atomic (issue #62 follow-up).**
  The migration loop, the `schema_version` stamp, and the COMMIT itself
  run inside a single DuckDB transaction. A crash / SIGKILL / OOM /
  Python exception during the loop -- including a failed COMMIT --
  triggers ROLLBACK so the on-disk schema and the `schema_version`
  sentinel can never diverge. `BaseException` is caught so
  KeyboardInterrupt and SystemExit also roll back. Today's only
  registered migration is an idempotent `ADD COLUMN IF NOT EXISTS`
  (a partial-then-retry would converge anyway), but the transaction
  wrap is the load-bearing safety the next non-idempotent migration
  (backfill, row rewrite, etc.) will rely on. (#65)

- **`--force` now bypasses ONLY the Tier 1 sha256 fast path; the Tier 2
  incremental hash-set skip stays active (issue #62 follow-up).** The
  initial #62 implementation had `--force` bypass both tiers, which made
  the flag re-import every row through the legacy Phase 4 dedup pipeline
  and reintroduced the DuckDB MVCC tombstone balloon on the on-disk file.
  There was no useful reading of "re-import this data but pay the
  on-disk tombstone cost"; the right semantic is "the file is byte-
  identical but I want to re-run the importer anyway". With the new
  scope the `--force` re-import on the maintainer's 1.2 GB export drops
  from ~142 s + 1.2 GB on disk to ~90 s (Phase 1 still parses every
  Record, but every hash hits the existing-set so INSERTs ~0 and Phase
  4 auto-skips). The fresh-import path is unchanged: an empty
  ``imports`` table still leaves ``existing = None`` so the legacy
  full-insert + Phase 4 dedup branch runs (no-op dedup, but the same
  code path).

### Added

- **Incremental re-import via `export.xml` sha256 fast path and an
  in-memory existing-hash snapshot (issue #62).** Re-importing the same
  Apple Health export over an existing DB is now near-instant when
  nothing has changed and orders of magnitude faster when only the
  trailing few days differ:
  - **Tier 1** stamps the sha256 of `export.xml` on every successful
    import. When a subsequent import sees the most recent stamped row
    match the incoming file byte-for-byte, the orchestrator logs
    `Skipping import: export.xml is byte-identical ...` and exits in
    roughly one disk-read of wall-clock without parsing the file or
    touching the DB. `--force` on the `import` subcommand bypasses
    ONLY this check (see the Changed entry above for the final
    `--force` scope).
  - **Tier 2** loads every dedup-keyed hash currently on disk
    (`record_hash`, `workout_hash`, `point_hash`, `ecg_hash`,
    `correlation_hash`, and the `activity_summaries.date_components`
    natural key) into Python sets at import start. Every XML / GPX /
    ECG handler checks the freshly-computed hash against the set
    BEFORE staging the row, so the new import contributes only
    genuinely-new rows. Phase 4 dedup auto-skips because the bulk
    staging buffers carry no duplicates -- this also avoids the
    DuckDB MVCC tombstones that were ballooning the on-disk file by
    ~120% on every re-import under the legacy path. A skipped Workout
    still updates `stats.workout_route_map` so the GPX importer
    computes point hashes with the correct workout component and
    hits the existing-point set.
  - Schema migration v1 → v2 adds `imports.export_xml_sha256`
    (nullable). Existing rows backfill `NULL`; the sha256 fast path
    filters `IS NOT NULL` so pre-#62 rows are simply skipped over
    and the next import stamps a real hash. (#62)

### Changed

- **Phase 4 dedup avoids the full-table rewrite (issue #60).** The
  18 `_DEDUPLICATE_SQL` blocks in `db/schema.py` were rewritten from
  `CREATE OR REPLACE TABLE foo AS SELECT DISTINCT ON (key) * FROM foo`
  (which copied every row of every table on every import, even when
  duplicates were zero) to a targeted
  `DELETE FROM foo WHERE rowid IN (... ROW_NUMBER() OVER (PARTITION BY
  key ORDER BY <same tie-breakers>) > 1 ...)` per table. Surviving-row
  semantics are preserved byte-for-byte (the partition + tie-breaker
  ordering mirrors each block's legacy `ORDER BY`). On a fresh import
  with a unique `import_id` the DELETE writes nothing -- only a
  partition scan -- where the legacy form paid the cost of a full
  rewrite of every table. The constraint-repair block
  (`_RESTORE_CONSTRAINTS_SQL`, issue #44) is now gated by
  `_legacy_schema_needs_constraint_repair` so it only fires as a
  one-shot migration on a pre-#44 on-disk DB; on a post-#60 DB the
  ALTERs would otherwise raise `DependencyException` against the
  indexes the historic `CREATE OR REPLACE TABLE` used to drop.

- **XML parse switches to `lxml.etree.XMLParser(target=...)` SAX target
  (issue #57, middle tier of #55).** The old `iterparse(events=("start",
  "end"))` pass built an `Element` for every one of the ~8 M element
  events a 1.2 GB export generates, then immediately tore it down with
  `elem.clear()` + a prev-sibling drop loop. The SAX target hands the
  importer `start(tag, attrib)` / `end(tag)` callbacks directly and
  never materialises any `Element` -- no tree, no clear, no sibling
  drop, no `elem.attrib` snapshot crossing the lxml C boundary.
  `parser_bench.py` measured the SAX target at ~1.57x of iterparse on
  the maintainer's real export. The Phase-1 progress emitter moved
  from per-event cadence to per-chunk cadence (1 MB chunks); the
  consecutive-error budget that the iterparse loop enforced is now
  enforced inside the SAX target adapter (`_SaxTarget._note_error`).

- **Faster XML import on real exports (issue #56, minimal tier of #55).**
  Four mechanical wins on the XML hot path with no architectural
  rewrite and no new runtime dependency:
  - `normalize_apple_offset` now takes a string-slice fast path for
    Apple's two well-formed offset shapes (`" +HHMM"` and `" +HH:MM"`)
    and falls back to the regex only for the legacy / malformed
    inputs. py-spy attributed ~15% of Phase 1 (~24 s on a 1.2 GB
    export) to the regex; the fast path skips it for the common case.
  - `bulk_load_via_arrow` builds one `pa.array` per column and feeds
    them to `Table.from_arrays`, skipping the intermediate `dict[str,
    list]` `Table.from_pydict` materialised. Microbench: +11% build
    throughput on a 100k-row records flush.
  - The hot per-element handlers (`_handle_record`, the workout /
    correlation / activity / metadata / BPM / route start handlers)
    snapshot `elem.attrib` once instead of crossing the lxml C
    boundary on every `elem.get(...)` call.
  - The three high-volume tables (`records`, `record_metadata`,
    `heart_rate_samples`) flush at 250 000 rows instead of 100 000,
    saving DuckDB INSERT round-trip overhead. Peak Python RSS rises
    by ~150 MB on the records run, well under the 1 GB budget.

## [0.1.5] - 2026-06-23

### Changed

- **`apple-health-mcp-server import` is dramatically faster on real
  exports.** The XML / GPX / ECG importer flush path now routes
  batches through a registered `pyarrow.Table` (issue #50). The
  previous v0.1.3-era `COPY FROM CSV` tempfile path threw away every
  cycle on per-row `csv.writer.writerow` calls, `NamedTemporaryFile`
  writes, and DuckDB's CSV auto-detector. The Arrow path builds the
  columnar buffer once per batch and hands DuckDB the same shape its
  internal storage uses, so the per-batch CSV serialise + tempfile +
  COPY round-trip the legacy helper paid is gone. PyArrow is added
  as a runtime dependency (~30 MB wheel); a unit test guards that
  it stays out of the `serve` import graph so MCP startup latency
  is unaffected. The Arrow path also drops the historical
  `_NULL_SENTINEL` collision check -- Arrow distinguishes `null`
  from the literal string `"\N"` natively. (#50)

### Added

- **Phase-1 progress log lines during `import`.** A streaming agent
  or human watching `apple-health-mcp-server import …` no longer
  sees a multi-minute silent stretch during the XML parse. Every
  10 seconds (configurable via `APPLE_HEALTH_IMPORT_PROGRESS_SECS`,
  clamped to 1..600) the importer emits a single newline-terminated
  `INFO  progress: xml NN% (X / Y MB, ~Z min remaining)` line on
  stderr. No `\r` carriage return, no ANSI cursor escapes, so the
  output stays readable when piped through `tee`, captured by CI,
  or buffered by an LLM agent. Sub-megabyte exports skip the
  emitter -- the phase markers already announce start + completion
  in that regime. (#51)

### Fixed

- **Date-only `end_date` filters are now inclusive of the named day.**
  All 5 date-filtered tools (`query_records`, `list_workouts`,
  `list_ecg_readings`, `list_state_of_mind`, `list_correlations`)
  previously cast a bare `YYYY-MM-DD` upper bound to `TIMESTAMPTZ` at
  start-of-day, silently dropping every record that happened after
  midnight on the named day. They now route the upper bound through a
  shared `normalise_end_date` helper that expands a bare date to
  `YYYY-MM-DD 23:59:59.999999` so the `<=` comparison includes the
  whole day. Full ISO 8601 timestamps (`YYYY-MM-DDTHH:MM:SS+HH:MM`)
  pass through unchanged so the caller's precision is respected.
  User-visible effect: `query_records(record_type=…,
  start_date='2026-06-22', end_date='2026-06-22')` now returns that
  day's records instead of zero rows. Pre-existing since v0.1.0. (#49)

## [0.1.4] - 2026-06-23

### Fixed

- **`imports.imported_at` now populates on every import.** Previously
  the column wrote as `NULL` because `deduplicate_tables()` rebuilds
  each table via `CREATE OR REPLACE TABLE ... AS SELECT ...`, and
  DuckDB does not carry NOT NULL / DEFAULT / CHECK constraints
  through `CREATE TABLE AS SELECT`. With the source schema's
  `DEFAULT CURRENT_TIMESTAMP` silently stripped, the orchestrator's
  `INSERT INTO imports (...)` that omits `imported_at` left it
  unset. The fix re-applies every NOT NULL constraint and the
  `imported_at` default with a metadata-only `ALTER TABLE` pass
  after dedup. User-visible effect: `get_import_history` now
  returns a real `imported_at` timestamp instead of `NULL`, so the
  tool's documented `ORDER BY imported_at DESC` is finally
  meaningful. The same dedup bug was silently dropping NOT NULL
  constraints on 17 other tables; no current code path INSERTs into
  those tables post-dedup so the loss was latent, but the
  restoration pass covers them defensively to prevent future drift.
  Pre-existing in every prior release (Rust upstream included).
  (#44)

## [0.1.3] - 2026-06-23

### Changed

- **`apple-health-mcp-server import` is dramatically faster** for real
  exports. The XML / GPX / ECG importers now route their flush path
  through DuckDB's `COPY FROM CSV` (via a per-batch tempfile) instead
  of `executemany("INSERT INTO ... VALUES (?, ...)")`. Measured throughput
  on a synthetic in-memory benchmark: ~325× speedup (~300 rows/s →
  ~100 000 rows/s); on the maintainer's real ~1.2 GB `export.xml`
  (2.6M records / 350 workouts / 325k GPX route points / 7 ECGs /
  1.5M metadata entries), an import that did not finish in 20 minutes
  under v0.1.2 now completes in **194 seconds end-to-end** (~3 min
  wall-clock, vs the Rust reference's 67-73 s — the remaining ~3×
  gap is the per-batch CSV serialise+write+COPY overhead, which
  cannot be flattened without adding pandas / pyarrow as a runtime
  dependency). (#41, #42)
- `run_import` now issues `PRAGMA preserve_insertion_order = false`
  for the import session — the bulk-load path is unordered by design
  and downstream queries always `ORDER BY` anyway, so the per-row sort
  during checkpoint is pure waste.

### Fixed

- ECG sample parser now rejects non-finite voltages (`inf`, `-inf`,
  `nan`) so a single malformed line does not fail the entire
  bulk-load `COPY` for that ECG file. Matches the rejection that the
  XML and GPX float parsers already enforced.

## [0.1.2] - 2026-06-22

### Fixed

- `apple-health-mcp-server serve` now starts successfully against a
  fresh machine that has never run `import` instead of exiting with
  "database does not exist". The MCP client sees the full tool list as
  usual, and every data-bearing tool returns a single guidance string —
  exposed as `apple_health_mcp.server.query.IMPORT_REQUIRED_MESSAGE` —
  pointing at the missing import step:

  ```
  Error: No Apple Health data has been imported yet.
  Run `apple-health-mcp-server import <export-dir>` to ingest your
  export, then restart this MCP server. See
  https://github.com/rinoshiyo/apple-health-mcp-server#usage for details.
  ```

  Two tools opt out: `get_import_history` returns an empty list on an
  empty DB so callers can confirm the empty-DB state, and
  `run_custom_query` stays callable so an LLM can introspect the
  freshly-bootstrapped scaffold (e.g. `SELECT COUNT(*) FROM imports`).
  Consumers parsing tool errors should anchor on the message prefix
  (the trailing URL may change between minor versions). The bootstrap
  itself is atomic (per-PID temp file + `os.replace`) so a crash
  mid-DDL leaves no half-initialised file the next run would mistake
  for a real DB, and concurrent `serve` processes race-safely. A
  WARNING is logged when the bootstrap fires so a typo'd `--db` does
  not silently masquerade as a successful install. README EN/JA gain
  a Troubleshooting section documenting the path. (#38, #39)

## [0.1.1] - 2026-06-22

### Added

- README install instructions (EN/JA) for **Claude Code** (`claude mcp
  add` CLI + `.mcp.json` / `~/.claude.json` manual snippets) and
  **Codex CLI** (TOML-based `~/.codex/config.toml`), each with the
  reload semantics, scope notes, and an official-doc source URL with
  fetch date. (#35)

## [0.1.0] - 2026-06-22

### Added

- **PyPI Trusted Publishing release workflow.** Pushing a
  `v<MAJOR>.<MINOR>.<PATCH>` tag builds the sdist and wheel, verifies
  metadata with `twine check --strict`, and publishes to PyPI via
  OIDC. The v0.1.0 tag is pushed manually by the maintainer after the
  release branch is merged. (#20)
- **Project bootstrap.** `pyproject.toml` targeting Python 3.12+ with
  `uv`, `ruff`, `mypy --strict`, `pytest --cov-branch --cov-fail-under=100`,
  and `pre-commit` wired up. (#1, #2, #3, #4)
- **DuckDB schema and connection layer** ported from the Rust reference
  implementation, including deterministic deduplication and derived
  daily-stats rebuild. (#9, #10)
- **Importers** for Apple Health XML, ECG CSV (multi-locale), and GPX
  workout routes, orchestrated through
  `apple_health_mcp.importers.run_import`. Time-zone alignment between
  XML (local wall-clock) and GPX (true UTC) is preserved via per-workout
  offsets. (#5, #6, #7, #8)
- **FastMCP server with 17 read-oriented tools**:
  `list_record_types`, `query_records`, `get_record_statistics`,
  `list_workouts`, `get_workout_details`, `get_activity_summaries`,
  `get_workout_route`, `get_heart_rate_samples`, `list_correlations`,
  `get_correlation_details`, `list_ecg_readings`, `get_ecg_data`,
  `run_custom_query`, `list_data_sources`, `get_import_history`,
  `list_state_of_mind`, `get_me_attributes`. (#11, #12, #13, #30)
- **Test fixtures and end-to-end integration smoke** under
  `tests/fixtures/` and `tests/integration/`, plus per-matrix-cell
  coverage XML and a single canonical HTML coverage artifact in CI.
  (#14, #15, #16)
- **Documentation**: English and Japanese READMEs, this changelog, and
  `CLAUDE.md` covering architecture, dev commands, conventions, domain
  rules, language policy, release operations, and the mandatory
  `/code-review --fix` policy for every PR. (#17, #18, #19)
- **CLI `import` subcommand** wired to `importers.run_import` so
  `apple-health-mcp-server import <export>` actually ingests data
  before `serve` is started. (#27)
- README "Updating" section (EN/JA) explaining how to refresh the
  `uvx`-cached package and how to pin a specific version in the Claude
  Desktop / Codex / Cursor configuration. (#33)

### Changed

- **Time-zone handling: every timestamp column is `TIMESTAMPTZ`.**
  Apple Health XML offsets are normalised to ISO 8601 `+HH:MM` form and
  fed straight to DuckDB; GPX `Z`-suffixed timestamps land as true UTC
  instants. New `--tz` CLI flag and `APPLE_HEALTH_TZ` env var override
  the session TZ used to render TIMESTAMPTZ on read; the OS local TZ
  remains the default. Required `pytz` as an explicit dependency
  because DuckDB's Python binding lazily imports it for TIMESTAMPTZ ->
  tz-aware `datetime` materialisation. (#29)
- **Wire-format: tool responses include a UTC offset suffix on
  datetime fields** (e.g. `"2024-01-01 10:00:00+00:00"` instead of the
  previous `"2024-01-01 10:00:00"`). Consumers that pinned the previous
  19-character fixed-width form should update their parsers to ISO
  8601. (#29)

[Unreleased]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rinoshiyo/apple-health-mcp-server/releases/tag/v0.1.0
