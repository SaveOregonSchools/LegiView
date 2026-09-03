from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any

from olis_archive.config import AppConfig
from olis_archive.services.collection import CollectionService
from olis_archive.database import Database
from olis_archive.runtime import build_runtime
from olis_archive.services.downloads import DownloadError, DownloadResult, LowDiskSpace
from olis_archive.services.file_types import FileTypeDetection, FileValidation
from olis_archive.services.olis_http import HTMLResponse
from olis_archive.services.runs import RunStore
from olis_archive.services.storage import StorageService


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class FakeOData:
    def __init__(
        self,
        measure: dict[str, Any],
        *,
        measures: list[dict[str, Any]] | None = None,
    ) -> None:
        self.measure = dict(measure)
        self.measures = [dict(row) for row in (measures or [measure])]
        self.calls: list[tuple[str, str]] = []
        self.session_calls = 0
        self.measure_limits: list[int | None] = []

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        assert session_key == "2026R1"
        self.session_calls += 1
        return {
            "SessionKey": "2026R1",
            "SessionName": "2026 Regular Session",
            "BeginDate": "2026-02-02T00:00:00",
            "EndDate": None,
            "CreatedDate": "2025-12-01T10:00:00",
            "ModifiedDate": None,
        }

    def get_measure(self, session_key: str, prefix: str, number: int) -> dict[str, Any] | None:
        assert session_key == "2026R1"
        return next(
            (
                dict(row)
                for row in self.measures
                if row["MeasurePrefix"] == prefix and int(row["MeasureNumber"]) == number
            ),
            None,
        )

    def get_measures(self, session_key: str, *, max_bills: int | None = None):
        assert session_key == "2026R1"
        self.measure_limits.append(max_bills)
        rows = [dict(row) for row in self.measures]
        return rows if max_bills is None else rows[:max_bills]

    def query(self, entity_set: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("query", entity_set))
        if entity_set == "Legislators":
            return [
                {
                    "SessionKey": "2026R1",
                    "LegislatorCode": "Sen President Wagner",
                    "FirstName": "Rob",
                    "LastName": "Wagner",
                    "Chamber": "S",
                    "Party": "D",
                    "DistrictNumber": "19",
                    "EmailAddress": "rob@example.invalid",
                },
                {
                    "SessionKey": "2026R1",
                    "LegislatorCode": "Sen Manning Jr",
                    "FirstName": "James",
                    "LastName": "Manning Jr",
                    "Chamber": "S",
                    "Party": "D",
                    "DistrictNumber": "7",
                },
            ]
        if entity_set == "Committees":
            return [
                {
                    "SessionKey": "2026R1",
                    "CommitteeCode": "SRULES",
                    "CommitteeName": "Rules",
                    "CommitteeType": "Senate Committee On",
                    "HouseOfAction": "S",
                }
            ]
        if entity_set == "CommitteeMeetings":
            return [
                {
                    "SessionKey": "2026R1",
                    "CommitteeCode": "SRULES",
                    "MeetingDate": "2026-02-11T08:00:00",
                    "Location": "HR C",
                    "MeetingStatus": "Scheduled",
                    "AgendaUrl": "https://olis.oregonlegislature.gov/liz/2026R1/Committees/SRULES/2026-02-11-08-00/Agenda",
                }
            ]
        raise AssertionError(f"unexpected query for {entity_set}: {params}")

    def for_measure(
        self,
        entity_set: str,
        session_key: str,
        prefix: str,
        number: int,
        **params: Any,
    ) -> list[dict[str, Any]]:
        assert session_key == "2026R1"
        self.calls.append(("for_measure", entity_set))
        if (prefix, number) != ("SB", 1501):
            return []
        if entity_set == "MeasureSponsors":
            return [
                {
                    "MeasureSponsorId": "157656",
                    "SessionKey": "2026R1",
                    "SponsorType": "Member",
                    "LegislatoreCode": "Sen President Wagner",
                    "CommitteeCode": None,
                    "SponsorLevel": "Chief",
                    "PrintOrder": "1",
                },
                {
                    "MeasureSponsorId": "157708",
                    "SessionKey": "2026R1",
                    "SponsorType": "Member",
                    "LegislatoreCode": "Sen Manning Jr",
                    "CommitteeCode": None,
                    "SponsorLevel": "Regular",
                    "PrintOrder": "20",
                },
            ]
        if entity_set == "CommitteeAgendaItems":
            return [
                {
                    "CommitteeAgendaItemId": "210001",
                    "SessionKey": "2026R1",
                    "CommitteCode": "SRULES",
                    "MeetingDate": "2026-02-11T08:00:00",
                    "MeetingType": "Public Hearing",
                    "Action": "Heard",
                    "PrintOrder": "1",
                }
            ]
        if entity_set == "CommitteeMeetingDocuments":
            return [
                {
                    "CommitteeMeetingDocumentId": "313285",
                    "SessionKey": "2026R1",
                    "CommitteeCode": "SRULES",
                    "MeetingDate": "2026-02-11T08:00:00",
                    "ExhibitReference": "2",
                    "ExhibitTitle": "Committee presentation",
                    "Submitter": "Committee staff",
                    "DocumentType": "Presentation",
                    "DocumentUrl": "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/CommitteeMeetingDocument/313285",
                    "CreatedDate": "2026-02-11T07:00:00",
                    "ModifiedDate": "2026-02-11T07:30:00",
                }
            ]
        if entity_set == "FloorLetters":
            return [
                {
                    "FloorLetterId": "4701",
                    "SessionKey": "2026R1",
                    "LetterDate": "2026-03-04T00:00:00",
                    "Chamber": "S",
                    "LetterDescription": "Vote Yes On SB 1501 B",
                    "LetterTitle": "Vote Yes On SB 1501 B",
                    "FloorLetterUrl": "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/FloorLetter/4701",
                }
            ]
        if entity_set == "CommitteePublicTestimonies":
            return [
                {
                    "CommTestId": "244133",
                    "SessionKey": "2026R1",
                    "SubmitterFirstName": "OData",
                    "SubmitterLastName": "Name",
                    "BehalfOf": None,
                    "Organization": "Salem",
                    "DocumentDescription": "Testimony",
                    "PositionOnMeasureId": "3983",
                    "CommitteeCode": "SRULES",
                    "MeetingDate": "2026-02-11T08:00:00",
                    "CreatedDate": "2026-02-10T12:00:00",
                    "ModifiedDate": "2026-02-10T13:00:00",
                },
                {
                    "CommTestId": "244244",
                    "SessionKey": "2026R1",
                    "SubmitterFirstName": "Second",
                    "SubmitterLastName": "Sample",
                    "BehalfOf": "Community Group",
                    "Organization": "Eugene",
                    "DocumentDescription": "Letter",
                    "PositionOnMeasureId": "3982",
                    "CommitteeCode": "SRULES",
                    "MeetingDate": "2026-02-11T08:00:00",
                    "CreatedDate": "2026-02-10T12:01:00",
                    "ModifiedDate": None,
                },
            ]
        raise AssertionError(f"unexpected measure query for {entity_set}: {params}")


