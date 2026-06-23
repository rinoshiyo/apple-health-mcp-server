"""DuckDB schema definitions, deduplication, and derived-column population.

Ported from the Rust reference implementation (``src/db.rs``) with three
deliberate Python-only changes:

* ``export_metadata`` — root-level ``<HealthData locale="...">`` attribute and
  ``<ExportDate value="...">`` value, keyed by ``import_id``.
* ``me_attributes`` — the five ``<Me ...>`` element fields (date of birth,
  biological sex, blood type, Fitzpatrick skin type, cardio-fitness medications
  use), keyed by ``import_id``.
* ``workout_routes.device`` — ``<WorkoutRoute device="...">`` attribute that
  the Rust version dropped on the floor.

Every timestamp column is ``TIMESTAMPTZ`` (UTC instant under the hood with
session-TZ rendering on read). The Rust port stored XML attributes as naive
``TIMESTAMP`` holding local wall-clock time and shifted GPX UTC timestamps
back to local using a per-workout ``start_offset_minutes`` column; that
workaround is gone — the importers now feed the raw offset/Z-suffixed
strings straight through to DuckDB's TIMESTAMPTZ parser.

The audit memory ``project_data_audit_2026_06_21`` and the TZ memo
``project_tz_offset_inconsistency`` document the justification.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS records (
    record_hash     VARCHAR,
    record_type     VARCHAR NOT NULL,
    value           DOUBLE,
    text_value      VARCHAR,
    unit            VARCHAR,
    source_name     VARCHAR,
    source_version  VARCHAR,
    device          VARCHAR,
    creation_date   TIMESTAMPTZ,
    start_date      TIMESTAMPTZ NOT NULL,
    end_date        TIMESTAMPTZ NOT NULL,
    import_id       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS record_metadata (
    record_hash     VARCHAR NOT NULL,
    key             VARCHAR NOT NULL,
    value           VARCHAR
);

CREATE TABLE IF NOT EXISTS workouts (
    workout_hash         VARCHAR,
    activity_type        VARCHAR NOT NULL,
    duration             DOUBLE,
    duration_unit        VARCHAR,
    total_distance       DOUBLE,
    total_distance_unit  VARCHAR,
    total_energy_burned  DOUBLE,
    total_energy_unit    VARCHAR,
    source_name          VARCHAR,
    source_version       VARCHAR,
    device               VARCHAR,
    creation_date        TIMESTAMPTZ,
    start_date           TIMESTAMPTZ NOT NULL,
    end_date             TIMESTAMPTZ NOT NULL,
    import_id            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_events (
    workout_hash    VARCHAR NOT NULL,
    event_type      VARCHAR NOT NULL,
    date            TIMESTAMPTZ,
    duration        DOUBLE,
    duration_unit   VARCHAR
);

CREATE TABLE IF NOT EXISTS workout_statistics (
    workout_hash    VARCHAR NOT NULL,
    stat_type       VARCHAR NOT NULL,
    start_date      TIMESTAMPTZ,
    end_date        TIMESTAMPTZ,
    average         DOUBLE,
    minimum         DOUBLE,
    maximum         DOUBLE,
    sum             DOUBLE,
    unit            VARCHAR
);

CREATE TABLE IF NOT EXISTS activity_summaries (
    date_components               VARCHAR,
    active_energy_burned          DOUBLE,
    active_energy_burned_goal     DOUBLE,
    active_energy_burned_unit     VARCHAR,
    apple_move_time               DOUBLE,
    apple_move_time_goal          DOUBLE,
    apple_exercise_time           DOUBLE,
    apple_exercise_time_goal      DOUBLE,
    apple_stand_hours             DOUBLE,
    apple_stand_hours_goal        DOUBLE,
    import_id                     VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS ecg_readings (
    ecg_hash         VARCHAR,
    recorded_date    TIMESTAMPTZ NOT NULL,
    classification   VARCHAR,
    device           VARCHAR,
    sample_rate_hz   DOUBLE,
    symptoms         VARCHAR,
    software_version VARCHAR,
    import_id        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS ecg_samples (
    ecg_hash    VARCHAR NOT NULL,
    sample_idx  INTEGER NOT NULL,
    voltage_uv  DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS route_points (
    point_hash    VARCHAR,
    workout_hash  VARCHAR,
    latitude      DOUBLE NOT NULL,
    longitude     DOUBLE NOT NULL,
    elevation     DOUBLE,
    timestamp     TIMESTAMPTZ NOT NULL,
    speed         DOUBLE,
    course        DOUBLE,
    h_accuracy    DOUBLE,
    v_accuracy    DOUBLE,
    import_id     VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_metadata (
    workout_hash    VARCHAR NOT NULL,
    key             VARCHAR NOT NULL,
    value           VARCHAR,
    import_id       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_routes (
    workout_hash    VARCHAR NOT NULL,
    file_path       VARCHAR NOT NULL,
    source_name     VARCHAR,
    source_version  VARCHAR,
    device          VARCHAR,
    creation_date   TIMESTAMPTZ,
    start_date      TIMESTAMPTZ,
    end_date        TIMESTAMPTZ,
    import_id       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS heart_rate_samples (
    parent_record_hash  VARCHAR NOT NULL,
    sample_idx          INTEGER NOT NULL,
    bpm                 DOUBLE,
    sample_time         VARCHAR,
    import_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS correlations (
    correlation_hash    VARCHAR NOT NULL,
    correlation_type    VARCHAR NOT NULL,
    source_name         VARCHAR,
    source_version      VARCHAR,
    device              VARCHAR,
    creation_date       TIMESTAMPTZ,
    start_date          TIMESTAMPTZ NOT NULL,
    end_date            TIMESTAMPTZ NOT NULL,
    import_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS correlation_members (
    correlation_hash    VARCHAR NOT NULL,
    record_hash         VARCHAR NOT NULL,
    import_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    import_id          VARCHAR,
    export_dir         VARCHAR NOT NULL,
    imported_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    record_count       BIGINT,
    workout_count      BIGINT,
    duration_secs      DOUBLE,
    -- Hex sha256 of the source export.xml. NULL on rows finalized before
    -- the column was introduced (#62); a fresh import always stamps it so
    -- the orchestrator can match a subsequent re-import against the most
    -- recent stamped row and exit early when the file is byte-identical.
    export_xml_sha256  VARCHAR
);

-- Captures the root <HealthData locale="..."> attribute and the
-- <ExportDate value="..."> element value. Keyed by import_id so multiple
-- imports stay distinguishable.
CREATE TABLE IF NOT EXISTS export_metadata (
    import_id    VARCHAR NOT NULL,
    export_date  TIMESTAMPTZ,
    locale       VARCHAR
);

-- All five <Me ...> attributes. Apple emits at most one <Me> element per
-- export, so this table holds one row per import_id.
CREATE TABLE IF NOT EXISTS me_attributes (
    import_id                              VARCHAR NOT NULL,
    date_of_birth                          VARCHAR,
    biological_sex                         VARCHAR,
    blood_type                             VARCHAR,
    fitzpatrick_skin_type                  VARCHAR,
    cardio_fitness_medications_use         VARCHAR
);

-- StateOfMind (iOS 17+) deserves first-class storage because the generic
-- record_metadata path loses the structured valence / labels / associations
-- relationship. Linked back to the parent `records` row via record_hash.
CREATE TABLE IF NOT EXISTS state_of_mind (
    record_hash   VARCHAR NOT NULL,
    valence       DOUBLE,
    kind          VARCHAR,
    labels        VARCHAR,
    associations  VARCHAR,
    import_id     VARCHAR NOT NULL
);
"""


