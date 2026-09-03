"""Bounded HTTP access to known OLIS pages (never a generic crawler)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from ..config import OLIS_BASE_URL
from .odata import parse_retry_after


LOGGER = logging.getLogger(__name__)
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}


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
        user_agent: str = "OLISArchive/0.1 (+https://www.saveoregonschools.com/)",
        max_attempts: int = 3,
        concurrency: int = 1,
        inter_request_delay: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlsplit(self.base_url)
        self._host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or not self._host:
            raise ValueError("OLIS base URL must be absolute HTTP(S)")
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.max_attempts = max(1, int(max_attempts))
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
        if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != self._host:
            raise OLISHTTPError("URL is outside the configured OLIS HTTPS host", url=absolute)
        if not parsed.path.casefold().startswith("/liz/"):
            raise OLISHTTPError("URL is outside the supported OLIS application path", url=absolute)
        return absolute

    def get_testimony_page(self, session_key: str, bill_id: str) -> HTMLResponse:
        return self.get(self.testimony_url(session_key, bill_id))

    def get(self, url: str) -> HTMLResponse:
        validated = self._validate(url)
        last: OLISHTTPError | None = None
        for attempt in range(1, self.max_attempts + 1):
            with self._slots:
                with self._gate:
                    remaining = self._delay - (time.monotonic() - self._last_request)
                    if remaining > 0:
                        self._sleep(remaining)
                    self._last_request = time.monotonic()
                try:
                    request = Request(
                        validated,
                        headers={"Accept": "text/html", "User-Agent": self.user_agent},
                        method="GET",
                    )
                    with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                        final_url = self._validate(response.geturl())
                        content_type = response.headers.get("Content-Type", "")
                        if "html" not in content_type.casefold():
                            raise OLISHTTPError(
                                f"Expected HTML but received {content_type or 'an unknown content type'}",
                                url=final_url,
                                status_code=response.status,
                            )
                        payload = response.read(8 * 1024 * 1024 + 1)
                        if len(payload) > 8 * 1024 * 1024:
                            raise OLISHTTPError("OLIS page exceeded the 8 MiB safety limit", url=final_url)
                        return HTMLResponse(
                            final_url,
                            payload.decode("utf-8-sig", errors="replace"),
                            int(response.status),
                            dict(response.headers.items()),
                        )
                except HTTPError as exc:
                    last = OLISHTTPError(
                        f"OLIS returned HTTP {exc.code}",
                        url=validated,
                        status_code=exc.code,
                        retryable=exc.code in TRANSIENT,
                    )
                    retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                except (URLError, TimeoutError, OSError) as exc:
                    last = OLISHTTPError(f"OLIS request failed: {exc}", url=validated, retryable=True)
                    retry_after = None
                except OLISHTTPError:
                    raise
            if not last.retryable or attempt >= self.max_attempts:
                raise last
            wait_for = max(float(2 ** (attempt - 1)), float(retry_after or 0))
            LOGGER.warning("Retrying OLIS page in %.1fs after %s", wait_for, last)
            self._sleep(min(wait_for, 3600))
        raise last or OLISHTTPError("OLIS request failed", url=validated)


__all__ = ["HTMLResponse", "OLISHTTPClient", "OLISHTTPError"]

