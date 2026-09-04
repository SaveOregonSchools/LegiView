"""Shared application bootstrap for the Flask UI and command-line tools."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .config import AppConfig
from .database import Database
from .services.archive_paths import ensure_archive_root_owned, validate_archive_root_candidate
from .services.collection import CollectionService
from .services.recovery import cleanup_stale_parts
from .services.storage import StorageService


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    """The process-local services built around one durable SQLite database."""

    config: AppConfig
    database: Database
    storage: StorageService
    collection: CollectionService
    instance_lock: "InstanceLock | None" = None


class InstanceAlreadyRunning(RuntimeError):
    """Another mutating LegiView process owns this database."""


class InstanceLock:
    """Advisory cross-process lock guarding startup recovery and workers."""

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle = handle

    @classmethod
    def acquire(cls, database_path: str | Path) -> "InstanceLock":
        raw = str(database_path)
        if raw == ":memory:" or raw.startswith("file::memory:"):
            raise ValueError("an in-memory database cannot be used by the background application")
        database = Path(raw).expanduser().resolve(strict=False)
        lock_path = database.with_name(database.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the primary target
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise InstanceAlreadyRunning(
                f"Another LegiView process is already using {database}."
            ) from exc
        return cls(lock_path, handle)

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the primary target
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


def load_effective_config(
    config: AppConfig | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    initialize: bool = True,
) -> tuple[AppConfig, Database, StorageService]:
    """Load environment defaults, initialize SQLite, then apply saved settings."""

    base = _bootstrap_config(config, overrides=overrides)

    database = Database(base.database_path)
    if initialize:
        database.initialize()
    storage = StorageService(database, initialize=False)
    effective = base.with_settings(storage.get_settings())
    return effective, database, storage


def _bootstrap_config(
    config: AppConfig | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Resolve the database-bearing configuration without opening SQLite."""

    base = config or AppConfig.from_env()
    if overrides:
        if overrides.get("project_root") is not None:
            base = base.with_project_root(overrides["project_root"])
        runtime_values = {
            key: value
            for key, value in overrides.items()
            if key in base.snapshot()
            and key not in {"database_path", "project_root", "archive_root_configured"}
        }
        if runtime_values:
            base = base.with_settings(runtime_values)
        if overrides.get("database_path") is not None:
            from dataclasses import replace

            base = replace(
                base,
                database_path=Path(overrides["database_path"]),
                database_path_configured=str(overrides["database_path"]),
            )

    return base


def _database_needs_migration(database: Database) -> bool:
    """Check whether bootstrap would mutate an on-disk database schema."""

    if database.is_memory:
        return True
    path = Path(database.path).expanduser().resolve(strict=False)
    if not path.is_file():
        return True
    return not database.migration_manifest_is_current()


def build_runtime(
    config: AppConfig | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    normalize_interrupted: bool = True,
    clean_parts: bool = True,
    exclusive: bool = False,
    instance_lock: InstanceLock | None = None,
) -> Runtime:
    """Construct the same collector graph used by both web and CLI entrypoints."""

    bootstrap = _bootstrap_config(config, overrides=overrides)
    bootstrap_database = Database(bootstrap.database_path)
    needs_migration = _database_needs_migration(bootstrap_database)
    lock = instance_lock
    owns_lock = False
    bootstrap_lock: InstanceLock | None = None
    if exclusive and lock is None:
        # Migration and settings bootstrap both touch SQLite. Acquire the same
        # ownership lock used by recovery/workers before either can run so a
        # newly deployed process cannot migrate beneath an older live worker.
        lock = InstanceLock.acquire(bootstrap.database_path)
        owns_lock = True
    elif not exclusive:
        if (
            (needs_migration or normalize_interrupted or clean_parts)
            and not bootstrap_database.is_memory
        ):
            # Read-only commands remain concurrent once the schema is current,
            # but a first-run/upgrade bootstrap is still a mutation and must
            # not migrate beneath a live worker.
            bootstrap_lock = InstanceLock.acquire(bootstrap.database_path)

    try:
        effective, database, storage = load_effective_config(
            bootstrap,
            initialize=exclusive or needs_migration,
        )
        # Establish explicit ownership before any archive-wide startup
        # maintenance or mutating runtime starts. A genuinely read-only
        # runtime validates the path but does not create a directory/marker.
        # Existing Phase 1/2 trees are conservatively recognized and adopted
        # once; arbitrary nonempty directories are rejected.
        if exclusive or normalize_interrupted or clean_parts:
            archive_root = ensure_archive_root_owned(effective.archive_root)
        else:
            archive_root = validate_archive_root_candidate(effective.archive_root)

        if normalize_interrupted:
            normalized = storage.normalize_interrupted_work()
            if any(normalized.values()):
                LOGGER.warning("Recovered interrupted durable work: %s", normalized)

        # A partial file has no trusted completion marker. Remove only .part files
        # beneath the explicitly configured archive root before any worker starts;
        # the associated durable document remains retryable.
        if clean_parts:
            removed = cleanup_stale_parts(archive_root)
            if removed:
                LOGGER.warning(
                    "Removed %d incomplete .part file(s) during startup recovery", len(removed)
                )

        collection = CollectionService(effective, database=database)
        if bootstrap_lock is not None:
            bootstrap_lock.close()
            bootstrap_lock = None
        return Runtime(effective, database, storage, collection, lock)
    except BaseException:
        if bootstrap_lock is not None:
            bootstrap_lock.close()
        if owns_lock and lock is not None:
            lock.close()
        raise


__all__ = [
    "InstanceAlreadyRunning",
    "InstanceLock",
    "Runtime",
    "build_runtime",
    "load_effective_config",
]