class FakeOLISHTTP:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls = 0

    def testimony_url(self, session_key: str, bill_id: str) -> str:
        return f"https://olis.oregonlegislature.gov/liz/{session_key}/Measures/Testimony/{bill_id}"

    def get_testimony_page(self, session_key: str, bill_id: str) -> HTMLResponse:
        self.calls += 1
        html = self.html if bill_id == "SB1501" else "<html><body><p>No items to display</p></body></html>"
        return HTMLResponse(self.testimony_url(session_key, bill_id), html, 200, {})


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_urls: set[str] = set()
        self.terminal_urls: set[str] = set()
        self.low_space_urls: set[str] = set()
        self._lock = Lock()

    def download_to_path(
        self,
        url: str,
        destination: str | Path,
        *,
        archive_root: str | Path,
        cancellation_requested=None,
        **_kwargs: Any,
    ) -> DownloadResult:
        with self._lock:
            self.calls.append(url)
            should_fail = url in self.fail_urls
            terminal_failure = url in self.terminal_urls
            low_space = url in self.low_space_urls
        if low_space:
            raise LowDiskSpace("fixture free-space floor reached")
        if terminal_failure:
            raise DownloadError(
                "fixture payload is terminally invalid",
                retryable=False,
                status_code=404,
                code="fixture_terminal",
            )
        if should_fail:
            raise DownloadError(
                "temporary fixture outage",
                retryable=True,
                status_code=503,
                code="fixture_outage",
            )
        assert not cancellation_requested or not cancellation_requested()
        path = Path(destination)
        if path.suffix.casefold() != ".pdf":
            path = path.with_suffix(".pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PDF_BYTES)
        digest = sha256(PDF_BYTES).hexdigest()
        validation = FileValidation(
            "valid",
            "fixture PDF passed",
            FileTypeDetection(".pdf", "application/pdf", "strong", "PDF signature"),
        )
        root = Path(archive_root).resolve()
        return DownloadResult(
            source_url=url,
            final_url=url,
            path=path,
            relative_path=path.resolve().relative_to(root).as_posix(),
            filename=path.name,
            remote_filename="source.pdf",
            byte_count=len(PDF_BYTES),
            expected_length=len(PDF_BYTES),
            declared_mime_type="application/pdf",
            detected_mime_type="application/pdf",
            sha256=digest,
            validation=validation,
            response_metadata={":status": "200"},
        )


def _row_count(service: CollectionService, table: str) -> int:
    with service.database.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_collect_bill_is_durable_idempotent_and_retries_only_failed_payloads(
    tmp_path: Path,
    fixture_dir: Path,
):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(encoding="utf-8")
    odata = FakeOData(measure)
    olis = FakeOLISHTTP(html)
    downloader = FakeDownloader()
    failed_url = "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/FloorLetter/4701"
    downloader.fail_urls.add(failed_url)
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        download_worker_count=2,
        inter_request_delay=0,
    )
    service = CollectionService(
        config,
        odata=odata,
        olis_http=olis,
        downloader=downloader,
        sleep=lambda _seconds: None,
    )

    first_run = service.create_collect_bill_run("2026R1", "SB 1501")
    assert service.execute_run(first_run) == "completed_with_errors"
    first_header = service.runs.get_run(first_run)
    assert first_header["stage"] == "finalize"
    assert first_header["bills_completed"] == 1
    assert first_header["documents_discovered"] == 5
    assert first_header["documents_downloaded"] == 4
    assert first_header["documents_failed"] == 1

    stages = {item["stage"] for item in service.runs.run_items(first_run) if item["item_type"] == "stage"}
    assert {
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
    } <= stages

    bill = service.storage.get_bill("2026R1", "SB1501")
    assert bill["bill_title"] == measure["RelatingTo"]
    assert bill["relating_to_full"] == measure["RelatingToFull"].strip()
    assert json.loads(bill["raw_json"])["RelatingToFull"] == measure["RelatingToFull"]
    sponsors = service.storage.list_bill_sponsors(bill["id"])
    assert [(row["normalized_category"], row["resolved_display_name"]) for row in sponsors] == [
        ("chief", "Rob Wagner"),
        ("regular", "James Manning Jr"),
    ]

    documents = service.storage.list_bill_documents(bill["id"])
    assert len(documents) == 5
    by_identity = {(row["source_entity_type"], row["source_id"]): row for row in documents}
    # 244133 came from both sources but is one logical row, with richer HTML display metadata.
    assert by_identity[("CommitteePublicTestimony", "244133")]["submitter"] == "Sample Person"
    # 248220 exists only in the returned HTML and is still retained/downloaded.
    assert by_identity[("CommitteePublicTestimony", "248220")]["source_section"] == "submitted_written_testimony"
    assert by_identity[("FloorLetter", "4701")]["download_status"] == "failed_retryable"
    assert by_identity[("FloorLetter", "4701")]["attempt_count"] == 3
    assert all(
        row["download_status"] == "downloaded"
        for key, row in by_identity.items()
        if key != ("FloorLetter", "4701")
    )
    assert len(service.runs.errors(first_run)) == 1
    floor_item = next(
        item
        for item in service.runs.run_items(first_run)
        if item["item_type"] == "document" and item["document_id"] == by_identity[("FloorLetter", "4701")]["id"]
    )
    assert floor_item["attempt_count"] == 3

    stable_counts = {
        table: _row_count(service, table)
        for table in (
            "sessions", "legislators", "committees", "bills", "bill_sponsors",
            "committee_meetings", "committee_agenda_items", "documents",
        )
    }
    assert stable_counts == {
        "sessions": 1,
        "legislators": 2,
        "committees": 1,
        "bills": 1,
        "bill_sponsors": 2,
        "committee_meetings": 1,
        "committee_agenda_items": 1,
        "documents": 5,
    }

    # Retry is a separate durable run and includes only the failed logical document.
    downloader.fail_urls.clear()
    retry_run = service.create_retry_failures_run(first_run)
    retry_scope = json.loads(service.runs.get_run(retry_run)["requested_scope_json"])
    assert retry_scope["document_ids"] == [by_identity[("FloorLetter", "4701")]["id"]]
    assert service.execute_run(retry_run) == "completed"
    assert service.storage.get_document(by_identity[("FloorLetter", "4701")]["id"])["download_status"] == "downloaded"
    assert service.runs.get_run(retry_run)["documents_downloaded"] == 1
    floor_version = service.storage.list_document_versions(
        by_identity[("FloorLetter", "4701")]["id"]
    )[0]
    assert floor_version["collection_run_id"] == retry_run
    assert floor_version["source_url"] == failed_url

    calls_after_retry = len(downloader.calls)
    versions_after_retry = _row_count(service, "document_versions")
    first_collected_at = bill["first_collected_at"]

    # A complete bill rerun refreshes source metadata, preserves identities and
    # first-seen values, and validates/skips every already-complete local payload.
    rerun = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(rerun) == "completed"
    assert len(downloader.calls) == calls_after_retry
    assert service.runs.get_run(rerun)["documents_skipped"] == 5
    assert _row_count(service, "document_versions") == versions_after_retry == 5
    assert {
        table: _row_count(service, table)
        for table in stable_counts
    } == stable_counts
    assert service.storage.get_bill("2026R1", "SB1501")["first_collected_at"] == first_collected_at


