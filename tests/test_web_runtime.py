from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from olis_archive import create_app
from olis_archive import __main__ as cli
from olis_archive.config import AppConfig
from olis_archive.database import Database
from olis_archive.runtime import InstanceAlreadyRunning, build_runtime
from olis_archive.services.archive_paths import ARCHIVE_OWNERSHIP_MARKER, UnsafeArchivePath
from olis_archive.services.odata import ODataError


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@pytest.fixture
def web_app(tmp_path: Path):
    database_path = tmp_path / "data" / "legiview.sqlite3"
    archive_root = tmp_path / "archive"
    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "PROJECT_ROOT": tmp_path,
            "DATABASE_PATH": database_path,
            "ARCHIVE_ROOT": archive_root,
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "INTER_REQUEST_DELAY": 0,
        }
    )
    yield app
    extension = app.extensions["legiview"]
    extension["workers"].stop(wait=True)
    if extension["runtime"].instance_lock is not None:
        extension["runtime"].instance_lock.close()


def _get_csrf_token(client, path: str = "/collect/bill") -> str:  # noqa: ANN001
    response = client.get(path)
    assert response.status_code == 200, response.get_data(as_text=True)
    with client.session_transaction() as browser_session:
        token = browser_session.get("_csrf_token")
    assert isinstance(token, str)
    assert len(token) >= 32
    html = response.get_data(as_text=True)
    assert 'name="_csrf_token"' in html
    assert f'value="{token}"' in html
    return token


def _seed_bill(runtime) -> int:  # noqa: ANN001
    runtime.storage.upsert_session(
        {
            "session_key": "2026R1",
            "source_session_id": "2026R1",
            "session_name": "2026 Regular Session",
            "session_year": 2026,
        }
    )
    return runtime.storage.upsert_bill(
        {
            "session_key": "2026R1",
            "measure_id": "fixture-measure",
            "measure_prefix": "SB",
            "measure_number": "1501",
            "bill_title": "Fixture bill title",
            "catchline": "A fixture catchline.",
            "emergency_clause": True,
            "vetoed": False,
            "source_url": "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Overview/SB1501",
        }
    )


def _seed_document(runtime, bill_id: int, source_id: str, title: str) -> int:  # noqa: ANN001
    return runtime.storage.upsert_document(
        {
            "bill_id": bill_id,
            "document_kind": "public_testimony",
            "source_entity_type": "PublicTestimony",
            "source_id": source_id,
            "title": title,
            "canonical_download_url": (
                "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
                f"CommitteeMeetingDocument/{source_id}"
            ),
        }
    )


def test_app_factory_uses_isolated_paths_and_does_not_start_workers(web_app, tmp_path: Path):
    extension = web_app.extensions["legiview"]
    runtime = extension["runtime"]

    assert web_app.testing
    assert runtime.config.database_path == tmp_path / "data" / "legiview.sqlite3"
    assert runtime.config.archive_root == tmp_path / "archive"
    assert runtime.config.archive_root.is_dir()
    assert runtime.database.schema_version() == 6
    assert runtime.collection.downloader.calculate_sha256 is False
    assert runtime.collection.downloader.durable_writes is False
    assert extension["workers"].snapshot() == {
        "workers": 0,
        "queued": 0,
        "active": 0,
        "alive": 0,
    }


def test_app_rejects_untrusted_host_headers(web_app):
    client = web_app.test_client()

    assert client.get("/health", headers={"Host": "localhost:5055"}).status_code == 200
    assert client.get("/health", headers={"Host": "127.0.0.1:5055"}).status_code == 200
    assert client.get("/health", headers={"Host": "[::1]:5055"}).status_code == 200
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400


def test_app_factory_keeps_run_dispatcher_single_with_larger_configured_limits(
    tmp_path: Path,
):
    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "DATABASE_PATH": tmp_path / "data" / "legiview.sqlite3",
            "ARCHIVE_ROOT": tmp_path / "archive",
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "INTER_REQUEST_DELAY": 0,
            "ODATA_WORKER_COUNT": 4,
            "DOWNLOAD_WORKER_COUNT": 8,
            "HTML_REQUEST_CONCURRENCY": 2,
        }
    )
    try:
        extension = app.extensions["legiview"]
        runtime = extension["runtime"]
        assert runtime.config.odata_worker_count == 4
        assert runtime.config.download_worker_count == 8
        assert runtime.config.html_request_concurrency == 2
        assert extension["workers"].worker_count == 1
    finally:
        assert app.extensions["legiview"]["shutdown"]()


