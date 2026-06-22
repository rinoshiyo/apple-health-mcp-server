"""StateOfMind path through the XML importer.

iOS 17+ State of Mind records show up as Category records with extra
``MetadataEntry`` children. The importer breaks them out into the
``state_of_mind`` table so ``list_state_of_mind`` can return them as
first-class fields.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import duckdb
import pytest

from apple_health_mcp.db import ensure_schema, get_in_memory_connection
from apple_health_mcp.importers.xml import import_xml


@pytest.fixture
def conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    c = get_in_memory_connection()
    ensure_schema(c)
    yield c
    c.close()


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "export.xml"
    p.write_text(body, encoding="utf-8")
    return p


def test_state_of_mind_with_metadata_populates_dedicated_table(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.7" startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:00:00 +0000">
  <MetadataEntry key="HKMetadataKeyMoodValenceClassification" value="0.42"/>
  <MetadataEntry key="HKMetadataKeyMoodLabels" value="Joy,Calm"/>
  <MetadataEntry key="HKMetadataKeyMoodAssociations" value="Family,Friends"/>
  <MetadataEntry key="HKMetadataKeyMoodKind" value="momentary"/>
 </Record>
</HealthData>"""
    stats = import_xml(conn, _write(tmp_path, xml), "imp_som")
    assert stats.state_of_mind_rows == 1
    row = conn.execute("SELECT valence, kind, labels, associations FROM state_of_mind").fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.42)
    assert row[1] == "momentary"
    assert row[2] == "Joy,Calm"
    assert row[3] == "Family,Friends"


def test_state_of_mind_falls_back_to_record_value_when_metadata_missing(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.3" startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:00:00 +0000"/>
</HealthData>"""
    stats = import_xml(conn, _write(tmp_path, xml), "imp_som2")
    assert stats.state_of_mind_rows == 1
    row = conn.execute("SELECT valence, kind, labels, associations FROM state_of_mind").fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.3)
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None


def test_state_of_mind_ignores_unparseable_valence(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.1" startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:00:00 +0000">
  <MetadataEntry key="HKMetadataKeyMoodValence" value="not-a-number"/>
  <MetadataEntry key="HKMetadataKeyMoodValenceInfinity" value="inf"/>
 </Record>
</HealthData>"""
    import_xml(conn, _write(tmp_path, xml), "imp_som3")
    # Unparseable / non-finite valence is ignored; record-level value sticks.
    row = conn.execute("SELECT valence FROM state_of_mind").fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.1)


def test_state_of_mind_unrelated_metadata_key_left_alone(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Metadata keys outside the StateOfMind vocabulary fall through every
    elif branch without raising and without polluting the dedicated row."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.2" startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:00:00 +0000">
  <MetadataEntry key="HKMetadataKeyUserMotionContext" value="active"/>
 </Record>
</HealthData>"""
    import_xml(conn, _write(tmp_path, xml), "imp_som5")
    row = conn.execute("SELECT valence, kind, labels, associations FROM state_of_mind").fetchone()
    assert row is not None
    assert row == (pytest.approx(0.2), None, None, None)


def test_state_of_mind_batch_flush_threshold_fires(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-import batch flush of state_of_mind fires once _BATCH_SIZE is hit."""
    from apple_health_mcp.importers import xml as xml_module

    monkeypatch.setattr(xml_module, "_BATCH_SIZE", 1)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.4" startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:00:00 +0000"/>
 <Record type="HKCategoryTypeIdentifierStateOfMind" sourceName="iPhone"
         value="0.6" startDate="2024-06-02 09:00:00 +0000"
         endDate="2024-06-02 09:00:00 +0000"/>
</HealthData>"""
    stats = import_xml(conn, _write(tmp_path, xml), "imp_som_batch")
    assert stats.state_of_mind_rows == 2
    rows = conn.execute("SELECT COUNT(*) FROM state_of_mind").fetchone()
    assert rows is not None and int(rows[0]) == 2


def test_non_state_of_mind_record_leaves_table_empty(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple Watch"
         unit="count/min" value="72"
         startDate="2024-06-01 09:00:00 +0000"
         endDate="2024-06-01 09:01:00 +0000">
  <MetadataEntry key="HKMetadataKeyMoodLabels" value="should-not-leak"/>
 </Record>
</HealthData>"""
    stats = import_xml(conn, _write(tmp_path, xml), "imp_som4")
    assert stats.state_of_mind_rows == 0
    row = conn.execute("SELECT COUNT(*) FROM state_of_mind").fetchone()
    assert row is not None and int(row[0]) == 0
