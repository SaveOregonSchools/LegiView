"""Bounded read models for the Phase 2 archive and operations UI.

The collector owns writes.  This module keeps Flask routes thin and makes every
historical-scale list use SQL filtering, counting, and pagination instead of
materializing the archive in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..database import Database
from .storage import MATCHING_RETRY_DOWNLOAD_STATUSES, RETRY_PAYLOAD_DOCUMENT_KINDS


HISTORICAL_BOUNDARY_KEY = "2014R1"
RETRYABLE_STATUSES = tuple(
    sorted(MATCHING_RETRY_DOWNLOAD_STATUSES - {"failed_terminal"})
)
@dataclass(frozen=True, slots=True)
class QueryPage:
    """One exact SQL page and its filtered total."""

    rows: tuple[dict[str, Any], ...]
    total: int

class ArchiveQueries:
    """Read-only database queries shared by HTML pages and CSV exports."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def bills(
        self,
        *,
        session_key: str | None = None,
        chamber: str | None = None,
        query: str | None = None,
        enacted: bool | None = None,
        sponsor: str | None = None,
        sort: str = "bill",
        descending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        sort_columns = {
            "bill": (
                "b.session_key DESC, b.measure_prefix, "
                "CAST(b.measure_number AS INTEGER), b.measure_number"
            ),
            "title": "COALESCE(b.bill_title, '')",
            "chapter": "COALESCE(b.chapter_number, '')",
            "last_synced": "b.last_synced_at",
            "documents": "document_count",
        }
        if sort not in sort_columns:
            raise ValueError(f"unsupported bill sort: {sort!r}")
        where: list[str] = []
        params: list[Any] = []
        if session_key:
            where.append("b.session_key = ?")
            params.append(session_key.strip().upper())
        if chamber:
            where.append("b.bill_chamber = ?")
            params.append(chamber)
        if query:
            needle = f"%{query.strip()}%"
            where.append(
                "(b.bill_id_compact LIKE ? OR b.bill_title LIKE ? "
                "OR b.catchline LIKE ? OR b.at_the_request_of LIKE ?)"
            )
            params.extend((needle, needle, needle, needle))
        if enacted is True:
            where.append("(b.enacted = 1 OR NULLIF(trim(b.chapter_number), '') IS NOT NULL)")
        elif enacted is False:
            where.append(
                "COALESCE(b.enacted, 0) = 0 "
                "AND NULLIF(trim(b.chapter_number), '') IS NULL"
            )
        if sponsor:
            where.append(
                "EXISTS (SELECT 1 FROM bill_sponsors sf WHERE sf.bill_id=b.id "
                "AND (sf.resolved_display_name LIKE ? OR sf.legislator_code LIKE ? "
                "OR sf.committee_code LIKE ?))"
            )
            needle = f"%{sponsor.strip()}%"
            params.extend((needle, needle, needle))

        where_sql = " WHERE " + " AND ".join(where) if where else ""
        direction = "DESC" if descending else "ASC"
        select_sql = f"""
            SELECT b.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.bill_id=b.id) AS document_count,
                   (SELECT GROUP_CONCAT(resolved_display_name, ', ')
                    FROM (
                        SELECT resolved_display_name
                        FROM bill_sponsors bs
                        WHERE bs.bill_id=b.id
                          AND NULLIF(trim(bs.resolved_display_name), '') IS NOT NULL
                        ORDER BY COALESCE(bs.print_order, 2147483647), bs.id
                    )) AS sponsor_summary
            FROM bills b
            {where_sql}
            ORDER BY {sort_columns[sort]} {direction}, b.id {direction}
            LIMIT ? OFFSET ?
        """
        count_sql = f"SELECT COUNT(*) FROM bills b {where_sql}"
        return self._page(select_sql, count_sql, params, limit=limit, offset=offset)

    def documents(
        self,
        *,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        document_kind: str | None = None,
        committee: str | None = None,
        submitter: str | None = None,
        organization: str | None = None,
        testimony_position: str | None = None,
        download_status: str | None = None,
        source_presence: str | None = None,
        displayed_in_olis: bool | None | str = "any",
        failed_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        where, params = self._document_filters(
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            document_kind=document_kind,
            committee=committee,
            submitter=submitter,
            organization=organization,
            testimony_position=testimony_position,
            download_status=download_status,
            source_presence=source_presence,
            displayed_in_olis=displayed_in_olis,
            failed_only=failed_only,
        )
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        select_sql = f"""
            SELECT d.*, b.bill_title, b.bill_id_display,
                   p.probe_status, p.probed_at, p.content_length AS probed_bytes,
                   p.final_url AS probe_final_url
            FROM documents d
            JOIN bills b ON b.id=d.bill_id
            LEFT JOIN document_remote_probes p ON p.document_id=d.id
            {where_sql}
            ORDER BY d.session_key DESC, d.bill_id_compact,
                     d.document_kind, COALESCE(d.meeting_date, d.letter_date, ''), d.id
            LIMIT ? OFFSET ?
        """
        count_sql = f"""
            SELECT COUNT(*)
            FROM documents d
            JOIN bills b ON b.id=d.bill_id
            {where_sql}
        """
        return self._page(select_sql, count_sql, params, limit=limit, offset=offset)

    def runs(
        self,
        *,
        run_type: str | None = None,
        status: str | None = None,
        scope: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        where: list[str] = []
        params: list[Any] = []
        if run_type:
            where.append("run_type=?")
            params.append(run_type)
        if status:
            where.append("status=?")
            params.append(status)
        if scope:
            needle = f"%{scope.strip()}%"
            where.append(
                "(requested_session_key LIKE ? OR requested_bill_id_compact LIKE ? "
                "OR requested_scope_json LIKE ? OR CAST(id AS TEXT)=?)"
            )
            params.extend((needle, needle, needle, scope.strip().lstrip("#")))
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        return self._page(
            f"SELECT * FROM collection_runs {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            f"SELECT COUNT(*) FROM collection_runs {where_sql}",
            params,
            limit=limit,
            offset=offset,
        )

    def run_stages(self, run_id: int) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM collection_run_items
            WHERE run_id=? AND item_type='stage'
            ORDER BY id
            """,
            (run_id,),
        )

    def download_run_counts(self, run_id: int) -> dict[str, int]:
        """Read live outcomes from the ledger, including runs from older workers.

        Run headers only receive document totals when the download loop ends.
        Grouping durable items also avoids double-counting attempts after resume.
        """
        rows = self._all(
            """
            SELECT status, COUNT(*) AS count FROM collection_run_items
            WHERE run_id=? AND item_type='document'
            GROUP BY status
            """,
            (run_id,),
        )
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "documents_recorded": sum(counts.values()),
            "documents_downloaded": counts.get("completed", 0),
            "documents_skipped": counts.get("skipped", 0),
            "documents_failed": counts.get("failed_retryable", 0)
            + counts.get("failed_terminal", 0),
        }

    def run_items(
        self, run_id: int, *, limit: int = 50, offset: int = 0
    ) -> QueryPage:
        select_sql = """
            SELECT i.*, d.title AS document_title, d.source_id AS source_document_id,
                   d.bill_id_compact AS document_bill_id_compact,
                   b.bill_id_display
            FROM collection_run_items i
            LEFT JOIN documents d ON d.id=i.document_id
            LEFT JOIN bills b ON b.id=COALESCE(i.bill_id, d.bill_id)
            WHERE i.run_id=? AND i.item_type!='stage'
            ORDER BY i.id
            LIMIT ? OFFSET ?
        """
        return self._page(
            select_sql,
            "SELECT COUNT(*) FROM collection_run_items WHERE run_id=? AND item_type!='stage'",
            [run_id],
            limit=limit,
            offset=offset,
        )

    def run_errors(
        self, run_id: int, *, limit: int = 50, offset: int = 0
    ) -> QueryPage:
        return self._page(
            """
            SELECT e.*, d.document_kind, d.http_status
            FROM collection_errors e
            LEFT JOIN documents d ON d.id=e.document_id
            WHERE e.run_id=?
            ORDER BY e.last_occurred_at DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            "SELECT COUNT(*) FROM collection_errors WHERE run_id=?",
            [run_id],
            limit=limit,
            offset=offset,
        )

    def session_choices(
        self,
        *,
        inventoried_only: bool = False,
        include_unsupported: bool = False,
    ) -> list[dict[str, Any]]:
        condition = "AND a.inventory_status!='not_started'" if inventoried_only else ""
        support_condition = (
            ""
            if include_unsupported
            else "AND julianday(s.begin_date)>=julianday(boundary.begin_date)"
        )
        return self._all(
            f"""
            SELECT s.session_key, s.session_name, s.session_type, s.session_year,
                   s.begin_date, s.end_date,
                   CASE WHEN julianday(s.begin_date)>=julianday(boundary.begin_date)
                        THEN 1 ELSE 0 END
                       AS supported,
                   CASE WHEN julianday(s.begin_date)<julianday(boundary.begin_date)
                        THEN 'Predates the validated ' || boundary.session_key ||
                             ' support boundary'
                        ELSE NULL END AS support_reason,
                   COALESCE(a.inventory_status, 'not_started') AS inventory_status,
                   (SELECT r.finished_at FROM collection_runs r
                    WHERE r.id=a.last_successful_inventory_run_id)
                       AS last_successful_inventory_at,
                   COALESCE(a.last_download_completed_at, a.last_download_started_at)
                       AS last_download_activity_at
            FROM sessions s
            LEFT JOIN session_archive_state a ON a.session_key=s.session_key
            LEFT JOIN sessions boundary ON boundary.session_key=?
            WHERE julianday(boundary.begin_date) IS NOT NULL
              {support_condition}
              {condition}
            ORDER BY julianday(s.begin_date) DESC, s.session_key DESC
            """,
            (HISTORICAL_BOUNDARY_KEY,),
        )

    def session_state_map(self, session_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        keys = tuple(dict.fromkeys(str(value).strip().upper() for value in session_keys if value))
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self._all(
            f"""
            SELECT s.session_key, s.session_name, s.session_type, s.session_year,
                   s.begin_date, s.end_date,
                   COALESCE(a.inventory_status, 'not_started') AS inventory_status,
                   a.last_inventory_completed_at, a.last_download_completed_at,
                   a.source_anomaly_count, a.material_anomaly_count
            FROM sessions s
            LEFT JOIN session_archive_state a ON a.session_key=s.session_key
            WHERE s.session_key IN ({placeholders})
            """,
            keys,
        )
        return {str(row["session_key"]): row for row in rows}

    def session_status(
        self,
        *,
        session_key: str | None = None,
        inventory_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        # The historical boundary is authoritative.  If its row or begin date
        # is missing, return no scope rather than silently widening to every
        # session in the database.
        where = ["boundary.begin_date IS NOT NULL", "s.begin_date>=boundary.begin_date"]
        params: list[Any] = [HISTORICAL_BOUNDARY_KEY]
        if session_key:
            where.append("s.session_key=?")
            params.append(session_key.strip().upper())
        if inventory_status:
            where.append("COALESCE(a.inventory_status, 'not_started')=?")
            params.append(inventory_status)
        where_sql = " WHERE " + " AND ".join(where)
        base_from = """
            FROM sessions s
            LEFT JOIN session_archive_state a ON a.session_key=s.session_key
            LEFT JOIN sessions boundary ON boundary.session_key=?
        """
        select_sql = f"""
            SELECT s.session_key, s.session_name, s.session_type, s.session_year,
                   s.begin_date, s.end_date,
                   COALESCE(a.inventory_status, 'not_started') AS inventory_status,
                   a.last_inventory_completed_at, a.last_download_completed_at,
                   a.last_successful_inventory_run_id, a.last_download_run_id,
                   (SELECT r.finished_at FROM collection_runs r
                    WHERE r.id=a.last_successful_inventory_run_id)
                       AS last_successful_inventory_at,
                   COALESCE(a.last_download_completed_at, a.last_download_started_at)
                       AS last_download_activity_at,
                   COALESCE(a.source_anomaly_count, 0) AS recorded_anomalies,
                   COALESCE(a.material_anomaly_count, 0) AS material_anomalies,
                   (SELECT COUNT(*) FROM bills b
                    WHERE b.session_key=s.session_key AND b.measure_prefix='HB') AS hb_count,
                   (SELECT COUNT(*) FROM bills b
                    WHERE b.session_key=s.session_key AND b.measure_prefix='SB') AS sb_count,
                   (SELECT COUNT(*) FROM bills b
                    WHERE b.session_key=s.session_key AND b.bill_chamber='House')
                       AS house_measure_count,
                   (SELECT COUNT(*) FROM bills b
                    WHERE b.session_key=s.session_key AND b.bill_chamber='Senate')
                       AS senate_measure_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key
                      AND d.document_kind='public_testimony') AS public_testimony_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key
                      AND d.document_kind IN ('legacy_testimony','committee_presentation'))
                       AS presentation_legacy_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key
                      AND d.document_kind='floor_letter') AS floor_letter_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key) AS document_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key
                      AND NULLIF(trim(d.canonical_download_url), '') IS NOT NULL
                      AND d.download_status!='not_applicable') AS downloadable_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key AND d.download_status='downloaded'
                      AND d.validation_status='valid' AND d.current_version_id IS NOT NULL)
                       AS downloaded_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key
                      AND d.download_status IN ('failed_retryable','failed_terminal'))
                   + (SELECT COUNT(*) FROM collection_errors e
                      WHERE e.session_key=s.session_key AND e.resolved_at IS NULL
                        AND e.document_id IS NULL)
                       AS failure_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key AND d.document_kind='unknown'
                      AND NOT EXISTS (
                          SELECT 1 FROM source_anomalies ux
                          WHERE ux.document_id=d.id AND ux.resolved_at IS NULL
                            AND ux.anomaly_type='unknown_document_type'
                      ))
                    + (SELECT COUNT(*) FROM source_anomalies x
                       WHERE x.session_key=s.session_key AND x.resolved_at IS NULL)
                       AS unknown_anomaly_count,
                   (SELECT COALESCE(SUM(p.content_length), 0)
                    FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing')
                       AS known_remote_bytes,
                   (SELECT COUNT(*)
                    FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing'
                      AND p.content_length IS NOT NULL) AS known_size_count,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing'
                      AND NULLIF(trim(d.canonical_download_url),'') IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM document_remote_probes p
                          WHERE p.document_id=d.id AND p.content_length IS NOT NULL
                      )) AS unknown_size_count,
                   (SELECT COALESCE(SUM(v.downloaded_bytes), 0)
                    FROM document_versions v JOIN documents d ON d.id=v.document_id
                    WHERE d.session_key=s.session_key AND v.status='downloaded') AS local_archive_bytes
            {base_from}
            {where_sql}
            ORDER BY COALESCE(s.begin_date, '') DESC, s.session_key DESC
            LIMIT ? OFFSET ?
        """
        count_sql = f"SELECT COUNT(*) {base_from} {where_sql}"
        return self._page(select_sql, count_sql, params, limit=limit, offset=offset)

    def operations(
        self,
        *,
        view: str,
        run_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        stage_or_entity: str | None = None,
        document_kind: str | None = None,
        retryable: bool | None = None,
        error_class: str | None = None,
        anomaly_type: str | None = None,
        severity: str | None = None,
        unresolved_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        if view == "errors":
            return self._operation_errors(
                run_id=run_id,
                session_key=session_key,
                bill_id_compact=bill_id_compact,
                stage_or_entity=stage_or_entity,
                document_kind=document_kind,
                retryable=retryable,
                error_class=error_class,
                unresolved_only=unresolved_only,
                limit=limit,
                offset=offset,
            )
        if view == "anomalies":
            return self._operation_anomalies(
                run_id=run_id,
                session_key=session_key,
                bill_id_compact=bill_id_compact,
                stage_or_entity=stage_or_entity,
                document_kind=document_kind,
                anomaly_type=anomaly_type,
                severity=severity,
                unresolved_only=unresolved_only,
                limit=limit,
                offset=offset,
            )
        raise ValueError("view must be 'errors' or 'anomalies'")

    def retry_documents(
        self,
        *,
        run_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        include_terminal: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> QueryPage:
        joins, where, params = self._retry_document_scope(
            run_id=run_id,
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            include_terminal=include_terminal,
        )
        where_sql = " WHERE " + " AND ".join(where)
        distinct = "DISTINCT " if run_id is not None else ""
        select_sql = f"""
            SELECT {distinct}d.*
            FROM documents d {joins}
            {where_sql}
            ORDER BY d.last_attempt_at DESC, d.id DESC
            LIMIT ? OFFSET ?
        """
        if run_id is None:
            count_sql = f"SELECT COUNT(*) FROM documents d {where_sql}"
        else:
            count_sql = f"SELECT COUNT(DISTINCT d.id) FROM documents d {joins} {where_sql}"
        return self._page(select_sql, count_sql, params, limit=limit, offset=offset)

    def dashboard_stats(self) -> dict[str, Any]:
        statements = {
            "sessions_in_scope": """
                SELECT COUNT(*) FROM sessions s
                LEFT JOIN sessions boundary ON boundary.session_key='2014R1'
                WHERE boundary.begin_date IS NOT NULL
                  AND s.begin_date>=boundary.begin_date
            """,
            "sessions_inventory_complete": """
                SELECT COUNT(*) FROM session_archive_state
                WHERE inventory_status IN ('inventory_complete','inventory_complete_with_errors')
            """,
            "bills": "SELECT COUNT(*) FROM bills",
            "documents_discovered": "SELECT COUNT(*) FROM documents",
            "public_testimony": (
                "SELECT COUNT(*) FROM documents WHERE document_kind='public_testimony'"
            ),
            "presentation_legacy": """
                SELECT COUNT(*) FROM documents
                WHERE document_kind IN ('legacy_testimony','committee_presentation')
            """,
            "floor_letters": "SELECT COUNT(*) FROM documents WHERE document_kind='floor_letter'",
            "documents_downloaded": """
                SELECT COUNT(*) FROM documents
                WHERE download_status='downloaded' AND validation_status='valid'
                  AND current_version_id IS NOT NULL
            """,
            "download_failures": """
                SELECT COUNT(*) FROM documents
                WHERE download_status IN ('failed_retryable','failed_terminal')
            """,
            "archive_bytes": """
                SELECT COALESCE(SUM(downloaded_bytes),0)
                FROM document_versions WHERE status='downloaded'
            """,
            "known_remote_bytes": """
                SELECT COALESCE(SUM(p.content_length),0)
                FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                WHERE d.source_presence!='missing' AND p.content_length IS NOT NULL
            """,
            "known_size_documents": """
                SELECT COUNT(*)
                FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                WHERE d.source_presence!='missing' AND p.content_length IS NOT NULL
            """,
            "unknown_size_documents": """
                SELECT COUNT(*) FROM documents d
                WHERE d.source_presence!='missing'
                  AND NULLIF(trim(d.canonical_download_url),'') IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM document_remote_probes p
                      WHERE p.document_id=d.id AND p.content_length IS NOT NULL
                  )
            """,
            "last_historical_inventory": """
                SELECT MAX(finished_at) FROM collection_runs
                WHERE run_type='inventory_backfill'
                  AND status IN ('completed','completed_with_errors')
            """,
        }
        with self.database.connection() as connection:
            return {
                key: connection.execute(sql).fetchone()[0]
                for key, sql in statements.items()
            }

    def run_anomaly_count(self, run_id: int) -> int:
        """Count unresolved anomaly records most recently observed by one run."""

        with self.database.connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_anomalies
                    WHERE last_run_id=? AND resolved_at IS NULL
                    """,
                    (int(run_id),),
                ).fetchone()[0]
            )

    def document_export_query(self, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
        where, params = self._document_filters(
            session_key=filters.get("session"),
            bill_id_compact=filters.get("bill"),
            document_kind=filters.get("kind"),
            committee=filters.get("committee"),
            submitter=filters.get("submitter"),
            organization=filters.get("organization"),
            testimony_position=filters.get("position"),
            download_status=filters.get("download_status"),
            source_presence=filters.get("source_presence"),
            displayed_in_olis=filters.get("displayed_in_olis", "any"),
            failed_only=bool(filters.get("failed_only")),
        )
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        sql = f"""
            SELECT d.session_key AS session, d.bill_id_compact AS bill,
                   d.document_kind AS kind, d.source_entity_type, d.source_id,
                   d.raw_document_type, d.title, d.submitter, d.on_behalf_of,
                   d.testimony_position AS position,
                   d.city_organization AS organization_city,
                   COALESCE(d.committee_name, d.committee_code) AS committee,
                   COALESCE(d.meeting_date, d.letter_date) AS meeting_or_letter_date,
                   d.source_url, d.canonical_download_url AS download_url,
                   CASE d.displayed_in_olis WHEN 1 THEN 'yes' WHEN 0 THEN 'no'
                        ELSE 'unknown' END AS displayed_in_olis,
                   d.source_presence, d.local_relative_path, d.download_status,
                   d.downloaded_bytes AS current_payload_bytes, d.sha256
            FROM documents d JOIN bills b ON b.id=d.bill_id
            {where_sql}
            ORDER BY d.session_key, d.bill_id_compact, d.document_kind, d.id
        """
        return sql, params

    def session_export_query(self) -> tuple[str, list[Any]]:
        # Reuse the status projection without LIMIT by maintaining this explicit
        # compact audit projection.  It remains one streaming SQL cursor.
        sql = """
            SELECT s.session_key AS session, s.session_name, s.begin_date, s.end_date,
                   COALESCE(a.inventory_status,'not_started') AS inventory_status,
                   (SELECT COUNT(*) FROM bills b WHERE b.session_key=s.session_key
                       AND b.measure_prefix='HB') AS hb_count,
                   (SELECT COUNT(*) FROM bills b WHERE b.session_key=s.session_key
                       AND b.measure_prefix='SB') AS sb_count,
                   (SELECT COUNT(*) FROM bills b WHERE b.session_key=s.session_key
                       AND b.bill_chamber='House') AS house_measure_count,
                   (SELECT COUNT(*) FROM bills b WHERE b.session_key=s.session_key
                       AND b.bill_chamber='Senate') AS senate_measure_count,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key)
                       AS documents,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key
                       AND d.document_kind='public_testimony') AS public_testimony,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key
                       AND d.document_kind IN ('legacy_testimony','committee_presentation'))
                       AS presentation_legacy,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key
                       AND d.document_kind='floor_letter') AS floor_letters,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key
                       AND d.download_status='downloaded' AND d.validation_status='valid')
                       AS downloaded_current_payloads,
                   (SELECT COUNT(*) FROM documents d WHERE d.session_key=s.session_key
                       AND d.download_status IN ('failed_retryable','failed_terminal'))
                       AS failures,
                   COALESCE(a.source_anomaly_count,0) AS anomalies,
                   (SELECT COUNT(*)
                    FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing'
                      AND p.content_length IS NOT NULL) AS known_size_documents,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing'
                      AND NULLIF(trim(d.canonical_download_url),'') IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM document_remote_probes p
                          WHERE p.document_id=d.id AND p.content_length IS NOT NULL
                      )) AS unknown_size_documents,
                   (SELECT COALESCE(SUM(p.content_length),0)
                    FROM document_remote_probes p JOIN documents d ON d.id=p.document_id
                    WHERE d.session_key=s.session_key AND d.source_presence!='missing')
                       AS known_remote_bytes,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM documents d
                       WHERE d.session_key=s.session_key AND d.source_presence!='missing'
                         AND NULLIF(trim(d.canonical_download_url),'') IS NOT NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM document_remote_probes p
                             WHERE p.document_id=d.id AND p.content_length IS NOT NULL
                         )
                   ) THEN 1 ELSE 0 END AS known_remote_bytes_is_lower_bound,
                   (SELECT COALESCE(SUM(v.downloaded_bytes),0)
                    FROM document_versions v JOIN documents d ON d.id=v.document_id
                    WHERE d.session_key=s.session_key AND v.status='downloaded')
                       AS local_archive_bytes,
                   a.last_inventory_completed_at, a.last_download_completed_at
            FROM sessions s
            LEFT JOIN session_archive_state a ON a.session_key=s.session_key
            LEFT JOIN sessions boundary ON boundary.session_key='2014R1'
            WHERE boundary.begin_date IS NOT NULL
              AND s.begin_date>=boundary.begin_date
            ORDER BY COALESCE(s.begin_date,''), s.session_key
        """
        return sql, []

    def operations_export_query(
        self,
        *,
        view: str = "errors",
        run_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        stage_or_entity: str | None = None,
        document_kind: str | None = None,
        retryable: bool | None = None,
        error_class: str | None = None,
        anomaly_type: str | None = None,
        severity: str | None = None,
        unresolved_only: bool = True,
    ) -> tuple[str, list[Any]]:
        """Build a streaming export query with the same filters as Operations."""

        if view not in {"all", "errors", "anomalies"}:
            raise ValueError("unsupported operations export view")
        error_where, error_params = self._operation_error_filters(
            run_id=run_id,
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            stage_or_entity=stage_or_entity,
            document_kind=document_kind,
            retryable=retryable,
            error_class=error_class,
            unresolved_only=unresolved_only,
        )
        anomaly_where, anomaly_params = self._operation_anomaly_filters(
            run_id=run_id,
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            stage_or_entity=stage_or_entity,
            document_kind=document_kind,
            anomaly_type=anomaly_type,
            severity=severity,
            unresolved_only=unresolved_only,
        )
        errors = f"""
            SELECT 'error' AS record_type, e.id, e.run_id, e.session_key,
                   e.bill_id_compact AS bill, e.stage, e.source_entity_type,
                   e.source_id, d.document_kind, e.error_class,
                   CASE e.retryable WHEN 1 THEN 'yes' ELSE 'no' END AS retryable,
                   NULL AS anomaly_type, NULL AS severity,
                   e.message, e.attempt_count AS occurrence_count,
                   e.first_occurred_at AS first_observed_at,
                   e.last_occurred_at AS last_observed_at, e.resolved_at, e.source_url
            FROM collection_errors e LEFT JOIN documents d ON d.id=e.document_id
            {error_where}
        """
        anomalies = f"""
            SELECT 'anomaly' AS record_type, a.id, a.last_run_id AS run_id,
                   a.session_key, a.bill_id_compact AS bill, NULL AS stage,
                   a.source_entity_type, a.source_id, d.document_kind,
                   NULL AS error_class, NULL AS retryable, a.anomaly_type,
                   a.severity, a.message, a.occurrence_count,
                   a.first_observed_at, a.last_observed_at, a.resolved_at, a.source_url
            FROM source_anomalies a LEFT JOIN documents d ON d.id=a.document_id
            {anomaly_where}
        """
        if view == "errors":
            sql = errors
            params = error_params
            order_by = "e.last_occurred_at DESC, e.id DESC"
        elif view == "anomalies":
            sql = anomalies
            params = anomaly_params
            order_by = "a.last_observed_at DESC, a.id DESC"
        else:
            sql = f"SELECT * FROM ({errors} UNION ALL {anomalies})"
            params = [*error_params, *anomaly_params]
            order_by = "last_observed_at DESC, id DESC"
        return sql + f" ORDER BY {order_by}", params

    def _operation_errors(
        self,
        *,
        run_id: int | None,
        session_key: str | None,
        bill_id_compact: str | None,
        stage_or_entity: str | None,
        document_kind: str | None,
        retryable: bool | None,
        error_class: str | None,
        unresolved_only: bool,
        limit: int,
        offset: int,
    ) -> QueryPage:
        where_sql, params = self._operation_error_filters(
            run_id=run_id,
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            stage_or_entity=stage_or_entity,
            document_kind=document_kind,
            retryable=retryable,
            error_class=error_class,
            unresolved_only=unresolved_only,
        )
        base = "FROM collection_errors e LEFT JOIN documents d ON d.id=e.document_id"
        return self._page(
            f"""
            SELECT e.*, d.document_kind, d.http_status
            {base} {where_sql}
            ORDER BY e.last_occurred_at DESC, e.id DESC LIMIT ? OFFSET ?
            """,
            f"SELECT COUNT(*) {base} {where_sql}",
            params,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _retry_document_scope(
        *,
        run_id: int | None,
        session_key: str | None,
        bill_id_compact: str | None,
        include_terminal: bool,
    ) -> tuple[str, list[str], list[Any]]:
        statuses = (
            *RETRYABLE_STATUSES,
            *(("failed_terminal",) if include_terminal else ()),
        )
        placeholders = ",".join("?" for _ in statuses)
        kind_placeholders = ",".join("?" for _ in RETRY_PAYLOAD_DOCUMENT_KINDS)
        joins = ""
        where = [
            "CAST(substr(d.session_key,1,4) AS INTEGER) >= 2014",
            f"d.download_status IN ({placeholders})",
            f"d.document_kind IN ({kind_placeholders})",
            "NULLIF(trim(d.canonical_download_url),'') IS NOT NULL",
        ]
        params: list[Any] = [*statuses, *sorted(RETRY_PAYLOAD_DOCUMENT_KINDS)]
        if run_id is not None:
            joins = "JOIN collection_run_items i ON i.document_id=d.id"
            where.append("i.run_id=?")
            params.append(int(run_id))
        if session_key:
            where.append("d.session_key=?")
            params.append(session_key.strip().upper())
        if bill_id_compact:
            where.append("d.bill_id_compact=?")
            params.append(bill_id_compact.replace(" ", "").upper())
        return joins, where, params

    def _operation_anomalies(
        self,
        *,
        run_id: int | None,
        session_key: str | None,
        bill_id_compact: str | None,
        stage_or_entity: str | None,
        document_kind: str | None,
        anomaly_type: str | None,
        severity: str | None,
        unresolved_only: bool,
        limit: int,
        offset: int,
    ) -> QueryPage:
        where_sql, params = self._operation_anomaly_filters(
            run_id=run_id,
            session_key=session_key,
            bill_id_compact=bill_id_compact,
            stage_or_entity=stage_or_entity,
            document_kind=document_kind,
            anomaly_type=anomaly_type,
            severity=severity,
            unresolved_only=unresolved_only,
        )
        base = "FROM source_anomalies a LEFT JOIN documents d ON d.id=a.document_id"
        return self._page(
            f"""
            SELECT a.*, d.document_kind
            {base} {where_sql}
            ORDER BY a.last_observed_at DESC, a.id DESC LIMIT ? OFFSET ?
            """,
            f"SELECT COUNT(*) {base} {where_sql}",
            params,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _operation_error_filters(
        *,
        run_id: int | None,
        session_key: str | None,
        bill_id_compact: str | None,
        stage_or_entity: str | None,
        document_kind: str | None,
        retryable: bool | None,
        error_class: str | None,
        unresolved_only: bool,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if unresolved_only:
            where.append("e.resolved_at IS NULL")
        if run_id is not None:
            where.append("e.run_id=?")
            params.append(run_id)
        if session_key:
            where.append("e.session_key=?")
            params.append(session_key.strip().upper())
        if bill_id_compact:
            where.append("e.bill_id_compact=?")
            params.append(bill_id_compact.replace(" ", "").upper())
        if stage_or_entity:
            needle = f"%{stage_or_entity.strip()}%"
            where.append("(e.stage LIKE ? OR e.source_entity_type LIKE ?)")
            params.extend((needle, needle))
        if document_kind:
            where.append("d.document_kind=?")
            params.append(document_kind)
        if retryable is not None:
            where.append("e.retryable=?")
            params.append(int(retryable))
        if error_class:
            where.append("(e.error_class LIKE ? OR CAST(d.http_status AS TEXT) LIKE ?)")
            needle = f"%{error_class.strip()}%"
            params.extend((needle, needle))
        return (" WHERE " + " AND ".join(where) if where else ""), params

    @staticmethod
    def _operation_anomaly_filters(
        *,
        run_id: int | None,
        session_key: str | None,
        bill_id_compact: str | None,
        stage_or_entity: str | None,
        document_kind: str | None,
        anomaly_type: str | None,
        severity: str | None,
        unresolved_only: bool,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if unresolved_only:
            where.append("a.resolved_at IS NULL")
        if run_id is not None:
            where.append("(a.first_run_id=? OR a.last_run_id=?)")
            params.extend((run_id, run_id))
        if session_key:
            where.append("a.session_key=?")
            params.append(session_key.strip().upper())
        if bill_id_compact:
            where.append("a.bill_id_compact=?")
            params.append(bill_id_compact.replace(" ", "").upper())
        if stage_or_entity:
            where.append("a.source_entity_type LIKE ?")
            params.append(f"%{stage_or_entity.strip()}%")
        if document_kind:
            where.append("d.document_kind=?")
            params.append(document_kind)
        if anomaly_type:
            where.append("a.anomaly_type LIKE ?")
            params.append(f"%{anomaly_type.strip()}%")
        if severity:
            where.append("a.severity=?")
            params.append(severity)
        return (" WHERE " + " AND ".join(where) if where else ""), params

    @staticmethod
    def _document_filters(
        *,
        session_key: Any = None,
        bill_id_compact: Any = None,
        document_kind: Any = None,
        committee: Any = None,
        submitter: Any = None,
        organization: Any = None,
        testimony_position: Any = None,
        download_status: Any = None,
        source_presence: Any = None,
        displayed_in_olis: bool | None | str = "any",
        failed_only: bool = False,
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        exact = {
            "d.session_key": str(session_key).strip().upper() if session_key else None,
            "d.bill_id_compact": (
                str(bill_id_compact).replace(" ", "").strip().upper()
                if bill_id_compact else None
            ),
            "d.document_kind": document_kind,
            "d.testimony_position": testimony_position,
            "d.download_status": download_status,
            "d.source_presence": source_presence,
        }
        for column, value in exact.items():
            if value:
                where.append(f"{column}=?")
                params.append(value)
        if committee:
            needle = f"%{str(committee).strip()}%"
            where.append("(d.committee_code LIKE ? OR d.committee_name LIKE ?)")
            params.extend((needle, needle))
        if submitter:
            where.append("d.submitter LIKE ?")
            params.append(f"%{str(submitter).strip()}%")
        if organization:
            needle = f"%{str(organization).strip()}%"
            where.append("(d.city_organization LIKE ? OR d.on_behalf_of LIKE ?)")
            params.extend((needle, needle))
        if displayed_in_olis is True or displayed_in_olis == "yes":
            where.append("d.displayed_in_olis=1")
        elif displayed_in_olis is False or displayed_in_olis == "no":
            where.append("d.displayed_in_olis=0")
        elif displayed_in_olis in {None, "unknown"}:
            where.append("d.displayed_in_olis IS NULL")
        if failed_only:
            where.append("d.download_status IN ('failed_retryable','failed_terminal')")
        return where, params

    def _page(
        self,
        select_sql: str,
        count_sql: str,
        params: Sequence[Any],
        *,
        limit: int,
        offset: int,
    ) -> QueryPage:
        bounded_limit = max(1, min(int(limit), 500))
        bounded_offset = max(0, int(offset))
        with self.database.connection() as connection:
            total = int(connection.execute(count_sql, tuple(params)).fetchone()[0])
            rows = connection.execute(
                select_sql, (*params, bounded_limit, bounded_offset)
            ).fetchall()
        return QueryPage(tuple(dict(row) for row in rows), total)

    def _all(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "ArchiveQueries",
    "HISTORICAL_BOUNDARY_KEY",
    "QueryPage",
    "RETRYABLE_STATUSES",
]
