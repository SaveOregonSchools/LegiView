from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from olis_archive.services.downloads import (
    ContentLengthMismatch,
    DestinationConflict,
    DiskReservationManager,
    DownloadValidationError,
    Downloader,
    LowDiskSpace,
    RetryPolicy,
    SafeHTTPClient,
    UnsafeDownloadTarget,
)
from olis_archive.services.recovery import (
    cleanup_stale_parts,
    recover_incomplete_download,
    validate_completed_download,
)


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@pytest.fixture
def download_server():
    counters: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            counters[path] = counters.get(path, 0) + 1
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.end_headers()
                return
            if path == "/bad-redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/file")
                self.end_headers()
                return
            if path == "/rate" and counters[path] == 1:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.end_headers()
                return
            if path == "/flaky" and counters[path] == 1:
                self.send_response(503)
                self.end_headers()
                return
            if path == "/partial":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(PDF_BYTES) + 20))
                self.end_headers()
                self.wfile.write(PDF_BYTES[:20])
                return
            if path == "/html":
                body = b"<html><title>Access denied</title></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/garbage":
                body = b"\x00\x01\x02\x03not-an-archive-document"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/fake-png":
                body = b"this is not a PNG image"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/octet":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(PDF_BYTES)))
                self.send_header("Content-Disposition", 'attachment; filename="Session 2. Final"')
                self.end_headers()
                self.wfile.write(PDF_BYTES)
                return
            if path in {"/file", "/rate", "/flaky"}:
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(PDF_BYTES)))
                self.send_header("Content-Disposition", 'attachment; filename="Evidence.pdf"')
                self.send_header("ETag", '"fixture-etag"')
                self.send_header("Last-Modified", "Wed, 02 Sep 2026 12:00:00 GMT")
                self.end_headers()
                self.wfile.write(PDF_BYTES)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counters
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _downloader(**overrides) -> Downloader:
    settings = {
        "allowed_hosts": {"127.0.0.1"},
        "allow_private_network": True,
        "minimum_free_space_bytes": 0,
        "chunk_size": 7,
    }
    settings.update(overrides)
    return Downloader(**settings)


def test_streamed_download_hashes_validates_and_atomically_promotes(download_server, tmp_path: Path):
    base_url, _counters = download_server
    result = _downloader().download_to_directory(
        f"{base_url}/file",
        tmp_path,
        archive_root=tmp_path,
        expected_mime_type="application/pdf",
    )

    assert result.path == tmp_path / "Evidence.pdf"
    assert result.path.read_bytes() == PDF_BYTES
    assert result.sha256 == sha256(PDF_BYTES).hexdigest()
    assert result.byte_count == result.expected_length == len(PDF_BYTES)
    assert result.declared_mime_type == result.detected_mime_type == "application/pdf"
    assert result.etag == '"fixture-etag"'
    assert result.last_modified == "Wed, 02 Sep 2026 12:00:00 GMT"
    assert result.validation.valid
    assert not list(tmp_path.rglob("*.part"))


def test_manual_redirect_is_recorded_and_revalidated(download_server, tmp_path: Path):
    base_url, counters = download_server
    result = _downloader().download_to_path(
        f"{base_url}/redirect",
        tmp_path / "redirected.pdf",
        archive_root=tmp_path,
        expected_mime_type="application/pdf",
    )

    assert result.final_url == f"{base_url}/file"
    assert result.redirects == (f"{base_url}/file",)
    assert counters == {"/redirect": 1, "/file": 1}


@pytest.mark.parametrize(("endpoint", "expected_delay"), [("rate", 1), ("flaky", 0)])
def test_retryable_http_failures_are_bounded_and_retried(
    endpoint: str,
    expected_delay: float,
    download_server,
    tmp_path: Path,
):
    base_url, counters = download_server
    delays: list[float] = []
    result = _downloader().download_to_path_with_retries(
        f"{base_url}/{endpoint}",
        tmp_path / f"{endpoint}.pdf",
        archive_root=tmp_path,
        expected_mime_type="application/pdf",
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, maximum_delay_seconds=0),
        sleeper=delays.append,
    )

    assert result.validation.valid
    assert counters[f"/{endpoint}"] == 2
    assert delays == [expected_delay]


def test_redirect_to_non_allowlisted_host_is_blocked(download_server, tmp_path: Path):
    base_url, counters = download_server
    with pytest.raises(UnsafeDownloadTarget, match="allowlist"):
        _downloader().download_to_path(
            f"{base_url}/bad-redirect",
            tmp_path / "blocked.pdf",
            archive_root=tmp_path,
        )
    assert counters == {"/bad-redirect": 1}


