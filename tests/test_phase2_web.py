from __future__ import annotations

from contextlib import contextmanager
import csv
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from olis_archive import create_app
from olis_archive.services.archive_queries import ArchiveQueries
from olis_archive.services.csv_exports import stream_query_csv
from olis_archive.services.historical_sources import resolve_historical_session_scope
from olis_archive.web import _official_session_rows


@pytest.fixture
def phase2_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "DATABASE_PATH": tmp_path / "data" / "legiview.sqlite3",
            "ARCHIVE_ROOT": tmp_path / "archive",
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "INTER_REQUEST_DELAY": 0,
        }
    )
    yield app
    extension = app.extensions["legiview"]
    extension["workers"].stop(wait=True)
    if extension["runtime"].instance_lock is not None:
        extension["runtime"].instance_lock.close()


def _csrf(client) -> str:  # noqa: ANN001
    assert client.get("/inventory-backfill").status_code == 200
    with client.session_transaction() as browser_session:
        return str(browser_session["_csrf_token"])


def _official_scope():  # noqa: ANN201
    def session(key: str, name: str, begin: str):
        return {
            "SessionKey": key,
            "SessionName": name,
            "BeginDate": begin,
            "EndDate": None,
            "CreatedDate": begin,
            "ModifiedDate": None,
            "DefaultSession": key == "2026R1",
        }

    return resolve_historical_session_scope(
        [
            session("2013R1", "2013 Regular Session", "2013-02-04T00:00:00"),
            session("2014R1", "2014 Regular Session", "2014-02-03T00:00:00"),
            session("2014S1", "2014 Special Session", "2014-09-15T00:00:00"),
            session("2015I1", "2015 Interim Session", "2015-12-01T00:00:00"),
            session("2026R1", "2026 Regular Session", "2026-02-02T00:00:00"),
        ]
    )


def _seed_scope(runtime) -> tuple[int, int]:  # noqa: ANN001
    for key, name, year, begin in (
        ("2014R1", "2014 Regular Session", 2014, "2014-02-03T00:00:00Z"),
        ("2026R1", "2026 Regular Session", 2026, "2026-02-02T00:00:00Z"),
    ):
        runtime.storage.upsert_session(
            {
                "session_key": key,
                "source_session_id": key,
                "session_name": name,
                "session_year": year,
                "begin_date": begin,
            }
        )
    run_id = runtime.collection.runs.create_run(
        "inventory_backfill", session_keys=["2014R1", "2026R1"]
    )
    runtime.storage.finish_session_inventory(
        "2014R1", run_id, "inventory_complete"
    )
    runtime.storage.finish_session_inventory(
        "2026R1", run_id, "inventory_complete_with_errors"
    )
    bill_id = runtime.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_id": "phase2-measure",
            "measure_prefix": "SB",
            "measure_number": "1501",
            "bill_title": "Phase 2 fixture bill",
        }
    )
    return run_id, bill_id


def _seed_document(runtime, bill_id: int, source_id: str, **values) -> int:  # noqa: ANN001
    fields = {
        "bill_id": bill_id,
        "document_kind": "public_testimony",
        "source_entity_type": "CommitteePublicTestimony",
        "source_id": source_id,
        "title": f"Document {source_id}",
        "submitter": "Casey Citizen",
        "city_organization": "Salem Schools",
        "displayed_in_olis": True,
        "source_presence": "active",
        "canonical_download_url": (
            "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
            f"PublicTestimonyDocument/{source_id}"
        ),
    }
    fields.update(values)
    return runtime.storage.upsert_document(fields)


@pytest.mark.parametrize(
    "url",
    [
        "/inventory-backfill",
        "/download-archive",
        "/session-status",
        "/operations",
        "/operations?view=anomalies",
        "/exports/sessions.csv",
        "/exports/documents.csv",
        "/exports/operations.csv",
    ],
)
def test_phase2_pages_and_exports_render_offline(phase2_app, url: str):
    response = phase2_app.test_client().get(url)
    assert response.status_code == 200, response.get_data(as_text=True)


