"""Bounded HTTP access to known OLIS pages (never a generic crawler)."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import logging
import posixpath
import threading
import time
from typing import Callable, Mapping
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request

from ..config import DEFAULT_USER_AGENT, OLIS_BASE_URL
from .odata import parse_retry_after


LOGGER = logging.getLogger(__name__)
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
CancellationRequested = Callable[[], bool]
_COOPERATIVE_SLEEP_SLICE_SECONDS = 0.25
# The rendered 2025R1/SB210 testimony page exceeds 8 MiB.  Keep HTML bounded,
# but leave enough headroom for observed high-testimony bills.
DEFAULT_MAXIMUM_RESPONSE_BYTES = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):  # noqa: ANN001
        return None


def _normalized_url_path(path: str) -> str:
    decoded = path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "\x00" in decoded:
        raise ValueError("URL path contained a null byte")
    return posixpath.normpath(decoded.replace("\\", "/"))


class OLISHTTPError(RuntimeError):
    def __init__(self, message: str, *, url: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class HTMLResponse:
    url: str
    text: str
    status_code: int
    headers: Mapping[str, str]


class OLISHTTPClient:
    def __init__(
        self,
        base_url: str = OLIS_BASE_URL,
        *,
        timeout: float = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        max_attempts: int = 3,
        concurrency: int = 1,
        inter_request_delay: float = 0.25,
        max_redirects: int = 8,
        maximum_response_bytes: int = DEFAULT_MAXIMUM_RESPONSE_BYTES,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlsplit(self.base_url)
        self._host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or not self._host:
            raise ValueError("OLIS base URL must be an absolute HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("OLIS base URL must not contain credentials")
        try:
            self._port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise ValueError("OLIS base URL contains an invalid port") from exc
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum_response_bytes must be positive")
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.max_attempts = max(1, int(max_attempts))
        self.max_redirects = int(max_redirects)
        self.maximum_response_bytes = int(maximum_response_bytes)
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._slots = threading.BoundedSemaphore(max(1, int(concurrency)))
        self._delay = max(0.0, float(inter_request_delay))
        self._sleep = sleep
        self._gate = threading.Lock()
        self._last_request = 0.0

    def testimony_url(self, session_key: str, bill_id: str) -> str:
        if not session_key.isalnum() or not bill_id.isalnum():
            raise ValueError("Unsafe OLIS path segment")
        return urljoin(self.base_url, f"liz/{session_key}/Measures/Testimony/{bill_id}")

    def _validate(self, url: str) -> str:
        absolute = urljoin(self.base_url, url)
        parsed = urlsplit(absolute)
        try:
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise OLISHTTPError("URL contains an invalid port", url=absolute) from exc
        if parsed.username is not None or parsed.password is not None:
            raise OLISHTTPError("URL contains embedded credentials", url=absolute)
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold().rstrip(".") != self._host
            or port != self._port
        ):
            raise OLISHTTPError("URL is outside the configured OLIS HTTPS host", url=absolute)
        try:
            normalized_path = _normalized_url_path(parsed.path).casefold()
        except ValueError as exc:
            raise OLISHTTPError("URL contains an invalid path", url=absolute) from exc
        if normalized_path != "/liz" and not normalized_path.startswith("/liz/"):
            raise OLISHTTPError("URL is outside the supported OLIS application path", url=absolute)
        return absolute

    def _open_with_validated_redirects(self, url: str):  # noqa: ANN202
        current = self._validate(url)
        redirects_followed = 0
        while True:
            request = Request(
                current,
                headers={"Accept": "text/html", "User-Agent": self.user_agent},
                method="GET",
            )
            try:
                response = self._opener.open(request, timeout=self.timeout)  # noqa: S310
            except HTTPError as exc:
                status = int(exc.code)
                if status not in REDIRECT_STATUSES:
                    raise
                location = (exc.headers or {}).get("Location", "")
                exc.close()
                if not location:
                    raise OLISHTTPError(
                        "OLIS redirect did not provide a Location header",
                        url=current,
                        status_code=status,
                    ) from None
                if redirects_followed >= self.max_redirects:
                    raise OLISHTTPError(
                        "OLIS maximum redirect count exceeded",
                        url=current,
                        status_code=status,
                    ) from None
                current = self._validate(urljoin(current, location))
                redirects_followed += 1
                continue

            try:
                raw_status = getattr(response, "status", None)
                status = int(raw_status if raw_status is not None else response.getcode())
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise OLISHTTPError(
                            "OLIS redirect did not provide a Location header",
                            url=current,
                            status_code=status,
                        )
                    if redirects_followed >= self.max_redirects:
                        raise OLISHTTPError(
                            "OLIS maximum redirect count exceeded",
                            url=current,
                            status_code=status,
                        )
                    next_url = self._validate(urljoin(current, location))
                    response.close()
                    current = next_url
                    redirects_followed += 1
                    continue
                reported_url = self._validate(response.geturl())
                if reported_url != current:
                    raise OLISHTTPError(
                        "OLIS HTTP opener followed an unexpected redirect",
                        url=reported_url,
                        status_code=status,
                    )
                if not 200 <= status <= 299:
                    raise OLISHTTPError(
                        f"OLIS returned unexpected HTTP {status}",
                        url=current,
                        status_code=status,
                        retryable=status in TRANSIENT or 500 <= status <= 599,
                    )
                return response, current
            except BaseException:
                response.close()
                raise

    def _raise_if_canceled(
        self, url: str, cancellation_requested: CancellationRequested | None
    ) -> None:
        if cancellation_requested is not None and cancellation_requested():
            raise OLISHTTPError(
                "OLIS request interrupted by run control",
                url=url,
                retryable=True,
            )

    def _sleep_with_control(
        self,
        seconds: float,
        *,
        url: str,
        cancellation_requested: CancellationRequested | None,
    ) -> None:
        duration = max(0.0, float(seconds))
        if cancellation_requested is None:
            # Preserve the original one-shot sleep for every existing caller.
            self._sleep(duration)
            return
        remaining = duration
        while remaining > 0:
            self._raise_if_canceled(url, cancellation_requested)
            interval = min(_COOPERATIVE_SLEEP_SLICE_SECONDS, remaining)
            self._sleep(interval)
            remaining -= interval
        self._raise_if_canceled(url, cancellation_requested)

    def get_testimony_page(
        self,
        session_key: str,
        bill_id: str,
        *,
        cancellation_requested: CancellationRequested | None = None,
    ) -> HTMLResponse:
        return self.get(
            self.testimony_url(session_key, bill_id),
            cancellation_requested=cancellation_requested,
        )

    def get(
        self,
        url: str,
        *,
        cancellation_requested: CancellationRequested | None = None,
    ) -> HTMLResponse:
        validated = self._validate(url)
        last: OLISHTTPError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._raise_if_canceled(validated, cancellation_requested)
            retry_after = None
            with self._slots:
                with self._gate:
                    remaining = self._delay - (time.monotonic() - self._last_request)
                    if remaining > 0:
                        self._sleep_with_control(
                            remaining,
                            url=validated,
                            cancellation_requested=cancellation_requested,
                        )
                    self._last_request = time.monotonic()
                try:
                    response, final_url = self._open_with_validated_redirects(validated)
                    try:
                        content_type = response.headers.get("Content-Type", "")
                        if "html" not in content_type.casefold():
                            raise OLISHTTPError(
                                f"Expected HTML but received {content_type or 'an unknown content type'}",
                                url=final_url,
                                status_code=response.status,
                            )
                        raw_length = response.headers.get("Content-Length", "")
                        if raw_length:
                            try:
                                content_length = int(raw_length)
                            except ValueError as exc:
                                raise OLISHTTPError(
                                    "OLIS returned an invalid Content-Length header",
                                    url=final_url,
                                    status_code=response.status,
                                ) from exc
                            if content_length < 0:
                                raise OLISHTTPError(
                                    "OLIS returned a negative Content-Length header",
                                    url=final_url,
                                    status_code=response.status,
                                )
                            if content_length > self.maximum_response_bytes:
                                raise OLISHTTPError(
                                    "OLIS page exceeded the configured safety limit",
                                    url=final_url,
                                    status_code=response.status,
                                )
                        payload = response.read(self.maximum_response_bytes + 1)
                        if len(payload) > self.maximum_response_bytes:
                            raise OLISHTTPError(
                                "OLIS page exceeded the configured safety limit",
                                url=final_url,
                            )
                        return HTMLResponse(
                            final_url,
                            payload.decode("utf-8-sig", errors="replace"),
                            int(response.status),
                            dict(response.headers.items()),
                        )
                    finally:
                        response.close()
                except HTTPError as exc:
                    last = OLISHTTPError(
                        f"OLIS returned HTTP {exc.code}",
                        url=validated,
                        status_code=exc.code,
                        retryable=exc.code in TRANSIENT,
                    )
                    retry_after = parse_retry_after((exc.headers or {}).get("Retry-After"))
                    exc.close()
                except http.client.IncompleteRead:
                    last = OLISHTTPError(
                        "OLIS response ended before the advertised payload was complete",
                        url=validated,
                        retryable=True,
                    )
                    retry_after = None
                except (URLError, TimeoutError, OSError) as exc:
                    last = OLISHTTPError(f"OLIS request failed: {exc}", url=validated, retryable=True)
                    retry_after = None
                except OLISHTTPError as exc:
                    last = exc
            if not last.retryable or attempt >= self.max_attempts:
                raise last
            wait_for = max(float(2 ** (attempt - 1)), float(retry_after or 0))
            LOGGER.warning("Retrying OLIS page in %.1fs after %s", wait_for, last)
            self._sleep_with_control(
                min(wait_for, 3600),
                url=validated,
                cancellation_requested=cancellation_requested,
            )
        raise last or OLISHTTPError("OLIS request failed", url=validated)


__all__ = ["HTMLResponse", "OLISHTTPClient", "OLISHTTPError"]
