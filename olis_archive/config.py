"""Runtime configuration with conservative source and download defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import os
from pathlib import Path
from typing import Any, Mapping

from . import __version__


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
ODATA_BASE_URL = "https://api.oregonlegislature.gov/odata/odataservice.svc/"
OLIS_BASE_URL = "https://olis.oregonlegislature.gov/"
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


def _load_simple_env(path: Path) -> None:
    """Load an optional .env without overriding the caller's environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ.setdefault(name, value)


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    archive_root: Path
    request_timeout: float = 30.0
    odata_worker_count: int = 1
    download_worker_count: int = 2
    html_request_concurrency: int = 1
    minimum_free_space_bytes: int = 1024 * 1024 * 1024
    inter_request_delay: float = 0.25
    odata_base_url: str = ODATA_BASE_URL
    olis_base_url: str = OLIS_BASE_URL
    user_agent: str = f"OLISArchive/{__version__} (+https://www.saveoregonschools.com/)"
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = False

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "AppConfig":
        _load_simple_env(env_file or PROJECT_ROOT / ".env")
        return cls(
            database_path=Path(
                os.environ.get("LEGIVIEW_DATABASE_PATH", DEFAULT_DATA_ROOT / "legiview.sqlite3")
            ).expanduser(),
            archive_root=Path(
                os.environ.get("LEGIVIEW_ARCHIVE_ROOT", DEFAULT_DATA_ROOT / "archive")
            ).expanduser(),
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
            minimum_free_space_bytes=_positive_int(
                os.environ.get("LEGIVIEW_MIN_FREE_SPACE_BYTES", 1024 * 1024 * 1024),
                "minimum free-space bytes",
                0,
                10**15,
            ),
            inter_request_delay=_positive_float(
                os.environ.get("LEGIVIEW_INTER_REQUEST_DELAY", 0.25),
                "inter-request delay",
                0,
                60,
            ),
            odata_base_url=os.environ.get("LEGIVIEW_ODATA_BASE_URL", ODATA_BASE_URL).rstrip("/") + "/",
            olis_base_url=os.environ.get("LEGIVIEW_OLIS_BASE_URL", OLIS_BASE_URL).rstrip("/") + "/",
            host=os.environ.get("LEGIVIEW_HOST", "127.0.0.1"),
            port=_positive_int(os.environ.get("LEGIVIEW_PORT", 5000), "port", 1, 65535),
            debug=_env_bool("LEGIVIEW_DEBUG"),
        )

    def with_settings(self, values: Mapping[str, Any]) -> "AppConfig":
        """Overlay validated persistent UI settings onto environment defaults."""
        updates: dict[str, Any] = {}
        if values.get("archive_root"):
            updates["archive_root"] = Path(str(values["archive_root"])).expanduser()
        numeric = {
            "request_timeout": (_positive_float, "request timeout", 1, 600),
            "odata_worker_count": (_positive_int, "OData workers", 1, 4),
            "download_worker_count": (_positive_int, "download workers", 1, 8),
            "html_request_concurrency": (_positive_int, "HTML concurrency", 1, 2),
            "minimum_free_space_bytes": (_positive_int, "minimum free-space bytes", 0, 10**15),
            "inter_request_delay": (_positive_float, "inter-request delay", 0, 60),
        }
        for key, (validator, label, low, high) in numeric.items():
            if key in values and values[key] not in (None, ""):
                updates[key] = validator(values[key], label, low, high)
        return replace(self, **updates)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        result["database_path"] = str(self.database_path)
        result["archive_root"] = str(self.archive_root)
        return result


SETTING_FIELDS = (
    "archive_root",
    "request_timeout",
    "odata_worker_count",
    "download_worker_count",
    "html_request_concurrency",
    "minimum_free_space_bytes",
    "inter_request_delay",
)

