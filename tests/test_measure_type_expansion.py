from pathlib import Path
import sqlite3

import pytest

from olis_archive.config import AppConfig
from olis_archive.services.archive_paths import relative_document_directory
from olis_archive.services.archive_queries import ArchiveQueries
from olis_archive.services.collection import (
    CollectionService,
    SessionReferenceData,
    SourceRecordMismatch,
)
from olis_archive.services.historical_collection import HistoricalCollectionService
from olis_archive.services.source_mapping import MEASURE_PREFIX_METADATA
from olis_archive.services.storage import StorageService


def test_storage_persists_every_supported_measure_type(tmp_path):
    storage = StorageService(tmp_path / "legiview.sqlite3")
    storage.upsert_session(
        {"session_key": "2025R1", "session_name": "2025 Regular Session"}
    )

    for number, (prefix, metadata) in enumerate(
        MEASURE_PREFIX_METADATA.items(), start=1
    ):
        bill_id = storage.upsert_bill(
            {
                "session_key": "2025R1",
                "measure_prefix": prefix,
                "measure_number": number,
                "bill_id_compact": f"{prefix}{number}",
                "bill_id_display": f"{prefix} {number}",
                "prefix_meaning": metadata.official_meaning,
            }
        )
        stored = storage.get_bill_by_id(bill_id)
        assert stored is not None
        assert stored["measure_prefix"] == prefix
        assert stored["measure_type"] == metadata.measure_type
        assert stored["bill_chamber"] == metadata.originating_chamber

    assert storage.database.foreign_key_violations() == []


def test_storage_rejects_prefix_metadata_mismatches(tmp_path):
    storage = StorageService(tmp_path / "legiview.sqlite3")
    storage.upsert_session(
        {"session_key": "2025R1", "session_name": "2025 Regular Session"}
    )

    with pytest.raises(ValueError, match="originates in the House"):
        storage.upsert_bill(
            {
                "session_key": "2025R1",
                "measure_prefix": "HJR",
                "measure_number": 11,
                "bill_chamber": "Senate",
            }
        )
    with pytest.raises(ValueError, match="joint_resolution"):
        storage.upsert_bill(
            {
                "session_key": "2025R1",
                "measure_prefix": "HJR",
                "measure_number": 11,
                "measure_type": "bill",
            }
        )
    with pytest.raises(ValueError, match="bill_id_compact does not match"):
        storage.upsert_bill(
            {
                "session_key": "2025R1",
                "measure_prefix": "HJR",
                "measure_number": 11,
                "bill_id_compact": "SB999",
            }
        )
    with pytest.raises(ValueError, match="bill_id_display does not match"):
        storage.upsert_bill(
            {
                "session_key": "2025R1",
                "measure_prefix": "HJR",
                "measure_number": 11,
                "bill_id_display": "HJR 12",
            }
        )


def test_database_enforces_prefix_type_and_originating_chamber_pairs(tmp_path):
    storage = StorageService(tmp_path / "legiview.sqlite3")
    storage.upsert_session(
        {"session_key": "2025R1", "session_name": "2025 Regular Session"}
    )
    bill_id = storage.upsert_bill(
        {
            "session_key": "2025R1",
            "measure_prefix": "HJR",
            "measure_number": 11,
        }
    )

    with pytest.raises(sqlite3.IntegrityError):
        with storage.database.transaction() as connection:
            connection.execute(
                "UPDATE bills SET measure_type='bill' WHERE id=?", (bill_id,)
            )
    with pytest.raises(sqlite3.IntegrityError):
        with storage.database.transaction() as connection:
            connection.execute(
                "UPDATE bills SET bill_chamber='Senate' WHERE id=?", (bill_id,)
            )


def test_session_chamber_counts_include_non_bill_measures(tmp_path):
    storage = StorageService(tmp_path / "legiview.sqlite3")
    storage.upsert_session(
        {
            "session_key": "2014R1",
            "session_name": "2014 Regular Session",
            "begin_date": "2014-02-03T00:00:00",
        }
    )
    for prefix, number in (("HB", 1), ("HJR", 2), ("SJM", 3)):
        storage.upsert_bill(
            {
                "session_key": "2014R1",
                "measure_prefix": prefix,
                "measure_number": number,
            }
        )

    queries = ArchiveQueries(storage.database)
    status = queries.session_status(limit=10)

    assert status.total == 1
    assert status.rows[0]["house_measure_count"] == 2
    assert status.rows[0]["senate_measure_count"] == 1
    assert status.rows[0]["hb_count"] == 1
    assert status.rows[0]["sb_count"] == 0

    export_sql, export_params = queries.session_export_query()
    with storage.database.connection() as connection:
        exported = connection.execute(export_sql, export_params).fetchone()
    assert exported["house_measure_count"] == 2
    assert exported["senate_measure_count"] == 1
    assert exported["hb_count"] == 1
    assert exported["sb_count"] == 0


def test_targeted_collection_run_accepts_a_non_bill_measure(tmp_path):
    config = AppConfig(
        project_root=tmp_path,
        database_path=Path("data/legiview.sqlite3"),
        archive_root=Path("archive"),
        minimum_free_space_gb=0,
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )
    service = CollectionService(config)

    run_id = service.create_collect_bill_run("2025R1", "hjr 11")
    run = service.runs.get_run(run_id)

    assert run is not None
    assert run["requested_bill_id_compact"] == "HJR11"


def test_targeted_collection_rejects_a_different_returned_measure(tmp_path):
    config = AppConfig(
        project_root=tmp_path,
        database_path=Path("data/legiview.sqlite3"),
        archive_root=Path("archive"),
        minimum_free_space_gb=0,
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )
    service = CollectionService(config)
    run_id = service.create_collect_bill_run("2025R1", "HJR11")
    assert service.runs.claim_run(run_id)

    with pytest.raises(
        SourceRecordMismatch,
        match=r"2025R1/SJR11.*2025R1/HJR11",
    ):
        service.collect_bill(
            run_id,
            "2025R1",
            "HJR11",
            known_measure={
                "SessionKey": "2025R1",
                "MeasurePrefix": "SJR",
                "MeasureNumber": 11,
            },
            reference_data=SessionReferenceData("2025R1", {}, {}, {}),
        )

    assert service.storage.get_bill("2025R1", "SJR11") is None


def test_historical_child_lookup_and_archive_path_accept_non_bill_measure(tmp_path):
    storage = StorageService(tmp_path / "legiview.sqlite3")
    storage.upsert_session(
        {"session_key": "2025R1", "session_name": "2025 Regular Session"}
    )
    bill_id = storage.upsert_bill(
        {
            "session_key": "2025R1",
            "measure_prefix": "HJR",
            "measure_number": 11,
        }
    )
    service = HistoricalCollectionService(
        AppConfig(
            project_root=tmp_path,
            database_path=Path("legiview.sqlite3"),
            archive_root=Path("archive"),
            minimum_free_space_gb=0,
            minimum_free_space_bytes=0,
        ),
        database=storage.database,
        storage=storage,
        runs=object(),
        odata=object(),
        olis_http=object(),
        size_probe=object(),
        download_claimed=lambda _run_id, _document: (0, "skipped", 0),
    )
    service._reset_session_cache("2025R1")

    row = service._bill_from_source_row(
        {
            "SessionKey": "2025R1",
            "MeasurePrefix": "HJR",
            "MeasureNumber": 11,
        }
    )

    assert int(row["id"]) == bill_id
    assert relative_document_directory(
        "2025R1", "HJR11", "public_testimony", 147861
    ).as_posix() == "2025R1/HJR11/public_testimony/147861"
