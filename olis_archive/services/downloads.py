"""Safe, streamed, non-overwriting downloads for known OLIS documents.

One-shot failures carry retry metadata so a durable collection worker can
persist its own attempt and ``next_attempt_at`` state.  A small bounded retry
wrapper is also provided for CLI/tests; it never retries low-space pauses,
cancellation, unsafe targets, or invalid payloads.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
import errno
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import ssl
import threading
import time
from typing import Callable, Collection, Iterator, Mapping
import urllib.error
from urllib.parse import unquote, urljoin, urlsplit
import urllib.request

from ..config import DEFAULT_ALLOWED_DOWNLOAD_HOSTS
from .archive_paths import (
    ArchivePathCollision,
    collision_safe_destination,
    ensure_archive_directory,
    ensure_within_archive,
    part_path_for,
    sanitize_windows_filename,
    stored_relative_path,
)
from .file_types import (
    FileValidation,
    filename_with_extension,
    has_compatible_extension,
    normalize_mime_type,
    validate_file,
)
from .hashing import sha256_file


DEFAULT_USER_AGENT = "LegiView/0.1 (local Oregon legislative archive)"
SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-disposition",
        "etag",
        "last-modified",
        "accept-ranges",
        "cache-control",
    }
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


class DownloadError(RuntimeError):
    """A classified failure suitable for durable job state."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        code: str = "download_error",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.status = status_code
        self.retry_after_seconds = retry_after_seconds
        self.code = code


class UnsafeDownloadTarget(DownloadError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="unsafe_target")


class NetworkDownloadError(DownloadError):
    pass


class ContentLengthMismatch(DownloadError):
    def __init__(self, expected: int, received: int) -> None:
        super().__init__(
            f"Received {received} bytes, but Content-Length advertised {expected}.",
            retryable=True,
            code="content_length_mismatch",
        )
        self.expected = expected
        self.received = received


class DownloadValidationError(DownloadError):
    def __init__(self, message: str, *, validation: FileValidation | None = None) -> None:
        super().__init__(message, code="validation_failed")
        self.validation = validation


class DownloadHashMismatch(DownloadValidationError):
    def __init__(self, expected: str, received: str) -> None:
        super().__init__(f"SHA-256 mismatch: expected {expected}, received {received}.")
        self.expected = expected
        self.received = received
        self.code = "sha256_mismatch"


class DownloadTooLarge(DownloadError):
    def __init__(self, maximum_bytes: int) -> None:
        super().__init__(
            f"Download exceeds the configured maximum of {maximum_bytes} bytes.",
            code="download_too_large",
        )


class DestinationConflict(DownloadError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, code="destination_conflict")


class LowDiskSpace(RuntimeError):
    """The transfer must pause until the configured free-space floor is met."""


LowDiskSpaceError = LowDiskSpace


