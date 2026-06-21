"""DuckDB schema definitions, deduplication, and derived-column population.

Ported verbatim from the Rust reference implementation (``src/db.rs``) plus
the additional storage required by the Python port:

* ``export_metadata`` — root-level ``<HealthData locale="...">`` attribute and
  ``<ExportDate value="...">`` value, keyed by ``import_id``.
* ``me_attributes`` — the five ``<Me ...>`` element fields (date of birth,
  biological sex, blood type, Fitzpatrick skin type, cardio-fitness medications
  use), keyed by ``import_id``.
* ``workout_routes.device`` — ``<WorkoutRoute device="...">`` attribute that
  the Rust version dropped on the floor.

The audit memory ``project_data_audit_2026_06_21`` documents these gaps and
their justification.
"""

from __future__ import annotations

import logging
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
    creation_date   TIMESTAMP,
    start_date      TIMESTAMP NOT NULL,
    end_date        TIMESTAMP NOT NULL,
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
    creation_date        TIMESTAMP,
    start_date           TIMESTAMP NOT NULL,
    end_date             TIMESTAMP NOT NULL,
    -- Minutes east of UTC parsed from the workout's `startDate` attribute
    -- (e.g. 540 for `+0900`, -420 for `-0700`). The XML importer strips the
    -- offset before storing `start_date`, leaving a naive TIMESTAMP that
    -- holds local wall-clock time. This column preserves the original offset
    -- so the GPX importer can shift true-UTC route timestamps onto the same
    -- local-time basis as the rest of the data.
    start_offset_minutes INTEGER,
    import_id            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_events (
    workout_hash    VARCHAR NOT NULL,
    event_type      VARCHAR NOT NULL,
    date            TIMESTAMP,
    duration        DOUBLE,
    duration_unit   VARCHAR
);

