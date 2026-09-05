from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import threading

import pytest

from olis_archive.config import AppConfig
from olis_archive.runtime import build_runtime
from olis_archive.services.collection import CollectionService
from olis_archive.services.downloads import Downloader
from olis_archive.services.historical_collection import ClaimedPreparation, HistoricalRunControl
from olis_archive.services.historical_sources import (
    REQUIRED_METADATA_PROPERTIES,
    HistoricalSourceError,
)
from olis_archive.services.odata import ODataPage
from olis_archive.services.probes import ProbeResult


T1 = "2026-01-02T03:04:05Z"
T2 = "2026-02-03T04:05:06Z"
FUTURE = "2999-01-01T00:00:00Z"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
PDF_BYTES_V2 = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture
def archive_server():
    """Mutable loopback-only payload source for archive integration tests."""

    state: dict[str, object] = {
        "bodies": defaultdict(lambda: PDF_BYTES),
        "failures_remaining": defaultdict(int),
    }
    counters: defaultdict[str, int] = defaultdict(int)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            counters[path] += 1
            failures = state["failures_remaining"]
            assert isinstance(failures, defaultdict)
            if failures[path] > 0:
                failures[path] -= 1
                self.send_response(503)
                self.end_headers()
                return
            bodies = state["bodies"]
            assert isinstance(bodies, defaultdict)
            body = bytes(bodies[path])
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="Evidence.pdf"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, counters
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _session(key: str, begin: str) -> dict[str, object]:
    return {
        "SessionKey": key,
        "SessionName": f"Session {key}",
        "SessionType": "Regular",
        "BeginDate": begin,
        "EndDate": None,
        "CreatedDate": begin,
        "ModifiedDate": None,
        "DefaultSession": key == "2026R1",
    }


def _valid_metadata() -> str:
    entity_types: list[str] = []
    entity_sets: list[str] = []
    for entity_set, properties in REQUIRED_METADATA_PROPERTIES.items():
        type_name = f"{entity_set}Row"
        entity_types.append(
            f'<EntityType Name="{type_name}">'
            + "".join(
                f'<Property Name="{name}" Type="Edm.String" />'
                for name in sorted(properties)
            )
            + "</EntityType>"
        )
        entity_sets.append(
            f'<EntitySet Name="{entity_set}" EntityType="Test.{type_name}" />'
        )
    return (
        '<edmx:Edmx xmlns:edmx="urn:edmx">'
        '<edmx:DataServices><Schema xmlns="urn:edm" Namespace="Test">'
        + "".join(entity_types)
        + '<EntityContainer Name="Container">'
        + "".join(entity_sets)
        + "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    )


def _modern_rows() -> dict[str, list[dict[str, object]]]:
    created = "2026-02-01T10:00:00"
    modified = "2026-02-02T11:00:00"
    return {
        "Legislators": [
            {
                "SessionKey": "2026R1",
                "LegislatorCode": "SMITH",
                "FirstName": "Sample",
                "LastName": "Smith",
                "Chamber": "S",
                "CreatedDate": created,
                "ModifiedDate": modified,
            }
        ],
        "Committees": [
            {
                "SessionKey": "2026R1",
                "CommitteeCode": "SRULES",
                "CommitteeName": "Rules",
                "CommitteeType": "Senate Committee On",
                "HouseOfAction": "S",
                "CreatedDate": created,
                "ModifiedDate": modified,
            }
        ],
        "Measures": [
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "HB",
                "MeasureNumber": 1001,
                "RelatingTo": "schools",
                "RelatingToFull": "Relating to schools.",
                "CreatedDate": created,
                "ModifiedDate": None,
            },
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "SB",
                "MeasureNumber": 1501,
                "RelatingTo": "public records",
                "RelatingToFull": "Relating to public records.",
                "CreatedDate": created,
                "ModifiedDate": modified,
                "CurrentCommitteeCode": "SRULES",
            },
        ],
        "MeasureSponsors": [
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "SB",
                "MeasureNumber": 1501,
                "MeasureSponsorId": 501,
                "SponsorType": "Member",
                "SponsorLevel": "Chief",
                "LegislatoreCode": "SMITH",
                "CreatedDate": created,
                "ModifiedDate": modified,
            }
        ],
        "CommitteeMeetings": [
            {
                "SessionKey": "2026R1",
                "CommitteeCode": "SRULES",
                "MeetingDate": "2026-02-11T08:00:00",
                "MeetingType": "Public Hearing",
                "CreatedDate": created,
                "ModifiedDate": modified,
            }
        ],
        "CommitteeAgendaItems": [
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "SB",
                "MeasureNumber": 1501,
                "CommitteeAgendaItemId": 601,
                "CommitteCode": "SRULES",
                "MeetingDate": "2026-02-11T08:00:00",
                "MeetingType": "Public Hearing",
                "CreatedDate": created,
                "ModifiedDate": modified,
            }
        ],
        "CommitteeMeetingDocuments": [],
        "CommitteePublicTestimonies": [
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "SB",
                "MeasureNumber": 1501,
                "CommTestId": 244133,
                "CommitteeCode": "SRULES",
                "MeetingDate": "2026-02-11T08:00:00",
                "SubmitterFirstName": "Sample",
                "SubmitterLastName": "Person",
                "DocumentDescription": "Testimony",
                "PositionOnMeasureId": 3983,
                "PdfCreatedFlag": "Y",
                "CreatedDate": created,
                "ModifiedDate": modified,
            },
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "SB",
                "MeasureNumber": 1501,
                "CommTestId": 255890,
                "CommitteeCode": "SRULES",
                "MeetingDate": "2026-02-11T08:00:00",
                "SubmitterFirstName": "OData",
                "SubmitterLastName": "Only",
                "DocumentDescription": "Testimony",
                "PositionOnMeasureId": 3983,
                "PdfCreatedFlag": "Y",
                "CreatedDate": created,
                "ModifiedDate": modified,
            },
        ],
        "FloorLetters": [
            {
                "SessionKey": "2026R1",
                "MeasurePrefix": "HB",
                "MeasureNumber": 1001,
                "FloorLetterId": 701,
                "FloorLetterUrl": (
                    "https://olis.oregonlegislature.gov/liz/2026R1/"
                    "Downloads/FloorLetter/701"
                ),
                "LetterTitle": "Floor letter",
                "Chamber": "H",
            }
        ],
    }


class FakePagedOData:
    def __init__(self) -> None:
        self.sessions = [
            _session("2013R1", "2013-01-14T00:00:00"),
            _session("2014R1", "2014-02-03T00:00:00"),
            _session("2014S1", "2014-09-15T00:00:00"),
            _session("2026R1", "2026-02-02T00:00:00"),
        ]
        self.rows = _modern_rows()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.metadata_calls = 0
        self.fail_once_after_page: dict[str, int] = {}

    def iter_pages(self, entity_set: str, **params: object):
        self.calls.append((entity_set, dict(params)))
        rows = self.sessions if entity_set == "LegislativeSessions" else self.rows[entity_set]
        page_size = 1 if entity_set in {"LegislativeSessions", "Measures"} else max(1, len(rows))
        for start in range(0, len(rows), page_size):
            page_number = start // page_size + 1
            end = min(start + page_size, len(rows))
            yield ODataPage(
                tuple(dict(row) for row in rows[start:end]),
                f"https://example.test/{entity_set}?page={page_number + 1}"
                if end < len(rows)
                else None,
                len(rows),
                "https://example.test/$metadata",
            )
            if self.fail_once_after_page.get(entity_set) == page_number:
                del self.fail_once_after_page[entity_set]
                raise TimeoutError(f"{entity_set} page {page_number + 1} timed out")

    def build_url(self, entity_set: str, **params: object) -> str:
        return f"https://example.test/{entity_set}?{json.dumps(params, sort_keys=True)}"

    def get_metadata_xml(self):
        self.metadata_calls += 1
        return _valid_metadata(), {}, "https://example.test/$metadata"


class FakeOLIS:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def testimony_url(session_key: str, bill_id: str) -> str:
        return (
            f"https://olis.oregonlegislature.gov/liz/{session_key}/"
            f"Measures/Testimony/{bill_id}"
        )

    def get_testimony_page(self, session_key: str, bill_id: str):
        self.calls.append((session_key, bill_id))
        return SimpleNamespace(
            text=self.html,
            url=self.testimony_url(session_key, bill_id),
            status_code=200,
        )


class NoPayloadDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download_to_path(self, url: str, *_args, **_kwargs):
        self.calls.append(url)
        raise AssertionError("Inventory metadata work must not download payload bytes")


class FakeProbe:
    def __init__(self, content_length: int = 100) -> None:
        self.content_length = content_length
        self.calls: list[str] = []

    def probe(self, url: str) -> ProbeResult:
        self.calls.append(url)
        return ProbeResult(
            status="known",
            method="HEAD",
            source_url=url,
            final_url=url,
            http_status=200,
            content_type="application/pdf",
            content_length=self.content_length,
            etag=None,
            last_modified=None,
        )


def _service(tmp_path: Path, fixture_dir: Path, *, workers: int = 2):
    config = AppConfig(
        project_root=tmp_path,
        database_path=Path("data/legiview.sqlite3"),
        archive_root=Path("archive"),
        minimum_free_space_gb=0,
        minimum_free_space_bytes=0,
        inter_request_delay=0,
        download_worker_count=workers,
    )
    odata = FakePagedOData()
    olis = FakeOLIS((fixture_dir / "modern_testimony_2026_sb1501.html").read_text())
    downloader = NoPayloadDownloader()
    service = CollectionService(
        config,
        odata=odata,
        olis_http=olis,
        downloader=downloader,
        sleep=lambda _seconds: None,
    )
    return service, odata, olis, downloader


def _mark_session_inventoried(
    service: CollectionService, session_key: str = "2026R1"
) -> None:
    state = service.storage.get_session_archive_state(session_key)
    if state and state["inventory_status"] != "not_started":
        return
    run_id = service.runs.create_run(
        "inventory_backfill", session_keys=[session_key]
    )
    assert service.runs.claim_run(run_id)
    service.storage.mark_session_inventory_started(session_key, run_id, started_at=T1)
    service.storage.finish_session_inventory(
        session_key,
        run_id,
        "inventory_complete",
        completed_at=T1,
    )
    service.runs.finish_item(run_id, "session", session_key, "completed")
    service.runs.set_counters(
        run_id, sessions_total=1, sessions_completed=1
    )
    assert service.runs.finish_run(run_id) == "completed"


def _use_loopback_downloader(
    service: CollectionService, *, minimum_free_space_bytes: int = 0
) -> None:
    service.downloader = Downloader(
        allowed_hosts={"127.0.0.1"},
        allow_private_network=True,
        minimum_free_space_bytes=minimum_free_space_bytes,
        chunk_size=11,
        calculate_sha256=False,
        durable_writes=False,
    )