_DEDUPLICATE_SQL = """
-- Issue #60: targeted ``DELETE WHERE rowid IN (... ROW_NUMBER OVER ... > 1)``
-- per table, instead of the historic ``CREATE OR REPLACE TABLE foo AS
-- SELECT DISTINCT ON (...)`` full-table rewrite. The legacy form paid the
-- write cost of every row in every table on every import even when there
-- were zero duplicates (the overwhelmingly common case of a fresh import
-- with a unique ``import_id``); the DELETE form only writes for rows that
-- actually need to disappear. Semantics are preserved byte-for-byte by
-- mirroring each block's ``ORDER BY`` clause inside the corresponding
-- ``ROW_NUMBER() OVER (PARTITION BY <key> ORDER BY <tie-breakers>)`` --
-- the row that survives is the same row the DISTINCT ON path kept.
--
-- ``_REAPPLY_CONSTRAINTS_SQL`` below now finds the NOT NULL / DEFAULT
-- constraints already intact (the DELETE path does not strip them like
-- the old ``CREATE OR REPLACE TABLE`` did) so its ALTERs are no-ops for
-- new imports. It stays in place as a one-shot migration for any DB
-- that finalized under a pre-#44 schema and still has the stripped
-- constraints sitting in its on-disk file.

DELETE FROM records WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY record_hash
            ORDER BY import_id DESC, creation_date DESC
        ) AS rn
        FROM records
    ) WHERE rn > 1
);

DELETE FROM record_metadata WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY record_hash, key
            ORDER BY value
        ) AS rn
        FROM record_metadata
    ) WHERE rn > 1
);

DELETE FROM workouts WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY workout_hash
            ORDER BY import_id DESC, creation_date DESC
        ) AS rn
        FROM workouts
    ) WHERE rn > 1
);

DELETE FROM activity_summaries WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY date_components
            ORDER BY import_id DESC
        ) AS rn
        FROM activity_summaries
    ) WHERE rn > 1
);

DELETE FROM ecg_readings WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY ecg_hash
            ORDER BY import_id DESC
        ) AS rn
        FROM ecg_readings
    ) WHERE rn > 1
);

DELETE FROM ecg_samples WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY ecg_hash, sample_idx
            ORDER BY voltage_uv
        ) AS rn
        FROM ecg_samples
    ) WHERE rn > 1
);

DELETE FROM route_points WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY point_hash
            ORDER BY import_id DESC
        ) AS rn
        FROM route_points
    ) WHERE rn > 1
);

DELETE FROM workout_metadata WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY workout_hash, key
            ORDER BY import_id DESC
        ) AS rn
        FROM workout_metadata
    ) WHERE rn > 1
);

DELETE FROM workout_routes WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY workout_hash, file_path
            ORDER BY import_id DESC
        ) AS rn
        FROM workout_routes
    ) WHERE rn > 1
);

-- workout_events and workout_statistics carry no import_id column, so the
-- dedupe key has to come from the row's own structure. Apple Health spec
-- emits at most one event per (workout, type, date) and one statistic per
-- (workout, stat_type) — re-importing the same export collapses cleanly
-- under those keys. ``ORDER BY`` inside ``ROW_NUMBER`` mirrors the legacy
-- ``DISTINCT ON ... ORDER BY ...`` exactly so any tie-break is
-- deterministic.
DELETE FROM workout_events WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY workout_hash, event_type, date
            ORDER BY workout_hash, event_type, date
        ) AS rn
        FROM workout_events
    ) WHERE rn > 1
);

DELETE FROM workout_statistics WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY workout_hash, stat_type
            ORDER BY start_date
        ) AS rn
        FROM workout_statistics
    ) WHERE rn > 1
);

DELETE FROM heart_rate_samples WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY parent_record_hash, sample_idx
            ORDER BY import_id DESC
        ) AS rn
        FROM heart_rate_samples
    ) WHERE rn > 1
);

DELETE FROM correlations WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY correlation_hash
            ORDER BY import_id DESC
        ) AS rn
        FROM correlations
    ) WHERE rn > 1
);

DELETE FROM correlation_members WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY correlation_hash, record_hash
            ORDER BY import_id DESC
        ) AS rn
        FROM correlation_members
    ) WHERE rn > 1
);

DELETE FROM imports WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY import_id
            ORDER BY imported_at DESC
        ) AS rn
        FROM imports
    ) WHERE rn > 1
);

-- The next three tables hold at most one row per import_id (or per
-- record_hash for state_of_mind), but the dedupe still adds a secondary
-- tie-break column so the surviving row stays deterministic if a partial /
-- replayed import happens to insert the same key twice. import_id DESC
-- matches the "prefer the newest import" convention used everywhere else
-- in this block.
DELETE FROM export_metadata WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY import_id
            ORDER BY import_id DESC, export_date DESC
        ) AS rn
        FROM export_metadata
    ) WHERE rn > 1
);

DELETE FROM me_attributes WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY import_id
            ORDER BY import_id DESC, date_of_birth
        ) AS rn
        FROM me_attributes
    ) WHERE rn > 1
);

DELETE FROM state_of_mind WHERE rowid IN (
    SELECT rowid FROM (
        SELECT rowid, ROW_NUMBER() OVER (
            PARTITION BY record_hash
            ORDER BY import_id DESC, valence
        ) AS rn
        FROM state_of_mind
    ) WHERE rn > 1
);
"""


