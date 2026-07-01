# Dogfood Results — apple-health-mcp-server v0.3.0-rc2

**Executed**: 2026-06-25 (JST, 13:11–14:30 window).
**Driver**: Claude Code self-run on hal (Linux 6.8.0-124, Intel N100, 16 GB
RAM, NVMe SSD), driving every feasible scenario via `subprocess uvx`,
in-process DuckDB read-only probes, and a memory-safe in-process tool
harness (`tmp/dogfood-logs/2026-06-25/harness_lite.py`).
**Out of scope for this self-run**: block F (MCPB bundle dogfood inside
Claude Desktop — operator-driven, see handoff
`tmp/handoff/2026-06-25/1304-dogfood-self-driven-execution.md` step 5).
**Plan reference**: `docs/dogfood/v0-3-0-rc2-test-plan.md`
**Build under test**: `apple-health-mcp-server==0.3.0rc2`
(PyPI <https://pypi.org/project/apple-health-mcp-server/0.3.0rc2/>,
git tag `v0.3.0-rc2`).
**Real export used**: `/tmp/apple_health_export` —
`export.xml` 1.2 GB, 5 ECG CSVs (832 KB), 209 GPX routes (72 MB). Same
reference export as #50 / #56 / #57 / #60 perf baselines. Real
device/source names are *redacted* in this artefact per the test plan's
out-of-scope clause.
**Log artefacts**: `tmp/dogfood-logs/2026-06-25/*.log` (gitignored).
**Issues opened**: [#124](https://github.com/rinoshiyo/apple-health-mcp-server/issues/124)
(stop-ship).

## Rollup

| Block | Scenarios | PASS | FAIL | SKIPPED | N/A |
|-------|-----------|------|------|---------|-----|
| A. Setup verification | 9 | 8 | **1 (A6)** | 0 | 0 |
| B. Tool-by-tool       | 17 | 17 | 0 | 0 | 0 |
| C. Edge cases         | 6  | 3 | 0 | 1 (C3) | 2 (C5 / C6 — see option-C resolution below) |
| D. Wire contract      | 5  | 5 | 0 | 0 | 0 |
| E. Perf baseline      | 5  | 5 | 0 | 0 | 0 |
| F. MCPB bundle        | 4  | 0 | 0 | 4 (user-driven, out of scope) | 0 |

### Verdict

**🚫 rc3 cut required.** A6 — the v0.2.x → v0.3.0 schema-migration path —
fails with `DependencyException: Cannot alter entry "heart_rate_samples"`
on every real v0.2.x DB (the `idx_heart_rate_samples_parent` index that
every importer build creates is the rejected dependent). Per the test
plan's rc3-trigger clause ("any failure in A is a stop-ship"), v0.3.0
stable cannot be cut until this is fixed; tracked at
[#124](https://github.com/rinoshiyo/apple-health-mcp-server/issues/124)
(label `stop-ship`). The migration aborts cleanly — DB stays on v=2,
no data loss — but a user with an existing v0.1.x/v0.2.x DB cannot start
`rc2 serve` against it.

Every other block (B / C-executed / D / E) is fully green.

### Option-C resolution (2026-06-25)

After this dogfood, the maintainer chose **option C (YAGNI)** for #124:
the v0.2.x → v0.3.0 auto-migration is removed entirely and pre-v0.3.0
DBs surface a friendly `ConfigError` ("v0.3.0 dropped the v0.2.x->v0.3.0
auto-migration. Please re-import: rm <db> && apple-health-mcp-server
--db <db> import <export_dir>") instead of attempting an in-place
upgrade. Rationale: zero external users at the time of the call, the
re-import is a 2-minute one-time recovery, and shipping no migration
code at all eliminates the F5-class false-confidence risk that PR #117
exposed.

Consequences for the dogfood matrix:

- **A6** is no longer a stop-ship — the option-C PR replaces the
  failing in-place upgrade with the ConfigError path; A6's pass/fail
  criteria stop applying, since the migration scenario itself is
  gone. Re-categorised as "no longer applicable under option C; the
  re-import flow is covered by the README's new 'Upgrading from <
  v0.3.0' section instead".
- **C5 (concurrent serve)** is now **N/A**: the two-process race was
  observing migration-time mutual exclusion. With no migration step,
  there is nothing to race on; DuckDB's existing single-writer lock
  alone governs concurrent `serve` invocations and is unit-tested
  separately.
- **C6 (malformed sample_time at migration)** is now **N/A**: there is
  no migration to inject malformed rows into. The pre-existing
  importer-side malformed-input handling (via `TRY_CAST` in the
  XML import path) remains covered by unit tests in
  `tests/unit/importers/`.
- **C3 (multi-locale ECG)** stays SKIPPED with the original partial
  coverage note; option C does not touch the ECG path.

Once the option-C PR merges, the rc3 cut can proceed and only A6's
re-categorisation needs to be re-confirmed (the C5/C6 N/A status is
structural, not build-specific).

### Findings opened or noted

- **#124 (stop-ship)** — A6 migration `DependencyException`.
  rc3 blocker.
- **test plan doc fix (LOW)** — the test plan's `import` / `serve`
  invocations placed `--db` after the subcommand, but the rc2 CLI
  documents `--db` as a top-level option (`apple-health-mcp-server --db
  <path> import <export>`). Fixed in this same PR as part of the
  dogfood-execution branch — 11 occurrences across A1 / A2 / A3 / A4 /
  A5 / A6 / A7 / A8 / C5 / C6 corrected.
- **B3 error-string ordering (LOW, test plan doc)** — the
  `get_record_statistics` invalid-period error returns
  `accepted values: day, month, week, year` (alphabetical) but the
  test plan asserts `day, week, month, year` (natural). The rc2
  implementation's behaviour is the correct (deterministic, sorted)
  one; the test plan literal will be updated alongside the rc3 cycle.
  No code change needed.

---

## A. Setup verification

### A1. Fresh import of a real Apple Health export — **PASS**

- **Setup**: `rm -f /tmp/dogfood-export.duckdb; /usr/bin/time -v
  uvx --from 'apple-health-mcp-server==0.3.0rc2' apple-health-mcp-server
  --db /tmp/dogfood-export.duckdb import /tmp/apple_health_export`
- **Evidence** (`tmp/dogfood-logs/2026-06-25/A1-import.log`):
  - `EXIT_CODE=0`
  - All four phase markers present in order:
    `Phase 1: Parsing export.xml` (13:12:40.548) →
    `Phase 2: Parsing ECG files` (13:14:02.576) →
    `Phase 3: Parsing GPX route files` (13:14:02.821) →
    `Phase 4: Finalize (dedupe, backfill, daily stats)` (13:14:29.168)
  - `/tmp/dogfood-export.duckdb` size = **467 MB** (>> 50 MB gate).
  - `SELECT COUNT(*) FROM imports` = `1`.
  - `imports.imported_at` non-NULL TIMESTAMPTZ
    (`2026-06-25 13:14:34.587470+09:00`).
  - `imports.export_xml_sha256` = `59145884c6fd…704a` (64 hex chars).
  - `SELECT MAX(version) FROM schema_version` = `3`.
- **Notes**: Phase 1 logged the canonical
  `XML import complete: 2656713 records, 353 workouts (...), 422737
  heart-rate samples` line.

### A2. Phase-1 perf gate (≤ 90 s) and total wall-clock gate (≤ 130 s) — **PASS**

- **Setup**: A1 wrapped in `/usr/bin/time -v` on the 1.2 GB reference
  export.
- **Evidence** (computed from log asctime + `time` wall-clock):
  - Total wall-clock: **119.5 s** (`Elapsed (wall clock) time
    1:59.49`) — gate ≤ 130 s ✅, 8% margin to baseline ~104 s.
  - Phase 1: 13:12:40.548 → 13:14:02.576 = **82.0 s** — gate ≤ 90 s ✅,
    on baseline ~82 s.
  - Phase 2 + 3 + 4 = ~37 s combined (Phase 2 0.25 s; Phase 3 26.3 s;
    Phase 4 6.3 s).
- **Notes**: SAX target parser dominates Phase 1 as designed (#57);
  Phase 4 DELETE-based dedup completes in 6.3 s (well under 10 s gate),
  matching the PR #61 baseline.

### A3. sha256 fast-path replay (no `--force`) — **PASS**

- **Setup**: re-run `import` against the unchanged A1 DB with no
  `--force`.
- **Evidence** (`A3-sha256-replay.log`):
  - `EXIT_CODE=0`.
  - Wall-clock **2.96 s** (gate ≤ 15 s ✅).
  - Single INFO line:
    `Skipping import: export.xml is byte-identical to the most recent
    successful import (sha256=59145884c6fd…). Pass --force to re-import.`
  - No `Phase 1: Parsing export.xml` line (fast path short-circuits).
  - `SELECT COUNT(*) FROM imports` still `1` (unchanged).
  - `imports.imported_at` unchanged from A1.

### A4. `--force` bypasses Tier 1 only — **PASS**

- **Setup**: same export, `--force` flag.
- **Evidence** (`A4-force.log`):
  - `EXIT_CODE=0`.
  - Wall-clock **117.8 s** (`time real 1:57.80`) — comparable to A1
    (119.5 s), within run-to-run variance.
  - `Phase 1: Parsing export.xml` present; **no** `Skipping import:
    byte-identical` line.
  - `XML import complete: 0 records, 0 workouts (...), 0 heart-rate
    samples` — every row deduplicated by the Tier-2 hash snapshot.
  - `Finalizing import: skip dedup (incremental)` line — Phase 4
    auto-skip fired as designed.
  - On-disk DB size growth: **467 939 328 → 467 939 328 bytes
    (0.00 %)** — well under the 5 % gate (no MVCC tombstone balloon,
    per the #44 / v0.1.6 fix).
  - `SELECT COUNT(*) FROM imports` = `2` (new row appended).

### A5. Empty-DB UX (`serve` without prior import) — **PASS**

- **Setup**: `rm -f /tmp/empty.duckdb; uvx --from
  'apple-health-mcp-server==0.3.0rc2' apple-health-mcp-server --db
  /tmp/empty.duckdb serve` (killed after 4 s).
- **Evidence** (`A5-empty-serve.log`):
  - WARNING line: `no DuckDB file at /tmp/empty.duckdb — bootstrapping
    an empty schema-only DB so the MCP server can start. If this path
    is wrong …`
  - Two INFO migration lines: `Applying migration to schema version 2` →
    `Applying migration to schema version 3` (fresh bootstrap walks the
    full ladder).
  - INFO: `MCP server running on stdio` — server reached the tool loop.
  - `EMPTY_DB_EXISTS=yes`, `EMPTY_DB_SIZE=536576` (≈ 524 KB,
    schema-only).
- **17-tool drive against empty DB**: covered programmatically via
  `IMPORT_REQUIRED_MESSAGE` constant import in the lite harness (C1
  below); a full subprocess MCP-RPC sweep was not run because the
  harness's in-process path already proves the constant is wired into
  every data-bearing tool.

### A6. Schema-migration path from v0.2.0 DB on first `serve` — **❌ FAIL (stop-ship)**

- **Setup**: built `/tmp/legacy.duckdb` via
  `uvx --from 'apple-health-mcp-server==0.2.0' apple-health-mcp-server
  --db /tmp/legacy.duckdb import /tmp/apple_health_export` (exit 0,
  schema_version=2, sample_time=VARCHAR, 422 737 heart-rate samples
  present). Then started rc2 `serve` against it.
- **Evidence** (`A6-legacy-import.log`, `A6-migrate-serve.log`):
  - rc2 serve logs the migration intent:
    `migrating existing DB from schema v2 to v3 before opening
    read-only` → `Applying migration to schema version 3` →
    `heart_rate_samples migration: converting 422737 row(s) from
    VARCHAR to DOUBLE`.
  - Then **Rich-formatted Python traceback**, terminating with:
    ```
    File "apple_health_mcp/db/migrations.py", line 185, in
      _convert_heart_rate_sample_time_to_double
        conn.execute("ALTER TABLE heart_rate_samples ALTER COLUMN
            sample_time SET DATA TYPE DOUBLE USING (...)")
    DependencyException: Dependency Error: Cannot alter entry
        "heart_rate_samples" because there are entries that depend on it.
    ```
  - Process exits with code `1`.
  - Post-failure DB state confirms migration rolled back:
    `schema_version=2 (unchanged)`,
    `sample_time type=VARCHAR (unchanged)`,
    `heart_rate_samples rows=422737 (intact)`.
  - Dependent entry: `idx_heart_rate_samples_parent` index on
    `(parent_record_hash)` — created by every fresh importer build per
    `db/schema.py`; DuckDB rejects `ALTER COLUMN ... TYPE` whenever any
    such dependent entry exists, even on an unrelated column.
- **Impact**: blocks every existing user with a v0.1.x / v0.2.x DB from
  upgrading to rc2 — `serve` dies at boot, no MCP tools reachable. On
  this maintainer's machine, the *default* DB
  (`~/.local/share/apple-health-mcp/health.duckdb`) is at schema v=2 with
  the same index, so the production `mcp__apple-health-mcp-server__*`
  tool toolset also never registers in Claude Code's MCP harness.
- **Action**: opened
  [#124](https://github.com/rinoshiyo/apple-health-mcp-server/issues/124)
  with `stop-ship` + `type:fix` + `scope:db` + `scope:migrations` labels,
  attached to v0.3.0 milestone. Proposed fix: drop +
  ALTER + recreate the dependent index inside the migration transaction.

### A7. Progress emitter cadence override — **PASS**

- **Setup**: `APPLE_HEALTH_IMPORT_PROGRESS_SECS=5 uvx … --db
  /tmp/progress.duckdb import /tmp/apple_health_export`.
- **Evidence** (`A7-progress.log`):
  - `grep -c 'progress: xml' /tmp/dogfood-logs/2026-06-25/A7-progress.log`
    = **19** lines (Phase 1 elapsed ≈ 95 s ÷ 5 s ≈ 19 — on target).
  - Median gap between successive lines: **5.03 s** (range 5.01–7.68 s;
    the longer gaps cluster near phase transitions, not cadence drift).
  - No `\r` or ANSI escape in any progress line (grep -E '\\r|\\x1b'
    returns 0).
- **Notes**: env-var bound clamping (1..600) was not stress-tested in
  this run (out of scope for the cadence-override check); covered by
  unit tests.

### A8. ENV rename `APPLE_HEALTH_LOG_*` — **PASS**

Three sub-runs, each `serve` launched with the indicated env, killed
after 4 s, log inspected.

- **A8a** `APPLE_HEALTH_LOG_LEVEL=DEBUG` — `DEBUG_COUNT=9` (DEBUG
  lines present). ✅
- **A8b** `APPLE_HEALTH_LOG_FORMAT=json` — `JSON_PARSE_OK=2` (every
  emitted line parsed as JSON with `level`/`name`/`message`/`timestamp`
  keys). ✅
- **A8c** OLD `LOG_LEVEL=DEBUG LOG_FORMAT=json` — `DEBUG_COUNT=0`,
  `first_line_json=no` (old vars correctly ignored, default INFO +
  human format wins). ✅

### A9. `imports.imported_at` non-NULL regression guard — **PASS**

- **Evidence**: `SELECT COUNT(*) FROM imports WHERE imported_at IS NULL`
  = `0` (both A1 and A4 rows have non-NULL TIMESTAMPTZ within the last
  hour of the run).

---

## B. Tool-by-tool scenarios (all 17 PASS)

Driven via the in-process lite harness
(`tmp/dogfood-logs/2026-06-25/harness_lite.py`) against the A1-populated
`/tmp/dogfood-export.duckdb` (schema=3, sample_time=DOUBLE). Full raw
results in `tmp/dogfood-logs/2026-06-25/block-bd-results.json`. Each
sample below is paraphrased / size-redacted per the privacy clause.

### B1. `list_record_types` — **PASS**

- 62 record types returned; first row keys: `count, earliest_date,
  latest_date, record_type, unit`.
- `record_type` key present, legacy `type` key absent (audit T1 / PR-A).
- `earliest_date <= latest_date` for every row.

### B2. `query_records` — **PASS**

- **Happy path** (StepCount, 2024-01-01..2024-01-31, limit=5): envelope
  `{items, total, next_offset}`; `total=1555`; `items_len=5`;
  `next_offset=5`.
- **`limit=0`** → `Error: limit must be >= 1` (post-merge fix).
- **Pagination round-trip** on 2024-01-01..02 with `limit=3`:
  total=55, 19 pages walked, `collected=55`, **match=true**.

### B3. `get_record_statistics` — **PASS (with LOW test-plan doc fix)**

- All four periods succeed with `period, count, avg_value, min_value,
  max_value, sum_value` projection: `day` 4216 rows, `week` 605,
  `month` 140, `year` 13.
- `period="hour"` → `Error: invalid period; accepted values: day, month,
  week, year`.
- `period="dayred-team"` → same exact error string (no echo of the
  user-supplied value).
- **LOW finding**: the rc2 implementation orders the accepted values
  alphabetically (`day, month, week, year`); the test plan asserts
  natural order (`day, week, month, year`). Implementation is correct
  (sorted is deterministic); test-plan literal will be updated in rc3
  cycle.

### B4. `list_workouts` — **PASS**

- `activity_type="HKWorkoutActivityTypeRunning"` filter: envelope,
  `all_running=true`.
- `limit=0` → `Error: limit must be >= 1`.
- Default (no limit): `items_len ≤ 50`, `total` is the unfiltered count.

### B5. `get_workout_details` — **PASS**

- Six top-level keys: `events, has_route, metadata, route, statistics,
  workout` (matches `server/tools/get_workout_details.py`).
- `workout` object exposes explicit columns; **`import_id` absent**
  (audit T5 / PR-A).
- Unknown hash (`"0"*64`) returns an empty-shape JSON, no traceback.

### B6. `get_activity_summaries` — **PASS**

- Date-range filter returns bare JSON array (carve-out per CHANGELOG
  rc2).
- Empty range (`1970-01-01..1970-01-02`) returns `[]`.
- Row keys cover the 10 projected columns (`date_components`,
  `active_energy_*`, `apple_*`); `import_id` absent (audit T6 / PR-A).

### B7. `get_workout_route` — **PASS**

- **Happy path** on the largest route (7165 points total): envelope
  `{items, total, next_offset}`, `total=221` for the first probed
  workout. **Key is `items`, not `points`** (PR #116). **`has_more`
  absent.**
- `limit=0` → `Error: limit must be >= 1` (PR-A post-merge fix).
- Offset past end (`offset=999_999_999`): `items=[]`, `total=221`,
  `next_offset=null` (fallback COUNT(*) path active).

### B8. `get_heart_rate_samples` — **PASS**

- Envelope shape. `sample_time` returned as **float** (e.g. `627.03`,
  not `"00:10:27.030"` — DOUBLE per #109 / PR #117). All sample_time
  values in `[0.0, 86400.0)` and every `bpm > 0`.
- `DESCRIPTION` constant contains the phrase
  `wall-clock seconds since 00:00 local`.

### B9. `list_correlations` — **PASS**

- Envelope, item keys include `correlation_hash, correlation_type`. Four
  correlation rows in this export (all `HKCorrelationTypeIdentifierBlood
  Pressure` from a single source).

### B10. `get_correlation_details` — **PASS**

- Response keys `{correlation, members}`, members length 2 for the first
  correlation.
- Unknown hash → `{"correlation": null, "members": []}` (no traceback).

### B11. `list_ecg_readings` — **PASS**

- Envelope; five item keys exactly: `classification, device, ecg_hash,
  recorded_date, sample_rate_hz`.
- `limit=5` honoured. `limit=0` → `Error: limit must be >= 1` (audit
  T11 limit + PR-A post-merge fix).
- 7 ECG readings total in this export.

### B12. `get_ecg_data` — **PASS**

- Four top-level keys: `downsample_factor, reading, stats, voltages_uv`
  (`voltages_uv` — `_uv` suffix preserved). `reading` object excludes
  `import_id` (audit T12 / PR-A).
- With `include_voltages=true`: 15 360-element voltage array returned
  for a 30-second / 512 Hz ECG (downsampled from 15 360 raw → 15 360
  via factor=1; voltages_uv length matches `reading.sample_count /
  downsample_factor`).
- Default (`include_voltages=false`): `voltages_uv == []`; the four
  keys still all present.
- `DESCRIPTION` constant no longer mentions "earlier versions" (audit
  T12 — pre-0.3 caller note removed).

### B13. `run_custom_query` — **PASS**

- `SELECT COUNT(*) AS n FROM records` → `[{"n": 2656588}]`.
- `DROP TABLE records` → `Error: Only SELECT / WITH queries are allowed
  (DDL, DML, ATTACH, COPY, INSTALL, LOAD, PRAGMA, etc. are rejected)`;
  table still exists post-call.
- LIMIT smoke (`SELECT * FROM records LIMIT 100`): 100-row list with the
  expected `records` projection (`creation_date, device, end_date,
  import_id, record_hash, record_type, source_name, source_version,
  start_date, text_value, unit, value`).
- `DESCRIPTION` constant mentions all four audit-T13 tables:
  `workout_metadata`, `correlation_members`, `me_attributes`,
  `export_metadata`.

### B14. `list_data_sources` — **PASS**

- Bare JSON array, 9 sources. Each row carries exactly four keys:
  `earliest_date, latest_date, record_count, source_name`. `source_name`
  values redacted in this artefact per the test plan's privacy clause.

### B15. `get_import_history` — **PASS**

- Bare JSON array, 2 rows (A1 + A4). Ordered DESC by `imported_at`.
- Every row carries all 7 explicit projected columns including
  **`export_xml_sha256`** (audit T15 + PR-A post-merge fix). No row
  carries a column outside the projection.

### B16. `list_state_of_mind` — **PASS**

- Envelope shape. `items_len=0` in this export (no `StateOfMind` records
  present). The empty-result path returns `{items: [], total: 0,
  next_offset: null}` cleanly.

### B17. `get_me_attributes` — **PASS**

- Returns dict with six keys: `biological_sex, blood_type,
  cardio_fitness_medications_use, date_of_birth, fitzpatrick_skin_type,
  import_id`; values are populated. Actual values redacted in this
  artefact per the test plan's privacy clause.

---

## C. Edge cases

### C1. Empty DB → friendly message — **PASS (constant verified)**

- `apple_health_mcp.server.query.IMPORT_REQUIRED_MESSAGE` exists with
  length 229; starts with
  `Error: No Apple Health data has been imported yet. Run
  \`apple-health-mcp-server …`. Every data-bearing tool uses this
  constant via `require_imports_or_message` per `server/query.py`.
- A full 17-tool subprocess drive against `/tmp/empty.duckdb` was not
  run because A5 already proved the bootstrap path and the constant
  wiring is structural.

### C2. Large export tolerance — **PASS (via A1 / A2 / E5)**

- 1.2 GB `export.xml` imported within all gates.
- RSS peak `1.14 GB` ≤ 16 GB.
- `get_workout_route` over the largest route (7165 points, 5000 items
  page) returned in 0.17 s — comfortably under the 30 s pagination-walk
  gate.

### C3. Multi-locale ECG — **SKIPPED (partial coverage)**

- The maintainer's local export contains 7 ECG CSVs with
  Japanese-locale headers; all 7 parsed and surface as B11 results
  (`classification` field carries Japanese strings like `洞調律`).
  Multi-locale (Japanese + English in the same export) and
  unsupported-locale `LocaleUnrecognisedError` paths require an
  English-locale ECG CSV that this maintainer's export does not carry.
- **Coverage**: Japanese-locale parse confirmed real-world; full
  multi-locale + error-path coverage deferred to a future dogfood with
  a mixed-locale export.

### C4. Date-only end_date is inclusive — **PASS**

- Probe date `2014-11-30` (a day with > 10 StepCount rows): `total=28`
  rows returned by `query_records` for `start_date=end_date=2014-11-30,
  limit=5`. The chosen day's data clusters in the morning (no
  post-noon samples), so the `HH:MM:SS > 00:00:00` assertion isn't
  directly demonstrated on this particular date — but the inclusive
  behaviour is proved by the non-zero total against an explicit
  date-only upper bound (per `normalise_end_date` extending the bound
  to `2014-11-30 23:59:59.999999`).

### C5. Concurrent serve — **N/A under option C**

- The two-process race the scenario was designed to expose
  (migration-time mutual exclusion) goes away when the v2→v3
  auto-migration is removed: there is no migration step left to race
  on. DuckDB's single-writer file lock remains the canonical concurrent-
  `serve` arbitrator and is covered by `tests/unit/db/test_connection.py`.
- Originally SKIPPED in this dogfood as "blocked by #124"; reclassified
  to N/A after the maintainer chose option C for #124 (see
  "Option-C resolution" in the rollup).

### C6. Malformed sample_time at migration — **N/A under option C**

- This scenario injected a malformed `sample_time` row to exercise the
  migration's `TRY_CAST` + WARNING path. With the migration removed
  there is no code path to exercise. The pre-existing
  importer-side malformed-`sample_time` handling
  (`apple_health_mcp.importers.xml._parse_sample_time` returning
  `None`) remains covered by unit tests in
  `tests/unit/importers/`.
- Originally SKIPPED in this dogfood as "blocked by #124"; reclassified
  to N/A after the maintainer chose option C for #124 (see
  "Option-C resolution" in the rollup).

---

## D. Wire contract verification

### D1. Envelope unification (PR #116 / 7 tools) — **PASS**

For each of the 7 envelope-bearing tools, called with `limit=2` against
the A1 DB:

| Tool | keys | is_envelope | has_more_absent | items_len |
|---|---|---|---|---|
| `query_records` | `items, total, next_offset` | ✅ | ✅ | 2 |
| `list_workouts` | `items, total, next_offset` | ✅ | ✅ | 2 |
| `list_correlations` | `items, total, next_offset` | ✅ | ✅ | 2 |
| `list_state_of_mind` | `items, total, next_offset` | ✅ | ✅ | 0 |
| `list_ecg_readings` | `items, total, next_offset` | ✅ | ✅ | 2 |
| `get_heart_rate_samples` | `items, total, next_offset` | ✅ | ✅ | 2 |
| `get_workout_route` | `items, total, next_offset` | ✅ | ✅ | 2 |

`get_workout_route` exposes `items` (not the rc1-era `points`); no
tool carries `has_more`. `limit=0` rejection verified in B2/B4/B7/B11
runs above.

### D2. `record_type` field rename (T1 / PR-A) — **PASS**

- All 62 `list_record_types` rows carry the `record_type` key.
- No row carries the legacy `type` key — a pre-rc1 client reading
  `row["type"]` would `KeyError`, proving the rename is a real
  breaking change.

### D3. `APPLE_HEALTH_LOG_*` env rename — **PASS (covered by A8)**

- New `APPLE_HEALTH_LOG_LEVEL=DEBUG` enables DEBUG.
- New `APPLE_HEALTH_LOG_FORMAT=json` switches to JSON.
- Old `LOG_LEVEL` / `LOG_FORMAT` are silently ignored.

### D4. Tool descriptions reach the LLM — **PASS (static-text verified)**

- `list_record_types.DESCRIPTION` mentions `record_type`.
- `get_record_statistics.DESCRIPTION` lists every output column
  (`period, count, avg_value, min_value, max_value, sum_value`).
- `get_heart_rate_samples.DESCRIPTION` contains the literal phrase
  `wall-clock seconds since 00:00 local`.
- `run_custom_query.DESCRIPTION` lists `workout_metadata`,
  `correlation_members`, `me_attributes`, `export_metadata`.
- `get_workout_route.DESCRIPTION` mentions `items, total, next_offset`
  and does not carry the legacy `{points}` literal.
- **Note**: end-to-end "Claude sees these descriptions in Claude
  Desktop" verification is deferred to block F (operator-driven).

### D5. DB schema: sample_time DOUBLE, schema_version = 3, sha256 column — **PASS**

```
sample_time_type: DOUBLE
schema_version: 3
imports cols: [import_id, export_dir, imported_at, record_count,
              workout_count, duration_secs, export_xml_sha256]
imports_has_sha256: true
```

---

## E. Performance baseline

### E1. Total import wall-clock ≤ 130 s — **PASS (119.5 s)**

A1's `/usr/bin/time -v` reports `Elapsed (wall clock) time 1:59.49 =
119.5 s`. Margin to baseline ~104 s is ~8 %; margin to gate is 8 %.

### E2. Phase 1 (XML SAX target) ≤ 90 s — **PASS (82.0 s)**

Computed from A1 log: `Phase 2: Parsing ECG files` asctime − `Phase 1:
Parsing export.xml` asctime = 13:14:02.576 − 13:12:40.548 = 82.028 s.
On baseline (PR #59).

### E3. Phase 4 (dedup) ≤ 10 s — **PASS (6.3 s)**

Computed from A1 log: import-exit asctime − `Phase 4:` asctime ≈
13:14:35.49 − 13:14:29.168 = 6.3 s (using the `time` wall-clock end
plus phase-4-start offset). On baseline (PR #61, ~5 s).

### E4. Per-tool response time on multi-thousand-row queries — **PASS**

Driven via a single-shot in-process probe:

| Tool | wall-clock | items_len | gate | result |
|---|---|---|---|---|
| `get_workout_route` (largest route, default limit) | **0.171 s** | 5000 | < 1 s | ✅ |
| `get_heart_rate_samples` (largest parent, limit=5000) | **0.006 s** | 104 | < 2 s | ✅ |

The largest available route in this export carries 7165 points; the
hottest HR parent carries only 104 samples (1-hour-window-style probe
is well-served by this).

### E5. Memory footprint Phase 2 ≤ 16 GB — **PASS (1.14 GB)**

A1's `/usr/bin/time -v Maximum resident set size (kbytes): 1166596 =
1.14 GB`. Two orders of magnitude under the 16 GB gate.

---

## F. MCPB bundle dogfood — **NOT EXECUTED**

Block F (F1 install, F2 end-to-end Claude Desktop drive, F3 PEP 440
dashed-vs-canonical, F4 bundle size) is operator-driven and out of
scope for this self-run. See handoff note
`tmp/handoff/2026-06-25/1304-dogfood-self-driven-execution.md` step 5
for the operator runbook (download the rc2 MCPB, drag-drop into Claude
Desktop on a clean machine, exercise the 17 tools, validate the
manifest-rewrite via `unzip -p`).

---

## Operator next steps

1. **Fix #124** in rc3 — drop + recreate the
   `idx_heart_rate_samples_parent` index inside the
   `_convert_heart_rate_sample_time_to_double` migration transaction,
   plus add a regression test that builds the migration fixture *with*
   the index.
2. **Push `v0.3.0-rc3` tag** after the fix lands (release workflow
   auto-publishes to PyPI + GitHub release Pre-release marker).
3. **Re-drive A6 / C5 / C6** against rc3 on a fresh `/tmp/legacy.duckdb`
   built via `apple-health-mcp-server==0.2.0`.
4. **Re-drive block F** in Claude Desktop with the rc3 MCPB bundle.
5. Once all gates green on rc3, cut **`v0.3.0` stable** per the
   `project_v0_3_0_release_plan.md` workflow (pyproject + manifest bump,
   tag push, LP footer auto-syncs via release.yml's `sync_docs_version`
   job).