def test_app_factory_database_override_never_opens_environment_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    environment_root = tmp_path / "environment-root"
    environment_database = environment_root / "data" / "would-be-production.sqlite3"
    override_database = tmp_path / "isolated" / "test.sqlite3"
    override_archive = tmp_path / "isolated-archive"
    monkeypatch.setenv("LEGIVIEW_PROJECT_ROOT", str(environment_root))
    monkeypatch.setenv("LEGIVIEW_DATABASE_PATH", "data/would-be-production.sqlite3")
    monkeypatch.setenv("LEGIVIEW_ARCHIVE_ROOT", "environment-archive")

    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "DATABASE_PATH": override_database,
            "ARCHIVE_ROOT": override_archive,
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "INTER_REQUEST_DELAY": 0,
        }
    )
    try:
        runtime = app.extensions["legiview"]["runtime"]
        run_id = runtime.collection.runs.create_run(
            "collect_bill",
            session_key="2026R1",
            bill_id_compact="SB1501",
        )

        assert runtime.config.database_path == override_database.resolve()
        assert runtime.config.database_path_configured == str(override_database.resolve())
        assert runtime.collection.runs.get_run(run_id)["requested_bill_id_compact"] == "SB1501"
        assert override_database.is_file()
        assert not environment_database.exists()
    finally:
        assert app.extensions["legiview"]["shutdown"]()


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/collect/bill",
        "/collect/session",
        "/bills",
        "/documents",
        "/runs",
        "/retry-failures",
        "/settings",
        "/help",
        "/health",
    ],
)
def test_required_get_pages_render_without_source_access(web_app, url: str):
    response = web_app.test_client().get(url)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_data()