def test_interrupted_response_is_retryable_and_part_is_removed(download_server, tmp_path: Path):
    base_url, _counters = download_server
    with pytest.raises(ContentLengthMismatch) as raised:
        _downloader().download_to_path(
            f"{base_url}/partial",
            tmp_path / "partial.pdf",
            archive_root=tmp_path,
            expected_mime_type="application/pdf",
        )

    assert raised.value.retryable
    assert not (tmp_path / "partial.pdf").exists()
    assert not (tmp_path / "partial.pdf.part").exists()


def test_html_access_page_is_never_accepted_as_a_document(download_server, tmp_path: Path):
    base_url, _counters = download_server
    with pytest.raises(DownloadValidationError, match="HTML"):
        _downloader().download_to_path(
            f"{base_url}/html",
            tmp_path / "should-be.pdf",
            archive_root=tmp_path,
            expected_mime_type="application/pdf",
        )
    assert not list(tmp_path.rglob("*.part"))
    assert not (tmp_path / "should-be.pdf").exists()


@pytest.mark.parametrize(
    ("endpoint", "filename"),
    [("garbage", "garbage"), ("fake-png", "fake.png")],
)
def test_unsigned_or_declared_only_bytes_are_not_accepted(
    endpoint: str,
    filename: str,
    download_server,
    tmp_path: Path,
):
    base_url, _counters = download_server
    destination = tmp_path / filename

    with pytest.raises(DownloadValidationError, match="recognized archival document signature"):
        _downloader().download_to_path(
            f"{base_url}/{endpoint}",
            destination,
            archive_root=tmp_path,
        )

    assert not destination.exists()
    assert not list(tmp_path.rglob("*.part"))


def test_same_remote_filename_gets_deterministic_non_overwriting_version(download_server, tmp_path: Path):
    base_url, _counters = download_server
    downloader = _downloader()
    first = downloader.download_to_directory(f"{base_url}/file", tmp_path, archive_root=tmp_path)
    second = downloader.download_to_directory(f"{base_url}/file", tmp_path, archive_root=tmp_path)

    assert first.path.name == "Evidence.pdf"
    assert second.path.name == "Evidence__v0002.pdf"
    assert first.path.read_bytes() == second.path.read_bytes() == PDF_BYTES


def test_strong_signature_appends_safe_extension_to_ambiguous_remote_name(download_server, tmp_path: Path):
    base_url, _counters = download_server
    result = _downloader().download_to_directory(
        f"{base_url}/octet",
        tmp_path,
        archive_root=tmp_path,
    )
    assert result.path.name == "Session 2. Final.pdf"
    assert result.detected_mime_type == "application/pdf"


def test_existing_part_is_not_deleted_by_a_competing_claim(download_server, tmp_path: Path):
    base_url, _counters = download_server
    part = tmp_path / "claimed.pdf.part"
    part.write_bytes(b"owned by another worker")

    with pytest.raises(DestinationConflict) as raised:
        _downloader().download_to_path(
            f"{base_url}/file",
            tmp_path / "claimed.pdf",
            archive_root=tmp_path,
        )

    assert raised.value.retryable
    assert part.read_bytes() == b"owned by another worker"


def test_completed_file_is_skipped_only_after_local_validation(download_server, tmp_path: Path):
    base_url, counters = download_server
    destination = tmp_path / "stable.pdf"
    downloader = _downloader()
    first = downloader.download_to_path(
        f"{base_url}/file",
        destination,
        archive_root=tmp_path,
        expected_mime_type="application/pdf",
    )
    second = downloader.download_to_path(
        f"{base_url}/file",
        destination,
        archive_root=tmp_path,
        expected_mime_type="application/pdf",
        expected_length=first.byte_count,
        expected_sha256=first.sha256,
    )

    assert second.skipped
    assert counters["/file"] == 1


def test_existing_destination_requires_allowed_source_and_stored_hash(
    download_server,
    tmp_path: Path,
):
    base_url, counters = download_server
    destination = tmp_path / "preplaced.pdf"
    destination.write_bytes(PDF_BYTES)
    downloader = _downloader()

    with pytest.raises(UnsafeDownloadTarget, match="allowlist"):
        downloader.download_to_path(
            "https://example.com/document.pdf",
            destination,
            archive_root=tmp_path,
            expected_sha256=sha256(PDF_BYTES).hexdigest(),
        )

    with pytest.raises(DestinationConflict, match="no stored SHA-256"):
        downloader.download_to_path(
            f"{base_url}/file",
            destination,
            archive_root=tmp_path,
            expected_mime_type="application/pdf",
            expected_length=len(PDF_BYTES),
        )

    assert counters == {}
    assert destination.read_bytes() == PDF_BYTES


