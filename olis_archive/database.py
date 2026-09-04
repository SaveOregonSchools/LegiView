"""SQLite connection and migration management for LegiView.

The application deliberately opens short-lived connections.  This works well for
Flask requests and background workers, avoids sharing sqlite connection objects
between threads, and lets SQLite's WAL mode coordinate readers with the single
writer.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
import threading
from typing import Iterator, Sequence


_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_(?P<name>.+)\.sql$")


class MigrationError(RuntimeError):
    """Raised when the on-disk migration history is unsafe or inconsistent."""


@dataclass(frozen=True)
class Migration:
    """One immutable SQL migration discovered on disk."""

    version: int
    name: str
    path: Path
    sql: str
    checksum: str


class Database:
    """Own SQLite connection configuration and apply versioned migrations."""

    def __init__(
        self,
        path: str | Path,
        *,
        migrations_path: str | Path | None = None,
        busy_timeout_ms: int = 15_000,
    ) -> None:
        raw_path = str(path)
        if not raw_path:
            raise ValueError("database path must not be empty")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self.path = raw_path
        self.migrations_path = Path(migrations_path or Path(__file__).with_name("migrations"))
        self.busy_timeout_ms = busy_timeout_ms
        # Page-sized historical ingestion deliberately groups many existing
        # StorageService calls into one transaction.  Those calls still use
        # ``Database.transaction()`` themselves, so a thread-local unit of work
        # lets nested calls join the bounded outer transaction without sharing
        # connections across worker threads.
        self._local = threading.local()

    @property
    def is_memory(self) -> bool:
        return self.path == ":memory:" or self.path.startswith("file::memory:")

    def connect(self) -> sqlite3.Connection:
        """Open one configured connection.

        Callers should close the connection, normally with :meth:`connection` or
        :meth:`transaction`.  Each worker should use its own connection.
        """

        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield an explicit transaction, joining a thread-local outer unit.

        Joining is intentional rather than savepoint-based: the historical
        collector wraps exactly one bounded source page, and all storage writes
        for that page must either commit together or roll back together.  The
        outermost context remains responsible for commit/rollback.
        """

        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._local.transaction_connection = connection
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                del self._local.transaction_connection

    def initialize(self) -> int:
        """Create the database if needed and apply every unapplied migration.

        Applied migration checksums are immutable.  Editing a migration that has
        already run raises :class:`MigrationError` instead of silently leaving
        different installations with different schemas.
        """

        if not self.is_memory:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

        migrations = self._discover_migrations()
        with self.connection() as connection:
            # journal_mode persists in the database.  It cannot be changed while a
            # transaction is active, so establish it before migrating.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

        self._validate_migration_history(migrations)
        for migration in migrations:
            self._apply_migration(migration)
        return self.schema_version()

    def schema_version(self) -> int:
        with self.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if exists is None:
                return 0
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

    def latest_schema_version(self) -> int:
        """Return the greatest packaged migration version without opening SQLite."""

        migrations = self._discover_migrations()
        return migrations[-1].version

    def migration_manifest_is_current(self) -> bool:
        """Verify an exact, fully applied migration manifest without writing."""

        migrations = self._discover_migrations()
        with self.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if exists is None:
                return False
            rows = connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        if len(rows) != len(migrations):
            return False
        return all(
            int(row["version"]) == migration.version
            and str(row["name"]) == migration.name
            and str(row["checksum"]) == migration.checksum
            for row, migration in zip(rows, migrations, strict=True)
        )

    def foreign_key_violations(self) -> list[sqlite3.Row]:
        """Return SQLite's current foreign-key integrity report."""

        with self.connection() as connection:
            return list(connection.execute("PRAGMA foreign_key_check").fetchall())

    def table_names(self) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            return [str(row["name"]) for row in rows]

    def _discover_migrations(self) -> Sequence[Migration]:
        if not self.migrations_path.is_dir():
            raise MigrationError(f"migration directory does not exist: {self.migrations_path}")

        migrations: list[Migration] = []
        seen_versions: set[int] = set()
        for path in sorted(self.migrations_path.glob("*.sql")):
            match = _MIGRATION_NAME.match(path.name)
            if match is None:
                raise MigrationError(
                    f"invalid migration filename {path.name!r}; expected NNN_name.sql"
                )
            version = int(match.group("version"))
            if version in seen_versions:
                raise MigrationError(f"duplicate migration version: {version}")
            seen_versions.add(version)
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                Migration(
                    version=version,
                    name=match.group("name"),
                    path=path,
                    sql=sql,
                    checksum=sha256(sql.encode("utf-8")).hexdigest(),
                )
            )
        if not migrations:
            raise MigrationError(f"no SQL migrations found in {self.migrations_path}")
        return sorted(migrations, key=lambda migration: migration.version)

    def _validate_migration_history(self, migrations: Sequence[Migration]) -> None:
        """Reject altered, gapped, or newer migration histories before applying."""

        with self.connection() as connection:
            rows = connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        if len(rows) > len(migrations):
            newest = int(rows[-1]["version"])
            raise MigrationError(
                f"database schema version {newest} is newer than this LegiView build"
            )
        expected_prefix = migrations[: len(rows)]
        for position, (row, migration) in enumerate(
            zip(rows, expected_prefix, strict=True), start=1
        ):
            version = int(row["version"])
            if version != migration.version:
                raise MigrationError(
                    "database migration history is not a contiguous packaged prefix "
                    f"at position {position}: found version {version}, expected {migration.version}"
                )
            if str(row["name"]) != migration.name:
                raise MigrationError(
                    f"migration {version} name differs from the packaged migration"
                )
            if str(row["checksum"]) != migration.checksum:
                raise MigrationError(
                    f"migration {version} ({migration.name}) was modified after it was applied"
                )

    def _apply_migration(self, migration: Migration) -> None:
        # SQLite cannot rebuild a referenced table while foreign-key enforcement
        # is enabled.  A migration may nevertheless need such a rebuild (for
        # example, to expand a CHECK constraint).  Disable enforcement *before*
        # beginning the migration transaction, then run a full integrity check
        # before committing.  Connections used by application code continue to
        # enable foreign keys normally in ``connect``.
        connection = self.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            applied = connection.execute(
                "SELECT name, checksum FROM schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if applied is not None:
                if applied["checksum"] != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.name}) was modified after "
                        "it was applied"
                    )
                connection.rollback()
                return

            for statement in _sql_statements(migration.sql):
                connection.execute(statement)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                sample = ", ".join(
                    f"{row[0]} row {row[1]} -> {row[2]}"
                    for row in violations[:5]
                )
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) introduced "
                    f"{len(violations)} foreign-key violation(s): {sample}"
                )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
            connection.execute(f"PRAGMA user_version = {migration.version:d}")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            # This is mostly documentary because the connection is about to be
            # closed, but it prevents a future refactor from accidentally reusing
            # it with enforcement disabled.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.close()


def _sql_statements(script: str) -> Iterator[str]:
    """Split a migration without breaking SQL strings, comments, or triggers."""

    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                yield statement
    if pending.strip():
        raise MigrationError("migration ends with an incomplete SQL statement")


def initialize_database(path: str | Path) -> Database:
    """Initialize *path* and return its configured :class:`Database`."""

    database = Database(path)
    database.initialize()
    return database


@contextmanager
def get_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Small compatibility helper for scripts that only need a connection."""

    with Database(path).connection() as connection:
        yield connection


__all__ = [
    "Database",
    "Migration",
    "MigrationError",
    "get_connection",
    "initialize_database",
]
