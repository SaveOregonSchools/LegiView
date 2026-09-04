from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from olis_archive.database import Database
from olis_archive.services.runs import RunStore
from olis_archive.services.source_mapping import normalize_bill_id
from olis_archive.services.storage import StorageService


T1 = "2026-01-02T03:04:05Z"
T2 = "2026-02-03T04:05:06Z"
T3 = "2026-03-04T05:06:07Z"


def _storage(tmp_path: Path) -> StorageService:
    return StorageService(tmp_path / "legiview.sqlite3")


def _seed_session(storage: StorageService, key: str = "2026R1") -> None:
    storage.upsert_session(
        {"session_key": key, "session_name": f"Session {key}"}, seen_at=T1
    )


def _seed_bill(
    storage: StorageService,
    *,
    session_key: str = "2026R1",
    compact: str = "SB1501",
    run_id: int | None = None,
) -> int:
    _seed_session(storage, session_key)
    prefix, number, normalized, _display = normalize_bill_id(compact)
    return storage.upsert_bill(
        {
            "session_key": session_key,
            "measure_prefix": prefix,
            "measure_number": number,
            "bill_id_compact": normalized,
            "bill_title": "Relating to a persistence test.",
        },
        seen_at=T1,
        run_id=run_id,
    )


def _seed_document(
    storage: StorageService,
    bill_id: int,
    source_id: str,
    *,
    seen_at: str = T1,
    run_id: int | None = None,
    raw_type: str = "Testimony",
    kind: str = "public_testimony",
) -> int:
    return storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": kind,
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": source_id,
            "raw_document_type": raw_type,
            "canonical_download_url": (
                "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
                f"PublicTestimonyDocument/{source_id}"
            ),
        },
        seen_at=seen_at,
        run_id=run_id,
    )


def test_phase1_database_upgrades_without_data_loss(tmp_path: Path):
    migrations = tmp_path / "phase1_migrations"
    migrations.mkdir()
    source = Path(__file__).parents[1] / "olis_archive" / "migrations" / "001_initial.sql"
    shutil.copyfile(source, migrations / source.name)
    path = tmp_path / "upgraded.sqlite3"
    phase1 = Database(path, migrations_path=migrations)
    assert phase1.initialize() == 1
    with phase1.transaction() as connection:
        connection.execute(
            """
            INSERT INTO collection_runs(
                id,run_uuid,run_type,requested_scope_json,status,stage,queued_at,
                updated_at,config_snapshot_json,summary_json
            ) VALUES (7,'phase1-run','collect_bill','{}','completed','finalize',?,?, '{}','{}')
            """,
            (T1, T1),
        )
        connection.execute(
            """
            INSERT INTO sessions(
                session_key,session_name,first_seen_at,last_seen_at,last_synced_at,raw_json
            ) VALUES ('2014R1','2014 Regular Session',?,?,?,'{}')
            """,
            (T1, T1, T1),
        )
        bill_cursor = connection.execute(
            """
            INSERT INTO bills(
                session_key,measure_prefix,measure_number,bill_id_compact,bill_id_display,
                bill_chamber,bill_title,first_collected_at,last_seen_at,last_synced_at,raw_json
            ) VALUES ('2014R1','HB','4111','HB4111','HB 4111','House','Retained',?,?,?,'{}')
            """,
            (T1, T1, T1),
        )
        bill_id = int(bill_cursor.lastrowid)
        document_cursor = connection.execute(
            """
            INSERT INTO documents(
                bill_id,session_key,bill_id_compact,document_kind,source_section,
                source_entity_type,source_id,first_seen_at,last_seen_at,download_status,
                validation_status,raw_json
            ) VALUES (?,'2014R1','HB4111','committee_presentation','presentations',
                      'CommitteeMeetingDocument','32769',?,?,'downloaded','valid','{}')
            """,
            (bill_id, T1, T1),
        )
        document_id = int(document_cursor.lastrowid)
        version_cursor = connection.execute(
            """
            INSERT INTO document_versions(
                document_id,collection_run_id,version_number,observed_at,status,
                validation_status,created_at,completed_at
            ) VALUES (?,7,1,?,'downloaded','valid',?,?)
            """,
            (document_id, T1, T1, T1),
        )
        connection.execute(
            "UPDATE documents SET current_version_id=? WHERE id=?",
            (int(version_cursor.lastrowid), document_id),
        )
        connection.execute(
            """
            INSERT INTO collection_run_items(
                run_id,item_type,item_key,bill_id,document_id,stage,status,
                queued_at,started_at,finished_at,updated_at
            ) VALUES (7,'document','CommitteeMeetingDocument:32769',?,?,'download_documents',
                      'completed',?,?,?,?)
            """,
            (bill_id, document_id, T1, T1, T1, T1),
        )

    upgraded = Database(path)
    assert upgraded.initialize() == 6
    assert upgraded.foreign_key_violations() == []
    with upgraded.connection() as connection:
        assert connection.execute(
            "SELECT bill_title FROM bills WHERE session_key='2014R1'"
        ).fetchone()[0] == "Retained"
        assert connection.execute(
            "SELECT run_type FROM collection_runs WHERE id=7"
        ).fetchone()[0] == "collect_bill"
        assert connection.execute(
            "SELECT collection_run_id FROM document_versions WHERE document_id=?",
            (document_id,),
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT session_key FROM collection_run_items WHERE document_id=?",
            (document_id,),
        ).fetchone()[0] == "2014R1"
        connection.execute(
            """
            INSERT INTO collection_runs(
                run_uuid,run_type,requested_scope_json,status,stage,queued_at,updated_at
            ) VALUES ('phase2-run','inventory_backfill','{}','queued','queued',?,?)
            """,
            (T2, T2),
        )