def _seed_archive_document(
    service: CollectionService,
    source_url: str,
    *,
    source_id: str,
    source_modified_at: str = T1,
    existing_payload: bytes | None = None,
    seen_at: str = T1,
) -> tuple[int, Path | None, int | None]:
    """Create one inventoried document and optionally a validated current version."""

    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"},
        seen_at=seen_at,
    )
    _mark_session_inventoried(service)
    bill = service.storage.get_bill("2026R1", "SB1")
    bill_id = int(bill["id"]) if bill else service.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1,
            "bill_id_compact": "SB1",
            "bill_title": "Archive test",
        },
        seen_at=seen_at,
    )
    document_id = service.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": source_id,
            "title": "Evidence.pdf",
            "canonical_download_url": source_url,
            "source_modified_at": source_modified_at,
        },
        seen_at=seen_at,
    )
    if existing_payload is None:
        return document_id, None, None

    relative = (
        Path("2026R1")
        / "SB1"
        / "public_testimony"
        / source_id
        / "Evidence.pdf"
    )
    path = service.config.archive_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(existing_payload)
    version_id = service.storage.complete_document_download(
        document_id,
        sha256=sha256(existing_payload).hexdigest(),
        local_relative_path=relative.as_posix(),
        downloaded_bytes=len(existing_payload),
        mime_type="application/pdf",
        local_filename=path.name,
        remote_filename=path.name,
        source_modified_at=source_modified_at,
        source_url=source_url,
        validation_status="valid",
        http_status=200,
        downloaded_at=T1,
    )
    return document_id, path, version_id


def test_inventory_run_freezes_official_scope_streams_pages_and_never_downloads(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, olis, downloader = _service(tmp_path, fixture_dir)

    run_id = service.create_inventory_backfill_run(["2026r1"])
    frozen = json.loads(service.runs.get_run(run_id)["requested_scope_json"])
    assert frozen["boundary_session_key"] == "2014R1"
    assert frozen["boundary_begin_date"] == "2014-02-03T00:00:00"
    assert frozen["session_keys"] == ["2026R1"]
    assert frozen["resolved_from"] == "official LegislativeSessions BeginDate chronology"
    assert {
        row["session_key"] for row in service.storage.list_sessions()
    } == {"2013R1", "2014R1", "2014S1", "2026R1"}
    odata.sessions.append(_session("2027R1", "2027-01-01T00:00:00"))

    assert service.execute_run(run_id) == "completed"
    assert service.historical.inventory_scope_from_run(run_id) == ("2026R1",)
    assert downloader.calls == []
    assert odata.metadata_calls == 1
    assert olis.calls == [("2026R1", "SB1501")]

    run = service.runs.get_run(run_id)
    assert run["sessions_total"] == 1
    assert run["sessions_completed"] == 1
    assert run["sessions_incomplete"] == 0
    run_items = service.runs.run_items(run_id)
    session_items = [
        item
        for item in run_items
        if item["item_type"] == "session"
        or str(item["item_key"]).startswith("2026R1:")
    ]
    assert session_items
    assert {item["session_key"] for item in session_items} == {"2026R1"}
    global_items = {
        str(item["item_key"]): item
        for item in run_items
        if item["item_key"] in {"resolve_sessions", "finalize_run"}
    }
    assert set(global_items) == {"resolve_sessions", "finalize_run"}
    assert {item["session_key"] for item in global_items.values()} == {None}
    state = service.storage.get_session_archive_state("2026R1")
    assert state["inventory_status"] == "inventory_complete"
    assert state["material_anomaly_count"] == 0

    with service.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bills WHERE session_key='2026R1'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM source_fetches WHERE entity_set='Measures' AND succeeded=1"
        ).fetchone()[0] == 2
        documents = connection.execute(
            """
            SELECT source_id,source_presence,displayed_in_olis,reconciliation_origin
            FROM documents WHERE source_entity_type='CommitteePublicTestimony'
            ORDER BY CAST(source_id AS INTEGER)
            """
        ).fetchall()
        display_rows = connection.execute(
            """
            SELECT source_entity_type,status,odata_record_count,
                   displayed_record_count,page_only_count,odata_only_count
            FROM olis_display_reconciliations
            WHERE bill_id=(
                SELECT id FROM bills
                WHERE session_key='2026R1' AND bill_id_compact='SB1501'
            )
            ORDER BY source_entity_type
            """
        ).fetchall()
    by_source = {str(row["source_id"]): dict(row) for row in documents}
    assert by_source["244133"]["reconciliation_origin"] == "odata_and_olis"
    assert by_source["255890"]["displayed_in_olis"] == 0
    assert by_source["255890"]["reconciliation_origin"] == "odata_only"
    assert by_source["244244"]["source_presence"] == "unknown"
    assert by_source["244244"]["displayed_in_olis"] == 1
    assert by_source["244244"]["reconciliation_origin"] == "olis_only"
    reconciliations = {
        str(row["source_entity_type"]): dict(row) for row in display_rows
    }
    assert set(reconciliations) == {
        "CommitteeMeetingDocument",
        "CommitteePublicTestimony",
    }
    assert reconciliations["CommitteePublicTestimony"]["odata_record_count"] == 2
    assert reconciliations["CommitteePublicTestimony"]["displayed_record_count"] == 3
    assert reconciliations["CommitteePublicTestimony"]["page_only_count"] == 2
    assert reconciliations["CommitteePublicTestimony"]["odata_only_count"] == 1
    assert reconciliations["CommitteeMeetingDocument"]["odata_record_count"] == 0
    assert reconciliations["CommitteeMeetingDocument"]["displayed_record_count"] == 0

    # An incremental overlap rerun is idempotent and uses inclusive cursors.
    second_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(second_id) == "completed"
    with service.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bills WHERE session_key='2026R1'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM documents WHERE session_key='2026R1'"
        ).fetchone()[0] == 5
    measure_calls = [params for entity, params in odata.calls if entity == "Measures"]
    assert "ModifiedDate ge datetime'2026-02-02T11:00:00'" in measure_calls[-1]["filter"]
    floor_calls = [params for entity, params in odata.calls if entity == "FloorLetters"]
    assert "datetime'" not in floor_calls[-1]["filter"]


def test_omitted_selection_freezes_every_official_session_at_or_after_boundary(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)

    run_id = service.create_inventory_backfill_run()
    frozen = json.loads(service.runs.get_run(run_id)["requested_scope_json"])

    assert frozen["boundary_session_key"] == "2014R1"
    assert frozen["session_keys"] == ["2014R1", "2014S1", "2026R1"]
    session_items = [
        item
        for item in service.runs.run_items(run_id)
        if item["item_type"] == "session"
    ]
    assert [item["session_key"] for item in session_items] == frozen["session_keys"]


def test_pre_boundary_cli_style_exact_selection_is_rejected_before_run_creation(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)

    with pytest.raises(
        HistoricalSourceError,
        match="predate LegiView's validated support boundary 2014R1.*2013R1",
    ):
        service.create_inventory_backfill_run(["2013R1"])

    assert service.runs.list_runs() == []


def test_direct_measure_and_session_runs_reject_pre_boundary_year_before_creation(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)

    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.create_collect_bill_run("2007R1", "HB2001")
    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.create_collect_session_run("2007R1")

    assert service.runs.list_runs() == []

    legacy_run = service.runs.create_run(
        "collect_bill",
        session_key="2007R1",
        bill_id_compact="HB2001",
        scope={"session_key": "2007R1", "bill_id_compact": "HB2001"},
    )
    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.create_retry_failures_run(legacy_run)
    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.create_retry_matching_run(session_key="2007R1")

    assert [row["id"] for row in service.runs.list_runs()] == [legacy_run]


