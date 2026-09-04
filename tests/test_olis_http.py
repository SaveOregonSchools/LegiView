import http.client
from urllib.error import HTTPError

import pytest

from olis_archive.services.olis_http import (
    DEFAULT_MAXIMUM_RESPONSE_BYTES,
    OLISHTTPClient,
    OLISHTTPError,
)


class FakeResponse:
    def __init__(
        self,
        url,
        payload=b"<html></html>",
        *,
        status=200,
        headers=None,
        read_error=None,
    ):
        self._url = url
        self._payload = payload
        self.status = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.read_error = read_error
        self.closed = False
        self.read_limits = []

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        self.read_limits.append(limit)
        if self.read_error is not None:
            raise self.read_error
        return self._payload if limit < 0 else self._payload[:limit]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_olis_retry_wait_is_cooperatively_interrupted():
    sleeps = []
    stop = {"requested": False}
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    opener = FakeOpener([HTTPError(url, 503, "busy", {"Retry-After": "10"}, None)])

    def sleep(seconds):
        sleeps.append(seconds)
        stop["requested"] = True

    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=3,
        opener=opener,
        sleep=sleep,
    )

    with pytest.raises(OLISHTTPError, match="interrupted by run control"):
        client.get_testimony_page(
            "2026R1",
            "SB1501",
            cancellation_requested=lambda: stop["requested"],
        )

    assert len(opener.calls) == 1
    assert sleeps == [0.25]


def test_olis_retry_wait_keeps_one_shot_sleep_without_callback():
    sleeps = []
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    opener = FakeOpener(
        [
            HTTPError(url, 503, "busy", {}, None),
            HTTPError(url, 503, "busy", {}, None),
        ]
    )
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=2,
        opener=opener,
        sleep=sleeps.append,
    )

    with pytest.raises(OLISHTTPError, match="HTTP 503"):
        client.get_testimony_page("2026R1", "SB1501")

    assert len(opener.calls) == 2
    assert sleeps == [1.0]


def test_olis_redirect_is_validated_before_following():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    opener = FakeOpener(
        [HTTPError(url, 302, "redirect", {"Location": "https://example.com/stolen"}, None)]
    )
    client = OLISHTTPClient(inter_request_delay=0, max_attempts=1, opener=opener)

    with pytest.raises(OLISHTTPError, match="outside the configured"):
        client.get(url)

    assert [call[0] for call in opener.calls] == [url]


def test_olis_follows_only_a_validated_relative_redirect():
    source = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    target = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1502"
    response = FakeResponse(target, b"<html>ok</html>")
    opener = FakeOpener(
        [HTTPError(source, 302, "redirect", {"Location": "SB1502"}, None), response]
    )
    client = OLISHTTPClient(inter_request_delay=0, max_attempts=1, opener=opener)

    result = client.get(source)

    assert result.url == target
    assert [call[0] for call in opener.calls] == [source, target]
    assert response.closed


@pytest.mark.parametrize(
    "url",
    [
        "https://user@olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501",
        "https://olis.oregonlegislature.gov:8443/liz/2026R1/Measures/Testimony/SB1501",
        "https://olis.oregonlegislature.gov/liz/%2e%2e/private",
    ],
)
def test_olis_rejects_noncanonical_origin_and_path(url):
    opener = FakeOpener([])
    client = OLISHTTPClient(inter_request_delay=0, max_attempts=1, opener=opener)

    with pytest.raises(OLISHTTPError):
        client.get(url)

    assert opener.calls == []


def test_olis_rejects_oversized_content_length_before_reading():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    response = FakeResponse(
        url,
        headers={"Content-Type": "text/html", "Content-Length": "11"},
    )
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
        opener=FakeOpener([response]),
    )

    with pytest.raises(OLISHTTPError, match="safety limit"):
        client.get(url)

    assert response.read_limits == []
    assert response.closed


def test_default_olis_body_limit_covers_large_testimony_pages_but_stays_bounded():
    url = "https://olis.oregonlegislature.gov/liz/2025R1/Measures/Testimony/SB210"
    payload = b"x" * ((8 * 1024 * 1024) + 1)
    accepted = FakeResponse(
        url,
        payload,
        headers={"Content-Type": "text/html", "Content-Length": str(len(payload))},
    )
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        opener=FakeOpener([accepted]),
    )

    assert DEFAULT_MAXIMUM_RESPONSE_BYTES == 32 * 1024 * 1024
    assert len(client.get(url).text) == len(payload)
    assert accepted.read_limits == [DEFAULT_MAXIMUM_RESPONSE_BYTES + 1]
    assert accepted.closed

    oversized = FakeResponse(
        url,
        headers={
            "Content-Type": "text/html",
            "Content-Length": str(DEFAULT_MAXIMUM_RESPONSE_BYTES + 1),
        },
    )
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        opener=FakeOpener([oversized]),
    )

    with pytest.raises(OLISHTTPError, match="safety limit"):
        client.get(url)

    assert oversized.read_limits == []
    assert oversized.closed


def test_olis_accepts_a_response_exactly_at_the_body_limit():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    response = FakeResponse(
        url,
        b"x" * 10,
        headers={"Content-Type": "text/html", "Content-Length": "10"},
    )
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
        opener=FakeOpener([response]),
    )

    result = client.get(url)

    assert result.text == "x" * 10
    assert response.read_limits == [11]
    assert response.closed


def test_olis_rejects_oversized_stream_without_content_length():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    response = FakeResponse(url, b"x" * 11)
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
        opener=FakeOpener([response]),
    )

    with pytest.raises(OLISHTTPError, match="safety limit"):
        client.get(url)

    assert response.read_limits == [11]
    assert response.closed


def test_olis_retries_incomplete_response_and_closes_each_stream():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    incomplete = FakeResponse(
        url,
        headers={"Content-Type": "text/html", "Content-Length": "20"},
        read_error=http.client.IncompleteRead(b"<html>", 14),
    )
    completed = FakeResponse(url, b"<html>ok</html>")
    sleeps = []
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=2,
        opener=FakeOpener([incomplete, completed]),
        sleep=sleeps.append,
    )

    result = client.get(url)

    assert result.text == "<html>ok</html>"
    assert sleeps == [1.0]
    assert incomplete.closed
    assert completed.closed


def test_olis_closes_an_unexpected_auto_follow_response():
    requested = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
    response = FakeResponse("https://example.com/stolen")
    client = OLISHTTPClient(
        inter_request_delay=0,
        max_attempts=1,
        opener=FakeOpener([response]),
    )

    with pytest.raises(OLISHTTPError, match="outside the configured"):
        client.get(requested)

    assert response.closed