def test_display_reconciliation_migration_preserves_v2_rows_and_adds_family_key(
    tmp_path: Path,
):
    migrations = tmp_path / "phase2_migrations"
    migrations.mkdir()
    source_root = Path(__file__).parents[1] / "olis_archive" / "migrations"
    for name in ("001_initial.sql", "002_historical_inventory.sql"):
        shutil.copyfile(source_root / name, migrations / name)

    path = tmp_path / "phase2.sqlite3"
    phase2 = Database(path, migrations_path=migrations)
    assert phase2.initialize() == 2
    with phase2.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sessions(
                session_key,session_name,first_seen_at,last_seen_at,last_synced_at
            ) VALUES ('2026R1','Session 2026R1',?,?,?)
            """,
            (T1, T1, T1),
        )
        bill_id = int(
            connection.execute(
                """
                INSERT INTO bills(
                    session_key,measure_prefix,measure_number,bill_id_compact,
                    bill_id_display,bill_chamber,bill_title,first_collected_at,
                    last_seen_at,last_synced_at
                ) VALUES ('2026R1','SB','1501','SB1501','SB 1501','Senate',
                          'Public testimony measure',?,?,?)
                """,
                (T1, T1, T1),
            ).lastrowid
        )
        committee_bill_id = int(
            connection.execute(
                """
                INSERT INTO bills(
                    session_key,measure_prefix,measure_number,bill_id_compact,
                    bill_id_display,bill_chamber,bill_title,first_collected_at,
                    last_seen_at,last_synced_at
                ) VALUES ('2026R1','HB','4111','HB4111','HB 4111','House',
                          'Committee document measure',?,?,?)
                """,
                (T1, T1, T1),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO documents(
                bill_id,session_key,bill_id_compact,document_kind,source_section,
                source_entity_type,source_id,first_seen_at,last_seen_at
            ) VALUES (?,'2026R1','SB1501','public_testimony','odata_public_testimony',
                      'CommitteePublicTestimony','10',?,?)
            """,
            (bill_id, T1, T1),
        )
        connection.execute(
            """
            INSERT INTO documents(
                bill_id,session_key,bill_id_compact,document_kind,source_section,
                source_entity_type,source_id,raw_document_type,canonical_download_url,
                first_seen_at,last_seen_at
            ) VALUES (?,'2026R1','HB4111','committee_presentation',
                      'odata_committee_document','CommitteeMeetingDocument','32769',
                      'Presentation',
                      'https://olis.oregonlegislature.gov/liz/2026R1/Downloads/CommitteeMeetingDocument/32769',
                      ?,?)
            """,
            (committee_bill_id, T1, T1),
        )
        connection.execute(
            """
            INSERT INTO olis_display_reconciliations(
                bill_id,session_key,status,checked_at,odata_record_count,
                displayed_record_count,page_only_count,odata_only_count,
                source_url,details_json
            ) VALUES (?,'2026R1','checked_with_records',?,2,1,0,1,
                      'https://olis.oregonlegislature.gov/example','{"legacy":true}')
            """,
            (bill_id, T1),
        )
        connection.execute(
            """
            INSERT INTO olis_display_reconciliations(
                bill_id,session_key,status,checked_at,odata_record_count,
                displayed_record_count,page_only_count,odata_only_count,
                source_url,details_json
            ) VALUES (?,'2026R1','checked_with_records',?,1,1,0,0,
                      'https://olis.oregonlegislature.gov/legacy','{"legacy":true}')
            """,
            (committee_bill_id, T1),
        )

    upgraded = Database(path)
    assert upgraded.initialize() == 6
    migrated = StorageService(upgraded, initialize=False)
    rows = migrated.list_olis_display_reconciliations(bill_id)
    assert len(rows) == 1
    assert rows[0]["source_entity_type"] == "CommitteePublicTestimony"
    assert rows[0]["odata_only_count"] == 1
    assert json.loads(rows[0]["details_json"]) == {"legacy": True}
    committee_rows = migrated.list_olis_display_reconciliations(committee_bill_id)
    assert len(committee_rows) == 1
    assert committee_rows[0]["source_entity_type"] == "CommitteeMeetingDocument"
    assert committee_rows[0]["displayed_record_count"] == 1

    migrated.record_olis_display_reconciliation(
        bill_id,
        "checked_zero",
        source_entity_type="CommitteeMeetingDocument",
        checked_at=T2,
        odata_record_count=0,
        displayed_record_count=0,
    )
    assert {
        row["source_entity_type"]
        for row in migrated.list_olis_display_reconciliations(bill_id)
    } == {"CommitteePublicTestimony", "CommitteeMeetingDocument"}
    assert upgraded.foreign_key_violations() == []


