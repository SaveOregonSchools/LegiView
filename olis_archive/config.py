"""Runtime configuration with conservative, cross-platform defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from . import __version__


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
BYTES_PER_GB = 1024**3
DEFAULT_MINIMUM_FREE_SPACE_GB = 5.0
MAXIMUM_FREE_SPACE_BYTES = 10**15
MAXIMUM_FREE_SPACE_GB = MAXIMUM_FREE_SPACE_BYTES / BYTES_PER_GB
ODATA_BASE_URL = "https://api.oregonlegislature.gov/odata/odataservice.svc/"
OLIS_BASE_URL = "https://olis.oregonlegislature.gov/"
DEFAULT_USER_AGENT = f"LegiView/{__version__} (+https://www.saveoregonschools.com/)"
DEFAULT_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "olis.oregonlegislature.gov",
        "www.oregonlegislature.gov",
    }
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _positive_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _load_simple_env(path: Path) -> Path | None:
    """Load an optional .env without overriding the caller's environment."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve(strict=False)
    if not candidate.is_file():
        return None
    for raw_line in candidate.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ.setdefault(name, value)
    return candidate.resolve(strict=False)


def _resolve_path(value: str | Path, *, relative_to: Path) -> Path:
    """Normalize a path without allowing the process CWD to choose its base."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = relative_to / path
    return path.resolve(strict=False)


def _normalize_configured_path(value: str | Path, *, name: str) -> str:
    """Return a stable value suitable for environment or settings storage."""

    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} is required")
    path = Path(text).expanduser()
    if path.is_absolute():
        return str(path.resolve(strict=False))
    # Keep relative settings portable across Windows and POSIX installations.
    return Path(os.path.normpath(str(path))).as_posix()


def _gb_from_legacy_bytes(value: Any, *, source: str) -> tuple[float, int]:
    byte_value = _positive_int(
        value,
        "minimum free-space bytes",
        0,
        MAXIMUM_FREE_SPACE_BYTES,
    )
    LOGGER.warning(
        "%s uses deprecated minimum_free_space_bytes; configure "
        "minimum_free_space_gb (LEGIVIEW_MIN_FREE_SPACE_GB) instead.",
        source,
    )
    return byte_value / BYTES_PER_GB, byte_value


def _bytes_from_gb(value: float) -> int:
    return int(value * BYTES_PER_GB)


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    archive_root: Path
    project_root: Path = PROJECT_ROOT
    database_path_configured: str | None = None
    archive_root_configured: str | None = None
    request_timeout: float = 30.0
    odata_worker_count: int = 1
    download_worker_count: int = 2
    html_request_concurrency: int = 1
    minimum_free_space_gb: float = DEFAULT_MINIMUM_FREE_SPACE_GB
    # Kept as a concrete derived field so existing Phase 1 downloader call
    # sites and direct AppConfig constructions remain compatible.
    minimum_free_space_bytes: int | None = None
    inter_request_delay: float = 0.25
    odata_base_url: str = ODATA_BASE_URL
    olis_base_url: str = OLIS_BASE_URL
    user_agent: str = DEFAULT_USER_AGENT
    host: str = "127.0.0.1"
    # Dedicated loopback port used by both direct development and the supplied
    # one-worker Gunicorn deployment. It remains configurable for hosts where
    # this port is already assigned.
    port: int = 5055
    debug: bool = False

    def __post_init__(self) -> None:
        bind_host = str(self.host or "").strip()
        if bind_host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "LegiView's web bind host must be a loopback address "
                "(127.0.0.1, localhost, or ::1)"
            )
        port = _positive_int(self.port, "port", 1, 65535)
        project_root = _resolve_path(self.project_root, relative_to=PROJECT_ROOT)
        configured_database = _normalize_configured_path(
            self.database_path_configured
            if self.database_path_configured is not None
            else self.database_path,
            name="database path",
        )
        configured_archive = _normalize_configured_path(
            self.archive_root_configured
            if self.archive_root_configured is not None
            else self.archive_root,
            name="archive root",
        )
        database_path = _resolve_path(configured_database, relative_to=project_root)
        archive_root = _resolve_path(configured_archive, relative_to=project_root)
        minimum_gb = _positive_float(
            self.minimum_free_space_gb,
            "minimum free-space GB",
            0,
            MAXIMUM_FREE_SPACE_GB,
        )

        if self.minimum_free_space_bytes is None:
            minimum_bytes = _bytes_from_gb(minimum_gb)
        else:
            minimum_bytes = _positive_int(
                self.minimum_free_space_bytes,
                "minimum free-space bytes",
                0,
                MAXIMUM_FREE_SPACE_BYTES,
            )
            calculated = _bytes_from_gb(minimum_gb)
            if calculated != minimum_bytes:
                if minimum_gb == DEFAULT_MINIMUM_FREE_SPACE_GB:
                    # Compatibility for Phase 1 callers that construct
                    # AppConfig(minimum_free_space_bytes=...).
                    minimum_gb = minimum_bytes / BYTES_PER_GB
                else:
                    raise ValueError(
                        "minimum free-space GB and byte values do not agree"
                    )

        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "database_path_configured", configured_database)
        object.__setattr__(self, "archive_root", archive_root)
        object.__setattr__(self, "archive_root_configured", configured_archive)
        object.__setattr__(self, "minimum_free_space_gb", minimum_gb)
        object.__setattr__(self, "minimum_free_space_bytes", minimum_bytes)
        object.__setattr__(self, "host", bind_host)
        object.__setattr__(self, "port", port)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "AppConfig":
        selected_env = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
        if not selected_env.expanduser().is_absolute():
            selected_env = PROJECT_ROOT / selected_env
        loaded_env = _load_simple_env(selected_env)

        raw_project_root = os.environ.get("LEGIVIEW_PROJECT_ROOT")
        if raw_project_root is None:
            project_root = PROJECT_ROOT
        else:
            project_base = loaded_env.parent if loaded_env is not None else PROJECT_ROOT
            project_root = _resolve_path(raw_project_root, relative_to=project_base)

        raw_database_path = os.environ.get(
            "LEGIVIEW_DATABASE_PATH", "data/legiview.sqlite3"
        )
        raw_archive_root = os.environ.get("LEGIVIEW_ARCHIVE_ROOT", "archive")

        if "LEGIVIEW_MIN_FREE_SPACE_GB" in os.environ:
            minimum_gb = _positive_float(
                os.environ["LEGIVIEW_MIN_FREE_SPACE_GB"],
                "minimum free-space GB",
                0,
                MAXIMUM_FREE_SPACE_GB,
            )
            minimum_bytes = _bytes_from_gb(minimum_gb)
        elif "LEGIVIEW_MIN_FREE_SPACE_BYTES" in os.environ:
            minimum_gb, minimum_bytes = _gb_from_legacy_bytes(
                os.environ["LEGIVIEW_MIN_FREE_SPACE_BYTES"],
                source="Environment configuration",
            )
        else:
            minimum_gb = DEFAULT_MINIMUM_FREE_SPACE_GB
            minimum_bytes = _bytes_from_gb(minimum_gb)

        return cls(
            project_root=project_root,
            database_path=Path(raw_database_path),
            database_path_configured=_normalize_configured_path(
                raw_database_path, name="database path"
            ),
            archive_root=Path(raw_archive_root),
            archive_root_configured=_normalize_configured_path(
                raw_archive_root, name="archive root"
            ),
            request_timeout=_positive_float(
                os.environ.get("LEGIVIEW_REQUEST_TIMEOUT", 30), "request timeout", 1, 600
            ),
            odata_worker_count=_positive_int(
                os.environ.get("LEGIVIEW_ODATA_WORKERS", 1), "OData workers", 1, 4
            ),
            download_worker_count=_positive_int(
                os.environ.get("LEGIVIEW_DOWNLOAD_WORKERS", 2), "download workers", 1, 8
            ),
            html_request_concurrency=_positive_int(
                os.environ.get("LEGIVIEW_HTML_CONCURRENCY", 1), "HTML concurrency", 1, 2
            ),
            minimum_free_space_gb=minimum_gb,
            minimum_free_space_bytes=minimum_bytes,
            inter_request_delay=_positive_float(
                os.environ.get("LEGIVIEW_INTER_REQUEST_DELAY", 0.25),
                "inter-request delay",
                0,
                60,
            ),
            odata_base_url=os.environ.get("LEGIVIEW_ODATA_BASE_URL", ODATA_BASE_URL).rstrip("/") + "/",
            olis_base_url=os.environ.get("LEGIVIEW_OLIS_BASE_URL", OLIS_BASE_URL).rstrip("/") + "/",
            host=os.environ.get("LEGIVIEW_HOST", "127.0.0.1"),
            port=_positive_int(os.environ.get("LEGIVIEW_PORT", 5055), "port", 1, 65535),
            debug=_env_bool("LEGIVIEW_DEBUG"),
        )

    def with_project_root(self, value: str | Path) -> "AppConfig":
        """Apply a bootstrap project-root override outside persisted settings."""

        project_root = _resolve_path(value, relative_to=PROJECT_ROOT)
        return replace(self, project_root=project_root)

    def resolve_path(self, value: str | Path) -> Path:
        """Resolve a configured path against the effective project root."""

        return _resolve_path(value, relative_to=self.project_root)

    def with_settings(self, values: Mapping[str, Any]) -> "AppConfig":
        """Overlay validated persistent UI settings onto environment defaults."""

        updates: dict[str, Any] = {}
        if values.get("archive_root"):
            configured = _normalize_configured_path(
                values["archive_root"], name="archive root"
            )
            updates["archive_root_configured"] = configured
            updates["archive_root"] = self.resolve_path(configured)
        numeric = {
            "request_timeout": (_positive_float, "request timeout", 1, 600),
            "odata_worker_count": (_positive_int, "OData workers", 1, 4),
            "download_worker_count": (_positive_int, "download workers", 1, 8),
            "html_request_concurrency": (_positive_int, "HTML concurrency", 1, 2),
            "inter_request_delay": (_positive_float, "inter-request delay", 0, 60),
        }
        for key, (validator, label, low, high) in numeric.items():
            if key in values and values[key] not in (None, ""):
                updates[key] = validator(values[key], label, low, high)

        if values.get("minimum_free_space_gb") not in (None, ""):
            minimum_gb = _positive_float(
                values["minimum_free_space_gb"],
                "minimum free-space GB",
                0,
                MAXIMUM_FREE_SPACE_GB,
            )
            updates["minimum_free_space_gb"] = minimum_gb
            updates["minimum_free_space_bytes"] = _bytes_from_gb(minimum_gb)
        elif values.get("minimum_free_space_bytes") not in (None, ""):
            minimum_gb, minimum_bytes = _gb_from_legacy_bytes(
                values["minimum_free_space_bytes"],
                source="Persisted application settings",
            )
            updates["minimum_free_space_gb"] = minimum_gb
            updates["minimum_free_space_bytes"] = minimum_bytes
        return replace(self, **updates)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        result["project_root"] = str(self.project_root)
        result["database_path"] = str(self.database_path)
        result["archive_root"] = str(self.archive_root)
        return result


SETTING_FIELDS = (
    "archive_root",
    "request_timeout",
    "odata_worker_count",
    "download_worker_count",
    "html_request_concurrency",
    "minimum_free_space_gb",
    "inter_request_delay",
)


__all__ = [
    "AppConfig",
    "BYTES_PER_GB",
    "DEFAULT_ALLOWED_DOWNLOAD_HOSTS",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_MINIMUM_FREE_SPACE_GB",
    "DEFAULT_USER_AGENT",
    "ODATA_BASE_URL",
    "OLIS_BASE_URL",
    "PROJECT_ROOT",
    "SETTING_FIELDS",
]
