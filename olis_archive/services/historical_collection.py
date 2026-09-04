"""Durable Phase 2 historical inventory and archive-download orchestration.

This service coordinates the already-separated source, parser, persistence,
probe, and payload components.  It intentionally contains no Flask behavior so
the web UI and CLI execute exactly the same durable workflows.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import inspect
import json
import shutil
import threading
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

from ..config import AppConfig, DEFAULT_ALLOWED_DOWNLOAD_HOSTS
from ..database import Database
from .archive_paths import resolve_stored_path
from .documents import committee_document, floor_letter_document, public_testimony_documents
from .file_types import validate_file
from .historical_sources import (
    ENTITY_SYNC_SPECS,
    SessionEntityPlan,
    SessionScope,
    build_session_entity_plan,
    resolve_historical_session_scope_from_odata,
    require_supported_session_key,
    stream_session_entity,
    testimony_reconciliation_candidate,
    validate_odata_metadata,
)
from .probes import RemoteSizeProbe
from .reconciliation import (
    DisplayCheck,
    DocumentReconciliationResult,
    reconcile_historical_presentations,
    reconcile_modern_public_testimony,
)
from .record_mapping import (
    committee_display,
    integer,
    map_committee,
    map_legislator,
    map_meeting,
    map_session,
    meeting_source_id,
    text,
)
from .runs import RunStore
from .source_mapping import (
    map_measure,
    map_sponsor,
    normalize_bill_id,
    normalize_session_key,
)
from .storage import DOCUMENT_KINDS, StorageService
from .testimony_parser import inspect_testimony_page


INVENTORY_ENTITY_ORDER = (
    "Legislators",
    "Committees",
    "Measures",
    "MeasureSponsors",
    "CommitteeMeetings",
    "CommitteeAgendaItems",
    "CommitteeMeetingDocuments",
    "CommitteePublicTestimonies",
    "FloorLetters",
)

ENTITY_STAGES = {
    "Legislators": "sync_reference_data",
    "Committees": "sync_reference_data",
    "Measures": "sync_measures",
    "MeasureSponsors": "sync_sponsors",
    "CommitteeMeetings": "sync_committee_meetings",
    "CommitteeAgendaItems": "sync_agenda_items",
    "CommitteeMeetingDocuments": "sync_committee_documents",
    "CommitteePublicTestimonies": "sync_public_testimony",
    "FloorLetters": "sync_floor_letters",
}

SOURCE_ENTITY_TYPES = {
    "CommitteeMeetingDocuments": "CommitteeMeetingDocument",
    "CommitteePublicTestimonies": "CommitteePublicTestimony",
    "FloorLetters": "FloorLetter",
}

DISPLAY_RECONCILIATION_FAMILIES = (
    "CommitteePublicTestimony",
    "CommitteeMeetingDocument",
)

MAX_CONSECUTIVE_RETRYABLE_OLIS_FAILURES = 3

CURRENT_DISPLAY_DISCREPANCY_ANOMALIES = (
    "odata_olis_count_mismatch",
    "odata_only_display_candidate",
    "olis_page_only_record",
    "presentation_type_mismatch",
    "conflicting_duplicate_display_id",
)

CURRENT_DISPLAY_ANOMALIES = (
    *CURRENT_DISPLAY_DISCREPANCY_ANOMALIES,
    "olis_display_fetch_failed",
    "olis_display_parser_anomaly",
    "olis_parser_anomaly",
    "testimony_candidate_rule_mismatch",
)

DEFAULT_ARCHIVE_KINDS = frozenset(
    {"public_testimony", "legacy_testimony", "committee_presentation", "floor_letter"}
)

DEFAULT_ARCHIVE_STATUSES = frozenset(
    {
        "discovered",
        "queued",
        "failed_retryable",
        "paused_low_space",
        "interrupted",
        "missing_local",
        "changed_remote",
    }
)

FAILURE_ONLY_STATUSES = frozenset(
    {"failed_retryable", "paused_low_space", "interrupted", "missing_local", "changed_remote"}
)


class ODataLike(Protocol):
    def iter_pages(
        self,
        entity_set: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        **params: Any,
    ): ...  # noqa: ANN201
    def build_url(self, entity_set: str, **params: Any) -> str: ...


class OLISLike(Protocol):
    def get_testimony_page(
        self,
        session_key: str,
        bill_id: str,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
    ): ...  # noqa: ANN201
    def testimony_url(self, session_key: str, bill_id: str) -> str: ...


class HistoricalRunControl(RuntimeError):
    """Raised when persisted operator/process state asks work to wind down."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Historical run is {status}")
        self.status = status


@dataclass(frozen=True, slots=True)
class DownloadPreflight:
    session_keys: tuple[str, ...]
    document_kinds: tuple[str, ...]
    eligible_statuses: tuple[str, ...]
    documents_in_scope: int
    already_downloaded: int
    pending_or_missing: int
    retryable_failures: int
    terminal_or_non_downloadable: int
    known_pending_bytes: int
    unknown_size_pending: int
    free_bytes: int
    minimum_free_space_bytes: int
    archive_root: str
    download_worker_count: int

    @property
    def known_bytes_fit(self) -> bool:
        return self.free_bytes - self.known_pending_bytes >= self.minimum_free_space_bytes

    @property
    def estimate_is_lower_bound(self) -> bool:
        return self.unknown_size_pending > 0

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["known_bytes_fit"] = self.known_bytes_fit
        result["estimate_is_lower_bound"] = self.estimate_is_lower_bound
        return result


ClaimedDownload = Callable[[int, Mapping[str, Any]], tuple[int, str, int]]