def test_low_space_floor_stops_before_part_creation(download_server, tmp_path: Path, monkeypatch):
    base_url, _counters = download_server
    monkeypatch.setattr(
        "olis_archive.services.downloads.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=95, free=5),
    )
    with pytest.raises(LowDiskSpace):
        _downloader(minimum_free_space_bytes=1).download_to_path(
            f"{base_url}/file",
            tmp_path / "no-space.pdf",
            archive_root=tmp_path,
        )
    assert not list(tmp_path.rglob("*.part"))


def test_unknown_length_reservation_counts_active_writes_only_once(tmp_path: Path, monkeypatch):
    consumed = {"bytes": 0}
    monkeypatch.setattr(
        "olis_archive.services.downloads._free_bytes",
        lambda _path: 200 - consumed["bytes"],
    )
    reservations = DiskReservationManager(minimum_reservation_bytes=0)

    with reservations.reserve(tmp_path, 0, 40) as update:
        for next_count in (40, 80, 120, 150):
            update(next_count, written_bytes=consumed["bytes"])
            consumed["bytes"] = next_count
            update(next_count, written_bytes=consumed["bytes"])

        # 150 written bytes leave 50 free, so another 20 bytes would cross the
        # configured floor of 40 and must still be rejected.
        with pytest.raises(LowDiskSpace):
            update(170, written_bytes=consumed["bytes"])

    assert consumed["bytes"] == 150
    assert reservations._reserved == {}
    assert reservations._written == {}


def test_expected_host_allowlist_blocks_other_hosts_before_opening():
    client = SafeHTTPClient(
        allowed_hosts={"olis.oregonlegislature.gov"},
        resolver=lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    with pytest.raises(UnsafeDownloadTarget, match="allowlist"):
        client.validate_target("https://example.com/document.pdf")


def test_recovery_adopts_only_provably_complete_part_and_discards_partial(tmp_path: Path):
    final = tmp_path / "2026R1" / "SB1501" / "public_testimony" / "1" / "Evidence.pdf"
    final.parent.mkdir(parents=True)
    part = final.with_name(f"{final.name}.part")
    part.write_bytes(PDF_BYTES)
    relative = final.relative_to(tmp_path).as_posix()

    adopted = recover_incomplete_download(
        tmp_path,
        relative,
        expected_bytes=len(PDF_BYTES),
        expected_sha256=sha256(PDF_BYTES).hexdigest(),
        mime_type="application/pdf",
    )
    assert adopted.action == "adopted_part"
    assert final.read_bytes() == PDF_BYTES
    assert not part.exists()
    assert validate_completed_download(
        tmp_path,
        relative,
        expected_bytes=len(PDF_BYTES),
        expected_sha256=adopted.sha256,
        mime_type="application/pdf",
    ).adopted

    other = final.with_name("Other.pdf")
    other_part = other.with_name(f"{other.name}.part")
    other_part.write_bytes(PDF_BYTES[:10])
    discarded = recover_incomplete_download(
        tmp_path,
        other.relative_to(tmp_path).as_posix(),
        expected_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
    )
    assert discarded.action == "discarded_part"
    assert discarded.needs_retry
    assert not other_part.exists() and not other.exists()


def test_stale_part_cleanup_stays_within_archive_and_can_preserve_active_parts(tmp_path: Path):
    stale = tmp_path / "one.pdf.part"
    active = tmp_path / "two.pdf.part"
    stale.write_bytes(b"stale")
    active.write_bytes(b"active")

    removed = cleanup_stale_parts(tmp_path, keep_relative_paths={"two.pdf.part"})

    assert removed == ["one.pdf.part"]
    assert not stale.exists()
    assert active.exists()


def test_recovery_unlinks_a_part_symlink_without_touching_its_target(tmp_path: Path):
    archive = tmp_path / "archive"
    final = archive / "2026R1" / "SB1501" / "public_testimony" / "1" / "Evidence.pdf"
    final.parent.mkdir(parents=True)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF_BYTES)
    part = final.with_name(f"{final.name}.part")
    try:
        part.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks are not available for this test user")

    recovered = recover_incomplete_download(
        archive,
        final.relative_to(archive).as_posix(),
        expected_bytes=len(PDF_BYTES),
        expected_sha256=sha256(PDF_BYTES).hexdigest(),
        mime_type="application/pdf",
    )

    assert recovered.action == "discarded_part"
    assert not part.exists()
    assert outside.read_bytes() == PDF_BYTES