CREATE TABLE IF NOT EXISTS workout_statistics (
    workout_hash    VARCHAR NOT NULL,
    stat_type       VARCHAR NOT NULL,
    start_date      TIMESTAMP,
    end_date        TIMESTAMP,
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
    recorded_date    TIMESTAMP NOT NULL,
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
    timestamp     TIMESTAMP NOT NULL,
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
    creation_date   TIMESTAMP,
    start_date      TIMESTAMP,
    end_date        TIMESTAMP,
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
    creation_date       TIMESTAMP,
    start_date          TIMESTAMP NOT NULL,
    end_date            TIMESTAMP NOT NULL,
    import_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS correlation_members (
    correlation_hash    VARCHAR NOT NULL,
    record_hash         VARCHAR NOT NULL,
    import_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    import_id     VARCHAR,
    export_dir    VARCHAR NOT NULL,
    imported_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    record_count  BIGINT,
    workout_count BIGINT,
    duration_secs DOUBLE
);

-- Captures the root <HealthData locale="..."> attribute and the
-- <ExportDate value="..."> element value. Keyed by import_id so multiple
-- imports stay distinguishable.
CREATE TABLE IF NOT EXISTS export_metadata (
    import_id    VARCHAR NOT NULL,
    export_date  TIMESTAMP,
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
CREATE OR REPLACE TABLE records AS
SELECT * FROM (
    SELECT DISTINCT ON (record_hash) *
    FROM records
    ORDER BY record_hash, import_id DESC, creation_date DESC
);

CREATE OR REPLACE TABLE record_metadata AS
SELECT * FROM (
    SELECT DISTINCT ON (record_hash, key) *
    FROM record_metadata
    ORDER BY record_hash, key, value
);

CREATE OR REPLACE TABLE workouts AS
SELECT * FROM (
    SELECT DISTINCT ON (workout_hash) *
    FROM workouts
    ORDER BY workout_hash, import_id DESC, creation_date DESC
);

CREATE OR REPLACE TABLE activity_summaries AS
SELECT * FROM (
    SELECT DISTINCT ON (date_components) *
    FROM activity_summaries
    ORDER BY date_components, import_id DESC
);

CREATE OR REPLACE TABLE ecg_readings AS
SELECT * FROM (
    SELECT DISTINCT ON (ecg_hash) *
    FROM ecg_readings
    ORDER BY ecg_hash, import_id DESC
);

CREATE OR REPLACE TABLE ecg_samples AS
SELECT * FROM (
    SELECT DISTINCT ON (ecg_hash, sample_idx) *
    FROM ecg_samples
    ORDER BY ecg_hash, sample_idx, voltage_uv
);

CREATE OR REPLACE TABLE route_points AS
SELECT * FROM (
    SELECT DISTINCT ON (point_hash) *
    FROM route_points
    ORDER BY point_hash, import_id DESC
);

CREATE OR REPLACE TABLE workout_metadata AS
SELECT * FROM (
    SELECT DISTINCT ON (workout_hash, key) *
    FROM workout_metadata
    ORDER BY workout_hash, key, import_id DESC
);

CREATE OR REPLACE TABLE workout_routes AS
SELECT * FROM (
    SELECT DISTINCT ON (workout_hash, file_path) *
    FROM workout_routes
    ORDER BY workout_hash, file_path, import_id DESC
);

-- workout_events and workout_statistics carry no import_id column, so the
-- dedupe key has to come from the row's own structure. Apple Health spec
-- emits at most one event per (workout, type, date) and one statistic per
-- (workout, stat_type) — re-importing the same export collapses cleanly
-- under those keys.
CREATE OR REPLACE TABLE workout_events AS
SELECT * FROM (
    SELECT DISTINCT ON (workout_hash, event_type, date) *
    FROM workout_events
    ORDER BY workout_hash, event_type, date
);

CREATE OR REPLACE TABLE workout_statistics AS
SELECT * FROM (
    SELECT DISTINCT ON (workout_hash, stat_type) *
    FROM workout_statistics
    ORDER BY workout_hash, stat_type, start_date
);

CREATE OR REPLACE TABLE heart_rate_samples AS
SELECT * FROM (
    SELECT DISTINCT ON (parent_record_hash, sample_idx) *
    FROM heart_rate_samples
    ORDER BY parent_record_hash, sample_idx, import_id DESC
);

CREATE OR REPLACE TABLE correlations AS
SELECT * FROM (
    SELECT DISTINCT ON (correlation_hash) *
    FROM correlations
    ORDER BY correlation_hash, import_id DESC
);

CREATE OR REPLACE TABLE correlation_members AS
SELECT * FROM (
    SELECT DISTINCT ON (correlation_hash, record_hash) *
    FROM correlation_members
    ORDER BY correlation_hash, record_hash, import_id DESC
);

CREATE OR REPLACE TABLE imports AS
SELECT * FROM (
    SELECT DISTINCT ON (import_id) *
    FROM imports
    ORDER BY import_id, imported_at DESC
);

CREATE OR REPLACE TABLE export_metadata AS
SELECT * FROM (
    SELECT DISTINCT ON (import_id) *
    FROM export_metadata
    ORDER BY import_id
);

CREATE OR REPLACE TABLE me_attributes AS
SELECT * FROM (
    SELECT DISTINCT ON (import_id) *
    FROM me_attributes
    ORDER BY import_id
);

CREATE OR REPLACE TABLE state_of_mind AS
SELECT * FROM (
    SELECT DISTINCT ON (record_hash) *
    FROM state_of_mind
    ORDER BY record_hash, import_id DESC
);

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


# Number of tables ensure_schema() creates. Used by the test suite as a smoke
# check that no table is silently dropped during refactors.
TABLE_COUNT = 18


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all tables if they do not already exist.

    Tables are created without ``PRIMARY KEY`` constraints so the bulk loader
    can append duplicates; deduplication is a separate phase
    (:func:`deduplicate_tables`) that runs after the import.
    """
    conn.execute(_CREATE_TABLES_SQL)


def deduplicate_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Collapse duplicate rows across every importable table.

    Each ``DISTINCT ON`` carries an explicit ``ORDER BY`` so the surviving
    row is deterministic across re-imports. The tie-break prefers the most
    recent ``import_id`` so re-importing the same export never silently flips
    ``source_version`` / ``device`` / etc.
    """
    _logger.info("Deduplicating tables...")
    conn.execute(_DEDUPLICATE_SQL)
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
    # table only carries one distance type per workout in practice, so SUM is
    # effectively a passthrough while still tolerating multi-row stats.
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