class HistoricalCollectionService:
    """Run coordinator shared by :class:`CollectionService`, CLI, and Flask."""

    def __init__(
        self,
        config: AppConfig,
        *,
        database: Database,
        storage: StorageService,
        runs: RunStore,
        odata: ODataLike,
        olis_http: OLISLike,
        size_probe: RemoteSizeProbe,
        download_claimed: ClaimedDownload,
    ) -> None:
        self.config = config
        self.database = database
        self.storage = storage
        self.runs = runs
        self.odata = odata
        self.olis_http = olis_http
        self.size_probe = size_probe
        self.download_claimed = download_claimed
        self._cached_session: str | None = None
        self._cached_legislator_names: dict[str, str] | None = None
        self._cached_committee_names: dict[str, str] | None = None
        self._cached_committee_ids: dict[str, int] | None = None
        self._cached_meeting_ids: dict[str, int] | None = None
        self._cached_bills: dict[str, dict[str, Any]] | None = None

    # -- scope and run creation --------------------------------------------

    def historical_session_scope(self) -> SessionScope:
        """Resolve scope from official chronology; never infer session suffixes."""

        return resolve_historical_session_scope_from_odata(self.odata)

    def create_inventory_backfill_run(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        probe_remote_sizes: bool = False,
        force_full: bool = False,
        resolved_scope: SessionScope | None = None,
    ) -> int:
        # Web range selection resolves the official catalogue once, validates
        # both endpoints on that immutable snapshot, and passes the resulting
        # exact selection here.  CLI callers omit this argument and retain the
        # same authoritative discovery behavior.
        resolved_scope = resolved_scope or self.historical_session_scope()
        scope = resolved_scope
        if session_keys is not None:
            scope = scope.selected(session_keys)
        frozen = scope.requested_scope()
        frozen["probe_remote_sizes"] = bool(probe_remote_sizes)
        frozen["force_full"] = bool(force_full)
        frozen["resolved_from"] = "official LegislativeSessions BeginDate chronology"
        # The durable run must never outlive a partially persisted catalogue.
        # RunStore and StorageService join this outer transaction through
        # Database's thread-local transaction nesting.
        with self.database.transaction():
            run_id = self.runs.create_run(
                "inventory_backfill",
                session_keys=scope.session_keys,
                scope=frozen,
                probe_remote_sizes=probe_remote_sizes,
                config_snapshot=self.config.snapshot(),
            )
            # Persist the complete officially resolved chronology, including
            # visible pre-boundary rows, even when this run selects only a later
            # subset. Read models can therefore fail closed if the boundary
            # cannot be proven without silently hiding older official sessions.
            for session in resolved_scope.sessions:
                if session.compatibility_issue is not None:
                    # The frozen scope retains the source key and diagnostic,
                    # but incompatible catalogue rows must not enter tables or
                    # collection code whose key/date invariants they violate.
                    continue
                self.storage.upsert_session(map_session(session.raw), run_id=run_id)
        return run_id

    def inventory_scope_from_run(self, run_id: int) -> tuple[str, ...]:
        run = self.runs.get_run(run_id)
        if not run or run.get("run_type") != "inventory_backfill":
            raise ValueError(f"Run #{run_id} is not an Inventory Backfill run")
        return tuple(_json_object(run.get("requested_scope_json")).get("session_keys", ()))

    def download_preflight(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        document_kinds: Iterable[str] | None = None,
        retryable_failures_only: bool = False,
        missing_pending_only: bool = True,
    ) -> DownloadPreflight:
        del missing_pending_only  # Current downloaded payloads are never force-redownloaded.
        sessions = self._download_session_scope(session_keys)
        kinds = _document_kinds(document_kinds)
        statuses = tuple(sorted(FAILURE_ONLY_STATUSES if retryable_failures_only else DEFAULT_ARCHIVE_STATUSES))
        session_marks = ",".join("?" for _ in sessions)
        kind_marks = ",".join("?" for _ in kinds)
        status_marks = ",".join("?" for _ in statuses)
        in_scope = (
            f"d.session_key IN ({session_marks}) AND "
            f"d.document_kind IN ({kind_marks}) AND d.source_presence!='missing'"
        )
        pending = in_scope + f" AND d.download_status IN ({status_marks}) AND NULLIF(trim(d.canonical_download_url),'') IS NOT NULL"
        with self.database.connection() as connection:
            base_params = (*sessions, *kinds)
            documents_in_scope = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM documents d WHERE {in_scope}", base_params
                ).fetchone()[0]
            )
            already_downloaded = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM documents d WHERE {in_scope} AND d.download_status='downloaded'",
                    base_params,
                ).fetchone()[0]
            )
            pending_or_missing = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM documents d WHERE {pending}",
                    (*base_params, *statuses),
                ).fetchone()[0]
            )
            retryable_failures = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM documents d WHERE {in_scope}
                      AND d.download_status IN ('failed_retryable','paused_low_space','interrupted','missing_local','changed_remote')
                    """,
                    base_params,
                ).fetchone()[0]
            )
            terminal_or_non_downloadable = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM documents d WHERE {in_scope}
                      AND (d.download_status IN ('failed_terminal','not_applicable')
                           OR NULLIF(trim(d.canonical_download_url),'') IS NULL)
                    """,
                    base_params,
                ).fetchone()[0]
            )
            size = connection.execute(
                f"""
                SELECT COALESCE(SUM(p.content_length),0) AS known_bytes,
                       COALESCE(SUM(CASE WHEN p.content_length IS NULL THEN 1 ELSE 0 END),0) AS unknown_count
                FROM documents d
                LEFT JOIN document_remote_probes p ON p.document_id=d.id
                WHERE {pending}
                """,
                (*base_params, *statuses),
            ).fetchone()
        known_pending_bytes = int(size["known_bytes"] or 0)
        unknown_size_pending = int(size["unknown_count"] or 0)
        self.config.archive_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.config.archive_root).free
        return DownloadPreflight(
            sessions,
            kinds,
            statuses,
            documents_in_scope,
            already_downloaded,
            pending_or_missing,
            retryable_failures,
            terminal_or_non_downloadable,
            known_pending_bytes,
            unknown_size_pending,
            int(free),
            int(self.config.minimum_free_space_bytes or 0),
            str(self.config.archive_root),
            self.config.download_worker_count,
        )

    def create_download_archive_run(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        document_kinds: Iterable[str] | None = None,
        retryable_failures_only: bool = False,
        missing_pending_only: bool = True,
    ) -> int:
        sessions = self._download_session_scope(session_keys)
        kinds = _document_kinds(document_kinds)
        preflight = self.download_preflight(
            sessions,
            document_kinds=kinds,
            retryable_failures_only=retryable_failures_only,
            missing_pending_only=missing_pending_only,
        )
        if not preflight.known_bytes_fit:
            raise ValueError(
                "Known pending payload bytes would cross the configured minimum "
                "free-space floor. Free space or raise the archive capacity before starting."
            )
        cutoff = _utc_now()
        scope = {
            "session_keys": list(preflight.session_keys),
            "document_kinds": list(preflight.document_kinds),
            "eligible_statuses": list(preflight.eligible_statuses),
            "retryable_failures_only": bool(retryable_failures_only),
            "missing_pending_only": bool(missing_pending_only),
            "scope_cutoff_at": cutoff,
            "preflight": preflight.as_dict(),
        }
        return self.runs.create_run(
            "download_archive",
            session_keys=preflight.session_keys,
            scope=scope,
            scope_cutoff_at=cutoff,
            config_snapshot=self.config.snapshot(),
        )

    # -- execution ----------------------------------------------------------

    def execute_inventory_backfill(
        self, run_id: int, scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        sessions = tuple(
            require_supported_session_key(value)
            for value in scope.get("session_keys", ())
        )
        if not sessions:
            raise ValueError("Inventory Backfill has no frozen session scope")
        probe_sizes = bool(scope.get("probe_remote_sizes"))
        force_full = bool(scope.get("force_full"))
        completed_items = self._completed_session_items(run_id)
        totals: Counter[str] = Counter()
        totals["sessions_total"] = len(sessions)
        metadata_gap = self._validate_metadata_contract(run_id, sessions)
        for session_key in sessions:
            if session_key in completed_items:
                stored = self._session_inventory_counts(session_key)
                totals["bills"] += stored["bills"]
                totals["documents"] += stored["documents"]
                totals["sessions_completed"] += 1
                continue
            self._check_control(run_id)
            try:
                result = self._inventory_session(
                    run_id,
                    session_key,
                    probe_sizes=probe_sizes,
                    inherited_material_gap=metadata_gap,
                    force_full=force_full,
                )
            except HistoricalRunControl:
                raise
            except Exception as exc:
                # Shutdown or an operator control transition may win after the
                # loop's status check but before a stage begins. Preserve that
                # durable control state instead of recording a false source
                # failure for the session.
                self._check_control(run_id)
                retryable = _retryable(exc)
                self.runs.record_error(
                    run_id,
                    stage=str((self.runs.get_run(run_id) or {}).get("stage") or "sync_session"),
                    error=exc,
                    retryable=retryable,
                    session_key=session_key,
                )
                self.runs.finish_item(
                    run_id,
                    "session",
                    session_key,
                    "failed_retryable" if retryable else "failed_terminal",
                    details={"inventory_status": "inventory_failed", "error": str(exc)},
                )
                self.storage.finish_session_inventory(
                    session_key,
                    run_id,
                    "inventory_failed",
                    details={"error": str(exc)},
                )
                totals["sessions_failed"] += 1
                continue
            totals.update(result)
            if result.get("inventory_complete"):
                totals["sessions_completed"] += 1
            else:
                totals["sessions_incomplete"] += 1
            self.runs.set_counters(
                run_id,
                sessions_total=len(sessions),
                sessions_completed=totals["sessions_completed"],
                sessions_incomplete=totals["sessions_incomplete"],
                sessions_failed=totals["sessions_failed"],
                bills_total=totals["bills"],
                documents_discovered=totals["documents"],
            )
        summary = {
            "session_keys": list(sessions),
            "probe_remote_sizes": probe_sizes,
            "force_full": force_full,
            **dict(totals),
        }
        self.runs.set_counters(
            run_id,
            sessions_total=len(sessions),
            sessions_completed=totals["sessions_completed"],
            sessions_incomplete=totals["sessions_incomplete"],
            sessions_failed=totals["sessions_failed"],
            bills_total=totals["bills"],
            documents_discovered=totals["documents"],
        )
        return summary

    def execute_download_archive(
        self, run_id: int, scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        sessions = tuple(
            require_supported_session_key(value)
            for value in scope.get("session_keys", ())
        )
        if not sessions:
            raise ValueError("Download Archive has no frozen session scope")
        for session_key in sessions:
            self.storage.record_session_download_activity(session_key, run_id)
            self.runs.begin_stage(
                run_id,
                "download_archive",
                f"Downloading inventoried payloads for {len(sessions)} selected session(s)",
                item_key=session_key,
                item_type="session",
                session_key=session_key,
            )
        download_stage_item = self.runs.begin_stage(
            run_id,
            "download_archive",
            f"Downloading inventoried payloads for {len(sessions)} selected session(s)",
            item_key="download_archive",
        )
        kinds = _document_kinds(scope.get("document_kinds"))
        # A failure-only run must not expand its frozen eligible-status scope
        # by auditing and adding durable skip items for healthy downloads.
        # Normal archive runs audit those files so missing/corrupt local bytes
        # can re-enter the database-backed claim queue safely.
        audit_counts = Counter()
        if not bool(scope.get("retryable_failures_only")):
            audit_counts = self._audit_downloaded_scope(
                run_id,
                sessions,
                kinds,
                scope_cutoff_at=str(scope.get("scope_cutoff_at") or ""),
                progress_item_id=download_stage_item,
            )

        counts: Counter[str] = Counter()
        counter_lock = threading.Lock()

        def worker() -> None:
            while True:
                if self.runs.status(run_id) != "running":
                    return
                document = self.storage.claim_next_archive_document(run_id)
                if document is None:
                    return
                document_id, outcome, byte_count = self.download_claimed(run_id, document)
                del document_id
                del byte_count
                with counter_lock:
                    counts[outcome] += 1

        with ThreadPoolExecutor(
            max_workers=max(1, self.config.download_worker_count),
            thread_name_prefix="legiview-archive",
        ) as pool:
            futures = [pool.submit(worker) for _ in range(max(1, self.config.download_worker_count))]
            for future in futures:
                future.result()

        item_counts = self._download_item_counts(run_id)
        self.runs.set_counters(
            run_id,
            documents_downloaded=item_counts.get("completed", 0),
            documents_skipped=item_counts.get("skipped", 0),
            documents_failed=(
                item_counts.get("failed_retryable", 0)
                + item_counts.get("failed_terminal", 0)
            ),
        )
        status = self.runs.status(run_id)
        if status in {"paused", "canceled", "interrupted"}:
            raise HistoricalRunControl(status)
        sessions_completed = 0
        sessions_incomplete = 0
        for session_key in sessions:
            session_counts = self._download_item_counts(run_id, session_key=session_key)
            session_status = (
                "failed_retryable"
                if session_counts.get("failed_retryable", 0)
                else "failed_terminal"
                if session_counts.get("failed_terminal", 0)
                else "completed"
            )
            if session_status == "completed":
                sessions_completed += 1
            else:
                sessions_incomplete += 1
            self.storage.record_session_download_activity(session_key, run_id, completed=True)
            self.runs.finish_item(
                run_id,
                "session",
                session_key,
                session_status,
                details={"download_counts": session_counts},
            )
        self.runs.set_counters(
            run_id,
            sessions_total=len(sessions),
            sessions_completed=sessions_completed,
            sessions_incomplete=sessions_incomplete,
            sessions_failed=0,
        )
        durable_run = self.runs.get_run(run_id) or {}
        return {
            "session_keys": list(sessions),
            "scope_cutoff_at": scope.get("scope_cutoff_at"),
            "payload_audit": dict(audit_counts),
            "item_counts": item_counts,
            "bytes_downloaded": int(durable_run.get("bytes_downloaded") or 0),
        }

    # -- one session --------------------------------------------------------

    def _inventory_session(
        self,
        run_id: int,
        session_key: str,
        *,
        probe_sizes: bool,
        inherited_material_gap: bool,
        force_full: bool,
    ) -> Counter[str]:
        self._reset_session_cache(session_key)
        self.storage.mark_session_inventory_started(session_key, run_id)
        session_item = self.runs.begin_stage(
            run_id,
            "sync_session",
            f"Synchronizing official inventory for {session_key}",
            item_key=session_key,
            item_type="session",
            session_key=session_key,
        )
        self.runs.begin_stage(
            run_id,
            "sync_session",
            f"Synchronizing official inventory for {session_key}",
            item_key=f"{session_key}:sync_session",
            session_key=session_key,
        )
        failures: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        authoritative_entities: set[str] = set()
        measures_succeeded = False

        for entity_set in INVENTORY_ENTITY_ORDER:
            self._check_control(run_id)
            stage = ENTITY_STAGES[entity_set]
            self.runs.begin_stage(
                run_id,
                stage,
                f"{session_key}: synchronizing {entity_set}",
                item_key=f"{session_key}:{stage}:{entity_set}",
                session_key=session_key,
            )
            sync_state = self.storage.get_source_sync_state(session_key, entity_set)
            watermark = sync_state.get("source_watermark") if sync_state else None
            plan = build_session_entity_plan(
                entity_set,
                session_key,
                source_watermark=watermark,
                force_full=force_full,
            )
            try:
                result = self._sync_entity(run_id, session_item, plan)
            except HistoricalRunControl:
                raise
            except Exception as exc:
                self.storage.record_source_sync_failure(
                    session_key,
                    entity_set,
                    strategy=plan.strategy,
                    error=exc,
                )
                retryable = _retryable(exc)
                self.runs.record_error(
                    run_id,
                    run_item_id=session_item,
                    stage=stage,
                    error=exc,
                    retryable=retryable,
                    session_key=session_key,
                    source_entity_type=entity_set,
                    source_url=self._entity_url(plan),
                )
                failures.append(
                    {"entity_set": entity_set, "error": str(exc), "retryable": retryable}
                )
                # Later entities depend on the reference/measure/meeting rows
                # that precede them.  Advancing a child cursor after a parent
                # failed can leave unresolved foreign/display context forever
                # on the next incremental pass.  End this session attempt at
                # the first source gap; its successful predecessors retain
                # their safe cursors and the failed entity remains full-sync or
                # on its last proven cursor for an explicit rerun.
                break
            counts[_count_name(entity_set)] += result.returned_count
            if entity_set == "Measures":
                measures_succeeded = True
            if result.authoritative_presence:
                authoritative_entities.add(entity_set)
            self.runs.update_progress(
                run_id,
                session_item,
                sum(counts.values()),
                f"{session_key}: stored {result.returned_count} {entity_set} row(s)",
            )

        self._check_control(run_id)
        reconciliation = (
            self._reconcile_session_display(
                run_id,
                session_key,
                session_item,
                force_full=force_full,
                authoritative_entities=authoritative_entities,
            )
            if measures_succeeded and not failures
            else {
                "counts": Counter(),
                "failures": [],
                "material_gap": True,
                "display_status": "failed_fetch",
            }
        )
        counts.update(reconciliation["counts"])
        failures.extend(reconciliation["failures"])
        material_gap = inherited_material_gap or bool(reconciliation["material_gap"])

        probe_failures = 0
        if probe_sizes:
            probe_counts = self._probe_session(run_id, session_key, session_item)
            counts.update(probe_counts)
            probe_failures = probe_counts["probes_failed"]

        stored = self._session_inventory_counts(session_key)
        counts["bills"] = stored["bills"]
        counts["documents"] = stored["documents"]
        material_gap = (
            material_gap
            or bool(failures)
            or self._unresolved_material_anomaly_count(session_key) > 0
        )
        if material_gap:
            inventory_status = "inventory_incomplete"
            item_status = "failed_retryable"
        elif probe_failures:
            inventory_status = "inventory_complete_with_errors"
            item_status = "completed"
        else:
            inventory_status = "inventory_complete"
            item_status = "completed"
        details = {
            "counts": dict(counts),
            "source_failures": failures,
            "authoritative_presence_entities": sorted(authoritative_entities),
            "probe_remote_sizes": probe_sizes,
            "completeness_rule": "all required source queries and candidate display checks succeeded",
        }
        self.runs.begin_stage(
            run_id,
            "finalize_session",
            f"{session_key}: {inventory_status.replace('_', ' ')}",
            item_key=f"{session_key}:finalize_session",
            session_key=session_key,
        )
        self.storage.finish_session_inventory(
            session_key,
            run_id,
            inventory_status,
            display_reconciliation_status=reconciliation["display_status"],
            details=details,
        )
        self.runs.finish_item(
            run_id,
            "session",
            session_key,
            item_status,
            details={"inventory_status": inventory_status, **details},
        )
        counts["inventory_complete"] = int(
            inventory_status in {"inventory_complete", "inventory_complete_with_errors"}
        )
        return counts

    def _sync_entity(
        self,
        run_id: int,
        run_item_id: int,
        plan: SessionEntityPlan,
    ):  # noqa: ANN202
        source_url = self._entity_url(plan)

        def consume(batch) -> None:  # noqa: ANN001
            self._check_control(run_id)
            # One bounded source page is the persistence unit.  StorageService
            # methods join this thread-local transaction, avoiding one SQLite
            # connection/commit per row while retaining short transactions and
            # crash-safe page boundaries.
            with self.database.transaction():
                for raw in batch.items:
                    self._persist_source_row(run_id, plan.spec.entity_set, raw)
                self.storage.record_source_fetch(
                    source_kind="odata",
                    source_url=source_url,
                    run_id=run_id,
                    run_item_id=run_item_id,
                    entity_set=plan.spec.entity_set,
                    request_params=plan.request_params,
                    completed_at=_utc_now(),
                    succeeded=True,
                    http_status=200,
                    item_count=len(batch.items),
                    continuation_url=batch.continuation_url,
                )

        try:
            result = stream_session_entity(
                self.odata,
                plan,
                consume,
                cancellation_requested=lambda: self.runs.should_abort_active_work(
                    run_id
                ),
            )
        except Exception as exc:
            # A source client reports cooperative interruption in its own
            # exception vocabulary. Convert persisted run control to the
            # coordinator-level signal before recording a false source error.
            self._check_control(run_id)
            self.storage.record_source_fetch(
                source_kind="odata",
                source_url=source_url,
                run_id=run_id,
                run_item_id=run_item_id,
                entity_set=plan.spec.entity_set,
                request_params=plan.request_params,
                completed_at=_utc_now(),
                succeeded=False,
                error_class=type(exc).__name__,
                error_message=str(exc)[:2000],
            )
            raise
        # Presence and cursor state are one commit.  A crash can therefore
        # never leave a newly advanced successful cursor without the matching
        # authoritative disappearance reconciliation.
        with self.database.transaction():
            if result.authoritative_presence:
                if plan.spec.entity_set == "Measures":
                    self.storage.reconcile_source_presence(
                        "bill",
                        plan.session_key,
                        run_id,
                        authoritative_complete=True,
                    )
                elif plan.spec.entity_set in SOURCE_ENTITY_TYPES:
                    self.storage.reconcile_source_presence(
                        "document",
                        plan.session_key,
                        run_id,
                        source_entity_type=SOURCE_ENTITY_TYPES[plan.spec.entity_set],
                        authoritative_complete=True,
                    )
            self.storage.record_source_sync_success(
                plan.session_key,
                plan.spec.entity_set,
                strategy=plan.strategy,
                run_id=run_id,
                source_count=result.returned_count,
                source_watermark=result.next_source_watermark,
                full_session=result.authoritative_presence,
                incremental=plan.strategy == "watermark",
                reconciliation_outcome=(
                    "authoritative_full_session"
                    if result.authoritative_presence
                    else "incremental_overlap"
                ),
                details={
                    "pages": result.page_count,
                    "maximum_observed_source_date": result.maximum_observed_source_date,
                    "filter": plan.filter_expression,
                },
            )
            self.runs.resolve_source_errors(
                run_id,
                stage=ENTITY_STAGES[plan.spec.entity_set],
                session_key=plan.session_key,
                source_entity_type=plan.spec.entity_set,
            )
        return result

    # -- row normalization --------------------------------------------------

    def _persist_source_row(
        self, run_id: int, entity_set: str, raw: Mapping[str, Any]
    ) -> None:
        session_key = str(raw["SessionKey"])
        if entity_set == "Legislators":
            self.storage.upsert_legislator(map_legislator(raw), run_id=run_id)
            self._cached_legislator_names = None
            return
        if entity_set == "Committees":
            self.storage.upsert_committee(map_committee(raw), run_id=run_id)
            self._cached_committee_names = None
            self._cached_committee_ids = None
            return
        if entity_set == "Measures":
            record = map_measure(raw)
            if record.get("current_committee_code"):
                record["current_committee_name"] = self._committee_names(session_key).get(
                    str(record["current_committee_code"])
                )
            record["enacted"] = int(bool(record.get("chapter_number")))
            bill_id = self.storage.upsert_bill(record, run_id=run_id)
            if self._cached_bills is not None:
                stored = self.storage.get_bill(session_key, str(record["bill_id_compact"]))
                if stored:
                    self._cached_bills[str(record["bill_id_compact"])] = stored
            return

        committee_ids = self._committee_ids(session_key)
        committee_names = self._committee_names(session_key)
        if entity_set == "CommitteeMeetings":
            code = text(raw.get("CommitteeCode")) or ""
            meeting_id = self.storage.upsert_committee_meeting(
                map_meeting(raw, committee_ids.get(code), committee_names.get(code)),
                run_id=run_id,
            )
            if self._cached_meeting_ids is not None:
                self._cached_meeting_ids[meeting_source_id(raw)] = meeting_id
            return

        bill = self._bill_from_source_row(raw)
        if entity_set == "MeasureSponsors":
            mapped = map_sponsor(raw)
            legislator_names = self._legislator_names(session_key)
            committee_names = self._committee_names(session_key)
            display = (
                legislator_names.get(mapped.display_code or "")
                if mapped.kind == "legislator"
                else committee_names.get(mapped.display_code or "")
                if mapped.kind == "committee"
                else text(raw.get("PresessionFiledMessage"))
            )
            self.storage.upsert_bill_sponsor(
                {
                    "bill_id": int(bill["id"]),
                    "source_measure_sponsor_id": str(raw["MeasureSponsorId"]),
                    "raw_sponsor_type": text(raw.get("SponsorType")),
                    "raw_sponsor_level": text(raw.get("SponsorLevel")),
                    "normalized_category": mapped.category,
                    "legislator_code": text(raw.get("LegislatoreCode")),
                    "committee_code": text(raw.get("CommitteeCode")),
                    "resolved_display_name": display or mapped.display_code,
                    "sponsor_kind": mapped.kind,
                    "print_order": integer(raw.get("PrintOrder")),
                    "pre_session_filed_message": text(raw.get("PresessionFiledMessage")),
                    "source_created_at": text(raw.get("CreatedDate")),
                    "source_modified_at": text(raw.get("ModifiedDate")),
                    "raw_json": dict(raw),
                },
                run_id=run_id,
            )
            return

        code = text(raw.get("CommitteCode")) or text(raw.get("CommitteeCode")) or ""
        meeting_date = text(raw.get("MeetingDate")) or ""
        meeting_id = self._meeting_id(session_key, code, meeting_date)
        if entity_set == "CommitteeAgendaItems":
            self.storage.upsert_committee_agenda_item(
                {
                    "session_key": session_key,
                    "source_agenda_item_id": str(raw["CommitteeAgendaItemId"]),
                    "committee_meeting_id": meeting_id,
                    "bill_id": int(bill["id"]),
                    "measure_id": f"{session_key}:{bill['bill_id_compact']}",
                    "bill_id_compact": str(bill["bill_id_compact"]),
                    "agenda_order": integer(raw.get("PrintOrder")),
                    "agenda_item_type": text(raw.get("MeetingType")) or text(raw.get("AgendaItemType")),
                    "description": text(raw.get("Action")) or text(raw.get("Comments")),
                    "source_created_at": text(raw.get("CreatedDate")),
                    "source_modified_at": text(raw.get("ModifiedDate")),
                    "raw_json": dict(raw),
                },
                run_id=run_id,
            )
            return
        if entity_set == "CommitteeMeetingDocuments":
            document = committee_document(
                raw,
                displayed_ids=set(),
                committee_name=committee_names.get(code),
            )
            document.update(
                bill_id=int(bill["id"]),
                committee_meeting_id=meeting_id,
                displayed_in_olis=None,
                display_reconciled_at=None,
                source_presence="active",
            )
            document_id = self.storage.upsert_document(document, run_id=run_id)
            self._diagnose_document_url(run_id, session_key, bill, document_id, document)
            return
        if entity_set == "CommitteePublicTestimonies":
            document = public_testimony_documents([raw], [], committees=committee_names)[0]
            document.update(
                bill_id=int(bill["id"]),
                displayed_in_olis=None,
                display_reconciled_at=None,
                source_section="odata_public_testimony",
                source_presence="active",
            )
            document_id = self.storage.upsert_document(document, run_id=run_id)
            self._diagnose_document_url(run_id, session_key, bill, document_id, document)
            return
        if entity_set == "FloorLetters":
            document = floor_letter_document(raw)
            document.update(bill_id=int(bill["id"]), source_presence="active")
            document_id = self.storage.upsert_document(document, run_id=run_id)
            self._diagnose_document_url(run_id, session_key, bill, document_id, document)
            return
        raise ValueError(f"Unsupported historical entity set: {entity_set}")

    # -- narrow OLIS reconciliation ---------------------------------------

    def _reconcile_session_display(
        self,
        run_id: int,
        session_key: str,
        session_item: int,
        *,
        force_full: bool = False,
        authoritative_entities: Iterable[str] = (),
    ) -> dict[str, Any]:
        authoritative_entities = frozenset(authoritative_entities)
        self.runs.begin_stage(
            run_id,
            "reconcile_olis_display",
            f"{session_key}: reconciling candidate OLIS testimony pages",
            item_key=f"{session_key}:reconcile_olis_display",
            session_key=session_key,
        )
        counts: Counter[str] = Counter()
        failures: list[dict[str, Any]] = []
        material_gap = False
        observed_statuses: Counter[str] = Counter()
        consecutive_retryable_page_failures = 0
        committee_names = self._committee_names(session_key)
        for bill in self._iter_session_bills(session_key):
            self._check_control(run_id)
            bill_id = int(bill["id"])
            compact = str(bill["bill_id_compact"])
            public_rows = self._raw_document_rows(
                bill_id, "CommitteePublicTestimony", active_only=True
            )
            committee_rows = self._raw_document_rows(
                bill_id, "CommitteeMeetingDocument", active_only=True
            )
            agenda_rows = self._raw_agenda_rows(
                bill_id,
                current_run_id=(
                    run_id
                    if "CommitteeAgendaItems" in authoritative_entities
                    else None
                ),
            )
            candidate = testimony_reconciliation_candidate(
                public_testimony_rows=public_rows,
                committee_document_rows=committee_rows,
                agenda_item_rows=agenda_rows,
            )
            if not candidate.candidate:
                if hasattr(self.storage, "resolve_source_anomalies_for_bill"):
                    # A successful authoritative source refresh can make a
                    # formerly display-capable bill no longer applicable. Its
                    # prior page/parser discrepancies describe stale current
                    # state and must not keep the session incomplete forever.
                    self.storage.resolve_source_anomalies_for_bill(
                        bill_id,
                        anomaly_types=CURRENT_DISPLAY_ANOMALIES,
                    )
                for source_entity_type in DISPLAY_RECONCILIATION_FAMILIES:
                    self.storage.record_olis_display_reconciliation(
                        bill_id,
                        "not_applicable",
                        source_entity_type=source_entity_type,
                        run_id=run_id,
                        source_url=self.olis_http.testimony_url(session_key, compact),
                        details={"candidate_reasons": []},
                    )
                observed_statuses["not_applicable"] += 1
                counts["olis_not_applicable"] += 1
                continue

            reusable_status = (
                None
                if force_full
                else self._reusable_display_status(run_id, bill_id)
            )
            if reusable_status is not None:
                observed_statuses[reusable_status] += 1
                counts["olis_pages_reused"] += 1
                processed = sum(observed_statuses.values())
                self.runs.update_progress(
                    run_id,
                    session_item,
                    processed,
                    (
                        f"{session_key}: processed {processed} OLIS candidate state(s); "
                        f"reused {counts['olis_pages_reused']} unchanged check(s)"
                    ),
                )
                continue

            page_url = self.olis_http.testimony_url(session_key, compact)
            checked_at = _utc_now()
            pause_after_page_failure = False
            try:
                response = _call_with_optional_cancellation(
                    self.olis_http.get_testimony_page,
                    session_key,
                    compact,
                    cancellation_requested=lambda: self.runs.should_abort_active_work(
                        run_id
                    ),
                )
                parsed = inspect_testimony_page(
                    response.text,
                    page_url=response.url,
                    expected_session=session_key,
                )
                # A returned and parsed response proves connectivity even when
                # its markup is anomalous. Parser anomalies are diagnosed by
                # reconciliation and must not trip the source-outage breaker.
                consecutive_retryable_page_failures = 0
                check = DisplayCheck.from_parse_result(parsed, checked_at=checked_at)
                self.storage.record_source_fetch(
                    source_kind="olis_html",
                    source_url=response.url,
                    run_id=run_id,
                    run_item_id=session_item,
                    completed_at=checked_at,
                    succeeded=True,
                    http_status=response.status_code,
                    item_count=len(parsed.documents),
                )
                if parsed.successful:
                    self.runs.resolve_source_errors(
                        run_id,
                        stage="reconcile_olis_display",
                        session_key=session_key,
                        bill_id_compact=compact,
                    )
            except HistoricalRunControl:
                raise
            except Exception as exc:
                self._check_control(run_id)
                check = DisplayCheck.failed(exc, checked_at=checked_at)
                retryable = _retryable(exc)
                if retryable:
                    consecutive_retryable_page_failures += 1
                    pause_after_page_failure = (
                        consecutive_retryable_page_failures
                        >= MAX_CONSECUTIVE_RETRYABLE_OLIS_FAILURES
                    )
                else:
                    consecutive_retryable_page_failures = 0
                self.storage.record_source_fetch(
                    source_kind="olis_html",
                    source_url=page_url,
                    run_id=run_id,
                    run_item_id=session_item,
                    completed_at=checked_at,
                    succeeded=False,
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:2000],
                )
                self.runs.record_error(
                    run_id,
                    run_item_id=session_item,
                    stage="reconcile_olis_display",
                    error=exc,
                    retryable=retryable,
                    session_key=session_key,
                    bill_id_compact=compact,
                    source_url=page_url,
                )
                failures.append(
                    {
                        "bill": compact,
                        "stage": "reconcile_olis_display",
                        "error": str(exc),
                        "retryable": retryable,
                    }
                )

            public_result = reconcile_modern_public_testimony(
                public_rows,
                check,
                committees=committee_names,
            )
            presentation_result = reconcile_historical_presentations(
                committee_rows,
                check,
                committee_names=committee_names,
            )
            if check.complete and hasattr(
                self.storage, "resolve_source_anomalies_for_bill"
            ):
                # Display discrepancies describe the latest successfully parsed
                # page, rather than immutable historical facts. Clear the prior
                # current-state set before persisting any discrepancies observed
                # in this response so resolved mismatches do not remain open and
                # recurring mismatches are immediately re-recorded.
                self.storage.resolve_source_anomalies_for_bill(
                    bill_id,
                    anomaly_types=CURRENT_DISPLAY_ANOMALIES,
                )

            family_results = (
                ("CommitteePublicTestimony", public_result),
                ("CommitteeMeetingDocument", presentation_result),
            )
            # One bounded OLIS page is the persistence unit, matching the
            # OData page contract above.  Storage and RunStore join this outer
            # transaction, avoiding thousands of connection/commit cycles for
            # high-testimony bills while also preventing a half-reconciled page
            # from becoming reusable after a crash.
            with self.database.transaction():
                for source_entity_type, result in family_results:
                    self._persist_reconciled_result(
                        run_id,
                        session_key,
                        bill,
                        result,
                        observed_at=checked_at,
                    )
                    material_gap = material_gap or result.material_completeness_gap
                    self.storage.record_olis_display_reconciliation(
                        bill_id,
                        run_id=run_id,
                        checked_at=checked_at,
                        source_url=page_url,
                        **result.storage_display_values(source_entity_type),
                    )
            observed_statuses[check.status] += 1
            if check.status in {"checked_with_records", "checked_zero"}:
                counts["olis_pages_checked"] += 1
            elif check.status == "parser_anomalous":
                counts["olis_pages_anomalous"] += 1
            else:
                counts["olis_pages_failed"] += 1
            counts["olis_page_only"] += sum(
                result.page_only_count for _source_type, result in family_results
            )
            counts["odata_only"] += sum(
                result.odata_only_count for _source_type, result in family_results
            )
            self.runs.update_progress(
                run_id,
                session_item,
                sum(observed_statuses.values()),
                f"{session_key}: checked {sum(observed_statuses.values())} OLIS candidate state(s)",
            )
            if pause_after_page_failure:
                activity = (
                    "Paused after "
                    f"{MAX_CONSECUTIVE_RETRYABLE_OLIS_FAILURES} consecutive retryable "
                    "OLIS page failures; resume when source access is healthy"
                )
                if self.runs.pause(run_id, activity):
                    raise HistoricalRunControl("paused")
                self._check_control(run_id)

        display_status = _aggregate_display_status(observed_statuses)
        return {
            "counts": counts,
            "failures": failures,
            "material_gap": material_gap,
            "display_status": display_status,
        }

    def _persist_reconciled_result(
        self,
        run_id: int,
        session_key: str,
        bill: Mapping[str, Any],
        result: DocumentReconciliationResult,
        *,
        observed_at: str,
    ) -> None:
        for normalized in result.documents:
            document = dict(normalized)
            document["bill_id"] = int(bill["id"])
            # A failed/anomalous response is not negative display evidence.  Do
            # not erase a prior successful reconciliation while recording the
            # failed attempt itself in its dedicated state table.
            if document.get("displayed_in_olis") is None:
                document.pop("displayed_in_olis", None)
                document.pop("display_reconciled_at", None)
            if (
                document.get("document_kind") in DEFAULT_ARCHIVE_KINDS
                and not document.get("canonical_download_url")
            ):
                document["download_status"] = "not_applicable"
            # These normalized rows are the result of the page observation whose
            # check timestamp is persisted beside them.  Using a later wall-clock
            # value would make the page appear stale immediately and defeat
            # restart reuse for the same durable run.
            document_id = self.storage.upsert_document(
                document,
                run_id=run_id,
                seen_at=observed_at,
            )
            self._diagnose_document_url(
                run_id, session_key, bill, document_id, document
            )
        for anomaly in result.anomalies:
            self.storage.record_source_anomaly(
                session_key=session_key,
                bill_id=int(bill["id"]),
                bill_id_compact=str(bill["bill_id_compact"]),
                run_id=run_id,
                **anomaly.as_record(),
            )
            if anomaly.material_to_completeness:
                self.runs.record_error(
                    run_id,
                    stage="reconcile_olis_display",
                    error=anomaly.message,
                    retryable=True,
                    session_key=session_key,
                    bill_id_compact=str(bill["bill_id_compact"]),
                    source_entity_type=anomaly.source_entity_type,
                    source_id=anomaly.source_id,
                )

    # -- optional bounded probes ------------------------------------------

    def _probe_session(
        self, run_id: int, session_key: str, session_item: int
    ) -> Counter[str]:
        self.runs.begin_stage(
            run_id,
            "probe_documents",
            f"{session_key}: probing eligible remote payload sizes",
            item_key=f"{session_key}:probe_documents",
            session_key=session_key,
        )
        counts: Counter[str] = Counter()
        last_id = 0
        while True:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id,canonical_download_url FROM documents
                    WHERE session_key=? AND id>? AND source_presence!='missing'
                      AND document_kind IN ('public_testimony','legacy_testimony','committee_presentation','floor_letter')
                      AND NULLIF(trim(canonical_download_url),'') IS NOT NULL
                    ORDER BY id LIMIT 100
                    """,
                    (session_key, last_id),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                self._check_control(run_id)
                document_id = int(row["id"])
                last_id = document_id
                try:
                    result = _call_with_optional_cancellation(
                        self.size_probe.probe,
                        str(row["canonical_download_url"]),
                        cancellation_requested=lambda: self.runs.should_abort_active_work(
                            run_id
                        ),
                    )
                except Exception as exc:
                    self._check_control(run_id)
                    counts["probes_failed"] += 1
                    self.storage.record_document_probe(
                        document_id,
                        status="failed",
                        run_id=run_id,
                        error_class=type(exc).__name__,
                        error_message=str(exc)[:2000],
                    )
                    self.runs.record_error(
                        run_id,
                        run_item_id=session_item,
                        stage="probe_documents",
                        error=exc,
                        retryable=_retryable(exc),
                        session_key=session_key,
                        document_id=document_id,
                        source_url=str(row["canonical_download_url"]),
                    )
                    continue
                counts["probes_known" if result.size_known else "probes_unknown"] += 1
                counts["known_remote_bytes"] += int(result.content_length or 0)
                self.storage.record_document_probe(
                    document_id,
                    status=result.status,
                    run_id=run_id,
                    http_status=result.http_status,
                    final_url=result.final_url,
                    content_type=result.content_type,
                    content_length=result.content_length,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    details={"method": result.method, "source_url": result.source_url},
                )
            self.runs.update_progress(
                run_id,
                session_item,
                counts["probes_known"] + counts["probes_unknown"] + counts["probes_failed"],
                f"{session_key}: probed {counts['probes_known'] + counts['probes_unknown'] + counts['probes_failed']} document(s)",
            )
        return counts

    # -- source validation and diagnostics --------------------------------

    def _validate_metadata_contract(
        self, run_id: int, session_keys: tuple[str, ...]
    ) -> bool:
        getter = getattr(self.odata, "get_metadata_xml", None)
        if getter is None:
            return False
        self.runs.begin_stage(
            run_id,
            "resolve_sessions",
            "Validating the official OData metadata contract",
        )
        try:
            xml, _headers, final_url = _call_with_optional_cancellation(
                getter,
                cancellation_requested=lambda: self.runs.should_abort_active_work(
                    run_id
                ),
            )
            report = validate_odata_metadata(xml)
            self.storage.record_source_fetch(
                source_kind="odata_metadata",
                source_url=final_url,
                run_id=run_id,
                entity_set="$metadata",
                completed_at=_utc_now(),
                succeeded=True,
                http_status=200,
                item_count=len(report.properties_by_entity_set),
            )
            for session_key in session_keys:
                self.storage.resolve_source_anomalies_for_session(
                    session_key,
                    anomaly_types=(
                        "metadata_validation_failed",
                        "metadata_parse_error",
                        "missing_entity_set",
                        "missing_property",
                    ),
                )
        except HistoricalRunControl:
            raise
        except Exception as exc:
            self._check_control(run_id)
            self.runs.record_error(
                run_id,
                stage="resolve_sessions",
                error=exc,
                retryable=_retryable(exc),
                source_entity_type="$metadata",
            )
            for session_key in session_keys:
                self.storage.record_source_anomaly(
                    "metadata_validation_failed",
                    severity="error",
                    affects_completeness=True,
                    message="The live OData metadata contract could not be validated.",
                    session_key=session_key,
                    source_entity_type="$metadata",
                    run_id=run_id,
                    details={"error": str(exc)},
                )
            return True
        for issue in report.issues:
            if issue.material_to_completeness:
                self.runs.record_error(
                    run_id,
                    stage="resolve_sessions",
                    error=issue.message,
                    retryable=False,
                    source_entity_type=issue.entity_set,
                    source_id=issue.property_name,
                )
            for session_key in session_keys:
                self.storage.record_source_anomaly(
                    issue.issue_type,
                    severity="error" if issue.material_to_completeness else "warning",
                    affects_completeness=issue.material_to_completeness,
                    message=issue.message,
                    session_key=session_key,
                    source_entity_type=issue.entity_set,
                    source_id=issue.property_name,
                    run_id=run_id,
                )
        return not report.compatible

    def _diagnose_document_url(
        self,
        run_id: int,
        session_key: str,
        bill: Mapping[str, Any],
        document_id: int,
        document: Mapping[str, Any],
    ) -> None:
        kind = str(document.get("document_kind") or "")
        url = text(document.get("canonical_download_url"))
        if kind in DEFAULT_ARCHIVE_KINDS and not url:
            self.storage.record_source_anomaly(
                "missing_official_download_url",
                severity="warning",
                affects_completeness=False,
                message="A normally downloadable document had no official download URL.",
                session_key=session_key,
                bill_id=int(bill["id"]),
                document_id=document_id,
                run_id=run_id,
            )
            return
        if not url:
            return
        parsed = urlsplit(url)
        try:
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError:
            port = -1
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold().rstrip(".")
            not in DEFAULT_ALLOWED_DOWNLOAD_HOSTS
            or port != 443
            or parsed.username is not None
            or parsed.password is not None
        ):
            self.storage.record_source_anomaly(
                "unexpected_source_host",
                severity="error",
                affects_completeness=False,
                message="An official record supplied a download URL outside the configured trusted HTTPS hosts.",
                session_key=session_key,
                bill_id=int(bill["id"]),
                document_id=document_id,
                source_url=url,
                run_id=run_id,
            )

    # -- bounded read helpers ---------------------------------------------

    def _bill_from_source_row(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        session_key = normalize_session_key(str(raw["SessionKey"]))
        try:
            prefix, number, compact, _ = normalize_bill_id(
                f"{raw.get('MeasurePrefix') or ''}{raw.get('MeasureNumber') or ''}"
            )
        except ValueError as exc:
            raise ValueError(
                "Historical child row had no valid supported measure identity"
            ) from exc
        if self._cached_session == session_key and self._cached_bills is None:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM bills WHERE session_key=?",
                    (session_key,),
                ).fetchall()
            self._cached_bills = {
                str(row["bill_id_compact"]): dict(row) for row in rows
            }
        bill = (
            (self._cached_bills or {}).get(compact)
            if self._cached_session == session_key
            else self.storage.get_bill(session_key, compact)
        )
        if bill is None:
            raise ValueError(
                f"{session_key}/{prefix}{number} child row had no persisted measure"
            )
        return bill

    def _legislator_names(self, session_key: str) -> dict[str, str]:
        if self._cached_session == session_key and self._cached_legislator_names is not None:
            return self._cached_legislator_names
        values = {
            str(row["legislator_code"]): (
                text(row.get("display_name"))
                or " ".join(
                    value
                    for value in (text(row.get("first_name")), text(row.get("last_name")))
                    if value
                )
            )
            for row in self.storage.list_legislators(session_key)
        }
        if self._cached_session == session_key:
            self._cached_legislator_names = values
        return values

    def _committee_names(self, session_key: str) -> dict[str, str]:
        if self._cached_session == session_key and self._cached_committee_names is not None:
            return self._cached_committee_names
        values = {
            str(row["committee_code"]): committee_display(
                {
                    "CommitteeType": row.get("committee_type"),
                    "CommitteeName": row.get("committee_name"),
                    "CommitteeCode": row.get("committee_code"),
                }
            )
            for row in self.storage.list_committees(session_key)
        }
        if self._cached_session == session_key:
            self._cached_committee_names = values
        return values

    def _committee_ids(self, session_key: str) -> dict[str, int]:
        if self._cached_session == session_key and self._cached_committee_ids is not None:
            return self._cached_committee_ids
        values = {
            str(row["committee_code"]): int(row["id"])
            for row in self.storage.list_committees(session_key)
        }
        if self._cached_session == session_key:
            self._cached_committee_ids = values
        return values

    def _meeting_id(self, session_key: str, code: str, meeting_date: str) -> int | None:
        if not code or not meeting_date:
            return None
        if self._cached_session == session_key and self._cached_meeting_ids is None:
            with self.database.connection() as connection:
                rows = connection.execute(
                    "SELECT id,source_meeting_id FROM committee_meetings WHERE session_key=?",
                    (session_key,),
                ).fetchall()
            self._cached_meeting_ids = {
                str(row["source_meeting_id"]): int(row["id"]) for row in rows
            }
        key = f"{code}|{meeting_date}"
        if self._cached_session == session_key:
            return (self._cached_meeting_ids or {}).get(key)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM committee_meetings WHERE session_key=? AND source_meeting_id=?",
                (session_key, key),
            ).fetchone()
        return int(row["id"]) if row else None

    def _reset_session_cache(self, session_key: str) -> None:
        self._cached_session = session_key
        self._cached_legislator_names = None
        self._cached_committee_names = None
        self._cached_committee_ids = None
        self._cached_meeting_ids = None
        self._cached_bills = None

    def _iter_session_bills(self, session_key: str):  # noqa: ANN202
        last_id = 0
        while True:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM bills
                    WHERE session_key=? AND source_presence!='missing' AND id>?
                    ORDER BY id LIMIT 100
                    """,
                    (session_key, last_id),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last_id = int(row["id"])
                yield dict(row)

    def _raw_document_rows(
        self, bill_id: int, source_entity_type: str, *, active_only: bool
    ) -> list[dict[str, Any]]:
        # Only active structured rows feed the OData side of reconciliation.
        # OLIS-only rows are retained as source_presence=unknown but must not be
        # reinterpreted as OData on the next run.
        where = " AND source_presence='active'" if active_only else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT raw_json FROM documents WHERE bill_id=? AND source_entity_type=?{where} ORDER BY id",
                (bill_id, source_entity_type),
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def _raw_agenda_rows(
        self, bill_id: int, *, current_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        where = " AND last_seen_run_id=?" if current_run_id is not None else ""
        params: tuple[int, ...] = (
            (bill_id, int(current_run_id))
            if current_run_id is not None
            else (bill_id,)
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT raw_json FROM committee_agenda_items WHERE bill_id=?{where} ORDER BY id",
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def _reusable_display_status(self, run_id: int, bill_id: int) -> str | None:
        """Return a prior successful page state when no candidate input changed."""

        del run_id  # Reuse is governed by observation order, including same-run resume.
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT source_entity_type,status,checked_at
                FROM olis_display_reconciliations
                WHERE bill_id=?
                """,
                (bill_id,),
            ).fetchall()
            statuses = {
                str(row["source_entity_type"]): str(row["status"])
                for row in rows
            }
            successful = {"checked_with_records", "checked_zero"}
            if (
                set(statuses) != set(DISPLAY_RECONCILIATION_FAMILIES)
                or any(status not in successful for status in statuses.values())
            ):
                return None

            # Run IDs alone cannot distinguish the initial pass from a resume of
            # that same durable run: every source row still carries the same run
            # ID and would force thousands of already-successful HTML requests.
            # Local observation timestamps provide the required ordering.  A
            # page is reusable only when both family checks happened at or after
            # every measure/document/agenda input that can affect reconciliation.
            checked_at = min(str(row["checked_at"]) for row in rows)
            changed = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM bills
                    WHERE id=? AND (
                        last_synced_at > ?
                        OR COALESCE(last_source_reconciled_at, '') > ?
                    )
                    UNION ALL
                    SELECT 1 FROM documents
                    JOIN olis_display_reconciliations input_check
                      ON input_check.bill_id=documents.bill_id
                     AND input_check.source_entity_type=documents.source_entity_type
                    WHERE documents.bill_id=?
                      AND documents.source_entity_type IN (
                          'CommitteePublicTestimony','CommitteeMeetingDocument'
                      )
                      AND (
                          (
                              documents.source_presence='active'
                              AND (
                                  documents.display_reconciled_at IS NULL
                                  OR documents.display_reconciled_at < input_check.checked_at
                              )
                          )
                          OR COALESCE(documents.last_source_reconciled_at, '')
                             > input_check.checked_at
                      )
                    UNION ALL
                    SELECT 1 FROM committee_agenda_items
                    WHERE bill_id=? AND last_seen_at > ?
                )
                """,
                (
                    bill_id,
                    checked_at,
                    checked_at,
                    bill_id,
                    bill_id,
                    checked_at,
                ),
            ).fetchone()[0]
        if changed:
            return None
        return (
            "checked_with_records"
            if "checked_with_records" in statuses.values()
            else "checked_zero"
        )

    def _session_inventory_counts(self, session_key: str) -> dict[str, int]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM bills WHERE session_key=? AND source_presence!='missing') AS bills,
                    (SELECT COUNT(*) FROM documents WHERE session_key=? AND source_presence!='missing') AS documents
                """,
                (session_key, session_key),
            ).fetchone()
        return {"bills": int(row["bills"]), "documents": int(row["documents"])}

    def _unresolved_material_anomaly_count(self, session_key: str) -> int:
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_anomalies
                    WHERE session_key=? AND affects_completeness=1 AND resolved_at IS NULL
                    """,
                    (session_key,),
                ).fetchone()[0]
            )

    def _completed_session_items(self, run_id: int) -> set[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT session_key FROM collection_run_items
                WHERE run_id=? AND item_type='session' AND status='completed'
                """,
                (run_id,),
            ).fetchall()
        return {str(row["session_key"]) for row in rows if row["session_key"]}

    def _download_session_scope(
        self, session_keys: Iterable[str] | None
    ) -> tuple[str, ...]:
        if session_keys is None:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT s.session_key
                    FROM sessions s
                    JOIN session_archive_state a ON a.session_key=s.session_key
                    WHERE a.inventory_status!='not_started'
                    ORDER BY s.begin_date,s.session_key
                    """
                ).fetchall()
            supported: list[str] = []
            for row in rows:
                try:
                    supported.append(
                        require_supported_session_key(str(row["session_key"]))
                    )
                except ValueError:
                    continue
            sessions = tuple(supported)
        else:
            sessions = tuple(
                dict.fromkeys(
                    require_supported_session_key(value) for value in session_keys
                )
            )
        if not sessions:
            raise ValueError("No inventoried sessions were selected")
        with self.database.connection() as connection:
            marks = ",".join("?" for _ in sessions)
            found = {
                str(row["session_key"]): (
                    str(row["inventory_status"])
                    if row["inventory_status"] is not None
                    else "not_started"
                )
                for row in connection.execute(
                    f"""
                    SELECT s.session_key,a.inventory_status
                    FROM sessions s
                    LEFT JOIN session_archive_state a ON a.session_key=s.session_key
                    WHERE s.session_key IN ({marks})
                    """,
                    sessions,
                ).fetchall()
            }
        missing = [key for key in sessions if key not in found]
        if missing:
            raise ValueError("Unknown session selection: " + ", ".join(missing))
        not_inventoried = [
            key for key in sessions if found.get(key) == "not_started"
        ]
        if not_inventoried:
            raise ValueError(
                "Session selection has not been inventoried: "
                + ", ".join(not_inventoried)
            )
        return sessions

    def _download_item_counts(
        self, run_id: int, *, session_key: str | None = None
    ) -> dict[str, int]:
        params: list[Any] = [run_id]
        where = "run_id=? AND item_type='document'"
        if session_key:
            where += " AND session_key=?"
            params.append(session_key)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT status,COUNT(*) AS count FROM collection_run_items WHERE {where} GROUP BY status",
                params,
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _audit_downloaded_scope(
        self,
        run_id: int,
        sessions: tuple[str, ...],
        kinds: tuple[str, ...],
        *,
        scope_cutoff_at: str,
        progress_item_id: int,
    ) -> Counter[str]:
        """Boundedly verify current payloads recorded as downloaded.

        A durable ``downloaded`` flag is evidence of the last successful
        promotion, not permission to assume a file can never be removed or
        corrupted later.  This audit hashes/validates each selected current
        payload and also compares its source timestamp with the retained
        version.  It runs in the background only after an explicit Download
        Archive start, never while rendering the preflight page.  Valid files
        receive durable skipped items; recoverable files enter the normal
        database-backed claim path.
        """

        if not scope_cutoff_at:
            raise ValueError("Download Archive payload audit has no frozen scope cutoff")
        session_marks = ",".join("?" for _ in sessions)
        kind_marks = ",".join("?" for _ in kinds)
        counters: Counter[str] = Counter()
        inspected = 0
        with self.database.connection() as connection:
            audit_total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM documents d
                    LEFT JOIN collection_run_items i
                      ON i.run_id=? AND i.item_type='document' AND i.document_id=d.id
                    WHERE d.session_key IN ({session_marks})
                      AND d.document_kind IN ({kind_marks})
                      AND d.first_seen_at<=?
                      AND d.source_presence!='missing'
                      AND d.download_status='downloaded'
                      AND (i.id IS NULL OR i.status NOT IN ('completed','skipped'))
                    """,
                    (run_id, *sessions, *kinds, scope_cutoff_at),
                ).fetchone()[0]
            )
        self.runs.update_progress(
            run_id,
            progress_item_id,
            0,
            f"Validating {audit_total} existing current payload(s)",
            total=audit_total,
        )
        last_id = 0
        while True:
            with self.database.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT d.*,
                           v.source_modified_at AS current_payload_source_modified_at,
                           p.content_length AS probe_content_length
                    FROM documents d
                    LEFT JOIN document_versions v ON v.id=d.current_version_id
                    LEFT JOIN document_remote_probes p ON p.document_id=d.id
                    LEFT JOIN collection_run_items i
                      ON i.run_id=? AND i.item_type='document' AND i.document_id=d.id
                    WHERE d.session_key IN ({session_marks})
                      AND d.document_kind IN ({kind_marks})
                      AND d.first_seen_at<=?
                      AND d.source_presence!='missing'
                      AND d.download_status='downloaded' AND d.id>?
                      AND (i.id IS NULL OR i.status NOT IN ('completed','skipped'))
                    ORDER BY d.id LIMIT 100
                    """,
                    (run_id, *sessions, *kinds, scope_cutoff_at, last_id),
                ).fetchall()
            if not rows:
                break
            transitions: list[tuple[int, str, str | None]] = []
            for row in rows:
                self._check_control(run_id)
                document = dict(row)
                last_id = int(document["id"])
                inspected += 1
                source_modified = text(document.get("source_modified_at"))
                version_source_modified = text(
                    document.get("current_payload_source_modified_at")
                )
                source_changed = bool(
                    source_modified and source_modified != version_source_modified
                )
                locally_valid = False if source_changed else self._valid_current_payload(document)
                if not source_changed and locally_valid:
                    counters["valid_downloaded"] += 1
                    self.runs.record_archive_document_skip(
                        run_id,
                        document_id=int(document["id"]),
                        bill_id=int(document["bill_id"]),
                        session_key=str(document["session_key"]),
                    )
                    continue
                has_url = bool(text(document.get("canonical_download_url")))
                counters[
                    "invalid_with_url" if has_url else "invalid_without_url"
                ] += 1
                if has_url:
                    probe_length = document.get("probe_content_length")
                    if probe_length is None:
                        counters["invalid_unknown_size"] += 1
                    else:
                        counters["invalid_known_bytes"] += int(probe_length)
                status = (
                    "failed_terminal"
                    if not has_url
                    else "changed_remote" if source_changed else "missing_local"
                )
                message = (
                    "Previously downloaded file is missing or invalid and no official "
                    "download URL is available"
                    if not has_url
                    else None if source_changed
                    else "Previously downloaded file is missing or invalid"
                )
                transitions.append(
                    (
                        int(document["id"]),
                        status,
                        message,
                    )
                )
                if not has_url:
                    item_id = self.runs.record_archive_document_failure(
                        run_id,
                        document_id=int(document["id"]),
                        bill_id=int(document["bill_id"]),
                        session_key=str(document["session_key"]),
                        message=message or "No official download URL is available",
                        retryable=False,
                    )
                    self.runs.record_error(
                        run_id,
                        stage="download_archive",
                        error=message or "No official download URL is available",
                        retryable=False,
                        session_key=str(document["session_key"]),
                        bill_id_compact=text(document.get("bill_id_compact")),
                        source_entity_type=text(document.get("source_entity_type")),
                        source_id=text(document.get("source_id")),
                        document_id=int(document["id"]),
                        run_item_id=item_id,
                        details={"payload_audit": "invalid_without_recovery_url"},
                    )
            if transitions:
                with self.database.transaction() as connection:
                    connection.executemany(
                        """
                        UPDATE documents SET download_status=?,last_error=?
                        WHERE id=? AND download_status='downloaded'
                        """,
                        ((status, message, document_id) for document_id, status, message in transitions),
                    )
            self.runs.update_progress(
                run_id,
                progress_item_id,
                inspected,
                (
                    "Validated existing payloads and prepared missing/changed "
                    f"files ({counters['valid_downloaded']} valid, "
                    f"{counters['invalid_with_url']} recoverable, "
                    f"{counters['invalid_without_url']} unavailable)"
                ),
                total=audit_total,
            )
        return counters

    def _valid_current_payload(self, document: Mapping[str, Any]) -> bool:
        relative = text(document.get("local_relative_path"))
        if not relative:
            return False
        try:
            path = resolve_stored_path(self.config.archive_root, relative)
            expected_length = document.get("downloaded_bytes")
            result = validate_file(
                path,
                text(document.get("mime_type")) or "",
                int(expected_length) if expected_length is not None else None,
                expected_sha256=text(document.get("sha256")) or "",
                logical_filename=text(document.get("local_filename")) or path.name,
            )
            return result.valid
        except (OSError, TypeError, ValueError):
            return False

    def _entity_url(self, plan: SessionEntityPlan) -> str:
        builder = getattr(self.odata, "build_url", None)
        if builder is None:
            return f"{plan.spec.entity_set}?{plan.filter_expression}"
        return str(builder(plan.spec.entity_set, **plan.request_params))

    def _check_control(self, run_id: int) -> None:
        status = self.runs.status(run_id)
        if status in {"paused", "canceled", "interrupted"}:
            raise HistoricalRunControl(str(status))


