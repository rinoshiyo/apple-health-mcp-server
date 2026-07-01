# Dogfood Test Plan — apple-health-mcp-server v0.3.0-rc2

**Build under test:** `apple-health-mcp-server` v0.3.0-rc2 (PyPI canonical
`0.3.0rc2`, git tag `v0.3.0-rc2`, MCPB bundle
`apple-health-mcp-server-v0.3.0-rc2.mcpb`). **Purpose:** pre-stable
dogfood gate before the v0.3.0 final tag — confirm the v1.0.0-targeting
breaking-change batch from rc1 (audit T1/T3/T5/T6/T7/T8/T11/T12/T13/T15,
mandatory PR-B SECURITY/Compatibility, workflow PR-C pre-release flag) and
the rc2 additions (PR #116 envelope unification, PR #117 sample_time
DOUBLE migration, PR #113 LP install delinking) all behave the way
CHANGELOG.md and the corresponding GitHub issues advertise. **How to
consume this plan:** work the blocks in order A → F. Block A proves the
binary can be installed and the perf gates still hold; B exercises every
one of the 17 MCP tools shipped from `server/tools/`; C covers cross-cutting
edge cases (empty DB / large export / multi-locale ECG / boundary inputs
/ concurrent serve / malformed migration source); D verifies the public
wire contracts that the v1.0.0 freeze will inherit; E re-confirms the
perf baselines from #50/#56/#57/#60 have not regressed; F validates the
MCPB bundle end-to-end inside Claude Desktop. The dogfood is declared
successful when every Pass/Fail criterion in A–F passes on at least one
real `~1.2 GB` export. Any failure in A, D, or F is a stop-ship that
triggers an rc3 cut. Failures in B, C, or E that map cleanly to a single
tool may be allowed to ship under a documented `Known Issue` instead, at
the maintainer's discretion (see Pass/Fail rollup at the end).

---

## Common setup

All scenarios invoke the rc2 build via uvx. Define a shell alias once so
that bumping to rc3 / v0.3.0 stable / v1.0.0 requires editing a single
line instead of every invocation block in this document:

```bash
export RC2_PIN='apple-health-mcp-server==0.3.0rc2'
# Then everywhere the plan writes `uvx --from 'apple-health-mcp-server==0.3.0rc2'`
# the operator may equivalently use `uvx --from "$RC2_PIN"`.
```

The remainder of the plan keeps the literal `apple-health-mcp-server==0.3.0rc2`
form inline for self-contained readability of each scenario; the
alias above is the single-point bump for the next release cycle.

---

## A. Setup verification

### A1. Fresh import of a real Apple Health export

**Setup**

```bash
# Clean slate.
rm -rf /tmp/dogfood-export.duckdb
# Real Apple Health export directory (export.xml + electrocardiograms/*.csv
# + workout-routes/*.gpx) on the maintainer's machine.
EXPORT_DIR=/path/to/apple_health_export
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/dogfood-export.duckdb \
    import "$EXPORT_DIR" \
    2> /tmp/dogfood-import.log
```

**Expected behaviour**

The orchestrator runs Phase 1 (XML) → Phase 2 (ECG) → Phase 3 (GPX) →
Phase 4 (dedup), prints a single `Phase N:` start marker per phase on
stderr (no separate completion line — phase end is implicit at the next
`Phase N+1:` line; the import ends after the Phase 4 line), writes
exactly one row to the `imports` table, and exits 0.

**Pass/Fail criteria**

- Process exit code is `0`.
- `/tmp/dogfood-import.log` contains, in this exact order, the literal
  lines `Phase 1: Parsing export.xml`, `Phase 2: Parsing ECG files`,
  `Phase 3: Parsing GPX route files`, `Phase 4: Finalize (dedupe,
  backfill, daily stats)`.
- `/tmp/dogfood-export.duckdb` exists and is > 50 MB.
- `SELECT COUNT(*) FROM imports` returns `1`.
- `SELECT imported_at FROM imports` returns a non-NULL TIMESTAMPTZ (regression
  guard for #44).
- `SELECT export_xml_sha256 FROM imports` returns a 64-hex-char value (not
  NULL, since this is a fresh import on rc2 with #62 active).
- `SELECT MAX(version) FROM schema_version` returns `3`.

**Log artefacts to inspect**

- stderr `Phase N:` start lines (4 total).
- stderr `progress: xml NN% (X / Y MB, ~Z min remaining)` lines (only if
  the export is > 1 MB).
- `imports` row including `imported_at`, `export_xml_sha256`,
  `record_count`, `workout_count`, `duration_secs` (the canonical
  `imports` columns per `db/schema.py`; there is NO `ecg_count` column).

### A2. Phase-1 perf gate (≤ 90 s) and total wall-clock gate (≤ 130 s)

**Setup**

Same as A1, but wrap in `time`:

```bash
rm -rf /tmp/dogfood-export.duckdb
time uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/dogfood-export.duckdb \
    import "$EXPORT_DIR" \
    2> /tmp/dogfood-import.log
```

Run on the maintainer's reference ~1.2 GB export (the baseline used for
#50 / #56 / #57 / #60).

**Expected behaviour**

Total wall-clock ≤ 130 s. Phase boundaries are inferred from successive
`Phase N:` lines in the timestamped log: Phase 1 elapsed = `(Phase 2
asctime − Phase 1 asctime)` ≤ 90 s; Phase 4 elapsed = `(end-of-log
asctime − Phase 4 asctime)` ≤ 10 s where end-of-log is the exit-0
moment (use `/usr/bin/time` `real` minus the Phase 4 asctime offset, or
wrap each phase explicitly via a process wrapper).

**Pass/Fail criteria**

- `time` reports `real` ≤ 130 s.
- `(Phase 2 asctime − Phase 1 asctime)` in `/tmp/dogfood-import.log` is
  ≤ 90 s.
- `(import exit asctime − Phase 4 asctime)` is ≤ 10 s.
- A 5–10% margin above the historical baselines (~104 s total, ~82 s
  Phase 1, ~5 s Phase 4) is acceptable — Phase 1 SAX-target parse and the
  Phase 4 DELETE-based dedup must still dominate.

**Log artefacts to inspect**

- The four `Phase N:` start lines with their asctime prefixes (the
  standard `logging` formatter timestamps each line; differences give
  per-phase elapsed).

### A3. sha256 fast-path replay (no `--force`)

**Setup**

After A1 has populated `/tmp/dogfood-export.duckdb`, re-run the import
without `--force` against the same export directory:

```bash
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/dogfood-export.duckdb \
    import "$EXPORT_DIR" \
    2> /tmp/dogfood-replay.log
```

**Expected behaviour**

The orchestrator stamps the sha256 of `export.xml`, compares it to the
last `imports.export_xml_sha256` row, sees a byte-identical match, logs
`Skipping import: export.xml is byte-identical ...`, and exits 0 without
parsing the XML or writing to the DB.

**Pass/Fail criteria**

- Process exit code is `0`.
- Wall-clock ≤ 15 s (single disk read of the 1.2 GB XML for the sha256,
  no parse — on a cold-cache consumer SSD a 1 GB read is ~2-5 s, plus
  uvx startup overhead; the gate is generous to absorb jitter).
- `/tmp/dogfood-replay.log` contains the literal substring `Skipping
  import: export.xml is byte-identical`.
- `SELECT COUNT(*) FROM imports` still returns `1` (no new row).
- `imports.imported_at` is unchanged from the A1 value.

**Log artefacts to inspect**

- Single `Skipping import:` line at INFO level on stderr.
- Absence of `Phase 1: Parsing export.xml` (the fast path short-circuits
  before Phase 1).

### A4. `--force` bypasses Tier 1 only (Tier 2 still active)

**Setup**

After A1 / A3, run:

```bash
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/dogfood-export.duckdb \
    import "$EXPORT_DIR" \
    --force \
    2> /tmp/dogfood-force.log
```

**Expected behaviour**

The Tier 1 sha256 skip is bypassed (Phase 1 runs), but the Tier 2
existing-hash snapshot suppresses duplicate inserts during Phase 1
(the dominant saving — XML is still parsed but no rows are written
for already-seen hashes), and Phase 4 dedup auto-skips because nothing
new landed (per CHANGELOG v0.1.6 entry). Wall-clock should be
measurably lower than A1; the saving comes from the Phase 1 no-INSERT
path, not from Phase 4 — Phase 4 is only ~5 s of the baseline.

**Pass/Fail criteria**

- Process exit code is `0`.
- `/tmp/dogfood-force.log` does NOT contain `Skipping import: export.xml
  is byte-identical`.
- `/tmp/dogfood-force.log` contains `Phase 1: Parsing export.xml`.
- A new `imports` row is appended (`SELECT COUNT(*) FROM imports` returns
  `2`).
- On-disk DB file size growth is ≤ 5% of the A1 size (no MVCC tombstone
  balloon — the legacy bug fixed in v0.1.6).
- Wall-clock measurably below A1 (Phase 4 fast-skip is the dominant
  saving; no absolute number is documented as a gate).

**Log artefacts to inspect**

- `Phase 1: Parsing export.xml` present.
- `Phase 4: Finalize (dedupe, backfill, daily stats)` present and the
  `(import exit asctime − Phase 4 asctime)` gap is < 1 s (the
  fast-skip path).

### A5. Empty-DB UX (`serve` without prior import)

**Setup**

```bash
rm -rf /tmp/empty.duckdb
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/empty.duckdb serve \
    2> /tmp/dogfood-empty-serve.log &
SERVE_PID=$!
# Drive 17 tool calls via a MCP client (Claude Desktop with the bundle, or
# the test harness in tests/integration). For each tool, observe the
# returned text.
```

**Expected behaviour**

The bootstrap creates an empty DuckDB file (no `Error: database does not
exist`); `serve` logs a WARNING that the bootstrap fired; every
data-bearing tool returns the canonical
`apple_health_mcp.server.query.IMPORT_REQUIRED_MESSAGE`; the two
opt-outs (`get_import_history`, `run_custom_query`) stay callable.

**Pass/Fail criteria**

- `serve` does not exit with `database does not exist`.
- `/tmp/empty.duckdb` exists after `serve` starts.
- `/tmp/dogfood-empty-serve.log` contains a single WARNING about the
  bootstrap firing.
- 15 of 17 tools return a string starting with
  `Error: No Apple Health data has been imported yet.`
- `get_import_history` returns the JSON-encoded literal string `"[]"`
  (every tool wraps its rows via `json.dumps` — `json.loads(response)
  == []` is the canonical assertion form, NOT a raw Python `[]`).
- `run_custom_query("SELECT COUNT(*) FROM imports")` returns `0`
  (callable on empty DB).
- After observing tool responses, **stop the serve process before any
  follow-on read-only DuckDB probe** (`kill %1; wait`) — DuckDB holds a
  single-writer file lock while serve is up, so a parallel
  `duckdb.connect(..., read_only=True)` will fail.

**Log artefacts to inspect**

- WARNING line from the bootstrap.
- Tool responses (each one a JSON-encodable string).

### A6. Schema-migration path from v0.2.0 DB on first `serve`

**Setup**

```bash
# Step 1: build a v0.2.0 DB.
rm -rf /tmp/legacy.duckdb
uvx --from 'apple-health-mcp-server==0.2.0' \
    apple-health-mcp-server --db /tmp/legacy.duckdb import "$EXPORT_DIR"

# Step 2: confirm the legacy schema_version and sample_time type BEFORE
# touching it with rc2.
python3 -c "
import duckdb
c = duckdb.connect('/tmp/legacy.duckdb', read_only=True)
print('schema_version =', c.execute('SELECT MAX(version) FROM schema_version').fetchone())
print('sample_time type =', c.execute(\"SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'\").fetchone())
"
# Expected: schema_version = (2,), sample_time type = ('VARCHAR',)

# Step 3: start rc2 serve against the legacy DB.
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/legacy.duckdb serve \
    2> /tmp/dogfood-migrate.log &
```

**Expected behaviour**

On the first `get_connection`, `_migrate_if_needed` runs in autocommit,
detects `schema_version < 3`, executes the `_convert_heart_rate_sample_
time_to_double` migration inside a transaction, bumps `schema_version`
to `3`, and only then accepts MCP tool calls. A multi-million-row
heart_rate_samples table emits the `heart_rate_samples migration:
converting %d row(s) from VARCHAR to DOUBLE` INFO line.

**Pass/Fail criteria**

- `serve` starts without error.
- `/tmp/dogfood-migrate.log` contains `Applying migration to schema
  version 3`.
- `/tmp/dogfood-migrate.log` contains `heart_rate_samples migration:
  converting N row(s) from VARCHAR to DOUBLE` (N > 0 on a real export).
- Drive at least one tool call (e.g. `list_record_types`) via the MCP
  client to confirm tools are reachable post-migration, then stop the
  serve process (`kill %1; wait`) before the read-only probes below
  (DuckDB single-writer lock).
- After serve is stopped, `SELECT MAX(version) FROM schema_version`
  returns `3`.
- After serve is stopped, `SELECT type FROM pragma_table_info('heart_
  rate_samples') WHERE name = 'sample_time'` returns `DOUBLE`.
- `get_heart_rate_samples` (driven before serve is stopped) returns
  float `sample_time` values (not `HH:MM:SS.SSS` strings) — see B8.

**Log artefacts to inspect**

- `Applying migration to schema version 3` INFO line.
- `heart_rate_samples migration: converting %d row(s)` INFO line.

### A7. Progress emitter cadence override

**Setup**

```bash
rm -rf /tmp/progress.duckdb
APPLE_HEALTH_IMPORT_PROGRESS_SECS=5 \
    uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/progress.duckdb \
    import "$EXPORT_DIR" \
    2> /tmp/dogfood-progress.log
```

**Expected behaviour**

Phase 1 progress lines emit every ~5 s instead of the default 10 s. Each
line is newline-terminated, no `\r`, no ANSI escapes.

**Pass/Fail criteria**

- `grep -c 'progress: xml' /tmp/dogfood-progress.log` returns roughly
  Phase-1-duration ÷ 5 (e.g. 80 s phase ≈ 14–17 lines).
- Median gap between successive `progress: xml` lines is 4–6 s
  (`grep 'progress: xml' /tmp/dogfood-progress.log | awk ...`).
- No line contains `\r` or `\x1b[` escape sequences.
- The env var bounds (1..600) are honoured — values outside the bound
  clamp silently; a value of `0` or negative does not crash the importer.

**Log artefacts to inspect**

- The matched `progress: xml` lines and their timestamps.

### A8. ENV rename — `APPLE_HEALTH_LOG_*` (PR #105 / `feat!`)

**Setup**

Three sub-runs:

```bash
# (a) new prefixed name works.
APPLE_HEALTH_LOG_LEVEL=DEBUG \
    uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/empty.duckdb serve \
    2> /tmp/dogfood-env-new.log &
PID=$!; sleep 1; kill "$PID"; wait

# (b) new prefixed JSON format works.
APPLE_HEALTH_LOG_FORMAT=json \
    uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/empty.duckdb serve \
    2> /tmp/dogfood-env-json.log &
PID=$!; sleep 1; kill "$PID"; wait

# (c) OLD unprefixed names MUST no-op (not silently honoured).
LOG_LEVEL=DEBUG LOG_FORMAT=json \
    uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/empty.duckdb serve \
    2> /tmp/dogfood-env-old.log &
PID=$!; sleep 1; kill "$PID"; wait
```

**Expected behaviour**

(a) Logs include DEBUG-level lines. (b) Logs are JSON-formatted. (c) Logs
default to INFO + human format (the old vars are ignored).

**Pass/Fail criteria**

- `/tmp/dogfood-env-new.log` contains at least one `DEBUG` level line.
- `/tmp/dogfood-env-json.log` lines are valid JSON (`jq '.level'` parses).
- `/tmp/dogfood-env-old.log` does NOT contain `DEBUG` level lines.
- `/tmp/dogfood-env-old.log` is NOT JSON-formatted (first line matches
  `%(asctime)s %(levelname)-8s %(name)s: %(message)s`).

**Log artefacts to inspect**

- First 5 lines of each log file.

### A9. `imports.imported_at` non-NULL regression guard (#44)

**Setup**

Covered by A1 — re-asserted here so the executor checks it explicitly:

```bash
python3 -c "
import duckdb
c = duckdb.connect('/tmp/dogfood-export.duckdb', read_only=True)
print(c.execute('SELECT imported_at FROM imports').fetchall())
"
```

**Expected behaviour**

Every row's `imported_at` is a TIMESTAMPTZ, never NULL.

**Pass/Fail criteria**

- `SELECT COUNT(*) FROM imports WHERE imported_at IS NULL` returns `0`.
- The most recent `imported_at` is within the last hour of wall-clock.

---

## B. Tool-by-tool scenarios (all 17 tools)

Each tool name maps to `src/apple_health_mcp/server/tools/<name>.py`. For
each tool: at least one happy-path scenario and at least one
rc2-specific verification (envelope, rename, explicit-columns, or
description fix). All tool calls are driven by an MCP client (Claude
Desktop with the rc2 MCPB bundle, or the integration harness) against
the A1-populated `/tmp/dogfood-export.duckdb`.

### B1. `list_record_types`

**File:** `src/apple_health_mcp/server/tools/list_record_types.py`

**Scenarios**

1. *Happy path.* Call with no args. **Expected:** JSON array of objects
   each carrying exactly five keys:
   `{record_type, count, unit, earliest_date, latest_date}` (per
   `server/tools/list_record_types.py:25–28`).
   **Pass/Fail:**
   - Array length ≥ 1 on a real export.
   - Every element has a `record_type` key (rc2 rename per audit T1 / PR-A).
   - NO element carries a `type` key.
   - `earliest_date <= latest_date` for every row.
2. *Field rename verification.* In the JSON, assert presence of
   `record_type` and absence of `type`. **Pass/Fail:**
   - `jq '.[0] | has("record_type")'` returns `true`.
   - `jq '.[0] | has("type")'` returns `false`.

**Artefacts:** raw tool response JSON.

### B2. `query_records`

**File:** `src/apple_health_mcp/server/tools/query_records.py`

**Scenarios**

1. *Date-range + record-type filter.* Call with
   `record_type="HKQuantityTypeIdentifierStepCount"`,
   `start_date="2024-01-01"`, `end_date="2024-01-31"`. **Pass/Fail:**
   - Response is an object with keys `items`, `total`, `next_offset`
     (rc2 envelope per PR #116).
   - Every `items[i].start_date` is between `2024-01-01T00:00:00` and
     `2024-01-31T23:59:59.999999` (inclusive — see C4).
   - `total` ≥ `len(items)`.
2. *Limit boundary.* Call with `limit=0`. **Pass/Fail:**
   - Returns `Error: limit must be >= 1` (rc1 PR-A post-merge follow-up).
3. *Pagination round-trip.* Call with `limit=10` and follow `next_offset`
   until `next_offset is None`. **Pass/Fail:**
   - Sum of `len(items)` across all pages equals the first response's
     `total`.
   - `has_more` is NOT present in any response (removed per PR #116).
   - Last page has `next_offset is None`.

**Artefacts:** the envelope JSON for each page; the COUNT(*) of the
underlying source rows for cross-check.

### B3. `get_record_statistics`

**File:** `src/apple_health_mcp/server/tools/get_record_statistics.py`

**Scenarios**

1. *Each accepted period.* Call with
   `record_type="HKQuantityTypeIdentifierStepCount"` and `period` each
   of `day`, `week`, `month`, `year`. **Pass/Fail:**
   - All four calls succeed.
   - Each response's bucketing matches the period (`day` → one row per
     calendar date with samples, `month` → one row per year-month, etc.).
2. *Invalid period rejected.* Call with `period="hour"`. **Pass/Fail:**
   - Returns exactly `Error: invalid period; accepted values: day, week,
     month, year` (audit T3 + PR-A post-merge follow-up: the rejected
     value is NOT echoed in the error string, closing the prompt-injection
     vector).
3. *Period rejection does not echo user input.* Call with
   `period="dayred-team"`. **Pass/Fail:**
   - Error string is verbatim
     `Error: invalid period; accepted values: day, week, month, year`.
   - The user-supplied bytes are NOT present in the response.

**Artefacts:** raw response strings.

### B4. `list_workouts`

**File:** `src/apple_health_mcp/server/tools/list_workouts.py`

**Scenarios**

1. *Activity-type filter.* Call with
   `activity_type="HKWorkoutActivityTypeRunning"`. **Pass/Fail:**
   - Envelope `{items, total, next_offset}` shape (PR #116).
   - Every `items[i].activity_type` equals the filter.
2. *Limit boundary.* Call with `limit=0`. **Pass/Fail:**
   - Returns `Error: limit must be >= 1` (per `normalise_pagination`).
3. *Default limit cap.* Call with no `limit`. **Pass/Fail:**
   - `len(items)` ≤ 50 (`_DEFAULT_LIMIT = 50`,
     `_MAX_LIMIT = 500`, per `server/tools/list_workouts.py:32-33`).
   - `total` is the COUNT over the full filtered set, not just the page.

**Artefacts:** envelope JSON, distinct values of `activity_type` from a
direct SQL probe.

### B5. `get_workout_details`

**File:** `src/apple_health_mcp/server/tools/get_workout_details.py`

**Scenarios**

1. *Happy path.* Pick any `workout_hash` from `B4.items[0]` and call.
   **Pass/Fail:**
   - Response is a top-level object with exactly these six keys:
     `workout`, `events`, `statistics`, `metadata`, `route`,
     `has_route` (per `server/tools/get_workout_details.py`).
   - `workout` object does NOT carry `import_id` (audit T5 / PR-A — wire
     contract is now explicit columns).
   - `workout` carries `workout_hash`, `activity_type`, `start_date`,
     `end_date`, `total_distance`, `total_energy_burned`, etc.
2. *Unknown hash.* Call with `workout_hash="0" * 64`. **Pass/Fail:**
   - Returns an empty / not-found shape (no traceback).

**Artefacts:** the `workout` keys list, asserted against the tool's
explicit column projection in the source file.

### B6. `get_activity_summaries`

**File:** `src/apple_health_mcp/server/tools/get_activity_summaries.py`

**Scenarios**

1. *Date-range filter.* Call with `start_date="2024-01-01"`,
   `end_date="2024-01-31"`. **Pass/Fail:**
   - Returns a bare JSON array (not the envelope — this tool paginates
     by date range per CHANGELOG rc2).
   - No row carries `import_id` (audit T6 / PR-A — explicit columns).
   - Each row carries the exact ten projected columns:
     `date_components`, `active_energy_burned`,
     `active_energy_burned_goal`, `active_energy_burned_unit`,
     `apple_move_time`, `apple_move_time_goal`, `apple_exercise_time`,
     `apple_exercise_time_goal`, `apple_stand_hours`,
     `apple_stand_hours_goal` (per
     `server/tools/get_activity_summaries.py`).
2. *Empty range.* Call with an obviously-empty range like
   `start_date="1970-01-01"`, `end_date="1970-01-02"`. **Pass/Fail:**
   - Returns `[]` (empty array, no error).

**Artefacts:** array of row dicts; the column list from the source file.

### B7. `get_workout_route`

**File:** `src/apple_health_mcp/server/tools/get_workout_route.py`

**Scenarios**

1. *Paginated walk.* Pick a workout with a known GPS route (a
   `Running`/`Walking`/`Cycling` workout with `has_route=true` in B5).
   Call with `limit=1000` and follow `next_offset` until None.
   **Pass/Fail:**
   - Response is `{items, total, next_offset}` (PR #116 unified
     naming — verify `items`, NOT the legacy `points` key from rc1).
   - `has_more` key is absent.
   - Each `items[i]` has `latitude`, `longitude`, `elevation`,
     `timestamp`, `speed`, `course`.
   - Sum of `len(items)` across pages equals first response's `total`.
2. *`limit=0` rejection.* Call with `limit=0`. **Pass/Fail:**
   - Returns `Error: limit must be >= 1` (PR-A post-merge fix that
     closed the infinite-pagination loop bug).
3. *Offset past end.* Call with `offset=999999999` against the same
   workout chosen in scenario 1 (must have `route_points > 0`).
   **Pass/Fail:**
   - Returns `items=[]` and a `total` equal to the underlying filtered
     `SELECT COUNT(*) FROM route_points WHERE workout_hash = ?` (the
     fallback `_count_sql_from_page_sql` path in `run_query_envelope`
     re-counts the filtered set — note that for a workout with zero
     route points the legitimate `total` is `0`, so this assertion only
     holds against a workout known to carry GPS samples).
   - `next_offset is None`.

**Artefacts:** envelope JSON for each page; underlying
`SELECT COUNT(*) FROM route_points WHERE workout_hash = ?` cross-check.

### B8. `get_heart_rate_samples`

**File:** `src/apple_health_mcp/server/tools/get_heart_rate_samples.py`

**Scenarios**

1. *Happy path against an HR record.* Pick a `record_hash` from
   `query_records(record_type="HKQuantityTypeIdentifierHeartRate")` that
   has child samples. Call with `limit=100`. **Pass/Fail:**
   - Envelope `{items, total, next_offset}` shape.
   - Each `items[i].sample_time` is a `float` (DOUBLE — issue #109 / PR
     #117), NOT a `HH:MM:SS.SSS` string.
   - `sample_time` values are in `[0.0, 86400.0)` (wall-clock seconds
     since 00:00 local).
   - Each `items[i].bpm` is a positive float.
2. *Envelope offset round-trip.* Same as B7 scenario 1.
3. *Description sanity.* The tool's `description` (visible to Claude
   Desktop) explicitly states `sample_time` is wall-clock seconds since
   00:00 local (not a relative offset from the parent record), per PR-A
   post-merge follow-up. **Pass/Fail:**
   - Reading `server/tools/get_heart_rate_samples.py::DESCRIPTION`
     reveals the phrase `wall-clock seconds since 00:00 local`.

**Artefacts:** envelope JSON; the `DESCRIPTION` constant.

### B9. `list_correlations`

**File:** `src/apple_health_mcp/server/tools/list_correlations.py`

**Scenarios**

1. *Happy path.* Call with no args. **Pass/Fail:**
   - Envelope shape `{items, total, next_offset}` (PR #116 — `list_*`
     paginated tool).
   - Each `items[i]` has `correlation_hash` and `correlation_type`.
2. *Date-range filter.* Call with `start_date`/`end_date` covering the
   last 30 days. **Pass/Fail:**
   - Every `items[i].start_date` is within the requested window
     (inclusive end — see C4).

**Artefacts:** envelope JSON.

### B10. `get_correlation_details`

**File:** `src/apple_health_mcp/server/tools/get_correlation_details.py`

**Scenarios**

1. *Happy path.* Pick `correlation_hash` from B9. **Pass/Fail:**
   - Response carries the parent correlation row + its `members` (each
     member is a child `records` row referenced by
     `correlation_members.record_hash`).
   - At least one member row.
2. *Unknown hash.* Call with all zeros. **Pass/Fail:**
   - Empty / not-found response, no traceback.

**Artefacts:** response JSON; cross-check `SELECT COUNT(*) FROM
correlation_members WHERE correlation_hash = ?`.

### B11. `list_ecg_readings`

**File:** `src/apple_health_mcp/server/tools/list_ecg_readings.py`

**Scenarios**

1. *Happy path.* Call with no args. **Pass/Fail:**
   - Envelope shape `{items, total, next_offset}`.
   - Each `items[i]` carries exactly five keys: `ecg_hash`,
     `recorded_date`, `classification`, `device`, `sample_rate_hz`
     (per the projection in
     `src/apple_health_mcp/server/tools/list_ecg_readings.py:63-64`).
2. *New `limit` parameter (audit T11).* Call with `limit=5`.
   **Pass/Fail:**
   - `len(items)` ≤ 5.
3. *`limit=0` rejection (PR-A post-merge fix).* Call with `limit=0`.
   **Pass/Fail:**
   - Returns `Error: limit must be >= 1`.

**Artefacts:** envelope JSON.

### B12. `get_ecg_data`

**File:** `src/apple_health_mcp/server/tools/get_ecg_data.py`

**Scenarios**

1. *Happy path with downsampling.* Pick `ecg_hash` from B11; call with
   `include_voltages=true` (the default omits voltages — see scenario 3
   below). **Pass/Fail:**
   - Response is a top-level object with exactly four keys: `reading`,
     `stats`, `downsample_factor`, `voltages_uv` (per
     `server/tools/get_ecg_data.py:23-30, 96-105` — note the `_uv`
     suffix; the legacy `voltages` key does NOT exist).
   - `reading` does NOT carry `import_id` (audit T12 / PR-A — explicit
     columns).
   - `len(voltages_uv) == ceil(reading.sample_count / downsample_factor)`
     (or full length if downsample_factor is `1`).
2. *Default omits voltages.* Same `ecg_hash`, default args
   (`include_voltages` defaults to `false`). **Pass/Fail:**
   - `voltages_uv` is `[]` (the four keys still present, only the array
     is empty).
3. *Description regression.* **Pass/Fail:**
   - `server/tools/get_ecg_data.py::DESCRIPTION` does NOT mention
     "earlier versions" (audit T12 — that historical note was removed;
     v0.3.0 is the SemVer baseline and pre-0.3 callers are not
     supported).

**Artefacts:** response JSON; the `DESCRIPTION` constant.

### B13. `run_custom_query`

**File:** `src/apple_health_mcp/server/tools/run_custom_query.py`

**Scenarios**

1. *Happy path SELECT.* Call with `sql="SELECT COUNT(*) FROM records"`.
   **Pass/Fail:**
   - Returns the count.
2. *Non-SELECT rejected.* Call with `sql="DROP TABLE records"`.
   **Pass/Fail:**
   - Returns an error indicating only SELECT is permitted.
   - No mutation visible (the table still exists, `SELECT COUNT(*)`
     unchanged).
3. *LIMIT clamp.* Call with `sql="SELECT * FROM records LIMIT 9999999"`.
   **Pass/Fail:**
   - Returns at most the documented LIMIT cap, not the requested
     9,999,999 rows.
4. *Description table list (audit T13).* **Pass/Fail:**
   - `server/tools/run_custom_query.py::DESCRIPTION` lists every current
     table including `workout_metadata`, `correlation_members`,
     `me_attributes`, and `export_metadata`.

**Artefacts:** response strings; the `DESCRIPTION` constant.

### B14. `list_data_sources`

**File:** `src/apple_health_mcp/server/tools/list_data_sources.py`

**Scenarios**

1. *Happy path.* Call with no args. **Pass/Fail:**
   - Returns a bare JSON array (this tool paginates by source identifier,
     not by row offset — CHANGELOG rc2 carve-out).
   - Includes the maintainer's expected sources at the device-class level
     (e.g. `Apple Watch`, `iPhone`). Use only those generic class names
     in the test plan; the dogfood operator may see real source strings
     locally but must NOT paste them into issue comments or
     follow-up artefacts.
   - Each element carries exactly four keys: `source_name`,
     `record_count`, `earliest_date`, `latest_date` (per
     `server/tools/list_data_sources.py:19-24` — there is NO
     `source_version` field on this tool).
2. *Empty DB.* Re-run against `/tmp/empty.duckdb`. **Pass/Fail:**
   - Returns `IMPORT_REQUIRED_MESSAGE`.

**Artefacts:** array of source names (generic placeholders only in any
written artefact).

### B15. `get_import_history`

**File:** `src/apple_health_mcp/server/tools/get_import_history.py`

**Scenarios**

1. *Multi-import scenario.* After A1 + A4 the DB has two `imports`
   rows. Call the tool. **Pass/Fail:**
   - Returns a bare JSON array (this tool paginates by import id —
     CHANGELOG rc2 carve-out).
   - Length is `2`.
   - Rows are ORDER BY `imported_at DESC` (most recent first).
   - Each row carries explicit columns INCLUDING `export_xml_sha256`
     (audit T15 + PR-A post-merge explicit-projection follow-up).
   - No row carries any column not listed in the tool's projection (a
     future `ALTER TABLE imports ADD COLUMN` cannot leak).
2. *Empty DB.* Run against `/tmp/empty.duckdb`. **Pass/Fail:**
   - Returns the JSON-encoded literal string `"[]"`
     (`json.loads(response) == []`); this tool is one of the two
     empty-DB opt-outs that bypass `IMPORT_REQUIRED_MESSAGE`, see A5.

**Artefacts:** array of import dicts.

### B16. `list_state_of_mind`

**File:** `src/apple_health_mcp/server/tools/list_state_of_mind.py`

**Scenarios**

1. *Date-range filter.* Call with `start_date` / `end_date` covering
   the last 90 days. **Pass/Fail:**
   - Envelope shape `{items, total, next_offset}`.
   - Each `items[i]` has `start_date`, `valence`, `kind`, and optionally
     `labels`, `associations`.
2. *Labels surfaced.* Within the same date-range response, on at least
   one item `labels` is a non-empty VARCHAR string (the column is
   `labels VARCHAR` per `db/schema.py:257`; the XML importer writes the
   raw delimited string as-is per rc1 audit DB1+DB2 documentation —
   asserting list/object shape would mis-flag a correct build). Note:
   this tool exposes ONLY `start_date`, `end_date`, `limit`, `offset`
   parameters per `server/tools/list_state_of_mind.py:47-63`; no
   `min_valence` filter is available, so any valence-side check is
   read-only against the returned `items`.

**Artefacts:** envelope JSON; the underlying `record_metadata` rows for
that record.

### B17. `get_me_attributes`

**File:** `src/apple_health_mcp/server/tools/get_me_attributes.py`

**Scenarios**

1. *Round-trip.* Call with no args. **Pass/Fail:**
   - Response carries the canonical `Me` attributes:
     `date_of_birth`, `biological_sex`, `blood_type`,
     `fitzpatrick_skin_type`, `cardio_fitness_medications_use` (or the
     full set declared in `me_attributes` per `db/schema.py`).
   - Field count matches the table column count (Me 5-attribute follow-up
     from the 2026-06-21 audit, per project_data_audit_2026_06_21.md).
2. *Empty DB.* Run against `/tmp/empty.duckdb`. **Pass/Fail:**
   - Returns `IMPORT_REQUIRED_MESSAGE`.

**Artefacts:** the attribute dict (use generic / synthetic values when
the executor writes this up — no real DOB in any follow-up artefact).

---

## C. Edge cases

### C1. Empty DB → every tool returns the friendly message

**Setup:** see A5.

**Expected behaviour:** 15 tools return `IMPORT_REQUIRED_MESSAGE`;
`get_import_history` returns the JSON literal `"[]"`;
`run_custom_query` runs normally.

**Pass/Fail criteria**

- For each of the 15 data-bearing tools, the response is exactly the
  string in `apple_health_mcp.server.query.IMPORT_REQUIRED_MESSAGE` or
  starts with it (the trailing URL may be reformatted across minor
  versions per the rc1 CHANGELOG note).
- `get_import_history` returns `"[]"` (JSON-encoded empty array; assert
  via `json.loads(response) == []`).
- No tool emits a Python traceback or a raw FastMCP error envelope.

**Log artefacts:** stderr of `serve` while the 17 tool calls fire — no
ERROR-level lines from `apple_health_mcp.*` loggers.

### C2. Large export tolerance

**Setup**

A real export with `export.xml >= 1 GB`, `route_points >= 10 000` for at
least one workout, `heart_rate_samples >= 100 000` total. The
maintainer's reference 1.2 GB export from #50 satisfies this.

**Expected behaviour:** A1 import completes within the perf gates, A2
holds, and B7 / B8 envelope walks finish without OOM.

**Pass/Fail criteria**

- A1 import RSS peak (measured via `/usr/bin/time -v` `Maximum resident
  set size`) ≤ 16 GB (per E5).
- B7 paginated walk over the largest route completes in < 30 s wall-clock
  total (default `limit=5000` → ~2 pages for a 10 k-point workout).
- B8 paginated walk over the largest HR record returns all samples (sum
  of `len(items)` equals `total`).

### C3. Multi-locale ECG

**Setup**

Take an `electrocardiograms/*.csv` file with Japanese-locale headers and
another with English-locale headers, put both into the same export
directory, run A1.

**Expected behaviour:** Both parse successfully; `list_ecg_readings`
returns both. A truly unknown-locale CSV raises
`LocaleUnrecognisedError` (subclass of `HealthImportError`) with an
actionable message that lists supported locales.

**Pass/Fail criteria**

- Import succeeds (exit 0) when both Japanese + English CSVs are present.
- `list_ecg_readings` returns ≥ 2 rows.
- A synthetic CSV with garbage headers (e.g. all-zh-CN, all-de-DE) yields
  a `LocaleUnrecognisedError` whose message lists at least `English` and
  `Japanese` as supported locales and points at the issue tracker.
- The verbose guidance is emitted at most once per import run (subsequent
  failing files in the same batch get a short reference, per the v0.2.0
  CHANGELOG entry).

**Artefacts:** the error message text (no raw CSV content reproduced).

### C4. Date-only end_date is inclusive

**Setup**

```bash
# query_records with bare date strings — must include the named day.
# Pick a date with known samples in the real export.
```

Call the 5 date-filtered tools (`query_records`, `list_workouts`,
`list_ecg_readings`, `list_state_of_mind`, `list_correlations`) with
`start_date="2024-06-22"` and `end_date="2024-06-22"`.

**Expected behaviour:** Each returns records that occurred on
2024-06-22, including those after noon, per #49 / `normalise_end_date`
expanding the bare upper bound to `2024-06-22 23:59:59.999999`.

**Pass/Fail criteria**

- For each tool, `items` (or the bare array for `list_workouts`-style
  envelope) is non-empty on a day known to have data.
- Cross-check: at least one returned row's `start_date` has an
  `HH:MM:SS` part > `00:00:00`.
- Full ISO 8601 timestamps pass through unchanged (test by calling with
  `end_date="2024-06-22T12:00:00+09:00"` — rows after 12:00 are excluded).

### C5. Concurrent serve

**Setup**

```bash
# Start two serve processes against the same DB.
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/legacy.duckdb serve \
    2> /tmp/serve-A.log &
PID_A=$!
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/legacy.duckdb serve \
    2> /tmp/serve-B.log &
PID_B=$!
sleep 3  # let one of them acquire the writer lock and migrate.
```

After observing, stop both: `kill "$PID_A" "$PID_B" 2>/dev/null; wait`.

**Expected behaviour:** The first to acquire the DuckDB writer lock
runs `_migrate_if_needed` and migrates to `schema_version=3`. The
second's outcome depends on which step it reaches first:

- If it lost the writer-lock race **before** the migration completed,
  it MAY exit with a DuckDB writer-lock error (acceptable — the
  operator restarts after the winner finishes).
- If it acquired the writer lock **after** the migration completed,
  `_migrate_if_needed` sees `current >= CURRENT_SCHEMA_VERSION`, skips
  the loop, and does NOT re-run the migration.

**Pass/Fail criteria**

- At least one `serve` process starts and reaches the tool-loop without
  error.
- Exactly ONE of the two logs contains `Applying migration to schema
  version 3`. The other either skips silently OR exits cleanly with a
  single writer-lock error (no `TransactionException` traceback, no
  partial migration).
- After both processes are stopped, `SELECT MAX(version) FROM
  schema_version` returns `3`.
- Restarting either process against the now-migrated DB succeeds and
  emits NO migration line (proves the loser-process never silently
  re-ran the migration).

**Artefacts:** `/tmp/serve-A.log`, `/tmp/serve-B.log`.

### C6. Malformed sample_time at migration

**Setup**

```bash
# Build a v0.2.0 DB with a real export.
rm -rf /tmp/malformed.duckdb
uvx --from 'apple-health-mcp-server==0.2.0' \
    apple-health-mcp-server --db /tmp/malformed.duckdb import "$EXPORT_DIR"

# Inject a single malformed row so the migration's TRY_CAST returns NULL.
python3 -c "
import duckdb
c = duckdb.connect('/tmp/malformed.duckdb')
c.execute(\"INSERT INTO heart_rate_samples (parent_record_hash, sample_idx, bpm, sample_time, import_id) VALUES ('0' * 64, 999999, 60.0, 'not-a-time', (SELECT MAX(import_id) FROM imports))\")
"

# Run rc2 serve to trigger migration.
uvx --from 'apple-health-mcp-server==0.3.0rc2' \
    apple-health-mcp-server --db /tmp/malformed.duckdb serve \
    2> /tmp/dogfood-malformed.log &
```

**Expected behaviour:** Migration runs, the malformed row's sample_time
becomes NULL, exactly ONE WARNING is logged with the malformed count
(the warning does NOT list the offending VARCHAR value because it could
carry user wall-clock data — per PR #117).

**Pass/Fail criteria**

- Migration completes (exit 0, schema_version → 3).
- `/tmp/dogfood-malformed.log` contains exactly one line matching
  `heart_rate_samples migration: \d+ row\(s\) had malformed sample_time
  literals and were converted to NULL`.
- `SELECT COUNT(*) FROM heart_rate_samples WHERE sample_time IS NULL`
  returns ≥ 1.
- The WARNING message does NOT contain the literal `not-a-time` (or any
  fragment of the malformed value).

**Artefacts:** the WARNING line; the row's pre- and post-migration state.

---

## D. Wire contract verification

### D1. Envelope unification (PR #116 / 7 tools)

**Setup**

For each of the 7 tools touched by PR #116 — `query_records`,
`list_workouts`, `list_correlations`, `list_state_of_mind`,
`list_ecg_readings`, `get_heart_rate_samples`, `get_workout_route` —
call once with default args (limit small enough to force pagination).

**Expected behaviour**

Each response is an object with exactly three top-level keys: `items`,
`total`, `next_offset`. `has_more` is absent. `offset > total` returns
`items=[]` with a non-zero `total` (the fallback COUNT(*) path).
`limit < 1` is rejected with `Error: limit must be >= 1`.

**Pass/Fail criteria (per tool)**

- `set(response.keys()) == {"items", "total", "next_offset"}`.
- `"has_more" not in response`.
- `get_workout_route` uses the key `items`, NOT the rc1-era `points`.
- Call with `offset=10**9` AND filter values known to match ≥ 1 row
  (the fallback `_count_sql_from_page_sql` is filter-scoped — calling
  with a date range that matches zero rows legitimately returns
  `total=0`, so the operator must pre-pick filter values via
  `query_records` / `list_workouts` / etc. that the unfiltered tool
  already returned data for) → `items == []` and `total > 0`.
- Call with `limit=0` → `Error: limit must be >= 1`.

**Artefacts:** the 7 response JSON shapes.

### D2. `record_type` field rename (T1 / PR-A)

**Setup**

Call `list_record_types`. Inspect the raw JSON.

**Pass/Fail criteria**

- Every row has `record_type` and not `type`.
- A pre-rc1 client that read `row["type"]` would `KeyError` against the
  new shape (proves the rename is a real breaking change, as
  CHANGELOG advertises).

### D3. `APPLE_HEALTH_LOG_*` env rename (ENV1 / PR-A)

**Setup:** see A8.

**Pass/Fail criteria**

- Setting `LOG_LEVEL=DEBUG` alone does NOT enable DEBUG output.
- Setting `LOG_FORMAT=json` alone does NOT switch to JSON output.
- Setting `APPLE_HEALTH_LOG_LEVEL=DEBUG` DOES enable DEBUG output.
- Setting `APPLE_HEALTH_LOG_FORMAT=json` DOES switch to JSON output.

### D4. Tool descriptions reach the LLM

**Setup**

In Claude Desktop with the rc2 MCPB bundle installed, ask Claude to
"list the available `apple-health-mcp-server` tools and their descriptions".

**Expected behaviour**

Claude sees the rc2 descriptions, which include the audit-T1 / T3 / T8 /
T13 corrections.

**Pass/Fail criteria**

- The description for `list_record_types` mentions `record_type` (not
  `type`).
- The description for `get_record_statistics` lists the supported
  output columns (`period, count, avg_value, min_value, max_value,
  sum_value`) per
  `server/tools/get_record_statistics.py:17–22`. The period-rejection
  contract is NOT part of the description; the runtime error string
  contract is exercised by B3 scenario 2/3, not D4.
- The description for `get_heart_rate_samples` includes the literal
  phrase `wall-clock seconds since 00:00 local`.
- The description for `run_custom_query` lists `workout_metadata`,
  `correlation_members`, `me_attributes`, and `export_metadata` among
  the available tables.
- The description for `get_workout_route` mentions `{items, total,
  next_offset}` and NOT `points`.

**Artefacts:** Claude's tool list response (paraphrase only — no
private session content reproduced).

### D5. DB schema: sample_time is DOUBLE, schema_version = 3

**Setup**

After A1 (and with no `serve` process holding the DuckDB single-writer
lock against `/tmp/dogfood-export.duckdb`), probe the schema:

```bash
python3 -c "
import duckdb
c = duckdb.connect('/tmp/dogfood-export.duckdb', read_only=True)
print('sample_time:', c.execute(\"SELECT type FROM pragma_table_info('heart_rate_samples') WHERE name = 'sample_time'\").fetchone())
print('schema_version:', c.execute('SELECT MAX(version) FROM schema_version').fetchone())
print('imports cols:', c.execute(\"SELECT name FROM pragma_table_info('imports')\").fetchall())
"
```

**Expected behaviour**

`sample_time` is `DOUBLE`; `schema_version` is `3`; `imports` carries
`export_xml_sha256`.

**Pass/Fail criteria**

- `sample_time` type is `DOUBLE`.
- `MAX(version)` from `schema_version` is `3`.
- `imports` columns include `export_xml_sha256`.
- Note: the project tracks the schema sentinel in a `schema_version`
  table (per `db/migrations.py`), NOT a `_meta` table — the prompt's
  reference to `_meta` is incorrect and the executor should probe
  `schema_version` instead.

---

## E. Performance baseline

All numbers measured on the maintainer's reference ~1.2 GB
`export.xml` from #50 (`#50` gate context). On smaller exports the
absolute numbers must be smaller in proportion; on larger exports the
gates need to be re-baselined first.

### E1. Total import wall-clock ≤ 130 s

**Setup:** see A2.

**Pass/Fail:** `time real` ≤ 130 s. Historical baseline ~104 s.

### E2. Phase 1 (XML SAX target) ≤ 90 s

**Setup:** parse the gap between the `Phase 1: Parsing export.xml` and
`Phase 2: Parsing ECG files` lines in the A2 log (their asctime
prefixes are the canonical Phase 1 start / end markers — there is no
separate `Completed phase 1` line).

**Pass/Fail:** gap ≤ 90 s. Historical baseline ~82 s after PR #59.

### E3. Phase 4 (dedup) ≤ 10 s

**Setup:** parse the gap between `Phase 4: Finalize (dedupe, backfill,
daily stats)` and the import process exit timestamp (use `time` `real`
minus the offset of all earlier-phase elapsed gaps, or wrap the import
call in a stopwatch that stops at process exit).

**Pass/Fail:** gap ≤ 10 s. Historical baseline ~5 s after PR #61.

### E4. Per-tool response time on 10k-row queries

**Setup**

```bash
# Time get_workout_route over a 10k-point route, default limit=5000.
# Time get_heart_rate_samples over a 1-hour HR window (~3600 samples).
# Use a shell wrapper or the integration harness to call each tool with
# a stopwatch.
```

**Pass/Fail**

- `get_workout_route` single-page call (default `limit=5000`) returns in
  < 1 s wall-clock.
- `get_heart_rate_samples` over a 1-hour window returns in < 2 s
  wall-clock.

### E5. Memory footprint Phase 2 ≤ 16 GB on 1 GB export

**Setup**

Run A1 wrapped in `/usr/bin/time -v` and inspect `Maximum resident set
size (kbytes)`.

**Pass/Fail**

- Peak RSS ≤ 16 × 1024 × 1024 KB = 16 777 216 KB.
- Historical baseline well under this (~150 MB Python RSS increase from
  the v0.1.6 records-batch bump, per CHANGELOG #56).

---

## F. MCPB bundle dogfood

### F1. Bundle install on a clean machine

**Setup**

On a clean machine (no `apple-health-mcp-server` in uvx cache, no entry
in Claude Desktop's connectors):

1. Download `apple-health-mcp-server-v0.3.0-rc2.mcpb` from
   <https://github.com/rinoshiyo/apple-health-mcp-server/releases/tag/v0.3.0-rc2>.
2. Drag-and-drop onto the Claude Desktop Connectors panel.
3. Confirm the install dialog and restart Claude Desktop.

**Expected behaviour**

The bundle registers itself, Claude Desktop spawns `uvx --from
apple-health-mcp-server==0.3.0-rc2 apple-health-mcp-server serve`, and
the 17 tools appear in Claude's tool list.

**Pass/Fail criteria**

- The Connectors panel lists `apple-health-mcp-server` with version
  `0.3.0-rc2`.
- No "missing dependency" / "command not found" dialog appears (`uv`
  must be on PATH).
- Claude can list the 17 tool names.
- The `manifest.json` inside the downloaded `.mcpb` has `mcp_config.args`
  equal to `["--from", "apple-health-mcp-server==0.3.0-rc2",
  "apple-health-mcp-server", "serve"]` — proves the release workflow's
  args rewrite (audit #78 mandatory PR-B) fired correctly so uvx pins to
  rc2 and is NOT silently upgraded by uvx's cache-refresh path.
  **Important:** compare against the manifest **extracted from the
  downloaded `.mcpb` zip**, NOT the repo-root `manifest.json` (which
  intentionally ships the unpinned form `["apple-health-mcp-server",
  "serve"]`; the release workflow rewrites the pin at pack time only).

**Artefacts:** screenshot of Claude Desktop's connector panel showing
the pinned version; `unzip -p <bundle>.mcpb manifest.json | jq .mcp_config`.

### F2. End-to-end tool drive inside Claude Desktop

**Setup**

With F1 installed and the maintainer's real export imported into the
default DB path, ask Claude inside Claude Desktop:

1. "What record types are in my Apple Health data?" (→ `list_record_types`)
2. "How many steps did I take in the last week?" (→ `query_records` or
   `get_record_statistics`)
3. "Show me my last 5 workouts." (→ `list_workouts`)
4. Then for each of the remaining tools, drive a representative call
   ("Show me the GPS route for my last running workout", "What's my
   most recent ECG", etc.).

**Expected behaviour**

Each Claude turn calls the right tool, receives a well-formed envelope
or bare array, and renders a coherent human-readable answer. No `KeyError
'type'` (would mean rc1 cache stuck) or `KeyError 'points'` (would mean
rc1 envelope stuck) appears in the displayed JSON.

**Pass/Fail criteria**

- All 17 tools fire at least once across the session.
- No tool response contains a Python traceback or a `Error: invalid
  period` echo of a user-supplied string.
- The Claude-side ergonomics are acceptable: descriptions guide Claude
  to the right tool without explicit hand-holding.

**Artefacts:** session notes (paraphrased — no user data reproduced).

### F3. PEP 440 dashed-vs-canonical handling

**Setup**

```bash
# Both forms must work in uvx --from pin.
uvx --from 'apple-health-mcp-server==0.3.0-rc2' apple-health-mcp-server --version
uvx --from 'apple-health-mcp-server==0.3.0rc2' apple-health-mcp-server --version
```

**Expected behaviour**

Both invocations resolve to the same artefact (PyPI canonical
`0.3.0rc2`) and print `0.3.0rc2` (PEP 440 normalises the dash).

**Pass/Fail criteria**

- Both `--version` calls succeed and print `0.3.0rc2`.
- The `manifest.json` form `apple-health-mcp-server==0.3.0-rc2` (with
  dash) resolves correctly when Claude Desktop spawns the server — this
  is the form the release workflow writes.

### F4. Bundle size sanity check

**Setup**

```bash
ls -lh apple-health-mcp-server-v0.3.0-rc2.mcpb
unzip -l apple-health-mcp-server-v0.3.0-rc2.mcpb
```

**Expected behaviour**

The bundle is ~1 KB of metadata only (no wheel, no Python source) — per
the v0.2.0 CHANGELOG, the bundle wraps the same `uvx ... serve`
invocation as the manual JSON path, so the wheel is fetched from PyPI at
launch time.

**Pass/Fail criteria**

- Bundle size < 10 KB.
- Bundle contents are `manifest.json` plus minimal metadata; no `*.whl`,
  no `*.py`.

---

## Pass/Fail rollup

### Declaring the dogfood successful

The dogfood is successful — v0.3.0 stable may be cut — when:

- **Every** Pass/Fail criterion in blocks A, D, and F passes on at least
  one real ~1.2 GB Apple Health export.
- **Every** tool in block B passes its happy-path scenario; tool-specific
  rc2 verifications (envelope, rename, explicit-columns,
  description) all pass.
- **Every** edge case in block C passes.
- **Every** perf gate in block E passes (≤ 130 s total, ≤ 90 s Phase 1,
  ≤ 10 s Phase 4, < 1 s `get_workout_route`, < 2 s `get_heart_rate_samples`
  hour-window, RSS ≤ 16 GB).

### Triggers for an rc3 cut (must-fix before stable)

- Any failure in A (setup / migration / env / perf gate path is broken).
- Any failure in D (wire contract regression — the v1.0.0 baseline is
  not what CHANGELOG advertises).
- Any failure in F1 / F3 (bundle install or pin form broken — first-time
  users cannot get the server running).
- Any tool in B returning a Python traceback or leaking `import_id`
  (audit T5/T6/T12 regression).
- Any C6 violation that produces an error message containing the
  user-supplied malformed value (prompt-injection vector reopened).

### Triggers for a documented `Known Issue` (may ship under maintainer
discretion)

- A B-block scenario that fails only on an edge tool (e.g. a tool that
  returns correct data but with a sub-optimal default `limit`) and does
  not break any wire contract.
- An E-block perf gate missed by < 10% on a known-noisy host.
- An F2 ergonomics observation that Claude picks the wrong tool for a
  prompt — recorded as a follow-up issue, not a release blocker.

### Out of scope

- Landing page changes (LP install snippet, hero rotator, GA tracking) —
  the LP is shipped, GitHub `/releases/latest` covers the rc2 → stable
  transition automatically per PR #113 / CHANGELOG rc2.
- Future perf work beyond the #56 / #57 / #60 baseline (Phase 3 GPX
  parallelisation, multiprocessing chunk, pyo3 Rust extension) — tracked
  as `priority:future` backlog.
- Adding new MCP tools beyond the 17 already shipped — out of scope for
  v0.3.0 stable; the 17 are the v1.0.0 freeze surface.
- Adding new schema columns / tables — Layer 2 changes per PR #107, can
  ship in a future minor without a rc cycle.
- Real user data in any artefact, screenshot, or follow-up issue — every
  device UUID, source name, GPS point, DOB, and biometric value must be
  replaced with a generic placeholder (`Apple Watch`, `iPhone`,
  `<workout-hash>`, etc.) before publication.
