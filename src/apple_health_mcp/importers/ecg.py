"""Importer for Apple Health ECG CSV files.

One CSV per recording lives under ``<export>/electrocardiograms/``. The file
opens with localized ``Key,Value`` header lines (English on a US watch,
Japanese on a Japanese-language watch, etc.), a blank separator line, and
then one ASCII voltage sample per line. We use ``chardet`` to sniff the
encoding (Apple writes UTF-8 with an optional BOM, but a hand-edited copy
or a non-standard locale could be different) and fall back to UTF-8 when
detection is inconclusive.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

import chardet

from apple_health_mcp import ISSUES_URL
from apple_health_mcp.exceptions import HealthImportError, LocaleUnrecognisedError
from apple_health_mcp.importers._bulk_arrow import bulk_load_via_arrow
from apple_health_mcp.importers._existing_hashes import ExistingHashes
from apple_health_mcp.importers._hash import compute_hash
from apple_health_mcp.importers._tz import normalize_apple_offset

if TYPE_CHECKING:
    import duckdb

_logger = logging.getLogger(__name__)


# Localized header labels emitted by Apple Health ECG CSV export. The English
# labels are authoritative (taken from a US English watch); the Japanese
# labels are verified against a real export. Labels for other locales are
# best-effort and may need correction from contributors with watches set to
# those locales -- file an issue if a label is wrong.
_RECORDED_DATE_LABELS = (
    "Recorded Date",
    # Apple emits one of two Japanese spellings depending on the watchOS
    # version. Both are verified against real exports.
    "記録日",  # ja, current iOS in JP
    "記録日時",  # ja, older iOS in JP
    # unverified below
    "记录日期",  # zh-Hans
    "記錄日期",  # zh-Hant
    "기록 일시",  # ko
)
_CLASSIFICATION_LABELS = (
    "Classification",
    "分類",  # ja verified / zh-Hant unverified (same glyphs)
    "分类",  # zh-Hans unverified
    "분류",  # ko unverified
)
_SYMPTOMS_LABELS = (
    "Symptoms",
    "症状",  # ja verified / zh-Hans unverified (same glyphs)
    "症狀",  # zh-Hant unverified
    "증상",  # ko unverified
)
_SOFTWARE_VERSION_LABELS = (
    "Software Version",
    "ソフトウェアバージョン",  # ja
    "软件版本",  # zh-Hans
    "軟體版本",  # zh-Hant
    "소프트웨어 버전",  # ko
)
_DEVICE_LABELS = (
    "Device",
    "デバイス",  # ja
    "设备",  # zh-Hans
    "裝置",  # zh-Hant
    "기기",  # ko
)
_SAMPLE_RATE_LABELS = (
    "Sample Rate",
    "サンプルレート",  # ja
    "采样率",  # zh-Hans
    "取樣率",  # zh-Hant
    "샘플 레이트",  # ko
)

# Labels for fields that should be skipped (privacy or irrelevant).
_NAME_LABELS = ("Name", "名前", "姓名", "이름")
_DOB_LABELS = (
    "Date of Birth",
    "生年月日",
    "出生日期",
    "출생일",
)
_LEAD_LABELS = (
    "Lead",
    "リード",
    "导联",
    "導程",
    "리드",
)
_UNIT_LABELS = (
    "Unit",
    "単位",
    "单位",
    "單位",
    "단위",
)

# Human-readable locale names that mirror the ``_*_LABELS`` tuples above.
# These two tuples are the single source of truth used to format both the
# ``LocaleUnrecognisedError`` message and the README "Locales" sections;
# anyone adding a new locale tuple entry must extend the appropriate one
# of these two so the user-facing surfaces stay in sync.
_VERIFIED_LOCALES: tuple[str, ...] = ("English", "Japanese")
_BEST_EFFORT_LOCALES: tuple[str, ...] = (
    "Chinese (Simplified)",
    "Chinese (Traditional)",
    "Korean",
)


def _match_header(line: str, labels: tuple[str, ...]) -> str | None:
    """If ``line`` is ``"<label>,..."`` for any label, return what follows.

    Preserves any commas that appear inside the value because the match is
    anchored to the *first* comma after the label, not split on every comma.
    """
    for label in labels:
        prefix = label + ","
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def _strip_bom(text: str) -> str:
    """Drop a leading UTF-8 BOM (``\\ufeff``) if present."""
    if text.startswith("﻿"):
        return text[1:]
    return text


def _parse_sample_rate(raw: str) -> float | None:
    """Extract the leading numeric span from a sample-rate value.

    Handles ``"513.992 hertz"`` (English, space-separated), ``"512ヘルツ"``
    (Japanese, no separator), and the compact form ``"512Hz"`` by collecting
    ASCII digits and a single decimal point until the first non-numeric char.
    """
    numeric_chars: list[str] = []
    for ch in raw:
        if ch.isascii() and (ch.isdigit() or ch == "."):
            numeric_chars.append(ch)
        else:
            break
    if not numeric_chars:
        return None
    try:
        return float("".join(numeric_chars))
    except ValueError:
        return None


_CHARDET_SNIFF_BYTES = 4096


def _read_text(path: Path) -> str:
    """Read ``path`` and decode with a UTF-8-first / chardet fallback strategy.

    Apple ECG CSVs are predominantly ASCII voltage samples after a small
    localized header, so chardet on the full file routinely misdetects
    Japanese / Chinese / Korean UTF-8 as Windows-1252 (a known weakness with
    ASCII-dominant inputs). We try strict UTF-8 first — the documented Apple
    format — and only fall back to chardet sniffing the first 4 KB when the
    UTF-8 decode itself fails. The BOM, if present, is stripped by the
    caller via :func:`_strip_bom`.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    detected = chardet.detect(raw[:_CHARDET_SNIFF_BYTES])
    encoding = detected.get("encoding") if detected else None
    if not encoding:
        encoding = "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def import_single_ecg(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    import_id: str,
    *,
    existing: ExistingHashes | None = None,
) -> bool:
    """Parse one ECG CSV at ``path`` and insert into ``conn``; return whether a row landed.

    Raises :class:`HealthImportError` when no recorded-date label is present
    (the file is not a valid ECG export). The caller in :func:`import_ecg_files`
    catches and logs that so a single bad CSV does not abort the batch.

    ``existing`` enables the Tier 2 incremental skip (issue #62): a CSV
    whose ``ecg_hash`` is already on disk is dropped before INSERT and
    its voltage samples are not parsed. Returns ``False`` in that case so
    the caller can keep its "ECG files actually imported" count accurate.
    """
    text = _strip_bom(_read_text(path))
    lines = text.splitlines()

    recorded_date = ""
    classification: str | None = None
    device: str | None = None
    sample_rate_hz: float | None = None
    symptoms: str | None = None
    software_version: str | None = None
    voltage_start = len(lines)  # default: no voltages found

    for idx, line in enumerate(lines):
        trimmed = line.strip()
        if trimmed == "":
            continue
        if (
            _match_header(trimmed, _NAME_LABELS) is not None
            or _match_header(trimmed, _DOB_LABELS) is not None
        ):
            # Privacy: name and date of birth are skipped intentionally.
            continue
        raw = _match_header(trimmed, _RECORDED_DATE_LABELS)
        if raw is not None:
            recorded_date = normalize_apple_offset(raw)
            continue
        raw = _match_header(trimmed, _CLASSIFICATION_LABELS)
        if raw is not None:
            classification = raw
            continue
        raw = _match_header(trimmed, _SYMPTOMS_LABELS)
        if raw is not None:
            if raw != "":
                symptoms = raw
            continue
        raw = _match_header(trimmed, _SOFTWARE_VERSION_LABELS)
        if raw is not None:
            software_version = raw
            continue
        raw = _match_header(trimmed, _DEVICE_LABELS)
        if raw is not None:
            # Apple wraps device strings in double quotes.
            device = raw.strip('"')
            continue
        raw = _match_header(trimmed, _SAMPLE_RATE_LABELS)
        if raw is not None:
            sample_rate_hz = _parse_sample_rate(raw)
            continue
        if (
            _match_header(trimmed, _LEAD_LABELS) is not None
            or _match_header(trimmed, _UNIT_LABELS) is not None
        ):
            continue
        # First unrecognized line: the voltage section starts here.
        voltage_start = idx
        break

    if recorded_date == "":
        # No "Recorded Date" header label matched. Most common cause is an
        # Apple Watch set to a locale whose header labels are not in our
        # lookup tables (see ``_RECORDED_DATE_LABELS`` etc. at the top of
        # this module). Surface the locale coverage explicitly and point the
        # user at a one-action remediation path so a silent skip turns into
        # a 30-second issue report. The locale lists are derived from the
        # module-level ``_VERIFIED_LOCALES`` / ``_BEST_EFFORT_LOCALES``
        # tuples so a future locale-tuple addition cannot leave this
        # message stale.
        raise LocaleUnrecognisedError(
            f"no recorded date found in ECG file: {path}. "
            f"This usually means the CSV header labels are in a locale we do "
            f"not yet recognise (verified: {', '.join(_VERIFIED_LOCALES)}; "
            f"best-effort: {', '.join(_BEST_EFFORT_LOCALES)}). "
            f"Please file an issue at {ISSUES_URL} "
            f"and paste the first 10 lines of the CSV so we can add your locale."
        )

    ecg_hash = compute_hash([recorded_date, device or ""])

    # Tier 2 incremental skip (issue #62). When the ecg_hash is already on
    # disk, skip the row AND every voltage sample below. We bail BEFORE
    # parsing the voltage section so the skip path also dodges the per-line
    # ``float`` cost (typical ECG carries ~15 000 voltage samples).
    if existing is not None and ecg_hash in existing.ecg_readings:
        _logger.debug("Skipping ECG %s: already on disk", path.name)
        return False

    conn.execute(
        """
        INSERT INTO ecg_readings (
            ecg_hash, recorded_date, classification, device,
            sample_rate_hz, symptoms, software_version, import_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ecg_hash,
            recorded_date,
            classification,
            device,
            sample_rate_hz,
            symptoms,
            software_version,
            import_id,
        ],
    )

    samples: list[tuple[str, int, float]] = []
    sample_idx = 0
    for line in lines[voltage_start:]:
        trimmed = line.strip()
        if trimmed == "":
            continue
        try:
            voltage = float(trimmed)
        except ValueError:
            # Mid-stream non-numeric line ends the voltage section -- the
            # Rust importer behaves the same way for forward compatibility.
            break
        # Reject NaN / Inf so a single bad sample does not poison the
        # whole ECG file's bulk load. DuckDB's CSV reader does not parse
        # ``inf`` / ``-inf`` into DOUBLE by default, so without this guard
        # one malformed voltage line would fail the entire COPY for the
        # file. Mirrors :func:`apple_health_mcp.importers.xml._parse_opt_float`
        # and :func:`apple_health_mcp.importers.gpx._parse_float`.
        if not math.isfinite(voltage):
            continue
        samples.append((ecg_hash, sample_idx, voltage))
        sample_idx += 1
    # Per-file ECGs often carry several thousand voltage samples; route
    # through bulk_load_via_arrow (issues #41 / #50) for the same perf
    # reason the XML importer does.
    bulk_load_via_arrow(conn, "ecg_samples", samples)
    return True


def import_ecg_files(
    conn: duckdb.DuckDBPyConnection,
    ecg_dir: Path,
    import_id: str,
    *,
    existing: ExistingHashes | None = None,
) -> int:
    """Import every ``*.csv`` under ``ecg_dir``; return the number imported.

    A missing directory is not an error -- many exports lack ECG data
    entirely. Individual file failures are logged and skipped so one bad
    CSV does not abort the import.

    The returned count tracks ECG rows actually INSERTed -- a CSV whose
    ``ecg_hash`` is already on disk (Tier 2 incremental re-import, issue
    #62) does not contribute to the count. The total skipped count is
    surfaced via the same ``Imported N (skipped M)`` log line so the
    caller can see whether the re-import contributed any new readings.
    """
    if not ecg_dir.exists():
        _logger.info("No electrocardiograms directory found, skipping ECG import")
        return 0

    entries = sorted(p for p in ecg_dir.iterdir() if p.suffix.lower() == ".csv")
    count = 0
    skipped = 0
    # Rate-limit the verbose locale-coverage guidance to one full emission
    # per import run. Without this, a user with N ECG files in an
    # unrecognised locale gets N copies of the same ~6-line guidance,
    # drowning unrelated warnings (XML / GPX) in the same import run.
    locale_help_shown = False
    for path in entries:
        try:
            inserted = import_single_ecg(conn, path, import_id, existing=existing)
        except LocaleUnrecognisedError as exc:
            if not locale_help_shown:
                _logger.warning("Failed to import ECG file %s: %s", path, exc)
                locale_help_shown = True
            else:
                _logger.warning(
                    "Failed to import ECG file %s: locale not recognised "
                    "(see earlier warning for the full guidance and "
                    "issue-tracker URL).",
                    path,
                )
        except HealthImportError as exc:
            _logger.warning("Failed to import ECG file %s: %s", path, exc)
        except OSError as exc:
            _logger.warning("Failed to read ECG file %s: %s", path, exc)
        else:
            if inserted:
                count += 1
            else:
                skipped += 1
    if skipped:
        _logger.info("Imported %d ECG recordings (skipped %d already on disk)", count, skipped)
    else:
        _logger.info("Imported %d ECG recordings", count)
    return count
