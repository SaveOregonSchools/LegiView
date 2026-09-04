from __future__ import annotations

import sqlite3

import pytest

from olis_archive.database import Database, MigrationError
from olis_archive.services.storage import StorageService


T1 = "2026-01-02T03:04:05Z"
T2 = "2026-02-03T04:05:06Z"
T3 = "2026-03-04T05:06:07Z"


def make_storage(tmp_path) -> StorageService:
    return StorageService(tmp_path / "legiview.sqlite3")


def seed_bill(storage: StorageService, *, session: str = "2026R1", bill: str = "SB1501") -> int:
    storage.upsert_session(
        {"session_key": session, "session_name": "2026 Regular Session"}, seen_at=T1
    )
    prefix = bill[:2]
    number = bill[2:]
    return storage.upsert_bill(
        {
            "session_key": session,
            "measure_prefix": prefix,
            "measure_number": number,
            "bill_id_compact": bill,
            "bill_id_display": f"{prefix} {number}",
            "bill_chamber": "Senate" if prefix == "SB" else "House",
            "bill_title": "Relating to a test measure.",
            "raw_json": {"MeasurePrefix": prefix, "MeasureNumber": int(number)},
        },
        seen_at=T1,
    )


def test_migrations_enable_wal_foreign_keys_and_expected_tables(tmp_path):
    path = tmp_path / "state" / "legiview.sqlite3"
    database = Database(path)

    assert database.initialize() == 5
    # A second startup is an idempotent migration no-op.
    assert database.initialize() == 5

    expected = {
        "schema_migrations",
        "app_settings",
        "sessions",
        "legislators",
        "committees",
        "bills",
        "bill_sponsors",
        "committee_meetings",
        "committee_agenda_items",
        "documents",
        "document_versions",
        "collection_runs",
        "collection_run_items",
        "collection_errors",
        "source_fetches",
        "source_sync_state",
        "session_archive_state",
        "olis_display_reconciliations",
        "source_anomalies",
        "document_remote_probes",
        "source_presence_events",
        "archive_claim_cursors",
    }
    assert set(database.table_names()) == expected
    assert database.foreign_key_violations() == []

    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO bills(
                    session_key, measure_prefix, measure_number, bill_id_compact,
                    bill_id_display, bill_chamber, first_collected_at, last_seen_at,
                    last_synced_at
                ) VALUES ('MISSING', 'SB', '1', 'SB1', 'SB 1', 'Senate', ?, ?, ?)
                """,
                (T1, T1, T1),
            )


@pytest.mark.parametrize(
    ("history_case", "expected_error"),
    [
        ("checksum", "modified after it was applied"),
        ("gap", "not a contiguous packaged prefix"),
        ("future", "newer than this LegiView build"),
    ],
)
def test_migration_manifest_rejects_drift_gaps_and_future_schema(
    tmp_path, history_case, expected_error
):
    database = Database(tmp_path / f"{history_case}.sqlite3")
    assert database.initialize() == 5
    assert database.migration_manifest_is_current()

    with database.transaction() as connection:
        if history_case == "checksum":
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=5", ("0" * 64,)
            )
        elif history_case == "gap":
            connection.execute("DELETE FROM schema_migrations WHERE version=4")
        else:
            connection.execute(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES (99,'future','future','2099-01-01T00:00:00Z')
                """
            )

    assert not database.migration_manifest_is_current()
    with pytest.raises(MigrationError, match=expected_error):
        database.initialize()


