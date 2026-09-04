from __future__ import annotations

from pathlib import Path
import runpy

import pytest
from flask import Flask, redirect, url_for

from olis_archive.deployment import (
    WebDeploymentConfig,
    apply_proxy_fix,
    normalize_url_prefix,
    parse_trusted_hosts,
    validate_production_web_config,
)


def test_url_prefix_normalization_accepts_root_and_canonicalizes_subpaths():
    assert normalize_url_prefix("") == "/"
    assert normalize_url_prefix("/") == "/"
    assert normalize_url_prefix(" legiview/ ") == "/legiview"
    assert normalize_url_prefix("//tools//legiview//") == "/tools/legiview"


@pytest.mark.parametrize(
    "value",
    (
        "https://example.test/legiview",
        "/legiview?mode=1",
        "/legiview#fragment",
        r"\legiview",
        "/../legiview",
    ),
)
def test_url_prefix_rejects_values_that_are_not_safe_path_prefixes(value: str):
    with pytest.raises(ValueError, match="URL path"):
        normalize_url_prefix(value)


def test_trusted_hosts_are_explicit_deduplicated_hostnames():
    assert parse_trusted_hosts("Archive.Local, archive.local, 10.0.0.8") == (
        "archive.local",
        "10.0.0.8",
    )
    with pytest.raises(ValueError, match="hostnames"):
        parse_trusted_hosts("https://archive.local")
    with pytest.raises(ValueError, match="exact hostnames"):
        parse_trusted_hosts(".example.com")
    with pytest.raises(ValueError, match="exact hostnames"):
        parse_trusted_hosts("*.example.com")


def test_proxy_mode_requires_a_persistent_secret():
    with pytest.raises(ValueError, match="LEGIVIEW_SECRET_KEY"):
        validate_production_web_config(
            {
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_SECRET_KEY_CONFIGURED": False,
                "LEGIVIEW_TRUSTED_HOSTS_CONFIGURED": False,
                "LEGIVIEW_BIND_HOST": "127.0.0.1",
            }
        )
    validate_production_web_config(
        {
            "LEGIVIEW_TRUST_PROXY": False,
            "LEGIVIEW_SECRET_KEY_CONFIGURED": False,
        }
    )


def test_proxy_mode_requires_explicit_hosts_and_loopback_bind():
    with pytest.raises(ValueError, match="LEGIVIEW_TRUSTED_HOSTS"):
        validate_production_web_config(
            {
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_SECRET_KEY_CONFIGURED": True,
                "LEGIVIEW_TRUSTED_HOSTS_CONFIGURED": False,
                "LEGIVIEW_BIND_HOST": "127.0.0.1",
            }
        )
    with pytest.raises(ValueError, match="loopback"):
        validate_production_web_config(
            {
                "LEGIVIEW_TRUST_PROXY": True,
                "LEGIVIEW_SECRET_KEY_CONFIGURED": True,
                "LEGIVIEW_TRUSTED_HOSTS_CONFIGURED": True,
                "LEGIVIEW_BIND_HOST": "0.0.0.0",
            }
        )


def test_web_deployment_config_scopes_cookie_and_keeps_explicit_hosts():
    deployment = WebDeploymentConfig(
        url_prefix="/legiview",
        trust_proxy=True,
        trusted_hosts=("archive.local",),
        secret_key="fixture-secret",
    )

    values = deployment.flask_config(bind_host="127.0.0.1")

    assert values["APPLICATION_ROOT"] == "/legiview"
    assert values["SESSION_COOKIE_NAME"] == "legiview_session"
    assert values["SESSION_COOKIE_PATH"] == "/legiview"
    assert values["TRUSTED_HOSTS"] == ["archive.local"]

    direct = WebDeploymentConfig().flask_config(bind_host="127.0.0.1")
    assert direct["TRUSTED_HOSTS"] == ["localhost", "127.0.0.1", "[::1]"]


