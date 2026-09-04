from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from olis_archive.services.downloads import DownloadError, NetworkDownloadError, SafeHTTPClient
from olis_archive.services.probes import RemoteSizeProbe


@pytest.fixture
def probe_server():
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_HEAD(self):  # noqa: N802
            requests.append(("HEAD", self.path))
            if self.path == "/known":
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", "12345")
                self.send_header("ETag", '"probe-etag"')
                self.end_headers()
                return
            if self.path in {"/range", "/unknown"}:
                self.send_response(405 if self.path == "/range" else 200)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            requests.append(("GET", self.path))
            if self.path == "/range":
                self.send_response(206)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", "1")
                self.send_header("Content-Range", "bytes 0-0/98765")
                self.end_headers()
                self.wfile.write(b"x")
                return
            if self.path == "/unknown":
                self.send_response(206)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _probe() -> RemoteSizeProbe:
    return RemoteSizeProbe(
        SafeHTTPClient(
            allowed_hosts={"127.0.0.1"},
            allow_private_network=True,
            timeout_seconds=2,
        ),
        sleep=lambda _seconds: None,
    )


def test_probe_prefers_head_when_content_length_is_known(probe_server):
    base_url, requests = probe_server

    result = _probe().probe(f"{base_url}/known")

    assert result.status == "known"
    assert result.method == "HEAD"
    assert result.content_length == 12345
    assert result.content_type == "application/pdf"
    assert result.etag == '"probe-etag"'
    assert requests == [("HEAD", "/known")]


def test_probe_uses_one_byte_range_when_head_is_rejected(probe_server):
    base_url, requests = probe_server

    result = _probe().probe(f"{base_url}/range")

    assert result.status == "known"
    assert result.method == "RANGE_GET"
    assert result.content_length == 98765
    assert requests == [("HEAD", "/range"), ("GET", "/range")]


def test_probe_does_not_mistake_partial_response_length_for_total(probe_server):
    base_url, requests = probe_server

    result = _probe().probe(f"{base_url}/unknown")

    assert result.status == "unknown"
    assert result.method == "RANGE_GET"
    assert result.content_length is None
    assert requests == [("HEAD", "/unknown"), ("GET", "/unknown")]


def test_probe_retry_wait_is_cooperatively_interruptible(monkeypatch):
    probe = _probe()
    stopped = {"value": False}
    sleeps: list[float] = []

    def fail_head(_url: str):
        raise NetworkDownloadError("temporary failure", retryable=True)

    def stop_after_first_slice(seconds: float) -> None:
        sleeps.append(seconds)
        stopped["value"] = True

    monkeypatch.setattr(probe, "_head_once", fail_head)
    probe.sleep = stop_after_first_slice

    with pytest.raises(DownloadError, match="interrupted by run control"):
        probe.probe(
            "https://olis.oregonlegislature.gov/document/1",
            cancellation_requested=lambda: stopped["value"],
        )

    assert sleeps == [0.25]