def test_low_space_pauses_run_and_same_run_can_resume_cleanly(tmp_path: Path, fixture_dir: Path):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(encoding="utf-8")
    downloader = FakeDownloader()
    floor_url = "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/FloorLetter/4701"
    downloader.low_space_urls.add(floor_url)
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        download_worker_count=1,
        inter_request_delay=0,
    )
    service = CollectionService(
        config,
        odata=FakeOData(measure),
        olis_http=FakeOLISHTTP(html),
        downloader=downloader,
        sleep=lambda _seconds: None,
    )

    run_id = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(run_id) == "paused"
    paused = service.runs.get_run(run_id)
    assert paused["status"] == "paused"
    assert paused["finished_at"] is None
    floor = service.storage.get_document_by_identity(
        "2026R1", "SB1501", "FloorLetter", "4701"
    )
    assert floor["download_status"] == "paused_low_space"
    assert paused["documents_failed"] == 0
    assert len(service.runs.unresolved_errors(run_id)) == 1

    downloader.low_space_urls.clear()
    assert service.runs.requeue(run_id)
    assert service.execute_run(run_id) == "completed"
    assert service.storage.get_document(floor["id"])["download_status"] == "downloaded"
    assert service.runs.get_run(run_id)["error_count"] == 0
    errors = service.runs.errors(run_id)
    assert len(errors) == 1 and errors[0]["resolved_at"] is not None


