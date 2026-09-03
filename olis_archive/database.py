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
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a connection in an explicit commit-or-rollback transaction."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

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

    def _apply_migration(self, migration: Migration) -> None:
        with self.transaction() as connection:
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
                return

            for statement in _sql_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
            connection.execute(f"PRAGMA user_version = {migration.version:d}")


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