def test_stable_upserts_preserve_first_seen_and_do_not_duplicate(tmp_path):
    storage = make_storage(tmp_path)
    bill_id = seed_bill(storage)
    first = storage.get_bill("2026R1", "SB1501")

    same_bill_id = storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1501,
            "bill_title": "Updated official relating-to title.",
            # catchline is deliberately absent: a partial source response must not
            # erase columns that were not part of this observation.
        },
        seen_at=T2,
    )
    second = storage.get_bill("2026R1", "SB1501")

    assert same_bill_id == bill_id
    assert second["first_collected_at"] == first["first_collected_at"]
    assert second["last_seen_at"] == "2026-02-03T04:05:06.000000Z"
    assert second["bill_title"] == "Updated official relating-to title."
    assert second["raw_json"]

    sponsor = {
        "bill_id": bill_id,
        "source_measure_sponsor_id": "919001",
        "raw_sponsor_type": "Member",
        "raw_sponsor_level": "Chief",
        "normalized_category": "chief",
        "legislator_code": "DOE",
        "resolved_display_name": "Sen. Jane Doe",
        "sponsor_kind": "legislator",
    }
    sponsor_id = storage.upsert_bill_sponsor(sponsor, seen_at=T1)
    assert storage.upsert_bill_sponsor({**sponsor, "print_order": 1}, seen_at=T2) == sponsor_id
    assert len(storage.list_bill_sponsors(bill_id)) == 1

    document = {
        "bill_id": bill_id,
        "document_kind": "public_testimony",
        "source_section": "submitted_written_testimony",
        "source_entity_type": "PublicTestimony",
        "source_id": "244133",
        "title": "Written testimony",
        "canonical_download_url": "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/PublicTestimonyDocument/244133",
        "raw_json": {"documentId": 244133},
    }
    document_id = storage.upsert_document(document, seen_at=T1)
    assert storage.upsert_document({**document, "submitter": "A. Person"}, seen_at=T2) == document_id
    # Numeric IDs are namespaced by source family, as required by the canonical identity.
    other_family_id = storage.upsert_document(
        {
            **document,
            "source_entity_type": "CommitteeMeetingDocument",
            "document_kind": "committee_presentation",
        },
        seen_at=T2,
    )
    assert other_family_id != document_id
    assert len(storage.list_bill_documents(bill_id)) == 2


def test_payload_versions_are_immutable_and_discovery_does_not_reset_download(tmp_path):
    storage = make_storage(tmp_path)
    bill_id = seed_bill(storage)
    document_id = storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "floor_letter",
            "source_section": "floor_letters",
            "source_entity_type": "FloorLetter",
            "source_id": "4513",
            "title": "Floor letter",
        },
        seen_at=T1,
    )

    first_hash = "a" * 64
    first_version = storage.complete_document_download(
        document_id,
        sha256=first_hash,
        local_relative_path="2026R1/SB1501/floor_letter/4513/letter.pdf",
        local_filename="letter.pdf",
        downloaded_bytes=123,
        mime_type="application/pdf",
        downloaded_at=T1,
    )
    # Re-observing the source metadata does not regress a valid payload to discovered.
    storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "floor_letter",
            "source_section": "floor_letters",
            "source_entity_type": "FloorLetter",
            "source_id": "4513",
            "title": "Updated source title",
            "download_status": "discovered",
        },
        seen_at=T2,
    )
    logical = storage.get_document(document_id)
    assert logical["download_status"] == "downloaded"
    assert logical["sha256"] == first_hash
    assert logical["title"] == "Updated source title"

    # The same bytes select the existing version; changed bytes append a new one.
    assert storage.complete_document_download(
        document_id,
        sha256=first_hash,
        local_relative_path="ignored/new/path.pdf",
        downloaded_bytes=123,
        downloaded_at=T2,
    ) == first_version
    second_version = storage.complete_document_download(
        document_id,
        sha256="b" * 64,
        local_relative_path="2026R1/SB1501/floor_letter/4513/letter-v2.pdf",
        local_filename="letter-v2.pdf",
        downloaded_bytes=125,
        downloaded_at=T3,
    )
    versions = storage.list_document_versions(document_id)
    assert second_version != first_version
    assert [row["version_number"] for row in reversed(versions)] == [1, 2]
    assert {row["local_relative_path"] for row in versions} == {
        "2026R1/SB1501/floor_letter/4513/letter.pdf",
        "2026R1/SB1501/floor_letter/4513/letter-v2.pdf",
    }


