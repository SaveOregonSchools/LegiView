from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import jsonify, request, url_for

from olis_archive import create_app
from olis_archive.web import _pagination


PROXY_HEADERS = {
    "Host": "127.0.0.1:5055",
    "X-Forwarded-For": "192.0.2.25",
    "X-Forwarded-Host": "archive.test",
    "X-Forwarded-Proto": "http",
    "X-Forwarded-Prefix": "/legiview",
}


@pytest.fixture
def subpath_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "PROJECT_ROOT": tmp_path,
            "DATABASE_PATH": tmp_path / "data" / "legiview.sqlite3",
            "ARCHIVE_ROOT": tmp_path / "archive",
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "INTER_REQUEST_DELAY": 0,
            "LEGIVIEW_SECRET_KEY": "stable-subpath-test-secret-0123456789",
            "LEGIVIEW_TRUST_PROXY": True,
            "LEGIVIEW_URL_PREFIX": "/legiview",
            "LEGIVIEW_TRUSTED_HOSTS": ["archive.test"],
        }
    )

    @app.get("/_test/prefix-links")
    def prefix_links():
        return jsonify(
            home=url_for("home"),
            health=url_for("health"),
            static=url_for("static", filename="app.css"),
            form=url_for("collect_bill"),
            download=url_for("document_file", document_id=42),
            export=url_for("export_documents"),
        )

    @app.get("/_test/pagination")
    def prefixed_pagination():
        result = _pagination(2, True, request.args, total=200)
        return jsonify(previous=result.previous_url, next=result.next_url)

    @app.get("/_test/pagination/<int:item_id>")
    def prefixed_pagination_with_route_value(item_id: int):
        result = _pagination(2, True, request.args, total=200)
        return jsonify(item_id=item_id, previous=result.previous_url, next=result.next_url)

    yield app
    assert app.extensions["legiview"]["shutdown"]()


def test_legiview_links_forms_static_api_and_downloads_retain_prefix(subpath_app):
    client = subpath_app.test_client()

    home = client.get("/", headers=PROXY_HEADERS)
    assert home.status_code == 200
    html = home.get_data(as_text=True)
    assert 'href="/legiview/"' in html
    assert 'href="/legiview/static/app.css"' in html
    assert 'href="/legiview/inventory-backfill"' in html
    assert 'content="/legiview/"' in html
    assert 'data-app-base-url="/legiview/"' in html

    form_page = client.get("/collect/bill", headers=PROXY_HEADERS)
    assert form_page.status_code == 200
    form = BeautifulSoup(form_page.get_data(as_text=True), "html.parser").find("form")
    assert form is not None
    assert form.get("action") == "/legiview/collect/bill"

    links = client.get("/_test/prefix-links", headers=PROXY_HEADERS).get_json()
    assert links == {
        "home": "/legiview/",
        "health": "/legiview/health",
        "static": "/legiview/static/app.css",
        "form": "/legiview/collect/bill",
        "download": "/legiview/documents/42/file",
        "export": "/legiview/exports/documents.csv",
    }

    pagination = client.get(
        "/_test/pagination?kind=public_testimony&page=2", headers=PROXY_HEADERS
    ).get_json()
    assert pagination == {
        "previous": "/legiview/_test/pagination?kind=public_testimony&page=1",
        "next": "/legiview/_test/pagination?kind=public_testimony&page=3",
    }


def test_subpath_session_cookie_and_post_redirect_are_prefix_scoped(subpath_app):
    client = subpath_app.test_client()
    response = client.get("/collect/bill", headers=PROXY_HEADERS)
    cookie = response.headers.get("Set-Cookie", "")
    assert cookie.startswith("legiview_session=")
    assert "Path=/legiview" in cookie
    page = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    token = str(page.find("input", {"name": "_csrf_token"})["value"])
    # The test client sees the prefix-stripped upstream path when selecting its
    # cookie jar. A browser selects the cookie on /legiview before Nginx strips
    # the path, so mirror that already-selected Cookie header with a test-only
    # root-path copy of the same signed value.
    parsed = SimpleCookie()
    parsed.load(cookie)
    client.set_cookie(
        "legiview_session",
        parsed["legiview_session"].value,
        domain="127.0.0.1",
        path="/",
    )

    queued = client.post(
        "/collect/bill",
        headers=PROXY_HEADERS,
        data={
            "_csrf_token": token,
            "session_key": "2025R1",
            "bill_id": "HJR11",
        },
        follow_redirects=False,
    )

    assert queued.status_code == 303
    assert queued.headers["Location"] == "/legiview/runs/1"