def test_expanded_measure_migration_preserves_data_and_invalidates_old_scope(
    tmp_path: Path,
):
    migrations = tmp_path / "pre_expansion_migrations"
    migrations.mkdir()
    source_root = Path(__file__).parents[1] / "olis_archive" / "migrations"
    for name in (
        "001_initial.sql",
        "002_historical_inventory.sql",
        "003_display_reconciliation_source_family.sql",
        "004_archive_claim_plans.sql",
        "005_archive_claim_cursors.sql",
    ):
        shutil.copyfile(source_root / name, migrations / name)

    path = tmp_path / "expanded.sqlite3"
    before = Database(path, migrations_path=migrations)
    assert before.initialize() == 5
    with before.transaction() as connection:
        connection.execute(
            """
            INSERT INTO collection_runs(
                id,run_uuid,run_type,requested_scope_json,status,stage,queued_at,
                started_at,finished_at,updated_at,sessions_total,sessions_completed,
                bills_total,documents_discovered,summary_json
            ) VALUES (7,'terminal-inventory','inventory_backfill','{}','completed',
                      'finalize_run',?,?,?,?,1,1,1,1,'{"historical":true}')
            """,
            (T1, T1, T2, T2),
        )
        connection.execute(
            """
            INSERT INTO collection_runs(
                id,run_uuid,run_type,requested_scope_json,status,stage,queued_at,
                started_at,updated_at,sessions_total,sessions_completed,
                sessions_incomplete,sessions_failed,bills_total,
                bills_completed,documents_discovered,summary_json
            ) VALUES (8,'paused-inventory','inventory_backfill','{}','paused',
                      'sync_measures',?,?,?,1,1,1,1,99,77,88,'{"partial":true}')
            """,
            (T1, T1, T2),
        )
        connection.execute(
            """
            INSERT INTO sessions(
                session_key,session_name,first_seen_at,last_seen_at,last_synced_at
            ) VALUES ('2026R1','2026 Regular Session',?,?,?)
            """,
            (T1, T1, T1),
        )
        connection.execute(
            """
            INSERT INTO bills(
                id,session_key,measure_id,measure_prefix,measure_number,
                bill_id_compact,bill_id_display,bill_chamber,bill_title,
                first_collected_at,last_seen_at,last_synced_at,
                last_collected_run_id,raw_json,source_presence,
                last_source_reconciled_at
            ) VALUES (41,'2026R1','9001','HB','4001','HB4001','HB 4001',
                      'House','Preserved measure',?,?,?,7,'{"preserved":true}',
                      'active',?)
            """,
            (T1, T2, T2, T2),
        )
        connection.execute(
            """
            INSERT INTO documents(
                id,bill_id,session_key,bill_id_compact,document_kind,
                source_section,source_entity_type,source_id,title,
                first_seen_at,last_seen_at,last_seen_run_id
            ) VALUES (51,41,'2026R1','HB4001','public_testimony',
                      'odata_public_testimony','CommitteePublicTestimony','123',
                      'Preserved testimony',?,?,7)
            """,
            (T1, T2),
        )
        connection.execute(
            """
            INSERT INTO olis_display_reconciliations(
                bill_id,source_entity_type,session_key,status,checked_at,run_id,
                odata_record_count,displayed_record_count,page_only_count,
                odata_only_count,details_json
            ) VALUES (41,'CommitteePublicTestimony','2026R1',
                      'checked_with_records',?,7,1,1,0,0,'{}')
            """,
            (T2,),
        )
        for entity_set in (
            "Measures",
            "CommitteePublicTestimonies",
            "Legislators",
        ):
            connection.execute(
                """
                INSERT INTO source_sync_state(
                    session_key,entity_set,sync_strategy,last_attempted_at,
                    last_successful_sync_at,last_full_session_sync_at,
                    last_incremental_sync_at,source_watermark,
                    last_successful_run_id,last_returned_source_count,
                    last_reconciliation_outcome,is_incomplete,last_failure_at,
                    last_error_class,last_error_message,details_json,updated_at
                ) VALUES ('2026R1',?,'watermark',?,?,?,?,?,7,10,
                          'incremental_overlap',0,?,'TimeoutError','preserved',
                          '{"preserved":true}',?)
                """,
                (entity_set, T2, T2, T2, T2, T2, T2, T2),
            )
        connection.execute(
            """
            INSERT INTO session_archive_state(
                session_key,inventory_status,last_inventory_started_at,
                last_inventory_completed_at,last_inventory_run_id,
                last_successful_inventory_run_id,last_download_started_at,
                last_download_completed_at,last_download_run_id,
                display_reconciliation_status,last_testimony_reconciled_at,
                source_anomaly_count,material_anomaly_count,
                completeness_details_json,updated_at
            ) VALUES ('2026R1','inventory_complete',?,?,7,7,?,?,7,
                      'complete',?,7,2,'{"complete":true}',?)
            """,
            (T1, T2, T2, T3, T2, T2),
        )
        for run_id in (7, 8):
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,stage,status,
                    current_activity,progress_current,progress_total,queued_at,
                    started_at,finished_at,updated_at
                ) VALUES (?,'session','2026R1','2026R1','finalize_session',
                          'completed','Complete under old scope',1,1,?,?,?,?)
                """,
                (run_id, T1, T1, T2, T2),
            )

    upgraded = Database(path)
    assert upgraded.initialize() == 6
    assert upgraded.foreign_key_violations() == []
    with upgraded.connection() as connection:
        bill = connection.execute(
            "SELECT * FROM bills WHERE id=41"
        ).fetchone()
        assert bill is not None
        assert bill["measure_prefix"] == "HB"
        assert bill["measure_type"] == "bill"
        assert bill["bill_title"] == "Preserved measure"
        assert bill["last_collected_run_id"] == 7
        assert json.loads(bill["raw_json"]) == {"preserved": True}
        assert connection.execute(
            "SELECT bill_id FROM documents WHERE id=51"
        ).fetchone()[0] == 41
        assert connection.execute(
            "SELECT bill_id FROM olis_display_reconciliations WHERE bill_id=41"
        ).fetchone()[0] == 41

        measure_type_column = next(
            row for row in connection.execute("PRAGMA table_info(bills)")
            if row["name"] == "measure_type"
        )
        assert measure_type_column["notnull"] == 1
        indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(bills)")
        }
        assert {
            "idx_bills_session_measure_id",
            "idx_bills_session_chamber_number",
            "idx_bills_title",
            "idx_bills_last_synced",
            "idx_bills_id_session",
            "idx_bills_presence_page",
            "idx_bills_session_compact",
        } <= indexes

        sync_rows = connection.execute(
            "SELECT * FROM source_sync_state ORDER BY entity_set"
        ).fetchall()
        assert len(sync_rows) == 3
        sync_by_entity = {row["entity_set"]: row for row in sync_rows}
        for entity_set in ("Measures", "CommitteePublicTestimonies"):
            row = sync_by_entity[entity_set]
            assert row["last_attempted_at"] == T2
            assert row["last_successful_sync_at"] is None
            assert row["last_full_session_sync_at"] is None
            assert row["last_incremental_sync_at"] is None
            assert row["source_watermark"] is None
            assert row["last_successful_run_id"] is None
            assert row["last_returned_source_count"] is None
            assert row["last_reconciliation_outcome"] == (
                "invalidated_expanded_measure_scope"
            )
            assert row["is_incomplete"] == 1
            assert row["last_error_class"] == "TimeoutError"
            assert json.loads(row["details_json"]) == {"preserved": True}
        reference_sync = sync_by_entity["Legislators"]
        assert reference_sync["last_successful_sync_at"] == T2
        assert reference_sync["last_full_session_sync_at"] == T2
        assert reference_sync["last_incremental_sync_at"] == T2
        assert reference_sync["source_watermark"] == T2
        assert reference_sync["last_successful_run_id"] == 7
        assert reference_sync["last_returned_source_count"] == 10
        assert reference_sync["last_reconciliation_outcome"] == "incremental_overlap"
        assert reference_sync["is_incomplete"] == 0

        session_state = connection.execute(
            "SELECT * FROM session_archive_state WHERE session_key='2026R1'"
        ).fetchone()
        assert session_state["inventory_status"] == "not_started"
        assert session_state["last_inventory_started_at"] is None
        assert session_state["last_inventory_completed_at"] is None
        assert session_state["last_inventory_run_id"] is None
        assert session_state["last_successful_inventory_run_id"] is None
        assert session_state["display_reconciliation_status"] is None
        assert session_state["last_testimony_reconciled_at"] is None
        assert session_state["last_download_started_at"] == T2
        assert session_state["last_download_completed_at"] == T3
        assert session_state["last_download_run_id"] == 7
        assert session_state["source_anomaly_count"] == 7
        assert session_state["material_anomaly_count"] == 2
        assert json.loads(session_state["completeness_details_json"]) == {
            "invalidated_by_migration": 6,
            "reason": "expanded_measure_scope",
        }

        terminal_item = connection.execute(
            "SELECT status FROM collection_run_items WHERE run_id=7"
        ).fetchone()
        paused_item = connection.execute(
            """
            SELECT status,progress_current,progress_total,started_at,finished_at,
                   details_json
            FROM collection_run_items WHERE run_id=8
            """
        ).fetchone()
        assert terminal_item["status"] == "completed"
        assert paused_item["status"] == "interrupted"
        assert paused_item["progress_current"] == 0
        assert paused_item["progress_total"] is None
        assert paused_item["started_at"] is None
        assert paused_item["finished_at"] is None
        assert paused_item["details_json"] == "{}"
        terminal_run = connection.execute(
            "SELECT sessions_completed,bills_total,summary_json FROM collection_runs WHERE id=7"
        ).fetchone()
        paused_run = connection.execute(
            """
            SELECT sessions_completed,sessions_incomplete,sessions_failed,
                   bills_total,bills_completed,documents_discovered,summary_json
            FROM collection_runs WHERE id=8
            """
        ).fetchone()
        assert tuple(terminal_run) == (1, 1, '{"historical":true}')
        assert tuple(paused_run) == (0, 0, 0, 0, 0, 0, "{}")

        prefix_types = {
            "HB": ("bill", "House"),
            "SB": ("bill", "Senate"),
            "HJR": ("joint_resolution", "House"),
            "SJR": ("joint_resolution", "Senate"),
            "HCR": ("concurrent_resolution", "House"),
            "SCR": ("concurrent_resolution", "Senate"),
            "HR": ("resolution", "House"),
            "SR": ("resolution", "Senate"),
            "HJM": ("joint_memorial", "House"),
            "SJM": ("joint_memorial", "Senate"),
            "HM": ("memorial", "House"),
            "SM": ("memorial", "Senate"),
        }
        inserted_ids = []
        for number, (prefix, (measure_type, chamber)) in enumerate(
            prefix_types.items(), start=101
        ):
            inserted_ids.append(
                int(
                    connection.execute(
                        """
                        INSERT INTO bills(
                            session_key,measure_prefix,measure_type,measure_number,
                            bill_id_compact,bill_id_display,bill_chamber,
                            first_collected_at,last_seen_at,last_synced_at
                        ) VALUES ('2026R1',?,?,?,?,?,?,?, ?, ?)
                        """,
                        (
                            prefix,
                            measure_type,
                            str(number),
                            f"{prefix}{number}",
                            f"{prefix} {number}",
                            chamber,
                            T1,
                            T1,
                            T1,
                        ),
                    ).lastrowid
                )
            )
        assert min(inserted_ids) > 41
        assert {
            (row["measure_prefix"], row["measure_type"])
            for row in connection.execute(
                "SELECT measure_prefix,measure_type FROM bills WHERE id<>41"
            )
        } == {(prefix, values[0]) for prefix, values in prefix_types.items()}

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO bills(
                    session_key,measure_prefix,measure_type,measure_number,
                    bill_id_compact,bill_id_display,bill_chamber,
                    first_collected_at,last_seen_at,last_synced_at
                ) VALUES ('2026R1','XYZ','bill','1','XYZ1','XYZ 1','House',?,?,?)
                """,
                (T1, T1, T1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO bills(
                    session_key,measure_prefix,measure_type,measure_number,
                    bill_id_compact,bill_id_display,bill_chamber,
                    first_collected_at,last_seen_at,last_synced_at
                ) VALUES ('2026R1','HJR','bill','999','HJR999','HJR 999',
                          'House',?,?,?)
                """,
                (T1, T1, T1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO bills(
                    session_key,measure_prefix,measure_type,measure_number,
                    bill_id_compact,bill_id_display,bill_chamber,
                    first_collected_at,last_seen_at,last_synced_at
                ) VALUES ('2026R1','HJR','joint_resolution','998','HJR998',
                          'HJR 998','Senate',?,?,?)
                """,
                (T1, T1, T1),
            )