def test_startup_normalizes_active_work_and_removes_blocking_part(tmp_path: Path):
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        inter_request_delay=0,
    )
    database = Database(config.database_path)
    database.initialize()
    storage = StorageService(database, initialize=False)
    storage.upsert_session({"session_key": "2026R1", "session_name": "2026 Regular Session"})
    bill_id = storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1501,
            "bill_id_compact": "SB1501",
            "bill_id_display": "SB 1501",
            "bill_chamber": "Senate",
        }
    )
    document_id = storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "submitted_written_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "244133",
            "canonical_download_url": "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/PublicTestimonyDocument/244133",
        }
    )
    runs = RunStore(database)
    run_id = runs.create_run(
        "collect_bill", session_key="2026R1", bill_id_compact="SB1501", bills_total=1
    )
    assert runs.claim_run(run_id)
    runs.add_document_item(run_id, document_id, bill_id, "CommitteePublicTestimony:244133")
    assert storage.queue_document(document_id)
    assert storage.claim_document(document_id)

    part = (
        config.archive_root
        / "2026R1"
        / "SB1501"
        / "public_testimony"
        / "244133"
        / "Testimony.pdf.part"
    )
    part.parent.mkdir(parents=True)
    part.write_bytes(b"incomplete")

    restarted = build_runtime(config=config)
    assert not part.exists()
    assert restarted.collection.runs.get_run(run_id)["status"] == "interrupted"
    assert restarted.storage.get_document(document_id)["download_status"] == "interrupted"
    assert restarted.collection.runs.requeue(run_id)
    assert restarted.storage.get_document(document_id)["download_status"] == "queued"
    # With deterministic cleanup complete, the durable item can be claimed
    # immediately instead of colliding with an orphaned `.part` file.
    assert restarted.storage.claim_document(document_id)