def test_proxy_mode_rejects_missing_wrong_prefix_and_untrusted_forwarded_host(
    subpath_app,
):
    client = subpath_app.test_client()
    without_prefix = dict(PROXY_HEADERS)
    without_prefix.pop("X-Forwarded-Prefix")
    assert client.get("/health", headers=without_prefix).status_code == 400

    wrong_prefix = {**PROXY_HEADERS, "X-Forwarded-Prefix": "/another-app"}
    assert client.get("/health", headers=wrong_prefix).status_code == 400

    for malformed_prefix in ("//legiview//", "legiview", "/legiview/"):
        malformed = {
            **PROXY_HEADERS,
            "X-Forwarded-Prefix": malformed_prefix,
        }
        response = client.get("/health", headers=malformed)
        assert response.status_code == 400

    wrong_host = {**PROXY_HEADERS, "X-Forwarded-Host": "attacker.example"}
    assert client.get("/health", headers=wrong_host).status_code == 400

    missing_forwarded_host = dict(PROXY_HEADERS)
    missing_forwarded_host.pop("X-Forwarded-Host")
    assert client.get("/health", headers=missing_forwarded_host).status_code == 400

    local_forwarded_host = {**PROXY_HEADERS, "X-Forwarded-Host": "127.0.0.1"}
    assert client.get("/health", headers=local_forwarded_host).status_code == 400


def test_prefixed_pagination_treats_query_keys_only_as_query_data(subpath_app):
    response = subpath_app.test_client().get(
        "/_test/pagination/7?item_id=2&_external=1&_method=POST&tag=a&tag=b&page=2",
        headers=PROXY_HEADERS,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["item_id"] == 7
    for name, expected_page in (("previous", "1"), ("next", "3")):
        target = urlsplit(payload[name])
        assert target.scheme == ""
        assert target.netloc == ""
        assert target.path == "/legiview/_test/pagination/7"
        assert parse_qs(target.query) == {
            "item_id": ["2"],
            "_external": ["1"],
            "_method": ["POST"],
            "tag": ["a", "b"],
            "page": [expected_page],
        }


def test_direct_local_mode_ignores_forwarded_prefix_and_stays_at_root(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "START_WORKER": False,
            "DATABASE_PATH": tmp_path / "direct.sqlite3",
            "ARCHIVE_ROOT": tmp_path / "archive",
            "MINIMUM_FREE_SPACE_BYTES": 0,
            "LEGIVIEW_TRUST_PROXY": False,
            "LEGIVIEW_URL_PREFIX": "/",
        }
    )
    try:
        response = app.test_client().get(
            "/", headers={"X-Forwarded-Prefix": "/attacker-controlled"}
        )
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'href="/static/app.css"' in html
        assert "/attacker-controlled" not in html
    finally:
        assert app.extensions["legiview"]["shutdown"]()


def test_proxy_mode_without_persistent_secret_fails_before_database_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("LEGIVIEW_SECRET_KEY", raising=False)
    database_path = tmp_path / "must-not-be-created.sqlite3"

    with pytest.raises(ValueError, match="LEGIVIEW_SECRET_KEY"):
        create_app(
            {
                "TESTING": True,
                "START_WORKER": False,
                "DATABASE_PATH": database_path,
                "ARCHIVE_ROOT": tmp_path / "archive",
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_URL_PREFIX": "/legiview",
                "LEGIVIEW_TRUSTED_HOSTS": ["archive.test"],
            }
        )

    assert not database_path.exists()


@pytest.mark.parametrize(
    "secret",
    ("short-secret", "replace-with-a-persistent-random-value", "   "),
)
def test_proxy_mode_rejects_weak_or_placeholder_secret_before_database_creation(
    tmp_path: Path,
    secret: str,
):
    database_path = tmp_path / "must-not-be-created.sqlite3"

    with pytest.raises(ValueError, match="at least 32"):
        create_app(
            {
                "TESTING": True,
                "START_WORKER": False,
                "DATABASE_PATH": database_path,
                "ARCHIVE_ROOT": tmp_path / "archive",
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_URL_PREFIX": "/legiview",
                "LEGIVIEW_TRUSTED_HOSTS": ["archive.test"],
                "LEGIVIEW_SECRET_KEY": secret,
            }
        )

    assert not database_path.exists()


def test_proxy_mode_refuses_a_non_loopback_backend_before_database_creation(
    tmp_path: Path,
):
    database_path = tmp_path / "must-not-be-created.sqlite3"

    with pytest.raises(ValueError, match="loopback"):
        create_app(
            {
                "TESTING": True,
                "START_WORKER": False,
                "HOST": "0.0.0.0",
                "DATABASE_PATH": database_path,
                "ARCHIVE_ROOT": tmp_path / "archive",
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_URL_PREFIX": "/legiview",
                "LEGIVIEW_TRUSTED_HOSTS": ["archive.test"],
                "LEGIVIEW_SECRET_KEY": "stable-subpath-test-secret-0123456789",
            }
        )

    assert not database_path.exists()
