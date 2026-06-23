"""Streaming importer for Apple Health ``export.xml``.

Mirrors the Rust implementation in ``rust/src/import/xml.rs``: a single
lxml ``iterparse`` pass with ``elem.clear()`` on every ``end`` event so the
parser never accumulates the full document tree in memory. The Python
implementation additionally captures elements the Rust version dropped on
the floor (per ``project_data_audit_2026_06_21``):

* ``HealthData[@locale]`` -> ``export_metadata.locale``
* ``ExportDate[@value]`` -> ``export_metadata.export_date``
* ``Me`` (5 attributes) -> ``me_attributes`` (one row per import)
* ``WorkoutRoute[@device]`` -> ``workout_routes.device``

Per-row state is built up while ``start`` events fire and committed at the
matching ``end`` event (so a Workout that fails to close never leaks
orphaned children into the database). Hashes match the Rust version
byte-for-byte via :func:`apple_health_mcp.importers._hash.compute_hash`.

Bulk-load policy (issues #41 / #50): every multi-row buffer flushes
through :func:`apple_health_mcp.importers._bulk_arrow.bulk_load_via_arrow`
via the 12 ``_flush_*`` helpers below. v0.1.5 replaced the v0.1.3-era
``COPY FROM CSV`` tempfile path with a PyArrow ``Table`` registered
against DuckDB -- the columnar buffer hands DuckDB the same shape its
internal storage uses, so the per-batch CSV serialise + tempfile +
COPY round-trip the legacy helper paid is gone. The three
singleton-row inserts (HealthData, ExportDate, Me) deliberately bypass
the bulk helper -- each fires at most once per import, so even the
trimmed Arrow overhead would dwarf a single ``conn.execute("INSERT ...")``.
Future contributors adding a per-import singleton row should follow
the same direct-INSERT pattern.

Phase 1 progress (issue #51): the iterparse loop emits a single-line
``progress: xml NN% (X / Y MB, ~Z min remaining)`` log entry every
``APPLE_HEALTH_IMPORT_PROGRESS_SECS`` seconds (default 10, clamped to
1..600) so a streaming agent or human can confirm forward motion
during the multi-minute parse. The cadence is non-TTY-safe (no ``\\r``,
no ANSI cursor games) so the output survives ``tee``, CI capture, and
LLM-agent stdout buffers.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from apple_health_mcp.exceptions import HealthImportError
from apple_health_mcp.importers._bulk_arrow import bulk_load_via_arrow
from apple_health_mcp.importers._hash import compute_hash
from apple_health_mcp.importers._tz import (
    normalize_apple_offset,
    normalize_apple_offset_opt,
)

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


# Records flushed in chunks so an interrupted import never holds more than
# this many rows in memory. The Rust version used 100_000; we keep that
# as the default for the low-volume tables.
_BATCH_SIZE = 100_000

# High-volume tables (records, record_metadata, heart_rate_samples) account
# for ~99% of the flushed rows on a typical 1.2 GB export, so flushing them
# less often saves DuckDB INSERT round-trip overhead (issue #56). Peak
# Python RSS rises by ~150 MB during the records run, well under the
# importer's 1 GB budget on the 16 GB development target.
_BATCH_SIZE_HOT = 250_000

# Match the Rust safeguard: bail out if the parser hits this many
# consecutive errors so a corrupt-stream loop cannot spin forever.
_MAX_CONSECUTIVE_PARSE_ERRORS = 100

# Default cadence and bounds for the Phase-1 progress emitter (issue #51).
# Overridable per-run via ``APPLE_HEALTH_IMPORT_PROGRESS_SECS``; values
# outside the bounds are clamped (rather than rejected) so a typo at the
# command line never crashes the import.
_PROGRESS_INTERVAL_DEFAULT_SECS = 10
_PROGRESS_INTERVAL_MIN_SECS = 1
_PROGRESS_INTERVAL_MAX_SECS = 600
# Imports smaller than this skip the progress emitter entirely: the
# enclosing phase markers already announce start + completion, and the
# CI smoke fixtures are sub-second so an emitted line would be noise.
_PROGRESS_MIN_BYTES = 1_000_000


def _resolve_progress_interval() -> int:
    """Return the configured Phase-1 progress cadence in seconds.

    Reads ``APPLE_HEALTH_IMPORT_PROGRESS_SECS`` lazily so unit tests can
    monkeypatch the env var per call. A non-integer / out-of-bounds
    value falls back to the default with a single WARNING; the import
    must not crash because of a typo at the command line.
    """
    raw = os.environ.get("APPLE_HEALTH_IMPORT_PROGRESS_SECS")
    if raw is None or raw == "":
        return _PROGRESS_INTERVAL_DEFAULT_SECS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "APPLE_HEALTH_IMPORT_PROGRESS_SECS=%r is not an integer; falling back to default %d",
            raw,
            _PROGRESS_INTERVAL_DEFAULT_SECS,
        )
        return _PROGRESS_INTERVAL_DEFAULT_SECS
    if value < _PROGRESS_INTERVAL_MIN_SECS:
        return _PROGRESS_INTERVAL_MIN_SECS
    if value > _PROGRESS_INTERVAL_MAX_SECS:
        return _PROGRESS_INTERVAL_MAX_SECS
    return value


# iOS 17+ State of Mind records are emitted as Category records of this type.
# The XML importer breaks them out into the dedicated ``state_of_mind`` table
# so the ``list_state_of_mind`` MCP tool can return valence / kind / labels /
# associations as first-class fields instead of opaque metadata blobs.
_STATE_OF_MIND_RECORD_TYPE = "HKCategoryTypeIdentifierStateOfMind"


@dataclass
class ImportStats:
    """Counters and lookup map returned by :func:`import_xml`.

    ``workout_route_map`` keys the route file path emitted by Apple
    (verbatim, including the ``/workout-routes/`` prefix) to the owning
    workout's hash so the GPX importer can attach each route file's points
    to the correct workout.
    """

    records: int = 0
    workouts: int = 0
    activity_summaries: int = 0
    correlations: int = 0
    ecg_readings: int = 0
    route_points: int = 0
    metadata_entries: int = 0
    workout_events: int = 0
    workout_statistics: int = 0
    workout_metadata_entries: int = 0
    workout_routes: int = 0
    heart_rate_samples: int = 0
    correlation_members: int = 0
    me_rows: int = 0
    export_metadata_rows: int = 0
    state_of_mind_rows: int = 0
    workout_route_map: dict[str, str] = field(default_factory=dict)


# --- attribute helpers -------------------------------------------------------


def _parse_opt_float(raw: str | None) -> float | None:
    """Parse ``raw`` as a finite float, returning ``None`` on failure.

    Third-party HealthKit contributors have been observed to emit ``NaN`` /
    ``Infinity``; a single non-finite row poisons every downstream aggregate
    because DuckDB propagates NaN through SUM/AVG. Drop them at parse time.
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