def test_historical_run_scope_is_frozen_with_session_items_and_cutoff(tmp_path: Path):
    storage = _storage(tmp_path)
    for key in ("2014R1", "2016S1"):
        _seed_session(storage, key)
    runs = RunStore(storage.database)
    selected = ["2014r1", "2016S1", "2014R1"]
    inventory_id = runs.create_run(
        "inventory_backfill",
        session_keys=selected,
        probe_remote_sizes=True,
        queued_at=T1,
    )
    selected.append("2026R1")
    inventory = runs.get_run(inventory_id)
    scope = json.loads(inventory["requested_scope_json"])
    assert scope == {
        "probe_remote_sizes": True,
        "session_keys": ["2014R1", "2016S1"],
    }
    assert inventory["sessions_total"] == 2
    assert [item["session_key"] for item in runs.run_items(inventory_id)] == [
        "2014R1",
        "2016S1",
    ]

    download_id = storage.create_run(
        "download_archive",
        session_keys=["2014R1"],
        scope_cutoff_at=T2,
        requested_scope={"document_kinds": ["public_testimony"]},
        queued_at=T1,
    )
    download = storage.get_run(download_id)
    assert download["scope_cutoff_at"] == "2026-02-03T04:05:06.000000Z"
    assert json.loads(download["requested_scope_json"])["scope_cutoff_at"] == download[
        "scope_cutoff_at"
    ]
    assert storage.list_run_items(download_id)[0]["item_type"] == "session"
    with pytest.raises(ValueError, match="at least one selected session"):
        runs.create_run("inventory_backfill", session_keys=[])