def test_losing_document_claim_does_not_demote_another_workers_active_transfer(tmp_path: Path):
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        inter_request_delay=0,
    )
    service = CollectionService(config)
    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"}
    )
    bill_id = service.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1501,
            "bill_id_compact": "SB1501",
            "bill_id_display": "SB 1501",
            "bill_chamber": "Senate",
        }
    )
    document_id = service.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "submitted_written_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "244133",
            "canonical_download_url": "https://olis.oregonlegislature.gov/document.pdf",
        }
    )
    competing_run = service.runs.create_run(
        "collect_bill",
        session_key="2026R1",
        bill_id_compact="SB1501",
        bills_total=1,
    )
    assert service.runs.claim_run(competing_run)
    service.runs.add_document_item(
        competing_run,
        document_id,
        bill_id,
        "CommitteePublicTestimony:244133",
    )
    assert service.storage.queue_document(document_id)
    assert service.storage.claim_document(document_id)  # owned by a different worker

    document = service.storage.get_document(document_id)
    assert document is not None
    assert not service._claim_download_attempt(competing_run, document)

    still_owned = service.storage.get_document(document_id)
    assert still_owned is not None
    assert still_owned["download_status"] == "downloading"
    assert still_owned["attempt_count"] == 1
    item = next(
        row
        for row in service.runs.run_items(competing_run)
        if row["item_type"] == "document"
    )
    assert item["status"] == "failed_retryable"


def test_collect_session_loads_reference_data_once_and_honors_max_bills(
    tmp_path: Path,
    fixture_dir: Path,
):
    base = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    second = {
        **base,
        "MeasureNumber": 1502,
        "RelatingTo": "Relating to a second fixture bill.",
        "RelatingToFull": "Relating to a second fixture bill.",
        "ChapterNumber": None,
        "EffectiveDate": None,
    }
    excluded_by_limit = {
        **base,
        "MeasurePrefix": "HB",
        "MeasureNumber": 4001,
        "PrefixMeaning": "House Bill",
        "RelatingTo": "Relating to a bill beyond the requested limit.",
        "RelatingToFull": "Relating to a bill beyond the requested limit.",
    }
    odata = FakeOData(base, measures=[base, second, excluded_by_limit])
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(encoding="utf-8")
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        download_worker_count=1,
        inter_request_delay=0,
    )
    service = CollectionService(
        config,
        odata=odata,
        olis_http=FakeOLISHTTP(html),
        downloader=FakeDownloader(),
        sleep=lambda _seconds: None,
    )

    run_id = service.create_collect_session_run("2026R1", max_bills=2)
    assert service.execute_run(run_id) == "completed"

    assert odata.measure_limits == [2]
    assert odata.session_calls == 1
    assert odata.calls.count(("query", "Legislators")) == 1
    assert odata.calls.count(("query", "Committees")) == 1
    assert service.runs.get_run(run_id)["bills_total"] == 2
    assert service.runs.get_run(run_id)["bills_completed"] == 2
    assert service.storage.get_bill("2026R1", "SB1501") is not None
    assert service.storage.get_bill("2026R1", "SB1502") is not None
    assert service.storage.get_bill("2026R1", "HB4001") is None

    stage_items = [
        item for item in service.runs.run_items(run_id) if item["item_type"] == "stage"
    ]
    assert sum(item["stage"] == "load_session" for item in stage_items) == 1
    assert sum(item["stage"] == "load_reference_data" for item in stage_items) == 1
    assert _row_count(service, "legislators") == 2
    assert _row_count(service, "committees") == 1


