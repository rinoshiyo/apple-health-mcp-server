"""DataState helper for the v0.4 ZIP-flow read-tool gate (issue #148).

Every read-oriented MCP tool short-circuits before its own SQL runs to
report one of three states:

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


EXPORT_ZIPS_DIR_ENV_VAR = "APPLE_HEALTH_EXPORT_ZIPS_DIR"


class DataState(StrEnum):
    """Three-state machine for whether a read tool can proceed."""

    READY = "READY"
    NEEDS_CONFIG = "NEEDS_CONFIG"
    NEEDS_IMPORT = "NEEDS_IMPORT"


def check_data_state(
    conn: duckdb.DuckDBPyConnection,
    *,
    lock: Lock | None = None,
) -> DataState:
    """Determine the data-readiness state of the database the server has open.

    Order of evaluation (most → least specific):

    1. The ``imports`` table has at least one row → ``READY``. By
       construction the orchestrator only INSERTs after the pipeline
       succeeds, so a present row is treated as a successful import
       without an explicit ``status='success'`` column. Tier-2
       incremental re-imports also write a row, so a serve process
       opened against a partially-replicated DB still reports READY
       once any historical import landed.
    2. ``APPLE_HEALTH_EXPORT_ZIPS_DIR`` env unset / blank-after-strip →
       ``NEEDS_CONFIG``. The MCPB user_config injects this env at server
       launch; absence means the operator never picked a drop-zone
       directory.
    3. Env set but no successful import yet → ``NEEDS_IMPORT``. The
       ``list_zips`` tool can now discover ZIPs in that directory and
       walk the user through ``import_zip``.

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
        row = _probe_imports_row(conn)
    else:
        with lock:
            row = _probe_imports_row(conn)
    if row is not None:
        return DataState.READY
    if (os.environ.get(EXPORT_ZIPS_DIR_ENV_VAR) or "").strip():
        return DataState.NEEDS_IMPORT
    return DataState.NEEDS_CONFIG


def _probe_imports_row(conn: duckdb.DuckDBPyConnection) -> tuple[object, ...] | None:
    """Return the first ``imports`` row or ``None`` on a missing table.

    Catches broadly so any DuckDB error (catalog miss on an alien DB,
    file-locked write contention, etc.) reads as "no imports yet" and
    surfaces the friendly state-machine guidance instead of a raw SQL
    error. The original exception is logged at debug so the operator
    can diagnose if needed.
    """
    try:
        return conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone()
    except Exception as exc:
        _logger.debug("imports probe failed (%s); treating as empty DB", exc)
        return None


def build_state_error_payload(state: DataState) -> str:
    """Render the structured error JSON for ``NEEDS_CONFIG`` / ``NEEDS_IMPORT``.

    Returns a pretty-printed JSON object containing ``state``,
    ``reason``, ``suggested_action`` (an enum the agent branches on),
    and ``human_message`` (the prose the agent relays to the user). The
    schema mirrors the grill decision recorded in
    ``tmp/grill-sessions/v0-4-zip-import-tool-decisions-2026-06-26.md``.

    Calling this with ``READY`` is a programming error (the READY path
    is for the tool's normal SQL output, not for an error envelope);
    raises ``ValueError`` so a regression surfaces at the call site
    rather than silently shipping an empty-shaped error to the agent.
    """
    if state == DataState.NEEDS_CONFIG:
        payload = {
            "state": DataState.NEEDS_CONFIG.value,
            "reason": f"{EXPORT_ZIPS_DIR_ENV_VAR} is not set",
            "suggested_action": "ask_user_to_open_settings",
            "human_message": (
                "Please open Claude Desktop → Settings → MCP → "
                "apple-health-mcp-server → set the Export ZIPs directory, "
                f"or set the {EXPORT_ZIPS_DIR_ENV_VAR} environment variable."
            ),
        }
    elif state == DataState.NEEDS_IMPORT:
        payload = {
            "state": DataState.NEEDS_IMPORT.value,
            "reason": "no successful Apple Health import found in this database",
            "suggested_action": "call_list_zips",
            "human_message": (
                "No Apple Health export has been imported yet. Call "
                "list_zips to discover ZIPs in your configured directory, "
                "then import_zip(id) to import one."
            ),
        }
    else:
        raise ValueError(f"build_state_error_payload called with non-error state {state!r}")
    return json.dumps(payload, indent=2, ensure_ascii=False)


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
