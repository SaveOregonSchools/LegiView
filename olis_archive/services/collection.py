"""Phase 1 bill/session collection orchestration shared by CLI and Flask."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import logging
import threading
import time
from typing import Any, Iterable, Mapping

from ..config import AppConfig, DEFAULT_ALLOWED_DOWNLOAD_HOSTS
from ..database import Database
from .archive_paths import archive_document_path, resolve_stored_path, sanitize_windows_filename
from .documents import (
    committee_document,
    floor_letter_document,
    html_testimony_document,
    public_testimony_documents,
)
from .downloads import (
    DownloadError,
    DownloadInterrupted,
    DownloadResult,
    Downloader,
    LowDiskSpace,
    RetryPolicy,
    SafeHTTPClient,
    retry_delay_seconds,
)
from .file_types import validate_file
from .historical_collection import HistoricalCollectionService, HistoricalRunControl
from .historical_sources import SessionScope, require_supported_session_key
from .odata import (
    ODataClient,
    odata_datetime_literal,
    odata_literal,
)
from .olis_http import OLISHTTPClient
from .probes import RemoteSizeProbe
from .record_mapping import (
    committee_display as map_committee_display,
    map_committee as map_committee_record,
    map_legislator as map_legislator_record,
    map_meeting as map_meeting_record,
    map_session as map_session_record,
)
from .runs import RunStore
from .source_mapping import (
    chamber_for_code,
    map_measure,
    map_sponsor,
    normalize_bill_id,
    normalize_session_key,
)
from .storage import (
    MATCHING_RETRY_DOWNLOAD_STATUSES,
    RETRY_PAYLOAD_DOCUMENT_KINDS,
    StorageService,
)
from .testimony_parser import ParsedTestimonyDocument, parse_testimony_page


LOGGER = logging.getLogger(__name__)
IN_SCOPE_DOWNLOAD_KINDS = {
    "public_testimony",
    "legacy_testimony",
    "committee_presentation",
    "floor_letter",
}


class CollectionCanceled(RuntimeError):
    pass


class CollectionSuspended(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__(f"Collection is {status}")
        self.status = status


class _DownloadFinalizationError(RuntimeError):
    """A promoted payload could not be committed to its still-owned DB claim."""

    retryable = True


class SourceRecordNotFound(RuntimeError):
    pass


class SourceRecordMismatch(RuntimeError):
    """The source returned a different measure than the requested identity."""


@dataclass(frozen=True, slots=True)
class BillCollectionResult:
    bill_id: int
    bill_id_compact: str
    documents_discovered: int
    documents_downloaded: int
    documents_skipped: int
    documents_failed: int
    bytes_downloaded: int
    source_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SessionReferenceData:
    """Reference rows loaded and persisted once for one session execution."""

    session_key: str
    legislator_names: Mapping[str, str]
    committee_names: Mapping[str, str]
    committee_ids: Mapping[str, int]


class CollectionService:
    """Coordinates source services, normalized persistence, and file workers."""

    def __init__(
        self,
        config: AppConfig,
        *,
        database: Database | None = None,
        odata: ODataClient | None = None,
        olis_http: OLISHTTPClient | None = None,
        downloader: Downloader | None = None,
        sleep=time.sleep,
    ) -> None:
        self.config = config
        if database is None:
            self.database = Database(config.database_path)
            self.database.initialize()
        else:
            # Shared runtime bootstrap already initialized or verified this
            # database, potentially while holding the process ownership lock.
            self.database = database
        self.storage = StorageService(self.database, initialize=False)
        self.runs = RunStore(self.database)
        self.odata = odata or ODataClient(
            config.odata_base_url,
            timeout=config.request_timeout,
            user_agent=config.user_agent,
            inter_request_delay=config.inter_request_delay,
        )
        self.olis_http = olis_http or OLISHTTPClient(
            config.olis_base_url,
            timeout=config.request_timeout,
            user_agent=config.user_agent,
            concurrency=config.html_request_concurrency,
            inter_request_delay=config.inter_request_delay,
        )
        self.downloader = downloader or Downloader(
            allowed_hosts=DEFAULT_ALLOWED_DOWNLOAD_HOSTS,
            timeout_seconds=config.request_timeout,
            user_agent=config.user_agent,
            minimum_free_space_bytes=config.minimum_free_space_bytes,
            require_https=True,
            allowed_ports={443},
        )
        self.sleep = sleep
        probe_http = getattr(self.downloader, "client", None) or SafeHTTPClient(
            allowed_hosts=DEFAULT_ALLOWED_DOWNLOAD_HOSTS,
            timeout_seconds=config.request_timeout,
            user_agent=config.user_agent,
            require_https=True,
            allowed_ports={443},
        )
        self.historical = HistoricalCollectionService(
            config,
            database=self.database,
            storage=self.storage,
            runs=self.runs,
            odata=self.odata,
            olis_http=self.olis_http,
            size_probe=RemoteSizeProbe(probe_http, sleep=sleep),
            download_claimed=self._download_claimed_document,
        )

    # -- durable run creation ------------------------------------------------

    def create_collect_bill_run(self, session_key: str, bill_id: str) -> int:
        session = require_supported_session_key(session_key)
        _, _, compact, _ = normalize_bill_id(bill_id)
        return self.runs.create_run(
            "collect_bill",
            session_key=session,
            bill_id_compact=compact,
            scope={"session_key": session, "bill_id_compact": compact},
            config_snapshot=self.config.snapshot(),
            bills_total=1,
        )

    def create_collect_session_run(self, session_key: str, *, max_bills: int | None = None) -> int:
        session = require_supported_session_key(session_key)
        if max_bills is not None and not 1 <= int(max_bills) <= 10_000:
            raise ValueError("max_bills must be between 1 and 10000")
        return self.runs.create_run(
            "collect_session",
            session_key=session,
            scope={"session_key": session, "max_bills": max_bills},
            config_snapshot=self.config.snapshot(),
        )

    def create_retry_failures_run(self, source_run_id: int) -> int:
        source = self.runs.get_run(source_run_id)
        if not source:
            raise KeyError(f"Collection run {source_run_id} does not exist")
        if source.get("run_type") in {"inventory_backfill", "download_archive"}:
            raise ValueError(
                "Historical archive retries must use Download Archive with its "
                "frozen session scope and retryable-failures-only filter."
            )
        source_session = source.get("requested_session_key")
        if source_session:
            require_supported_session_key(str(source_session))
        retryable = self.storage.list_documents_for_retry(
            run_id=source_run_id,
            include_terminal=True,
            limit=100_000,
        )
        run_id = self.runs.create_run(
            "retry_failures",
            session_key=source.get("requested_session_key"),
            bill_id_compact=source.get("requested_bill_id_compact"),
            scope={"source_run_id": source_run_id, "document_ids": [row["id"] for row in retryable]},
            config_snapshot=self.config.snapshot(),
            bills_total=0,
        )
        return run_id

    def create_retry_selected_run(
        self,
        document_ids: Iterable[int],
        *,
        source_run_id: int | None = None,
    ) -> int:
        """Validate and freeze an explicit retry selection in the core service."""

        ids = list(dict.fromkeys(int(value) for value in document_ids))
        if not ids:
            raise ValueError("Select at least one document to retry")
        documents: list[dict[str, Any]] = []
        for document_id in ids:
            document = self.storage.get_document(document_id)
            if document is None:
                raise ValueError(f"Document {document_id} no longer exists")
            require_supported_session_key(str(document.get("session_key") or ""))
            if (
                str(document.get("download_status"))
                not in MATCHING_RETRY_DOWNLOAD_STATUSES
                or str(document.get("document_kind"))
                not in RETRY_PAYLOAD_DOCUMENT_KINDS
                or not str(document.get("canonical_download_url") or "").strip()
            ):
                raise ValueError(
                    f"Document {document_id} is no longer eligible for retry"
                )
            documents.append(document)

        sessions = {str(document["session_key"]) for document in documents}
        bills = {str(document["bill_id_compact"]) for document in documents}
        return self.runs.create_run(
            "retry_failures",
            session_key=next(iter(sessions)) if len(sessions) == 1 else None,
            bill_id_compact=next(iter(bills)) if len(bills) == 1 else None,
            scope={"source_run_id": source_run_id, "document_ids": ids},
            config_snapshot=self.config.snapshot(),
            bills_total=0,
        )

    def create_retry_matching_run(
        self,
        *,
        source_run_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
    ) -> tuple[int, int]:
        """Freeze every filtered retry candidate without a JSON ID list."""

        session = require_supported_session_key(session_key) if session_key else None
        if source_run_id is not None:
            source = self.runs.get_run(source_run_id)
            if source and source.get("requested_session_key"):
                require_supported_session_key(str(source["requested_session_key"]))
        bill = (
            bill_id_compact.replace(" ", "").strip().upper()
            if bill_id_compact
            else None
        )
        retry_match = {
            "source_run_id": source_run_id,
            "session_key": session,
            "bill_id_compact": bill,
            "include_terminal": True,
            "eligible_statuses": sorted(MATCHING_RETRY_DOWNLOAD_STATUSES),
        }
        scope = {
            "source_run_id": source_run_id,
            "selection": "all_matching",
            "retry_match": retry_match,
        }
        # BEGIN IMMEDIATE makes the filtered INSERT...SELECT and its run row one
        # exact durable snapshot while keeping the Python scope constant-sized.
        with self.database.transaction() as connection:
            run_id = self.runs.create_run(
                "retry_failures",
                session_key=session,
                bill_id_compact=bill,
                scope=scope,
                config_snapshot=self.config.snapshot(),
                bills_total=0,
            )
            matching_count = self.storage.snapshot_retry_matching_items(
                run_id,
                source_run_id=source_run_id,
                session_key=session,
                bill_id_compact=bill,
                include_terminal=True,
            )
            if matching_count < 1:
                raise ValueError("No failed documents match the selected filters")
            retry_match["matching_count"] = matching_count
            connection.execute(
                "UPDATE collection_runs SET requested_scope_json=? WHERE id=?",
                (
                    json.dumps(
                        scope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                    run_id,
                ),
            )
        return run_id, matching_count

    def historical_session_scope(self):  # noqa: ANN201
        return self.historical.historical_session_scope()

    def create_inventory_backfill_run(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        probe_remote_sizes: bool = False,
        force_full: bool = False,
        resolved_scope: SessionScope | None = None,
    ) -> int:
        return self.historical.create_inventory_backfill_run(
            session_keys,
            probe_remote_sizes=probe_remote_sizes,
            force_full=force_full,
            resolved_scope=resolved_scope,
        )

    def download_archive_preflight(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        document_kinds: Iterable[str] | None = None,
        retryable_failures_only: bool = False,
        missing_pending_only: bool = True,
    ):
        return self.historical.download_preflight(
            session_keys,
            document_kinds=document_kinds,
            retryable_failures_only=retryable_failures_only,
            missing_pending_only=missing_pending_only,
        )

    def create_download_archive_run(
        self,
        session_keys: Iterable[str] | None = None,
        *,
        document_kinds: Iterable[str] | None = None,
        retryable_failures_only: bool = False,
        missing_pending_only: bool = True,
    ) -> int:
        return self.historical.create_download_archive_run(
            session_keys,
            document_kinds=document_kinds,
            retryable_failures_only=retryable_failures_only,
            missing_pending_only=missing_pending_only,
        )

    # -- execution -----------------------------------------------------------

    def execute_run(self, run_id: int, *, _already_claimed: bool = False) -> str:
        if not _already_claimed and not self.runs.claim_run(run_id):
            row = self.runs.get_run(run_id)
            if row is None:
                raise KeyError(f"Collection run {run_id} does not exist")
            return str(row["status"])
        run = self.runs.get_run(run_id)
        if run is None:
            raise KeyError(f"Collection run {run_id} does not exist")
        if run["status"] != "running":
            return str(run["status"])
        try:
            scope = _json_object(run.get("requested_scope_json"))
            self._validate_frozen_run_scope(run, scope)
            if run["run_type"] == "collect_bill":
                result = self.collect_bill(
                    run_id,
                    str(run["requested_session_key"]),
                    str(run["requested_bill_id_compact"]),
                )
                self.runs.set_counters(run_id, bills_completed=1)
                summary = _result_dict(result)
            elif run["run_type"] == "collect_session":
                summary = self.collect_session(
                    run_id,
                    str(run["requested_session_key"]),
                    max_bills=scope.get("max_bills"),
                )
            elif run["run_type"] == "retry_failures":
                retry_match = scope.get("retry_match")
                if isinstance(retry_match, Mapping):
                    summary = self.retry_matching_failures(run_id, retry_match)
                else:
                    summary = self.retry_failures(
                        run_id,
                        [int(value) for value in scope.get("document_ids", [])],
                    )
            elif run["run_type"] == "inventory_backfill":
                summary = self.historical.execute_inventory_backfill(run_id, scope)
            elif run["run_type"] == "download_archive":
                summary = self.historical.execute_download_archive(run_id, scope)
            else:  # constrained by SQL, retained for defensive readability
                raise ValueError(f"Unknown collection run type: {run['run_type']}")
            current_status = self.runs.status(run_id)
            if current_status in {"canceled", "paused", "interrupted"}:
                return current_status
            return self.runs.finish_run(
                run_id,
                summary=summary,
                session_key=(
                    run.get("requested_session_key")
                    if run["run_type"] not in {"inventory_backfill", "download_archive"}
                    else None
                ),
            )
        except CollectionCanceled:
            self.runs.cancel(run_id)
            return "canceled"
        except CollectionSuspended as exc:
            return exc.status
        except HistoricalRunControl as exc:
            if run["run_type"] == "inventory_backfill":
                self.storage.stop_session_inventory_run(run_id, exc.status)
            if exc.status == "canceled":
                self.runs.cancel(run_id)
            return exc.status
        except Exception as exc:
            current = self.runs.get_run(run_id) or run
            current_status = str(current.get("status") or "")
            if current_status in {"canceled", "paused", "interrupted"}:
                # Shutdown, operator cancellation, or low-space pause owns the
                # outcome even if an in-flight source request fails while
                # unwinding. Do not replace a recoverable state with `failed`.
                return current_status
            retryable = _is_retryable(exc)
            self.runs.record_error(
                run_id,
                stage=str(current.get("stage") or "unknown"),
                error=exc,
                retryable=retryable,
                session_key=current.get("requested_session_key"),
                bill_id_compact=current.get("requested_bill_id_compact"),
            )
            self.runs.fail_run(run_id, exc, retryable=retryable)
            LOGGER.exception("Collection run %s failed", run_id)
            return "failed"

    def requeue_run(self, run_id: int) -> bool:
        """Validate a durable scope before making a paused run runnable again."""

        run = self.runs.get_run(run_id)
        if run is None:
            return False
        scope = _json_object(run.get("requested_scope_json"))
        self._validate_frozen_run_scope(run, scope)
        return self.runs.requeue(run_id)

    def _validate_frozen_run_scope(
        self,
        run: Mapping[str, Any],
        scope: Mapping[str, Any],
    ) -> None:
        """Fail closed on legacy durable rows before source or payload work."""

        run_type = str(run.get("run_type") or "")
        requested_session = str(run.get("requested_session_key") or "").strip()
        if requested_session:
            require_supported_session_key(requested_session)
        if run_type in {"inventory_backfill", "download_archive"}:
            for session_key in scope.get("session_keys", ()):
                require_supported_session_key(str(session_key))
        if run_type != "retry_failures":
            return
        for document_id in scope.get("document_ids", ()):
            document = self.storage.get_document(int(document_id))
            if document is None:
                raise ValueError(f"Document {document_id} no longer exists")
            require_supported_session_key(str(document.get("session_key") or ""))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT d.session_key
                FROM collection_run_items i
                JOIN documents d ON d.id=i.document_id
                WHERE i.run_id=? AND i.item_type='document'
                """,
                (int(run["id"]),),
            ).fetchall()
        for row in rows:
            require_supported_session_key(str(row["session_key"]))

    def collect_session(self, run_id: int, session_key: str, *, max_bills: int | None = None) -> dict[str, Any]:
        session_key = require_supported_session_key(session_key)
        self._check_canceled(run_id)
        item_id = self._load_session_record(
            run_id,
            session_key,
            item_key=f"{session_key}:load_session",
        )
        measures = self.odata.get_measures(session_key, max_bills=int(max_bills) if max_bills else None)
        self.runs.update_progress(
            run_id,
            item_id,
            len(measures),
            f"Found {len(measures)} supported legislative measures",
        )
        self.runs.set_counters(run_id, bills_total=len(measures))
        reference_data = self._load_reference_data(
            run_id,
            session_key,
            item_key=f"{session_key}:load_reference_data",
        )
        aggregate = {
            "bills_completed": 0,
            "documents_discovered": 0,
            "documents_downloaded": 0,
            "documents_skipped": 0,
            "documents_failed": 0,
            "bytes_downloaded": 0,
        }
        for index, measure in enumerate(measures, 1):
            self._check_canceled(run_id)
            compact = f"{measure['MeasurePrefix']}{int(measure['MeasureNumber'])}"
            self.runs.begin_stage(
                run_id,
                "load_measure",
                f"Collecting {compact} ({index} of {len(measures)})",
                item_key=f"bill:{compact}",
                item_type="bill",
                progress_total=1,
                session_key=session_key,
            )
            try:
                result = self.collect_bill(
                    run_id,
                    session_key,
                    compact,
                    known_measure=measure,
                    reference_data=reference_data,
                )
            except CollectionCanceled:
                raise
            except CollectionSuspended:
                raise
            except Exception as exc:
                current_status = self.runs.status(run_id)
                if current_status == "canceled":
                    raise CollectionCanceled() from exc
                if current_status in {"paused", "interrupted"}:
                    raise CollectionSuspended(current_status) from exc
                retryable = _is_retryable(exc)
                self.runs.record_error(
                    run_id,
                    stage=str((self.runs.get_run(run_id) or {}).get("stage") or "load_measure"),
                    error=exc,
                    retryable=retryable,
                    session_key=session_key,
                    bill_id_compact=compact,
                )
                self.runs.fail_active_items(run_id, exc, retryable=retryable)
                continue
            aggregate["bills_completed"] += 1
            for key in aggregate:
                if key != "bills_completed":
                    aggregate[key] += int(getattr(result, key))
            self.runs.finish_item(run_id, "bill", f"bill:{compact}")
            self.runs.set_counters(run_id, **aggregate)
        return {"session_key": session_key, **aggregate}

    def collect_bill(
        self,
        run_id: int,
        session_key: str,
        bill_id: str,
        *,
        known_measure: Mapping[str, Any] | None = None,
        reference_data: SessionReferenceData | None = None,
    ) -> BillCollectionResult:
        session_key = require_supported_session_key(session_key)
        prefix, number, compact, _ = normalize_bill_id(bill_id)
        stage_key = lambda stage: f"{compact}:{stage}"  # noqa: E731

        if reference_data is None:
            self._load_session_record(
                run_id,
                session_key,
                item_key=stage_key("load_session"),
            )
            reference_data = self._load_reference_data(
                run_id,
                session_key,
                item_key=stage_key("load_reference_data"),
            )
        elif reference_data.session_key != session_key:
            raise ValueError(
                f"session reference data for {reference_data.session_key} cannot collect {session_key}"
            )
        legislator_names = reference_data.legislator_names
        committee_names = reference_data.committee_names
        committee_ids = reference_data.committee_ids

        self._check_canceled(run_id)
        self.runs.begin_stage(
            run_id,
            "load_measure",
            f"Loading {compact}",
            item_key=stage_key("load_measure"),
            session_key=session_key,
        )
        raw_measure = dict(known_measure) if known_measure is not None else self.odata.get_measure(session_key, prefix, number)
        if not raw_measure:
            raise SourceRecordNotFound(f"OData measure {session_key}/{compact} was not found")
        measure = map_measure(raw_measure)
        returned_session = normalize_session_key(str(measure["session_key"]))
        returned_compact = str(measure["bill_id_compact"])
        if returned_session != session_key or returned_compact != compact:
            raise SourceRecordMismatch(
                "OData returned measure "
                f"{returned_session}/{returned_compact} for requested "
                f"{session_key}/{compact}"
            )
        measure["session_key"] = returned_session
        measure["current_committee_name"] = committee_names.get(measure.get("current_committee_code") or "")
        measure["enacted"] = int(bool(measure.get("chapter_number")))
        bill_pk = self.storage.upsert_bill(measure, run_id=run_id)

        self._check_canceled(run_id)
        sponsor_item = self.runs.begin_stage(
            run_id,
            "load_sponsors",
            f"Loading sponsors for {compact}",
            item_key=stage_key("load_sponsors"),
            session_key=session_key,
        )
        sponsor_rows = self.odata.for_measure(
            "MeasureSponsors", session_key, prefix, number, orderby="PrintOrder,MeasureSponsorId"
        )
        for raw in sponsor_rows:
            mapped = map_sponsor(raw)
            display = (
                legislator_names.get(mapped.display_code or "")
                if mapped.kind == "legislator"
                else committee_names.get(mapped.display_code or "")
                if mapped.kind == "committee"
                else _text(raw.get("PresessionFiledMessage"))
            )
            self.storage.upsert_bill_sponsor(
                {
                    "bill_id": bill_pk,
                    "source_measure_sponsor_id": str(raw["MeasureSponsorId"]),
                    "raw_sponsor_type": _text(raw.get("SponsorType")),
                    "raw_sponsor_level": _text(raw.get("SponsorLevel")),
                    "normalized_category": mapped.category,
                    "legislator_code": _text(raw.get("LegislatoreCode")),
                    "committee_code": _text(raw.get("CommitteeCode")),
                    "resolved_display_name": display or mapped.display_code,
                    "sponsor_kind": mapped.kind,
                    "print_order": _int_or_none(raw.get("PrintOrder")),
                    "pre_session_filed_message": _text(raw.get("PresessionFiledMessage")),
                    "source_created_at": _text(raw.get("CreatedDate")),
                    "source_modified_at": _text(raw.get("ModifiedDate")),
                    "raw_json": raw,
                },
                run_id=run_id,
            )
        self.runs.update_progress(run_id, sponsor_item, len(sponsor_rows), f"Loaded {len(sponsor_rows)} sponsor records")

        self._check_canceled(run_id)
        committee_item = self.runs.begin_stage(
            run_id,
            "discover_committee_documents",
            f"Discovering committee context for {compact}",
            item_key=stage_key("discover_committee_documents"),
            session_key=session_key,
        )
        agenda_rows = self.odata.for_measure("CommitteeAgendaItems", session_key, prefix, number)
        committee_doc_rows = self.odata.for_measure(
            "CommitteeMeetingDocuments", session_key, prefix, number, orderby="MeetingDate,CommitteeMeetingDocumentId"
        )
        meeting_keys = {
            (_text(row.get("CommitteCode")) or _text(row.get("CommitteeCode")), _text(row.get("MeetingDate")))
            for row in [*agenda_rows, *committee_doc_rows]
            if (_text(row.get("CommitteCode")) or _text(row.get("CommitteeCode"))) and _text(row.get("MeetingDate"))
        }
        meeting_rows = self._load_meetings(session_key, meeting_keys)
        meeting_ids: dict[tuple[str, str], int] = {}
        for raw in meeting_rows:
            code = str(raw["CommitteeCode"])
            date = str(raw["MeetingDate"])
            record = _map_meeting(raw, committee_ids.get(code), committee_names.get(code))
            meeting_ids[(code, date)] = self.storage.upsert_committee_meeting(record, run_id=run_id)
        for raw in agenda_rows:
            code = _text(raw.get("CommitteCode")) or ""
            date = _text(raw.get("MeetingDate")) or ""
            self.storage.upsert_committee_agenda_item(
                {
                    "session_key": session_key,
                    "source_agenda_item_id": str(raw["CommitteeAgendaItemId"]),
                    "committee_meeting_id": meeting_ids.get((code, date)),
                    "bill_id": bill_pk,
                    "measure_id": f"{session_key}:{compact}",
                    "bill_id_compact": compact,
                    "agenda_order": _int_or_none(raw.get("PrintOrder")),
                    "agenda_item_type": _text(raw.get("MeetingType")),
                    "description": _text(raw.get("Action")) or _text(raw.get("Comments")),
                    "source_created_at": _text(raw.get("CreatedDate")),
                    "source_modified_at": _text(raw.get("ModifiedDate")),
                    "raw_json": raw,
                },
                run_id=run_id,
            )
        self.runs.update_progress(
            run_id,
            committee_item,
            len(committee_doc_rows),
            f"Found {len(committee_doc_rows)} committee documents",
        )

        self._check_canceled(run_id)
        floor_item = self.runs.begin_stage(
            run_id,
            "discover_floor_letters",
            f"Discovering floor letters for {compact}",
            item_key=stage_key("discover_floor_letters"),
            session_key=session_key,
        )
        floor_rows = self.odata.for_measure("FloorLetters", session_key, prefix, number, orderby="LetterDate,FloorLetterId")
        self.runs.update_progress(run_id, floor_item, len(floor_rows), f"Found {len(floor_rows)} floor letters")

        self._check_canceled(run_id)
        testimony_item = self.runs.begin_stage(
            run_id,
            "discover_public_testimony",
            f"Discovering submitted testimony for {compact}",
            item_key=stage_key("discover_public_testimony"),
            session_key=session_key,
        )
        public_rows = self.odata.for_measure(
            "CommitteePublicTestimonies", session_key, prefix, number, orderby="CommTestId"
        )
        html_rows: list[ParsedTestimonyDocument] = []
        try:
            html_response = self.olis_http.get_testimony_page(session_key, compact)
            html_rows = parse_testimony_page(
                html_response.text, page_url=html_response.url, expected_session=session_key
            )
        except Exception as exc:
            self.runs.record_error(
                run_id,
                stage="discover_public_testimony",
                error=exc,
                retryable=_is_retryable(exc),
                session_key=session_key,
                bill_id_compact=compact,
                source_url=self.olis_http.testimony_url(session_key, compact),
            )
        html_public_count = sum(row.source_entity_type == "CommitteePublicTestimony" for row in html_rows)
        html_presentation_count = sum(
            row.source_entity_type == "CommitteeMeetingDocument" for row in html_rows
        )
        self.runs.update_progress(
            run_id,
            testimony_item,
            len(public_rows),
            (
                f"Found {len(public_rows)} OData testimony records, "
                f"{html_public_count} displayed testimony rows, and "
                f"{html_presentation_count} displayed historical presentation rows"
            ),
        )

        self._check_canceled(run_id)
        normalize_item = self.runs.begin_stage(
            run_id,
            "normalize_documents",
            f"Reconciling document sources for {compact}",
            item_key=stage_key("normalize_documents"),
            session_key=session_key,
        )
        html_committee_ids = {
            row.source_document_id
            for row in html_rows
            if row.source_entity_type == "CommitteeMeetingDocument"
        }
        normalized_documents: list[dict[str, Any]] = []
        normalized_documents.extend(public_testimony_documents(public_rows, html_rows, committees=committee_names))
        odata_committee_ids = {
            str(raw["CommitteeMeetingDocumentId"])
            for raw in committee_doc_rows
        }
        for raw in committee_doc_rows:
            code = _text(raw.get("CommitteeCode")) or ""
            date = _text(raw.get("MeetingDate")) or ""
            doc = committee_document(
                raw,
                displayed_ids=html_committee_ids,
                committee_name=committee_names.get(code),
            )
            doc["committee_meeting_id"] = meeting_ids.get((code, date))
            normalized_documents.append(doc)
        # Preserve page-only historical presentation rows if OLIS display and
        # OData briefly drift. Matching IDs above remain one OData-enriched row.
        normalized_documents.extend(
            html_testimony_document(row)
            for row in html_rows
            if row.source_entity_type == "CommitteeMeetingDocument"
            and str(row.source_document_id) not in odata_committee_ids
        )
        normalized_documents.extend(floor_letter_document(raw) for raw in floor_rows)
        # Identity-level dedup protects against a malformed duplicate source page.
        unique_documents: dict[tuple[str, str], dict[str, Any]] = {}
        for document in normalized_documents:
            unique_documents[(document["source_entity_type"], str(document["source_id"]))] = document
        document_ids: list[int] = []
        for document in unique_documents.values():
            document["bill_id"] = bill_pk
            if (
                document.get("document_kind") in IN_SCOPE_DOWNLOAD_KINDS
                and not document.get("canonical_download_url")
            ):
                document["download_status"] = "not_applicable"
            document_ids.append(self.storage.upsert_document(document, run_id=run_id))
        self.runs.update_progress(
            run_id,
            normalize_item,
            len(unique_documents),
            f"Stored {len(unique_documents)} logical documents",
        )

        self._check_canceled(run_id)
        download_item = self.runs.begin_stage(
            run_id,
            "download_documents",
            f"Validating and downloading documents for {compact}",
            item_key=stage_key("download_documents"),
            progress_total=sum(
                1
                for document_id in document_ids
                if self._is_download_candidate(self.storage.get_document(document_id) or {})
            ),
            session_key=session_key,
        )
        download_summary = self._download_documents(run_id, bill_pk, document_ids, download_item)
        self.runs.set_counters(
            run_id,
            documents_discovered=len(unique_documents),
            documents_queued=download_summary["queued"],
            documents_downloaded=download_summary["downloaded"],
            documents_skipped=download_summary["skipped"],
            documents_failed=download_summary["failed"],
            bytes_downloaded=download_summary["bytes_downloaded"],
        )
        return BillCollectionResult(
            bill_pk,
            compact,
            len(unique_documents),
            download_summary["downloaded"],
            download_summary["skipped"],
            download_summary["failed"],
            download_summary["bytes_downloaded"],
            {
                "sponsors": len(sponsor_rows),
                "agenda_items": len(agenda_rows),
                "committee_documents": len(committee_doc_rows),
                "floor_letters": len(floor_rows),
                "odata_public_testimony": len(public_rows),
                "html_public_testimony": html_public_count,
                "html_presentations": len(html_committee_ids),
            },
        )

    def retry_failures(self, run_id: int, document_ids: Iterable[int]) -> dict[str, Any]:
        ids = list(dict.fromkeys(int(value) for value in document_ids))
        for document_id in ids:
            document = self.storage.get_document(document_id)
            if document is None:
                raise ValueError(f"Document {document_id} no longer exists")
            require_supported_session_key(str(document.get("session_key") or ""))
        run = self.runs.get_run(run_id)
        item = self.runs.begin_stage(
            run_id,
            "download_documents",
            f"Retrying {len(ids)} failed downloads",
            progress_total=len(ids),
            session_key=run.get("requested_session_key") if run else None,
        )
        bills = {
            int(document["bill_id"])
            for document_id in ids
            if (document := self.storage.get_document(document_id)) is not None
        }
        summary = self._download_documents(run_id, next(iter(bills), 0), ids, item, retry=True)
        self.runs.set_counters(
            run_id,
            documents_discovered=len(ids),
            documents_queued=summary["queued"],
            documents_downloaded=summary["downloaded"],
            documents_skipped=summary["skipped"],
            documents_failed=summary["failed"],
            bytes_downloaded=summary["bytes_downloaded"],
        )
        return summary

    def retry_matching_failures(
        self, run_id: int, retry_match: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Process an exact retry snapshot through bounded atomic claims."""

        matching_count = max(0, int(retry_match.get("matching_count") or 0))
        session_key = str(retry_match.get("session_key") or "").strip() or None
        stage_item = self.runs.begin_stage(
            run_id,
            "download_documents",
            f"Retrying {matching_count} matching failed downloads",
            progress_total=matching_count,
            session_key=session_key,
        )
        progress_lock = threading.Lock()
        processed = 0

        def worker() -> None:
            nonlocal processed
            while self.runs.status(run_id) == "running":
                document = self.storage.claim_next_retry_document(run_id)
                if document is None:
                    return
                require_supported_session_key(str(document.get("session_key") or ""))
                _document_id, _outcome, byte_count = self._download_claimed_document(
                    run_id, document
                )
                del byte_count
                with progress_lock:
                    processed += 1
                    self.runs.update_progress(
                        run_id,
                        stage_item,
                        processed,
                        f"Processed {processed} of {matching_count} matching failures",
                    )

        with ThreadPoolExecutor(
            max_workers=max(1, self.config.download_worker_count),
            thread_name_prefix="legiview-retry",
        ) as pool:
            futures = [
                pool.submit(worker)
                for _ in range(max(1, self.config.download_worker_count))
            ]
            for future in futures:
                future.result()

        with self.database.connection() as connection:
            item_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status,COUNT(*) AS count
                    FROM collection_run_items
                    WHERE run_id=? AND item_type='document'
                    GROUP BY status
                    """,
                    (run_id,),
                ).fetchall()
            }
        finished_count = sum(
            item_counts.get(status, 0)
            for status in (
                "completed",
                "skipped",
                "failed_retryable",
                "failed_terminal",
                "canceled",
            )
        )
        self.runs.update_progress(
            run_id,
            stage_item,
            finished_count,
            f"Processed {finished_count} of {matching_count} matching failures",
        )
        self.runs.set_counters(
            run_id,
            documents_discovered=matching_count,
            documents_queued=matching_count,
            documents_downloaded=item_counts.get("completed", 0),
            documents_skipped=item_counts.get("skipped", 0),
            documents_failed=(
                item_counts.get("failed_retryable", 0)
                + item_counts.get("failed_terminal", 0)
            ),
        )
        status = self.runs.status(run_id)
        if status == "canceled":
            raise CollectionCanceled()
        if status in {"paused", "interrupted"}:
            raise CollectionSuspended(status)
        durable_run = self.runs.get_run(run_id) or {}
        return {
            "matching_count": matching_count,
            "item_counts": item_counts,
            "bytes_downloaded": int(durable_run.get("bytes_downloaded") or 0),
        }

    # -- source/payload helpers ---------------------------------------------

    def _load_session_record(self, run_id: int, session_key: str, *, item_key: str) -> int:
        self._check_canceled(run_id)
        item_id = self.runs.begin_stage(
            run_id,
            "load_session",
            f"Loading {session_key}",
            item_key=item_key,
            session_key=session_key,
        )
        raw_session = self.odata.get_session(session_key)
        if not raw_session:
            raise SourceRecordNotFound(f"OData session {session_key} was not found")
        self.storage.upsert_session(_map_session(raw_session), run_id=run_id)
        self.runs.update_progress(run_id, item_id, 1, f"Loaded {session_key}")
        return item_id

    def _load_reference_data(
        self,
        run_id: int,
        session_key: str,
        *,
        item_key: str,
    ) -> SessionReferenceData:
        self._check_canceled(run_id)
        reference_item = self.runs.begin_stage(
            run_id,
            "load_reference_data",
            f"Loading legislators and committees for {session_key}",
            item_key=item_key,
            session_key=session_key,
        )
        base_filter = f"SessionKey eq {odata_literal(session_key)}"

        def reference_filter(entity: str) -> str:
            watermark = self.storage.reference_source_watermark(entity, session_key)
            if not watermark:
                return base_filter
            try:
                source_date = odata_datetime_literal(watermark)
            except ValueError:
                LOGGER.warning(
                    "Ignoring unusable %s source-date watermark %r for %s",
                    entity,
                    watermark,
                    session_key,
                )
                return base_filter
            # Inclusive comparison deliberately overlaps the newest retained
            # timestamp, preventing equal-timestamp source updates from falling
            # through a cursor boundary. Stable upserts make the overlap cheap.
            return (
                f"{base_filter} and (ModifiedDate ge {source_date} "
                f"or CreatedDate ge {source_date})"
            )

        raw_legislators = self.odata.query(
            "Legislators", filter=reference_filter("legislators")
        )
        raw_committees = self.odata.query(
            "Committees", filter=reference_filter("committees")
        )
        for raw in raw_legislators:
            record = _map_legislator(raw)
            self.storage.upsert_legislator(record, run_id=run_id)
        for raw in raw_committees:
            record = _map_committee(raw)
            self.storage.upsert_committee(record, run_id=run_id)

        # Incremental fetches contain only changed rows. Resolve the current
        # bill against the complete retained, session-scoped reference set.
        stored_legislators = self.storage.list_legislators(session_key)
        stored_committees = self.storage.list_committees(session_key)
        legislator_names = {
            str(row["legislator_code"]): (
                _text(row.get("display_name"))
                or " ".join(
                    value
                    for value in (_text(row.get("first_name")), _text(row.get("last_name")))
                    if value
                )
            )
            for row in stored_legislators
        }
        committee_names = {
            str(row["committee_code"]): _committee_display(
                {
                    "CommitteeType": row.get("committee_type"),
                    "CommitteeName": row.get("committee_name"),
                    "CommitteeCode": row.get("committee_code"),
                }
            )
            for row in stored_committees
        }
        committee_ids = {
            str(row["committee_code"]): int(row["id"])
            for row in stored_committees
        }
        self.runs.update_progress(
            run_id,
            reference_item,
            len(raw_legislators) + len(raw_committees),
            (
                f"Refreshed {len(raw_legislators)} legislators and {len(raw_committees)} "
                f"committees; {len(stored_legislators)} legislators and "
                f"{len(stored_committees)} committees retained"
            ),
        )
        return SessionReferenceData(
            session_key=session_key,
            legislator_names=legislator_names,
            committee_names=committee_names,
            committee_ids=committee_ids,
        )

    def _load_meetings(self, session_key: str, keys: set[tuple[str | None, str | None]]) -> list[dict[str, Any]]:
        expected = {(code, date) for code, date in keys if code and date}
        rows: list[dict[str, Any]] = []
        for code in sorted({code for code, _ in expected}):
            candidates = self.odata.query(
                "CommitteeMeetings",
                filter=f"SessionKey eq {odata_literal(session_key)} and CommitteeCode eq {odata_literal(code)}",
                orderby="MeetingDate",
            )
            rows.extend(
                raw for raw in candidates if (str(raw.get("CommitteeCode")), str(raw.get("MeetingDate"))) in expected
            )
        return rows

    def _download_documents(
        self,
        run_id: int,
        bill_pk: int,
        document_ids: Iterable[int],
        progress_item_id: int,
        *,
        retry: bool = False,
    ) -> dict[str, int]:
        summary = {"queued": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes_downloaded": 0}
        work: list[tuple[dict[str, Any], int]] = []
        progress = 0
        for document_id in document_ids:
            document = self.storage.get_document(document_id)
            if not document or not self._is_download_candidate(document):
                continue
            actual_bill_id = int(document["bill_id"])
            item_key = f"{document['source_entity_type']}:{document['source_id']}"
            self.runs.add_document_item(
                run_id,
                document_id,
                actual_bill_id,
                item_key,
                session_key=str(document["session_key"]),
            )
            existing = self._valid_completed_document(document)
            if existing and not self._source_changed(document):
                self.runs.mark_document_item(run_id, document_id, "skipped", "Valid completed file already present")
                summary["skipped"] += 1
                progress += 1
                self.runs.update_progress(run_id, progress_item_id, progress)
                continue
            if document["download_status"] == "failed_terminal" and not retry:
                self.runs.mark_document_item(run_id, document_id, "failed_terminal", document.get("last_error"))
                self.runs.record_error(
                    run_id,
                    stage="download_documents",
                    error=str(document.get("last_error") or "Previously recorded terminal download failure"),
                    retryable=False,
                    session_key=str(document["session_key"]),
                    bill_id_compact=str(document["bill_id_compact"]),
                    source_entity_type=str(document["source_entity_type"]),
                    source_id=str(document["source_id"]),
                    document_id=document_id,
                    source_url=document.get("canonical_download_url"),
                    details={"preserved_terminal_failure": True},
                )
                summary["failed"] += 1
                progress += 1
                self.runs.update_progress(run_id, progress_item_id, progress)
                continue
            if document["download_status"] == "downloaded":
                self.storage.update_document_download_state(
                    document_id,
                    "changed_remote" if existing else "missing_local",
                    last_error=None if existing else "Previously downloaded file is missing or invalid",
                )
            elif document["download_status"] == "failed_terminal" and retry:
                # Explicit retry is authority to give a terminal source response a new cycle.
                self.storage.update_document_download_state(document_id, "failed_retryable", last_error=None)
            queued = self.storage.queue_document(document_id)
            if not queued:
                refreshed = self.storage.get_document(document_id)
                if refreshed and refreshed["download_status"] == "queued":
                    queued = True
            if queued:
                version_number = self._next_version_number(document_id)
                work.append((self.storage.get_document(document_id) or document, version_number))
                summary["queued"] += 1
            else:
                self.runs.mark_document_item(run_id, document_id, "failed_terminal", "Document could not be queued")
                summary["failed"] += 1
                progress += 1

        total_to_process = progress + len(work)

        def completed(future):
            nonlocal progress
            document_id, outcome, byte_count = future.result()
            if outcome == "downloaded":
                summary["downloaded"] += 1
                summary["bytes_downloaded"] += byte_count
            elif outcome == "skipped":
                summary["skipped"] += 1
            elif outcome == "failed":
                summary["failed"] += 1
            progress += 1
            self.runs.update_progress(
                run_id,
                progress_item_id,
                progress,
                f"Processed {progress} of {total_to_process} payloads",
            )

        if work:
            with ThreadPoolExecutor(
                max_workers=max(1, min(self.config.download_worker_count, len(work))),
                thread_name_prefix="legiview-download",
            ) as pool:
                futures = [
                    pool.submit(self._download_one, run_id, document, version_number)
                    for document, version_number in work
                ]
                for future in as_completed(futures):
                    completed(future)
                    current_status = self.runs.status(run_id)
                    if current_status == "canceled":
                        for pending in futures:
                            pending.cancel()
                        raise CollectionCanceled()
                    if current_status == "interrupted":
                        for pending in futures:
                            pending.cancel()
                        raise CollectionSuspended("interrupted")
        return summary

    def _download_claimed_document(
        self, run_id: int, document: Mapping[str, Any]
    ) -> tuple[int, str, int]:
        """Run the proven Phase 1 transfer path for a DB-preclaimed document."""

        document_id = int(document["id"])
        try:
            return self._download_one(
                run_id,
                document,
                self._next_version_number(document_id),
                already_claimed=True,
            )
        except CollectionCanceled:
            self.storage.release_archive_document_claim(
                run_id,
                document_id,
                document_status="interrupted",
                item_status="canceled",
                message="Download canceled while preparing or retrying the transfer",
            )
            return document_id, "canceled", 0
        except CollectionSuspended as exc:
            item_status = "paused" if exc.status == "paused" else "interrupted"
            self.storage.release_archive_document_claim(
                run_id,
                document_id,
                document_status="interrupted",
                item_status=item_status,
                message=f"Download {exc.status} while preparing or retrying the transfer",
            )
            return document_id, item_status, 0
        except Exception as exc:
            retryable = _is_retryable(exc)
            status = "failed_retryable" if retryable else "failed_terminal"
            self.storage.release_archive_document_claim(
                run_id,
                document_id,
                document_status=status,
                item_status=status,
                message=str(exc),
            )
            self.runs.record_error(
                run_id,
                stage="download_archive",
                error=exc,
                retryable=retryable,
                session_key=str(document.get("session_key") or "") or None,
                bill_id_compact=str(document.get("bill_id_compact") or "") or None,
                source_entity_type=str(document.get("source_entity_type") or "") or None,
                source_id=str(document.get("source_id") or "") or None,
                document_id=document_id,
                source_url=document.get("canonical_download_url"),
                details=(
                    {
                        "claim_finalization_failure": True,
                        "cause_class": (
                            type(exc.__cause__).__name__
                            if exc.__cause__ is not None
                            else None
                        ),
                    }
                    if isinstance(exc, _DownloadFinalizationError)
                    else {"claim_setup_failure": True}
                ),
            )
            return document_id, "failed", 0

    def _download_one(
        self,
        run_id: int,
        document: Mapping[str, Any],
        version_number: int,
        *,
        already_claimed: bool = False,
    ) -> tuple[int, str, int]:
        document_id = int(document["id"])
        run_status = self.runs.status(run_id)
        if run_status == "paused":
            if already_claimed:
                self.storage.release_archive_document_claim(
                    run_id,
                    document_id,
                    document_status="interrupted",
                    item_status="paused",
                    message="Deferred because the run is paused",
                )
            else:
                self.runs.mark_document_item(
                    run_id, document_id, "paused", "Deferred because the run is paused"
                )
            return document_id, "paused", 0
        if run_status == "interrupted":
            if already_claimed:
                self.storage.release_archive_document_claim(
                    run_id,
                    document_id,
                    document_status="interrupted",
                    item_status="interrupted",
                    message="Deferred because the run was interrupted",
                )
            else:
                self.runs.mark_document_item(
                    run_id, document_id, "interrupted", "Deferred because the run was interrupted"
                )
            return document_id, "interrupted", 0
        if run_status == "canceled":
            if already_claimed:
                self.storage.update_document_download_state(
                    document_id,
                    "interrupted",
                    last_error="Download canceled before transfer started",
                )
            return document_id, "canceled", 0
        if not already_claimed and not self._claim_download_attempt(run_id, document):
            return document_id, "failed", 0
        title = _text(document.get("title")) or f"{document['document_kind']}-{document['source_id']}"
        destination = archive_document_path(
            self.config.archive_root,
            str(document["session_key"]),
            str(document["bill_id_compact"]),
            str(document["document_kind"]),
            str(document["source_id"]),
            sanitize_windows_filename(title),
            version_number=version_number,
            create_directory=True,
        )
        policy = RetryPolicy(max_attempts=3, base_delay_seconds=2, maximum_delay_seconds=30)
        for attempt in range(1, policy.max_attempts + 1):
            try:
                result = self.downloader.download_to_path(
                    str(document["canonical_download_url"]),
                    destination,
                    archive_root=self.config.archive_root,
                    cancellation_requested=lambda: self.runs.should_abort_active_work(run_id),
                )
                outcome_status = "skipped" if result.skipped else "completed"
                try:
                    with self.database.transaction():
                        self._record_download_result(run_id, document, result)
                        message = (
                            "Validated existing bytes"
                            if result.skipped
                            else "Downloaded and validated"
                        )
                        if already_claimed:
                            finalized = self.runs.finalize_claimed_document_item(
                                run_id,
                                document_id,
                                outcome_status,
                                message,
                            )
                            if not finalized:
                                self._raise_claim_finalization_control(run_id, document_id)
                        else:
                            self.runs.mark_document_item(
                                run_id,
                                document_id,
                                outcome_status,
                                message,
                            )
                        if already_claimed and not result.skipped:
                            # Claimed archive/retry workers have no later aggregate
                            # transaction. Keep their durable byte counter in the
                            # same commit as the document version and run-item result.
                            self.runs.add_downloaded_bytes(run_id, result.byte_count)
                except (CollectionCanceled, CollectionSuspended, _DownloadFinalizationError):
                    raise
                except Exception as exc:
                    if already_claimed:
                        # The file may already be atomically visible. Preserve it
                        # as an adoptable orphan and keep the document retryable.
                        raise _DownloadFinalizationError(
                            "Downloaded payload could not be durably finalized"
                        ) from exc
                    raise
                return document_id, "skipped" if result.skipped else "downloaded", 0 if result.skipped else result.byte_count
            except (CollectionCanceled, CollectionSuspended, _DownloadFinalizationError):
                raise
            except LowDiskSpace as exc:
                self.storage.update_document_download_state(
                    document_id, "paused_low_space", last_error=str(exc), validation_status="not_validated"
                )
                self.runs.mark_document_item(run_id, document_id, "paused", str(exc))
                self.runs.record_error(
                    run_id,
                    stage="download_documents",
                    error=exc,
                    retryable=True,
                    session_key=str(document["session_key"]),
                    bill_id_compact=str(document["bill_id_compact"]),
                    source_entity_type=str(document["source_entity_type"]),
                    source_id=str(document["source_id"]),
                    document_id=document_id,
                    source_url=document.get("canonical_download_url"),
                )
                self.runs.pause(run_id, str(exc))
                return document_id, "paused", 0
            except DownloadInterrupted as exc:
                self.storage.update_document_download_state(
                    document_id, "interrupted", last_error=str(exc)
                )
                if self.runs.status(run_id) != "canceled":
                    self.runs.mark_document_item(run_id, document_id, "interrupted", str(exc))
                return document_id, "interrupted", 0
            except DownloadError as exc:
                if exc.retryable and attempt < policy.max_attempts:
                    self.storage.update_document_download_state(
                        document_id,
                        "failed_retryable",
                        last_error=str(exc),
                        http_status=exc.status_code,
                        validation_status="invalid" if exc.code == "validation_failed" else "not_validated",
                    )
                    delay = retry_delay_seconds(exc, attempt, policy)
                    self.sleep(delay)
                    self._check_canceled(run_id)
                    self.storage.queue_document(document_id)
                    if not self._claim_download_attempt(run_id, document):
                        return document_id, "failed", 0
                    continue
                status = "failed_retryable" if exc.retryable else "failed_terminal"
                self.storage.update_document_download_state(
                    document_id,
                    status,
                    last_error=str(exc),
                    http_status=exc.status_code,
                    validation_status="invalid" if exc.code in {"validation_failed", "content_length_mismatch"} else "not_validated",
                )
                self.runs.mark_document_item(run_id, document_id, status, str(exc))
                self.runs.record_error(
                    run_id,
                    stage="download_documents",
                    error=exc,
                    retryable=exc.retryable,
                    session_key=str(document["session_key"]),
                    bill_id_compact=str(document["bill_id_compact"]),
                    source_entity_type=str(document["source_entity_type"]),
                    source_id=str(document["source_id"]),
                    document_id=document_id,
                    source_url=document.get("canonical_download_url"),
                    details={"code": exc.code, "attempts": attempt},
                )
                return document_id, "failed", 0
            except Exception as exc:
                self.storage.update_document_download_state(document_id, "failed_terminal", last_error=str(exc))
                self.runs.mark_document_item(run_id, document_id, "failed_terminal", str(exc))
                self.runs.record_error(
                    run_id,
                    stage="download_documents",
                    error=exc,
                    retryable=False,
                    session_key=str(document["session_key"]),
                    bill_id_compact=str(document["bill_id_compact"]),
                    source_entity_type=str(document["source_entity_type"]),
                    source_id=str(document["source_id"]),
                    document_id=document_id,
                    source_url=document.get("canonical_download_url"),
                )
                return document_id, "failed", 0
        return document_id, "failed", 0

    def _raise_claim_finalization_control(
        self, run_id: int, document_id: int
    ) -> None:
        """Translate a lost success transition without overwriting control state."""

        with self.database.connection() as connection:
            state = connection.execute(
                """
                SELECT r.status AS run_status,i.status AS item_status
                FROM collection_runs r
                LEFT JOIN collection_run_items i
                  ON i.run_id=r.id AND i.item_type='document' AND i.document_id=?
                WHERE r.id=?
                """,
                (document_id, run_id),
            ).fetchone()
        if state is not None:
            statuses = {str(state["run_status"]), str(state["item_status"] or "")}
            if "canceled" in statuses:
                raise CollectionCanceled()
            for status in ("paused", "interrupted"):
                if status in statuses:
                    raise CollectionSuspended(status)
        raise _DownloadFinalizationError(
            "Document payload completed after its durable download claim was lost"
        )

    def _claim_download_attempt(self, run_id: int, document: Mapping[str, Any]) -> bool:
        """Claim both durable ledgers or persist a retryable orchestration error."""

        document_id = int(document["id"])
        document_claimed = self.storage.claim_document(document_id)
        if document_claimed and self.runs.begin_document_attempt(run_id, document_id):
            return True
        error = DownloadError(
            "Document download could not be claimed by this worker",
            retryable=True,
            code="claim_failed",
        )
        # A failed document claim normally means another run owns the active
        # transfer.  Never revoke that worker's durable claim.  Roll back only
        # when this invocation won the document claim but could not claim its
        # corresponding run item.
        refreshed = self.storage.get_document(document_id)
        if (
            document_claimed
            and refreshed
            and refreshed.get("download_status") == "downloading"
        ):
            self.storage.update_document_download_state(
                document_id, "failed_retryable", last_error=str(error)
            )
        self.runs.mark_document_item(run_id, document_id, "failed_retryable", str(error))
        self.runs.record_error(
            run_id,
            stage="download_documents",
            error=error,
            retryable=True,
            session_key=str(document["session_key"]),
            bill_id_compact=str(document["bill_id_compact"]),
            source_entity_type=str(document["source_entity_type"]),
            source_id=str(document["source_id"]),
            document_id=document_id,
            source_url=document.get("canonical_download_url"),
        )
        return False

    def _record_download_result(
        self,
        run_id: int,
        document: Mapping[str, Any],
        result: DownloadResult,
    ) -> None:
        document_id = int(document["id"])
        self.runs.resolve_document_errors(run_id, document_id)
        existing = self._version_with_hash(document_id, result.sha256)
        if existing and existing.get("local_relative_path") != result.relative_path:
            existing_path = resolve_stored_path(self.config.archive_root, existing["local_relative_path"])
            valid = validate_file(
                existing_path,
                existing.get("mime_type") or "",
                existing.get("downloaded_bytes"),
                expected_sha256=existing.get("sha256") or "",
                logical_filename=existing.get("local_filename") or existing_path.name,
            )
            if valid.valid:
                result.path.unlink(missing_ok=True)
                self.storage.update_document_version(
                    int(existing["id"]),
                    source_url=result.final_url,
                    source_modified_at=document.get("source_modified_at"),
                    etag=result.etag or existing.get("etag"),
                    last_modified=result.last_modified or existing.get("last_modified"),
                    http_status=(
                        int(result.response_metadata[":status"])
                        if result.response_metadata.get(":status")
                        else existing.get("http_status")
                    ),
                    error=None,
                )
                self.storage.complete_document_download(
                    document_id,
                    sha256=str(existing["sha256"]),
                    local_relative_path=str(existing["local_relative_path"]),
                    downloaded_bytes=int(existing["downloaded_bytes"]),
                    mime_type=existing.get("mime_type"),
                    local_filename=existing.get("local_filename"),
                    remote_filename=existing.get("remote_filename"),
                    advertised_bytes=existing.get("advertised_bytes"),
                    etag=existing.get("etag"),
                    last_modified=existing.get("last_modified"),
                    source_modified_at=document.get("source_modified_at"),
                    validation_status="valid",
                    http_status=result.response_metadata.get(":status") and int(result.response_metadata[":status"]),
                    source_url=result.final_url,
                    run_id=run_id,
                )
                return
            # The payload bytes are unchanged, but their previously registered
            # path is no longer valid. Re-home that same immutable payload version
            # at the newly validated path rather than leaving the logical document
            # pointed at a missing file.
            self.storage.update_document_version(
                int(existing["id"]),
                source_url=result.final_url,
                source_modified_at=document.get("source_modified_at"),
                etag=result.etag or None,
                last_modified=result.last_modified or None,
                remote_filename=result.remote_filename,
                local_filename=result.filename,
                local_relative_path=result.relative_path,
                advertised_bytes=result.expected_length,
                downloaded_bytes=result.byte_count,
                mime_type=result.mime_type,
                status="downloaded",
                validation_status=result.validation.status,
                http_status=200,
                error=None,
            )
        self.storage.complete_document_download(
            document_id,
            sha256=result.sha256,
            local_relative_path=result.relative_path,
            downloaded_bytes=result.byte_count,
            mime_type=result.mime_type,
            local_filename=result.filename,
            remote_filename=result.remote_filename,
            advertised_bytes=result.expected_length,
            etag=result.etag or None,
            last_modified=result.last_modified or None,
            source_modified_at=document.get("source_modified_at"),
            validation_status=result.validation.status,
            http_status=200,
            source_url=result.final_url,
            run_id=run_id,
        )

    def _valid_completed_document(self, document: Mapping[str, Any]) -> bool:
        if document.get("download_status") != "downloaded" or not document.get("local_relative_path"):
            return False
        try:
            path = resolve_stored_path(self.config.archive_root, document["local_relative_path"])
            result = validate_file(
                path,
                document.get("mime_type") or "",
                _int_or_none(document.get("downloaded_bytes")),
                expected_sha256=document.get("sha256") or "",
                logical_filename=document.get("local_filename") or path.name,
            )
            return result.valid
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_download_candidate(document: Mapping[str, Any]) -> bool:
        return bool(
            document.get("document_kind") in IN_SCOPE_DOWNLOAD_KINDS
            and document.get("download_status") != "not_applicable"
            and document.get("canonical_download_url")
        )

    def _source_changed(self, document: Mapping[str, Any]) -> bool:
        versions = self.storage.list_document_versions(int(document["id"]))
        if not versions or not document.get("source_modified_at"):
            return False
        return (versions[0].get("source_modified_at") or "") != document.get("source_modified_at")

    def _next_version_number(self, document_id: int) -> int:
        versions = self.storage.list_document_versions(document_id)
        return 1 + max((int(row["version_number"]) for row in versions), default=0)

    def _version_with_hash(self, document_id: int, digest: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.storage.list_document_versions(document_id) if row.get("sha256") == digest),
            None,
        )

    def _check_canceled(self, run_id: int) -> None:
        status = self.runs.status(run_id)
        if status == "canceled":
            raise CollectionCanceled()
        if status in {"paused", "interrupted"}:
            raise CollectionSuspended(status)


