import json
from urllib.parse import urlsplit

import pytest

from olis_archive.services.odata import (
    ODataClient,
    ODataResponseError,
    odata_datetime_literal,
    parse_retry_after,
)


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