# Issue #44 fix: re-apply every NOT NULL constraint and the one DEFAULT
# clause that the source schema declares, so the post-dedup tables match
# the contract ``_CREATE_TABLES_SQL`` set out.
#
# Why this block still exists after #60: the pre-#60 dedup path used
# ``CREATE OR REPLACE TABLE foo AS SELECT ... FROM foo``, which infers
# column types from the SELECT projection but does NOT carry constraints
# through. After dedup, ``PRAGMA table_info(<table>)`` reported every
# column nullable with no default — the visible bug being
# ``imports.imported_at`` writing as NULL on every import. #44 added this
# block to repair the schema in place. #60 then rewrote dedup as targeted
# DELETEs so the schema is no longer stripped at all; the ALTERs below
# are now no-ops on fresh imports BUT remain load-bearing for any DB
# whose on-disk schema was finalized under the pre-#44 path and still
# carries the stripped constraints. Keep them as a one-shot migration.
#
# ``SET DEFAULT`` precedes ``SET NOT NULL`` on ``imports.imported_at`` so the
# DEFAULT is in place before NOT NULL comes into force. For an empty table
# the order is moot, but the pattern is correct for any future case where
# the ALTER fires against a populated table.
_RESTORE_CONSTRAINTS_SQL = """
-- records
ALTER TABLE records ALTER COLUMN record_type SET NOT NULL;
ALTER TABLE records ALTER COLUMN start_date SET NOT NULL;
ALTER TABLE records ALTER COLUMN end_date SET NOT NULL;
ALTER TABLE records ALTER COLUMN import_id SET NOT NULL;

-- record_metadata
ALTER TABLE record_metadata ALTER COLUMN record_hash SET NOT NULL;
ALTER TABLE record_metadata ALTER COLUMN key SET NOT NULL;

-- workouts
ALTER TABLE workouts ALTER COLUMN activity_type SET NOT NULL;
ALTER TABLE workouts ALTER COLUMN start_date SET NOT NULL;
ALTER TABLE workouts ALTER COLUMN end_date SET NOT NULL;
ALTER TABLE workouts ALTER COLUMN import_id SET NOT NULL;

-- workout_events
ALTER TABLE workout_events ALTER COLUMN workout_hash SET NOT NULL;
ALTER TABLE workout_events ALTER COLUMN event_type SET NOT NULL;

-- workout_statistics
ALTER TABLE workout_statistics ALTER COLUMN workout_hash SET NOT NULL;
ALTER TABLE workout_statistics ALTER COLUMN stat_type SET NOT NULL;

-- activity_summaries
ALTER TABLE activity_summaries ALTER COLUMN import_id SET NOT NULL;

-- ecg_readings
ALTER TABLE ecg_readings ALTER COLUMN recorded_date SET NOT NULL;
ALTER TABLE ecg_readings ALTER COLUMN import_id SET NOT NULL;

-- ecg_samples
ALTER TABLE ecg_samples ALTER COLUMN ecg_hash SET NOT NULL;
ALTER TABLE ecg_samples ALTER COLUMN sample_idx SET NOT NULL;
ALTER TABLE ecg_samples ALTER COLUMN voltage_uv SET NOT NULL;

-- route_points
ALTER TABLE route_points ALTER COLUMN latitude SET NOT NULL;
ALTER TABLE route_points ALTER COLUMN longitude SET NOT NULL;
ALTER TABLE route_points ALTER COLUMN timestamp SET NOT NULL;
ALTER TABLE route_points ALTER COLUMN import_id SET NOT NULL;

-- workout_metadata
ALTER TABLE workout_metadata ALTER COLUMN workout_hash SET NOT NULL;
ALTER TABLE workout_metadata ALTER COLUMN key SET NOT NULL;
ALTER TABLE workout_metadata ALTER COLUMN import_id SET NOT NULL;

-- workout_routes
ALTER TABLE workout_routes ALTER COLUMN workout_hash SET NOT NULL;
ALTER TABLE workout_routes ALTER COLUMN file_path SET NOT NULL;
ALTER TABLE workout_routes ALTER COLUMN import_id SET NOT NULL;

-- heart_rate_samples
ALTER TABLE heart_rate_samples ALTER COLUMN parent_record_hash SET NOT NULL;
ALTER TABLE heart_rate_samples ALTER COLUMN sample_idx SET NOT NULL;
ALTER TABLE heart_rate_samples ALTER COLUMN import_id SET NOT NULL;

-- correlations
ALTER TABLE correlations ALTER COLUMN correlation_hash SET NOT NULL;
ALTER TABLE correlations ALTER COLUMN correlation_type SET NOT NULL;
ALTER TABLE correlations ALTER COLUMN start_date SET NOT NULL;
ALTER TABLE correlations ALTER COLUMN end_date SET NOT NULL;
ALTER TABLE correlations ALTER COLUMN import_id SET NOT NULL;

-- correlation_members
ALTER TABLE correlation_members ALTER COLUMN correlation_hash SET NOT NULL;
ALTER TABLE correlation_members ALTER COLUMN record_hash SET NOT NULL;
ALTER TABLE correlation_members ALTER COLUMN import_id SET NOT NULL;

-- imports — also restore the DEFAULT so the orchestrator INSERT can keep
-- omitting the column and the timestamp still populates.
ALTER TABLE imports ALTER COLUMN export_dir SET NOT NULL;
ALTER TABLE imports ALTER COLUMN imported_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE imports ALTER COLUMN imported_at SET NOT NULL;

-- export_metadata
ALTER TABLE export_metadata ALTER COLUMN import_id SET NOT NULL;

-- me_attributes
ALTER TABLE me_attributes ALTER COLUMN import_id SET NOT NULL;

-- state_of_mind
ALTER TABLE state_of_mind ALTER COLUMN record_hash SET NOT NULL;
ALTER TABLE state_of_mind ALTER COLUMN import_id SET NOT NULL;
"""


