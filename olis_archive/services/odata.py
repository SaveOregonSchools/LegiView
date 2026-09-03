"""Narrow, retry-aware client for the Oregon Legislative OData v3 service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from ..config import ODATA_BASE_URL


LOGGER = logging.getLogger(__name__)
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
ENTITY_SETS = frozenset(
    {
        "LegislativeSessions",
        "Measures",
        "Committees",
        "CommitteeMeetings",
        "CommitteeAgendaItems",
        "CommitteeMeetingDocuments",
        "Legislators",
        "MeasureSponsors",
        "FloorLetters",
        "CommitteePublicTestimonies",
    }
)


class ODataError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        url: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class ODataResponseError(ODataError):
    pass


@dataclass(frozen=True, slots=True)
class ODataPage:
    items: tuple[dict[str, Any], ...]
    next_url: str | None
    count: int | None
    metadata_url: str | None


Transport = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes, str]]


def odata_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def odata_datetime_literal(value: str) -> str:
    """Return a validated OData v3 ``Edm.DateTime`` literal.

    The live OLIS service exposes source timestamps without an offset. Keep
    that wall-clock value unchanged while rejecting arbitrary filter syntax.
    """

    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid OData source datetime: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError("OLIS Edm.DateTime source values must not include an offset")
    if "T" not in text:
        raise ValueError("OLIS Edm.DateTime source values must include a time")
    return f"datetime'{text}'"


def measure_filter(session_key: str, prefix: str, number: int) -> str:
    return (
        f"SessionKey eq {odata_literal(session_key)} and "
        f"MeasurePrefix eq {odata_literal(prefix)} and MeasureNumber eq {int(number)}"
    )


class ODataClient:
    def __init__(
        self,
        base_url: str = ODATA_BASE_URL,
        *,
        timeout: float = 30.0,
        user_agent: str = "OLISArchive/0.1 (+https://www.saveoregonschools.com/)",
        max_attempts: int = 3,
        inter_request_delay: float = 0.25,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.max_attempts = max(1, int(max_attempts))
        self.inter_request_delay = max(0.0, float(inter_request_delay))
        self.transport = transport or self._default_transport
        self.sleep = sleep
        self._request_lock = threading.Lock()
        self._last_request_monotonic = 0.0
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OData base URL must be an absolute HTTP(S) URL")
        self._base_scheme = parsed.scheme.casefold()
        self._base_host = parsed.hostname.casefold()
        self._base_path = parsed.path.rstrip("/") + "/"

    def _default_transport(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, Mapping[str, str], bytes, str]:
        request = Request(url, headers=dict(headers), method="GET")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - base/continuations validated
            return (
                int(response.status),
                dict(response.headers.items()),
                response.read(),
                response.geturl(),
            )

    def _throttle(self) -> None:
        if not self.inter_request_delay:
            return
        with self._request_lock:
            now = time.monotonic()
            remaining = self.inter_request_delay - (now - self._last_request_monotonic)
            if remaining > 0:
                self.sleep(remaining)
            self._last_request_monotonic = time.monotonic()

    def _validated_url(self, url: str) -> str:
        absolute = urljoin(self.base_url, url)
        parsed = urlsplit(absolute)
        if parsed.scheme.casefold() != self._base_scheme or (parsed.hostname or "").casefold() != self._base_host:
            raise ODataResponseError("OData continuation left the configured service host", url=absolute)
        if not parsed.path.casefold().startswith(self._base_path.casefold()):
            raise ODataResponseError("OData continuation left the configured service path", url=absolute)
        return absolute

    def request_json(self, url: str) -> tuple[dict[str, Any], Mapping[str, str], str]:
        validated = self._validated_url(url)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "DataServiceVersion": "3.0",
            "MaxDataServiceVersion": "3.0",
        }
        last_error: ODataError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                status, response_headers, payload, final_url = self.transport(
                    validated, headers, self.timeout
                )
                if status < 200 or status >= 300:
                    retry_after = parse_retry_after(_header(response_headers, "Retry-After"))
                    raise ODataError(
                        f"OData returned HTTP {status}",
                        url=validated,
                        status_code=status,
                        retryable=status in TRANSIENT_HTTP_STATUSES,
                        retry_after=retry_after,
                    )
                final_validated = self._validated_url(final_url)
                try:
                    decoded = json.loads(payload.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ODataResponseError(
                        "OData returned invalid JSON", url=validated, status_code=status
                    ) from exc
                if not isinstance(decoded, dict):
                    raise ODataResponseError(
                        "OData response root is not an object", url=validated, status_code=status
                    )
                return decoded, response_headers, final_validated
            except HTTPError as exc:
                retryable = exc.code in TRANSIENT_HTTP_STATUSES
                last_error = ODataError(
                    f"OData returned HTTP {exc.code}",
                    url=validated,
                    status_code=exc.code,
                    retryable=retryable,
                    retry_after=parse_retry_after(exc.headers.get("Retry-After")),
                )
            except (URLError, TimeoutError, OSError) as exc:
                last_error = ODataError(
                    f"OData request failed: {exc}", url=validated, retryable=True
                )
            except ODataError as exc:
                last_error = exc
            if not last_error.retryable or attempt >= self.max_attempts:
                raise last_error
            delay = max(float(2 ** (attempt - 1)), float(last_error.retry_after or 0))
            LOGGER.warning("Retrying OData request in %.1fs after %s", delay, last_error)
            self.sleep(min(delay, 3600.0))
        raise last_error or ODataError("OData request failed", url=validated)

    def build_url(self, entity_set: str, **params: Any) -> str:
        if entity_set not in ENTITY_SETS:
            raise ValueError(f"Unsupported OData entity set: {entity_set}")
        query: list[tuple[str, str]] = []
        for name, value in params.items():
            if value is None:
                continue
            key = "$" + name.lstrip("$").replace("_", "")
            if isinstance(value, bool):
                value = str(value).lower()
            query.append((key, str(value)))
        base = urljoin(self.base_url, entity_set)
        return base + ("?" + urlencode(query) if query else "")

    def parse_page(self, payload: Mapping[str, Any], current_url: str) -> ODataPage:
        root: Mapping[str, Any] = payload
        if isinstance(payload.get("d"), dict):
            root = payload["d"]  # type: ignore[assignment]
        raw_items = root.get("value", root.get("results", []))
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise ODataResponseError("OData result does not contain an object list", url=current_url)
        raw_next = (
            root.get("odata.nextLink")
            or root.get("@odata.nextLink")
            or root.get("__next")
            or payload.get("odata.nextLink")
            or payload.get("@odata.nextLink")
        )
        next_url = self._validated_url(str(raw_next)) if raw_next else None
        raw_count = root.get("odata.count", root.get("@odata.count", root.get("__count")))
        try:
            count = int(raw_count) if raw_count is not None else None
        except (TypeError, ValueError):
            count = None
        metadata = payload.get("odata.metadata") or payload.get("@odata.context")
        return ODataPage(tuple(dict(item) for item in raw_items), next_url, count, str(metadata) if metadata else None)

    def iter_pages(self, entity_set: str, **params: Any) -> Iterator[ODataPage]:
        url: str | None = self.build_url(entity_set, **params)
        visited: set[str] = set()
        while url:
            if url in visited:
                raise ODataResponseError("OData continuation loop detected", url=url)
            if len(visited) >= 10_000:
                raise ODataResponseError("OData page limit exceeded", url=url)
            visited.add(url)
            payload, _, final_url = self.request_json(url)
            page = self.parse_page(payload, final_url)
            yield page
            url = page.next_url

    def query(self, entity_set: str, **params: Any) -> list[dict[str, Any]]:
        return [item for page in self.iter_pages(entity_set, **params) for item in page.items]

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        rows = self.query(
            "LegislativeSessions", filter=f"SessionKey eq {odata_literal(session_key)}", top=1
        )
        return rows[0] if rows else None

    def get_measure(self, session_key: str, prefix: str, number: int) -> dict[str, Any] | None:
        rows = self.query("Measures", filter=measure_filter(session_key, prefix, number), top=1)
        return rows[0] if rows else None

    def get_measures(self, session_key: str, *, max_bills: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "filter": (
                f"SessionKey eq {odata_literal(session_key)} and "
                "(MeasurePrefix eq 'HB' or MeasurePrefix eq 'SB')"
            ),
            "orderby": "MeasurePrefix,MeasureNumber",
        }
        if max_bills is not None:
            params["top"] = int(max_bills)
        return self.query("Measures", **params)

    def for_measure(self, entity_set: str, session_key: str, prefix: str, number: int, **params: Any) -> list[dict[str, Any]]:
        params["filter"] = measure_filter(session_key, prefix, number)
        return self.query(entity_set, **params)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    stripped = str(value).strip()
    try:
        return min(3600.0, max(0.0, float(stripped)))
    except ValueError:
        try:
            target = parsedate_to_datetime(stripped)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            return min(3600.0, max(0.0, (target - current).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


__all__ = [
    "ENTITY_SETS",
    "ODataClient",
    "ODataError",
    "ODataPage",
    "ODataResponseError",
    "measure_filter",
    "odata_datetime_literal",
    "odata_literal",
    "parse_retry_after",
]