def test_collect_bill_post_creates_a_queued_run_without_executing_it(web_app, monkeypatch):
    runtime = web_app.extensions["legiview"]["runtime"]
    executions: list[int] = []
    monkeypatch.setattr(runtime.collection, "execute_run", executions.append)
    client = web_app.test_client()
    token = _get_csrf_token(client)

    response = client.post(
        "/collect/bill",
        data={"_csrf_token": token, "session_key": "2026r1", "bill_id": "sb 1501"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/runs/1")
    run = runtime.collection.runs.get_run(1)
    assert run is not None
    assert run["status"] == "queued"
    assert run["requested_session_key"] == "2026R1"
    assert run["requested_bill_id_compact"] == "SB1501"
    assert executions == []
    assert web_app.extensions["legiview"]["workers"].snapshot()["queued"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "/collect/bill",
        "/collect/session",
        "/runs/999/cancel",
        "/runs/999/resume",
        "/retry-failures",
        "/settings",
    ],
)
def test_every_mutating_post_rejects_missing_and_invalid_csrf_tokens(web_app, url: str):
    client = web_app.test_client()
    _get_csrf_token(client)

    missing = client.post(url, data={})
    invalid = client.post(url, data={"_csrf_token": "not-the-session-token"})

    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert "security token is missing or invalid" in missing.get_data(as_text=True)
    assert "security token is missing or invalid" in invalid.get_data(as_text=True)


def test_valid_csrf_forms_allow_session_cancel_resume_retry_and_settings(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    client = web_app.test_client()
    token = _get_csrf_token(client, "/collect/session")

    collected = client.post(
        "/collect/session",
        data={"_csrf_token": token, "session_key": "2026R1", "max_bills": "1"},
    )
    assert collected.status_code == 303
    collected_run_id = int(collected.headers["Location"].rsplit("/", 1)[-1])

    run_page = client.get(f"/runs/{collected_run_id}")
    assert run_page.status_code == 200
    assert f'value="{token}"' in run_page.get_data(as_text=True)
    canceled = client.post(
        f"/runs/{collected_run_id}/cancel",
        data={"_csrf_token": token},
    )
    assert canceled.status_code == 303
    assert runtime.collection.runs.get_run(collected_run_id)["status"] == "canceled"

    interrupted_run_id = runtime.collection.create_collect_bill_run("2026R1", "SB1501")
    assert runtime.collection.runs.claim_run(interrupted_run_id)
    runtime.storage.normalize_interrupted_work()
    interrupted_page = client.get(f"/runs/{interrupted_run_id}")
    assert interrupted_page.status_code == 200
    assert interrupted_page.get_data(as_text=True).count(f'value="{token}"') >= 2
    resumed = client.post(
        f"/runs/{interrupted_run_id}/resume",
        data={"_csrf_token": token},
    )
    assert resumed.status_code == 303
    assert runtime.collection.runs.get_run(interrupted_run_id)["status"] == "queued"

    bill_id = _seed_bill(runtime)
    document_id = _seed_document(runtime, bill_id, "244133", "Retry fixture")
    runtime.storage.update_document_download_state(
        document_id,
        "failed_retryable",
        last_error="Fixture failure",
    )
    retry_page = client.get("/retry-failures")
    assert retry_page.status_code == 200
    assert f'value="{token}"' in retry_page.get_data(as_text=True)
    retried = client.post(
        "/retry-failures",
        data={
            "_csrf_token": token,
            "action": "selected",
            "document_ids": str(document_id),
        },
    )
    assert retried.status_code == 303
    retry_run_id = int(retried.headers["Location"].rsplit("/", 1)[-1])
    assert runtime.collection.runs.get_run(retry_run_id)["run_type"] == "retry_failures"

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert f'value="{token}"' in settings_page.get_data(as_text=True)
    saved = client.post("/settings", data={"_csrf_token": token})
    assert saved.status_code == 303
    assert saved.headers["Location"].endswith("/settings")


def test_settings_save_relative_archive_portably_and_use_gb_floor(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    client = web_app.test_client()
    token = _get_csrf_token(client, "/settings")
    page = client.get("/settings")
    html = page.get_data(as_text=True)
    assert "Minimum free-space floor (GB)" in html
    assert "Project root" in html
    assert str(runtime.config.project_root) in html
    assert str(runtime.config.database_path) in html

    proposed_archive = runtime.config.project_root / "storage" / "archive"
    assert not proposed_archive.exists()
    response = client.post(
        "/settings",
        data={
            "_csrf_token": token,
            "archive_root": "storage/archive",
            "minimum_free_space_gb": "7.5",
        },
    )

    assert response.status_code == 303
    assert runtime.storage.get_setting("archive_root") == "storage/archive"
    assert runtime.storage.get_setting("minimum_free_space_gb") == 7.5
    effective = runtime.config.with_settings(runtime.storage.get_settings())
    assert effective.archive_root_configured == "storage/archive"
    assert effective.archive_root == runtime.config.project_root / "storage" / "archive"
    assert effective.minimum_free_space_gb == 7.5
    assert effective.minimum_free_space_bytes == int(7.5 * 1024**3)
    assert not proposed_archive.exists()


def test_settings_reject_nonempty_unowned_archive_without_mutating_it(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    unowned = runtime.config.project_root / "Documents"
    unowned.mkdir()
    unrelated = unowned / "unrelated.part"
    unrelated.write_bytes(b"personal")
    client = web_app.test_client()
    token = _get_csrf_token(client, "/settings")

    response = client.post(
        "/settings",
        data={"_csrf_token": token, "archive_root": "Documents"},
    )

    assert response.status_code == 200
    assert "not owned by LegiView" in response.get_data(as_text=True)
    assert runtime.storage.get_setting("archive_root") is None
    assert unrelated.read_bytes() == b"personal"
    assert not (unowned / ARCHIVE_OWNERSHIP_MARKER).exists()


def test_detail_pages_and_registered_file_route_are_wired(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    bill_id = _seed_bill(runtime)
    runtime.storage.upsert_bill_sponsor(
        {
            "bill_id": bill_id,
            "source_measure_sponsor_id": "member-1",
            "normalized_category": "regular",
            "sponsor_kind": "legislator",
            "resolved_display_name": "Robin Regular",
        }
    )
    runtime.storage.upsert_bill_sponsor(
        {
            "bill_id": bill_id,
            "source_measure_sponsor_id": "presession-1",
            "raw_sponsor_type": "Presession",
            "raw_sponsor_level": "Regular",
            "normalized_category": "regular",
            "sponsor_kind": "other",
            "resolved_display_name": "(Presession filed.)",
            "pre_session_filed_message": "(Presession filed.)",
        }
    )
    runtime.storage.upsert_bill_sponsor(
        {
            "bill_id": bill_id,
            "source_measure_sponsor_id": "unknown-1",
            "raw_sponsor_type": "FutureType",
            "raw_sponsor_level": "FutureLevel",
            "normalized_category": "unknown",
            "sponsor_kind": "other",
            "resolved_display_name": "Future source record",
        }
    )
    runtime.storage.upsert_committee_agenda_item(
        {
            "session_key": "2026R1",
            "source_agenda_item_id": "fixture-agenda",
            "bill_id": bill_id,
            "bill_id_compact": "SB1501",
            "agenda_item_type": "Public Hearing",
            "description": "Heard",
        }
    )
    empty_bill_page = web_app.test_client().get(f"/bills/{bill_id}").get_data(as_text=True)
    assert "No documents discovered" in empty_bill_page
    assert "Public Hearing" in empty_bill_page
    assert "Heard" in empty_bill_page
    assert "Emergency Clause</span><strong>Yes" in empty_bill_page
    assert "Vetoed</span><strong>No" in empty_bill_page
    parsed_bill = BeautifulSoup(empty_bill_page, "html.parser")
    regular_card = parsed_bill.find("h3", string="Regular Sponsors").parent.get_text(" ", strip=True)
    filing_card = parsed_bill.find(
        "h3", string="Filing Notices and Unmapped Sponsor Records"
    ).parent.get_text(" ", strip=True)
    assert "Robin Regular" in regular_card
    assert "Presession filed" not in regular_card
    assert "Presession filed" in filing_card
    assert "FutureType / FutureLevel" in filing_card

    document_id = _seed_document(runtime, bill_id, "244133", "Written testimony")
    run_id = runtime.collection.create_collect_bill_run("2026R1", "SB1501")
    error_url = (
        "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/"
        "PublicTestimonyDocument/255890"
    )
    runtime.collection.runs.record_error(
        run_id,
        stage="download_documents",
        error="Downloaded file is empty",
        retryable=False,
        session_key="2026R1",
        bill_id_compact="SB1501",
        source_entity_type="CommitteePublicTestimony",
        source_id="255890",
        source_url=error_url,
    )

    relative_path = "2026R1/SB1501/public_testimony/244133/Evidence.pdf"
    local_file = runtime.config.archive_root.joinpath(*relative_path.split("/"))
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(PDF_BYTES)
    runtime.storage.complete_document_download(
        document_id,
        local_relative_path=relative_path,
        downloaded_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
        local_filename=local_file.name,
        remote_filename=local_file.name,
        advertised_bytes=len(PDF_BYTES),
        validation_status="valid",
        http_status=200,
    )

    client = web_app.test_client()
    for url in (f"/bills/{bill_id}", f"/documents/{document_id}", f"/runs/{run_id}"):
        response = client.get(url)
        assert response.status_code == 200, response.get_data(as_text=True)

    run_page = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "CommitteePublicTestimony #255890" in run_page
    assert error_url in run_page

    downloaded = client.get(f"/documents/{document_id}/file")
    assert downloaded.status_code == 200
    assert downloaded.data == PDF_BYTES
    assert downloaded.mimetype == "application/pdf"
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"
    assert downloaded.headers["Content-Security-Policy"] == "sandbox"


def test_registered_file_route_blocks_traversal_and_missing_paths(web_app, tmp_path: Path):
    runtime = web_app.extensions["legiview"]["runtime"]
    bill_id = _seed_bill(runtime)
    traversal_id = _seed_document(runtime, bill_id, "2", "Traversal fixture")
    missing_id = _seed_document(runtime, bill_id, "3", "Missing fixture")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF_BYTES)
    runtime.storage.update_document_download_state(
        traversal_id,
        "downloaded",
        local_relative_path="../outside.pdf",
        downloaded_bytes=len(PDF_BYTES),
        sha256=sha256(PDF_BYTES).hexdigest(),
        validation_status="valid",
    )
    runtime.storage.update_document_download_state(
        missing_id,
        "downloaded",
        local_relative_path="2026R1/SB1501/public_testimony/3/missing.pdf",
        downloaded_bytes=len(PDF_BYTES),
        sha256=sha256(PDF_BYTES).hexdigest(),
        validation_status="valid",
    )

    client = web_app.test_client()
    assert client.get(f"/documents/{traversal_id}/file").status_code == 404
    assert client.get(f"/documents/{missing_id}/file").status_code == 404
    assert outside.read_bytes() == PDF_BYTES


def test_registered_file_route_revalidates_bytes_hash_mime_and_filename(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    bill_id = _seed_bill(runtime)
    document_id = _seed_document(runtime, bill_id, "244133", "Validation fixture")
    relative_path = "2026R1/SB1501/public_testimony/244133/Evidence.pdf"
    local_file = runtime.config.archive_root.joinpath(*relative_path.split("/"))
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(PDF_BYTES)
    digest = sha256(PDF_BYTES).hexdigest()
    runtime.storage.complete_document_download(
        document_id,
        sha256=digest,
        local_relative_path=relative_path,
        downloaded_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
        local_filename=local_file.name,
        remote_filename=local_file.name,
        validation_status="valid",
        http_status=200,
    )
    client = web_app.test_client()
    file_url = f"/documents/{document_id}/file"
    assert client.get(file_url).status_code == 200

    same_size_replacement = PDF_BYTES.replace(b"Catalog", b"Corrupt")
    assert len(same_size_replacement) == len(PDF_BYTES)
    local_file.write_bytes(same_size_replacement)
    assert client.get(file_url).status_code == 404

    local_file.write_bytes(PDF_BYTES)
    runtime.storage.update_document_download_state(
        document_id,
        "downloaded",
        local_filename="Different.pdf",
    )
    assert client.get(file_url).status_code == 404

    runtime.storage.update_document_download_state(
        document_id,
        "downloaded",
        local_filename=local_file.name,
        mime_type="text/plain",
    )
    assert client.get(file_url).status_code == 404

    runtime.storage.update_document_download_state(
        document_id,
        "downloaded",
        mime_type="application/pdf",
        downloaded_bytes=len(PDF_BYTES) + 1,
    )
    assert client.get(file_url).status_code == 404

    runtime.storage.update_document_download_state(
        document_id,
        "downloaded",
        downloaded_bytes=len(PDF_BYTES),
        sha256="0" * 64,
    )
    assert client.get(file_url).status_code == 404


def test_registered_file_route_rejects_in_root_symlink_when_supported(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    bill_id = _seed_bill(runtime)
    document_id = _seed_document(runtime, bill_id, "244133", "Symlink fixture")
    relative_path = "2026R1/SB1501/public_testimony/244133/Evidence.pdf"
    linked_file = runtime.config.archive_root.joinpath(*relative_path.split("/"))
    target_file = (
        runtime.config.archive_root
        / "2026R1"
        / "SB1501"
        / "public_testimony"
        / "target"
        / "Evidence.pdf"
    )
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(PDF_BYTES)
    linked_file.parent.mkdir(parents=True)
    try:
        linked_file.symlink_to(target_file)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"File symlinks are unavailable: {exc}")
    runtime.storage.complete_document_download(
        document_id,
        sha256=sha256(PDF_BYTES).hexdigest(),
        local_relative_path=relative_path,
        downloaded_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
        local_filename=linked_file.name,
        remote_filename=linked_file.name,
        validation_status="valid",
        http_status=200,
    )

    assert linked_file.resolve() == target_file.resolve()
    assert target_file.is_relative_to(runtime.config.archive_root)
    assert web_app.test_client().get(f"/documents/{document_id}/file").status_code == 404


def test_cli_help_and_show_bill_use_the_shared_runtime(tmp_path: Path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "collect-bill" in capsys.readouterr().out

    database_path = tmp_path / "cli.sqlite3"
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LEGIVIEW_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("LEGIVIEW_ARCHIVE_ROOT", str(archive_root))
    runtime = build_runtime(clean_parts=False)
    bill_id = _seed_bill(runtime)
    _seed_document(runtime, bill_id, "244133", "CLI testimony")

    assert cli.main(["show-bill", "2026r1", "sb 1501"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["bill"]["bill_id_compact"] == "SB1501"
    assert output["documents"][0]["source_id"] == "244133"

    assert cli.main(["show-bill", "2026r1", "hb 9999"]) == 1
    assert "No stored measure found" in capsys.readouterr().err


def test_cli_serve_passes_host_and_port_to_the_app_factory(monkeypatch):
    from olis_archive import web as web_module

    captured: dict[str, object] = {}

    class FakeApp:
        extensions = {
            "legiview": {
                "runtime": SimpleNamespace(
                    config=SimpleNamespace(host="localhost", port=5099, debug=False)
                )
            }
        }

        @staticmethod
        def run(**values):
            captured["run"] = values

    def fake_create_app(overrides):
        captured["overrides"] = overrides
        return FakeApp()

    monkeypatch.setattr(web_module, "create_app", fake_create_app)

    assert cli.main(["serve", "--host", "localhost", "--port", "5099"]) == 0
    assert captured["overrides"] == {
        "START_WORKER": True,
        "HOST": "localhost",
        "PORT": 5099,
    }
    assert captured["run"] == {
        "host": "localhost",
        "port": 5099,
        "debug": False,
        "use_reloader": False,
    }


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("completed", 0), ("completed_with_errors", 1), ("failed", 1)],
)
def test_cli_exit_status_distinguishes_clean_and_partial_runs(status, expected_exit, capsys):
    class FakeRuns:
        @staticmethod
        def get_run(run_id):
            return {"id": run_id, "status": status, "error_count": int(status != "completed")}

    class FakeCollection:
        runs = FakeRuns()

        @staticmethod
        def execute_run(run_id):
            return status

    assert cli._execute_and_print(FakeCollection(), 42) == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    "arguments",
    (
        ["collect-measure", "2007R1", "HB2001"],
        ["collect-session", "2007R1"],
    ),
)
def test_direct_collection_cli_reports_boundary_rejection_without_traceback(
    arguments, monkeypatch, capsys
):
    class FakeCollection:
        @staticmethod
        def create_collect_bill_run(*_args, **_kwargs):
            raise ValueError(
                "Session 2007R1 predates LegiView's validated support boundary 2014R1"
            )

        @staticmethod
        def create_collect_session_run(*_args, **_kwargs):
            raise ValueError(
                "Session 2007R1 predates LegiView's validated support boundary 2014R1"
            )

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert "2007R1" in captured.err
    assert "support boundary 2014R1" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_resume_cli_reports_frozen_scope_rejection_without_requeue(
    monkeypatch, capsys
):
    class FakeCollection:
        @staticmethod
        def requeue_run(run_id):
            assert run_id == 73
            raise ValueError(
                "Session 2007R1 predates LegiView's validated support boundary 2014R1"
            )

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main(["resume-run", "73"]) == 2
    captured = capsys.readouterr()
    assert "Run #73 cannot be resumed" in captured.err
    assert "support boundary 2014R1" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_legacy_retry_snapshot_rejects_historical_bulk_runs(web_app):
    runtime = web_app.extensions["legiview"]["runtime"]
    _seed_bill(runtime)
    source_run = runtime.collection.runs.create_run(
        "inventory_backfill",
        session_keys=["2026R1"],
        scope={"session_keys": ["2026R1"]},
    )

    with pytest.raises(ValueError, match="Historical archive retries"):
        runtime.collection.create_retry_failures_run(source_run)


def test_download_archive_cli_prints_preflight_and_lower_bound_warning(
    monkeypatch, capsys
):
    class FakePreflight:
        known_bytes_fit = True
        unknown_size_pending = 2

        @staticmethod
        def as_dict():
            return {"documents_in_scope": 3, "unknown_size_pending": 2}

    class FakeRuns:
        @staticmethod
        def get_run(run_id):
            return {"id": run_id, "status": "completed"}

    class FakeCollection:
        runs = FakeRuns()

        @staticmethod
        def download_archive_preflight(*_args, **_kwargs):
            return FakePreflight()

        @staticmethod
        def create_download_archive_run(*_args, **_kwargs):
            return 91

        @staticmethod
        def execute_run(_run_id):
            return "completed"

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main(["download-archive", "--session", "2026R1"]) == 0
    captured = capsys.readouterr()
    assert "Download Archive preflight" in captured.err
    assert "lower bound" in captured.err
    assert json.loads(captured.out)["run_id"] == 91


@pytest.mark.parametrize("command", ["archive-preflight", "download-archive"])
def test_download_archive_cli_rejects_uninventoried_explicit_scope(
    command, monkeypatch, capsys
):
    class FakeCollection:
        @staticmethod
        def download_archive_preflight(*_args, **_kwargs):
            raise ValueError(
                "Session selection has not been inventoried: 2026R1"
            )

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main([command, "--session", "2026R1"]) == 2
    captured = capsys.readouterr()
    assert "has not been inventoried: 2026R1" in captured.err
    assert captured.out == ""


def test_whole_history_cli_prints_acceptable_use_window_reminder(monkeypatch, capsys):
    class FakeRuns:
        @staticmethod
        def get_run(run_id):
            return {"id": run_id, "status": "completed"}

    class FakeCollection:
        runs = FakeRuns()

        @staticmethod
        def create_inventory_backfill_run(*_args, **_kwargs):
            return 92

        @staticmethod
        def execute_run(_run_id):
            return "completed"

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main(["inventory-backfill"]) == 0
    captured = capsys.readouterr()
    assert "5:00 p.m.–6:00 a.m. Pacific" in captured.err
    assert "once per day" in captured.err


@pytest.mark.parametrize(
    "creation_error",
    [
        ValueError("Selected sessions are outside the resolved historical scope: 2099R1"),
        ODataError(
            "OData session discovery failed",
            url="https://api.oregonlegislature.gov/odata/odataservice.svc/LegislativeSessions",
            retryable=True,
        ),
    ],
)
def test_inventory_backfill_cli_reports_creation_errors_cleanly(
    creation_error, monkeypatch, capsys
):
    class FakeCollection:
        @staticmethod
        def create_inventory_backfill_run(*_args, **_kwargs):
            raise creation_error

    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(collection=FakeCollection()),
    )

    assert cli.main(["inventory-backfill", "--session", "2099R1"]) == 2
    captured = capsys.readouterr()
    assert "Inventory Backfill could not be created" in captured.err
    assert str(creation_error) in captured.err
    assert captured.out == ""


def test_startup_normalizes_active_states_and_removes_parts(tmp_path: Path):
    config = AppConfig(
        database_path=tmp_path / "recovery.sqlite3",
        archive_root=tmp_path / "archive",
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )
    first = build_runtime(
        config,
        normalize_interrupted=False,
        clean_parts=False,
        exclusive=True,
    )
    assert (config.archive_root / ARCHIVE_OWNERSHIP_MARKER).is_file()
    bill_id = _seed_bill(first)
    document_id = _seed_document(first, bill_id, "244133", "Interrupted testimony")
    running_id = first.collection.create_collect_bill_run("2026R1", "SB1501")
    queued_id = first.collection.create_collect_bill_run("2026R1", "SB1501")
    assert first.collection.runs.claim_run(running_id)
    first.collection.runs.begin_stage(running_id, "download_documents", "Downloading")
    assert first.storage.claim_document(document_id)
    version_id = first.storage.create_document_version(
        document_id,
        status="downloading",
        local_relative_path="2026R1/SB1501/public_testimony/244133/Evidence.pdf",
    )
    staged = config.archive_root / "2026R1" / "SB1501" / "public_testimony" / "244133" / "Evidence.pdf.part"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(PDF_BYTES[:20])
    assert first.instance_lock is not None
    first.instance_lock.close()

    recovered = build_runtime(config)

    assert recovered.collection.runs.get_run(running_id)["status"] == "interrupted"
    assert recovered.collection.runs.get_run(queued_id)["status"] == "queued"
    assert recovered.collection.runs.run_items(running_id)[0]["status"] == "interrupted"
    assert recovered.storage.get_document(document_id)["download_status"] == "interrupted"
    versions = recovered.storage.list_document_versions(document_id)
    assert next(row for row in versions if row["id"] == version_id)["status"] == "interrupted"
    assert not staged.exists()


def test_startup_rejects_unowned_nonempty_root_without_removing_parts(tmp_path: Path):
    archive = tmp_path / "Documents"
    archive.mkdir()
    unrelated = archive / "unrelated.part"
    unrelated.write_bytes(b"must survive")
    config = AppConfig(
        database_path=tmp_path / "unowned.sqlite3",
        archive_root=archive,
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )

    with pytest.raises(UnsafeArchivePath, match="not owned by LegiView"):
        build_runtime(config)

    assert unrelated.read_bytes() == b"must survive"
    assert not (archive / ARCHIVE_OWNERSHIP_MARKER).exists()


def test_read_only_runtime_does_not_create_archive_or_ownership_marker(tmp_path: Path):
    database_path = tmp_path / "readonly.sqlite3"
    Database(database_path).initialize()
    archive = tmp_path / "not-created"
    config = AppConfig(
        database_path=database_path,
        archive_root=archive,
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )

    build_runtime(config, normalize_interrupted=False, clean_parts=False)

    assert not archive.exists()


def test_exclusive_runtime_lock_blocks_a_second_mutator_but_allows_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = AppConfig(
        database_path=tmp_path / "locked.sqlite3",
        archive_root=tmp_path / "archive",
        minimum_free_space_bytes=0,
        inter_request_delay=0,
    )
    owner = build_runtime(
        config,
        normalize_interrupted=False,
        clean_parts=False,
        exclusive=True,
    )
    try:
        initialize_calls: list[str] = []
        original_initialize = Database.initialize

        def tracked_initialize(database: Database) -> int:
            initialize_calls.append(database.path)
            return original_initialize(database)

        monkeypatch.setattr(Database, "initialize", tracked_initialize)
        with pytest.raises(InstanceAlreadyRunning, match="Another LegiView process"):
            build_runtime(
                config,
                normalize_interrupted=False,
                clean_parts=False,
                exclusive=True,
            )
        assert initialize_calls == []

        reader = build_runtime(
            config,
            normalize_interrupted=False,
            clean_parts=False,
            exclusive=False,
        )
        assert initialize_calls == []
        assert reader.database.schema_version() == 6

        monkeypatch.setattr(
            Database, "migration_manifest_is_current", lambda _database: False
        )
        with pytest.raises(InstanceAlreadyRunning, match="Another LegiView process"):
            build_runtime(
                config,
                normalize_interrupted=False,
                clean_parts=False,
                exclusive=False,
            )
        assert initialize_calls == []
    finally:
        assert owner.instance_lock is not None
        owner.instance_lock.close()


def test_app_shutdown_releases_instance_lock_only_after_worker_quiescence(web_app):
    state = web_app.extensions["legiview"]
    original_runtime = state["runtime"]
    original_workers = state["workers"]
    events: list[str] = []

    class FakeWorkers:
        quiesced = False

        def stop(self, *, wait, timeout):
            assert wait is True
            assert timeout == 30
            events.append("stop")
            return self.quiesced

    class FakeLock:
        def close(self):
            events.append("close")

    fake_workers = FakeWorkers()
    state["workers"] = fake_workers
    state["runtime"] = SimpleNamespace(instance_lock=FakeLock())
    try:
        assert state["shutdown"]() is False
        assert events == ["stop"]

        fake_workers.quiesced = True
        assert state["shutdown"]() is True
        assert events == ["stop", "stop", "close"]
    finally:
        state["runtime"] = original_runtime
        state["workers"] = original_workers