def test_reference_refresh_uses_verified_source_date_watermarks_and_retained_names(
    tmp_path: Path,
    fixture_dir: Path,
):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))

    class IncrementalOData(FakeOData):
        def __init__(self, raw_measure):
            super().__init__(raw_measure)
            self.filters: dict[str, str] = {}

        def query(self, entity_set: str, **params: Any):
            if entity_set in {"Legislators", "Committees"}:
                self.filters[entity_set] = str(params.get("filter"))
            return super().query(entity_set, **params)

    odata = IncrementalOData(measure)
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )
    service = CollectionService(config, odata=odata, sleep=lambda _seconds: None)
    service.storage.upsert_session({"session_key": "2026R1"})
    service.storage.upsert_legislator(
        {
            "session_key": "2026R1",
            "legislator_code": "Sen Retained",
            "display_name": "Riley Retained",
            "source_created_at": "2026-01-08T16:46:04",
            "source_modified_at": "2026-05-15T12:28:47",
        }
    )
    service.storage.upsert_committee(
        {
            "session_key": "2026R1",
            "committee_code": "SRETAINED",
            "committee_name": "Retained Records",
            "committee_type": "Senate Committee On",
            "source_created_at": "2026-01-09T16:56:56",
            "source_modified_at": "2026-02-25T15:31:01",
        }
    )
    run_id = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.runs.claim_run(run_id)

    references = service._load_reference_data(
        run_id,
        "2026R1",
        item_key="incremental-reference-test",
    )

    assert "ModifiedDate ge datetime'2026-05-15T12:28:47'" in odata.filters["Legislators"]
    assert "CreatedDate ge datetime'2026-05-15T12:28:47'" in odata.filters["Legislators"]
    assert "ModifiedDate ge datetime'2026-02-25T15:31:01'" in odata.filters["Committees"]
    assert references.legislator_names["Sen Retained"] == "Riley Retained"
    assert references.committee_names["SRETAINED"] == "Senate Committee On Retained Records"


def test_single_bill_fatal_failure_finishes_the_current_stage_as_failed(
    tmp_path: Path,
    fixture_dir: Path,
):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))

    class MissingSessionOData(FakeOData):
        def get_session(self, session_key: str) -> dict[str, Any] | None:
            assert session_key == "2026R1"
            return None

    service = CollectionService(
        AppConfig(
            database_path=tmp_path / "legiview.sqlite3",
            archive_root=tmp_path / "archive",
            inter_request_delay=0,
        ),
        odata=MissingSessionOData(measure),
        olis_http=FakeOLISHTTP("<html><body><p>No items to display</p></body></html>"),
        downloader=FakeDownloader(),
        sleep=lambda _seconds: None,
    )

    run_id = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(run_id) == "failed"

    run = service.runs.get_run(run_id)
    assert run is not None
    assert run["stage"] == "load_session"
    items = service.runs.run_items(run_id)
    failed_stage = next(item for item in items if item["item_key"] == "SB1501:load_session")
    assert failed_stage["status"] == "failed_terminal"
    assert failed_stage["finished_at"] is not None
    assert "not found" in failed_stage["current_activity"]
    assert all(item["status"] != "running" for item in items)


def test_shutdown_interruption_is_not_overwritten_by_an_inflight_source_error(
    tmp_path: Path,
    fixture_dir: Path,
):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    holder: dict[str, CollectionService] = {}

    class InterruptedOData(FakeOData):
        def get_session(self, session_key: str) -> dict[str, Any] | None:
            holder["service"].storage.normalize_interrupted_work()
            raise TimeoutError("request unwound after shutdown")

    service = CollectionService(
        AppConfig(
            database_path=tmp_path / "legiview.sqlite3",
            archive_root=tmp_path / "archive",
            inter_request_delay=0,
        ),
        odata=InterruptedOData(measure),
        sleep=lambda _seconds: None,
    )
    holder["service"] = service

    run_id = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(run_id) == "interrupted"
    assert service.runs.get_run(run_id)["status"] == "interrupted"
    items = service.runs.run_items(run_id)
    assert items[0]["status"] == "interrupted"
    assert all(item["item_key"] != "finalize" for item in items)
    assert service.runs.errors(run_id) == []