def _map_session(raw: Mapping[str, Any]) -> dict[str, Any]:
    return map_session_record(raw)


def _map_legislator(raw: Mapping[str, Any]) -> dict[str, Any]:
    return map_legislator_record(raw)


def _map_committee(raw: Mapping[str, Any]) -> dict[str, Any]:
    return map_committee_record(raw)


def _committee_display(raw: Mapping[str, Any]) -> str:
    return map_committee_display(raw)


def _map_meeting(raw: Mapping[str, Any], committee_id: int | None, committee_name: str | None) -> dict[str, Any]:
    return map_meeting_record(raw, committee_id, committee_name)


def _result_dict(result: BillCollectionResult) -> dict[str, Any]:
    return {
        "bill_id": result.bill_id,
        "bill_id_compact": result.bill_id_compact,
        "documents_discovered": result.documents_discovered,
        "documents_downloaded": result.documents_downloaded,
        "documents_skipped": result.documents_skipped,
        "documents_failed": result.documents_failed,
        "bytes_downloaded": result.bytes_downloaded,
        "source_counts": dict(result.source_counts),
    }


def _is_retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False) or isinstance(exc, (TimeoutError, OSError)))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BillCollectionResult",
    "CollectionCanceled",
    "CollectionSuspended",
    "CollectionService",
    "IN_SCOPE_DOWNLOAD_KINDS",
    "SessionReferenceData",
    "SourceRecordNotFound",
]