def test_proxy_fix_honors_one_forwarded_prefix_and_direct_mode_ignores_it():
    def build(enabled: bool) -> Flask:
        app = Flask(__name__)
        app.secret_key = "fixture"

        @app.get("/")
        def index():
            return f'<a href="{url_for("next_page")}">next</a>'

        @app.get("/next")
        def next_page():
            return redirect(url_for("index"))

        apply_proxy_fix(app, enabled=enabled)
        return app

    headers = {
        "X-Forwarded-For": "192.0.2.10",
        "X-Forwarded-Host": "archive.local",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Prefix": "/legiview",
    }
    proxied = build(True).test_client()
    assert 'href="/legiview/next"' in proxied.get("/", headers=headers).text
    response = proxied.get("/next", headers=headers)
    assert response.headers["Location"] == "/legiview/"

    direct = build(False).test_client()
    assert 'href="/next"' in direct.get("/", headers=headers).text


def test_supplied_gunicorn_config_is_single_process_and_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LEGIVIEW_HOST", "127.0.0.1")
    monkeypatch.setenv("LEGIVIEW_PORT", "5055")
    monkeypatch.setenv("LEGIVIEW_WEB_THREADS", "4")

    values = runpy.run_path(
        str(Path(__file__).parents[1] / "deploy" / "gunicorn.conf.py")
    )

    assert values["bind"] == "127.0.0.1:5055"
    assert values["workers"] == 1
    assert values["threads"] == 4
    assert values["worker_class"] == "gthread"
    assert values["preload_app"] is False

    monkeypatch.setenv("LEGIVIEW_HOST", "::1")
    values = runpy.run_path(
        str(Path(__file__).parents[1] / "deploy" / "gunicorn.conf.py")
    )
    assert values["bind"] == "[::1]:5055"

    monkeypatch.setenv("LEGIVIEW_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="loopback"):
        runpy.run_path(
            str(Path(__file__).parents[1] / "deploy" / "gunicorn.conf.py")
        )


def test_supplied_systemd_unit_uses_project_local_configuration():
    root = Path(__file__).parents[1]
    unit = (root / "deploy" / "legiview.service").read_text(encoding="utf-8")
    environment = (root / "deploy" / "legiview.env.example").read_text(
        encoding="utf-8"
    )

    assert "User=legiview" in unit
    assert "Group=legiview" in unit
    assert "WorkingDirectory=/opt/legiview" in unit
    assert "EnvironmentFile=/opt/legiview/.env" in unit
    assert "ExecStart=/opt/legiview/.venv/bin/gunicorn" in unit
    assert "/etc/legiview" not in unit
    assert "/opt/legiview/.env" in environment
    assert "LEGIVIEW_HOST=127.0.0.1" in environment
    assert "LEGIVIEW_URL_PREFIX=/legiview" in environment
    assert "brad" not in unit.casefold()
    assert "brad" not in environment.casefold()


def test_fresh_install_guide_covers_prerequisites_proxy_and_operations():
    guide = (
        Path(__file__).parents[1] / "docs" / "linux_nginx.md"
    ).read_text(encoding="utf-8")

    required_steps = (
        "apt install --yes",
        "ca-certificates curl git iproute2 python3 python3-venv",
        "useradd --system",
        "git clone --branch main",
        ".[server,test]",
        "python -m pip check",
        "chmod -R a+rX,go-w /opt/legiview",
        "umask 0022 && git -C /opt/legiview pull --ff-only origin main",
        "install -d -o legiview -g legiview -m 0750 data archive",
        "deploy/legiview.env.example .env",
        "systemctl link /opt/legiview/deploy/legiview.service",
        "systemd-analyze verify",
        "location ^~ /legiview/",
        "proxy_pass http://127.0.0.1:5055/;",
        "X-Forwarded-Prefix /legiview",
        "nginx -t",
        "http://brad/legiview/static/app.css",
        "journalctl -u legiview.service",
        "pull --ff-only origin main",
    )
    for step in required_steps:
        assert step in guide

    assert "/etc/legiview" not in guide
