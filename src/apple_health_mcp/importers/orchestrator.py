"""Top-level pipeline that runs XML, ECG, GPX importers in order.

Mirrors the Rust ``import::run_import``: parse ``export.xml`` first
because its scan produces the route map the GPX phase needs, then ECG
(independent), then GPX (depends on the XML output), then the dedupe /
backfill / daily-stats finalize phase. Each phase logs at INFO level for
human-readable progress.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from apple_health_mcp.db.connection import get_connection
from apple_health_mcp.db.migrations import schema_version_is_stale, stamp_current_version
from apple_health_mcp.db.schema import ensure_schema, reset_db_for_fresh_import
from apple_health_mcp.importers._existing_hashes import (
    ExistingHashes,
    load_existing_hashes,
)
from apple_health_mcp.importers.dedup import finalize_import
from apple_health_mcp.importers.ecg import import_ecg_files
from apple_health_mcp.importers.gpx import import_gpx_files
from apple_health_mcp.importers.xml import _READ_CHUNK_BYTES, ImportStats, import_xml

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)

# Re-use the XML SAX target's 1 MiB (1,048,576-byte — binary, per
# the xml.py §"Units convention" docstring) read chunk size for the
# sha256 streaming hash (issue #62 Tier 1). Sharing the constant
# keeps the 'sha256 read keeps the OS page cache warm for the
# immediately-following XML parse' invariant alive across future
# tuning changes.
_SHA256_READ_CHUNK_BYTES = _READ_CHUNK_BYTES


def make_import_id(now: datetime | None = None) -> str:
    """Return a unique import identifier.

    Includes microseconds so two imports launched in the same wall-clock
    second (CI, scripted batches) cannot collide and dedupe into one
    ``imports`` row.
    """
    moment = now or datetime.now(UTC)
    return f"import_{moment.strftime('%Y%m%d_%H%M%S')}_{moment.microsecond:06d}"


def run_import(
    export_dir: Path,
    db_path: Path | None = None,
    *,
    conn: duckdb.DuckDBPyConnection | None = None,
    import_id: str | None = None,
    force: bool = False,
    source_zip: tuple[str, datetime, int] | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> ImportStats:
    """Run the full XML -> ECG -> GPX -> finalize pipeline on ``export_dir``.

    Returns the :class:`ImportStats` from the XML phase, augmented with the
    ECG / GPX counts in fields the dataclass already exposes
    (``ecg_readings``, ``route_points``).

    ``db_path`` defaults to the XDG-resolved location; pass ``Path(":memory:")``
    -- or an explicit file path under ``tmp_path`` in tests -- to override.

    Two re-import optimisations from issue #62 fire here:

    * **Tier 1 sha256 fast path.** The orchestrator streams sha256 over
      ``export.xml`` once and compares it against the most recent
      ``imports.export_xml_sha256`` row. A byte-identical export exits
      in roughly one disk-read of wall-clock without parsing the file.
      ``force=True`` bypasses ONLY this check -- the Tier 2 snapshot
      still loads and the handlers still skip on-disk hashes.
    * **Tier 2 incremental hash sets.** When the destination DB already
      holds prior import data, every dedup hash currently on disk is
      snapshotted into Python sets and threaded into the XML / GPX /
      ECG handlers. Each handler checks the freshly-computed hash
      before staging the row, so a re-import contributes only
      genuinely-new rows. Phase 4 dedup auto-skips because the bulk
      staging buffers carry no duplicates -- this avoids the DuckDB
      MVCC tombstones that would otherwise balloon the on-disk file
      on every re-import.

    ``force=True`` is the right call when the on-disk export.xml is
    byte-identical to a prior import BUT the user wants to re-run the
    import (e.g. after deleting some rows from the DB by hand, or to
    verify the importer still produces the same row counts). The hash
    skip in Tier 2 still keeps the re-import cheap; only the sha256
    bail-out is bypassed. On a modified ``export.xml`` (sha256 misses
    the prior row anyway), ``force=True`` is functionally equivalent
    to no flag at all -- adding it to every command "just to be safe"
    has no effect on a changed file.

    ``source_zip`` is the v0.4 (issue #148) hook for the upcoming
    ``import_zip`` MCP tool: a ``(sha256_hex, mtime, size_bytes)``
    triple captured from the source ZIP file the tool extracted into a
    temp directory. The orchestrator stamps the triple into the matching
    ``imports`` row so ``list_zips`` / ``import_zip`` can later skip a
    byte-identical re-import without rehashing the ZIP. CLI callers
    leave it ``None`` (the source artefact is a directory and the triple
    has no meaningful value there); ``imports.source_zip_*`` land NULL.

    ``conn`` is the v0.4 (issue #148) seam that lets the upcoming
    ``import_zip`` MCP tool reuse the server's already-open writable
    connection instead of opening a second handle (DuckDB rejects
    concurrent same-process opens of one on-disk file when either side
    is writable). When ``conn`` is provided ``db_path`` is ignored and
    the caller retains ownership of the connection -- the orchestrator
    does NOT close it in the ``finally`` block. CLI callers pass
    ``conn=None``; ``_open_db`` then resolves ``db_path`` and the
    orchestrator owns + closes that handle as before.
    """
    # v0.4 (issue #148): callers must pick exactly one of ``conn`` / ``db_path``.
    # Silently ignoring ``db_path`` when both are passed was the prior
    # contract, but that lets a misuse like ``import_zip`` defensively
    # threading both arguments corrupt the user's data targeting: the
    # import would land in ``conn``'s file while ConfigError messages
    # interpolate the unrelated ``db_path``. Fail fast at the entrypoint.
    if conn is not None and db_path is not None:
        raise ValueError(
            "run_import: pass either ``conn`` or ``db_path``, not both -- "
            "they would otherwise point at different files and the import "
            "would land in ``conn``'s database while diagnostics quoted "
            "``db_path``."
        )

    start = time.monotonic()
    # Issue #130: take a single wall-clock UTC snapshot at run start so
    # ``import_id`` (which formats it) and ``imports.imported_at``
    # (which stores it) record the SAME instant. Before this change
    # the schema's ``DEFAULT CURRENT_TIMESTAMP`` fired at INSERT time,
    # i.e. after the whole pipeline finished, so the two timestamps
    # diverged by the full import duration and looked like unrelated
    # events when a user grepped through the imports table.
    start_moment = datetime.now(UTC)
    actual_import_id = import_id or make_import_id(now=start_moment)
    _logger.info("Starting import %s from %s", actual_import_id, export_dir)

    xml_path = export_dir / "export.xml"
    export_sha = _compute_file_sha256(xml_path)

    # v0.4 (issue #148): the caller may pass a pre-opened writable
    # handle (typically the live serve connection the ``import_zip``
    # MCP tool reuses). In that case we MUST NOT close it in the
    # finally block below -- the server keeps using it. Track
    # ownership so the legacy CLI path still gets its connection
    # cleaned up.
    externally_owned = conn is not None
    if conn is None:
        conn = _open_db(db_path)
    try:
        # v0.4.1 (issue #156): when the DB carries a stale
        # ``schema_version`` (imported under an older package release),
        # drop every package-owned table so ``ensure_schema`` below
        # rebuilds the canonical shape and ``stamp_current_version``
        # (called further down) records the current sentinel. The
        # legacy contract refused to open such DBs and asked the user
        # to ``rm`` the file + re-run the CLI; that broke the v0.4
        # terminal-zero install pitch because the default DB path on
        # Windows lives behind the MSIX AppContainer sandbox redirect
        # and is invisible to Explorer / PowerShell.
        #
        # Atomicity caveat (v0.4.1 code-review #5): reset_db_for_fresh_import
        # opens its own ``BEGIN TRANSACTION ... COMMIT`` and closes it
        # before ``ensure_schema`` and the importer writes begin --
        # those run under separate autocommit statements. If the host
        # process is killed between the reset COMMIT and the end of
        # the import pipeline, the DB is left with the new (empty)
        # canonical schema and no rows; the previously-stale data is
        # NOT preserved. This is intentional: stale-shape rows are
        # not safe to read against the current package, and the next
        # ``import_zip`` call walks the same path and re-ingests from
        # the source ZIP. Do not promise "previously-stale DB intact"
        # in user-facing copy.
        if schema_version_is_stale(conn):
            _logger.warning(
                "Detected stale schema in %s; performing fresh-reset before re-import.",
                db_path if db_path is not None else "<externally-owned connection>",
            )
            reset_db_for_fresh_import(conn)
        ensure_schema(conn)
        # v0.5 (issue #178): retired ``apply_pending_migrations``. The
        # migration registry went empty once v0.3.0 made fresh-import
        # the upgrade contract, and v0.4.1 (#156)
        # ``schema_version_is_stale`` + ``reset_db_for_fresh_import``
        # (called above) made the ConfigError rejection path
        # unreachable too — by the time the importer writes the
        # sentinel, the DB is guaranteed to either be empty or have
        # just been fresh-reset. ``stamp_current_version`` is the thin
        # wrapper that records :data:`CURRENT_SCHEMA_VERSION` on the
        # schema_version sentinel.
        stamp_current_version(conn)

        # Tier 1: sha256 fast path. Skip the whole import when the
        # incoming export.xml is byte-identical to the last successful
        # one. ``--force`` bypasses this check only -- the Tier 2
        # block below stays active regardless (see the comment there
        # for the full ``--force`` semantic). A missing ``export.xml``
        # falls through so import_xml below can raise the proper error.
        if not force and export_sha is not None and _sha256_matches_prior(conn, export_sha):
            _logger.info(
                "Skipping import: export.xml is byte-identical to the most recent "
                "successful import (sha256=%s...). Pass --force to re-import.",
                export_sha[:12],
            )
            return ImportStats()

        # Tier 2: load every dedup-keyed hash currently on disk into Python
        # sets if the DB already holds prior import data. ``--force`` only
        # bypasses the Tier 1 sha256 fast path -- it does NOT disable the
        # incremental hash sets, because there is no useful interpretation
        # of "re-import this data, but pay the on-disk tombstone cost of
        # full Phase 4 dedup". The fresh-install / empty DB case still
        # falls through to the legacy full-insert + Phase 4 dedup path
        # (existing stays ``None`` because the imports table is empty).
        existing: ExistingHashes | None = None
        if _has_prior_imports(conn):
            existing = load_existing_hashes(conn)

        # ``preserve_insertion_order = false`` used to be set here for
        # the duration of this CLI-owned connection (issue #41 perf).
        # v0.4 (issue #148) moved it into ``db.connection.get_connection``'s
        # writable branch so that ``run_import(conn=server_handle)`` -- the
        # upcoming ``import_zip`` reuse path -- does not leak the override
        # onto the server's live connection for the rest of its lifetime.
        # Externally-owned conns (writable serve) already carry the PRAGMA
        # from open; CLI-owned conns get it via the same ``_open_db`` /
        # ``get_connection`` chain.

        # v0.5 (issue #157): emit phase markers ahead of each Phase log
        # so the new ``import_zip`` async worker can update its
        # ``import_jobs`` row for ``get_import_status`` polling. The
        # callback runs INSIDE the writer-lock context the MCP caller
        # already holds around ``run_import`` (see
        # ``importers.zip_extract``); the worker's implementation issues
        # the UPDATE directly without re-acquiring the lock to avoid
        # deadlocking on a ``threading.Lock``.
        if phase_callback is not None:
            phase_callback("xml_parsing")
        _logger.info("Phase 1: Parsing export.xml")
        stats = import_xml(conn, xml_path, actual_import_id, existing=existing)

        if phase_callback is not None:
            phase_callback("ecg")
        _logger.info("Phase 2: Parsing ECG files")
        stats.ecg_readings = import_ecg_files(
            conn, export_dir / "electrocardiograms", actual_import_id, existing=existing
        )

        if phase_callback is not None:
            phase_callback("gpx")
        _logger.info("Phase 3: Parsing GPX route files")
        stats.route_points = import_gpx_files(
            conn,
            export_dir / "workout-routes",
            actual_import_id,
            stats.workout_route_map,
            existing=existing,
        )

        if phase_callback is not None:
            phase_callback("finalize")
        _logger.info("Phase 4: Finalize (dedupe, backfill, daily stats)")
        finalize_import(conn, skip_dedup=existing is not None)

        duration_secs = time.monotonic() - start
        # Issue #129: ``record_count`` is the Phase-1 parse count
        # (BEFORE Phase 4's Correlation-child dedup). The companion
        # ``records_after_dedup`` is meaningful ONLY on Tier-1 fresh
        # imports where ``finalize_import`` ran the dedup pass: in
        # that case the surviving rows in ``records`` for this
        # ``actual_import_id`` reflect the dedup outcome, so
        # ``record_count - records_after_dedup`` is the Correlation
        # collapse count the docstring promises.
        #
        # On a Tier-2 incremental re-import (``existing is not None``)
        # the dedup pass is skipped and the importer drops every
        # previously-seen row before INSERT, so a ``COUNT WHERE
        # import_id = ?`` here would return "rows newly inserted in
        # this run" -- a number whose subtraction from
        # ``record_count`` does NOT mean "Correlation duplicates
        # collapsed". Storing NULL instead is the honest signal that
        # this import row carries no dedup measurement; the wire
        # description already documents NULL as "no Phase-4 dedup
        # ran for this import row" so downstream LLMs can skip the
        # subtraction rather than computing a misleading delta.
        if existing is None:
            # ``COUNT(*)`` always returns a single-row result so
            # ``fetchone()`` is never None at runtime; the assert is
            # purely for the type checker (``fetchone() -> tuple | None``).
            post_dedup_row = conn.execute(
                "SELECT COUNT(*) FROM records WHERE import_id = ?",
                [actual_import_id],
            ).fetchone()
            assert post_dedup_row is not None
            records_after_dedup: int | None = int(post_dedup_row[0])
            dedup_skipped = False
        else:
            records_after_dedup = None
            # Issue #163: explicit "Phase 4 dedup was skipped on
            # purpose" signal. Distinguishes a clean Tier-1 import that
            # found zero duplicates (records_after_dedup == record_count,
            # dedup_skipped=False) from a Tier-2 incremental that never
            # measured (records_after_dedup IS NULL, dedup_skipped=True).
            dedup_skipped = True
        conn.execute(
            """
            INSERT INTO imports (
                import_id, export_dir, imported_at, record_count, workout_count,
                duration_secs, export_xml_sha256, records_after_dedup, dedup_skipped,
                source_zip_sha256, source_zip_mtime, source_zip_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                actual_import_id,
                str(export_dir),
                # Pass the same UTC moment ``import_id`` was formatted
                # from so the two stamps point at the same wall-clock
                # event (issue #130). The schema's ``DEFAULT
                # CURRENT_TIMESTAMP`` remains as a safety net for any
                # future caller that bypasses ``run_import``.
                start_moment,
                stats.records,
                stats.workouts,
                duration_secs,
                export_sha,
                records_after_dedup,
                dedup_skipped,
                # v0.4 (issue #148): the source ZIP triple, stamped only
                # when the upcoming ``import_zip`` MCP tool drives this
                # call. CLI ``import <dir>`` callers pass ``None`` and
                # all three land NULL.
                source_zip[0] if source_zip is not None else None,
                source_zip[1] if source_zip is not None else None,
                source_zip[2] if source_zip is not None else None,
            ],
        )

        _logger.info("Import complete in %.1fs", duration_secs)
        # On a Tier 2 incremental re-import these stats report rows
        # NEWLY INSERTED in this run, not total rows present on disk -- a
        # no-change re-import legitimately reads "0 records, 0 workouts,
        # ..." even though the database still holds the full history.
        # The "Newly inserted" label keeps that distinction visible so a
        # user does not mistake the summary for missing data.
        label = "Newly inserted" if existing is not None else "Imported"
        _logger.info(
            "  %s: %d records, %d workouts, %d activity summaries",
            label,
            stats.records,
            stats.workouts,
            stats.activity_summaries,
        )
        _logger.info(
            "  %s: %d ECG readings, %d route points, %d metadata entries",
            label,
            stats.ecg_readings,
            stats.route_points,
            stats.metadata_entries,
        )
        return stats
    finally:
        if not externally_owned:
            conn.close()


def _compute_file_sha256(path: Path) -> str | None:
    """Stream sha256 over ``path``; return the hex digest or ``None`` if absent.

    Returns ``None`` and lets the downstream ``import_xml`` call surface
    the missing-file error in its normal context. Other OSError flavors
    (permission denied, mid-read EIO from a flaky disk) also return
    ``None`` so the orchestrator does not crash here, but the importer
    logs a WARNING in those cases -- a silent fall-through would stamp
    NULL into ``imports.export_xml_sha256`` and the next byte-identical
    re-import could no longer fast-path-skip, masquerading as a
    perf regression.
    """
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(_SHA256_READ_CHUNK_BYTES), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except FileNotFoundError:
        # Expected and handled by ``import_xml`` below; no log needed.
        return None
    except OSError as exc:
        # Anything other than "file absent" is a surprise; log so the
        # next maintainer can see why ``imports.export_xml_sha256``
        # landed NULL on an import that otherwise succeeded.
        _logger.warning(
            "failed to compute sha256 of %s for the Tier 1 fast path "
            "(import will proceed but the fast path is bypassed): %s",
            path,
            exc,
        )
        return None


def _sha256_matches_prior(conn: duckdb.DuckDBPyConnection, export_sha: str) -> bool:
    """Return True when the most recent recorded sha256 matches ``export_sha``.

    ``ORDER BY imported_at DESC, import_id DESC`` makes the ordering
    total even when two imports stamp the same wall-clock second (and
    therefore the same ``CURRENT_TIMESTAMP`` default). ``make_import_id``
    includes microseconds, so the secondary sort breaks every realistic
    tie. The ``imported_at`` field can still be NULL on a pre-#44 DB
    before :func:`repair_legacy_constraints_if_needed` runs; ``DESC``
    sorts NULLs last in DuckDB so the most recent populated row wins
    until the repair fires on the next finalize pass.
    """
    row = conn.execute(
        "SELECT export_xml_sha256 FROM imports "
        "WHERE export_xml_sha256 IS NOT NULL "
        "ORDER BY imported_at DESC, import_id DESC LIMIT 1"
    ).fetchone()
    return row is not None and row[0] == export_sha


def _has_prior_imports(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True when the ``imports`` table already holds at least one row.

    Used to gate the Tier 2 existing-hash snapshot load: on a fresh DB
    the sets would be empty anyway, so we skip the cost of issuing six
    ``SELECT DISTINCT`` round-trips and the orchestrator's later
    ``skip_dedup=False`` keeps the legacy Phase 4 path lit.
    """
    row = conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone()
    return row is not None


def _open_db(db_path: Path | None) -> duckdb.DuckDBPyConnection:
    """Open the destination DuckDB connection.

    Wraps :func:`get_connection` so tests can substitute via monkeypatch
    without touching the orchestrator's main path.
    """
    return get_connection(db_path)