def test_stage_upsert_without_session_key_preserves_historical_session_item(
    tmp_path: Path,
):
    storage = _storage(tmp_path)
    _seed_session(storage, "2026R1")
    runs = RunStore(storage.database)
    run_id = runs.create_run("inventory_backfill", session_keys=["2026R1"])
    assert runs.claim_run(run_id)

    runs.begin_stage(
        run_id,
        "sync_session",
        "Synchronizing official inventory for 2026R1",
        item_key="2026R1",
        item_type="session",
    )

    session_item = runs.run_items(run_id)[0]
    assert session_item["item_type"] == "session"
    assert session_item["session_key"] == "2026R1"


def test_begin_stage_cannot_restore_running_item_after_run_interruption(tmp_path: Path):
    storage = _storage(tmp_path)
    runs = RunStore(storage.database)
    run_id = runs.create_run(
        "collect_bill",
        session_key="2026R1",
        bill_id_compact="SB1501",
    )
    assert runs.claim_run(run_id)
    storage.normalize_interrupted_work(interrupted_at=T2)

    with pytest.raises(RuntimeError, match="interrupted, not running"):
        runs.begin_stage(run_id, "download_documents", "Downloading")

    assert runs.get_run(run_id)["status"] == "interrupted"
    assert runs.run_items(run_id) == []