def test_session_bill_failure_keeps_failed_bill_and_stage_after_later_work(
    tmp_path: Path,
    fixture_dir: Path,
):
    first = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    second = {
        **first,
        "MeasureNumber": 1502,
        "RelatingTo": "Relating to a later fixture bill.",
        "RelatingToFull": "Relating to a later fixture bill.",
        "ChapterNumber": None,
        "EffectiveDate": None,
    }

    class FirstBillFailsOData(FakeOData):
        def for_measure(
            self,
            entity_set: str,
            session_key: str,
            prefix: str,
            number: int,
            **params: Any,
        ) -> list[dict[str, Any]]:
            if (prefix, number, entity_set) == ("SB", 1501, "MeasureSponsors"):
                raise ValueError("fixture sponsor mapping failed")
            return super().for_measure(entity_set, session_key, prefix, number, **params)

    service = CollectionService(
        AppConfig(
            database_path=tmp_path / "legiview.sqlite3",
            archive_root=tmp_path / "archive",
            download_worker_count=1,
            inter_request_delay=0,
        ),
        odata=FirstBillFailsOData(first, measures=[first, second]),
        olis_http=FakeOLISHTTP("<html><body><p>No items to display</p></body></html>"),
        downloader=FakeDownloader(),
        sleep=lambda _seconds: None,
    )

    run_id = service.create_collect_session_run("2026R1")
    assert service.execute_run(run_id) == "completed_with_errors"

    items = service.runs.run_items(run_id)
    failed_bill = next(item for item in items if item["item_key"] == "bill:SB1501")
    failed_stage = next(item for item in items if item["item_key"] == "SB1501:load_sponsors")
    later_bill = next(item for item in items if item["item_key"] == "bill:SB1502")
    finalize = next(item for item in items if item["item_key"] == "finalize")
    assert failed_bill["status"] == "failed_terminal"
    assert failed_stage["status"] == "failed_terminal"
    assert failed_stage["finished_at"] is not None
    assert later_bill["status"] == "completed"
    assert finalize["status"] == "completed"
    assert all(item["status"] != "running" for item in items)


def test_normal_bill_rerun_preserves_terminal_failure_without_redownload(
    tmp_path: Path,
    fixture_dir: Path,
):
    measure = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text(encoding="utf-8"))
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(encoding="utf-8")
    downloader = FakeDownloader()
    terminal_url = "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/FloorLetter/4701"
    downloader.terminal_urls.add(terminal_url)
    config = AppConfig(
        database_path=tmp_path / "legiview.sqlite3",
        archive_root=tmp_path / "archive",
        download_worker_count=1,
        inter_request_delay=0,
    )
    service = CollectionService(
        config,
        odata=FakeOData(measure),
        olis_http=FakeOLISHTTP(html),
        downloader=downloader,
        sleep=lambda _seconds: None,
    )

    original_run = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(original_run) == "completed_with_errors"
    terminal_document = service.storage.get_document_by_identity(
        "2026R1", "SB1501", "FloorLetter", "4701"
    )
    assert terminal_document["download_status"] == "failed_terminal"
    assert downloader.calls.count(terminal_url) == 1

    call_count = len(downloader.calls)
    rerun = service.create_collect_bill_run("2026R1", "SB1501")
    assert service.execute_run(rerun) == "completed_with_errors"

    # Valid payloads are skipped and the retained terminal failure is surfaced
    # into this run's own durable accounting without another network attempt.
    assert len(downloader.calls) == call_count
    assert downloader.calls.count(terminal_url) == 1
    rerun_header = service.runs.get_run(rerun)
    assert rerun_header["documents_failed"] == 1
    assert rerun_header["documents_skipped"] == 4
    assert rerun_header["error_count"] == 1
    errors = service.runs.errors(rerun)
    assert len(errors) == 1
    assert json.loads(errors[0]["details_json"])["preserved_terminal_failure"] is True
    assert service.storage.get_document(terminal_document["id"])["attempt_count"] == 1
