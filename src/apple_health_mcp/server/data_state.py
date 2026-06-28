"""DataState helper for the v0.4 ZIP-flow read-tool gate (issue #148).

Every read-oriented MCP tool short-circuits before its own SQL runs to
report one of four states:

* ``READY`` — the ``imports`` table holds at least one row, so a successful
  Apple Health import has happened and the tool's query will return real
  data. The caller proceeds.
* ``NEEDS_CONFIG`` — no successful import yet AND the operator has not
  configured the ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` directory the
  ``list_zips`` / ``import_zip`` flow reads from. The agent is asked to
  prompt the user to set the env var (or the MCPB user_config field that
  ultimately injects it).
* ``NEEDS_IMPORT`` — no successful import yet BUT the directory IS
  configured. The agent is asked to call ``list_zips`` to discover the
  ZIPs already in the drop-zone and trigger ``import_zip`` on the chosen
  one.
* ``NEEDS_REIMPORT`` — the persisted ``schema_version`` trails the
  package's ``CURRENT_SCHEMA_VERSION`` (v0.4.1 / issue #156). The DB was
  imported under an older package release; the agent is asked to call
  ``list_zips`` and re-trigger ``import_zip`` on the chosen ZIP. The
  importer's fresh-reset path then drops every package-owned table and
  rebuilds the canonical schema before re-ingesting -- the user never
  has to touch a terminal or hunt down the MSIX sandbox path.

The structured error payload (``{state, reason, suggested_action,
human_message}``) gives the agent enough information to branch on
``suggested_action`` and a localisation-ready ``human_message`` to relay
to the user. The first reader is always the agent (Claude / Codex / ...);
the user reads the ``human_message`` after the agent renders it.

Replaces the v0.3.x ``IMPORT_REQUIRED_MESSAGE`` plain-string sentinel
that ``server.query`` returned on the empty-DB path. Tools that opt out
of the gate (``get_import_history`` is the canonical example) bypass
this module entirely.
"""

from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


EXPORT_ZIPS_DIR_ENV_VAR = "APPLE_HEALTH_EXPORT_ZIPS_DIR"


class DataState(StrEnum):
    """Four-state machine for whether a read tool can proceed."""

    READY = "READY"
    NEEDS_CONFIG = "NEEDS_CONFIG"
    NEEDS_IMPORT = "NEEDS_IMPORT"
    NEEDS_REIMPORT = "NEEDS_REIMPORT"


# Static envelope payloads. The two error states carry only constant
# strings (``EXPORT_ZIPS_DIR_ENV_VAR`` is module-level and interpolated
# at import time), so build the JSON once at module load instead of
# allocating a dict + running ``json.dumps`` per tool call. Tests that
# parse the JSON for content keep working unchanged.
#
# The NEEDS_CONFIG human_message fronts the env-var instruction so
# Claude Code / Codex / non-Desktop MCP clients (which have no
# user_config UI) get an actionable first line; Claude Desktop users
# still see the Settings → MCP path they would expect, just framed as
# the second route to the same setting.
_STATE_ERROR_PAYLOADS: Final[dict[DataState, str]] = {
    DataState.NEEDS_CONFIG: json.dumps(
        {
            "state": DataState.NEEDS_CONFIG.value,
            "reason": f"{EXPORT_ZIPS_DIR_ENV_VAR} is not set",
            "suggested_action": "ask_user_to_open_settings",
            "human_message": (
                f"Set the {EXPORT_ZIPS_DIR_ENV_VAR} environment variable "
                "to the directory that holds your Apple Health export ZIPs. "
                "Claude Desktop users can also configure this via "
                "Settings → MCP → apple-health-mcp-server → "
                "Export ZIPs directory; other MCP clients (Claude Code, "
                "Codex, etc.) set the env var directly in the server "
                "configuration."
            ),
        },
        indent=2,
        ensure_ascii=False,
    ),
    DataState.NEEDS_IMPORT: json.dumps(
        {
            "state": DataState.NEEDS_IMPORT.value,
            "reason": "no successful Apple Health import found in this database",
            "suggested_action": "call_list_zips",
            "human_message": (
                "No Apple Health export has been imported yet. Call "
                "list_zips to discover ZIPs in your configured directory, "
                "then import_zip(id) to import one."
            ),
        },
        indent=2,
        ensure_ascii=False,
    ),
    DataState.NEEDS_REIMPORT: json.dumps(
        {
            "state": DataState.NEEDS_REIMPORT.value,
            "reason": (
                "database was imported under an older package release; "
                "schema_version trails the current package."
            ),
            "suggested_action": "call_list_zips",
            "human_message": (
                "The database was imported under an older version of "
                "apple-health-mcp-server and is no longer compatible. "
                "Call list_zips to find your Apple Health ZIP, then "
                "import_zip(id) to re-import -- the old data will be "
                "replaced automatically (no terminal commands needed)."
            ),
        },
        indent=2,
        ensure_ascii=False,
    ),
}