# Indexes live in their own SQL block so ensure_schema can install them on a
# fresh database that has not yet run deduplicate_tables. The block is also
# re-issued by deduplicate_tables itself: the historic (#60-pre) dedup path
# used CREATE OR REPLACE TABLE which dropped associated indexes, and the
# re-apply was load-bearing. Since #60 rewrote dedup as targeted DELETEs
# the indexes survive untouched, but the re-issue stays in place as a
# one-shot reinstall for any DB that finalized under the pre-#60 schema
# and is missing them. CREATE INDEX IF NOT EXISTS is idempotent so the
# warm path eats no work.
_CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_records_type_date ON records(record_type, start_date);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_name);
CREATE INDEX IF NOT EXISTS idx_workouts_type_date ON workouts(activity_type, start_date);
CREATE INDEX IF NOT EXISTS idx_route_points_workout ON route_points(workout_hash);
CREATE INDEX IF NOT EXISTS idx_workout_metadata_hash ON workout_metadata(workout_hash);
CREATE INDEX IF NOT EXISTS idx_workout_routes_hash ON workout_routes(workout_hash);
CREATE INDEX IF NOT EXISTS idx_heart_rate_samples_parent
    ON heart_rate_samples(parent_record_hash);
CREATE INDEX IF NOT EXISTS idx_correlations_type_date
    ON correlations(correlation_type, start_date);
