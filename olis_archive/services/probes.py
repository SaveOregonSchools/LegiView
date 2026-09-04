"""Safe, bounded remote-size probes for inventoried official documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable, Mapping
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

from .downloads import (
    DownloadError,
    NetworkDownloadError,
    REDIRECT_STATUSES,
    RETRYABLE_HTTP_STATUSES,
    RetryPolicy,
    SafeHTTPClient,
    integer_header,
    retry_after_seconds,
    retry_delay_seconds,
    safe_response_headers,
)


_CONTENT_RANGE = re.compile(r"^bytes\s+\d+-\d+/(?P<total>\d+|\*)$", re.IGNORECASE)
_HEAD_FALLBACK_STATUSES = frozenset({400, 403, 405, 501})
CancellationRequested = Callable[[], bool]
_COOPERATIVE_SLEEP_SLICE_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Latest bounded observation for one official payload URL."""

    status: str
    method: str
    source_url: str
    final_url: str
    http_status: int
    content_type: str | None
    content_length: int | None
    etag: str | None
    last_modified: str | None

    @property
    def size_known(self) -> bool:
        return self.content_length is not None


class RemoteSizeProbe:
    """Probe metadata without consuming a complete response body.

    HEAD is attempted first. Servers that reject HEAD or omit a useful length
    are retried with ``Range: bytes=0-0`` through the existing safe HTTP client.
    The fallback reads at most one body byte before closing the response.
    """

    def __init__(
        self,
        http: SafeHTTPClient,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self.policy = RetryPolicy(
            max_attempts=max(1, int(max_attempts)),
            base_delay_seconds=2,
            maximum_delay_seconds=30,
        )
        self.sleep = sleep

    def probe(
        self,
        url: str,
        *,
        cancellation_requested: CancellationRequested | None = None,
    ) -> ProbeResult:
        try:
            head = self._with_retries(
                lambda: self._head_once(url),
                cancellation_requested=cancellation_requested,
            )
        except NetworkDownloadError as exc:
            if exc.status_code not in _HEAD_FALLBACK_STATUSES:
                raise
        else:
            if head.content_length is not None:
                return head
        return self._with_retries(
            lambda: self._range_once(url),
            cancellation_requested=cancellation_requested,
        )

    def _with_retries(
        self,
        operation: Callable[[], ProbeResult],
        *,
        cancellation_requested: CancellationRequested | None,
    ) -> ProbeResult:
        last: DownloadError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            self._raise_if_canceled(cancellation_requested)
            try:
                return operation()
            except DownloadError as exc:
                last = exc
                if not exc.retryable or attempt >= self.policy.max_attempts:
                    raise
                self._sleep_with_control(
                    retry_delay_seconds(exc, attempt, self.policy),
                    cancellation_requested,
                )
        raise last or DownloadError("Remote-size probe failed.")

    @staticmethod
    def _raise_if_canceled(
        cancellation_requested: CancellationRequested | None,
    ) -> None:
        if cancellation_requested is not None and cancellation_requested():
            raise DownloadError(
                "Remote-size probe interrupted by run control.",
                retryable=True,
                code="interrupted",
            )

    def _sleep_with_control(
        self,
        seconds: float,
        cancellation_requested: CancellationRequested | None,
    ) -> None:
        duration = max(0.0, float(seconds))
        if cancellation_requested is None:
            self.sleep(duration)
            return
        remaining = duration
        while remaining > 0:
            self._raise_if_canceled(cancellation_requested)
            interval = min(_COOPERATIVE_SLEEP_SLICE_SECONDS, remaining)
            self.sleep(interval)
            remaining -= interval
        self._raise_if_canceled(cancellation_requested)

    def _head_once(self, url: str) -> ProbeResult:
        final_url, status, headers = self._request_headers(url, method="HEAD")
        return _result(url, final_url, status, headers, method="HEAD")

    def _range_once(self, url: str) -> ProbeResult:
        with self.http.open_stream(url, headers={"Range": "bytes=0-0"}) as (response, metadata):
            raw_status = getattr(response, "status", None)
            status = int(raw_status if raw_status is not None else response.getcode())
            # Deliberately consume no more than the requested single byte. If a
            # source ignores Range, closing here still avoids a full download.
            response.read(1)
            headers = dict(metadata.headers)
            length = _range_total(headers, status)
            return _result(
                url,
                metadata.final_url,
                status,
                headers,
                method="RANGE_GET",
                content_length=length,
                infer_content_length=False,
            )

    def _request_headers(
        self,
        url: str,
        *,
        method: str,
    ) -> tuple[str, int, Mapping[str, str]]:
        current = url
        original_scheme = urlsplit(url).scheme.casefold()
        for redirect_count in range(self.http.max_redirects + 1):
            self.http.validate_target(current)
            request = urllib.request.Request(
                current,
                method=method,
                headers={"User-Agent": self.http.user_agent, "Accept": "*/*"},
            )
            response = None
            try:
                response = self.http.opener.open(request, timeout=self.http.timeout_seconds)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if status in REDIRECT_STATUSES:
                    location = exc.headers.get("Location", "")
                    exc.close()
                    if not location:
                        raise DownloadError(
                            "Redirect response did not provide a Location header.",
                            status_code=status,
                            code="invalid_redirect",
                        ) from None
                    destination = urljoin(current, location)
                    if (
                        original_scheme == "https"
                        and urlsplit(destination).scheme.casefold() == "http"
                        and not self.http.allow_https_downgrade
                    ):
                        from .downloads import UnsafeDownloadTarget

                        raise UnsafeDownloadTarget("HTTPS-to-HTTP redirects are blocked.")
                    current = destination
                    if redirect_count >= self.http.max_redirects:
                        raise DownloadError(
                            "Maximum redirect count exceeded.", code="too_many_redirects"
                        )
                    continue
                retryable = status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599
                retry_after = retry_after_seconds(exc.headers.get("Retry-After"))
                exc.close()
                raise NetworkDownloadError(
                    f"Remote server returned HTTP {status}.",
                    retryable=retryable,
                    status_code=status,
                    retry_after_seconds=retry_after,
                    code="http_error",
                ) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                raise NetworkDownloadError(
                    f"Remote-size probe failed: {exc}",
                    retryable=True,
                    code="network_error",
                ) from None

            try:
                self.http._validate_headers(response.headers)
                raw_status = getattr(response, "status", None)
                status = int(raw_status if raw_status is not None else response.getcode())
                if not 200 <= status <= 299:
                    raise NetworkDownloadError(
                        f"Remote server returned unexpected HTTP {status}.",
                        retryable=500 <= status <= 599,
                        status_code=status,
                        code="http_error",
                    )
                return current, status, safe_response_headers(response.headers)
            finally:
                response.close()
        raise DownloadError("Maximum redirect count exceeded.", code="too_many_redirects")


def _range_total(headers: Mapping[str, str], status: int) -> int | None:
    content_range = next(
        (value for name, value in headers.items() if name.casefold() == "content-range"),
        None,
    )
    if content_range:
        match = _CONTENT_RANGE.fullmatch(str(content_range).strip())
        if match and match.group("total") != "*":
            return int(match.group("total"))
    if status != 206:
        return integer_header(_header(headers, "Content-Length"))
    return None


def _result(
    source_url: str,
    final_url: str,
    status: int,
    headers: Mapping[str, str],
    *,
    method: str,
    content_length: int | None = None,
    infer_content_length: bool = True,
) -> ProbeResult:
    if content_length is None and infer_content_length:
        content_length = integer_header(_header(headers, "Content-Length"))
    return ProbeResult(
        status="known" if content_length is not None else "unknown",
        method=method,
        source_url=source_url,
        final_url=final_url,
        http_status=status,
        content_type=_header(headers, "Content-Type"),
        content_length=content_length,
        etag=_header(headers, "ETag"),
        last_modified=_header(headers, "Last-Modified"),
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return str(value)
    return None


__all__ = ["ProbeResult", "RemoteSizeProbe"]