def test_source_error_resolution_requires_an_exact_entity_or_bill_identity(
    tmp_path: Path,
):
    storage = _storage(tmp_path)
    runs = RunStore(storage.database)
    run_id = runs.create_run("inventory_backfill", session_keys=["2026R1"])

    matching_entity = runs.record_error(
        run_id,
        stage="sync_measures",
        session_key="2026R1",
        source_entity_type="Measures",
        error=TimeoutError("measures request timed out"),
        retryable=True,
    )
    other_entity = runs.record_error(
        run_id,
        stage="sync_measures",
        session_key="2026R1",
        source_entity_type="Committees",
        error=TimeoutError("committee request timed out"),
        retryable=True,
    )
    row_level = runs.record_error(
        run_id,
        stage="sync_measures",
        session_key="2026R1",
        source_entity_type="Measures",
        source_id="123",
        error="bad row",
        retryable=True,
    )
    matching_page = runs.record_error(
        run_id,
        stage="reconcile_olis_display",
        session_key="2026R1",
        bill_id_compact="SB1501",
        error=TimeoutError("OLIS page timed out"),
        retryable=True,
    )
    page_anomaly = runs.record_error(
        run_id,
        stage="reconcile_olis_display",
        session_key="2026R1",
        bill_id_compact="SB1501",
        error="OLIS page timed out",
        retryable=True,
    )
    other_bill = runs.record_error(
        run_id,
        stage="reconcile_olis_display",
        session_key="2026R1",
        bill_id_compact="HB1001",
        error=TimeoutError("another OLIS page timed out"),
        retryable=True,
    )

    assert runs.resolve_source_errors(
        run_id,
        stage="sync_measures",
        session_key="2026r1",
        source_entity_type="Measures",
    ) == 1
    assert runs.resolve_source_errors(
        run_id,
        stage="reconcile_olis_display",
        session_key="2026R1",
        bill_id_compact="SB 1501",
    ) == 2

    errors = {int(row["id"]): row for row in runs.errors(run_id)}
    assert errors[matching_entity]["resolved_at"] is not None
    assert errors[matching_page]["resolved_at"] is not None
    assert errors[page_anomaly]["resolved_at"] is not None
    assert errors[other_entity]["resolved_at"] is None
    assert errors[row_level]["resolved_at"] is None
    assert errors[other_bill]["resolved_at"] is None
    assert runs.get_run(run_id)["error_count"] == 3

    with pytest.raises(ValueError, match="exactly one"):
        runs.resolve_source_errors(
            run_id,
            stage="sync_measures",
            session_key="2026R1",
        )
    with pytest.raises(ValueError, match="exactly one"):
        runs.resolve_source_errors(
            run_id,
            stage="sync_measures",
            session_key="2026R1",
            source_entity_type="Measures",
            bill_id_compact="SB1501",
        )


def test_failed_source_sync_never_advances_successful_watermark(tmp_path: Path):
    storage = _storage(tmp_path)
    _seed_session(storage)
    run_id = storage.create_run("collect_session", session_key="2026R1", queued_at=T1)
    first = storage.record_source_sync_success(
        "2026R1",
        "Measures",
        strategy="watermark",
        run_id=run_id,
        source_count=12,
        source_watermark="2026-02-01T00:00:00",
        full_session=True,
        completed_at=T1,
    )
    assert first["source_watermark"] == "2026-02-01T00:00:00"
    failed = storage.record_source_sync_failure(
        "2026R1",
        "Measures",
        strategy="watermark",
        error=TimeoutError("source timed out"),
        failed_at=T2,
    )
    assert failed["source_watermark"] == "2026-02-01T00:00:00"
    assert failed["last_successful_sync_at"] == first["last_successful_sync_at"]
    assert failed["is_incomplete"] == 1
    final = storage.record_source_sync_success(
        "2026R1",
        "Measures",
        strategy="watermark",
        run_id=run_id,
        source_count=2,
        source_watermark="2026-02-03T00:00:00",
        incremental=True,
        completed_at=T3,
    )
    assert final["source_watermark"] == "2026-02-03T00:00:00"
    assert final["is_incomplete"] == 0
    assert final["last_full_session_sync_at"] == first["last_full_session_sync_at"]
    storage.mark_session_inventory_started("2026R1", run_id, started_at=T1)
    storage.finish_session_inventory(
        "2026R1", run_id, "inventory_complete", completed_at=T2
    )
    storage.record_session_download_activity("2026R1", run_id, changed_at=T2)
    storage.record_session_download_activity(
        "2026R1", run_id, completed=True, changed_at=T3
    )
    session_state = storage.get_session_archive_state("2026R1")
    assert session_state["last_successful_inventory_run_id"] == run_id
    assert session_state["last_download_started_at"] == "2026-02-03T04:05:06.000000Z"
    assert session_state["last_download_completed_at"] == "2026-03-04T05:06:07.000000Z"