# XML date attributes go through the shared ``importers/_tz.py`` helpers
# so the ECG importer applies the identical normalisation — a JST row from
# the XML feed and the same JST row from an ECG CSV must land as the same
# UTC instant in TIMESTAMPTZ columns.
_clean_date = normalize_apple_offset
_clean_date_opt = normalize_apple_offset_opt


# --- importer ----------------------------------------------------------------


class _XmlImporter:
    """Internal scanner that owns the iterparse loop and batch buffers.

    Factored into a class so the (relatively large) per-element handlers can
    share state without an ever-growing function-argument list. The public
    entry point :func:`import_xml` constructs one of these and drives it.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        import_id: str,
    ) -> None:
        self._conn = conn
        self._import_id = import_id
        self._stats = ImportStats()

        # Batch buffers; each entry is a tuple matching the appender column
        # order declared in `_flush_*` helpers below.
        self._records: list[tuple[object, ...]] = []
        self._record_metadata: list[tuple[object, ...]] = []
        self._workouts: list[tuple[object, ...]] = []
        self._workout_events: list[tuple[object, ...]] = []
        self._workout_stats: list[tuple[object, ...]] = []
        self._workout_metadata: list[tuple[object, ...]] = []
        self._workout_routes: list[tuple[object, ...]] = []
        self._activities: list[tuple[object, ...]] = []
        self._heart_rate_samples: list[tuple[object, ...]] = []
        self._correlations: list[tuple[object, ...]] = []
        self._correlation_members: list[tuple[object, ...]] = []
        self._state_of_mind: list[tuple[object, ...]] = []

        # Per-workout staging (only flushed when the Workout end-event fires
        # so a malformed mid-workout abort never leaks orphan children).
        self._current_workout: tuple[object, ...] | None = None
        self._current_workout_hash: str | None = None
        self._current_workout_events: list[tuple[object, ...]] = []
        self._current_workout_stats: list[tuple[object, ...]] = []
        self._current_workout_metadata: list[tuple[object, ...]] = []
        self._current_workout_route: dict[str, object] | None = None
        self._in_workout = False
        self._in_workout_route = False

        # Per-record state for nested MetadataEntry / InstantaneousBeatsPerMinute.
        self._in_record = False
        self._current_record_hash: str | None = None
        self._current_hr_sample_idx = 0

        # Per-StateOfMind-record staging. Populated only when the current
        # Record is a HKCategoryTypeIdentifierStateOfMind; flushed at the
        # Record end event.
        self._current_state_of_mind: dict[str, object] | None = None

        # Correlation children share the top-level Record structure but their
        # row is recorded by the top-level scanner; here we only capture the
        # linkage.
        self._in_correlation = False
        self._current_correlation_hash: str | None = None

    # -- public entry --------------------------------------------------------

    def run(self, xml_path: Path) -> ImportStats:
        # Open the file ourselves (rather than letting lxml open by path)
        # so the progress emitter can ask ``.tell()`` for the byte
        # position. ``rb`` matches the XML transport (lxml parses bytes,
        # not decoded text); ``huge_tree=True`` matches the original call.
        try:
            fp = xml_path.open("rb")
        except OSError as exc:
            raise HealthImportError(f"failed to open export.xml at {xml_path}: {exc}") from exc

        try:
            total_bytes = xml_path.stat().st_size
        except OSError as exc:  # pragma: no cover - already-open file rarely fails stat
            fp.close()
            raise HealthImportError(f"failed to open export.xml at {xml_path}: {exc}") from exc

        try:
            context = etree.iterparse(
                fp,
                events=("start", "end"),
                recover=True,
                huge_tree=True,
            )

            interval = _resolve_progress_interval()
            emit_progress = total_bytes >= _PROGRESS_MIN_BYTES
            start_ts = time.monotonic()
            last_log = start_ts

            consecutive_errors = 0
            try:
                for event, elem in context:
                    handler_failed = False
                    try:
                        if event == "start":
                            self._on_start(elem)
                        else:
                            self._on_end(elem)
                    except Exception as exc:
                        handler_failed = True
                        consecutive_errors += 1
                        _logger.warning(
                            "XML element handler error (%d/%d): %s",
                            consecutive_errors,
                            _MAX_CONSECUTIVE_PARSE_ERRORS,
                            exc,
                        )
                        if consecutive_errors > _MAX_CONSECUTIVE_PARSE_ERRORS:
                            raise HealthImportError(
                                f"aborting XML import after {consecutive_errors} consecutive errors"
                            ) from exc
                    # Reset the counter on any successful event (start OR
                    # end). The Rust reference resets after every
                    # successful event for the same reason: with iterparse
                    # firing roughly equal numbers of start and end events,
                    # gating the reset on `start` only would halve the
                    # effective budget and cause sparse-but-non-consecutive
                    # failures to trip the abort.
                    if not handler_failed:
                        consecutive_errors = 0
                    if emit_progress:
                        now = time.monotonic()
                        if now - last_log >= interval:
                            self._emit_progress(fp, total_bytes, start_ts, now)
                            last_log = now
            except etree.XMLSyntaxError as exc:
                raise HealthImportError(f"unrecoverable XML syntax error: {exc}") from exc
        finally:
            fp.close()

        self._flush_all()
        _logger.info(
            "XML import complete: %d records, %d workouts (%d metadata entries, %d routes),"
            " %d activity summaries, %d correlations (%d members), %d heart-rate samples",
            self._stats.records,
            self._stats.workouts,
            self._stats.workout_metadata_entries,
            self._stats.workout_routes,
            self._stats.activity_summaries,
            self._stats.correlations,
            self._stats.correlation_members,
            self._stats.heart_rate_samples,
        )
        return self._stats

    # -- progress emitter ---------------------------------------------------

    def _emit_progress(
        self,
        fp: object,
        total_bytes: int,
        start_ts: float,
        now: float,
    ) -> None:
        """Emit one Phase-1 progress line (issue #51).

        Single newline-terminated INFO record on stderr (the only stream
        the importer logger writes to); no ``\\r`` and no ANSI cursor
        escapes so the output stays sane through ``tee``, CI capture, and
        LLM-agent stdout buffers. ``fp`` is annotated as ``object`` only
        so the call site can pass the binary file handle without
        triggering a circular import on lxml's IO protocols; the real
        contract is that ``.tell()`` returns the byte position.
        """
        # ``tell`` shadows ``getattr`` here so mypy stops worrying about
        # the file-handle protocol; production callers pass a binary
        # file opened by :meth:`run`, but unit tests can pass a stub.
        tell = getattr(fp, "tell", None)
        if tell is None:  # pragma: no cover - defensive
            return
        consumed = int(tell())
        # ``total_bytes`` is guaranteed > 0 by the caller (we only enter
        # this branch when ``total_bytes >= _PROGRESS_MIN_BYTES``).
        pct_float = 100 * consumed / total_bytes
        pct = round(pct_float)
        elapsed = now - start_ts
        if pct_float > 0:
            eta_secs = elapsed * (100 - pct_float) / pct_float
            eta_min = eta_secs / 60
            eta_text = f"~{eta_min:.1f} min remaining"
        else:  # pragma: no cover - the first emission lands well after 0%
            eta_text = "ETA unknown"
        consumed_mb = consumed / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)
        _logger.info(
            "progress: xml %d%% (%.0f / %.0f MB, %s)",
            pct,
            consumed_mb,
            total_mb,
            eta_text,
        )

    # -- event dispatch ------------------------------------------------------

    def _on_start(self, elem: etree._Element) -> None:
        tag = elem.tag
        if tag == "HealthData":
            self._handle_health_data(elem)
        elif tag == "ExportDate":
            self._handle_export_date(elem)
        elif tag == "Me":
            self._handle_me(elem)
        elif tag == "Record":
            if self._in_correlation:
                self._handle_correlation_record(elem)
            else:
                self._handle_record(elem)
        elif tag == "MetadataEntry":
            self._handle_metadata_entry(elem)
        elif tag == "Workout":
            self._handle_workout_start(elem)
        elif tag == "WorkoutEvent" and self._in_workout:
            self._handle_workout_event(elem)
        elif tag == "WorkoutStatistics" and self._in_workout:
            self._handle_workout_stat(elem)
        elif tag == "WorkoutRoute" and self._in_workout:
            self._handle_workout_route_start(elem)
        elif tag == "FileReference" and self._in_workout_route:
            self._handle_file_reference(elem)
        elif tag == "InstantaneousBeatsPerMinute":
            self._handle_instantaneous_bpm(elem)
        elif tag == "ActivitySummary":
            self._handle_activity_summary(elem)
        elif tag == "Correlation":
            self._handle_correlation_start(elem)

    def _on_end(self, elem: etree._Element) -> None:
        tag = elem.tag
        if tag == "Record":
            self._finalize_state_of_mind()
            self._in_record = False
            self._current_record_hash = None
        elif tag == "WorkoutRoute":
            self._finalize_workout_route()
        elif tag == "Workout":
            self._finalize_workout()
        elif tag == "Correlation":
            self._in_correlation = False
            self._current_correlation_hash = None
        # Free memory: clear the element after every end event, then drop
        # any preceding siblings still attached to the parent. Without the
        # sibling drop the root element accumulates one (empty) child per
        # processed top-level node and the document-end memory cost is
        # O(number-of-records) instead of O(1). HealthData is the only
        # context where this matters in practice (millions of <Record>
        # children); inside small subtrees the prev-sibling loop is a no-op.
        elem.clear()
        prev = elem.getprevious()
        while prev is not None:
            parent = prev.getparent()
            if parent is None:  # pragma: no cover - prev was returned from a sibling lookup
                break
            parent.remove(prev)
            prev = elem.getprevious()

    # -- handlers ------------------------------------------------------------

    def _handle_health_data(self, elem: etree._Element) -> None:
        # HealthData fires exactly once at the document root; insert
        # whatever locale is present (the column is nullable so a missing
        # attribute still records the row).
        locale = elem.get("locale")
        self._conn.execute(
            "INSERT INTO export_metadata (import_id, export_date, locale) VALUES (?, NULL, ?)",
            [self._import_id, locale],
        )
        self._stats.export_metadata_rows += 1

    def _handle_export_date(self, elem: etree._Element) -> None:
        value = _clean_date_opt(elem.get("value"))
        # ExportDate normally appears AFTER HealthData (which inserted the
        # row), so a plain UPDATE works. But if HealthData failed (caught by
        # the per-event consecutive-error budget) or is missing in a
        # malformed export, the UPDATE matches zero rows and the export_date
        # is silently lost. Detect that case and INSERT a standalone row so
        # the data survives, logging a warning so the malformed root is
        # surfaced to the user.
        row = self._conn.execute(
            "SELECT 1 FROM export_metadata WHERE import_id = ? LIMIT 1",
            [self._import_id],
        ).fetchone()
        if row is None:
            _logger.warning(
                "ExportDate seen without a preceding HealthData row; "
                "inserting standalone export_metadata (locale will be NULL)"
            )
            self._conn.execute(
                "INSERT INTO export_metadata (import_id, export_date, locale) VALUES (?, ?, NULL)",
                [self._import_id, value],
            )
            self._stats.export_metadata_rows += 1
        else:
            self._conn.execute(
                "UPDATE export_metadata SET export_date = ? WHERE import_id = ?",
                [value, self._import_id],
            )

    def _handle_me(self, elem: etree._Element) -> None:
        self._conn.execute(
            """
            INSERT INTO me_attributes (
                import_id, date_of_birth, biological_sex, blood_type,
                fitzpatrick_skin_type, cardio_fitness_medications_use
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                self._import_id,
                elem.get("HKCharacteristicTypeIdentifierDateOfBirth"),
                elem.get("HKCharacteristicTypeIdentifierBiologicalSex"),
                elem.get("HKCharacteristicTypeIdentifierBloodType"),
                elem.get("HKCharacteristicTypeIdentifierFitzpatrickSkinType"),
                elem.get("HKCharacteristicTypeIdentifierCardioFitnessMedicationsUse"),
            ],
        )
        self._stats.me_rows += 1

    def _handle_record(self, elem: etree._Element) -> None:
        # Snapshot the attribute mapping once per element so the per-key
        # lookups below stay on the Python dict side instead of crossing
        # back into the lxml C boundary 8-15 times per record (issue #56).
        attr = elem.attrib
        record_type = attr.get("type", "")
        source_name = attr.get("sourceName", "")
        start_date = _clean_date(attr.get("startDate", ""))
        end_date = _clean_date(attr.get("endDate", ""))
        value_str = attr.get("value")
        unit = attr.get("unit")
        value = _parse_opt_float(value_str)
        # Preserve categorical / non-numeric values (e.g. sleep stages) that
        # do not parse as float, so downstream consumers can still read them.
        text_value = (
            value_str if value is None and value_str is not None and value_str != "" else None
        )

        record_hash = compute_hash(
            [
                record_type,
                source_name,
                start_date,
                end_date,
                value_str or "",
                unit or "",
            ]
        )

        self._records.append(
            (
                record_hash,
                record_type,
                value,
                text_value,
                unit,
                source_name,
                attr.get("sourceVersion"),
                attr.get("device"),
                _clean_date_opt(attr.get("creationDate")),
                start_date,
                end_date,
                self._import_id,
            )
        )
        self._stats.records += 1
        self._in_record = True
        self._current_record_hash = record_hash
        self._current_hr_sample_idx = 0
        if record_type == _STATE_OF_MIND_RECORD_TYPE:
            # Seed the StateOfMind buffer with the record's numeric value as
            # a starting valence; metadata-supplied valence (if any) wins.
            self._current_state_of_mind = {
                "record_hash": record_hash,
                "valence": value,
                "kind": None,
                "labels": None,
                "associations": None,
            }
        else:
            self._current_state_of_mind = None
        if len(self._records) >= _BATCH_SIZE_HOT:
            self._flush_records()

    def _handle_correlation_record(self, elem: etree._Element) -> None:
        # The child's own row is taken care of by the top-level pass (Apple
        # Health duplicates correlation members at the top level by spec);
        # we only record the linkage here. The hash must mirror the
        # top-level Record handler exactly so the join key matches (same
        # attribute read order, same string trimming) — keep this in lockstep
        # with :meth:`_handle_record`.
        if self._current_correlation_hash is None:  # pragma: no cover - defensive
            return
        attr = elem.attrib
        record_type = attr.get("type", "")
        source_name = attr.get("sourceName", "")
        start_date = _clean_date(attr.get("startDate", ""))
        end_date = _clean_date(attr.get("endDate", ""))
        value_str = attr.get("value")
        unit = attr.get("unit")
        child_hash = compute_hash(
            [record_type, source_name, start_date, end_date, value_str or "", unit or ""]
        )
        self._correlation_members.append(
            (self._current_correlation_hash, child_hash, self._import_id)
        )
        self._stats.correlation_members += 1
        if len(self._correlation_members) >= _BATCH_SIZE:
            self._flush_correlation_members()

    def _handle_metadata_entry(self, elem: etree._Element) -> None:
        attr = elem.attrib
        key = attr.get("key", "")
        value = attr.get("value", "")
        # Inner-most context wins: a <Record> nested inside a <Workout>
        # (e.g. InstantaneousBeatsPerMinute samples or HK plain Records under
        # a Workout block) must route its MetadataEntry to record_metadata,
        # not workout_metadata. Checking _in_record first ensures the inner
        # context takes priority over the enclosing Workout.
        if self._in_record and self._current_record_hash is not None:
            self._record_metadata.append((self._current_record_hash, key, value))
            self._stats.metadata_entries += 1
            self._capture_state_of_mind_metadata(key, value)
            if len(self._record_metadata) >= _BATCH_SIZE_HOT:
                self._flush_record_metadata()
        elif self._in_workout:
            # _in_workout is only set alongside _current_workout_hash, so the
            # None branch is unreachable in practice; keep the guard for
            # type-narrowing and tolerance to future refactors.
            if self._current_workout_hash is not None:  # pragma: no branch
                self._current_workout_metadata.append(
                    (self._current_workout_hash, key, value, self._import_id)
                )

    def _handle_workout_start(self, elem: etree._Element) -> None:
        self._in_workout = True
        attr = elem.attrib
        activity_type = attr.get("workoutActivityType", "")
        source_name = attr.get("sourceName", "")
        start_date = _clean_date(attr.get("startDate", ""))
        end_date = _clean_date(attr.get("endDate", ""))
        duration_str = attr.get("duration")
        duration = _parse_opt_float(duration_str)

        workout_hash = compute_hash(
            [activity_type, source_name, start_date, end_date, duration_str or ""]
        )
        self._current_workout_hash = workout_hash
        self._current_workout = (
            workout_hash,
            activity_type,
            duration,
            attr.get("durationUnit"),
            _parse_opt_float(attr.get("totalDistance")),
            attr.get("totalDistanceUnit"),
            _parse_opt_float(attr.get("totalEnergyBurned")),
            attr.get("totalEnergyBurnedUnit"),
            source_name,
            attr.get("sourceVersion"),
            attr.get("device"),
            _clean_date_opt(attr.get("creationDate")),
            start_date,
            end_date,
            self._import_id,
        )
        self._current_workout_events.clear()
        self._current_workout_stats.clear()
        self._current_workout_metadata.clear()
        self._current_workout_route = None
        self._in_workout_route = False

    def _handle_workout_event(self, elem: etree._Element) -> None:
        if self._current_workout_hash is None:  # pragma: no cover - defensive
            return
        attr = elem.attrib
        self._current_workout_events.append(
            (
                self._current_workout_hash,
                attr.get("type", ""),
                _clean_date_opt(attr.get("date")),
                _parse_opt_float(attr.get("duration")),
                attr.get("durationUnit"),
            )
        )

    def _handle_workout_stat(self, elem: etree._Element) -> None:
        if self._current_workout_hash is None:  # pragma: no cover - defensive
            return
        attr = elem.attrib
        self._current_workout_stats.append(
            (
                self._current_workout_hash,
                attr.get("type", ""),
                _clean_date_opt(attr.get("startDate")),
                _clean_date_opt(attr.get("endDate")),
                _parse_opt_float(attr.get("average")),
                _parse_opt_float(attr.get("minimum")),
                _parse_opt_float(attr.get("maximum")),
                _parse_opt_float(attr.get("sum")),
                attr.get("unit"),
            )
        )

    def _handle_workout_route_start(self, elem: etree._Element) -> None:
        if self._current_workout_hash is None:  # pragma: no cover - defensive
            return
        self._in_workout_route = True
        attr = elem.attrib
        self._current_workout_route = {
            "workout_hash": self._current_workout_hash,
            "file_path": "",
            "source_name": attr.get("sourceName"),
            "source_version": attr.get("sourceVersion"),
            # Captured even though the Rust version dropped it -- per
            # project_data_audit_2026_06_21 the device attribute on
            # WorkoutRoute carries useful provenance.
            "device": attr.get("device"),
            "creation_date": _clean_date_opt(attr.get("creationDate")),
            "start_date": _clean_date_opt(attr.get("startDate")),
            "end_date": _clean_date_opt(attr.get("endDate")),
            "import_id": self._import_id,
        }

    def _handle_file_reference(self, elem: etree._Element) -> None:
        if self._current_workout_route is None:  # pragma: no cover - defensive
            return
        path = elem.get("path")
        if path is not None:
            self._current_workout_route["file_path"] = path

    def _handle_instantaneous_bpm(self, elem: etree._Element) -> None:
        # Emitted as a child of either an HR record or an HRV record wrapped
        # in HeartRateVariabilityMetadataList. Both flatten into
        # heart_rate_samples keyed by the parent record's hash.
        if self._current_record_hash is None:
            return
        attr = elem.attrib
        bpm = _parse_opt_float(attr.get("bpm"))
        sample_time = attr.get("time")
        self._heart_rate_samples.append(
            (
                self._current_record_hash,
                self._current_hr_sample_idx,
                bpm,
                sample_time,
                self._import_id,
            )
        )
        self._current_hr_sample_idx += 1
        self._stats.heart_rate_samples += 1
        if len(self._heart_rate_samples) >= _BATCH_SIZE_HOT:
            self._flush_heart_rate_samples()

    def _handle_activity_summary(self, elem: etree._Element) -> None:
        attr = elem.attrib
        self._activities.append(
            (
                attr.get("dateComponents", ""),
                _parse_opt_float(attr.get("activeEnergyBurned")),
                _parse_opt_float(attr.get("activeEnergyBurnedGoal")),
                attr.get("activeEnergyBurnedUnit"),
                _parse_opt_float(attr.get("appleMoveTime")),
                _parse_opt_float(attr.get("appleMoveTimeGoal")),
                _parse_opt_float(attr.get("appleExerciseTime")),
                _parse_opt_float(attr.get("appleExerciseTimeGoal")),
                _parse_opt_float(attr.get("appleStandHours")),
                _parse_opt_float(attr.get("appleStandHoursGoal")),
                self._import_id,
            )
        )
        self._stats.activity_summaries += 1
        if len(self._activities) >= _BATCH_SIZE:
            self._flush_activities()

    def _handle_correlation_start(self, elem: etree._Element) -> None:
        self._in_correlation = True
        self._stats.correlations += 1
        attr = elem.attrib
        correlation_type = attr.get("type", "")
        source_name = attr.get("sourceName", "")
        start_date = _clean_date(attr.get("startDate", ""))
        end_date = _clean_date(attr.get("endDate", ""))
        correlation_hash = compute_hash([correlation_type, source_name, start_date, end_date])
        self._correlations.append(
            (
                correlation_hash,
                correlation_type,
                source_name,
                attr.get("sourceVersion"),
                attr.get("device"),
                _clean_date_opt(attr.get("creationDate")),
                start_date,
                end_date,
                self._import_id,
            )
        )
        self._current_correlation_hash = correlation_hash
        if len(self._correlations) >= _BATCH_SIZE:
            self._flush_correlations()

    # -- StateOfMind helpers ------------------------------------------------

    def _capture_state_of_mind_metadata(self, key: str, value: str) -> None:
        """Pull StateOfMind fields out of a generic ``MetadataEntry``.

        Apple shipped multiple key spellings between iOS 17 betas and the
        GM release (``HKMetadataKeyMoodValenceClassification`` vs
        ``HKMetadataKeyStateOfMindValence``), so we don't pin a hard-coded
        list. Instead the key must start with ``HKMetadataKey`` *and* end
        with one of the well-defined tokens, so an unrelated key that
        merely contains "association" / "kind" / "label" / "valence" as a
        substring (e.g. a hypothetical ``...StateOfMindAssociatedFood``)
        cannot silently overwrite the structured field.

        ``valence`` is coerced to ``float`` and silently dropped on parse
        failure (so a future Apple change from numeric to enum-string does
        not poison the row -- it just falls back to the record's seeded
        value).
        """
        if self._current_state_of_mind is None:
            return
        if not key.startswith("HKMetadataKey"):
            return
        key_lower = key.lower()
        if key_lower.endswith("valence") or key_lower.endswith("valenceclassification"):
            try:
                parsed = float(value)
            except ValueError:
                return
            if not math.isfinite(parsed):
                return
            self._current_state_of_mind["valence"] = parsed
        elif key_lower.endswith("labels"):
            self._current_state_of_mind["labels"] = value
        elif key_lower.endswith("associations"):
            self._current_state_of_mind["associations"] = value
        elif key_lower.endswith("kind"):
            self._current_state_of_mind["kind"] = value

    def _finalize_state_of_mind(self) -> None:
        som = self._current_state_of_mind
        self._current_state_of_mind = None
        if som is None:
            return
        # Skip records that yielded no structured StateOfMind information at
        # all. A category Record that happens to carry the StateOfMind type
        # identifier (or a stripped export with metadata removed) would
        # otherwise produce an all-NULL row that ``list_state_of_mind``
        # surfaces as a real mood entry.
        if (
            som["valence"] is None
            and som["kind"] is None
            and som["labels"] is None
            and som["associations"] is None
        ):
            return
        self._state_of_mind.append(
            (
                som["record_hash"],
                som["valence"],
                som["kind"],
                som["labels"],
                som["associations"],
                self._import_id,
            )
        )
        self._stats.state_of_mind_rows += 1
        if len(self._state_of_mind) >= _BATCH_SIZE:
            self._flush_state_of_mind()

    # -- finalizers for nested blocks ---------------------------------------

    def _finalize_workout_route(self) -> None:
        route = self._current_workout_route
        self._current_workout_route = None
        self._in_workout_route = False
        if route is None:  # pragma: no cover - defensive
            return
        file_path = route["file_path"]
        # A WorkoutRoute without a FileReference cannot be joined to a GPX
        # payload; dropping it (rather than inserting with an empty path) is
        # the Rust behavior and keeps later joins clean.
        if not isinstance(file_path, str) or file_path == "":
            return
        workout_hash = route["workout_hash"]
        assert isinstance(workout_hash, str)
        self._stats.workout_route_map[file_path] = workout_hash
        self._workout_routes.append(
            (
                workout_hash,
                file_path,
                route["source_name"],
                route["source_version"],
                route["device"],
                route["creation_date"],
                route["start_date"],
                route["end_date"],
                route["import_id"],
            )
        )
        self._stats.workout_routes += 1
        if len(self._workout_routes) >= _BATCH_SIZE:
            self._flush_workout_routes()

    def _finalize_workout(self) -> None:
        workout = self._current_workout
        self._current_workout = None
        workout_hash = self._current_workout_hash
        self._current_workout_hash = None
        self._in_workout = False
        if workout is None or workout_hash is None:  # pragma: no cover - defensive
            return
        self._workouts.append(workout)
        self._stats.workouts += 1
        for ev in self._current_workout_events:
            self._workout_events.append(ev)
            self._stats.workout_events += 1
        for st in self._current_workout_stats:
            self._workout_stats.append(st)
            self._stats.workout_statistics += 1
        for md in self._current_workout_metadata:
            self._workout_metadata.append(md)
            self._stats.workout_metadata_entries += 1
        self._current_workout_events.clear()
        self._current_workout_stats.clear()
        self._current_workout_metadata.clear()
        if len(self._workouts) >= _BATCH_SIZE:
            self._flush_workouts()
        if len(self._workout_events) >= _BATCH_SIZE:
            self._flush_workout_events()
        if len(self._workout_stats) >= _BATCH_SIZE:
            self._flush_workout_stats()
        if len(self._workout_metadata) >= _BATCH_SIZE:
            self._flush_workout_metadata()

    # -- flush helpers ------------------------------------------------------
    #
    # Every flush routes the buffered batch through
    # :func:`bulk_load_via_arrow` (issue #50). The v0.1.3-era CSV path
    # (issue #41) flushed at ~100k rows/s, bottlenecked by the per-row
    # csv.writer.writerow call + tempfile write + COPY auto-detect. The
    # Arrow path builds a columnar buffer once per batch and hands
    # DuckDB the same shape its internal storage uses, lifting the
    # measured throughput on the maintainer's real 1.2 GB export above
    # the ≥ 250 k rows/s target.

    def _flush_records(self) -> None:
        bulk_load_via_arrow(self._conn, "records", self._records)
        self._records.clear()

    def _flush_record_metadata(self) -> None:
        bulk_load_via_arrow(self._conn, "record_metadata", self._record_metadata)
        self._record_metadata.clear()

    def _flush_workouts(self) -> None:
        bulk_load_via_arrow(self._conn, "workouts", self._workouts)
        self._workouts.clear()

    def _flush_workout_events(self) -> None:
        bulk_load_via_arrow(self._conn, "workout_events", self._workout_events)
        self._workout_events.clear()

    def _flush_workout_stats(self) -> None:
        bulk_load_via_arrow(self._conn, "workout_statistics", self._workout_stats)
        self._workout_stats.clear()

    def _flush_workout_metadata(self) -> None:
        bulk_load_via_arrow(self._conn, "workout_metadata", self._workout_metadata)
        self._workout_metadata.clear()

    def _flush_workout_routes(self) -> None:
        bulk_load_via_arrow(self._conn, "workout_routes", self._workout_routes)
        self._workout_routes.clear()

    def _flush_activities(self) -> None:
        bulk_load_via_arrow(self._conn, "activity_summaries", self._activities)
        self._activities.clear()

    def _flush_heart_rate_samples(self) -> None:
        bulk_load_via_arrow(self._conn, "heart_rate_samples", self._heart_rate_samples)
        self._heart_rate_samples.clear()

    def _flush_correlations(self) -> None:
        bulk_load_via_arrow(self._conn, "correlations", self._correlations)
        self._correlations.clear()

    def _flush_correlation_members(self) -> None:
        bulk_load_via_arrow(self._conn, "correlation_members", self._correlation_members)
        self._correlation_members.clear()

    def _flush_state_of_mind(self) -> None:
        bulk_load_via_arrow(self._conn, "state_of_mind", self._state_of_mind)
        self._state_of_mind.clear()

    def _flush_all(self) -> None:
        self._flush_records()
        self._flush_record_metadata()
        self._flush_workouts()
        self._flush_workout_events()
        self._flush_workout_stats()
        self._flush_workout_metadata()
        self._flush_workout_routes()
        self._flush_activities()
        self._flush_heart_rate_samples()
        self._flush_correlations()
        self._flush_correlation_members()
        self._flush_state_of_mind()


def import_xml(conn: duckdb.DuckDBPyConnection, xml_path: Path, import_id: str) -> ImportStats:
    """Parse Apple Health ``export.xml`` and bulk-load it into ``conn``.

    Streams through the file with ``lxml.iterparse`` so memory stays bounded
    even on multi-gigabyte exports. Returns an :class:`ImportStats` with row
    counts plus the route / offset lookup maps the GPX importer needs.
    """
    importer = _XmlImporter(conn, import_id)
    return importer.run(xml_path)