class DownloadInterrupted(RuntimeError):
    """Cooperative cancellation interrupted a transfer between chunks."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    maximum_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")


@dataclass(frozen=True, slots=True)
class DownloadResult:
    source_url: str
    final_url: str
    path: Path
    relative_path: str
    filename: str
    remote_filename: str
    byte_count: int
    expected_length: int | None
    declared_mime_type: str
    detected_mime_type: str
    sha256: str
    validation: FileValidation
    etag: str = ""
    last_modified: str = ""
    response_metadata: dict[str, str] = field(default_factory=dict)
    redirects: tuple[str, ...] = ()
    skipped: bool = False

    @property
    def mime_type(self) -> str:
        return self.detected_mime_type or self.declared_mime_type

    @property
    def status(self) -> str:
        return "downloaded"


@dataclass(frozen=True, slots=True)
class _StreamMetadata:
    source_url: str
    final_url: str
    headers: dict[str, str]
    redirects: tuple[str, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):  # noqa: ANN001
        return None


class SafeHTTPClient:
    """HTTP client that validates the initial target and every redirect."""

    def __init__(
        self,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float = 30.0,
        max_redirects: int = 8,
        max_header_bytes: int = 64 * 1024,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_private_network: bool = False,
        allow_https_downgrade: bool = False,
        resolver: Callable = socket.getaddrinfo,
        opener=None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.allowed_hosts = _normalize_allowed_hosts(
            DEFAULT_ALLOWED_DOWNLOAD_HOSTS if allowed_hosts is None else allowed_hosts
        )
        self.timeout_seconds = float(timeout_seconds)
        self.max_redirects = int(max_redirects)
        self.max_header_bytes = int(max_header_bytes)
        self.user_agent = user_agent
        self.allow_private_network = allow_private_network
        self.allow_https_downgrade = allow_https_downgrade
        self.resolver = resolver
        self.opener = opener or urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    @contextlib.contextmanager
    def open_stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[tuple[object, _StreamMetadata]]:
        current = url
        redirects: list[str] = []
        response = None
        request_headers = _safe_request_headers(headers or {})
        original_scheme = urlsplit(url).scheme.casefold()
        try:
            for _ in range(self.max_redirects + 1):
                self.validate_target(current)
                outgoing_headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                    **request_headers,
                }
                request = urllib.request.Request(current, method="GET", headers=outgoing_headers)
                try:
                    response = self.opener.open(request, timeout=self.timeout_seconds)
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
                        parsed_destination = urlsplit(destination)
                        if (
                            original_scheme == "https"
                            and parsed_destination.scheme.casefold() == "http"
                            and not self.allow_https_downgrade
                        ):
                            raise UnsafeDownloadTarget("HTTPS-to-HTTP redirects are blocked.")
                        if urlsplit(current).hostname != parsed_destination.hostname:
                            request_headers = {
                                key: value
                                for key, value in request_headers.items()
                                if key.casefold() not in {"authorization", "cookie"}
                            }
                        current = destination
                        redirects.append(destination)
                        if len(redirects) > self.max_redirects:
                            raise DownloadError("Maximum redirect count exceeded.", code="too_many_redirects")
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
                except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
                    raise NetworkDownloadError(
                        network_error_message(exc),
                        retryable=True,
                        code="network_error",
                    ) from None

                self._validate_headers(response.headers)
                raw_status = getattr(response, "status", None)
                if raw_status is None:
                    raw_status = response.getcode()
                status = int(raw_status)
                if not 200 <= status <= 299:
                    response.close()
                    response = None
                    raise NetworkDownloadError(
                        f"Remote server returned unexpected HTTP {status}.",
                        retryable=500 <= status <= 599,
                        status_code=status,
                        code="http_error",
                    )
                safe_headers = safe_response_headers(response.headers)
                yield response, _StreamMetadata(
                    source_url=url,
                    final_url=current,
                    headers=safe_headers,
                    redirects=tuple(redirects),
                )
                return
            raise DownloadError("Maximum redirect count exceeded.", code="too_many_redirects")
        finally:
            if response is not None:
                response.close()

    def validate_target(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError as exc:
            raise UnsafeDownloadTarget(f"Invalid download URL: {exc}") from None
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise UnsafeDownloadTarget("Only absolute HTTP and HTTPS URLs are allowed.")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeDownloadTarget("Credentials embedded in download URLs are blocked.")
        host = parsed.hostname.casefold().rstrip(".")
        if not _host_is_allowed(host, self.allowed_hosts):
            raise UnsafeDownloadTarget(f"Download host is not on the expected-host allowlist: {host}")
        try:
            addresses = self.resolver(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise NetworkDownloadError(
                "DNS resolution failed.", retryable=True, code="dns_failure"
            ) from None
        if not addresses:
            raise NetworkDownloadError(
                "DNS resolution returned no addresses.", retryable=True, code="dns_failure"
            )
        for item in addresses:
            try:
                address = ipaddress.ip_address(item[4][0].split("%", 1)[0])
            except ValueError:
                raise UnsafeDownloadTarget("DNS returned an invalid network address.") from None
            if self.allow_private_network:
                continue
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
            ):
                raise UnsafeDownloadTarget(
                    "Download URL resolves to a blocked local or non-public address."
                )

    def _validate_headers(self, headers) -> None:  # noqa: ANN001
        size = sum(len(str(key)) + len(str(value)) + 4 for key, value in headers.items())
        if size > self.max_header_bytes:
            raise DownloadError(
                "Response headers exceeded the configured safety limit.",
                code="headers_too_large",
            )


class DiskReservationManager:
    """Coordinate a filesystem free-space floor across concurrent downloads."""

    def __init__(self, *, minimum_reservation_bytes: int = 64 * 1024 * 1024) -> None:
        self.minimum_reservation_bytes = max(0, int(minimum_reservation_bytes))
        self._lock = threading.Lock()
        self._reserved: dict[str, int] = {}
        # Bytes written by active reservations are already absent from the
        # filesystem's reported free-space value.  Track them separately so
        # they are not also counted as still-reserved future bytes.
        self._written: dict[str, int] = {}

    @contextlib.contextmanager
    def reserve(self, path: Path, byte_count: int, floor: int):
        key = _filesystem_key(path)
        amount = max(self.minimum_reservation_bytes, int(byte_count or 0))
        reservation = {"amount": amount}
        reservation["written"] = 0
        with self._lock:
            free = _free_bytes(path)
            already_reserved = self._reserved.get(key, 0)
            active_written = self._written.get(key, 0)
            if free + active_written - already_reserved - amount < max(0, int(floor)):
                raise LowDiskSpace(
                    "Concurrent download reservations would cross the configured free-space floor."
                )
            self._reserved[key] = already_reserved + amount
            self._written.setdefault(key, 0)

        def grow(required_bytes: int, *, written_bytes: int | None = None) -> None:
            required = max(reservation["amount"], int(required_bytes))
            delta = required - reservation["amount"]
            with self._lock:
                if written_bytes is not None:
                    written = max(reservation["written"], int(written_bytes))
                    written_delta = written - reservation["written"]
                    if written_delta:
                        self._written[key] = self._written.get(key, 0) + written_delta
                        reservation["written"] = written
                if delta <= 0:
                    return
                free = _free_bytes(path)
                active_written = self._written.get(key, 0)
                if (
                    free
                    + active_written
                    - self._reserved.get(key, 0)
                    - delta
                    < max(0, int(floor))
                ):
                    raise LowDiskSpace(
                        "Concurrent streaming downloads would cross the configured free-space floor."
                    )
                self._reserved[key] = self._reserved.get(key, 0) + delta
                reservation["amount"] = required

        try:
            yield grow
        finally:
            with self._lock:
                remaining = self._reserved.get(key, 0) - reservation["amount"]
                if remaining > 0:
                    self._reserved[key] = remaining
                else:
                    self._reserved.pop(key, None)
                written_remaining = self._written.get(key, 0) - reservation["written"]
                if written_remaining > 0:
                    self._written[key] = written_remaining
                else:
                    self._written.pop(key, None)


DISK_RESERVATIONS = DiskReservationManager()


class Downloader:
    def __init__(
        self,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float = 30.0,
        max_redirects: int = 8,
        user_agent: str = DEFAULT_USER_AGENT,
        minimum_free_space_bytes: int = 0,
        chunk_size: int = 1024 * 1024,
        maximum_download_bytes: int | None = None,
        allow_private_network: bool = False,
        client: SafeHTTPClient | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if minimum_free_space_bytes < 0:
            raise ValueError("minimum_free_space_bytes cannot be negative")
        if maximum_download_bytes is not None and maximum_download_bytes <= 0:
            raise ValueError("maximum_download_bytes must be positive when supplied")
        self.client = client or SafeHTTPClient(
            allowed_hosts=allowed_hosts,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
            user_agent=user_agent,
            allow_private_network=allow_private_network,
        )
        self.minimum_free_space_bytes = int(minimum_free_space_bytes)
        self.chunk_size = int(chunk_size)
        self.maximum_download_bytes = maximum_download_bytes

    def download_to_path(
        self,
        url: str,
        destination: str | Path,
        *,
        archive_root: str | Path | None = None,
        headers: Mapping[str, str] | None = None,
        expected_mime_type: str = "",
        expected_length: int | None = None,
        expected_sha256: str = "",
        cancellation_requested: Callable[[], bool] | None = None,
        correct_extension: bool = True,
    ) -> DownloadResult:
        destination_path = Path(destination).expanduser().resolve(strict=False)
        root = (
            Path(archive_root).expanduser().resolve(strict=False)
            if archive_root is not None
            else destination_path.parent
        )
        ensure_within_archive(root, destination_path)
        relative_parent = destination_path.parent.relative_to(root)
        ensure_archive_directory(root, relative_parent)

        # Even an idempotent local skip must be tied to an allowed HTTP(S)
        # source.  This deliberately happens before examining the destination.
        self.client.validate_target(url)
        if destination_path.exists():
            return self._validated_existing_result(
                url,
                destination_path,
                root,
                expected_mime_type=expected_mime_type,
                expected_length=expected_length,
                expected_sha256=expected_sha256,
            )
        return self._transfer(
            url,
            root,
            fixed_destination=destination_path,
            headers=headers,
            expected_mime_type=expected_mime_type,
            expected_length=expected_length,
            expected_sha256=expected_sha256,
            cancellation_requested=cancellation_requested,
            correct_extension=correct_extension,
            allow_collision_suffix=False,
        )

    def download_to_directory(
        self,
        url: str,
        destination_directory: str | Path,
        *,
        archive_root: str | Path | None = None,
        headers: Mapping[str, str] | None = None,
        suggested_filename: str = "",
        prefer_suggested_filename: bool = False,
        expected_mime_type: str = "",
        expected_length: int | None = None,
        expected_sha256: str = "",
        cancellation_requested: Callable[[], bool] | None = None,
        correct_extension: bool = True,
    ) -> DownloadResult:
        directory = Path(destination_directory).expanduser().resolve(strict=False)
        root = (
            Path(archive_root).expanduser().resolve(strict=False)
            if archive_root is not None
            else directory
        )
        ensure_within_archive(root, directory)
        ensure_archive_directory(root, directory.relative_to(root))
        if prefer_suggested_filename and suggested_filename:
            return self.download_to_path(
                url,
                directory / sanitize_windows_filename(suggested_filename),
                archive_root=root,
                headers=headers,
                expected_mime_type=expected_mime_type,
                expected_length=expected_length,
                expected_sha256=expected_sha256,
                cancellation_requested=cancellation_requested,
                correct_extension=correct_extension,
            )
        return self._transfer(
            url,
            root,
            destination_directory=directory,
            suggested_filename=suggested_filename,
            headers=headers,
            expected_mime_type=expected_mime_type,
            expected_length=expected_length,
            expected_sha256=expected_sha256,
            cancellation_requested=cancellation_requested,
            correct_extension=correct_extension,
            allow_collision_suffix=True,
        )

    def download_to_path_with_retries(
        self,
        url: str,
        destination: str | Path,
        *,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        on_retry: Callable[[DownloadError, int, float], None] | None = None,
        **kwargs,
    ) -> DownloadResult:
        policy = retry_policy or RetryPolicy()
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return self.download_to_path(url, destination, **kwargs)
            except DownloadError as exc:
                if not exc.retryable or attempt >= policy.max_attempts:
                    raise
                delay = retry_delay_seconds(exc, attempt, policy)
                if on_retry:
                    on_retry(exc, attempt, delay)
                sleeper(delay)
        raise AssertionError("retry loop terminated unexpectedly")

    def _validated_existing_result(
        self,
        source_url: str,
        destination: Path,
        root: Path,
        *,
        expected_mime_type: str,
        expected_length: int | None,
        expected_sha256: str,
    ) -> DownloadResult:
        if not expected_sha256.strip():
            raise DestinationConflict(
                "Existing destination has no stored SHA-256 expectation; refusing to adopt unrelated bytes."
            )
        validation = validate_file(
            destination,
            expected_mime_type,
            expected_length,
            expected_mime_type=expected_mime_type,
            expected_sha256=expected_sha256,
            logical_filename=destination.name,
        )
        if not validation.valid:
            raise DestinationConflict(
                f"Existing destination is not the expected completed file: {validation.details}"
            )
        digest = expected_sha256.casefold() or sha256_file(destination)
        return DownloadResult(
            source_url=source_url,
            final_url=source_url,
            path=destination,
            relative_path=stored_relative_path(root, destination),
            filename=destination.name,
            remote_filename=destination.name,
            byte_count=destination.stat().st_size,
            expected_length=expected_length,
            declared_mime_type=normalize_mime_type(expected_mime_type),
            detected_mime_type=validation.detection.mime_type,
            sha256=digest,
            validation=validation,
            skipped=True,
        )

    def _transfer(
        self,
        url: str,
        root: Path,
        *,
        fixed_destination: Path | None = None,
        destination_directory: Path | None = None,
        suggested_filename: str = "",
        headers: Mapping[str, str] | None = None,
        expected_mime_type: str = "",
        expected_length: int | None = None,
        expected_sha256: str = "",
        cancellation_requested: Callable[[], bool] | None = None,
        correct_extension: bool = True,
        allow_collision_suffix: bool,
    ) -> DownloadResult:
        part: Path | None = None
        part_owned = False
        metadata: _StreamMetadata | None = None
        declared_mime = ""
        advertised_length: int | None = None
        remote_filename = ""
        byte_count = 0
        digest = hashlib.sha256()
        try:
            with self.client.open_stream(url, headers=headers) as (response, metadata):
                declared_mime = normalize_mime_type(metadata.headers.get("content-type"))
                advertised_length = integer_header(metadata.headers.get("content-length"))
                if self.maximum_download_bytes is not None and advertised_length is not None:
                    if advertised_length > self.maximum_download_bytes:
                        raise DownloadTooLarge(self.maximum_download_bytes)
                remote_filename = filename_from_response(
                    metadata.headers,
                    metadata.final_url,
                    suggested_filename,
                    declared_mime,
                )
                if fixed_destination is not None:
                    destination = fixed_destination
                else:
                    assert destination_directory is not None
                    destination = collision_safe_destination(destination_directory, remote_filename)
                ensure_within_archive(root, destination)
                part = part_path_for(destination)
                if part.exists():
                    raise DestinationConflict(
                        f"A staged download already occupies {part.name}; run recovery before retrying.",
                        retryable=True,
                    )
                reservation_size = advertised_length if advertised_length is not None else (expected_length or 0)
                with DISK_RESERVATIONS.reserve(
                    destination,
                    reservation_size,
                    self.minimum_free_space_bytes,
                ) as grow_reservation:
                    check_free_space(destination, self.minimum_free_space_bytes, reservation_size)
                    handle = part.open("xb")
                    part_owned = True
                    with handle:
                        while True:
                            if cancellation_requested and cancellation_requested():
                                raise DownloadInterrupted("Download cancellation was requested.")
                            try:
                                chunk = response.read(self.chunk_size)
                            except (http.client.IncompleteRead, OSError, TimeoutError, ConnectionError) as exc:
                                raise NetworkDownloadError(
                                    network_error_message(exc),
                                    retryable=True,
                                    code="stream_interrupted",
                                ) from None
                            if not chunk:
                                break
                            next_count = byte_count + len(chunk)
                            if advertised_length is not None and next_count > advertised_length:
                                raise ContentLengthMismatch(advertised_length, next_count)
                            if self.maximum_download_bytes is not None and next_count > self.maximum_download_bytes:
                                raise DownloadTooLarge(self.maximum_download_bytes)
                            grow_reservation(next_count, written_bytes=byte_count)
                            check_free_space(destination, self.minimum_free_space_bytes, len(chunk))
                            handle.write(chunk)
                            digest.update(chunk)
                            byte_count = next_count
                            grow_reservation(byte_count, written_bytes=byte_count)
                        handle.flush()
                        os.fsync(handle.fileno())

            assert metadata is not None and part is not None
            transfer_expected = advertised_length if advertised_length is not None else expected_length
            if transfer_expected is not None and byte_count != transfer_expected:
                raise ContentLengthMismatch(transfer_expected, byte_count)
            received_hash = digest.hexdigest()
            normalized_expected_hash = expected_sha256.strip().casefold()
            if normalized_expected_hash and received_hash != normalized_expected_hash:
                raise DownloadHashMismatch(normalized_expected_hash, received_hash)
            validation = validate_file(
                part,
                declared_mime,
                transfer_expected,
                expected_mime_type=expected_mime_type,
                logical_filename=(fixed_destination.name if fixed_destination else remote_filename),
            )
            if not validation.valid:
                raise DownloadValidationError(validation.details, validation=validation)

            destination = fixed_destination or (part.parent / part.name.removesuffix(".part"))
            if correct_extension and validation.detection.extension:
                corrected_name = filename_with_extension(destination.name, validation.detection.extension)
                if corrected_name != destination.name:
                    corrected = destination.with_name(corrected_name)
                    destination = (
                        collision_safe_destination(destination.parent, corrected.name)
                        if allow_collision_suffix
                        else corrected
                    )
            ensure_within_archive(root, destination)
            if destination.exists():
                if _existing_file_matches(destination, received_hash, byte_count):
                    part.unlink(missing_ok=True)
                    part_owned = False
                    return DownloadResult(
                        source_url=url,
                        final_url=metadata.final_url,
                        path=destination,
                        relative_path=stored_relative_path(root, destination),
                        filename=destination.name,
                        remote_filename=remote_filename,
                        byte_count=byte_count,
                        expected_length=transfer_expected,
                        declared_mime_type=declared_mime,
                        detected_mime_type=validation.detection.mime_type,
                        sha256=received_hash,
                        validation=validation,
                        etag=metadata.headers.get("etag", ""),
                        last_modified=metadata.headers.get("last-modified", ""),
                        response_metadata=metadata.headers,
                        redirects=metadata.redirects,
                        skipped=True,
                    )
                raise DestinationConflict(
                    f"Refusing to overwrite an existing file with different bytes: {destination}"
                )
            atomic_promote_no_replace(part, destination)
            part = None
            part_owned = False
            return DownloadResult(
                source_url=url,
                final_url=metadata.final_url,
                path=destination,
                relative_path=stored_relative_path(root, destination),
                filename=destination.name,
                remote_filename=remote_filename,
                byte_count=byte_count,
                expected_length=transfer_expected,
                declared_mime_type=declared_mime,
                detected_mime_type=validation.detection.mime_type,
                sha256=received_hash,
                validation=validation,
                etag=metadata.headers.get("etag", ""),
                last_modified=metadata.headers.get("last-modified", ""),
                response_metadata=metadata.headers,
                redirects=metadata.redirects,
            )
        except FileExistsError as exc:
            if part is not None and part_owned:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
            raise DestinationConflict(
                "A download staging or destination path was claimed concurrently.",
                retryable=True,
            ) from exc
        except OSError as exc:
            if part is not None and part_owned:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
            if exc.errno == errno.ENOSPC:
                raise LowDiskSpace("The filesystem ran out of free space during download.") from exc
            raise DownloadError(
                f"Local filesystem operation failed: {type(exc).__name__}.",
                code="local_io_error",
            ) from exc
        except BaseException:
            if part is not None and part_owned:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def atomic_promote_no_replace(part: str | Path, destination: str | Path) -> None:
    """Atomically expose staged bytes without replacing an existing file."""

    source = Path(part)
    target = Path(destination)
    if target.exists():
        raise DestinationConflict(f"Destination already exists: {target}")
    try:
        os.link(source, target)
    except FileExistsError:
        raise DestinationConflict(f"Destination already exists: {target}") from None
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            raise
        if os.name != "nt":
            raise DownloadError(
                "Filesystem cannot provide atomic no-overwrite promotion.",
                code="atomic_promotion_unavailable",
            ) from exc
        try:
            os.rename(source, target)
        except FileExistsError:
            raise DestinationConflict(f"Destination already exists: {target}") from None
    else:
        source.unlink()
    _fsync_directory(target.parent)


def retry_delay_seconds(error: DownloadError, attempt: int, policy: RetryPolicy | None = None) -> float:
    selected = policy or RetryPolicy()
    exponential = min(
        selected.maximum_delay_seconds,
        selected.base_delay_seconds * (2 ** max(0, attempt - 1)),
    )
    retry_after = float(error.retry_after_seconds or 0)
    return max(exponential, min(3600.0, retry_after))


def retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, min(3600, int(value.strip())))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(
                0,
                min(3600, int((target - datetime.now(timezone.utc)).total_seconds())),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def filename_from_response(
    headers: Mapping[str, str],
    final_url: str,
    suggested_filename: str = "",
    mime_type: str = "",
) -> str:
    disposition = headers.get("content-disposition", "")
    filename = ""
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename() or ""
    if not filename:
        filename = unquote(Path(urlsplit(final_url).path).name)
    if not filename:
        filename = suggested_filename
    filename = sanitize_windows_filename(filename, fallback="document")
    extension = _extension_hint(mime_type)
    if extension and not has_compatible_extension(filename, extension):
        if not Path(filename).suffix:
            filename = f"{filename}{extension}"
    return filename


def safe_response_headers(headers) -> dict[str, str]:  # noqa: ANN001
    return {
        str(key).casefold(): str(value)[:4096]
        for key, value in headers.items()
        if str(key).casefold() in SAFE_RESPONSE_HEADERS
    }


def integer_header(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
        return parsed if parsed >= 0 else None
    except ValueError:
        return None


def check_free_space(path: str | Path, minimum: int, required: int = 0) -> None:
    target = Path(path)
    free = _free_bytes(target)
    if free - max(0, int(required)) < max(0, int(minimum)):
        raise LowDiskSpace("Download paused before crossing the configured free-space floor.")


def current_free_space(path: str | Path) -> int:
    return _free_bytes(Path(path))


def network_error_message(error: BaseException) -> str:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed."
    if isinstance(reason, ssl.SSLError):
        return "TLS negotiation failed."
    if isinstance(reason, socket.gaierror):
        return "DNS resolution failed."
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "Network request timed out."
    if isinstance(reason, ConnectionRefusedError):
        return "Remote connection was refused."
    if isinstance(reason, ConnectionResetError):
        return "Remote connection was reset."
    if isinstance(reason, http.client.IncompleteRead):
        return "Remote response ended before the advertised payload was complete."
    if isinstance(reason, http.client.RemoteDisconnected):
        return "Remote server closed the connection without a complete response."
    if isinstance(reason, OSError):
        return f"Network operating-system error: {type(reason).__name__}."
    return f"Network request failed: {type(reason).__name__}."


def _normalize_allowed_hosts(hosts: Collection[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for raw in hosts:
        host = str(raw or "").strip().casefold().rstrip(".")
        if not host:
            continue
        if "://" in host or "/" in host or "@" in host:
            raise ValueError(f"Allowed host must be a hostname, not a URL: {raw!r}")
        normalized.add(host)
    if not normalized:
        raise ValueError("At least one expected download host is required")
    return frozenset(normalized)


def _host_is_allowed(host: str, allowed_hosts: Collection[str]) -> bool:
    for allowed in allowed_hosts:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == allowed:
            return True
    return False


def _safe_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).strip()
        if not name or "\r" in name or "\n" in name or ":" in name:
            raise ValueError("Invalid HTTP request header name")
        if name.casefold() in {"host", "proxy-authorization", "content-length"}:
            raise ValueError(f"Caller may not override the {name} header")
        text = str(value)
        if "\r" in text or "\n" in text:
            raise ValueError("Invalid HTTP request header value")
        result[name] = text
    return result


def _filesystem_key(path: Path) -> str:
    resolved = path.resolve(strict=False)
    return str(resolved.anchor or resolved)


def _free_bytes(path: Path) -> int:
    target = path if path.exists() and path.is_dir() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return int(shutil.disk_usage(target).free)


def _extension_hint(mime_type: str) -> str:
    from .file_types import extension_for_mime_type

    return extension_for_mime_type(mime_type)


def _existing_file_matches(path: Path, expected_hash: str, expected_size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == expected_size and sha256_file(path) == expected_hash
    except OSError:
        return False


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


__all__ = [
    "ContentLengthMismatch",
    "DEFAULT_USER_AGENT",
    "DISK_RESERVATIONS",
    "DestinationConflict",
    "DiskReservationManager",
    "DownloadError",
    "DownloadHashMismatch",
    "DownloadInterrupted",
    "DownloadResult",
    "DownloadTooLarge",
    "DownloadValidationError",
    "Downloader",
    "LowDiskSpace",
    "LowDiskSpaceError",
    "NetworkDownloadError",
    "RetryPolicy",
    "SafeHTTPClient",
    "UnsafeDownloadTarget",
    "atomic_promote_no_replace",
    "check_free_space",
    "current_free_space",
    "filename_from_response",
    "integer_header",
    "network_error_message",
    "retry_after_seconds",
    "retry_delay_seconds",
    "safe_response_headers",
]