def test_restart_normalization_is_atomic_recoverable_and_idempotent(tmp_path):
    storage = make_storage(tmp_path)
    bill_id = seed_bill(storage)
    document_id = storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "submitted_written_testimony",
            "source_entity_type": "PublicTestimony",
            "source_id": "244133",
        },
        seen_at=T1,
    )
    run_id = storage.create_run(
        "collect_bill", session_key="2026R1", bill_id_compact="SB1501", queued_at=T1
    )
    assert storage.claim_run(run_id, started_at=T1)
    item_id = storage.upsert_run_item(
        run_id,
        "document",
        "PublicTestimony:244133",
        stage="download_documents",
        status="running",
        changed_at=T1,
        bill_id=bill_id,
        document_id=document_id,
    )
    assert storage.queue_document(document_id)
    assert storage.claim_document(document_id, attempted_at=T1)
    version_id = storage.create_document_version(document_id, observed_at=T1)

    queued_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T1)
    counts = storage.normalize_interrupted_work(interrupted_at=T2)

    assert counts == {
        "collection_runs": 1,
        "collection_run_items": 1,
        "documents": 1,
        "document_versions": 1,
    }
    assert storage.get_run(run_id)["status"] == "interrupted"
    assert storage.get_run(queued_run)["status"] == "queued"
    assert storage.list_run_items(run_id)[0]["status"] == "interrupted"
    assert storage.get_document(document_id)["download_status"] == "interrupted"
    assert storage.list_document_versions(document_id)[0]["status"] == "interrupted"
    assert storage.normalize_interrupted_work(interrupted_at=T3) == {
        "collection_runs": 0,
        "collection_run_items": 0,
        "documents": 0,
        "document_versions": 0,
    }

    assert storage.requeue_interrupted_run(run_id, queued_at=T3)
    assert storage.get_run(run_id)["status"] == "queued"
    assert storage.list_run_items(run_id)[0]["status"] == "queued"
    assert storage.get_document(document_id)["download_status"] == "queued"
    # The interrupted payload attempt remains an audit record.
    assert storage.list_document_versions(document_id)[0]["id"] == version_id
    assert storage.list_document_versions(document_id)[0]["status"] == "interrupted"


def test_storage_run_claim_helpers_preserve_single_running_run(tmp_path):
    storage = make_storage(tmp_path)
    first = storage.create_run(
        "collect_session", session_key="2026R1", queued_at=T1
    )
    second = storage.create_run(
        "collect_session", session_key="2026R1", queued_at=T2
    )
    third = storage.create_run(
        "collect_session", session_key="2026R1", queued_at=T3
    )

    assert storage.claim_collection_run(first, started_at=T1)
    assert not storage.claim_collection_run(second, started_at=T2)
    assert storage.claim_next_collection_run(started_at=T2) is None
    assert storage.get_run(second)["status"] == "queued"
    assert storage.get_run(third)["status"] == "queued"

    storage.update_collection_run(first, status="completed", changed_at=T2)
    claimed = storage.claim_next_collection_run(started_at=T3)
    assert claimed is not None
    assert claimed["id"] == second
    assert claimed["status"] == "running"
    assert storage.get_run(third)["status"] == "queued"
    with pytest.raises(ValueError, match="claim_collection_run"):
        storage.update_collection_run(third, status="running", changed_at=T3)


def test_reference_watermarks_and_session_scoped_reference_reads(tmp_path):
    storage = make_storage(tmp_path)
    seed_bill(storage)
    storage.upsert_legislator(
        {
            "session_key": "2026R1",
            "legislator_code": "Sen Example",
            "display_name": "Alex Example",
            "source_created_at": "2026-01-08T16:46:04",
            "source_modified_at": "2026-05-15T12:28:47",
        }
    )
    storage.upsert_committee(
        {
            "session_key": "2026R1",
            "committee_code": "SEXAMPLE",
            "committee_name": "Examples",
            "source_created_at": "2026-01-09T16:56:56",
            "source_modified_at": "2026-02-25T15:31:01",
        }
    )

    assert storage.reference_source_watermark("legislators", "2026R1") == "2026-05-15T12:28:47"
    assert storage.reference_source_watermark("committees", "2026R1") == "2026-02-25T15:31:01"
    assert [row["legislator_code"] for row in storage.list_legislators("2026R1")] == [
        "Sen Example"
    ]
    assert [row["committee_code"] for row in storage.list_committees("2026R1")] == [
        "SEXAMPLE"
    ]
    assert storage.reference_source_watermark("legislators", "2014R1") is None
    with pytest.raises(ValueError, match="unsupported reference entity"):
        storage.reference_source_watermark("bills", "2026R1")