CREATE INDEX IF NOT EXISTS idx_correlation_members_correlation
    ON correlation_members(correlation_hash);
CREATE INDEX IF NOT EXISTS idx_correlation_members_record
    ON correlation_members(record_hash);
"""


# Number of tables ensure_schema() creates. Derived from the canonical SQL
# string so adding or removing a CREATE TABLE statement cannot drift away
# from the smoke-check constant.
TABLE_COUNT = len(re.findall(r"\bCREATE TABLE IF NOT EXISTS\b", _CREATE_TABLES_SQL))


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all tables and indexes if they do not already exist.

    Tables are created without ``PRIMARY KEY`` constraints so the bulk loader
    can append duplicates; deduplication is a separate phase
    (:func:`deduplicate_tables`) that runs after the import. Indexes are
    installed here so ensure_schema-only callers (read-only consumers,
    integration tests, dry-runs) get an indexed database without having to
    run the full dedupe pipeline.
    """
    conn.execute(_CREATE_TABLES_SQL)
    conn.execute(_CREATE_INDEXES_SQL)


def _legacy_schema_needs_constraint_repair(conn: duckdb.DuckDBPyConnection) -> bool:
    """Detect a pre-#44 DB whose dedup-stripped schema still needs repair.

    Probes ``imports.imported_at`` for its NOT NULL flag -- the column
    that was the visible casualty of the pre-#44 bug (the orchestrator
    INSERT omits it expecting the DEFAULT to fire, but the
    ``CREATE OR REPLACE TABLE`` dedup path stripped both). Pre-#44
    finalize leaves the column nullable; post-#44 OR post-#60 finalize
    leaves it NOT NULL. Returning True here is what gates the one-shot
    ``_RESTORE_CONSTRAINTS_SQL`` execution: on a post-#60 DB the ALTERs
    would otherwise raise ``DependencyException`` because the indexes
    that ``CREATE OR REPLACE TABLE`` used to drop are now still in
    place (issue #60).
    """
    row = conn.execute(
        "SELECT \"notnull\" FROM pragma_table_info('imports') WHERE name = 'imported_at'"
    ).fetchone()
    if row is None:  # pragma: no cover - imports table missing implies a not-yet-seeded schema
        return False
    return int(row[0]) == 0