def check_data_state(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock | None = None,
) -> DataState:
    """Determine the data-readiness state of the database the server has open.

    Order of evaluation (most → least specific):

    1. ``schema_version`` is set but trails ``CURRENT_SCHEMA_VERSION`` →
       ``NEEDS_REIMPORT`` (v0.4.1 / issue #156). The DB carries usable
       rows under an older package release; the agent triggers
       ``list_zips`` + ``import_zip`` and the importer's fresh-reset
       path replaces the schema before re-ingesting.
    2. The ``imports`` table has at least one row → ``READY``. By
       construction the orchestrator only INSERTs after the pipeline
       succeeds, so a present row is treated as a successful import
       without an explicit ``status='success'`` column. Tier-2
       incremental re-imports also write a row, so a serve process
       opened against a partially-replicated DB still reports READY
       once any historical import landed.
    3. ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` env unset / blank-after-strip →
       ``NEEDS_CONFIG``. The MCPB user_config injects this env at server
       launch; absence means the operator never picked a drop-zone
       directory.
    4. Env set but no successful import yet → ``NEEDS_IMPORT``. The
       ``list_zips`` tool can now discover ZIPs in that directory and
       walk the user through ``import_zip``.

    The schema-staleness probe is intentionally ordered above the
    READY check: an old-shape DB may still have ``imports`` rows that
    a downstream read tool would happily try to query, but the row
    bodies were stamped under columns the package no longer
    recognises. Surfacing ``NEEDS_REIMPORT`` first keeps the user out
    of a maze of partial-shape errors and lands them on the recovery
    path immediately.

    The probe is intentionally defensive: a missing ``imports`` table
    (alien DB, cold install, the bootstrap-empty path
    ``_materialise_empty_db`` wrote, etc.) is treated as "no imports
    yet" and falls through to the env-check tier. Returning ``READY``
    on a DB without an ``imports`` table would surface a confusing
    ``Error: Table imports does not exist`` from whichever read tool
    happened to be called first.

    ``lock`` is the server's shared cursor lock; pass it through when
    the connection is being multiplexed across coroutines so the probe
    SELECT does not race the tool's own query. ``None`` is fine for
    single-thread test callers.
    """
    if lock is None:
        stale = _safe_schema_stale_probe(conn)
        has_rows = _imports_table_has_rows(conn)
    else:
        with lock:
            stale = _safe_schema_stale_probe(conn)
            has_rows = _imports_table_has_rows(conn)
    if stale:
        return DataState.NEEDS_REIMPORT
    if has_rows:
        return DataState.READY
    if (os.environ.get(EXPORT_ZIPS_DIR_ENV_VAR) or "").strip():
        return DataState.NEEDS_IMPORT
    return DataState.NEEDS_CONFIG


def _safe_schema_stale_probe(conn: duckdb.DuckDBPyConnection) -> bool:
    """Run :func:`schema_version_is_stale` defensively, returning False on error.

    Mirrors :func:`_imports_table_has_rows`'s catch-all: any DuckDB
    surprise (catalog miss on an alien DB, transient file-lock error,
    etc.) reads as "fresh" so the function falls through to the
    friendlier NEEDS_CONFIG / NEEDS_IMPORT tiers instead of surfacing
    a raw SQL exception to whichever tool happened to be called first.
    """
    from apple_health_mcp.db.migrations import schema_version_is_stale

    try:
        return schema_version_is_stale(conn)
    except Exception as exc:
        _logger.debug("schema_version_is_stale probe failed (%s); treating as fresh", exc)
        return False


def _imports_table_has_rows(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True when the ``imports`` table holds at least one row.

    Catches broadly so any DuckDB error (catalog miss on an alien DB,
    file-locked write contention, etc.) reads as "no imports yet" and
    surfaces the friendly state-machine guidance instead of a raw SQL
    error. The original exception is logged at debug so the operator
    can diagnose if needed.
    """
    try:
        return conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone() is not None
    except Exception as exc:
        _logger.debug("imports probe failed (%s); treating as empty DB", exc)
        return False


def build_state_error_payload(state: DataState) -> str:
    """Render the structured error JSON for ``NEEDS_CONFIG`` / ``NEEDS_IMPORT``.

    Returns a pretty-printed JSON object containing ``state``,
    ``reason``, ``suggested_action`` (an enum the agent branches on),
    and ``human_message`` (the prose the agent relays to the user). The
    schema mirrors the grill decision recorded in
    ``tmp/grill-sessions/v0-4-zip-import-tool-decisions-2026-06-26.md``.

    Looks up a precomputed payload (see :data:`_STATE_ERROR_PAYLOADS`)
    so the dict + json.dumps cost is paid once at import. Calling this
    with ``READY`` is a programming error (the READY path is for the
    tool's normal SQL output, not for an error envelope); raises
    ``ValueError`` so a regression surfaces at the call site rather
    than silently shipping an empty-shaped error to the agent.
    """
    try:
        return _STATE_ERROR_PAYLOADS[state]
    except KeyError:
        raise ValueError(
            f"build_state_error_payload called with non-error state {state!r}"
        ) from None


def require_ready_or_state_error(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock | None = None,
) -> str | None:
    """Return the structured error JSON when not READY, else ``None``.

    Convenience wrapper that matches the pre-v0.4
    ``require_imports_or_message`` shape so caller modules can drop
    in the new helper with a one-line change. The structured payload
    replaces the single ``IMPORT_REQUIRED_MESSAGE`` plain-string
    sentinel; agents that previously matched on the sentinel must now
    parse the JSON envelope for ``state`` / ``suggested_action``.
    """
    state = check_data_state(conn, lock=lock)
    if state == DataState.READY:
        return None
    return build_state_error_payload(state)
