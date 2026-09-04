import http.client
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from olis_archive.services.odata import (
    ODataClient,
    ODataError,
    ODataResponseError,
    odata_datetime_literal,
    parse_retry_after,
)


class FakeResponse:
    def __init__(
        self,
        url,
        payload=b'{"value": []}',
        *,
        status=200,
        headers=None,
        read_error=None,
    ):
        self._url = url
        self._payload = payload
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
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


def test_odata_follows_relative_v3_continuation(fixture_dir):
    page1 = (fixture_dir / "odata_pagination_page1.json").read_bytes()
    page2 = (fixture_dir / "odata_pagination_page2.json").read_bytes()
    requested = []

    def transport(url, headers, timeout):
        requested.append(url)
        payload = page2 if "$skiptoken=opaque123" in url else page1
        return 200, {"Content-Type": "application/json"}, payload, url

    client = ODataClient(transport=transport, inter_request_delay=0, sleep=lambda _: None)
    rows = client.query("Measures", filter="SessionKey eq '2026R1'")
    assert [row["MeasureNumber"] for row in rows] == [2001, 1501]
    assert len(requested) == 2
    assert urlsplit(requested[1]).hostname == "api.oregonlegislature.gov"


def test_odata_rejects_cross_host_continuation():
    client = ODataClient(inter_request_delay=0)
    payload = {"value": [], "odata.nextLink": "https://example.invalid/stolen"}
    with pytest.raises(ODataResponseError):
        client.parse_page(payload, client.base_url + "Measures")


def test_odata_redirect_is_validated_before_following():
    source = "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"
    opener = FakeOpener(
        [HTTPError(source, 302, "redirect", {"Location": "https://example.com/stolen"}, None)]
    )
    client = ODataClient(inter_request_delay=0, max_attempts=1, opener=opener)

    with pytest.raises(ODataResponseError, match="configured service host"):
        client.request_json(source)

    assert [call[0] for call in opener.calls] == [source]


def test_odata_follows_only_a_validated_relative_redirect():
    source = "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"
    target = source + "?$skiptoken=next"
    response = FakeResponse(target)
    opener = FakeOpener(
        [
            HTTPError(source, 302, "redirect", {"Location": "Measures?$skiptoken=next"}, None),
            response,
        ]
    )
    client = ODataClient(inter_request_delay=0, max_attempts=1, opener=opener)

    payload, _headers, final_url = client.request_json(source)

    assert payload == {"value": []}
    assert final_url == target
    assert [call[0] for call in opener.calls] == [source, target]
    assert response.closed


@pytest.mark.parametrize(
    "url",
    [
        "https://user@api.oregonlegislature.gov/odata/odataservice.svc/Measures",
        "https://api.oregonlegislature.gov:8443/odata/odataservice.svc/Measures",
        "https://api.oregonlegislature.gov/odata/odataservice.svc/%2e%2e/private",
    ],
)
def test_odata_rejects_noncanonical_origin_and_path(url):
    opener = FakeOpener([])
    client = ODataClient(inter_request_delay=0, max_attempts=1, opener=opener)

    with pytest.raises(ODataResponseError):
        client.request_json(url)

    assert opener.calls == []


def test_odata_rejects_oversized_content_length_before_reading():
    url = "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"
    response = FakeResponse(url, headers={"Content-Length": "11"})
    client = ODataClient(
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
        opener=FakeOpener([response]),
    )

    with pytest.raises(ODataResponseError, match="safety limit"):
        client.request_json(url)

    assert response.read_limits == []
    assert response.closed


def test_odata_rejects_oversized_stream_without_content_length():
    url = "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"
    response = FakeResponse(url, payload=b"x" * 11)
    client = ODataClient(
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
        opener=FakeOpener([response]),
    )

    with pytest.raises(ODataResponseError, match="safety limit"):
        client.request_json(url)

    assert response.read_limits == [11]
    assert response.closed


@pytest.mark.parametrize("metadata", [False, True])
def test_odata_size_limit_is_enforced_for_injected_transport(metadata):
    def oversized_transport(url, _headers, _timeout):
        return 200, {}, b"x" * 11, url

    client = ODataClient(
        transport=oversized_transport,
        inter_request_delay=0,
        max_attempts=1,
        maximum_response_bytes=10,
    )

    with pytest.raises(ODataResponseError, match="safety limit"):
        if metadata:
            client.get_metadata_xml()
        else:
            client.request_json(client.build_url("Measures"))


def test_odata_retries_incomplete_response_and_closes_each_stream():
    url = "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures"
    incomplete = FakeResponse(
        url,
        headers={"Content-Type": "application/json", "Content-Length": "100"},
        read_error=http.client.IncompleteRead(b'{"value":', 91),
    )
    completed = FakeResponse(url)
    sleeps = []
    client = ODataClient(
        inter_request_delay=0,
        max_attempts=2,
        opener=FakeOpener([incomplete, completed]),
        sleep=sleeps.append,
    )

    payload, _headers, _final_url = client.request_json(url)

    assert payload == {"value": []}
    assert sleeps == [1.0]
    assert incomplete.closed
    assert completed.closed


def test_retry_after_seconds_is_bounded():
    assert parse_retry_after("12") == 12
    assert parse_retry_after("99999") == 3600
    assert parse_retry_after("nonsense") is None


def test_invalid_result_shape_is_rejected():
    client = ODataClient(inter_request_delay=0)
    with pytest.raises(ODataResponseError):
        client.parse_page({"value": {"not": "a list"}}, client.base_url + "Measures")


def test_odata_datetime_literal_preserves_source_time_and_rejects_filter_injection():
    assert (
        odata_datetime_literal("2026-05-15T12:28:47")
        == "datetime'2026-05-15T12:28:47'"
    )
    with pytest.raises(ValueError):
        odata_datetime_literal("2026-05-15T12:28:47Z")
    with pytest.raises(ValueError):
        odata_datetime_literal("2026-05-15T12:28:47' or true")


def test_retry_wait_is_cooperatively_interrupted_before_another_request():
    calls = []
    sleeps = []
    stop = {"requested": False}

    def transport(url, headers, timeout):
        calls.append(url)
        return 429, {"Retry-After": "10"}, b"", url

    def sleep(seconds):
        sleeps.append(seconds)
        stop["requested"] = True

    client = ODataClient(
        transport=transport,
        inter_request_delay=0,
        max_attempts=3,
        sleep=sleep,
    )
    with pytest.raises(ODataError, match="interrupted by run control"):
        client.request_json(
            client.build_url("Measures"),
            cancellation_requested=lambda: stop["requested"],
        )

    assert len(calls) == 1
    assert sleeps == [0.25]


def test_retry_wait_keeps_original_one_shot_sleep_without_callback():
    calls = []
    sleeps = []

    def transport(url, headers, timeout):
        calls.append(url)
        if len(calls) == 1:
            return 503, {}, b"", url
        return 200, {"Content-Type": "application/json"}, b'{"value": []}', url

    client = ODataClient(
        transport=transport,
        inter_request_delay=0,
        max_attempts=2,
        sleep=sleeps.append,
    )
    payload, _headers, _url = client.request_json(client.build_url("Measures"))

    assert payload == {"value": []}
    assert len(calls) == 2
    assert sleeps == [1.0]
