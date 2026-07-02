# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
from v1.0.0 onward — see the README's "Compatibility" section for the
v0.x.y disclaimer and the public-API scope.

## [Unreleased]

## [0.6.1] - 2026-07-02

### Added

- `run_custom_query` now translates raw DuckDB engine exceptions
  (`CatalogException` → `unknown_table` / `unknown_view`,
  `BinderException` → `missing_column`, `ParserException` →
  `syntax_error`) and `QueryValidationError` sub-reasons
  (`empty_query`, `not_select_or_with`, `multi_statement`,
  `disallowed_function`, `syntax_error`) into typed envelopes
  matching the shape `{state:"error", reason, message, hint?}`
  (issue #273). This completes issue #227 — v0.6.0 shipped only
  the import-path translator; the query-path was still returning
  raw `f"Error: {exc}"` strings until this release. Envelope
  hints include `available_tables` (from
  `information_schema.tables`), `did_you_mean` (parsed out of
  DuckDB's own suggestion), and per-referenced-table
  `available_columns` (from `information_schema.columns`) —
  filling in the column list DuckDB's own "Candidate bindings"
  diagnostic truncates.

## [0.6.0] - 2026-07-02

### Changed

- **BREAKING: unified the state-machine error envelope's `reason`
  field to enum-style identifiers across all three error states**
  (issue #196). `NEEDS_CONFIG.reason` changes from the prose string
  `"APPLE_HEALTH_EXPORT_ZIPS_DIR is not set"` to `"env_unset"`;
  `NEEDS_IMPORT.reason` changes from
  `"no successful Apple Health import found in this database"` to
  `"no_imports"`. `NEEDS_REIMPORT.reason` was already `"schema_outdated"`
  (v0.5.1) and is unchanged. Agents that branched on `reason` via
  substring matching must switch to exact-match comparison; the
  human-readable explanation (env var name, recovery steps) still
  lives in `human_message`, unchanged.
- `APPLE_HEALTH_EXPORT_ZIPS_DIR` is now resolved to an absolute path
  before use (issue #226). A relative or `~`-prefixed value is
  expanded and absolutised via `os.path.abspath` +
  `Path.expanduser`, so envelopes (`list_zips.export_zips_dir`,
  `import_zip` error messages) always surface the fully-resolved
  path instead of the raw string a user might have typed. A
  logger warning fires on any relative input so an operator who
  intended an absolute path can see the fall-through immediately.
- `import_zip` prose in module docstrings, DESCRIPTION strings,
  and the multi-launch queued envelope now consistently uses the
  on-wire terminal status `"ok"` where it previously said `"done"`
  (issue #249). The internal `import_jobs.status` DB column value
  remains `"done"` — the mismatch between DB state and wire status
  is tracked at #257 for a follow-up alignment. Agents that were
  instructed to poll `get_import_status` "until `done` or `error`"
  now see the correct terminal `"ok"` in the message they read.
- Async import polling prose consolidated into two shared
  constants (`IMPORT_POLL_BLURB`, `IMPORT_RUNTIME_BLURB`) applied
  across `list_zips` hint, `import_zip` DESCRIPTION + queued
  envelopes, and `get_import_status` DESCRIPTION (issue #194). A
  cadence or hardware baseline change is now a one-file edit that
  reaches every agent-visible surface — the v0.4→v0.5 drift where
  `list_zips` lagged at `60 seconds` while every other tool moved
  to `10-30 seconds` (issue #187) cannot recur.

### Added

- Import path (`import_zip` via
  `orchestrator._translate_conversion_error`) now translates raw
  DuckDB `ConversionException` errors into typed envelopes with
  a human-friendly root cause (issue #227, partial). The query
  path (`run_custom_query`) is NOT translated in this release
  and continues to return raw `f"Error: {exc}"` strings; that
  gap is closed in v0.6.1 by issue #273.
- `import_zip` clamps the caller-supplied `id` echoed back in the
  `invalid_id` error envelope to the argument's declared
  `max_length=64`, with a `...` suffix on overflow (issue #228).
  Real MCP calls cannot reach the truncation path because
  FastMCP's `Field(max_length=64)` rejects oversized inputs at
  the boundary; the cap is defence-in-depth for unit tests and
  any future regression of the boundary constraint.
- `block_if_schema_outdated` memoises "fresh" decisions in a
  `WeakSet` keyed on the DB connection so long-running polling
  loops (`get_import_status` every 10-30 seconds) no longer
  re-probe the schema-version sentinel on every call (issue
  #197). A 10-minute import that polls twice a minute saves
  ~30 DuckDB roundtrips. Only the "fresh" verdict is cached —
  the orchestrator's mid-flight `reset_db_for_fresh_import` on a
  stale schema stays observable.
- Two decorator helpers, `schema_gated_tool` and
  `ready_gated_tool`, register MCP tools with the correct
  data-state gate injected at registration time (issue #198).
  Adding a new tool now inherits the correct gate by picking the
  decorator, rather than remembering to hand-write the first-
  statement guard clause; the two regressions the guard-clause
  pattern invited (v0.5.0 dogfood's raw
  `Catalog Error: Table import_jobs does not exist`) are
  structurally prevented at the type level.

### Refactor (internal)

- Folded the hand-rolled `_table_exists_in_main_conn` in
  `db.connection` into the sole `table_exists_in_main` in
  `db.migrations` and dropped the leading underscore now that
  three modules import the helper across package boundaries
  (issue #199). Future `v=7`-era catalog probes have a single
  helper to reuse.
- Test connections now apply the production
  `_set_engine_safety_pragmas` sequence at every fixture open
  through `tests/_helpers.open_test_connection` /
  `open_test_memory_connection` (issue #201). Test fixtures no
  longer diverge from production's engine-level lockdown
  (`enable_external_access=false`, `lock_configuration=true`,
  ...), so a change that would break at runtime cannot silently
  PASS the suite.
- README's async-import flow diagram + prose updated to reflect
  the v0.5 `queued` → `ok` state machine and the `job_id`
  polling contract (issue #193).

## [0.5.1] - 2026-06-29

### Security

- **Lock down all external resource access at the DuckDB engine level
  via `SET enable_external_access = false`** (issue #190, v0.5.0
  adversarial test stop-ship). v0.5.0 dogfood (`tmp/v0-5-0-adversarial-results_1.md`)
  found that the SQL safety validator's function-name denylist had
  alias / near-relative blind spots — `parquet_scan`, `parquet_metadata`,
  `parquet_schema`, and `sniff_csv` all bypassed the denylist and let
  `run_custom_query` read arbitrary host files (`sniff_csv` returned
  the file contents directly), while `parquet_scan('https://...')`
  fetched remote URLs and exfiltrated their content — breaking the
  project's "all data stays local, no external send" privacy contract
  at the implementation level. The fix is a single engine setting that
  forbids the entire family of file / network / extension functions
  (including future aliases the team has not enumerated), ATTACH /
  COPY / INSTALL / LOAD, and httpfs / S3 / GCS / Azure FileSystem
  egress. The setting is applied in both writable and read-only
  serve paths and on the in-memory test connection so adversarial
  tests run against the production contract. The importer pipeline
  is unaffected: bulk ingestion routes through PyArrow `conn.register
  → INSERT ... SELECT * FROM __bulk_arrow` and never touches the
  engine's external-fs surface.
- **Defense-in-depth: extend the function denylist with the missing
  aliases** (`parquet_scan`, `parquet_metadata`, `parquet_schema`,
  `sniff_csv`). The engine-level lockdown is the root-cause fix; the
  denylist now produces a friendlier parse-time `Function 'X' is not
  allowed` error than DuckDB's downstream `IO Error` on the same
  call. Future deliberate re-enables of `enable_external_access`
  still hit the denylist first.
- **New `tests/integration/test_security.py` adversarial suite**
  exercises both layers end-to-end: parametrised engine-level
  rejections for every fs table function (including the post-#190
  aliases), HTTPS / S3 / loopback URL fetches, file-backed
  ATTACH / INSTALL / LOAD, and a `current_setting('enable_external_access')`
  invariant probe so a future regression of the connection-open
  helper that silently drops the SET statement surfaces at CI rather
  than at the next dogfood. The denylist still does its parse-time
  job through the existing `tests/unit/server/test_safety.py`
  parametrised pin (the new aliases are picked up automatically by
  `pytest.mark.parametrize("fn", sorted(DENIED_FUNCTIONS))`).

### Added

- The v0.4 ZIP-flow write tools (`list_zips`, `import_zip`,
  `get_import_status`) now short-circuit on a pre-v0.5 DB with a typed
  `schema_outdated` envelope (`state: 'NEEDS_REIMPORT'`,
  `reason: 'schema_outdated'`). v0.5.0 dogfood found that opening a
  v=5-or-earlier DB against a v0.5.0 server let `import_zip` advance
  to the `INSERT INTO import_jobs` step and surfaced a raw DuckDB
  `Catalog Error: Table import_jobs does not exist!` before the
  tool's own error handling could fire. The new gate routes the agent
  at the fresh-reset recovery path documented under
  `[Unreleased]/Changed` (issue #188).
- `check_data_state` additionally detects a populated DB whose
  `import_jobs` table is missing (a v=5-or-earlier shape whose
  `schema_version` row was lost / never observed by
  `schema_version_is_stale`), so the schema_outdated envelope fires
  on both legitimate version-trail DBs and corrupted-stamp variants.

### Changed

- `NEEDS_REIMPORT` envelope's `reason` field tightened from the v0.4
  free-form sentence ("database was imported under an older package
  release; schema_version trails the current package.") to the stable
  enum-style identifier `"schema_outdated"`. The descriptive prose
  moved into `human_message` (which still names both failure modes:
  schema_version trail + missing `import_jobs`). Agents can now
  branch on `payload["reason"] == "schema_outdated"` without a
  fragile substring match.

### Fixed

- `list_zips` no longer returns a v0.4 synchronous-flow `hint` string
  on a populated directory. The new hint steers the agent at the v0.5
  async polling flow (`import_zip` returns `job_id` → poll
  `get_import_status`), matching the actual import surface (issue #187).
- `import_zip`'s `id` argument documentation now matches the
  implementation: 4-64 hex characters, leading/trailing whitespace is
  trimmed, uppercase is normalised to lowercase before lookup. The
  previous "lowercase" / "verbatim" wording in the docstring and
  `invalid_id` message contradicted the tolerant behaviour the
  implementation had always applied (issue #191).

### Changed

- `get_import_history` now reports the run_import body wall-clock as
  `processing_secs` (aliased from the underlying
  `imports.duration_secs` column) so it no longer collides with
  `get_import_status.duration_secs`, which captures the worker-thread
  wall-clock *including* ZIP extraction and can differ by several
  seconds on large ZIPs. The DB column name is unchanged, so
  `run_custom_query` against `imports.duration_secs` continues to
  work — only the MCP tool wire shape changes (issue #189).
- `run_custom_query` now rejects `parquet_scan` / `parquet_metadata` /
  `parquet_schema` / `sniff_csv` (validator denylist) and refuses
  *every* fs / network function plus ATTACH / COPY / INSTALL / LOAD
  at the engine level (`SET enable_external_access = false`). Saved
  queries from v0.5.0 or earlier that pulled in a local parquet file
  via `parquet_scan('/path/to/x.parquet')` now hit
  `Function 'parquet_scan' is not allowed`. See the Security section
  below for the rationale (issue #190).

## [0.5.0] - 2026-06-29

### Added

- **`import_zip` is now job-based async; new `get_import_status` MCP
  tool polls the worker** (issue #157). The v0.4 synchronous shape
  blocked the MCP client for the full XML → ECG → GPX → finalize
  pipeline (44 s on a fast workstation, several minutes on slower
  hardware), tripping the client's tool-call timeout on anything
  but a new high-end machine. v0.5 splits the surface so the call
  cannot deadline-out regardless of import duration:

  - `import_zip(id=...)` returns `{status: 'queued', job_id, id,
    queued_at, message}` in milliseconds after inserting an
    `import_jobs` row and spawning a daemon worker thread. The
    idempotent no-op branch (sha256 already in `imports`) still
    returns the `{status: 'ok', records_added: 0,
    already_imported_at, ...}` envelope synchronously without
    creating a job. Validation / config / invalid-zip /
    not-an-Apple-Health-ZIP / id-not-found errors stay synchronous
    too — only the genuinely-long branch goes async.
  - `get_import_status(job_id=...)` is the new companion tool. Poll
    every 10-30 s; it returns `{status: 'queued' | 'running' (+
    phase + elapsed_secs) | 'ok' (+ records_added / workouts_added
    / ecg_readings_added / route_points_added / duration_secs /
    already_imported_at) | 'error' (+ reason + message)}` from a
    single indexed SELECT against `import_jobs`. Unknown `job_id`
    surfaces as `{status: 'error', reason: 'job_not_found'}`.
  - Tool count: 20 → 21. `manifest.json` long_description,
    README.md / README.ja.md tool tables, and LP `tools.eyebrow`
    are updated.

  **Multi-launch guard.** If a second `import_zip` lands while a
  worker is in flight for the same sha256, the second call returns
  the *existing* `job_id` instead of spawning a duplicate worker
  that would queue on the writer lock and no-op anyway.

  **Orphan recovery.** Server boot runs a one-shot sweep that
  rewrites every `queued` / `running` row to `error` with
  `reason='server_restarted_while_running'`. Without the sweep, a
  worker the OS killed mid-import would wedge the multi-launch
  guard on a phantom job forever.

  **Schema.** Adds `import_jobs` table (and 2 indexes on
  `source_sha256` / `status`) via `CREATE TABLE IF NOT EXISTS`, so
  existing v=5 DBs gain the table on the next server boot without a
  schema_version bump — matching the v0.4.1 (#156) fresh-reset
  contract.

### Changed

- **Retire migration scaffolding from `db.migrations`** (issue #178,
  internal cleanup, wire-facing behaviour unchanged). The
  registry-style `apply_pending_migrations` + the only-ever-registered
  `_add_export_xml_sha256_column` step + the `_reimport_required_message`
  / `_REIMPORT_REQUIRED_TEMPLATE` / `_REGISTERED_TARGETS` / `MIGRATIONS`
  members + the matching v=2/v=3/v=4/v=5 rejection tests were dead
  code by v0.5: v0.3.0 (#124) made fresh-import the upgrade contract,
  and v0.4.1 (#156) `schema_version_is_stale` +
  `reset_db_for_fresh_import` made the ConfigError rejection path
  unreachable too — the read tools surface `NEEDS_REIMPORT` and the
  write tools auto-reset before the next import. `migrations.py`
  shrinks from ~306 lines to ~110, with the remaining surface
  (`CURRENT_SCHEMA_VERSION`, `schema_version_is_stale`,
  `get/set_current_version`, new `stamp_current_version` thin wrapper)
  matching what callers actually use. Orchestrator + bootstrap
  `connection.py` paths now call `stamp_current_version` directly.

### Added

- **`get_workout_route` server-side downsampling + heart-rate join**
  (issues #161 + #162). New optional parameters:
  - `every_nth=N` (issue #161): server-side equispaced downsampling.
    Returns every N-th point ordered by timestamp; N=5 cuts row
    count to ~20%, capped at 1000. `total` reports the downsampled
    count, not the underlying `route_points` row count.
  - `with_heart_rate=True` (issue #162): adds `{heart_rate,
    heart_rate_offset_secs}` to each item via a LATERAL JOIN
    against `records` filtered to
    `HKQuantityTypeIdentifierHeartRate` within ±30 s of the route
    timestamp; `heart_rate` is null when no HR sample falls in the
    window.

  The stride filter runs BEFORE the LATERAL HR-join so the join
  only fires for points actually returned. Both options are
  additive — the default response shape and existing wire contract
  are unchanged.

- **`imports.dedup_skipped BOOLEAN NOT NULL DEFAULT FALSE` column +
  wire field** (issue #163). Distinguishes a clean Tier-1 fresh
  import that found zero Correlation-child duplicates
  (`records_after_dedup == record_count`, `dedup_skipped=false`)
  from a Tier-2 incremental re-import that never measured
  (`records_after_dedup IS NULL`, `dedup_skipped=true`). The NOT
  NULL contract is sound under the fresh-reset rule:
  `CURRENT_SCHEMA_VERSION` bumped 5→6 in the same change, so every
  v=5 DB triggers `schema_version_is_stale` → fresh-reset path,
  and every v=6 row was written by post-#163 orchestrator code.
  `get_import_history` projection and DESCRIPTION extended.
  `tests/unit/db/test_schema.py` pins the column type / NOT NULL /
  DEFAULT shape, and three test sites switched from positional to
  named `INSERT INTO imports` so a future ADD COLUMN bump no longer
  forces a positional placeholder churn (the PR #62 / PR-D #129 /
  #148 pattern that hit the same sites repeatedly).

- **Doc-tests pinning README to runtime constants** (issues #121 +
  #122). `tests/unit/test_docs_in_sync.py` fails CI if the env-vars
  table drifts from `xml._PROGRESS_INTERVAL_{DEFAULT,MIN,MAX}_SECS`
  or if the README forgets the canonical Linux/macOS default DB
  path (`~/.local/share/apple-health-mcp/health.duckdb`). README
  cross-references now route every secondary mention of the path
  back to the § Database location anchor instead of restating the
  string.

- **CI `metadata-checks` job — version parity and LP version-literal
  guards** (issues #115 + #119). Two PR-time gates land in
  `ci.yml`. (1) `scripts/check_version_parity.py` fails if
  `pyproject.toml`, `manifest.json`, and `uv.lock` disagree on the
  project version, replacing the manual three-file bump dance with a
  PR-time failure rather than the post-tag hot-fix path PR #80 had to
  take after v0.2.0. (2) `scripts/check_lp_no_version_literal.py`
  fails if `docs/i18n/{ja,en}.json` embed a `vN.M.K` literal outside
  the sanctioned `footer.version` slot, codifying the 2026-06-25 LP
  grill decision (everywhere else uses `/releases/latest`). The
  convention is also written into `CLAUDE.md` §8 (release ops) and
  §9 (new LP Copy Conventions section).

### Changed

- **Progress emitter log label switched from `MB` to `MiB`** (issue
  #120, behavioural). The Phase-1 progress line previously labelled
  `(X / Y MB, ~Z min remaining)` while computing the values via
  `consumed / (1024 * 1024)` (binary). The label now matches the
  underlying math (`MiB`); the values themselves are unchanged.
  `apple_health_mcp.importers.xml` exposes module-level `MiB` and
  `MB` constants so every "1 MB"-shaped reference now names the
  scale it lives on, and the module docstring carries the convention
  paragraph (binary for memory buffer sizes, decimal for user-facing
  file-size thresholds). README entries that mention the 1 MB skip
  threshold now clarify "1 MB (1,000,000 bytes — decimal)".

### Fixed

- **`run_query_envelope` paging halt when no item fits the size budget**
  (post-PR #175 code-review #1/#2). When the size clamp dropped every
  item (budget < first-item cost), `next_offset` was set to the
  caller's current offset, so an agent paging blindly via
  `next_offset` looped forever on the same empty response. The
  envelope now returns `next_offset: null` in that case so the agent
  surfaces `truncated_by_size` + `size_budget_bytes` to the user and
  halts. This is the v0.4.1 Finding 9 hazard re-promoted from the
  per-tool `_clip_to_size_budget` in `get_workout_route` to the
  shared `run_query_envelope` helper.

### Performance

- **`import_zip` releases the server lock during ZIP extraction**
  (issue #173). Pre-v0.5 the MCP tool wrapped the full
  `extract_zip_and_import` call in `with lock:`, so concurrent
  read tools (`get_workouts`, `list_workouts`, etc.) were blocked
  for the multi-second extraction phase on top of the importer's
  own duration. v0.5 passes the lock INTO the helper and acquires
  it only around the `run_import` call. Read tools now wait only
  for the importer phase.
- **CLI folds inspect + sha256 into a single file open** (issue #174).
  `_zip_util.inspect_and_hash_zip` performs both classification and
  sha256 streaming in one `open()` pass, replacing the previous
  three-pass pattern (`inspect_zip` + `stream_sha256` + extractall).
  Saves ~5-10 seconds of cold-cache I/O per CLI import on multi-GB
  ZIPs. The MCP tool was unaffected — it already had the sha from
  id resolution.

### Changed

- **Size-budget clamp shared across every envelope-shaped read tool**
  (issue #171). The 1 MB host transport ceiling guard previously
  exclusive to `get_workout_route` (v0.4.1) is now inside
  `run_query_envelope`, so `query_records` / `list_workouts` /
  `list_ecg_readings` / `get_heart_rate_samples` / etc. all carry
  `truncated_by_size` + `size_budget_bytes` fields and self-clip
  before the wire response exceeds the host runtime's 1 MB ceiling.
  `next_offset` is set to the resume point when the clamp drops
  items so the caller can page the remainder cleanly. The
  per-tool `_clip_to_size_budget` / `_SIZE_BUDGET_BYTES` constants
  in `get_workout_route` moved to
  `server.query.clip_items_to_size_budget` /
  `DEFAULT_SIZE_BUDGET_BYTES`. `get_workout_route`'s per-field
  rounding (lat/lon 6 digits, etc.) stays a route-only payload trim
  applied via the envelope's `row_transform` hook.

### Breaking

- **CLI `import` subcommand accepts a ZIP path only** (issue #170). The
  argument shape changed from `apple-health-mcp-server import <dir>`
  to `apple-health-mcp-server import <export.zip>`. Pre-v0.5 the CLI
  took a directory (typically `apple_health_export/` after manual
  unzip); v0.5 onward the CLI extracts the ZIP internally so users
  never have to unzip manually. The CLI and the MCP `import_zip` tool
  now go through the same `importers.zip_extract.extract_zip_and_import`
  helper, so both stamp the matching `imports.source_zip_*` triple
  and idempotency works uniformly across CLI / MCP boundaries (the
  pre-v0.5 CLI left `source_zip_sha256 = NULL` and the MCP
  `_find_existing_import` check missed those rows). Existing users
  who run `import <dir>` get a typed exit-1 with a CHANGELOG pointer
  rather than a silent shape mismatch.

### Fixed

- **`get_workout_route` size budget honours indent=2 serialization**
  (post-v0.4.1 code-review #2). `_clip_to_size_budget` was estimating
  per-item byte cost with a compact `json.dumps` call while
  `run_query_payload` actually serializes with `indent=2`, undercounting
  by ~50%. A long-form workout (e.g. `limit=7000`) could pass the
  950 KB clamp with `truncated_by_size: false` and still produce a
  ~1.3 MB wire payload that the host MCP runtime truncated to a
  generic "Tool result is too large" message — the exact failure mode
  issue #160 set out to prevent. The estimator now uses indent=2
  to match the actual wire cost.
- **`import_zip` handles `APPLE_HEALTH_EXPORT_ZIPS_DIR` pointing at
  a file** (post-v0.4.1 code-review #4). The tool now catches
  `NotADirectoryError` and returns a typed
  `reason: "export_zips_dir_not_a_directory"` envelope, matching the
  contract `list_zips` has carried since v0.4.0. Pre-fix the
  exception propagated through `asyncio.to_thread` as an unstructured
  MCP error.

### Internal

- Orchestrator atomicity comment corrected (code-review #5): the
  reset uses its own `BEGIN/COMMIT`, not the importer's autocommit;
  a mid-pipeline crash leaves an empty schema, not a "previously-stale
  DB intact" as the prior comment promised.
- `_CANONICAL_TABLE_NAMES` regex now carries a fragility warning
  (code-review #6) so a future SQL reformat that inserts a comment
  between `IF NOT EXISTS` and the table name doesn't silently
  disable `reset_db_for_fresh_import`.
- `run_custom_query` trailing-line-comment safety regression test
  added on the live envelope path (code-review #8); `enforce_limit`
  docstring clarifies it is now a test-only helper since v0.4.1
  switched to inline `stmt.limit(MAX + 1).sql(...)`.

## [0.4.1] - 2026-06-28

### Documentation

- **README install step links to `/releases/latest`** (issue #114).
  Both `README.md` and `README.ja.md` Claude Desktop install snippets
  now link to the latest-release page that auto-resolves to the
  current `vX.Y.Z`, so the docs and the LP install path stay in sync
  with no copy churn on every release.

### Changed

- **`get_workout_route` payload trimmed and size-clamped** (issue #160).
  Default `limit` lowered from 5000 to 2000 so the typical wire
  payload stays under the host's 1 MB transport ceiling, where the
  previous default could trip a generic "Tool result is too large"
  truncation. Latitude / longitude rounded to 6 decimals (~0.1 m,
  below GPS precision); elevation rounded to 0.1 m; speed to 0.001
  m/s; course to 0.1°. The envelope adds `truncated_by_size` and
  `size_budget_bytes` fields; when the server has to clip the items
  list to stay under the ceiling, `truncated_by_size: true` is set
  and `next_offset` points at the resume location for paging.

### Breaking

- **`run_custom_query` returns an envelope** (issue #159). Previous
  versions returned a bare JSON array; v0.4.1 wraps the result in
  `{rows, row_count, truncated, max_rows, user_supplied_limit}` so
  the caller can detect silent truncation at the server-enforced cap.
  Callers that parsed the result as `list[dict]` need to read
  `.rows`. The server now probes for overflow with `LIMIT MAX+1`, so
  a result that fits exactly at the cap is correctly marked
  `truncated: false`. A caller-supplied `LIMIT` clause turns the
  envelope's `user_supplied_limit` to `true` and `truncated` is
  always `false` (truncation is the caller's concern in that case).

### Added

- **3-state ZIP inspection** (issue #158). `list_zips` now emits a
  `zip_status` field per entry — `valid_apple_health` /
  `valid_non_apple_health` / `invalid_zip` — and `import_zip` returns
  a dedicated `reason: "invalid_zip"` envelope when the file cannot
  be parsed as a ZIP archive at all (corruption, partial transfer, an
  HTML error page renamed to `.zip`). The pre-existing
  `not_apple_health_export` reason now strictly means "valid ZIP, no
  Apple Health marker"; the user-facing recovery action is different
  in each case (re-download vs. pick a different file). The legacy
  `is_apple_health: bool` field is retained for wire compatibility.

### Fixed

- **v0.4 upgrade path no longer wedges the server** (issue #156). A
  database imported under v0.3.x (or any pre-current `schema_version`)
  used to raise `ConfigError` at server boot, telling the user to
  `rm` the file and re-run the CLI. Claude Desktop on Windows hides
  the canonical DB path inside the MSIX AppContainer sandbox, so that
  recovery path was effectively impossible. The server now opens the
  stale DB cleanly and the read tools surface a new `NEEDS_REIMPORT`
  envelope (`{state: "NEEDS_REIMPORT", suggested_action:
  "call_list_zips", ...}`). The next `import_zip` call drops every
  package-owned table and rebuilds the canonical schema before
  re-ingesting — the user never has to touch a terminal.

## [0.4.0] - 2026-06-26

### Headline

- **Terminal-zero import flow via two new MCP tools** (issue #148,
  PRs #149 / #151 / #152 / #153). Drop your Apple Health `export.zip`
  into the configured directory and ask Claude — Claude calls
  `list_zips` to discover the ZIP, `import_zip(id="…")` to extract +
  ingest it on the server's writable handle. No terminal commands
  required. Tool count: **18 → 20**.

### Added

- **`list_zips()` MCP tool**: lists every ZIP in
  `APPLE_HEALTH_EXPORT_ZIPS_DIR` with `{id, file_name, mtime, size,
  sha256, imported, is_apple_health}`. sha256 cached via the
  `imports` table so multi-GB ZIPs are not rehashed on every scan.
- **`import_zip(id)` MCP tool**: resolves the 8-char sha prefix via
  a DB-cache fast path, falls through to streaming hash only for
  never-seen ZIPs, extracts into a temp directory, and drives
  `run_import` on the server's writable handle. Idempotent re-imports
  return `records_added: 0` + `already_imported_at` in milliseconds.
- **`DataState` envelope for read tools** (`server/data_state.py`):
  every read tool short-circuits with a structured
  `{state, reason, suggested_action, human_message}` JSON when the DB
  is not ready. Replaces the v0.3.x `IMPORT_REQUIRED_MESSAGE`
  plain-string sentinel.
- **`imports.source_zip_*` triple** (PR #149): `source_zip_sha256`
  VARCHAR / `source_zip_mtime` TIMESTAMPTZ / `source_zip_size` BIGINT.
  NULL on CLI-driven rows; populated by `import_zip` so the cache
  loader skips work on already-imported ZIPs.
- **`run_import(conn=…, source_zip=…)` orchestrator seam** (PRs
  #149 / #151): caller-owned writable connection + source-ZIP triple
  parameter. CLI callers keep their pre-v0.4 signature; the new
  `import_zip` MCP tool reuses the server's live handle.

### Breaking

- **Server connection now opens read-write** (PR #151). v0.3.x relied
  on OS-level read-only file locks as a defence-in-depth layer on top
  of `server/safety`'s SQL validator. v0.4 drops the read-only flag so
  `import_zip` can drive the importer inline. `server/safety` is now
  the **sole** wire-side guard — `validate_query` still rejects every
  DDL / DML / ATTACH / COPY / PRAGMA / quoted-path-FROM construct on
  the `run_custom_query` path. The connection-level guard is gone;
  the SQL-level guard is the contract.

- **CLI `import` workflow now requires stopping the server first**.
  Pre-v0.4 the server held a read-only DuckDB snapshot, so the
  documented flow was "leave Claude Desktop running, import from
  another shell, restart the server". v0.4 holds an exclusive
  writable file lock; a concurrent `apple-health-mcp-server import`
  from another shell fails with a lock-conflict error. Stop the
  server, run the CLI import, then restart. (Or skip the CLI
  entirely and use the new `list_zips` + `import_zip` flow from
  inside Claude.)

- **MCPB bundle `user_config.db_path` removed** in favor of
  `user_config.export_zips_dir` (PR #152). Existing v0.3.x bundle
  users must reconfigure once after upgrading. Claude Desktop users
  who need a custom DB path (rare; the platform default is now safe
  because the server reads + writes inside the same sandbox) edit
  `claude_desktop_config.json` directly and add `APPLE_HEALTH_DB` to
  the server's `env` map.

- **Read tools now return a structured `{state, reason,
  suggested_action, human_message}` envelope** (instead of the
  v0.3.x `IMPORT_REQUIRED_MESSAGE` plain-string sentinel) when the
  DB has no successful import. The `state` is one of `READY` /
  `NEEDS_CONFIG` (env var not set) / `NEEDS_IMPORT` (env var set but
  no import yet). Substring matchers on the pre-v0.4 sentinel must
  migrate. `get_import_history` is the one tool that opts out — it
  still returns `[]` on an empty DB.

- **`CURRENT_SCHEMA_VERSION` bumped 4 → 5** (PR #149). The bump adds
  the `imports.source_zip_*` triple with no in-place migration. v0.3.x
  DBs raise the canonical re-import `ConfigError` carrying the
  `rm <db>` recovery command, same fresh-start contract as #126 / #129.
  After re-import the data is queryable through the same tools, plus
  the new ZIP-flow tools cover future re-imports without a terminal.

### Changed

- **`require_imports_or_message` is now a re-export alias** of
  `data_state.require_ready_or_state_error`. The two helpers had
  byte-identical bodies after the rename and were drift-prone.
- **`get_import_history` wire shape** widened to include the
  `source_zip_*` triple (`source_zip_sha256` / `source_zip_mtime` /
  `source_zip_size`). NULL on CLI-driven rows.
- **`safety.py` module docstring rewritten** with the v0.4 threat
  model: the validator is now the SOLE wire-side guard against DDL /
  DML / ATTACH / COPY / etc., not a second layer on top of the
  read-only flag.
- **README.md / README.ja.md upgrade walkthroughs updated** for the
  v0.4 flow (`export_zips_dir` directory + `import_zip` from Claude
  + "stop serve before CLI import" caveat). The LP footer + i18n
  tool counts bumped to 20.

## [0.3.0] - 2026-06-26

### Documentation

- **README work-arounds bundled for the v0.3.0 stable release**
  (issue #144, closes #127 and #131). The Claude Desktop install
  section now spells out the Windows first-run uvx warmup (#127) and
  the MSIX `%LOCALAPPDATA%` sandbox-redirect recovery (#128), the
  Database location section enumerates the
  `APPLE_HEALTH_DB` / `APPLE_HEALTH_DATA_DIR` override chain and
  cross-references the new `get_server_info` MCP tool, the Locales
  section warns that cross-locale exports cannot be merged in
  v0.3.x (#131), and `schema.py`'s file-level docstring clarifies
  that the `export_*` identifiers refer to Apple Health's own
  "export" function — not the MCP server (re-)exporting anything.

### Added

- **`imports.imported_at` now reflects the import START moment, in UTC**
  (issue #130). The orchestrator now captures a single
  `datetime.now(UTC)` at the top of `run_import` and threads it into
  both `make_import_id` (which formats it) and the `INSERT INTO
  imports` value list. Pre-#130 the schema's `DEFAULT CURRENT_TIMESTAMP`
  fired at INSERT time (= pipeline end), so `import_id` and
  `imported_at` could diverge by the full import duration; on a
  multi-GB export this left the two stamps looking like unrelated
  events when grepping the imports table. The wire shape is
  unchanged (TIMESTAMPTZ rendered in the session timezone) so this
  is a Layer 2 internal-correctness fix, not a Layer 1 breaking
  change.

- **`resolve_db_path()` with env-override precedence** (issue #132, PR #133).
  New resolver that consults `APPLE_HEALTH_DB` (file path) > `APPLE_HEALTH_DATA_DIR`
  (directory) > the historical platform default. `default_db_path()` becomes a
  backward-compatible alias. `--db` on the CLI now also promotes its value
  into `APPLE_HEALTH_DB` so server, CLI, and any future caller that resolves
  through `resolve_db_path()` agree on the open file. Foundation for #128
  (Windows MSIX `%LOCALAPPDATA%` sandbox-redirect) recovery flows.

- **`get_server_info` MCP tool — 18th tool** (issue #137, PR #138).
  Returns `{db_path, version, record_count, config_source}` so a Claude
  Desktop user can ask the server itself which DB it has open without
  leaving the chat. `db_path` reports the live connection's open file via
  `PRAGMA database_list` (NOT a re-resolution), so a divergence between
  the resolver and the actual open handle is observable. `config_source`
  labels which `resolve_db_path()` tier produced the path
  (`env:APPLE_HEALTH_DB` / `env:APPLE_HEALTH_DATA_DIR` / `platform_default`).
  Tool count: **17 → 18**; the original Rust-mirrored 17 remain in their
  historical positions and the new tool is appended.

### Breaking

- **`imports.records_after_dedup` column + schema_version 4 bump** (issue #129).
  The `imports` table gains a `records_after_dedup BIGINT` column carrying
  the Phase-4 post-dedup row count for the import. `record_count` keeps its
  pre-dedup parse count semantics; the difference between the two is the
  number of Correlation-derived duplicates collapsed (Apple Health
  duplicates `<Correlation>` children at the top level by spec — see
  CLAUDE.md §5). `get_import_history` returns the new field in every row.
  Bumps `CURRENT_SCHEMA_VERSION` to `4`; no in-place migration is registered
  for v=3 → v=4, so pre-PR-D DBs raise the same friendly re-import
  `ConfigError` PR #126 introduced (same `rm <db> && apple-health-mcp-server
  import <export>` recovery). Closes the "where did the 125 records go?"
  diagnostic gap that surfaced during the v0.3.0-rc2 dogfood.

  **SemVer classification** (per the Layer 1 / Layer 2 split documented
  in PR #107): the schema bump itself is Layer 2 (DB on-disk surface);
  the `get_import_history` MCP wire response now contains the new
  `records_after_dedup` field, which IS Layer 1 (wire-facing). v0.3.0
  ships both classes of change together because the v0.x SemVer
  disclaimer in the README's "Compatibility" section already allows
  minor-version wire breakage; from v1.0.0 onward an additive
  Layer 1 field of this kind would justify a minor bump on its own.

- **Dropped automatic schema migration from v0.2.x DBs** (issue #124).
  The `heart_rate_samples.sample_time` VARCHAR → DOUBLE in-place
  migration introduced in v0.3.0-rc2 (PR #117) collided with the
  importer-created `idx_heart_rate_samples_parent` index — DuckDB
  rejects `ALTER COLUMN ... TYPE` whenever the table has any dependent
  index, so the migration failed with `DependencyException` on every
  real pre-v0.3.0 DB. Rather than ship a fragile in-place upgrade we
  removed the auto-migration entirely and surface a friendly
  `ConfigError` ("v0.3.0 dropped the v0.2.x->v0.3.0 auto-migration.
  Please re-import: rm <db> && apple-health-mcp-server --db <db>
  import <export_dir>") whenever `serve` opens a pre-v0.3.0 DB. The
  on-disk schema and data are left untouched; recovery is a one-time
  `rm <db> && apple-health-mcp-server import <export>` (a couple of
  minutes on a multi-GB `export.xml`). See the README's
  "Upgrading from < v0.3.0" section for the full recovery flow.

## [0.3.0-rc2] - 2026-06-25

Second pre-release of the v0.3.0 cycle. Bundles the wire envelope
unification (issue #108), the `heart_rate_samples.sample_time`
storage migration (issue #109), and the LP install-step delinking
from explicit version filenames (issue #111). Treat this rc as the
dogfood baseline; v0.3.0 stable follows after stability is confirmed.

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

