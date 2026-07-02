"""Shared prose fragments for the async import polling contract.

The polling cadence (``10-30 seconds``) and the hardware baseline
(``~45s on fast NVMe`` / stall threshold at ``~10 minutes``) show
up in every tool that touches the async importer: ``list_zips`` /
``import_zip`` DESCRIPTION and queued envelopes, plus
``get_import_status`` DESCRIPTION. Before issue #194 those two
paragraphs were copy-pasted across five sites; issue #187
happened when only one site was updated and ``list_zips`` drifted
to the previous ``60s`` value.

Two module-level constants concentrate the language so each tool
composes them into its DESCRIPTION / envelope via an f-string.
Changing the cadence or the runtime baseline is now a one-file
edit that reaches every tool automatically.
"""

from __future__ import annotations

# Cadence + polling cue. Full sentence, no trailing period, so the
# caller adds the terminator or continues the sentence with a
# semicolon / comma.
IMPORT_POLL_BLURB = (
    "Poll ``get_import_status(job_id=...)`` every 10-30 seconds "
    "to track progress and retrieve the final result"
)

# Hardware baseline + stall threshold. Full sentence, no trailing
# period. Include after IMPORT_POLL_BLURB whenever a tool exposes
# expected runtime to the caller so agents can decide how long to
# wait before flagging the worker as stalled.
IMPORT_RUNTIME_BLURB = (
    "Typical fresh-import wall-clock is ~45s on a fast NVMe + "
    "recent CPU and several minutes on slower hardware; if "
    "``elapsed_secs`` grows past ~10 minutes without the "
    "``phase`` field advancing, treat the worker as stalled and "
    "surface that to the user instead of polling forever"
)