def test_presence_reconciliation_is_authoritative_and_type_drift_is_durable(tmp_path: Path):
    storage = _storage(tmp_path)
    _seed_session(storage)
    first_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T1)
    bill_id = _seed_bill(storage, run_id=first_run)
    retained = _seed_document(storage, bill_id, "1", run_id=first_run)
    missing_then_returned = _seed_document(storage, bill_id, "2", run_id=first_run)
    second_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T2)
    page_only = storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "olis_display",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "3",
            "source_presence": "unknown",
            "reconciliation_origin": "olis_only",
        },
        seen_at=T2,
        run_id=second_run,
    )
    storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "unknown",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "1",
            "raw_document_type": "New official type",
            "canonical_download_url": "https://olis.oregonlegislature.gov/doc/1",
        },
        seen_at=T2,
        run_id=second_run,
    )
    storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "committee_document_other",
            "source_section": "odata_committee_meeting_document",
            "source_entity_type": "CommitteeMeetingDocument",
            "source_id": "future-type",
            "raw_document_type": "Future Official Type",
            "classification_method": "unrecognized_raw_document_type",
            "download_status": "not_applicable",
        },
        seen_at=T2,
        run_id=second_run,
    )

    unchanged = storage.reconcile_source_presence(
        "document",
        "2026R1",
        second_run,
        source_entity_type="CommitteePublicTestimony",
        authoritative_complete=False,
        reconciled_at=T2,
    )
    assert unchanged["records_reconciled"] == 0
    assert storage.get_document(missing_then_returned)["source_presence"] == "active"
    result = storage.reconcile_source_presence(
        "document",
        "2026R1",
        second_run,
        source_entity_type="CommitteePublicTestimony",
        authoritative_complete=True,
        reconciled_at=T2,
    )
    assert result == {
        "records_reconciled": 2,
        "active": 1,
        "missing": 1,
        "marked_missing": 1,
        "restored_active": 0,
    }
    assert storage.get_document(missing_then_returned)["source_presence"] == "missing"
    assert storage.get_document(retained)["source_presence"] == "active"
    assert storage.get_document(page_only)["source_presence"] == "unknown"
    anomaly_types = {row["anomaly_type"] for row in storage.list_source_anomalies()}
    assert {
        "raw_document_type_changed",
        "normalized_document_kind_changed",
        "unknown_document_type",
    } <= anomaly_types

    third_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T3)
    _seed_document(storage, bill_id, "2", seen_at=T3, run_id=third_run)
    assert storage.get_document(missing_then_returned)["source_presence"] == "active"
    with storage.database.connection() as connection:
        transitions = connection.execute(
            """
            SELECT previous_presence,new_presence FROM source_presence_events
            WHERE document_id=? ORDER BY id
            """,
            (missing_then_returned,),
        ).fetchall()
    assert [tuple(row) for row in transitions] == [
        ("active", "missing"),
        ("missing", "active"),
    ]


def test_presence_reconciliation_uses_bounded_set_based_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    storage = _storage(tmp_path)
    first_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T1)
    retained_bill = _seed_bill(storage, compact="SB1501", run_id=first_run)
    _seed_document(storage, retained_bill, "1", run_id=first_run)
    missing_count = 12
    for offset in range(missing_count):
        _seed_bill(storage, compact=f"HB{1001 + offset}", run_id=first_run)
        _seed_document(storage, retained_bill, str(2 + offset), run_id=first_run)

    second_run = storage.create_run("collect_session", session_key="2026R1", queued_at=T2)
    _seed_bill(storage, compact="SB1501", run_id=second_run)
    _seed_document(storage, retained_bill, "1", seen_at=T2, run_id=second_run)

    native_transaction = storage.database.transaction
    statements: list[str] = []

    class GuardedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            raise AssertionError("presence reconciliation must not materialize all rows")

        def fetchmany(self, *_args, **_kwargs):
            raise AssertionError("presence reconciliation must be set based")

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class GuardedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args, **kwargs):
            statements.append(" ".join(str(args[0]).split()).casefold())
            return GuardedCursor(self.connection.execute(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def guarded_transaction(*args, **kwargs):
        with native_transaction(*args, **kwargs) as connection:
            yield GuardedConnection(connection)

    monkeypatch.setattr(storage.database, "transaction", guarded_transaction)

    bill_result = storage.reconcile_source_presence(
        "bill", "2026R1", second_run, authoritative_complete=True, reconciled_at=T2
    )
    document_result = storage.reconcile_source_presence(
        "document",
        "2026R1",
        second_run,
        source_entity_type="CommitteePublicTestimony",
        authoritative_complete=True,
        reconciled_at=T2,
    )

    assert bill_result == {
        "records_reconciled": missing_count + 1,
        "active": 1,
        "missing": missing_count,
        "marked_missing": missing_count,
        "restored_active": 0,
    }
    assert document_result == bill_result
    event_inserts = [
        statement
        for statement in statements
        if statement.startswith("insert into source_presence_events")
    ]
    assert len(statements) == 10
    assert len(event_inserts) == 2
    assert all(" select " in f" {statement} " for statement in event_inserts)
    with storage.database.connection() as connection:
        event_groups = connection.execute(
            """
            SELECT entity_type,previous_presence,new_presence,details_json,COUNT(*) AS total
            FROM source_presence_events
            GROUP BY entity_type,previous_presence,new_presence,details_json
            ORDER BY entity_type
            """
        ).fetchall()
    assert [
        (
            row["entity_type"],
            row["previous_presence"],
            row["new_presence"],
            json.loads(row["details_json"]),
            row["total"],
        )
        for row in event_groups
    ] == [
        ("bill", "active", "missing", {"authoritative_full_session": True}, missing_count),
        (
            "document",
            "active",
            "missing",
            {"authoritative_full_session": True},
            missing_count,
        ),
    ]


def test_display_reconciliation_only_writes_negative_flags_after_success(tmp_path: Path):
    storage = _storage(tmp_path)
    bill_id = _seed_bill(storage)
    shown = _seed_document(storage, bill_id, "10")
    hidden = _seed_document(storage, bill_id, "11")
    presentation = storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "committee_presentation",
            "source_section": "presentations",
            "source_entity_type": "CommitteeMeetingDocument",
            "source_id": "20",
            "raw_document_type": "Presentation",
            "canonical_download_url": (
                "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
                "CommitteeMeetingDocument/20"
            ),
        },
        seen_at=T1,
    )
    row = storage.record_olis_display_reconciliation(
        bill_id,
        "checked_with_records",
        source_entity_type="CommitteePublicTestimony",
        displayed_source_ids=["10"],
        checked_at=T1,
        odata_record_count=2,
        displayed_record_count=1,
        odata_only_count=1,
    )
    assert row["status"] == "checked_with_records"
    assert row["source_entity_type"] == "CommitteePublicTestimony"
    assert storage.get_document(shown)["displayed_in_olis"] == 1
    assert storage.get_document(hidden)["displayed_in_olis"] == 0

    presentation_row = storage.record_olis_display_reconciliation(
        bill_id,
        "checked_with_records",
        source_entity_type="CommitteeMeetingDocument",
        displayed_source_ids=["20"],
        checked_at=T1,
        odata_record_count=1,
        displayed_record_count=1,
    )
    assert presentation_row["source_entity_type"] == "CommitteeMeetingDocument"
    assert storage.get_document(presentation)["displayed_in_olis"] == 1
    assert len(storage.list_olis_display_reconciliations(bill_id)) == 2
    assert storage.get_olis_display_reconciliation(
        bill_id, "CommitteeMeetingDocument"
    )["displayed_record_count"] == 1

    storage.record_olis_display_reconciliation(
        bill_id,
        "failed_fetch",
        source_entity_type="CommitteePublicTestimony",
        displayed_source_ids=[],
        checked_at=T2,
    )
    assert storage.get_document(shown)["displayed_in_olis"] == 1
    assert storage.get_document(hidden)["displayed_in_olis"] == 0
    assert storage.get_document(presentation)["displayed_in_olis"] == 1

    storage.record_olis_display_reconciliation(
        bill_id,
        "parser_anomalous",
        source_entity_type="CommitteePublicTestimony",
        checked_at=T3,
    )
    storage.record_source_anomaly(
        "olis_display_parser_anomaly",
        severity="error",
        affects_completeness=True,
        message="Recognizable OLIS document markup could not be parsed completely.",
        session_key="2026R1",
        bill_id=bill_id,
        observed_at=T3,
    )
    state = storage.get_session_archive_state("2026R1")
    assert state["material_anomaly_count"] == 1
    assert storage.resolve_source_anomalies_for_bill(
        bill_id,
        anomaly_types=["olis_display_fetch_failed", "olis_display_parser_anomaly"],
        resolved_at=T3,
    ) == 1
    assert storage.resolve_source_anomalies_for_bill(
        bill_id,
        anomaly_types=["olis_display_parser_anomaly"],
        resolved_at=T3,
    ) == 0
    assert storage.get_session_archive_state("2026R1")["material_anomaly_count"] == 0