def test_inventory_run_skips_incompatible_catalogue_rows_but_freezes_diagnostics(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    missing_key = _session("2001R1", "2001-01-01T00:00:00")
    missing_key.pop("SessionKey")
    odata.sessions.extend(
        [
            _session("1999R1", "1999-01-11T00:00:00"),
            _session("LEGACY-A", "not-a-date"),
            missing_key,
        ]
    )

    resolved = service.historical_session_scope()
    run_id = service.create_inventory_backfill_run(
        resolved_scope=resolved.selected(["2026R1"])
    )
    frozen = json.loads(service.runs.get_run(run_id)["requested_scope_json"])

    assert frozen["session_keys"] == ["2026R1"]
    assert len(frozen["catalogue_guardrails"]) == 3
    assert {row["session_key"] for row in service.storage.list_sessions()} == {
        "2013R1",
        "2014R1",
        "2014S1",
        "2026R1",
    }


@pytest.mark.parametrize("run_type", ("inventory_backfill", "download_archive"))
def test_legacy_frozen_historical_scope_fails_before_source_or_payload_work(
    tmp_path: Path,
    fixture_dir: Path,
    run_type: str,
):
    service, odata, _olis, downloader = _service(tmp_path, fixture_dir)
    run_id = service.runs.create_run(
        run_type,
        session_keys=["2007R1"],
        scope={"session_keys": ["2007R1"]},
    )

    assert service.execute_run(run_id) == "failed"
    assert odata.calls == []
    assert downloader.calls == []
    assert "2007R1" in service.runs.errors(run_id)[0]["message"]


@pytest.mark.parametrize("run_type", ("inventory_backfill", "download_archive"))
def test_legacy_historical_scope_cannot_be_requeued(
    tmp_path: Path,
    fixture_dir: Path,
    run_type: str,
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    run_id = service.runs.create_run(
        run_type,
        session_keys=["2007R1"],
        scope={"session_keys": ["2007R1"]},
    )
    assert service.runs.claim_run(run_id)
    service.storage.normalize_interrupted_work()
    assert service.runs.get_run(run_id)["status"] == "interrupted"

    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.requeue_run(run_id)

    assert service.runs.get_run(run_id)["status"] == "interrupted"


def test_download_preflight_rejects_or_ignores_legacy_inventory_scope(
    tmp_path: Path,
    fixture_dir: Path,
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    service.storage.upsert_session(
        {
            "session_key": "2007R1",
            "session_name": "2007 Regular Session",
            "session_year": 2007,
            "begin_date": "2007-01-08T00:00:00",
        }
    )
    inventory_run = service.runs.create_run(
        "inventory_backfill", session_keys=["2007R1"]
    )
    service.storage.finish_session_inventory(
        "2007R1", inventory_run, "inventory_complete"
    )

    with pytest.raises(HistoricalSourceError, match="2007R1.*2014R1"):
        service.download_archive_preflight(["2007R1"])
    with pytest.raises(ValueError, match="No inventoried sessions"):
        service.download_archive_preflight()


def test_pre_resolved_range_snapshot_is_frozen_without_second_source_scan(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    official = service.historical_session_scope()
    selected = official.selected_range("2014S1", "2026R1")
    session_scans_before = sum(
        entity == "LegislativeSessions" for entity, _params in odata.calls
    )

    run_id = service.create_inventory_backfill_run(resolved_scope=selected)

    session_scans_after = sum(
        entity == "LegislativeSessions" for entity, _params in odata.calls
    )
    assert session_scans_after == session_scans_before == 1
    frozen = json.loads(service.runs.get_run(run_id)["requested_scope_json"])
    assert frozen["session_keys"] == ["2014S1", "2026R1"]
    assert frozen["boundary_session_key"] == "2014R1"
    assert frozen["boundary_begin_date"] == "2014-02-03T00:00:00"
    assert {
        row["session_key"] for row in service.storage.list_sessions()
    } == {"2013R1", "2014R1", "2014S1", "2026R1"}


def test_inventory_run_creation_rolls_back_run_and_catalogue_on_persistence_failure(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    original_upsert = service.storage.upsert_session
    upsert_count = 0

    def fail_during_catalogue(*args, **kwargs):
        nonlocal upsert_count
        result = original_upsert(*args, **kwargs)
        upsert_count += 1
        if upsert_count == 2:
            raise RuntimeError("injected session catalogue failure")
        return result

    monkeypatch.setattr(service.storage, "upsert_session", fail_during_catalogue)

    with pytest.raises(RuntimeError, match="injected session catalogue failure"):
        service.create_inventory_backfill_run(["2026R1"])

    assert upsert_count == 2
    with service.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM collection_run_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_multi_session_resume_skips_completed_session_and_finishes_same_run(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    original_iter_pages = odata.iter_pages

    def session_aware_pages(entity_set: str, **params):
        if (
            entity_set != "LegislativeSessions"
            and "2014R1" in str(params.get("filter") or "")
        ):
            yield ODataPage(
                (),
                None,
                0,
                "https://example.test/$metadata",
            )
            return
        yield from original_iter_pages(entity_set, **params)

    monkeypatch.setattr(odata, "iter_pages", session_aware_pages)
    run_id = service.create_inventory_backfill_run(["2014R1", "2026R1"])
    original_inventory_session = service.historical._inventory_session
    original_sync_entity = service.historical._sync_entity
    session_attempts: list[str] = []
    interrupted = False

    def track_session(
        current_run_id: int,
        session_key: str,
        **kwargs,
    ):
        session_attempts.append(session_key)
        return original_inventory_session(current_run_id, session_key, **kwargs)

    def interrupt_second_session(current_run_id, run_item_id, plan):
        nonlocal interrupted
        if plan.session_key == "2026R1" and not interrupted:
            interrupted = True
            assert service.runs.pause(
                current_run_id, "test interruption during second session"
            )
            raise HistoricalRunControl("paused")
        return original_sync_entity(current_run_id, run_item_id, plan)

    monkeypatch.setattr(service.historical, "_inventory_session", track_session)
    monkeypatch.setattr(service.historical, "_sync_entity", interrupt_second_session)

    assert service.execute_run(run_id) == "paused"
    first_items = {
        str(row["session_key"]): str(row["status"])
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "session"
    }
    assert first_items == {"2014R1": "completed", "2026R1": "paused"}
    assert session_attempts == ["2014R1", "2026R1"]

    assert service.runs.requeue(run_id)
    assert service.execute_run(run_id) == "completed"
    assert session_attempts == ["2014R1", "2026R1", "2026R1"]
    run = service.runs.get_run(run_id)
    assert run["sessions_total"] == 2
    assert run["sessions_completed"] == 2
    assert run["sessions_incomplete"] == 0
    assert run["sessions_failed"] == 0
    final_items = {
        str(row["session_key"]): str(row["status"])
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "session"
    }
    assert final_items == {"2014R1": "completed", "2026R1": "completed"}


def test_multi_session_restart_normalizes_active_session_and_skips_completed_one(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    class SimulatedProcessExit(BaseException):
        pass

    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    original_iter_pages = odata.iter_pages

    def session_aware_pages(entity_set: str, **params):
        if (
            entity_set != "LegislativeSessions"
            and "2014R1" in str(params.get("filter") or "")
        ):
            yield ODataPage((), None, 0, "https://example.test/$metadata")
            return
        yield from original_iter_pages(entity_set, **params)

    monkeypatch.setattr(odata, "iter_pages", session_aware_pages)
    run_id = service.create_inventory_backfill_run(["2014R1", "2026R1"])
    original_sync_entity = service.historical._sync_entity

    def crash_in_second_session(current_run_id, run_item_id, plan):
        if plan.session_key == "2026R1":
            raise SimulatedProcessExit()
        return original_sync_entity(current_run_id, run_item_id, plan)

    monkeypatch.setattr(service.historical, "_sync_entity", crash_in_second_session)

    with pytest.raises(SimulatedProcessExit):
        service.execute_run(run_id)
    assert service.runs.get_run(run_id)["status"] == "running"
    before_restart = {
        str(row["session_key"]): str(row["status"])
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "session"
    }
    assert before_restart == {"2014R1": "completed", "2026R1": "running"}

    # Build a genuinely new runtime against the same database. Its normal
    # startup path must convert falsely-active durable state to interrupted.
    restarted_runtime = build_runtime(service.config, clean_parts=False)
    assert restarted_runtime.collection.runs.get_run(run_id)["status"] == "interrupted"
    after_restart = {
        str(row["session_key"]): str(row["status"])
        for row in restarted_runtime.collection.runs.run_items(run_id)
        if row["item_type"] == "session"
    }
    assert after_restart == {"2014R1": "completed", "2026R1": "interrupted"}
    assert restarted_runtime.storage.get_session_archive_state("2014R1")[
        "inventory_status"
    ] == "inventory_complete"
    assert restarted_runtime.storage.get_session_archive_state("2026R1")[
        "inventory_status"
    ] == "interrupted"

    resumed_service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    original_inventory_session = resumed_service.historical._inventory_session
    resumed_sessions: list[str] = []

    def track_resumed_session(current_run_id: int, session_key: str, **kwargs):
        resumed_sessions.append(session_key)
        return original_inventory_session(current_run_id, session_key, **kwargs)

    monkeypatch.setattr(
        resumed_service.historical, "_inventory_session", track_resumed_session
    )
    assert resumed_service.runs.requeue(run_id)
    assert resumed_service.execute_run(run_id) == "completed"
    assert resumed_sessions == ["2026R1"]
    run = resumed_service.runs.get_run(run_id)
    assert run["sessions_total"] == 2
    assert run["sessions_completed"] == 2
    assert run["sessions_incomplete"] == 0
    assert run["sessions_failed"] == 0


def test_later_successful_display_check_resolves_stale_mismatches_before_recording(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, olis, _downloader = _service(tmp_path, fixture_dir)
    first_run = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(first_run) == "completed"
    bill_id = int(service.storage.get_bill("2026R1", "SB1501")["id"])
    with service.database.connection() as connection:
        open_types = {
            str(row["anomaly_type"])
            for row in connection.execute(
                """
                SELECT anomaly_type FROM source_anomalies
                WHERE bill_id=? AND resolved_at IS NULL
                """,
                (bill_id,),
            ).fetchall()
        }
    assert {
        "odata_olis_count_mismatch",
        "odata_only_display_candidate",
        "olis_page_only_record",
    } <= open_types

    olis.html = """<!doctype html>
    <html><body>
    <h5>Submitted Written Public Testimony</h5>
    <div id="publicTestimony"><table id="ExhibitsTable" class="data-table">
      <thead><tr><th>Title</th><th>Submitter</th><th>On Behalf of</th><th>Position</th><th>City or Organization</th><th>Meeting</th><th>Committee</th></tr></thead>
      <tbody>
        <tr><td><a href="/liz/2026R1/Downloads/PublicTestimonyDocument/244133">Testimony</a></td><td>Sample Person</td><td></td><td>Support</td><td>Portland</td><td>2/11/2026</td><td>Senate Committee On Rules</td></tr>
        <tr><td><a href="/liz/2026R1/Downloads/PublicTestimonyDocument/255890">Testimony</a></td><td>OData Only</td><td></td><td>Support</td><td>Oregon</td><td>2/11/2026</td><td>Senate Committee On Rules</td></tr>
      </tbody>
    </table></div>
    </body></html>"""
    second_run = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(second_run) == "completed"

    with service.database.connection() as connection:
        unresolved = connection.execute(
            """
            SELECT COUNT(*) FROM source_anomalies
            WHERE bill_id=? AND resolved_at IS NULL
              AND anomaly_type IN (
                  'odata_olis_count_mismatch',
                  'odata_only_display_candidate',
                  'olis_page_only_record'
              )
            """,
            (bill_id,),
        ).fetchone()[0]
        retained = connection.execute(
            """
            SELECT COUNT(*) FROM source_anomalies
            WHERE bill_id=? AND resolved_at IS NOT NULL
              AND anomaly_type IN (
                  'odata_olis_count_mismatch',
                  'odata_only_display_candidate',
                  'olis_page_only_record'
              )
            """,
            (bill_id,),
        ).fetchone()[0]
    assert unresolved == 0
    assert retained >= 3


def test_incremental_display_reconciliation_reuses_unchanged_successful_page(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, olis, _downloader = _service(tmp_path, fixture_dir)
    first_run = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(first_run) == "completed"
    calls_after_first = list(olis.calls)

    second_run = service.create_inventory_backfill_run(["2026R1"])
    assert service.runs.claim_run(second_run)
    session_item = service.runs.begin_stage(
        second_run,
        "sync_session",
        "Synchronizing official inventory for 2026R1",
        item_key="2026R1",
        item_type="session",
        session_key="2026R1",
    )
    result = service.historical._reconcile_session_display(
        second_run,
        "2026R1",
        session_item,
    )

    assert olis.calls == calls_after_first
    assert result["counts"]["olis_pages_reused"] == 1
    assert result["counts"]["olis_pages_checked"] == 0


def test_interrupted_same_run_reuses_checks_older_than_source_inputs(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, olis, _downloader = _service(tmp_path, fixture_dir)
    run_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(run_id) == "completed"
    bill = service.storage.get_bill("2026R1", "SB1501")
    assert bill is not None
    assert int(bill["last_collected_run_id"]) == run_id

    # A resumed durable run retains its ID.  Its successful page check remains
    # reusable when every source observation for the candidate predates it.
    calls_after_first = list(olis.calls)
    assert (
        service.historical._reusable_display_status(run_id, int(bill["id"]))
        == "checked_with_records"
    )
    assert olis.calls == calls_after_first

    with service.database.transaction() as connection:
        # Older Phase 2 builds timestamped the normalized page-row write a few
        # moments after checked_at.  That is not a new source observation and
        # must not trigger thousands of duplicate requests during an upgrade.
        connection.execute(
            """
            UPDATE documents SET last_seen_at='9999-12-31T23:59:59.999999Z'
            WHERE bill_id=? AND source_entity_type='CommitteePublicTestimony'
            """,
            (int(bill["id"]),),
        )
    assert (
        service.historical._reusable_display_status(run_id, int(bill["id"]))
        == "checked_with_records"
    )

    with service.database.transaction() as connection:
        # A later OData upsert clears this marker until the refreshed page is
        # reconciled, so genuinely changed/new source rows are never reused.
        connection.execute(
            """
            UPDATE documents SET display_reconciled_at=NULL
            WHERE id=(
                SELECT id FROM documents
                WHERE bill_id=? AND source_entity_type='CommitteePublicTestimony'
                ORDER BY id LIMIT 1
            )
            """,
            (int(bill["id"]),),
        )
    assert service.historical._reusable_display_status(run_id, int(bill["id"])) is None


def test_authoritative_candidate_disappearance_resolves_stale_display_anomalies(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, olis, _downloader = _service(tmp_path, fixture_dir)
    first_run = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(first_run) == "completed"
    bill_id = int(service.storage.get_bill("2026R1", "SB1501")["id"])
    calls_after_first = list(olis.calls)

    for entity_set in (
        "CommitteeAgendaItems",
        "CommitteeMeetingDocuments",
        "CommitteePublicTestimonies",
    ):
        odata.rows[entity_set] = []
    second_run = service.create_inventory_backfill_run(
        ["2026R1"], force_full=True
    )
    assert service.execute_run(second_run) == "completed"

    assert olis.calls == calls_after_first
    with service.database.connection() as connection:
        unresolved = connection.execute(
            """
            SELECT COUNT(*) FROM source_anomalies
            WHERE bill_id=? AND resolved_at IS NULL
              AND anomaly_type IN (
                  'odata_olis_count_mismatch',
                  'odata_only_display_candidate',
                  'olis_page_only_record',
                  'olis_display_fetch_failed',
                  'olis_display_parser_anomaly',
                  'olis_parser_anomaly',
                  'testimony_candidate_rule_mismatch'
              )
            """,
            (bill_id,),
        ).fetchone()[0]
        statuses = {
            str(row["source_entity_type"]): str(row["status"])
            for row in connection.execute(
                """
                SELECT source_entity_type,status
                FROM olis_display_reconciliations WHERE bill_id=?
                """,
                (bill_id,),
            ).fetchall()
        }
    assert unresolved == 0
    assert statuses == {
        "CommitteePublicTestimony": "not_applicable",
        "CommitteeMeetingDocument": "not_applicable",
    }


def test_inventory_shutdown_race_does_not_record_false_session_failure(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    run_id = service.create_inventory_backfill_run(["2026R1"])

    def interrupt_before_stage(current_run_id: int, session_key: str, **_kwargs):
        service.storage.normalize_interrupted_work(interrupted_at=T2)
        service.runs.begin_stage(
            current_run_id,
            "sync_session",
            f"Synchronizing official inventory for {session_key}",
        )

    monkeypatch.setattr(
        service.historical, "_inventory_session", interrupt_before_stage
    )

    assert service.execute_run(run_id) == "interrupted"
    run = service.runs.get_run(run_id)
    assert run["status"] == "interrupted"
    assert run["error_count"] == 0
    assert all(
        item["status"] != "running" for item in service.runs.run_items(run_id)
    )


def test_inventory_persists_both_display_families_and_updates_each_family_flags(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, olis, _downloader = _service(tmp_path, fixture_dir)
    odata.rows["CommitteeMeetingDocuments"] = [
        {
            "CommitteeMeetingDocumentId": 32769,
            "SessionKey": "2026R1",
            "CommitteeCode": "SRULES",
            "MeetingDate": "2026-02-11T08:00:00",
            "ExhibitReference": "1",
            "ExhibitTitle": "Committee presentation",
            "Submitter": "Committee Staff",
            "DocumentType": "Presentation",
            "MeasurePrefix": "SB",
            "MeasureNumber": 1501,
            "DocumentUrl": (
                "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
                "CommitteeMeetingDocument/32769"
            ),
            "CreatedDate": "2026-02-01T10:00:00",
            "ModifiedDate": "2026-02-02T11:00:00",
        }
    ]
    olis.html = olis.html.replace(
        "</body>",
        """
        <h5>Presentations Displayed in Committee</h5>
        <table><thead><tr><th>Title</th><th>Submitter</th><th>Meeting</th><th>Committee</th></tr></thead>
        <tbody><tr>
          <td><a href="/liz/2026R1/Downloads/CommitteeMeetingDocument/32769">Committee presentation</a></td>
          <td>Committee Staff</td><td>2/11/2026</td><td>Senate Committee On Rules</td>
        </tr></tbody></table>
        </body>
        """,
    )

    run_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(run_id) == "completed"
    bill_id = int(service.storage.get_bill("2026R1", "SB1501")["id"])

    reconciliations = {
        str(row["source_entity_type"]): row
        for row in service.storage.list_olis_display_reconciliations(bill_id)
    }
    assert reconciliations["CommitteePublicTestimony"]["odata_record_count"] == 2
    assert reconciliations["CommitteePublicTestimony"]["displayed_record_count"] == 3
    assert reconciliations["CommitteeMeetingDocument"]["odata_record_count"] == 1
    assert reconciliations["CommitteeMeetingDocument"]["displayed_record_count"] == 1

    with service.database.connection() as connection:
        flags = {
            (str(row["source_entity_type"]), str(row["source_id"])): int(
                row["displayed_in_olis"]
            )
            for row in connection.execute(
                """
                SELECT source_entity_type,source_id,displayed_in_olis
                FROM documents
                WHERE bill_id=? AND displayed_in_olis IS NOT NULL
                """,
                (bill_id,),
            ).fetchall()
        }
    assert flags[("CommitteePublicTestimony", "244133")] == 1
    assert flags[("CommitteePublicTestimony", "255890")] == 0
    assert flags[("CommitteeMeetingDocument", "32769")] == 1


def test_metadata_retry_control_pauses_run_without_false_source_failure(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    run_id = service.create_inventory_backfill_run(["2026R1"])
    observed = {}

    def controlled_metadata(*, cancellation_requested=None):
        observed["callback"] = cancellation_requested
        assert cancellation_requested is not None
        assert not cancellation_requested()
        assert service.runs.pause(run_id, "operator pause during source backoff")
        assert cancellation_requested()
        raise TimeoutError("cooperative source interruption")

    odata.get_metadata_xml = controlled_metadata

    assert service.execute_run(run_id) == "paused"
    assert observed["callback"] is not None
    run = service.runs.get_run(run_id)
    assert run["status"] == "paused"
    assert run["error_count"] == 0
    with service.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM source_fetches WHERE run_id=? AND succeeded=0",
            (run_id,),
        ).fetchone()[0] == 0


def test_partial_page_is_persisted_but_failed_query_does_not_advance_watermark(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    initial_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(initial_id) == "completed"
    before = service.storage.get_source_sync_state("2026R1", "Measures")
    assert before["source_watermark"] == "2026-02-02T11:00:00"

    odata.rows["Measures"][0]["RelatingTo"] = "updated on a persisted first page"
    odata.rows["Measures"][0]["ModifiedDate"] = "2026-03-01T12:00:00"
    odata.fail_once_after_page["Measures"] = 1
    failed_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(failed_id) == "completed_with_errors"

    failed_state = service.storage.get_source_sync_state("2026R1", "Measures")
    assert failed_state["source_watermark"] == before["source_watermark"]
    assert failed_state["is_incomplete"] == 1
    assert (
        service.storage.get_bill("2026R1", "HB1001")["bill_title"]
        == "updated on a persisted first page"
    )
    assert service.storage.get_session_archive_state("2026R1")["inventory_status"] == (
        "inventory_incomplete"
    )

    retry_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(retry_id) == "completed"
    recovered = service.storage.get_source_sync_state("2026R1", "Measures")
    assert recovered["source_watermark"] == "2026-03-01T12:00:00"
    assert recovered["is_incomplete"] == 0
    assert service.storage.get_session_archive_state("2026R1")["inventory_status"] == (
        "inventory_complete"
    )


def test_interrupted_inventory_resolves_recovered_odata_error_on_same_run(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    baseline_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(baseline_id) == "completed"
    before = service.storage.get_source_sync_state("2026R1", "Measures")
    assert before["source_watermark"] == "2026-02-02T11:00:00"

    odata.rows["Measures"][0]["RelatingTo"] = "recovered same-run value"
    odata.rows["Measures"][0]["ModifiedDate"] = "2026-03-01T12:00:00"
    odata.fail_once_after_page["Measures"] = 1
    run_id = service.create_inventory_backfill_run(["2026R1"])
    original_inventory_session = service.historical._inventory_session
    interrupted = False

    def interrupt_after_failed_session(*args, **kwargs):
        nonlocal interrupted
        result = original_inventory_session(*args, **kwargs)
        if not interrupted:
            interrupted = True
            normalized = service.storage.normalize_interrupted_work(interrupted_at=T2)
            assert normalized["collection_runs"] == 1
        return result

    monkeypatch.setattr(
        service.historical, "_inventory_session", interrupt_after_failed_session
    )
    assert service.execute_run(run_id) == "interrupted"

    failed_state = service.storage.get_source_sync_state("2026R1", "Measures")
    assert failed_state["source_watermark"] == before["source_watermark"]
    assert failed_state["is_incomplete"] == 1
    errors = service.runs.unresolved_errors(run_id)
    assert len(errors) == 1
    assert errors[0]["stage"] == "sync_measures"
    assert errors[0]["source_entity_type"] == "Measures"

    monkeypatch.setattr(
        service.historical, "_inventory_session", original_inventory_session
    )
    assert service.runs.requeue(run_id)
    assert service.execute_run(run_id) == "completed"

    recovered = service.storage.get_source_sync_state("2026R1", "Measures")
    assert recovered["source_watermark"] == "2026-03-01T12:00:00"
    assert recovered["is_incomplete"] == 0
    run = service.runs.get_run(run_id)
    assert run["error_count"] == 0
    assert run["sessions_completed"] == 1
    assert run["sessions_incomplete"] == 0
    retained = service.runs.errors(run_id)
    assert len(retained) == 1
    assert retained[0]["resolved_at"] is not None


def test_retryable_olis_outage_pauses_after_three_pages_and_cleanly_resumes(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, odata, olis, _downloader = _service(tmp_path, fixture_dir)
    base_measure = dict(odata.rows["Measures"][0])
    base_testimony = dict(odata.rows["CommitteePublicTestimonies"][0])
    # HB1001 plus two new measures join the existing SB1501 candidate, leaving
    # at least one page that the outage breaker must not request.
    for number, source_id in ((1001, 300001), (1002, 300002), (1003, 300003)):
        if number != 1001:
            measure = dict(base_measure)
            measure["MeasureNumber"] = number
            odata.rows["Measures"].append(measure)
        testimony = dict(base_testimony)
        testimony["MeasurePrefix"] = "HB"
        testimony["MeasureNumber"] = number
        testimony["CommTestId"] = source_id
        odata.rows["CommitteePublicTestimonies"].append(testimony)

    failed_calls: list[tuple[str, str]] = []
    healthy_get = olis.get_testimony_page

    def source_outage(session_key: str, bill_id: str):
        failed_calls.append((session_key, bill_id))
        raise TimeoutError("temporary OLIS name-resolution failure")

    monkeypatch.setattr(olis, "get_testimony_page", source_outage)
    run_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(run_id) == "paused"
    assert len(failed_calls) == 3
    assert "3 consecutive retryable OLIS page failures" in str(
        service.runs.get_run(run_id)["current_activity"]
    )
    unresolved = service.runs.unresolved_errors(run_id)
    failed_bills = {bill for _session, bill in failed_calls}
    assert {str(row["bill_id_compact"]) for row in unresolved} == failed_bills
    assert len(unresolved) == 2 * len(failed_bills)

    olis.html = (
        "<html><body><h5>Submitted Written Public Testimony</h5>"
        "<p>No items to display.</p></body></html>"
    )
    monkeypatch.setattr(olis, "get_testimony_page", healthy_get)
    assert service.runs.requeue(run_id)
    assert service.execute_run(run_id) == "completed"
    run = service.runs.get_run(run_id)
    assert run["error_count"] == 0
    assert run["sessions_completed"] == 1
    assert run["sessions_incomplete"] == 0
    assert all(row["resolved_at"] is not None for row in service.runs.errors(run_id))


def test_olis_outage_streak_resets_on_parser_and_terminal_failures(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, odata, olis, _downloader = _service(tmp_path, fixture_dir)
    base_measure = dict(odata.rows["Measures"][0])
    base_testimony = dict(odata.rows["CommitteePublicTestimonies"][0])
    for number in range(1001, 1007):
        if number != 1001:
            measure = dict(base_measure)
            measure["MeasureNumber"] = number
            odata.rows["Measures"].append(measure)
        testimony = dict(base_testimony)
        testimony["MeasurePrefix"] = "HB"
        testimony["MeasureNumber"] = number
        testimony["CommTestId"] = 310000 + number
        odata.rows["CommitteePublicTestimonies"].append(testimony)

    calls: list[tuple[str, str]] = []

    def mixed_results(session_key: str, bill_id: str):
        calls.append((session_key, bill_id))
        call_number = len(calls)
        if call_number in {1, 2, 4, 6, 7}:
            raise TimeoutError(f"retryable outage {call_number}")
        if call_number == 5:
            raise ValueError("terminal page failure")
        return SimpleNamespace(
            text="<html><body><main>Unexpected redesigned OLIS page</main></body></html>",
            url=olis.testimony_url(session_key, bill_id),
            status_code=200,
        )

    monkeypatch.setattr(olis, "get_testimony_page", mixed_results)
    run_id = service.create_inventory_backfill_run(["2026R1"])

    assert service.execute_run(run_id) == "completed_with_errors"
    assert len(calls) == 7
    assert service.runs.get_run(run_id)["status"] != "paused"


def test_optional_probe_records_sizes_without_payload_download(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, downloader = _service(tmp_path, fixture_dir)
    probe = FakeProbe(content_length=321)
    service.historical.size_probe = probe

    run_id = service.create_inventory_backfill_run(
        ["2026R1"], probe_remote_sizes=True
    )
    assert service.execute_run(run_id) == "completed"
    assert downloader.calls == []
    assert len(probe.calls) == 5
    with service.database.connection() as connection:
        result = connection.execute(
            """
            SELECT COUNT(*) AS probe_count,COALESCE(SUM(content_length),0) AS total
            FROM document_remote_probes
            """
        ).fetchone()
    assert result["probe_count"] == 5
    assert result["total"] == 5 * 321


def _seed_download_scope(service: CollectionService) -> tuple[list[int], int, int]:
    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"},
        seen_at=T1,
    )
    _mark_session_inventoried(service)
    bill_id = service.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1,
            "bill_id_compact": "SB1",
            "bill_title": "Archive test",
        },
        seen_at=T1,
    )
    pending: list[int] = []
    for source_id in range(1, 5):
        pending.append(
            service.storage.upsert_document(
                {
                    "bill_id": bill_id,
                    "document_kind": "public_testimony",
                    "source_section": "odata_public_testimony",
                    "source_entity_type": "CommitteePublicTestimony",
                    "source_id": str(source_id),
                    "canonical_download_url": (
                        "https://olis.oregonlegislature.gov/liz/2026R1/"
                        f"Downloads/PublicTestimonyDocument/{source_id}"
                    ),
                },
                seen_at=T1,
            )
        )
    downloaded = service.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "10",
            "canonical_download_url": "https://olis.oregonlegislature.gov/document/10",
        },
        seen_at=T1,
    )
    downloaded_relative = (
        Path("2026R1")
        / "SB1"
        / "public_testimony"
        / "10"
        / "Existing.pdf"
    )
    downloaded_path = service.config.archive_root / downloaded_relative
    downloaded_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_path.write_bytes(PDF_BYTES)
    service.storage.complete_document_download(
        downloaded,
        sha256=sha256(PDF_BYTES).hexdigest(),
        local_relative_path=downloaded_relative.as_posix(),
        downloaded_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
        local_filename=downloaded_path.name,
        remote_filename=downloaded_path.name,
        source_url="https://olis.oregonlegislature.gov/document/10",
        validation_status="valid",
        http_status=200,
        downloaded_at=T1,
    )
    terminal = service.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "11",
            "canonical_download_url": "https://olis.oregonlegislature.gov/document/11",
            "download_status": "failed_terminal",
        },
        seen_at=T1,
    )
    service.storage.record_document_probe(
        pending[0], status="known", content_length=4096, probed_at=T2
    )
    return pending, downloaded, terminal


def test_download_scope_rejects_explicit_session_without_inventory_state(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"},
        seen_at=T1,
    )

    with pytest.raises(ValueError, match="has not been inventoried: 2026R1"):
        service.download_archive_preflight(["2026R1"])
    with pytest.raises(ValueError, match="has not been inventoried: 2026R1"):
        service.create_download_archive_run(["2026R1"])
    assert not any(
        row["run_type"] == "download_archive" for row in service.runs.list_runs()
    )

    _mark_session_inventoried(service)
    assert service.download_archive_preflight(["2026R1"]).session_keys == (
        "2026R1",
    )


def test_download_preflight_cutoff_and_database_claims_are_bounded(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=2)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    preflight = service.download_archive_preflight(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert preflight.documents_in_scope == 6
    assert preflight.already_downloaded == 1
    assert preflight.pending_or_missing == 4
    assert preflight.terminal_or_non_downloadable == 1
    assert preflight.known_pending_bytes == 4096
    assert preflight.unknown_size_pending == 3
    assert preflight.known_bytes_fit
    assert preflight.estimate_is_lower_bound

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    bill_id = service.storage.get_bill("2026R1", "SB1")["id"]
    late = service.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "99",
            "canonical_download_url": "https://olis.oregonlegislature.gov/document/99",
        },
        seen_at=FUTURE,
    )

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    claimed: list[int] = []

    def complete_claim(_run_id: int, document):
        nonlocal active, maximum_active
        document_id = int(document["id"])
        with lock:
            claimed.append(document_id)
            active += 1
            maximum_active = max(maximum_active, active)
            ordinal = len(claimed)
        try:
            if ordinal <= 2:
                barrier.wait(timeout=5)
            with service.database.transaction():
                service.storage.update_document_download_state(document_id, "downloaded")
                service.runs.mark_document_item(
                    _run_id, document_id, "completed", "fake transfer"
                )
                service.runs.add_downloaded_bytes(_run_id, 10)
            return document_id, "downloaded", 10
        finally:
            with lock:
                active -= 1

    service.historical.download_claimed = complete_claim
    assert service.execute_run(run_id) == "completed"
    assert sorted(claimed) == sorted(pending)
    assert len(claimed) == len(set(claimed))
    assert maximum_active == 2
    assert service.storage.get_document(late)["download_status"] == "discovered"
    run_items = service.runs.run_items(run_id)
    document_items = [
        item
        for item in run_items
        if item["item_type"] == "document"
    ]
    assert len(document_items) == 5
    assert [item["status"] for item in document_items].count("completed") == 4
    assert [item["status"] for item in document_items].count("skipped") == 1
    assert {item["session_key"] for item in document_items} == {"2026R1"}
    session_item = next(item for item in run_items if item["item_type"] == "session")
    assert session_item["session_key"] == "2026R1"
    audit_stage = next(
        item
        for item in run_items
        if item["item_type"] == "stage" and item["item_key"] == "download_archive"
    )
    final_stage = next(
        item
        for item in run_items
        if item["item_type"] == "stage" and item["item_key"] == "finalize_run"
    )
    assert audit_stage["session_key"] is None
    assert final_stage["session_key"] is None
    assert audit_stage["progress_current"] == 1
    assert audit_stage["progress_total"] == 1


def test_archive_transfer_workers_continue_while_results_are_finalized(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=2)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    first_pair = threading.Barrier(2)
    finalizer_started = threading.Event()
    later_transfer_started = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    preparation_count = 0

    def prepare(_run_id: int, document):
        nonlocal active, maximum_active, preparation_count
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            preparation_count += 1
            ordinal = preparation_count
        try:
            if ordinal <= 2:
                first_pair.wait(timeout=5)
            else:
                assert finalizer_started.wait(timeout=5)
                later_transfer_started.set()
            return ClaimedPreparation(int(document["id"]), payload=dict(document))
        finally:
            with lock:
                active -= 1

    def finalize_batch(finalized_run_id: int, documents):
        finalizer_started.set()
        assert later_transfer_started.wait(timeout=5)
        outcomes = []
        with service.database.transaction():
            for document in documents:
                document_id = int(document["id"])
                service.storage.update_document_download_state(document_id, "downloaded")
                assert service.runs.finalize_claimed_document_item(
                    finalized_run_id,
                    document_id,
                    "completed",
                    "fake pipelined transfer",
                )
                service.runs.add_downloaded_bytes(finalized_run_id, 10)
                outcomes.append((document_id, "downloaded", 10))
        return outcomes

    service.historical.prepare_claimed = prepare
    service.historical.finalize_prepared_batch = finalize_batch
    assert service.execute_run(run_id) == "completed"

    assert preparation_count == len(pending)
    assert maximum_active == 2
    assert finalizer_started.is_set()
    assert later_transfer_started.is_set()


def test_archive_keyset_cursor_claims_many_failures_once_and_advances_linearly(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"},
        seen_at=T1,
    )
    _mark_session_inventoried(service)
    bill_id = service.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_prefix": "SB",
            "measure_number": 1,
            "bill_id_compact": "SB1",
            "bill_title": "Failure-heavy archive test",
        },
        seen_at=T1,
    )
    document_ids = [
        service.storage.upsert_document(
            {
                "bill_id": bill_id,
                "document_kind": "public_testimony",
                "source_section": "odata_public_testimony",
                "source_entity_type": "CommitteePublicTestimony",
                "source_id": str(source_id),
                "canonical_download_url": (
                    "https://olis.oregonlegislature.gov/document/"
                    f"failure-{source_id}"
                ),
                "download_status": "failed_retryable",
            },
            seen_at=T1,
        )
        for source_id in range(1, 41)
    ]
    run_id = service.create_download_archive_run(
        ["2026R1"],
        document_kinds=["public_testimony"],
        retryable_failures_only=True,
    )
    assert service.runs.claim_run(run_id)

    traced_statements: list[str] = []
    original_connect = service.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(traced_statements.append)
        return connection

    monkeypatch.setattr(service.database, "connect", traced_connect)
    claimed: list[int] = []
    for expected_id in document_ids:
        document = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
        assert document is not None
        claimed.append(int(document["id"]))
        with service.database.connection() as connection:
            cursor = connection.execute(
                """
                SELECT after_document_id,exhausted FROM archive_claim_cursors
                WHERE run_id=? AND session_ordinal=0
                """,
                (run_id,),
            ).fetchone()
        assert cursor["after_document_id"] == expected_id
        assert cursor["exhausted"] == 0
        assert service.storage.release_archive_document_claim(
            run_id,
            expected_id,
            document_status="failed_retryable",
            item_status="failed_retryable",
            message="synthetic retryable failure",
            changed_at=T2,
        )

    assert claimed == document_ids
    assert service.storage.claim_next_archive_document(run_id, attempted_at=T2) is None
    with service.database.connection() as connection:
        cursor = connection.execute(
            """
            SELECT after_document_id,exhausted FROM archive_claim_cursors
            WHERE run_id=? AND session_ordinal=0
            """,
            (run_id,),
        ).fetchone()
    assert cursor["after_document_id"] == document_ids[-1]
    assert cursor["exhausted"] == 1
    fresh_selects = [
        statement
        for statement in traced_statements
        if "SELECT d.*,NULL AS existing_run_item_id" in statement
    ]
    assert len(fresh_selects) == len(document_ids) + 1
    cursor_inserts = [
        statement
        for statement in traced_statements
        if "INSERT INTO archive_claim_cursors" in statement
    ]
    assert len(cursor_inserts) == 1
    # Once the session cursor is exhausted, later workers/retries do not issue
    # another document walk or repeat cursor initialization for that session.
    assert service.storage.claim_next_archive_document(run_id, attempted_at=T2) is None
    assert len(
        [
            statement
            for statement in traced_statements
            if "SELECT d.*,NULL AS existing_run_item_id" in statement
        ]
    ) == len(fresh_selects)
    assert len(
        [
            statement
            for statement in traced_statements
            if "INSERT INTO archive_claim_cursors" in statement
        ]
    ) == len(cursor_inserts)
    document_items = [
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document"
    ]
    assert len(document_items) == len(document_ids)
    assert {row["status"] for row in document_items} == {"failed_retryable"}


def test_archive_cursor_starts_at_zero_despite_audit_created_high_id_item(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    service.runs.record_archive_document_skip(
        run_id,
        document_id=downloaded,
        bill_id=int(service.storage.get_document(downloaded)["bill_id"]),
        session_key="2026R1",
    )

    document = service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert document is not None
    assert document["id"] == pending[0]
    with service.database.connection() as connection:
        cursor = connection.execute(
            """
            SELECT after_document_id FROM archive_claim_cursors
            WHERE run_id=? AND session_ordinal=0
            """,
            (run_id,),
        ).fetchone()
    assert cursor["after_document_id"] == pending[0]


def test_archive_cursor_initialization_and_claim_are_rolled_back_together(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)

    with service.database.transaction() as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_archive_item_insert
            BEFORE INSERT ON collection_run_items
            WHEN NEW.run_id={int(run_id)} AND NEW.item_type='document'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic archive item failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic archive item failure"):
        service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert service.storage.get_document(pending[0])["download_status"] == "discovered"
    with service.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM archive_claim_cursors WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT COUNT(*) FROM collection_run_items
            WHERE run_id=? AND item_type='document'
            """,
            (run_id,),
        ).fetchone()[0] == 0
    with service.database.transaction() as connection:
        connection.execute("DROP TRIGGER fail_archive_item_insert")

    reclaimed = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert reclaimed["id"] == pending[0]


def test_archive_cursor_exhausts_each_frozen_session_in_ordinal_order(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    expected_by_session: dict[str, int] = {}
    for session_key, year in (("2014R1", 2014), ("2026R1", 2026)):
        service.storage.upsert_session(
            {"session_key": session_key, "session_name": f"Session {session_key}"},
            seen_at=T1,
        )
        _mark_session_inventoried(service, session_key)
        bill_id = service.storage.upsert_bill(
            {
                "session_key": session_key,
                "measure_prefix": "HB",
                "measure_number": year,
                "bill_id_compact": f"HB{year}",
                "bill_title": "Cursor order test",
            },
            seen_at=T1,
        )
        expected_by_session[session_key] = service.storage.upsert_document(
            {
                "bill_id": bill_id,
                "document_kind": "public_testimony",
                "source_section": "odata_public_testimony",
                "source_entity_type": "CommitteePublicTestimony",
                "source_id": str(year),
                "canonical_download_url": (
                    "https://olis.oregonlegislature.gov/document/"
                    f"cursor-{year}"
                ),
            },
            seen_at=T1,
        )
    frozen_sessions = ["2026R1", "2014R1"]
    run_id = service.create_download_archive_run(
        frozen_sessions, document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)

    first = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    first_id = expected_by_session["2026R1"]
    second_id = expected_by_session["2014R1"]
    assert first["id"] == first_id
    service.storage.update_document_download_state(first_id, "downloaded")
    service.runs.mark_document_item(run_id, first_id, "completed", "first session done")

    second = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert second["id"] == second_id
    with service.database.connection() as connection:
        cursors = connection.execute(
            """
            SELECT session_ordinal,session_key,after_document_id,exhausted
            FROM archive_claim_cursors WHERE run_id=? ORDER BY session_ordinal
            """,
            (run_id,),
        ).fetchall()
    assert [dict(row) for row in cursors] == [
        {
            "session_ordinal": 0,
            "session_key": "2026R1",
            "after_document_id": first_id,
            "exhausted": 1,
        },
        {
            "session_ordinal": 1,
            "session_key": "2014R1",
            "after_document_id": second_id,
            "exhausted": 0,
        },
    ]
    service.storage.update_document_download_state(second_id, "downloaded")
    service.runs.mark_document_item(run_id, second_id, "completed", "second session done")
    assert service.storage.claim_next_archive_document(run_id, attempted_at=T2) is None
    with service.database.connection() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM archive_claim_cursors
            WHERE run_id=? AND exhausted=1
            """,
            (run_id,),
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("mutation", "reason_fragment"),
    [
        ("downloaded", "document status"),
        ("source_missing", "presence scope"),
        ("url_missing", "download URL"),
        ("wrong_kind", "document kind"),
        ("terminal_status", "document status"),
        ("after_cutoff", "scope cutoff"),
    ],
)
def test_queued_archive_item_that_left_scope_is_skipped_and_does_not_block_work(
    mutation: str,
    reason_fragment: str,
    tmp_path: Path,
    fixture_dir: Path,
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    first = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert first["id"] == pending[0]
    assert service.runs.pause(run_id, "prepare queued scope change")
    assert service.storage.release_archive_document_claim(
        run_id,
        pending[0],
        document_status="interrupted",
        item_status="paused",
        changed_at=T2,
    )
    assert service.runs.requeue(run_id)
    assert service.runs.claim_run(run_id)

    assignments = {
        "downloaded": ("download_status='downloaded'", ()),
        "source_missing": ("source_presence='missing'", ()),
        "url_missing": ("canonical_download_url=NULL", ()),
        "wrong_kind": ("document_kind='unknown'", ()),
        "terminal_status": ("download_status='failed_terminal'", ()),
        "after_cutoff": ("first_seen_at=?", (FUTURE,)),
    }
    assignment, values = assignments[mutation]
    with service.database.transaction() as connection:
        connection.execute(
            f"UPDATE documents SET {assignment} WHERE id=?",
            (*values, pending[0]),
        )

    next_document = service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert next_document is not None
    assert next_document["id"] == pending[1]
    skipped = next(
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document" and row["document_id"] == pending[0]
    )
    assert skipped["status"] == "skipped"
    assert reason_fragment in skipped["current_activity"]
    assert reason_fragment in json.loads(skipped["details_json"])[
        "archive_resume_skip"
    ]


def test_queued_archive_item_without_document_is_audited_and_skipped(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    with service.database.transaction() as connection:
        malformed_item_id = int(
            connection.execute(
                """
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,stage,status,
                    current_activity,queued_at,updated_at,details_json
                ) VALUES (?, 'document', 'document:missing', '2026R1',
                          'download_archive', 'queued', 'Legacy malformed item',
                          ?, ?, '{}')
                """,
                (run_id, T1, T1),
            ).lastrowid
        )

    document = service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert document is not None
    assert document["id"] == pending[0]
    malformed = next(
        row for row in service.runs.run_items(run_id) if row["id"] == malformed_item_id
    )
    assert malformed["status"] == "skipped"
    assert "no stored document" in malformed["current_activity"]
    assert "no stored document" in json.loads(malformed["details_json"])[
        "archive_resume_skip"
    ]


def test_ineligible_queued_archive_items_commit_between_skip_transitions(
    tmp_path: Path, fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    first = service.storage.get_document(pending[0])
    second = service.storage.get_document(pending[1])
    with service.database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO collection_run_items(
                run_id,item_type,item_key,session_key,bill_id,document_id,stage,
                status,current_activity,queued_at,updated_at,details_json
            ) VALUES (?, 'document', ?, ?, ?, ?, 'download_archive', 'queued',
                      'Awaiting resumed archive claim', ?, ?, '{}')
            """,
            (
                (
                    run_id,
                    f"document:{document['id']}",
                    document["session_key"],
                    document["bill_id"],
                    document["id"],
                    T1,
                    T1,
                )
                for document in (first, second)
            ),
        )
        connection.execute(
            "UPDATE documents SET source_presence='missing' WHERE id=?",
            (pending[0],),
        )
        connection.execute(
            "UPDATE documents SET document_kind='unknown' WHERE id=?",
            (pending[1],),
        )

    traced_statements: list[str] = []
    original_connect = service.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(traced_statements.append)
        return connection

    monkeypatch.setattr(service.database, "connect", traced_connect)
    claimed = service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert claimed is not None
    assert claimed["id"] == pending[2]
    document_items = {
        row["document_id"]: row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document"
    }
    assert document_items[pending[0]]["status"] == "skipped"
    assert document_items[pending[1]]["status"] == "skipped"
    assert document_items[pending[2]]["status"] == "running"
    # Each invalid queued transition commits before the wrapper begins the next
    # claim attempt, preventing an unbounded writer lock during recovery.
    assert sum(statement == "BEGIN IMMEDIATE" for statement in traced_statements) == 3
    assert sum(statement == "COMMIT" for statement in traced_statements) == 3


@pytest.mark.parametrize("empty_field", ["eligible_statuses", "document_kinds"])
def test_empty_frozen_archive_selection_is_rejected_at_creation(
    empty_field: str, tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    service.storage.upsert_session(
        {"session_key": "2026R1", "session_name": "2026 Regular Session"},
        seen_at=T1,
    )
    scope = {
        "session_keys": ["2026R1"],
        "scope_cutoff_at": T2,
        "eligible_statuses": ["discovered"],
        "document_kinds": ["public_testimony"],
    }
    scope[empty_field] = []

    with pytest.raises(ValueError, match="at least one frozen"):
        service.runs.create_run(
            "download_archive",
            session_keys=["2026R1"],
            scope=scope,
            scope_cutoff_at=T2,
        )


@pytest.mark.parametrize("empty_field", ["eligible_statuses", "document_kinds"])
def test_legacy_empty_frozen_archive_selection_errors_instead_of_completing(
    empty_field: str, tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    _seed_download_scope(service)
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    with service.database.transaction() as connection:
        row = connection.execute(
            "SELECT requested_scope_json FROM collection_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        scope = json.loads(row["requested_scope_json"])
        scope[empty_field] = []
        connection.execute(
            "UPDATE collection_runs SET requested_scope_json=? WHERE id=?",
            (json.dumps(scope, sort_keys=True), run_id),
        )
    assert service.runs.claim_run(run_id)

    with pytest.raises(ValueError, match="no frozen"):
        service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    with service.database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM archive_claim_cursors WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0


def test_database_run_claim_prevents_overlapping_archive_walks(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    run_ids = [
        service.create_download_archive_run(
            ["2026R1"], document_kinds=["public_testimony"]
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(service.runs.claim_run, run_ids))

    assert sorted(claims) == [False, True]
    owner_id = run_ids[claims.index(True)]
    waiting_id = run_ids[claims.index(False)]
    assert service.runs.get_run(owner_id)["status"] == "running"
    assert service.runs.get_run(waiting_id)["status"] == "queued"

    first = service.storage.claim_next_archive_document(owner_id, attempted_at=T2)
    assert first["id"] == pending[0]
    assert service.storage.release_archive_document_claim(
        owner_id,
        pending[0],
        document_status="failed_retryable",
        item_status="failed_retryable",
        message="released before the next durable run",
        changed_at=T2,
    )
    service.storage.update_collection_run(owner_id, status="completed")

    assert service.runs.claim_run(waiting_id)
    retried = service.storage.claim_next_archive_document(waiting_id, attempted_at=T2)
    assert retried["id"] == pending[0]


def test_archive_cursor_survives_restart_and_resumes_owned_item_before_keyset_work(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    for document_id in pending[2:]:
        service.storage.update_document_download_state(document_id, "failed_terminal")
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    first = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert first["id"] == pending[0]
    first_item_id = int(first["run_item_id"])
    with service.database.connection() as connection:
        assert connection.execute(
            """
            SELECT after_document_id FROM archive_claim_cursors
            WHERE run_id=? AND session_ordinal=0
            """,
            (run_id,),
        ).fetchone()["after_document_id"] == pending[0]

    restarted_runtime = build_runtime(service.config, clean_parts=False)
    assert restarted_runtime.collection.runs.get_run(run_id)["status"] == "interrupted"
    with restarted_runtime.database.connection() as connection:
        assert connection.execute(
            """
            SELECT after_document_id FROM archive_claim_cursors
            WHERE run_id=? AND session_ordinal=0
            """,
            (run_id,),
        ).fetchone()["after_document_id"] == pending[0]

    resumed_service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    assert resumed_service.runs.requeue(run_id)
    assert resumed_service.runs.claim_run(run_id)
    resumed = resumed_service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert resumed["id"] == pending[0]
    assert resumed["run_item_id"] == first_item_id
    resumed_service.storage.update_document_download_state(pending[0], "downloaded")
    resumed_service.runs.mark_document_item(
        run_id, pending[0], "completed", "completed after restart"
    )

    second = resumed_service.storage.claim_next_archive_document(run_id, attempted_at=T2)

    assert second["id"] == pending[1]
    with resumed_service.database.connection() as connection:
        cursor = connection.execute(
            """
            SELECT after_document_id,exhausted FROM archive_claim_cursors
            WHERE run_id=? AND session_ordinal=0
            """,
            (run_id,),
        ).fetchone()
    assert cursor["after_document_id"] == pending[1]
    assert cursor["exhausted"] == 0


def test_paused_claim_and_restart_interruption_resume_same_durable_item(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=1)
    pending, _downloaded, _terminal = _seed_download_scope(service)
    # Narrow this recovery exercise to one document.
    for document_id in pending[1:]:
        service.storage.update_document_download_state(document_id, "failed_terminal")
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    first_claim = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert first_claim["id"] == pending[0]
    first_item_id = first_claim["run_item_id"]

    assert service.runs.pause(run_id, "operator pause")
    assert service.storage.release_archive_document_claim(
        run_id,
        pending[0],
        document_status="interrupted",
        item_status="paused",
        message="active stream stopped at a chunk boundary",
        changed_at=T2,
    )
    assert service.runs.requeue(run_id)
    assert service.runs.claim_run(run_id)
    resumed_claim = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert resumed_claim["id"] == pending[0]
    assert resumed_claim["run_item_id"] == first_item_id

    # Simulate the process dying after the resumable claim, then normalize on boot.
    recovered = service.storage.normalize_interrupted_work(interrupted_at=T2)
    assert recovered["collection_runs"] == 1
    assert recovered["documents"] == 1
    assert service.runs.status(run_id) == "interrupted"
    assert service.storage.get_document(pending[0])["download_status"] == "interrupted"
    assert service.runs.requeue(run_id)

    def complete_claim(resumed_run_id: int, document):
        document_id = int(document["id"])
        service.storage.update_document_download_state(document_id, "downloaded")
        service.runs.mark_document_item(
            resumed_run_id, document_id, "completed", "recovered transfer"
        )
        return document_id, "downloaded", 12

    service.historical.download_claimed = complete_claim
    assert service.execute_run(run_id) == "completed"
    document_items = [
        item
        for item in service.runs.run_items(run_id)
        if item["item_type"] == "document"
    ]
    assert len(document_items) == 2
    resumed_item = next(
        item for item in document_items if item["document_id"] == pending[0]
    )
    assert resumed_item["id"] == first_item_id
    assert resumed_item["status"] == "completed"
    assert resumed_item["attempt_count"] == 3
    assert next(
        item for item in document_items if item["document_id"] == _downloaded
    )["status"] == "skipped"
    assert service.storage.get_document(pending[0])["download_status"] == "downloaded"


def test_download_archive_invalid_current_payload_without_url_is_terminal_and_incomplete(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, downloader = _service(tmp_path, fixture_dir, workers=1)
    document_id, path, _version_id = _seed_archive_document(
        service,
        "",
        source_id="19",
        existing_payload=PDF_BYTES,
    )
    assert path is not None
    path.unlink()

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(run_id) == "completed_with_errors"
    assert downloader.calls == []
    document = service.storage.get_document(document_id)
    assert document["download_status"] == "failed_terminal"
    item = next(
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document" and row["document_id"] == document_id
    )
    assert item["status"] == "failed_terminal"
    errors = service.runs.unresolved_errors(run_id)
    assert len(errors) == 1
    assert errors[0]["document_id"] == document_id
    run = service.runs.get_run(run_id)
    assert run["sessions_completed"] == 0
    assert run["sessions_incomplete"] == 1


def test_downloaded_payload_audit_excludes_documents_after_frozen_cutoff(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, downloader = _service(tmp_path, fixture_dir, workers=1)
    document_id, path, _version_id = _seed_archive_document(
        service,
        "https://olis.oregonlegislature.gov/document/late",
        source_id="98",
        existing_payload=PDF_BYTES,
        seen_at=FUTURE,
    )
    assert path is not None
    path.unlink()

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(run_id) == "completed"
    assert downloader.calls == []
    # The file is deliberately missing, but it entered inventory after the
    # run's cutoff and therefore must remain untouched by both audit and claim.
    assert service.storage.get_document(document_id)["download_status"] == "downloaded"
    assert not [
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document"
    ]
    audit_stage = next(
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "stage" and row["item_key"] == "download_archive"
    )
    assert audit_stage["progress_current"] == 0
    assert audit_stage["progress_total"] == 0


def test_downloaded_payload_audit_uses_registered_path_filename_and_size(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, _olis, _downloader = _service(tmp_path, fixture_dir, workers=2)
    document_id, path, _version_id = _seed_archive_document(
        service,
        "https://olis.oregonlegislature.gov/document/size-only",
        source_id="size-only",
        existing_payload=PDF_BYTES,
    )
    assert path is not None
    service.storage.update_document_download_state(
        document_id,
        "downloaded",
        sha256="0" * 64,
    )

    document = service.storage.get_document(document_id)
    assert service.historical._valid_current_payload(document)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_download_archive_recovers_missing_or_corrupt_current_payload(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
    damage: str,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    document_id, original_path, original_version_id = _seed_archive_document(
        service,
        f"{base_url}/recover",
        source_id="20",
        existing_payload=PDF_BYTES,
    )
    assert original_path is not None and original_version_id is not None
    if damage == "missing":
        original_path.unlink()
    else:
        original_path.write_bytes(b"not a PDF")

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(run_id) == "completed"

    recovered = service.storage.get_document(document_id)
    versions = service.storage.list_document_versions(document_id)
    assert recovered["download_status"] == "downloaded"
    assert recovered["current_version_id"] != original_version_id
    assert len(versions) == 2
    current_version = next(
        version for version in versions if version["id"] == recovered["current_version_id"]
    )
    assert current_version["sha256"] is None
    assert current_version["local_relative_path"].endswith("Evidence__v0002.pdf")
    assert (service.config.archive_root / current_version["local_relative_path"]).read_bytes() == PDF_BYTES
    assert counters["/recover"] == 1
    assert not list(service.config.archive_root.rglob("*.part"))


def test_download_archive_source_change_creates_hashless_immutable_version(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    source_url = f"{base_url}/same-hash"
    document_id, original_path, original_version_id = _seed_archive_document(
        service,
        source_url,
        source_id="21",
        existing_payload=PDF_BYTES,
    )
    assert original_path is not None and original_version_id is not None

    service.storage.upsert_document(
        {
            "bill_id": service.storage.get_bill("2026R1", "SB1")["id"],
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "21",
            "title": "Evidence.pdf",
            "canonical_download_url": source_url,
            "source_modified_at": T2,
        },
        seen_at=T2,
    )
    assert service.storage.get_document(document_id)["download_status"] == "changed_remote"

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(run_id) == "completed"

    current = service.storage.get_document(document_id)
    versions = service.storage.list_document_versions(document_id)
    assert current["current_version_id"] != original_version_id
    assert current["local_relative_path"].endswith("Evidence__v0002.pdf")
    assert current["sha256"] is None
    assert original_path.read_bytes() == PDF_BYTES
    assert len(versions) == 2
    current_path = service.config.archive_root / current["local_relative_path"]
    assert current_path.read_bytes() == PDF_BYTES
    assert next(
        version for version in versions if version["id"] == current["current_version_id"]
    )["source_modified_at"] == T2
    assert counters["/same-hash"] == 1


def test_download_archive_changed_hash_promotes_v2_only_after_valid_completion(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
):
    base_url, state, counters = archive_server
    failures = state["failures_remaining"]
    bodies = state["bodies"]
    assert isinstance(failures, defaultdict)
    assert isinstance(bodies, defaultdict)
    source_url = f"{base_url}/changed"
    bodies["/changed"] = PDF_BYTES_V2
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    document_id, original_path, original_version_id = _seed_archive_document(
        service,
        source_url,
        source_id="22",
        existing_payload=PDF_BYTES,
    )
    assert original_path is not None and original_version_id is not None
    service.storage.upsert_document(
        {
            "bill_id": service.storage.get_bill("2026R1", "SB1")["id"],
            "document_kind": "public_testimony",
            "source_section": "odata_public_testimony",
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": "22",
            "title": "Evidence.pdf",
            "canonical_download_url": source_url,
            "source_modified_at": T2,
        },
        seen_at=T2,
    )
    failures["/changed"] = 3

    failed_run = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(failed_run) == "completed_with_errors"
    after_failure = service.storage.get_document(document_id)
    assert after_failure["download_status"] == "failed_retryable"
    assert after_failure["current_version_id"] == original_version_id
    assert len(service.storage.list_document_versions(document_id)) == 1
    assert original_path.read_bytes() == PDF_BYTES
    assert not (original_path.parent / "Evidence__v0002.pdf").exists()

    retry_run = service.create_download_archive_run(
        ["2026R1"],
        document_kinds=["public_testimony"],
        retryable_failures_only=True,
    )
    assert service.execute_run(retry_run) == "completed"
    current = service.storage.get_document(document_id)
    versions = service.storage.list_document_versions(document_id)
    assert len(versions) == 2
    assert {row["version_number"] for row in versions} == {1, 2}
    assert current["current_version_id"] != original_version_id
    current_version = next(row for row in versions if row["id"] == current["current_version_id"])
    assert current_version["version_number"] == 2
    assert current_version["local_relative_path"].endswith("Evidence__v0002.pdf")
    assert (service.config.archive_root / current_version["local_relative_path"]).read_bytes() == PDF_BYTES_V2
    assert original_path.read_bytes() == PDF_BYTES
    assert counters["/changed"] == 4


def test_download_archive_retry_isolates_failed_document(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
):
    base_url, state, counters = archive_server
    failures = state["failures_remaining"]
    assert isinstance(failures, defaultdict)
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=2)
    _use_loopback_downloader(service)
    failed_id, _path, _version = _seed_archive_document(
        service, f"{base_url}/retry-me", source_id="30"
    )
    successful_id, _path, _version = _seed_archive_document(
        service, f"{base_url}/leave-alone", source_id="31"
    )
    failures["/retry-me"] = 3

    first_run = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(first_run) == "completed_with_errors"
    assert service.storage.get_document(failed_id)["download_status"] == "failed_retryable"
    assert service.storage.get_document(successful_id)["download_status"] == "downloaded"
    assert counters["/retry-me"] == 3
    assert counters["/leave-alone"] == 1

    retry_run = service.create_download_archive_run(
        ["2026R1"],
        document_kinds=["public_testimony"],
        retryable_failures_only=True,
    )
    assert service.execute_run(retry_run) == "completed"
    assert counters["/retry-me"] == 4
    assert counters["/leave-alone"] == 1
    retry_document_items = [
        item
        for item in service.runs.run_items(retry_run)
        if item["item_type"] == "document"
    ]
    assert [item["document_id"] for item in retry_document_items] == [failed_id]


def test_download_archive_low_space_pause_and_duplicate_resume_are_idempotent(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
    monkeypatch,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service, minimum_free_space_bytes=1)
    document_id, _path, _version = _seed_archive_document(
        service, f"{base_url}/low-space", source_id="40"
    )
    monkeypatch.setattr("olis_archive.services.downloads._free_bytes", lambda _path: 0)

    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.execute_run(run_id) == "paused"
    assert service.storage.get_document(document_id)["download_status"] == "paused_low_space"
    assert service.runs.status(run_id) == "paused"
    assert not list(service.config.archive_root.rglob("*.part"))

    assert service.runs.requeue(run_id)
    assert not service.runs.requeue(run_id)
    monkeypatch.setattr(
        "olis_archive.services.downloads._free_bytes", lambda _path: 10**12
    )
    assert service.execute_run(run_id) == "completed"
    calls_after_completion = counters["/low-space"]
    assert service.execute_run(run_id) == "completed"
    assert counters["/low-space"] == calls_after_completion
    items = [
        item
        for item in service.runs.run_items(run_id)
        if item["item_type"] == "document"
    ]
    assert len(items) == 1
    assert items[0]["status"] == "completed"
    assert items[0]["attempt_count"] == 2


def test_promoted_orphan_is_recovered_after_atomic_database_outcome_rolls_back(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
    monkeypatch,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    document_id, _path, _version = _seed_archive_document(
        service, f"{base_url}/crash-window", source_id="41"
    )
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    claimed = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert claimed and int(claimed["id"]) == document_id

    class SimulatedProcessStop(BaseException):
        pass

    original_finalize = service.runs.finalize_claimed_document_item

    def stop_after_item_write(*args, **kwargs):  # noqa: ANN002, ANN003
        original_finalize(*args, **kwargs)
        raise SimulatedProcessStop()

    with monkeypatch.context() as patcher:
        patcher.setattr(
            service.runs,
            "finalize_claimed_document_item",
            stop_after_item_write,
        )
        with pytest.raises(SimulatedProcessStop):
            service._download_claimed_document(run_id, claimed)

    # Promotion happened, but every database outcome write rolled back together.
    assert counters["/crash-window"] == 1
    assert len(list(service.config.archive_root.rglob("*.pdf"))) == 1
    assert not list(service.config.archive_root.rglob("*.part"))
    assert service.storage.get_document(document_id)["download_status"] == "downloading"
    assert service.storage.list_document_versions(document_id) == []
    assert service.runs.get_run(run_id)["bytes_downloaded"] == 0
    document_item = next(
        item
        for item in service.runs.run_items(run_id)
        if item["item_type"] == "document"
    )
    assert document_item["status"] == "running"

    service.storage.normalize_interrupted_work()
    assert service.runs.status(run_id) == "interrupted"
    assert service.runs.requeue(run_id)
    assert service.execute_run(run_id) == "completed"

    # Resume redownloads into `.part`, matches the orphan by path and byte count,
    # and adopts it without replacing it or producing a duplicate version.
    assert counters["/crash-window"] == 2
    assert service.storage.get_document(document_id)["download_status"] == "downloaded"
    assert len(service.storage.list_document_versions(document_id)) == 1
    assert not list(service.config.archive_root.rglob("*.part"))
    document_item = next(
        item
        for item in service.runs.run_items(run_id)
        if item["item_type"] == "document"
    )
    assert document_item["status"] == "skipped"


def test_claimed_download_does_not_finalize_after_concurrent_cancel(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
    monkeypatch,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    document_id, _path, _version_id = _seed_archive_document(
        service, f"{base_url}/cancel-after-promotion", source_id="42"
    )
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    claimed = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert claimed and int(claimed["id"]) == document_id

    original_download = service.downloader.download_to_path

    def download_then_cancel(*args, **kwargs):  # noqa: ANN002, ANN003
        result = original_download(*args, **kwargs)
        assert service.runs.cancel(run_id)
        return result

    monkeypatch.setattr(service.downloader, "download_to_path", download_then_cancel)
    assert service._download_claimed_document(run_id, claimed) == (
        document_id,
        "canceled",
        0,
    )

    assert counters["/cancel-after-promotion"] == 1
    assert len(list(service.config.archive_root.rglob("*.pdf"))) == 1
    assert service.storage.list_document_versions(document_id) == []
    assert service.storage.get_document(document_id)["download_status"] == "interrupted"
    assert service.runs.get_run(run_id)["bytes_downloaded"] == 0
    item = next(
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document" and row["document_id"] == document_id
    )
    assert item["status"] == "canceled"
    assert service.runs.unresolved_errors(run_id) == []


def test_claimed_download_database_finalization_failure_stays_retryable(
    tmp_path: Path,
    fixture_dir: Path,
    archive_server,
    monkeypatch,
):
    base_url, _state, counters = archive_server
    service, _odata, _olis, _unused = _service(tmp_path, fixture_dir, workers=1)
    _use_loopback_downloader(service)
    document_id, _path, _version_id = _seed_archive_document(
        service, f"{base_url}/finalization-failure", source_id="43"
    )
    run_id = service.create_download_archive_run(
        ["2026R1"], document_kinds=["public_testimony"]
    )
    assert service.runs.claim_run(run_id)
    claimed = service.storage.claim_next_archive_document(run_id, attempted_at=T2)
    assert claimed and int(claimed["id"]) == document_id

    def fail_finalization(*_args, **_kwargs):
        raise RuntimeError("injected finalization failure")

    monkeypatch.setattr(
        service.runs,
        "finalize_claimed_document_item",
        fail_finalization,
    )
    assert service._download_claimed_document(run_id, claimed) == (
        document_id,
        "failed",
        0,
    )

    assert counters["/finalization-failure"] == 1
    assert len(list(service.config.archive_root.rglob("*.pdf"))) == 1
    assert service.storage.list_document_versions(document_id) == []
    assert service.storage.get_document(document_id)["download_status"] == "failed_retryable"
    assert service.runs.get_run(run_id)["bytes_downloaded"] == 0
    item = next(
        row
        for row in service.runs.run_items(run_id)
        if row["item_type"] == "document" and row["document_id"] == document_id
    )
    assert item["status"] == "failed_retryable"
    errors = service.runs.unresolved_errors(run_id)
    assert len(errors) == 1
    assert errors[0]["retryable"] == 1
    assert json.loads(errors[0]["details_json"])["claim_finalization_failure"] is True


def test_inventory_cancel_clears_inventory_running_session_state(
    tmp_path: Path, fixture_dir: Path
):
    service, odata, _olis, _downloader = _service(tmp_path, fixture_dir)
    run_id = service.create_inventory_backfill_run(["2026R1"])
    original_iter_pages = odata.iter_pages

    def cancel_after_first_entity_page(entity_set: str, **params: object):
        for page in original_iter_pages(entity_set, **params):
            yield page
            if entity_set == "Legislators":
                assert service.runs.cancel(run_id)

    odata.iter_pages = cancel_after_first_entity_page
    assert service.execute_run(run_id) == "canceled"
    assert service.runs.status(run_id) == "canceled"
    state = service.storage.get_session_archive_state("2026R1")
    assert state["inventory_status"] == "inventory_incomplete"
    assert state["inventory_status"] != "inventory_running"


def test_parser_anomaly_marks_historical_session_incomplete(
    tmp_path: Path, fixture_dir: Path
):
    service, _odata, olis, _downloader = _service(tmp_path, fixture_dir)
    olis.html = "<html><body><main>Unexpected redesigned OLIS page</main></body></html>"

    run_id = service.create_inventory_backfill_run(["2026R1"])
    assert service.execute_run(run_id) == "completed_with_errors"
    state = service.storage.get_session_archive_state("2026R1")
    assert state["inventory_status"] == "inventory_incomplete"
    assert state["material_anomaly_count"] >= 1
    assert any(
        row["anomaly_type"] == "olis_display_parser_anomaly"
        for row in service.storage.list_source_anomalies(
            session_key="2026R1", unresolved_only=True
        )
    )
