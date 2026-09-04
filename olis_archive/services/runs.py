"""Durable collection-run headers, stage ledgers, claims, and error records."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping
from uuid import uuid4

from ..database import Database


RUN_STATUSES = {
    "queued",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "paused",
    "canceled",
    "interrupted",
}
RUN_TYPES = {
    "collect_bill",
    "collect_session",
    "retry_failures",
    "inventory_backfill",
    "download_archive",
}
HISTORICAL_RUN_TYPES = {"inventory_backfill", "download_archive"}
ACTIVE_RUN_STATUSES = {"queued", "running"}
RUN_STAGES = (
    "queued",
    "load_session",
    "load_reference_data",
    "load_measure",
    "load_sponsors",
    "discover_committee_documents",
    "discover_floor_letters",
    "discover_public_testimony",
    "normalize_documents",
    "download_documents",
    "finalize",
    "resolve_sessions",
    "sync_session",
    "sync_reference_data",
    "sync_measures",
    "sync_sponsors",
    "sync_committee_meetings",
    "sync_agenda_items",
    "sync_committee_documents",
    "sync_public_testimony",
    "sync_floor_letters",
    "reconcile_olis_display",
    "probe_documents",
    "reconcile_presence",
    "finalize_session",
    "finalize_run",
    "download_archive",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_run(
        self,
        run_type: str,
        *,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        scope: Mapping[str, Any] | None = None,
        config_snapshot: Mapping[str, Any] | None = None,
        bills_total: int = 0,
        session_keys: Iterable[str] | None = None,
        scope_cutoff_at: str | datetime | None = None,
        probe_remote_sizes: bool | None = None,
        queued_at: str | datetime | None = None,
        run_uuid: str | None = None,
    ) -> int:
        if run_type not in RUN_TYPES:
            raise ValueError(f"Unsupported run type: {run_type}")
        now = _timestamp(queued_at)
        frozen_scope = dict(scope or {})
        selected_sessions: tuple[str, ...] = ()
        cutoff: str | None = None
        if run_type in HISTORICAL_RUN_TYPES:
            requested_sessions = session_keys
            if requested_sessions is None:
                requested_sessions = frozen_scope.get("session_keys", ())
            selected_sessions = _session_keys(requested_sessions)
            if not selected_sessions:
                raise ValueError(f"{run_type} requires at least one selected session")
            frozen_scope["session_keys"] = list(selected_sessions)
            if run_type == "inventory_backfill":
                if probe_remote_sizes is None:
                    probe_remote_sizes = bool(frozen_scope.get("probe_remote_sizes", False))
                frozen_scope["probe_remote_sizes"] = bool(probe_remote_sizes)
            else:
                for field, label in (
                    ("eligible_statuses", "eligible download statuses"),
                    ("document_kinds", "document kinds"),
                ):
                    value = frozen_scope.get(field)
                    if value is None:
                        continue
                    if isinstance(value, str):
                        has_value = bool(value.strip())
                    else:
                        try:
                            has_value = any(
                                str(item).strip() for item in value
                            )
                        except TypeError as exc:
                            raise ValueError(
                                f"download_archive {field} must be a collection"
                            ) from exc
                    if not has_value:
                        raise ValueError(
                            f"download_archive requires at least one frozen {label}"
                        )
                candidate_cutoff = scope_cutoff_at or frozen_scope.get("scope_cutoff_at") or now
                cutoff = _timestamp(candidate_cutoff)
                frozen_scope["scope_cutoff_at"] = cutoff
        elif session_keys is not None:
            raise ValueError("session_keys is only valid for historical runs")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_runs(
                    run_uuid, run_type, requested_session_key, requested_bill_id_compact,
                    requested_scope_json, scope_cutoff_at, status, stage, queued_at, updated_at,
                    sessions_total, bills_total, config_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    run_uuid or str(uuid4()),
                    run_type,
                    session_key.upper() if session_key else (
                        selected_sessions[0] if len(selected_sessions) == 1 else None
                    ),
                    bill_id_compact.replace(" ", "").upper() if bill_id_compact else None,
                    _json(frozen_scope),
                    cutoff,
                    now,
                    now,
                    len(selected_sessions),
                    max(0, int(bills_total)),
                    _json(config_snapshot or {}),
                ),
            )
            run_id = int(cursor.lastrowid)
            if selected_sessions:
                stage = "sync_session" if run_type == "inventory_backfill" else "download_archive"
                activity = (
                    "Waiting for session inventory"
                    if run_type == "inventory_backfill"
                    else "Waiting for session archive download"
                )
                connection.executemany(
                    """
                    INSERT INTO collection_run_items(
                        run_id,item_type,item_key,session_key,stage,status,current_activity,
                        queued_at,updated_at,details_json
                    ) VALUES (?, 'session', ?, ?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (
                        (
                            run_id,
                            selected,
                            selected,
                            stage,
                            activity,
                            now,
                            now,
                            _json({"session_key": selected}),
                        )
                        for selected in selected_sessions
                    ),
                )
            return run_id

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def run_items(self, run_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_run_items WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def errors(self, run_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_errors WHERE run_id = ? ORDER BY last_occurred_at DESC, id DESC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unresolved_errors(self, run_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM collection_errors
                WHERE run_id=? AND resolved_at IS NULL
                ORDER BY last_occurred_at DESC, id DESC
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_run(self, run_id: int) -> bool:
        """Claim one queued run while enforcing the single-run writer invariant."""

        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_runs
                SET status='running', started_at=COALESCE(started_at, ?), finished_at=NULL,
                    interrupted_at=NULL, updated_at=?, current_activity='Starting collection'
                WHERE id=? AND status='queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM collection_runs active
                      WHERE active.status='running' AND active.id<>?
                  )
                """,
                (now, now, run_id, run_id),
            ).rowcount
        return changed == 1

    def begin_stage(
        self,
        run_id: int,
        stage: str,
        activity: str,
        *,
        progress_total: int | None = None,
        item_key: str | None = None,
        item_type: str = "stage",
        bill_id: int | None = None,
        document_id: int | None = None,
        session_key: str | None = None,
    ) -> int:
        if stage not in RUN_STAGES:
            raise ValueError(f"Unknown run stage: {stage}")
        now = utc_now()
        key = item_key or stage
        item_session_key = _optional_session_key(session_key)
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM collection_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Collection run {run_id} does not exist")
            if run["status"] != "running":
                raise RuntimeError(
                    f"Collection run {run_id} is {run['status']}, not running"
                )
            connection.execute(
                """
                UPDATE collection_run_items
                SET status='completed', finished_at=COALESCE(finished_at, ?), updated_at=?,
                    progress_current=COALESCE(progress_total, progress_current)
                WHERE run_id=? AND item_type='stage' AND status='running'
                """,
                (now, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,bill_id,document_id,stage,status,
                    current_activity,progress_total,queued_at,started_at,updated_at
                ) VALUES (?,?,?,?,?,?,?, 'running',?,?,?,?,?)
                ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET
                    session_key=COALESCE(excluded.session_key,collection_run_items.session_key),
                    bill_id=excluded.bill_id, document_id=excluded.document_id,
                    stage=excluded.stage,status='running',current_activity=excluded.current_activity,
                    progress_total=excluded.progress_total,started_at=COALESCE(collection_run_items.started_at,excluded.started_at),
                    finished_at=NULL,interrupted_at=NULL,updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    item_type,
                    key,
                    item_session_key,
                    bill_id,
                    document_id,
                    stage,
                    activity,
                    progress_total,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM collection_run_items WHERE run_id=? AND item_type=? AND item_key=?",
                (run_id, item_type, key),
            ).fetchone()
            connection.execute(
                "UPDATE collection_runs SET stage=?,current_activity=?,updated_at=? WHERE id=? AND status='running'",
                (stage, activity, now, run_id),
            )
            return int(row["id"])

    def update_progress(
        self,
        run_id: int,
        item_id: int,
        current: int,
        activity: str | None = None,
        *,
        total: int | None = None,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_run_items
                SET progress_current=?,progress_total=COALESCE(?,progress_total),
                    current_activity=COALESCE(?,current_activity),updated_at=?
                WHERE id=? AND run_id=?
                """,
                (
                    max(0, int(current)),
                    None if total is None else max(0, int(total)),
                    activity,
                    now,
                    item_id,
                    run_id,
                ),
            )
            if activity:
                connection.execute(
                    "UPDATE collection_runs SET current_activity=?,updated_at=? WHERE id=?",
                    (activity, now, run_id),
                )

    def finish_item(self, run_id: int, item_type: str, item_key: str, status: str = "completed", *, details: Mapping[str, Any] | None = None) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_run_items
                SET status=?,finished_at=?,updated_at=?,
                    details_json=CASE WHEN ? IS NULL THEN details_json ELSE ? END,
                    progress_current=COALESCE(progress_total,progress_current)
                WHERE run_id=? AND item_type=? AND item_key=?
                """,
                (status, now, now, None if details is None else 1, _json(details or {}), run_id, item_type, item_key),
            )

    def add_document_item(
        self,
        run_id: int,
        document_id: int,
        bill_id: int,
        key: str,
        *,
        session_key: str | None = None,
    ) -> int:
        now = utc_now()
        item_session_key = _optional_session_key(session_key)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,bill_id,document_id,stage,status,
                    current_activity,queued_at,updated_at
                ) VALUES (?, 'document', ?, ?, ?, ?, 'download_documents', 'queued',
                          'Waiting to download', ?, ?)
                ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET
                    session_key=COALESCE(excluded.session_key,collection_run_items.session_key),
                    bill_id=excluded.bill_id,document_id=excluded.document_id,
                    status=CASE WHEN collection_run_items.status='completed' THEN 'completed' ELSE 'queued' END,
                    updated_at=excluded.updated_at
                """,
                (run_id, key, item_session_key, bill_id, document_id, now, now),
            )
            row = connection.execute(
                "SELECT id FROM collection_run_items WHERE run_id=? AND item_type='document' AND item_key=?",
                (run_id, key),
            ).fetchone()
            return int(row["id"])

    def record_archive_document_skip(
        self,
        run_id: int,
        *,
        document_id: int,
        bill_id: int,
        session_key: str,
        message: str = "Verified existing current payload",
    ) -> int:
        """Persist a bounded Download Archive validation/skip result."""

        now = utc_now()
        key = f"document:{int(document_id)}"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,bill_id,document_id,stage,
                    status,current_activity,queued_at,started_at,finished_at,updated_at
                ) VALUES (?, 'document', ?, ?, ?, ?, 'download_archive', 'skipped',
                          ?, ?, ?, ?, ?)
                ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET
                    session_key=excluded.session_key,bill_id=excluded.bill_id,
                    document_id=excluded.document_id,stage='download_archive',
                    status='skipped',current_activity=excluded.current_activity,
                    started_at=COALESCE(collection_run_items.started_at,excluded.started_at),
                    finished_at=excluded.finished_at,interrupted_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    key,
                    session_key,
                    int(bill_id),
                    int(document_id),
                    message[:2000],
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM collection_run_items
                WHERE run_id=? AND item_type='document' AND item_key=?
                """,
                (run_id, key),
            ).fetchone()
            return int(row["id"])

    def record_archive_document_failure(
        self,
        run_id: int,
        *,
        document_id: int,
        bill_id: int,
        session_key: str,
        message: str,
        retryable: bool,
    ) -> int:
        """Persist a Download Archive audit failure that cannot be claimed."""

        now = utc_now()
        key = f"document:{int(document_id)}"
        status = "failed_retryable" if retryable else "failed_terminal"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,bill_id,document_id,stage,
                    status,current_activity,queued_at,started_at,finished_at,updated_at
                ) VALUES (?, 'document', ?, ?, ?, ?, 'download_archive', ?,
                          ?, ?, ?, ?, ?)
                ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET
                    session_key=excluded.session_key,bill_id=excluded.bill_id,
                    document_id=excluded.document_id,stage='download_archive',
                    status=excluded.status,current_activity=excluded.current_activity,
                    started_at=COALESCE(collection_run_items.started_at,excluded.started_at),
                    finished_at=excluded.finished_at,interrupted_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    key,
                    session_key,
                    int(bill_id),
                    int(document_id),
                    status,
                    message[:2000],
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM collection_run_items
                WHERE run_id=? AND item_type='document' AND item_key=?
                """,
                (run_id, key),
            ).fetchone()
            return int(row["id"])

    def begin_document_attempt(self, run_id: int, document_id: int) -> bool:
        """Persist one payload attempt after the logical document claim succeeds."""

        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_run_items
                SET status='running',started_at=COALESCE(started_at,?),finished_at=NULL,
                    updated_at=?,attempt_count=attempt_count+1,
                    current_activity='Downloading and validating payload'
                WHERE run_id=? AND item_type='document' AND document_id=?
                  AND status NOT IN ('completed','skipped','canceled')
                """,
                (now, now, run_id, document_id),
            ).rowcount
        return changed == 1

    def mark_document_item(self, run_id: int, document_id: int, status: str, message: str | None = None) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_run_items
                SET status=?,current_activity=COALESCE(?,current_activity),
                    started_at=COALESCE(started_at,?),finished_at=?,updated_at=?
                WHERE run_id=? AND item_type='document' AND document_id=?
                """,
                (status, message, now, now, now, run_id, document_id),
            )

    def finalize_claimed_document_item(
        self,
        run_id: int,
        document_id: int,
        status: str,
        message: str | None = None,
    ) -> bool:
        """Finish a document item only while its run still owns the active claim."""

        if status not in {"completed", "skipped"}:
            raise ValueError("claimed document finalization requires a success status")
        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_run_items
                SET status=?,current_activity=COALESCE(?,current_activity),
                    started_at=COALESCE(started_at,?),finished_at=?,updated_at=?
                WHERE run_id=? AND item_type='document' AND document_id=?
                  AND status='running'
                  AND EXISTS (
                      SELECT 1 FROM collection_runs r
                      WHERE r.id=collection_run_items.run_id AND r.status='running'
                  )
                """,
                (status, message, now, now, now, run_id, document_id),
            ).rowcount
        return changed == 1

    def resolve_document_errors(self, run_id: int, document_id: int) -> int:
        """Retain recovered errors while removing them from the active error count."""

        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_errors SET resolved_at=?
                WHERE run_id=? AND document_id=? AND resolved_at IS NULL
                """,
                (now, run_id, document_id),
            ).rowcount
            connection.execute(
                """
                UPDATE collection_runs
                SET error_count=(
                    SELECT COUNT(*) FROM collection_errors
                    WHERE run_id=? AND resolved_at IS NULL
                ),updated_at=?
                WHERE id=?
                """,
                (run_id, now, run_id),
            )
        return changed

    def resolve_source_errors(
        self,
        run_id: int,
        *,
        stage: str,
        session_key: str,
        source_entity_type: str | None = None,
        bill_id_compact: str | None = None,
    ) -> int:
        """Resolve errors for one successfully recovered source query or OLIS page.

        Historical OData queries are identified by their entity set, while an
        OLIS testimony-page request is identified by its bill.  Requiring
        exactly one of those identities keeps a successful retry from clearing
        errors belonging to another entity, bill, stage, or session.  Row-level
        and document errors are deliberately excluded from this recovery path.
        """

        entity = str(source_entity_type or "").strip() or None
        bill = (
            str(bill_id_compact or "").replace(" ", "").upper().strip() or None
        )
        if (entity is None) == (bill is None):
            raise ValueError(
                "exactly one of source_entity_type or bill_id_compact is required"
            )
        normalized_stage = str(stage).strip()
        normalized_session = str(session_key).strip().upper()
        if not normalized_stage or not normalized_session:
            raise ValueError("stage and session_key are required")

        if entity is not None:
            identity_sql = (
                "source_entity_type=? AND bill_id_compact IS NULL "
                "AND source_id IS NULL AND document_id IS NULL"
            )
            identity_params: tuple[str, ...] = (entity,)
        else:
            identity_sql = (
                "bill_id_compact=? AND source_entity_type IS NULL "
                "AND source_id IS NULL AND document_id IS NULL"
            )
            identity_params = (bill,)

        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE collection_errors SET resolved_at=?
                WHERE run_id=? AND stage=? AND session_key=?
                  AND resolved_at IS NULL AND {identity_sql}
                """,
                (
                    now,
                    run_id,
                    normalized_stage,
                    normalized_session,
                    *identity_params,
                ),
            ).rowcount
            connection.execute(
                """
                UPDATE collection_runs
                SET error_count=(
                    SELECT COUNT(*) FROM collection_errors
                    WHERE run_id=? AND resolved_at IS NULL
                ),updated_at=?
                WHERE id=?
                """,
                (run_id, now, run_id),
            )
        return changed

    def set_counters(self, run_id: int, **counters: int) -> None:
        allowed = {
            "sessions_total", "sessions_completed", "sessions_incomplete", "sessions_failed",
            "bills_total", "bills_completed", "documents_discovered", "documents_queued",
            "documents_downloaded", "documents_skipped", "documents_failed", "bytes_downloaded",
            "error_count",
        }
        invalid = set(counters) - allowed
        if invalid:
            raise ValueError(f"Unknown counters: {sorted(invalid)}")
        if not counters:
            return
        values = {key: max(0, int(value)) for key, value in counters.items()}
        assignments = ",".join(f"{key}=?" for key in values)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE collection_runs SET {assignments},updated_at=? WHERE id=?",
                (*values.values(), utc_now(), run_id),
            )

    def add_downloaded_bytes(self, run_id: int, byte_count: int) -> None:
        """Durably add completed transfer bytes without a read/overwrite race."""

        amount = max(0, int(byte_count))
        if not amount:
            return
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE collection_runs
                SET bytes_downloaded=bytes_downloaded+?,updated_at=?
                WHERE id=?
                """,
                (amount, utc_now(), run_id),
            )

    def record_error(
        self,
        run_id: int,
        *,
        stage: str,
        error: BaseException | str,
        retryable: bool,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        source_entity_type: str | None = None,
        source_id: str | None = None,
        document_id: int | None = None,
        source_url: str | None = None,
        run_item_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        message = str(error).strip()[:2000] or type(error).__name__
        error_class = type(error).__name__ if isinstance(error, BaseException) else "Error"
        fingerprint = sha256(
            "|".join(
                str(value or "")
                for value in (stage, error_class, session_key, bill_id_compact, source_entity_type, source_id, message)
            ).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_errors(
                    run_id,run_item_id,error_fingerprint,stage,session_key,bill_id_compact,
                    source_entity_type,source_id,document_id,source_url,error_class,retryable,
                    message,first_occurred_at,last_occurred_at,details_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,error_fingerprint) DO UPDATE SET
                    run_item_id=COALESCE(excluded.run_item_id,collection_errors.run_item_id),
                    document_id=COALESCE(excluded.document_id,collection_errors.document_id),
                    last_occurred_at=excluded.last_occurred_at,
                    attempt_count=collection_errors.attempt_count+1,
                    retryable=excluded.retryable,message=excluded.message,details_json=excluded.details_json,
                    resolved_at=NULL
                """,
                (
                    run_id, run_item_id, fingerprint, stage, session_key, bill_id_compact,
                    source_entity_type, source_id, document_id, source_url, error_class,
                    int(retryable), message, now, now, _json(details or {}),
                ),
            )
            row = connection.execute(
                "SELECT id FROM collection_errors WHERE run_id=? AND error_fingerprint=?",
                (run_id, fingerprint),
            ).fetchone()
            connection.execute(
                """
                UPDATE collection_runs
                SET error_count=(
                    SELECT COUNT(*) FROM collection_errors
                    WHERE run_id=? AND resolved_at IS NULL
                ),updated_at=? WHERE id=?
                """,
                (run_id, now, run_id),
            )
            return int(row["id"])

    def finish_run(
        self,
        run_id: int,
        *,
        fatal_error: str | None = None,
        summary: Mapping[str, Any] | None = None,
        session_key: str | None = None,
    ) -> str:
        now = utc_now()
        item_session_key = _optional_session_key(session_key)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT r.run_type,r.status,r.error_count,r.documents_downloaded,
                       r.bills_completed,r.sessions_incomplete,r.sessions_failed,
                       (
                           SELECT COUNT(*) FROM collection_run_items i
                           WHERE i.run_id=r.id AND i.item_type='session'
                             AND i.status IN ('failed_retryable','failed_terminal')
                       ) AS failed_session_items
                FROM collection_runs r WHERE r.id=?
                """,
                (run_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Run {run_id} does not exist")
            if row["status"] == "canceled":
                status = "canceled"
            elif row["status"] == "paused":
                return "paused"
            elif fatal_error and not int(row["bills_completed"] or 0):
                status = "failed"
            elif (
                int(row["error_count"] or 0)
                or (
                    row["run_type"] in HISTORICAL_RUN_TYPES
                    and (
                        int(row["sessions_incomplete"] or 0)
                        or int(row["sessions_failed"] or 0)
                        or int(row["failed_session_items"] or 0)
                    )
                )
            ):
                status = "completed_with_errors"
            else:
                status = "completed"
            final_stage = "finalize_run" if row["run_type"] in HISTORICAL_RUN_TYPES else "finalize"
            connection.execute(
                """
                UPDATE collection_run_items SET status='completed',finished_at=COALESCE(finished_at,?),updated_at=?
                WHERE run_id=? AND item_type='stage' AND status='running'
                """,
                (now, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,stage,status,current_activity,
                    progress_current,progress_total,queued_at,started_at,finished_at,updated_at
                ) VALUES (?, 'stage', ?, ?, ?, 'completed', ?, 1, 1, ?, ?, ?, ?)
                ON CONFLICT(run_id,item_type,item_key) DO UPDATE SET
                    session_key=COALESCE(excluded.session_key,collection_run_items.session_key),
                    stage=excluded.stage,status='completed',current_activity=excluded.current_activity,
                    progress_current=1,progress_total=1,
                    started_at=COALESCE(collection_run_items.started_at,excluded.started_at),
                    finished_at=excluded.finished_at,updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    final_stage,
                    item_session_key,
                    final_stage,
                    fatal_error or "Collection finished",
                    now,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE collection_runs SET status=?,stage=?,current_activity=?,finished_at=?,
                    updated_at=?,summary_json=? WHERE id=?
                """,
                (
                    status,
                    final_stage,
                    fatal_error or "Collection finished",
                    now,
                    now,
                    _json(summary or {}),
                    run_id,
                ),
            )
            return status

    def fail_active_items(
        self,
        run_id: int,
        error: BaseException | str,
        *,
        retryable: bool,
    ) -> int:
        """Finish currently active ledger items with an explicit failure state."""

        now = utc_now()
        with self.database.transaction() as connection:
            return self._fail_active_items(
                connection,
                run_id,
                error,
                retryable=retryable,
                now=now,
            )

    def fail_run(
        self,
        run_id: int,
        error: BaseException | str,
        *,
        retryable: bool = False,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            self._fail_active_items(
                connection,
                run_id,
                error,
                retryable=retryable,
                now=now,
            )
            connection.execute(
                "UPDATE collection_runs SET status='failed',current_activity=?,finished_at=?,updated_at=? WHERE id=?",
                (str(error)[:2000], now, now, run_id),
            )

    @staticmethod
    def _fail_active_items(
        connection,
        run_id: int,
        error: BaseException | str,
        *,
        retryable: bool,
        now: str,
    ) -> int:
        status = "failed_retryable" if retryable else "failed_terminal"
        return connection.execute(
            """
            UPDATE collection_run_items
            SET status=?,current_activity=?,finished_at=?,updated_at=?
            WHERE run_id=? AND status='running'
            """,
            (status, str(error)[:2000], now, now, run_id),
        ).rowcount

    def pause(self, run_id: int, activity: str) -> bool:
        """Pause a running job without falsely marking it finished."""

        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_runs
                SET status='paused',current_activity=?,finished_at=NULL,updated_at=?
                WHERE id=? AND status='running'
                """,
                (activity[:2000], now, run_id),
            ).rowcount
            if changed:
                connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status='paused',current_activity=?,finished_at=NULL,updated_at=?
                    WHERE run_id=? AND item_type IN ('stage','session','document') AND status='running'
                    """,
                    (activity[:2000], now, run_id),
                )
        return changed == 1

    def cancel(self, run_id: int) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_runs SET status='canceled',current_activity='Canceled by operator',
                    finished_at=?,updated_at=? WHERE id=? AND status IN ('queued','running','paused','interrupted')
                """,
                (now, now, run_id),
            ).rowcount
            if changed:
                connection.execute(
                    """
                    UPDATE collection_run_items SET status='canceled',finished_at=?,updated_at=?
                    WHERE run_id=? AND status IN ('queued','running','paused','interrupted')
                    """,
                    (now, now, run_id),
                )
        return changed == 1

    def is_canceled(self, run_id: int) -> bool:
        run = self.get_run(run_id)
        return bool(run and run["status"] == "canceled")

    def is_paused(self, run_id: int) -> bool:
        run = self.get_run(run_id)
        return bool(run and run["status"] == "paused")

    def status(self, run_id: int) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status FROM collection_runs WHERE id=?", (run_id,)
            ).fetchone()
        return str(row["status"]) if row else None

    def should_abort_active_work(self, run_id: int) -> bool:
        return self.status(run_id) in {"paused", "canceled", "interrupted", "failed"}

    def requeue(self, run_id: int) -> bool:
        now = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE collection_runs SET status='queued',stage='queued',current_activity='Queued to resume',
                    finished_at=NULL,interrupted_at=NULL,updated_at=?
                WHERE id=? AND status IN ('interrupted','paused')
                """,
                (now, run_id),
            ).rowcount
            if changed:
                connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status='queued',started_at=NULL,finished_at=NULL,interrupted_at=NULL,updated_at=?
                    WHERE run_id=? AND status IN ('interrupted','paused','failed_retryable')
                    """,
                    (now, run_id),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET download_status='queued',last_error=NULL
                    WHERE id IN (
                        SELECT document_id FROM collection_run_items
                        WHERE run_id=? AND document_id IS NOT NULL
                    ) AND download_status IN ('interrupted','paused_low_space','failed_retryable')
                    """,
                    (run_id,),
                )
        return changed == 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _optional_session_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip().upper()
    if not key:
        raise ValueError("session key must not be empty")
    return key


def _timestamp(value: str | datetime | None = None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid RFC-3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _session_keys(values: Iterable[str] | Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        values = (values,)
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        key = str(value).strip().upper()
        if not key:
            raise ValueError("session keys must not be empty")
        if key not in seen:
            seen.add(key)
            result.append(key)
    return tuple(result)


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "HISTORICAL_RUN_TYPES",
    "RUN_STAGES",
    "RUN_STATUSES",
    "RUN_TYPES",
    "RunStore",
    "utc_now",
]
