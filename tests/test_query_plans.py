from __future__ import annotations

from pathlib import Path

from olis_archive.database import Database


def _plan(database: Database, sql: str, params: tuple[object, ...]) -> str:
    with database.connection() as connection:
        rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return "\n".join(str(row[3]) for row in rows)


def test_browse_documents_production_query_uses_session_pagination_index(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "legiview.sqlite3")
    database.initialize()

    # This is the SELECT shape emitted by ArchiveQueries.documents, including
    # its joins, filters, ordering, and pagination.
    plan = _plan(
        database,
        """
        SELECT d.*, b.bill_title, b.bill_id_display,
               p.probe_status, p.probed_at, p.content_length AS probed_bytes,
               p.final_url AS probe_final_url
        FROM documents d
        JOIN bills b ON b.id=d.bill_id
        LEFT JOIN document_remote_probes p ON p.document_id=d.id
        WHERE d.session_key=? AND d.document_kind=? AND d.source_presence=?
        ORDER BY d.session_key DESC, d.bill_id_compact,
                 d.document_kind, COALESCE(d.meeting_date, d.letter_date, ''), d.id
        LIMIT ? OFFSET ?
        """,
        ("2026R1", "public_testimony", "active", 50, 0),
    )

    assert "idx_documents_bill_compact_page" in plan
    assert "SCAN d" not in plan


def test_operations_production_query_uses_an_error_filter_index(tmp_path: Path) -> None:
    database = Database(tmp_path / "legiview.sqlite3")
    database.initialize()

    # stage_or_entity is a contains filter in the real Operations read model;
    # an exact stage predicate here would test a query the application never
    # issues for that control.
    plan = _plan(
        database,
        """
        SELECT e.*, d.document_kind, d.http_status
        FROM collection_errors e
        LEFT JOIN documents d ON d.id=e.document_id
        WHERE e.resolved_at IS NULL
          AND e.session_key=?
          AND (e.stage LIKE ? OR e.source_entity_type LIKE ?)
          AND e.retryable=?
        ORDER BY e.last_occurred_at DESC, e.id DESC
        LIMIT ? OFFSET ?
        """,
        ("2017R1", "%reconcile_olis_display%", "%reconcile_olis_display%", 1, 50, 0),
    )

    assert (
        "idx_collection_errors_review" in plan
        or "idx_collection_errors_retryable" in plan
    )
    assert "SCAN e" not in plan


def test_archive_new_claim_production_query_uses_keyset_and_item_indexes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "legiview.sqlite3")
    database.initialize()

    # This mirrors the previously-unclaimed branch of
    # StorageService.claim_next_archive_document. All statuses are real
    # documents.download_status values.
    plan = _plan(
        database,
        """
        SELECT d.*,NULL AS existing_run_item_id,
               NULL AS existing_run_item_details_json
        FROM documents d
        WHERE d.session_key=? AND d.id>?
          AND d.canonical_download_url IS NOT NULL
          AND trim(d.canonical_download_url)<>''
          AND d.download_status IN (
              'changed_remote','discovered','failed_retryable','failed_terminal',
              'interrupted','missing_local','paused_low_space','queued'
          )
          AND d.source_presence IN (?,?)
          AND d.download_status IN (?,?,?)
          AND d.first_seen_at<=?
          AND d.document_kind IN (?,?)
          AND NOT EXISTS (
              SELECT 1 FROM collection_run_items existing
              WHERE existing.run_id=? AND existing.item_type='document'
                AND existing.document_id=d.id
          )
        ORDER BY d.id
        LIMIT 1
        """,
        (
            "2014R1",
            101,
            "active",
            "unknown",
            "changed_remote",
            "discovered",
            "failed_retryable",
            "2026-09-04T00:00:00Z",
            "public_testimony",
            "committee_presentation",
            42,
        ),
    )

    assert "idx_documents_archive_walk (session_key=? AND id>?)" in plan
    assert (
        "idx_collection_run_items_archive_document "
        "(run_id=? AND item_type=? AND document_id=?)"
    ) in plan
    assert "SCAN d" not in plan
    assert "SCAN existing" not in plan
    assert "USE TEMP B-TREE" not in plan


def test_archive_resumed_claim_production_query_uses_queued_item_index(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "legiview.sqlite3")
    database.initialize()

    plan = _plan(
        database,
        """
        SELECT d.*,i.id AS existing_run_item_id,
               i.document_id AS queued_document_id,
               i.details_json AS existing_run_item_details_json
        FROM collection_run_items i
        LEFT JOIN documents d ON d.id=i.document_id
        WHERE i.run_id=? AND i.item_type='document' AND i.status='queued'
        ORDER BY i.document_id,i.id
        LIMIT 1
        """,
        (42,),
    )

    assert (
        "idx_collection_run_items_claim (run_id=? AND item_type=? AND status=?)"
    ) in plan
    assert "SCAN i" not in plan
    assert "USE TEMP B-TREE" not in plan


def test_archive_open_cursor_query_uses_durable_ordinal_index(tmp_path: Path) -> None:
    database = Database(tmp_path / "legiview.sqlite3")
    database.initialize()

    plan = _plan(
        database,
        """
        SELECT session_ordinal,session_key,after_document_id
        FROM archive_claim_cursors
        WHERE run_id=? AND exhausted=0
        ORDER BY session_ordinal
        LIMIT 1
        """,
        (42,),
    )

    assert "idx_archive_claim_cursors_open (run_id=? AND exhausted=?)" in plan
    assert "SCAN archive_claim_cursors" not in plan
    assert "USE TEMP B-TREE" not in plan