def test_historical_ui_and_export_fail_closed_without_boundary_session(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    runtime.storage.upsert_session(
        {
            "session_key": "2026R1",
            "source_session_id": "2026R1",
            "session_name": "Unbounded fixture",
            "session_year": 2026,
            "begin_date": "2026-02-02T00:00:00Z",
        }
    )

    queries = ArchiveQueries(runtime.database)
    assert queries.session_choices() == []
    assert queries.session_status().total == 0
    assert queries.dashboard_stats()["sessions_in_scope"] == 0

    response = phase2_app.test_client().get("/exports/sessions.csv")
    rows = list(csv.DictReader(StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows == []


def test_inventory_catalogue_shows_but_disables_pre_boundary_sessions(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    scope = _official_scope()
    for session in scope.sessions:
        runtime.storage.upsert_session(
            {
                "session_key": session.session_key,
                "source_session_id": session.session_key,
                "session_name": session.session_name,
                "session_year": int(session.session_key[:4]),
                "begin_date": session.begin_date,
            }
        )

    queries = ArchiveQueries(runtime.database)
    supported = queries.session_choices()
    complete = queries.session_choices(include_unsupported=True)
    assert [row["session_key"] for row in supported] == [
        "2026R1",
        "2015I1",
        "2014S1",
        "2014R1",
    ]
    assert [row["session_key"] for row in complete] == [
        "2026R1",
        "2015I1",
        "2014S1",
        "2014R1",
        "2013R1",
    ]
    assert complete[-1]["supported"] == 0

    html = phase2_app.test_client().get("/inventory-backfill").get_data(as_text=True)
    assert "2013 Regular Session" in html
    assert "Predates the validated 2014R1 support boundary" in html
    from_select = html.split('id="from_session"', 1)[1].split("</select>", 1)[0]
    assert "2013R1" not in from_select
    assert from_select.index("2026R1") < from_select.index("2015I1")
    assert from_select.index("2015I1") < from_select.index("2014S1")
    assert from_select.index("2014S1") < from_select.index("2014R1")


def test_resolved_catalogue_metadata_takes_precedence_over_stale_archive_state():
    scope = _official_scope()
    stored = {
        "2014R1": {
            "session_key": "2014R1",
            "session_name": "Stale stored session name",
            "begin_date": "2014-01-01T00:00:00",
            "end_date": "2014-01-02T00:00:00",
            "inventory_status": "complete",
            "measure_count": 42,
        }
    }

    rows = _official_session_rows(scope, stored)
    row = next(item for item in rows if item["session_key"] == "2014R1")

    assert row["session_name"] == "2014 Regular Session"
    assert row["begin_date"] == "2014-02-03T00:00:00"
    assert row["end_date"] is None
    assert row["inventory_status"] == "complete"
    assert row["measure_count"] == 42


def test_direct_collection_hides_and_rejects_pre_boundary_sessions(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    runtime.storage.upsert_session(
        {
            "session_key": "2007R1",
            "source_session_id": "2007R1",
            "session_name": "2007 Regular Session",
            "session_year": 2007,
            "begin_date": "2007-01-08T00:00:00",
        }
    )
    client = phase2_app.test_client()

    for url in ("/collect/bill", "/collect/session"):
        html = client.get(url).get_data(as_text=True)
        datalist = html.split('id="session-options"', 1)[1].split(
            "</datalist>", 1
        )[0]
        assert "2007R1" not in datalist

    token = _csrf(client)
    response = client.post(
        "/collect/bill",
        data={
            "_csrf_token": token,
            "session_key": "2007R1",
            "bill_id": "HB2001",
        },
    )
    assert response.status_code == 200
    assert "predates LegiView&#39;s validated support boundary 2014R1" in (
        response.get_data(as_text=True)
    )
    assert runtime.collection.runs.list_runs() == []


def test_web_resume_rejects_legacy_frozen_scope_without_requeueing(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    run_id = runtime.collection.runs.create_run(
        "inventory_backfill",
        session_keys=["2007R1"],
        scope={"session_keys": ["2007R1"]},
    )
    assert runtime.collection.runs.claim_run(run_id)
    runtime.storage.normalize_interrupted_work()

    client = phase2_app.test_client()
    response = client.post(
        f"/runs/{run_id}/resume",
        data={"_csrf_token": _csrf(client)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "cannot be resumed" in response.get_data(as_text=True)
    assert runtime.collection.runs.get_run(run_id)["status"] == "interrupted"


@pytest.mark.parametrize("url", ["/inventory-backfill", "/download-archive"])
def test_phase2_start_actions_require_csrf(phase2_app, url: str):
    client = phase2_app.test_client()
    _csrf(client)
    assert client.post(url, data={}).status_code == 400
    assert client.post(url, data={"_csrf_token": "invalid"}).status_code == 400


def test_official_session_discovery_is_csrf_protected_post_only(
    phase2_app, monkeypatch
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    calls: list[str] = []
    scope = _official_scope()

    def resolve_scope():
        calls.append("resolve")
        return scope

    monkeypatch.setattr(runtime.collection, "historical_session_scope", resolve_scope)
    client = phase2_app.test_client()

    # A legacy/query-string GET must remain offline.
    response = client.get("/inventory-backfill?resolve=1")
    assert response.status_code == 200
    assert calls == []

    with client.session_transaction() as browser_session:
        token = str(browser_session["_csrf_token"])
    assert client.post(
        "/inventory-backfill", data={"action": "resolve"}
    ).status_code == 400

    response = client.post(
        "/inventory-backfill",
        data={"_csrf_token": token, "action": "resolve"},
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert calls == ["resolve"]
    assert "2014 Regular Session" in html
    assert "2014 Special Session" in html
    assert "2015 Interim Session" in html
    assert "2013 Regular Session" in html
    assert "Begins before the validated 2014R1 boundary" in html
    assert 'id="from_session"' in html and 'id="to_session"' in html
    assert 'value="2014R1"' in html and "checked" in html


def test_historical_actions_snapshot_selected_ui_scope(phase2_app, monkeypatch):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _seed_scope(runtime)
    document_id = _seed_document(runtime, runtime.storage.get_bill("2026R1", "SB1501")["id"], "9")
    runtime.storage.record_document_probe(
        document_id,
        status="known_size",
        content_length=1024,
        final_url="https://olis.oregonlegislature.gov/document.pdf",
    )
    calls: list[tuple[str, object]] = []

    resolved = _official_scope()
    scope_calls: list[str] = []

    def resolve_scope():
        scope_calls.append("resolve")
        return resolved

    def create_inventory(  # noqa: ANN001
        session_keys=None,
        *,
        probe_remote_sizes=False,
        force_full=False,
        resolved_scope=None,
    ):
        assert session_keys is None
        calls.append(
            (
                "inventory",
                (
                    tuple(resolved_scope.session_keys),
                    probe_remote_sizes,
                    force_full,
                ),
            )
        )
        return 80

    def create_download(
        session_keys,
        *,
        document_kinds=None,
        retryable_failures_only=False,
        missing_pending_only=True,
    ):  # noqa: ANN001
        calls.append(
            (
                "download",
                (
                    tuple(session_keys),
                    tuple(document_kinds or ()),
                    retryable_failures_only,
                    missing_pending_only,
                ),
            )
        )
        return 81

    monkeypatch.setattr(runtime.collection, "create_inventory_backfill_run", create_inventory)
    monkeypatch.setattr(runtime.collection, "historical_session_scope", resolve_scope)
    monkeypatch.setattr(runtime.collection, "create_download_archive_run", create_download)
    monkeypatch.setattr(
        runtime.collection,
        "download_archive_preflight",
        lambda *args, **kwargs: SimpleNamespace(
            documents_in_scope=1,
            already_downloaded=0,
            pending_or_missing=1,
            retryable_failures=0,
            terminal_or_non_downloadable=0,
            known_pending_bytes=1024,
            unknown_size_pending=0,
            free_bytes=10 * 1024**3,
            minimum_free_space_bytes=0,
            known_bytes_fit=True,
        ),
    )
    client = phase2_app.test_client()
    token = _csrf(client)

    inventory = client.post(
        "/inventory-backfill",
        data={
            "_csrf_token": token,
            "session_keys": ["2014R1", "2026R1"],
            "probe_remote_sizes": "1",
        },
    )
    assert inventory.status_code == 303
    assert inventory.headers["Location"].endswith("/runs/80")
    assert scope_calls == ["resolve"]

    preview = client.post(
        "/download-archive",
        data={
            "_csrf_token": token,
            "session_keys": "2026R1",
            "document_kinds": "public_testimony",
            "eligibility": "missing_pending",
            "action": "preview",
        },
    )
    assert preview.status_code == 200
    assert "Known pending bytes" in preview.get_data(as_text=True)
    assert calls == [("inventory", (("2014R1", "2026R1"), True, False))]

    download = client.post(
        "/download-archive",
        data={
            "_csrf_token": token,
            "session_keys": "2026R1",
            "document_kinds": "public_testimony",
            "eligibility": "missing_pending",
            "action": "start",
        },
    )
    assert download.status_code == 303
    assert download.headers["Location"].endswith("/runs/81")
    assert calls[-1] == (
        "download",
        (("2026R1",), ("public_testimony",), False, True),
    )


def test_inventory_range_queues_exact_inclusive_official_chronology(
    phase2_app, monkeypatch
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    scope = _official_scope()
    resolutions: list[object] = []
    created: list[object] = []

    def resolve_scope():
        resolutions.append(scope)
        return scope

    def create_inventory(
        session_keys=None,
        *,
        probe_remote_sizes=False,
        force_full=False,
        resolved_scope=None,
    ):
        assert session_keys is None
        assert probe_remote_sizes is False
        assert force_full is False
        created.append(resolved_scope)
        return 83

    monkeypatch.setattr(runtime.collection, "historical_session_scope", resolve_scope)
    monkeypatch.setattr(
        runtime.collection, "create_inventory_backfill_run", create_inventory
    )
    client = phase2_app.test_client()
    token = _csrf(client)

    response = client.post(
        "/inventory-backfill",
        data={
            "_csrf_token": token,
            "scope_mode": "range",
            "from_session": "2014S1",
            "to_session": "2026R1",
        },
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/runs/83")
    assert resolutions == [scope]
    assert len(created) == 1
    assert created[0].sessions is scope.sessions
    assert created[0].session_keys == ("2014S1", "2015I1", "2026R1")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"scope_mode": "range", "from_session": "2026R1", "to_session": "2014R1"},
            "From session 2026R1 is newer than To session 2014R1",
        ),
        (
            {"scope_mode": "range", "from_session": "2013R1", "to_session": "2014R1"},
            "predate LegiView&#39;s validated support boundary 2014R1",
        ),
        (
            {"scope_mode": "range", "from_session": "2014R1", "to_session": "2099R1"},
            "not in the resolved official catalogue: 2099R1",
        ),
        (
            {"scope_mode": "exact", "session_keys": ["2014R1", "2013R1"]},
            "predate LegiView&#39;s validated support boundary 2014R1",
        ),
    ],
)
def test_inventory_scope_tampering_cannot_queue(
    phase2_app, monkeypatch, payload, message
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    scope = _official_scope()
    created: list[object] = []
    monkeypatch.setattr(
        runtime.collection, "historical_session_scope", lambda: scope
    )
    monkeypatch.setattr(
        runtime.collection,
        "create_inventory_backfill_run",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    client = phase2_app.test_client()
    token = _csrf(client)

    response = client.post(
        "/inventory-backfill", data={"_csrf_token": token, **payload}
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert message in html
    assert created == []


def test_download_archive_can_start_background_verification_only_scope(
    phase2_app, monkeypatch
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _seed_scope(runtime)
    created: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        runtime.collection,
        "download_archive_preflight",
        lambda *args, **kwargs: SimpleNamespace(
            documents_in_scope=2,
            already_downloaded=2,
            pending_or_missing=0,
            retryable_failures=0,
            terminal_or_non_downloadable=0,
            known_pending_bytes=0,
            unknown_size_pending=0,
            free_bytes=10 * 1024**3,
            minimum_free_space_bytes=0,
            known_bytes_fit=True,
        ),
    )

    def create_download(session_keys, **kwargs):  # noqa: ANN001
        created.append(tuple(session_keys))
        return 82

    monkeypatch.setattr(
        runtime.collection, "create_download_archive_run", create_download
    )
    client = phase2_app.test_client()
    token = _csrf(client)
    values = {
        "_csrf_token": token,
        "session_keys": "2026R1",
        "eligibility": "missing_pending",
    }

    preview = client.post("/download-archive", data={**values, "action": "preview"})
    preview_html = preview.get_data(as_text=True)
    assert preview.status_code == 200
    assert "Recorded downloaded" in preview_html
    assert "Background verification" in preview_html
    assert "Already valid" not in preview_html

    launch = client.post("/download-archive", data={**values, "action": "start"})
    assert launch.status_code == 303
    assert launch.headers["Location"].endswith("/runs/82")
    assert created == [("2026R1",)]


def test_document_filters_and_pagination_are_database_backed(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, bill_id = _seed_scope(runtime)
    for value in range(1, 61):
        _seed_document(
            runtime,
            bill_id,
            str(value),
            title=f"Page fixture {value:03d}",
            displayed_in_olis=value % 2 == 0,
            source_presence="missing" if value == 60 else "active",
        )

    client = phase2_app.test_client()
    first = client.get("/documents?session=2026R1&organization=Salem&page=1")
    second = client.get("/documents?session=2026R1&organization=Salem&page=2")
    assert first.status_code == second.status_code == 200
    first_html = first.get_data(as_text=True)
    second_html = second.get_data(as_text=True)
    assert "60 results" in first_html
    assert "Page fixture 001" in first_html
    assert "Page fixture 060" not in first_html
    assert "Page fixture 060" in second_html

    missing = client.get(
        "/documents?session=2026R1&source_presence=missing&displayed_in_olis=yes"
    ).get_data(as_text=True)
    assert "Page fixture 060" in missing
    assert "1 result" in missing


def test_session_status_and_operations_use_filtered_sql_pages(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    run_id, bill_id = _seed_scope(runtime)
    document_id = _seed_document(runtime, bill_id, "255890", title="Zero byte testimony")
    runtime.storage.update_document_download_state(
        document_id, "failed_terminal", last_error="Downloaded file is empty"
    )
    runtime.storage.record_document_probe(
        document_id,
        status="known_size",
        content_length=1024,
        final_url="https://olis.oregonlegislature.gov/document.pdf",
    )
    _seed_document(runtime, bill_id, "unprobed-size")
    runtime.collection.runs.record_error(
        run_id,
        stage="download_archive",
        error="Downloaded file is empty",
        retryable=False,
        session_key="2026R1",
        bill_id_compact="SB1501",
        document_id=document_id,
        source_entity_type="CommitteePublicTestimony",
        source_id="255890",
    )
    resolved_error_id = runtime.collection.runs.record_error(
        run_id,
        stage="download_archive",
        error="Resolved fixture error",
        retryable=False,
        session_key="2026R1",
        bill_id_compact="SB1501",
        document_id=document_id,
        source_entity_type="CommitteePublicTestimony",
        source_id="255890-resolved",
    )
    runtime.storage.resolve_collection_error(resolved_error_id)
    runtime.storage.record_source_anomaly(
        "type_drift",
        severity="warning",
        affects_completeness=True,
        message="Fixture source type drift",
        session_key="2026R1",
        bill_id=bill_id,
        bill_id_compact="SB1501",
        document_id=document_id,
        source_entity_type="CommitteePublicTestimony",
        source_id="255890",
        run_id=run_id,
    )
    later_run_id = runtime.collection.runs.create_run(
        "inventory_backfill", session_keys=["2026R1"]
    )
    runtime.storage.record_source_anomaly(
        "type_drift",
        severity="warning",
        affects_completeness=True,
        message="Fixture source type drift",
        session_key="2026R1",
        bill_id=bill_id,
        bill_id_compact="SB1501",
        document_id=document_id,
        source_entity_type="CommitteePublicTestimony",
        source_id="255890",
        run_id=later_run_id,
    )

    client = phase2_app.test_client()
    status = client.get("/session-status?session=2026R1").get_data(as_text=True)
    assert "2026 Regular Session" in status
    assert "inventory complete with errors" in status
    assert "Zero byte" not in status
    assert "1 known / 1 unknown" in status
    assert "Lower bound" in status

    errors = client.get(
        f"/operations?view=errors&run={run_id}&session=2026R1&retryable=no"
    ).get_data(as_text=True)
    assert "Downloaded file is empty" in errors
    anomalies = client.get(
        f"/operations?view=anomalies&run={run_id}&session=2026R1"
        "&anomaly_type=type_drift"
    ).get_data(as_text=True)
    assert "Fixture source type drift" in anomalies
    assert "Material gap" in anomalies
    run_detail = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Review anomalies" in run_detail

    error_export_url = (
        f"/exports/operations.csv?view=errors&run={run_id}&session=2026R1"
        "&bill=SB1501&stage=CommitteePublicTestimony&kind=public_testimony"
        "&retryable=no&error_class=Error"
    )
    error_rows = list(
        csv.DictReader(
            StringIO(
                client.get(error_export_url)
                .get_data(as_text=True)
                .lstrip("\ufeff")
            )
        )
    )
    assert [row["message"] for row in error_rows] == ["Downloaded file is empty"]

    resolved_rows = list(
        csv.DictReader(
            StringIO(
                client.get(error_export_url + "&include_resolved=1")
                .get_data(as_text=True)
                .lstrip("\ufeff")
            )
        )
    )
    assert {row["message"] for row in resolved_rows} == {
        "Downloaded file is empty",
        "Resolved fixture error",
    }

    anomaly_rows = list(
        csv.DictReader(
            StringIO(
                client.get(
                    f"/exports/operations.csv?view=anomalies&run={run_id}"
                    "&session=2026R1&bill=SB1501&stage=CommitteePublicTestimony"
                    "&kind=public_testimony&anomaly_type=type_drift&severity=warning"
                )
                .get_data(as_text=True)
                .lstrip("\ufeff")
            )
        )
    )
    assert [row["message"] for row in anomaly_rows] == ["Fixture source type drift"]


def test_retry_all_snapshots_every_filtered_page_and_claims_bounded_items(
    phase2_app, monkeypatch
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, bill_id = _seed_scope(runtime)
    source_run_id = runtime.collection.runs.create_run(
        "collect_bill",
        session_key="2026R1",
        bill_id_compact="SB1501",
        bills_total=1,
    )
    matching_ids: list[int] = []
    for value in range(55):
        document_id = _seed_document(
            runtime,
            bill_id,
            f"retry-{value:03d}",
        )
        runtime.storage.update_document_download_state(
            document_id,
            "failed_terminal" if value == 0 else "failed_retryable",
            last_error="Fixture download failure",
        )
        runtime.collection.runs.add_document_item(
            source_run_id,
            document_id,
            bill_id,
            f"source-retry-{value:03d}",
            session_key="2026R1",
        )
        matching_ids.append(document_id)
    # A duplicate source-run item must not duplicate the snapshotted document.
    runtime.collection.runs.add_document_item(
        source_run_id,
        matching_ids[0],
        bill_id,
        "duplicate-source-item",
        session_key="2026R1",
    )
    for value in range(5):
        document_id = _seed_document(runtime, bill_id, f"not-in-run-{value:03d}")
        runtime.storage.update_document_download_state(
            document_id,
            "failed_retryable",
            last_error="Nonmatching fixture failure",
        )

    client = phase2_app.test_client()
    token = _csrf(client)
    response = client.post(
        "/retry-failures",
        data={
            "_csrf_token": token,
            "action": "all",
            "run_id": str(source_run_id),
            "session": "2026R1",
            "bill": "SB1501",
            # The action must cover every match even when submitted from page 2.
            "page": "2",
        },
    )
    assert response.status_code == 303
    retry_run_id = int(response.headers["Location"].rsplit("/", 1)[-1])
    retry_run = runtime.collection.runs.get_run(retry_run_id)
    scope = json.loads(retry_run["requested_scope_json"])
    assert scope["selection"] == "all_matching"
    assert "document_ids" not in scope
    assert scope["retry_match"]["matching_count"] == 55
    assert scope["retry_match"]["source_run_id"] == source_run_id
    retry_items = [
        row
        for row in runtime.collection.runs.run_items(retry_run_id)
        if row["item_type"] == "document"
    ]
    assert len(retry_items) == 55
    assert {int(row["document_id"]) for row in retry_items} == set(matching_ids)
    assert all(str(row["item_key"]).startswith("document:") for row in retry_items)

    # A file repaired after the exact snapshot is skipped without reaching the
    # downloader; every other durable item is claimed exactly once.
    repaired_id = matching_ids[-1]
    runtime.storage.update_document_download_state(
        repaired_id,
        "downloaded",
        validation_status="valid",
    )
    downloaded: list[int] = []

    def complete_claim(run_id, document):  # noqa: ANN001
        document_id = int(document["id"])
        downloaded.append(document_id)
        # Model the production claimed-download callback contract: document,
        # run-item outcome, and byte accounting are one durable commit.
        with runtime.database.transaction():
            runtime.storage.update_document_download_state(
                document_id,
                "downloaded",
                validation_status="valid",
            )
            runtime.collection.runs.mark_document_item(
                run_id, document_id, "completed", "Fixture download completed"
            )
            runtime.collection.runs.add_downloaded_bytes(run_id, 1)
        return document_id, "downloaded", 1

    monkeypatch.setattr(
        runtime.collection, "_download_claimed_document", complete_claim
    )
    assert runtime.collection.execute_run(retry_run_id) == "completed"
    assert set(downloaded) == set(matching_ids) - {repaired_id}
    assert len(downloaded) == len(set(downloaded)) == 54
    completed = runtime.collection.runs.get_run(retry_run_id)
    assert completed["documents_downloaded"] == 54
    assert completed["documents_skipped"] == 1
    assert completed["bytes_downloaded"] == 54


def test_terminal_retry_is_available_but_not_checked_by_default(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, bill_id = _seed_scope(runtime)
    terminal_id = _seed_document(runtime, bill_id, "terminal-explicit")
    retryable_id = _seed_document(runtime, bill_id, "retryable-default")
    runtime.storage.update_document_download_state(
        terminal_id, "failed_terminal", last_error="Invalid payload"
    )
    runtime.storage.update_document_download_state(
        retryable_id, "failed_retryable", last_error="Temporary outage"
    )

    html = phase2_app.test_client().get("/retry-failures").get_data(as_text=True)
    terminal_start = html.index(f'value="{terminal_id}"')
    terminal_tag = html[terminal_start : html.index(">", terminal_start)]
    retryable_start = html.index(f'value="{retryable_id}"')
    retryable_tag = html[retryable_start : html.index(">", retryable_start)]

    assert "checked" not in terminal_tag
    assert "checked" in retryable_tag
    assert "Explicit selection required" in html


def test_retry_all_invalid_run_filter_cannot_widen_scope(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, bill_id = _seed_scope(runtime)
    document_id = _seed_document(runtime, bill_id, "invalid-filter")
    runtime.storage.update_document_download_state(
        document_id, "failed_retryable", last_error="Fixture failure"
    )
    before = len(runtime.collection.runs.list_runs())

    client = phase2_app.test_client()
    response = client.post(
        "/retry-failures",
        data={
            "_csrf_token": _csrf(client),
            "action": "all",
            "run_id": "not-a-run-id",
        },
    )
    assert response.status_code == 200
    assert "No failed documents match" in response.get_data(as_text=True)
    assert len(runtime.collection.runs.list_runs()) == before


def test_retry_ui_and_service_exclude_pre_boundary_documents(
    phase2_app, monkeypatch
):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    runtime.storage.upsert_session(
        {
            "session_key": "2007R1",
            "session_name": "2007 Regular Session",
            "session_year": 2007,
            "begin_date": "2007-01-08T00:00:00",
        }
    )
    legacy_bill_id = runtime.storage.upsert_bill(
        {
            "session_key": "2007R1",
            "measure_prefix": "HB",
            "measure_number": 2001,
        }
    )
    legacy_document_id = _seed_document(
        runtime,
        legacy_bill_id,
        "legacy-retry",
        canonical_download_url=(
            "https://olis.oregonlegislature.gov/liz/2007R1/Downloads/"
            "PublicTestimonyDocument/1"
        ),
    )
    runtime.storage.update_document_download_state(
        legacy_document_id,
        "failed_retryable",
        last_error="Legacy fixture failure",
    )

    client = phase2_app.test_client()
    html = client.get("/retry-failures").get_data(as_text=True)
    assert f'value="{legacy_document_id}"' not in html

    before = len(runtime.collection.runs.list_runs())
    response = client.post(
        "/retry-failures",
        data={
            "_csrf_token": _csrf(client),
            "document_ids": str(legacy_document_id),
        },
    )
    assert response.status_code == 200
    assert "Select at least one document to retry" in response.get_data(as_text=True)
    assert len(runtime.collection.runs.list_runs()) == before

    with pytest.raises(ValueError, match="2007R1.*2014R1"):
        runtime.collection.create_retry_selected_run([legacy_document_id])

    monkeypatch.setattr(
        runtime.collection,
        "_download_claimed_document",
        lambda *_args, **_kwargs: pytest.fail("legacy retry reached downloader"),
    )
    legacy_retry = runtime.collection.runs.create_run(
        "retry_failures",
        session_key="2007R1",
        scope={"document_ids": [legacy_document_id]},
    )
    assert runtime.collection.execute_run(legacy_retry) == "failed"


def test_unfiltered_retry_snapshot_excludes_pre_boundary_documents(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, supported_bill_id = _seed_scope(runtime)
    supported_id = _seed_document(runtime, supported_bill_id, "supported-retry")
    runtime.storage.update_document_download_state(
        supported_id, "failed_retryable", last_error="Supported failure"
    )
    runtime.storage.upsert_session(
        {
            "session_key": "2007R1",
            "session_name": "2007 Regular Session",
            "session_year": 2007,
            "begin_date": "2007-01-08T00:00:00",
        }
    )
    legacy_bill_id = runtime.storage.upsert_bill(
        {
            "session_key": "2007R1",
            "measure_prefix": "HB",
            "measure_number": 2001,
        }
    )
    legacy_id = _seed_document(runtime, legacy_bill_id, "legacy-retry")
    runtime.storage.update_document_download_state(
        legacy_id, "failed_retryable", last_error="Legacy failure"
    )

    run_id, count = runtime.collection.create_retry_matching_run()
    items = [
        row
        for row in runtime.collection.runs.run_items(run_id)
        if row["item_type"] == "document"
    ]

    assert count == 1
    assert [row["document_id"] for row in items] == [supported_id]


def test_source_links_require_https_and_an_allowlisted_official_host(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    run_id, bill_id = _seed_scope(runtime)
    runtime.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_id": "phase2-measure",
            "measure_prefix": "SB",
            "measure_number": "1501",
            "bill_title": "Phase 2 fixture bill",
            "source_url": "https://evil.example/phishing",
        }
    )
    document_id = _seed_document(
        runtime,
        bill_id,
        "safe-link",
        source_url="https://olis.oregonlegislature.gov/liz/2026R1/safe",
    )
    runtime.collection.runs.record_error(
        run_id,
        stage="sync_measures",
        error="Unsafe source fixture",
        retryable=False,
        source_url="http://olis.oregonlegislature.gov/not-https",
    )
    runtime.collection.runs.record_error(
        run_id,
        stage="sync_measures",
        error="Safe API source fixture",
        retryable=False,
        source_url="https://api.oregonlegislature.gov/odata/odataservice.svc/Measures",
    )

    client = phase2_app.test_client()
    bill_html = client.get(f"/bills/{bill_id}").get_data(as_text=True)
    assert 'href="https://evil.example/phishing"' not in bill_html
    assert 'href="https://olis.oregonlegislature.gov/liz/2026R1/safe"' in bill_html

    document_html = client.get(f"/documents/{document_id}").get_data(as_text=True)
    assert 'href="https://olis.oregonlegislature.gov/liz/2026R1/safe"' in document_html

    run_html = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert 'href="http://olis.oregonlegislature.gov/not-https"' not in run_html
    assert (
        'href="https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"'
        in run_html
    )


def test_document_csv_contains_audit_fields_filters_and_neutralizes_formulas(phase2_app):
    runtime = phase2_app.extensions["legiview"]["runtime"]
    _, bill_id = _seed_scope(runtime)
    _seed_document(runtime, bill_id, "formula", title="=HYPERLINK(\"bad\")")
    _seed_document(runtime, bill_id, "other", title="Excluded", source_presence="missing")

    response = phase2_app.test_client().get(
        "/exports/documents.csv?session=2026R1&source_presence=active"
    )
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].endswith(
        'filename="legiview-document-inventory.csv"'
    )
    text = response.get_data(as_text=True).lstrip("\ufeff")
    rows = list(csv.DictReader(StringIO(text)))
    assert len(rows) == 1
    assert rows[0]["source_id"] == "formula"
    assert rows[0]["title"].startswith("'=HYPERLINK")
    assert "displayed_in_olis" in rows[0]
    assert "current_payload_bytes" in rows[0]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_csv_helper_uses_fetchmany_in_bounded_batches():
    class Cursor:
        description = (("name",),)

        def __init__(self):
            self.calls: list[int] = []
            self.pages = [[{"name": "one"}, {"name": "two"}], [{"name": "three"}], []]

        def fetchmany(self, size):  # noqa: ANN001
            self.calls.append(size)
            return self.pages.pop(0)

    cursor = Cursor()

    class Connection:
        @staticmethod
        def execute(sql, params):  # noqa: ANN001
            assert sql == "SELECT name FROM fixture"
            assert params == ()
            return cursor

    class Database:
        @contextmanager
        def connection(self):
            yield Connection()

    output = "".join(
        stream_query_csv(Database(), "SELECT name FROM fixture", batch_size=2)
    )
    assert output.lstrip("\ufeff").splitlines() == ["name", "one", "two", "three"]
    assert cursor.calls == [2, 2, 2]