def repair_legacy_constraints_if_needed(conn: duckdb.DuckDBPyConnection) -> None:
    """Restore the NOT NULL / DEFAULT constraints stripped by pre-#44 dedup.

    Extracted from :func:`deduplicate_tables` (#62 follow-up) so the
    incremental re-import path (which auto-skips dedup) still repairs
    a pre-#44 on-disk DB. Without this gate firing, the first Tier 2
    re-import would skip ``deduplicate_tables`` -- the only historic
    caller of the repair -- and the orchestrator's
    ``INSERT INTO imports (...)`` would land ``imported_at = NULL``
    again, silently regressing the v0.1.4 user-visible bug fix.

    The ``_legacy_schema_needs_constraint_repair`` probe keeps this a
    one-shot migration: post-#44 DBs report the constraint as already
    NOT NULL and the ALTERs are skipped, so the warm path eats no work.
    """
    if _legacy_schema_needs_constraint_repair(conn):  # pragma: no branch
        _logger.info(  # pragma: no cover
            "Repairing pre-#44 dedup-stripped constraints (one-shot migration)"
        )
        conn.execute(_RESTORE_CONSTRAINTS_SQL)  # pragma: no cover


def deduplicate_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Collapse duplicate rows across every importable table.

    Each per-table ``DELETE WHERE rowid IN (... ROW_NUMBER OVER (PARTITION
    BY key ORDER BY <tie-breakers>) > 1 ...)`` keeps the same row a
    legacy ``DISTINCT ON (key) ... ORDER BY key, <tie-breakers>`` would
    have kept; the tie-break prefers the most recent ``import_id`` so
    re-importing the same export never silently flips
    ``source_version`` / ``device`` / etc. ``_CREATE_INDEXES_SQL`` is
    idempotent (``CREATE INDEX IF NOT EXISTS``) so it costs nothing on
    the warm path.

    The pre-#44 constraint repair pass is no longer chained from here;
    it lives in :func:`repair_legacy_constraints_if_needed` and the
    orchestrator runs it unconditionally so the Tier 2 incremental path
    still benefits.
    """
    _logger.info("Deduplicating tables...")
    conn.execute(_DEDUPLICATE_SQL)
    conn.execute(_CREATE_INDEXES_SQL)
    _logger.info("Deduplication complete")


def populate_workout_vestigial_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Backfill ``workouts.total_distance`` / ``total_energy_burned``.

    Apple Health stopped emitting these values as ``<Workout>`` attributes in
    iOS 11 and moved them into ``<WorkoutStatistics>`` children, leaving the
    legacy ``workouts`` columns as vestigial NULL on every modern row.
    Aggregating the statistics back into those columns lets existing tooling
    that queries ``workouts`` directly keep working without having to learn
    the statistics table.
    """
    _logger.info(
        "Populating workouts.total_distance / total_energy_burned from workout_statistics..."
    )

    # Active energy: sum across any rows for the same workout (rare but
    # allowed), take any one unit (they are consistent within a workout).
    conn.execute(
        """
        UPDATE workouts AS w
        SET total_energy_burned = agg.total_sum,
            total_energy_unit   = COALESCE(w.total_energy_unit, agg.unit)
        FROM (
            SELECT
                workout_hash,
                SUM(sum)  AS total_sum,
                MIN(unit) AS unit
            FROM workout_statistics
            WHERE stat_type = 'HKQuantityTypeIdentifierActiveEnergyBurned'
              AND sum IS NOT NULL
            GROUP BY workout_hash
        ) AS agg
        WHERE w.workout_hash = agg.workout_hash
          AND w.total_energy_burned IS NULL;
        """
    )

    # Distance across every distance-flavored quantity type. The statistics
    # table only carries one distance type per workout in practice. When that
    # invariant breaks (e.g. a triathlon workout records swimming metres plus
    # cycling kilometres on the same Workout element), the totals are
    # incommensurable — we refuse to backfill via ``HAVING COUNT(DISTINCT
    # unit) = 1`` so the column stays NULL rather than silently storing a
    # nonsense sum like 1550 km.
    conn.execute(
        """
        UPDATE workouts AS w
        SET total_distance      = agg.total_sum,
            total_distance_unit = COALESCE(w.total_distance_unit, agg.unit)
        FROM (
            SELECT
                workout_hash,
                SUM(sum)  AS total_sum,
                MIN(unit) AS unit
            FROM workout_statistics
            WHERE stat_type LIKE 'HKQuantityTypeIdentifierDistance%'
              AND sum IS NOT NULL
            GROUP BY workout_hash
            HAVING COUNT(DISTINCT unit) = 1
        ) AS agg
        WHERE w.workout_hash = agg.workout_hash
          AND w.total_distance IS NULL;
        """
    )

    _logger.info("Vestigial column population complete")


def rebuild_daily_stats(conn: duckdb.DuckDBPyConnection) -> None:
    """Materialise daily aggregates of the ``records`` table."""
    conn.execute(
        """
        CREATE OR REPLACE TABLE daily_record_stats AS
        SELECT
            record_type,
            CAST(start_date AS DATE) AS date,
            unit,
            COUNT(*) AS count,
            AVG(value) AS avg_value,
            MIN(value) AS min_value,
            MAX(value) AS max_value,
            SUM(value) AS sum_value
        FROM records
        WHERE value IS NOT NULL
        GROUP BY record_type, CAST(start_date AS DATE), unit;
        """
    )