def _call_with_optional_cancellation(
    function: Callable[..., Any],
    *args: Any,
    cancellation_requested: Callable[[], bool],
) -> Any:
    """Use cooperative run control only with explicitly compatible clients.

    Phase 1 adapters and fixture clients may expose ``**kwargs`` for source
    parameters without understanding a control callback. Requiring an explicit
    named parameter avoids breaking those callers or leaking the callback into
    a request query.
    """

    try:
        supports_control = "cancellation_requested" in inspect.signature(
            function
        ).parameters
    except (TypeError, ValueError):
        supports_control = False
    if supports_control:
        return function(
            *args,
            cancellation_requested=cancellation_requested,
        )
    return function(*args)


def _document_kinds(values: Iterable[str] | None) -> tuple[str, ...]:
    kinds = tuple(dict.fromkeys(str(value).strip() for value in (values or DEFAULT_ARCHIVE_KINDS)))
    if not kinds:
        raise ValueError("At least one document kind is required")
    invalid = set(kinds) - DOCUMENT_KINDS
    if invalid:
        raise ValueError("Unsupported document kinds: " + ", ".join(sorted(invalid)))
    return kinds


def _count_name(entity_set: str) -> str:
    return {
        "Legislators": "legislators",
        "Committees": "committees",
        "Measures": "measures_returned",
        "MeasureSponsors": "sponsors",
        "CommitteeMeetings": "committee_meetings",
        "CommitteeAgendaItems": "agenda_items",
        "CommitteeMeetingDocuments": "committee_documents",
        "CommitteePublicTestimonies": "public_testimony",
        "FloorLetters": "floor_letters",
    }[entity_set]


def _aggregate_display_status(statuses: Mapping[str, int]) -> str:
    if statuses.get("failed_fetch"):
        return "failed_fetch"
    if statuses.get("parser_anomalous"):
        return "parser_anomalous"
    if statuses.get("checked_with_records"):
        return "checked_with_records"
    if statuses.get("checked_zero"):
        return "checked_zero"
    return "not_applicable"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _retryable(error: BaseException) -> bool:
    return bool(
        getattr(error, "retryable", False)
        or isinstance(error, (TimeoutError, OSError))
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "DEFAULT_ARCHIVE_KINDS",
    "DownloadPreflight",
    "HistoricalCollectionService",
    "HistoricalRunControl",
    "INVENTORY_ENTITY_ORDER",
]