def test_remote_probe_keeps_latest_metadata(tmp_path: Path):
    storage = _storage(tmp_path)
    document_id = _seed_document(storage, _seed_bill(storage), "20")
    storage.record_document_probe(
        document_id,
        status="known_size",
        probed_at=T1,
        http_status=200,
        final_url="https://olis.oregonlegislature.gov/doc/20",
        content_type="application/pdf",
        content_length=1234,
        etag='"abc"',
    )
    probe = storage.get_document_probe(document_id)
    assert probe["content_length"] == 1234
    assert probe["etag"] == '"abc"'
    with pytest.raises(ValueError, match="must not be negative"):
        storage.record_document_probe(document_id, status="known_size", content_length=-1)


def test_archive_claims_are_bounded_atomic_paused_and_resumable(tmp_path: Path):
    storage = _storage(tmp_path)
    bill_id = _seed_bill(storage)
    eligible = _seed_document(storage, bill_id, "30", seen_at=T1)
    _seed_document(storage, bill_id, "31", seen_at=T3)
    runs = RunStore(storage.database)
    run_id = runs.create_run(
        "download_archive",
        session_keys=["2026R1"],
        scope={"document_kinds": ["public_testimony"]},
        scope_cutoff_at=T2,
        queued_at=T1,
    )
    competing_id = runs.create_run(
        "download_archive",
        session_keys=["2026R1"],
        scope_cutoff_at=T2,
        queued_at=T1,
    )
    assert runs.claim_run(run_id)
    assert not runs.claim_run(competing_id)
    assert runs.get_run(competing_id)["status"] == "queued"
    claimed = storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert claimed["id"] == eligible
    assert claimed["run_item_id"]
    assert storage.claim_next_archive_document(competing_id, attempted_at=T2) is None
    assert runs.pause(run_id, "operator pause")
    assert storage.claim_next_archive_document(run_id, attempted_at=T2) is None
    assert storage.release_archive_document_claim(
        run_id,
        eligible,
        document_status="interrupted",
        item_status="interrupted",
        message="paused after the active stream stopped",
        changed_at=T2,
    )
    assert runs.requeue(run_id)
    assert not runs.requeue(run_id)
    assert runs.claim_run(run_id)
    resumed = storage.claim_next_archive_document(run_id, attempted_at=T3)
    assert resumed["id"] == eligible
    assert resumed["run_item_id"] == claimed["run_item_id"]
    document_items = [
        row for row in runs.run_items(run_id) if row["item_type"] == "document"
    ]
    assert len(document_items) == 1
