"""Flask deployment settings for direct and trusted reverse-proxy use.

The collector configuration intentionally stays separate from these web-only
settings.  In particular, the session secret is never included in collection
run snapshots or persisted application settings.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from werkzeug.middleware.proxy_fix import ProxyFix


LOCAL_TRUSTED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def boolean_value(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else boolean_value(value, name=name)


def normalize_url_prefix(value: Any) -> str:
    """Return ``/`` or a safe, slash-prefixed WSGI application root."""

    text = str(value or "/").strip()
    if not text or text == "/":
        return "/"
    if "://" in text or "?" in text or "#" in text or "\\" in text:
        raise ValueError("LEGIVIEW_URL_PREFIX must be a URL path such as /legiview")
    segments = [segment for segment in text.split("/") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        raise ValueError("LEGIVIEW_URL_PREFIX must be a URL path such as /legiview")
    return "/" + "/".join(segments)


def parse_trusted_hosts(value: Any) -> tuple[str, ...]:
    """Parse a comma-separated host allowlist without introducing wildcards."""

    if value is None:
        return ()
    if isinstance(value, str):
        candidates = value.split(",")
    else:
        try:
            candidates = list(value)
        except TypeError as exc:
            raise ValueError("LEGIVIEW_TRUSTED_HOSTS must be comma-separated hosts") from exc
    hosts: list[str] = []
    for candidate in candidates:
        host = str(candidate).strip().casefold()
        if not host:
            continue
        if (
            "://" in host
            or "/" in host
            or host.startswith(".")
            or "*" in host
            or any(char.isspace() for char in host)
        ):
            raise ValueError(
                "LEGIVIEW_TRUSTED_HOSTS entries must be exact hostnames, not URLs, "
                "paths, or wildcard patterns"
            )
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


@dataclass(frozen=True, slots=True)
class WebDeploymentConfig:
    """Environment-backed settings that affect only Flask deployment."""

    url_prefix: str = "/"
    trust_proxy: bool = False
    trusted_hosts: tuple[str, ...] = ()
    secret_key: str | None = None
    session_cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> "WebDeploymentConfig":
        return cls(
            url_prefix=normalize_url_prefix(os.environ.get("LEGIVIEW_URL_PREFIX", "/")),
            trust_proxy=_environment_bool("LEGIVIEW_TRUST_PROXY", False),
            trusted_hosts=parse_trusted_hosts(
                os.environ.get("LEGIVIEW_TRUSTED_HOSTS", "")
            ),
            secret_key=os.environ.get("LEGIVIEW_SECRET_KEY") or None,
            session_cookie_secure=_environment_bool(
                "LEGIVIEW_SESSION_COOKIE_SECURE", False
            ),
        )

    def flask_config(self, *, bind_host: str) -> dict[str, Any]:
        """Build safe Flask defaults, retaining direct localhost operation."""

        # In proxy mode the forwarded public host must match the explicit
        # allowlist.  Including loopback here would let a missing/forged
        # X-Forwarded-Host fall back to the private upstream Host header.
        trusted = [] if self.trust_proxy else list(LOCAL_TRUSTED_HOSTS)
        if not self.trust_proxy:
            configured_bind = str(bind_host or "").strip().casefold()
            if configured_bind and configured_bind not in {"0.0.0.0", "::", "[::]"}:
                if ":" in configured_bind and not configured_bind.startswith("["):
                    configured_bind = f"[{configured_bind}]"
                trusted.append(configured_bind)
        trusted.extend(self.trusted_hosts)
        trusted = list(dict.fromkeys(trusted))

        cookie_path = self.url_prefix if self.url_prefix != "/" else "/"
        return {
            "APPLICATION_ROOT": self.url_prefix,
            "SESSION_COOKIE_NAME": "legiview_session",
            "SESSION_COOKIE_PATH": cookie_path,
            "SESSION_COOKIE_SECURE": self.session_cookie_secure,
            "TRUSTED_HOSTS": trusted,
            "LEGIVIEW_TRUST_PROXY": self.trust_proxy,
            "LEGIVIEW_URL_PREFIX": self.url_prefix,
            "LEGIVIEW_SECRET_KEY_CONFIGURED": bool(self.secret_key),
        }


def apply_proxy_fix(app: Any, *, enabled: bool) -> None:
    """Trust one reverse-proxy hop, and only when explicitly enabled.

    Nginx is expected to remove the public prefix from ``PATH_INFO`` and send
    it in ``X-Forwarded-Prefix``.  Binding the backend to loopback prevents a
    remote client from bypassing Nginx and forging these trusted headers.
    """

    if not enabled:
        return
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=0,
        x_prefix=1,
    )


def validate_production_web_config(config: Mapping[str, Any]) -> None:
    """Fail closed when trusted-proxy prerequisites are incomplete."""

    if not config.get("LEGIVIEW_TRUST_PROXY"):
        return
    if not config.get("LEGIVIEW_SECRET_KEY_CONFIGURED"):
        raise ValueError(
            "LEGIVIEW_SECRET_KEY must be a non-placeholder value of at least 32 "
            "characters when LEGIVIEW_TRUST_PROXY is enabled"
        )
    if not config.get("LEGIVIEW_TRUSTED_HOSTS_CONFIGURED"):
        raise ValueError(
            "LEGIVIEW_TRUSTED_HOSTS is required when LEGIVIEW_TRUST_PROXY is enabled"
        )
    bind_host = str(config.get("LEGIVIEW_BIND_HOST") or "").strip().casefold()
    if bind_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "LEGIVIEW_HOST must be a loopback address when LEGIVIEW_TRUST_PROXY "
            "is enabled"
        )


__all__ = [
    "LOCAL_TRUSTED_HOSTS",
    "WebDeploymentConfig",
    "apply_proxy_fix",
    "boolean_value",
    "normalize_url_prefix",
    "parse_trusted_hosts",
    "validate_production_web_config",
]
