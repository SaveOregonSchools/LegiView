"""Transactional persistence services for OLIS collection and downloads.

This module accepts normalized dictionaries from the source clients.  It does
not know how to fetch or parse OLIS data, and it performs no filesystem work.
Stable source identities are upserted without deleting records that disappear
from a later source response.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256 as hash_sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from ..database import Database


RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "completed_with_errors",
        "failed",
        "paused",
        "canceled",
        "interrupted",
    }
)
RUN_ITEM_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "skipped",
        "failed_retryable",
        "failed_terminal",
        "paused",
        "canceled",
        "interrupted",
    }
)
DOWNLOAD_STATUSES = frozenset(
    {
        "discovered",
        "queued",
        "downloading",
        "downloaded",
        "failed_retryable",
        "failed_terminal",
        "paused_low_space",
        "interrupted",
        "missing_local",
        "changed_remote",
        "not_applicable",
    }
)
RETRYABLE_DOWNLOAD_STATUSES = frozenset(
    {"discovered", "queued", "failed_retryable", "paused_low_space", "interrupted", "missing_local", "changed_remote"}
)
DOCUMENT_KINDS = frozenset(
    {
        "public_testimony",
        "legacy_testimony",
        "committee_presentation",
        "floor_letter",
        "committee_document_other",
        "unknown",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    """Return a sortable UTC RFC-3339 timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_timestamp(value: str | datetime | None = None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid RFC-3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
        except (TypeError, ValueError):
            pass
        else:
            return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _db_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _required_text(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _record(record: Mapping[str, Any] | None, fields: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record or {})
    result.update(fields)
    return result


def _apply_aliases(values: dict[str, Any], aliases: Mapping[str, str]) -> None:
    for source, target in aliases.items():
        if target not in values and source in values:
            values[target] = values[source]


def _filtered(values: Mapping[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    allowed_set = set(allowed)
    return {key: _db_scalar(value) for key, value in values.items() if key in allowed_set}


def _ident(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _upsert_row(
    connection: sqlite3.Connection,
    table: str,
    key_columns: Sequence[str],
    values: Mapping[str, Any],
    *,
    result_column: str = "id",
    preserve_on_conflict: Iterable[str] = (),
) -> Any:
    table = _ident(table)
    columns = [_ident(column) for column in values]
    if not columns:
        raise ValueError("upsert needs at least one value")
    for key in key_columns:
        if key not in values:
            raise ValueError(f"upsert key {key!r} is missing")
    preserve = set(preserve_on_conflict) | set(key_columns)
    updates = [column for column in columns if column not in preserve]
    placeholders = ", ".join("?" for _ in columns)
    if updates:
        conflict_action = "DO UPDATE SET " + ", ".join(
            f"{column} = excluded.{column}" for column in updates
        )
    else:
        conflict_action = "DO NOTHING"
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_columns)}) {conflict_action}",
        tuple(values[column] for column in columns),
    )
    where = " AND ".join(f"{_ident(key)} = ?" for key in key_columns)
    row = connection.execute(
        f"SELECT {_ident(result_column)} AS result FROM {table} WHERE {where}",
        tuple(values[key] for key in key_columns),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive; insert/select are in one transaction
        raise RuntimeError(f"upserted {table} row could not be read back")
    return row["result"]


class StorageService:
    """High-level, idempotent persistence API shared by CLI, UI, and workers."""

    def __init__(
        self,
        database: Database | str | Path,
        *,
        initialize: bool = True,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        if initialize:
            self.database.initialize()

    # -- application settings -------------------------------------------------

    def set_setting(
        self,
        key: str,
        value: Any,
        *,
        updated_by: str | None = None,
        updated_at: str | datetime | None = None,
    ) -> None:
        key = key.strip()
        if not key:
            raise ValueError("setting key is required")
        timestamp = _utc_timestamp(updated_at)
        with self.database.transaction() as connection:
            _upsert_row(
                connection,
                "app_settings",
                ("key",),
                {
                    "key": key,
                    "value_json": _json_text(value),
                    "updated_at": timestamp,
                    "updated_by": updated_by,
                },
                result_column="key",
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def get_settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM app_settings ORDER BY key"
            ).fetchall()
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}

    # -- authoritative reference and bill records ----------------------------

    def upsert_session(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> str:
        source = _record(record, fields)
        session_key = _required_text(source, "session_key").upper()
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "source_session_id", "session_name", "session_type", "session_year",
            "begin_date", "end_date", "source_url", "source_created_at", "source_modified_at",
        }
        values = _filtered(source, allowed)
        values.update(
            session_key=session_key,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            last_synced_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_seen_run_id"] = run_id
        with self.database.transaction() as connection:
            return str(
                _upsert_row(
                    connection,
                    "sessions",
                    ("session_key",),
                    values,
                    result_column="session_key",
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    def upsert_legislator(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(source, {"legislatore_code": "legislator_code", "code": "legislator_code"})
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "legislator_code", "source_legislator_id", "first_name", "middle_name",
            "last_name", "suffix", "display_name", "chamber", "party", "district", "email", "active",
            "source_created_at", "source_modified_at",
        }
        values = _filtered(source, allowed)
        values["session_key"] = _required_text(source, "session_key").upper()
        values["legislator_code"] = _required_text(source, "legislator_code")
        values.update(
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_seen_run_id"] = run_id
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "legislators",
                    ("session_key", "legislator_code"),
                    values,
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    def upsert_committee(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(source, {"code": "committee_code", "name": "committee_name"})
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "committee_code", "source_committee_id", "committee_name",
            "house_of_action", "chamber", "committee_type", "source_created_at", "source_modified_at",
        }
        values = _filtered(source, allowed)
        values["session_key"] = _required_text(source, "session_key").upper()
        values["committee_code"] = _required_text(source, "committee_code")
        values.update(
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_seen_run_id"] = run_id
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "committees",
                    ("session_key", "committee_code"),
                    values,
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    def upsert_bill(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(
            source,
            {
                "bill_title_source": "title_source_field",
                "relating_to_full": "relating_to_full",
                "current_subcommittee": "current_subcommittee_code",
            },
        )
        session_key = _required_text(source, "session_key").upper()
        prefix = _required_text(source, "measure_prefix").upper()
        if prefix not in {"HB", "SB"}:
            raise ValueError("only HB and SB measures are supported")
        number = _required_text(source, "measure_number")
        compact = str(source.get("bill_id_compact") or f"{prefix}{number}").replace(" ", "").upper()
        display = str(source.get("bill_id_display") or f"{prefix} {number}").strip().upper()
        chamber = str(source.get("bill_chamber") or ("House" if prefix == "HB" else "Senate"))
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "measure_id", "measure_prefix", "measure_number", "bill_id_compact",
            "bill_id_display", "bill_chamber", "at_the_request_of", "title_source_field", "bill_title",
            "catchline", "measure_summary", "chapter_number", "effective_date", "vetoed",
            "emergency_clause", "current_version", "current_location", "current_committee_code",
            "current_subcommittee_code", "current_committee_name", "relating_to", "relating_to_clause",
            "relating_to_full", "minority_catchline", "fiscal_impact", "revenue_impact", "lc_number",
            "prefix_meaning", "enacted", "source_url", "source_created_at", "source_modified_at",
        }
        values = _filtered(source, allowed)
        values.update(
            session_key=session_key,
            measure_prefix=prefix,
            measure_number=number,
            bill_id_compact=compact,
            bill_id_display=display,
            bill_chamber=chamber,
            first_collected_at=timestamp,
            last_seen_at=timestamp,
            last_synced_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_collected_run_id"] = run_id
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "bills",
                    ("session_key", "bill_id_compact"),
                    values,
                    preserve_on_conflict=("first_collected_at",),
                )
            )

    def upsert_bill_sponsor(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(
            source,
            {
                "measure_sponsor_id": "source_measure_sponsor_id",
                "source_id": "source_measure_sponsor_id",
                "sponsor_type": "raw_sponsor_type",
                "sponsor_level": "raw_sponsor_level",
                "category": "normalized_category",
                "display_name": "resolved_display_name",
                "kind": "sponsor_kind",
            },
        )
        timestamp = _utc_timestamp(seen_at)
        with self.database.transaction() as connection:
            bill_id = self._bill_id(connection, source)
            allowed = {
                "source_measure_sponsor_id", "raw_sponsor_type", "raw_sponsor_level",
                "normalized_category", "legislator_code", "committee_code", "resolved_display_name",
                "sponsor_kind", "print_order", "pre_session_filed_message", "source_created_at",
                "source_modified_at",
            }
            values = _filtered(source, allowed)
            values["bill_id"] = bill_id
            values["source_measure_sponsor_id"] = _required_text(source, "source_measure_sponsor_id")
            values.setdefault("normalized_category", "unknown")
            values.setdefault("sponsor_kind", "other")
            values.update(
                first_seen_at=timestamp,
                last_seen_at=timestamp,
                raw_json=_json_text(source.get("raw_json", source)),
            )
            if run_id is not None:
                values["last_seen_run_id"] = run_id
            return int(
                _upsert_row(
                    connection,
                    "bill_sponsors",
                    ("bill_id", "source_measure_sponsor_id"),
                    values,
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    def upsert_committee_meeting(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(source, {"meeting_id": "source_meeting_id", "source_id": "source_meeting_id"})
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "source_meeting_id", "committee_id", "committee_code", "committee_name",
            "meeting_date", "location", "meeting_type", "agenda_url", "source_url",
            "source_created_at", "source_modified_at",
        }
        values = _filtered(source, allowed)
        values["session_key"] = _required_text(source, "session_key").upper()
        values["source_meeting_id"] = _required_text(source, "source_meeting_id")
        values.update(
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_seen_run_id"] = run_id
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "committee_meetings",
                    ("session_key", "source_meeting_id"),
                    values,
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    def upsert_committee_agenda_item(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(source, {"agenda_item_id": "source_agenda_item_id", "source_id": "source_agenda_item_id"})
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "source_agenda_item_id", "committee_meeting_id", "bill_id", "measure_id",
            "bill_id_compact", "agenda_order", "agenda_item_type", "description", "source_created_at",
            "source_modified_at",
        }
        values = _filtered(source, allowed)
        values["session_key"] = _required_text(source, "session_key").upper()
        values["source_agenda_item_id"] = _required_text(source, "source_agenda_item_id")
        values.update(
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_seen_run_id"] = run_id
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "committee_agenda_items",
                    ("session_key", "source_agenda_item_id"),
                    values,
                    preserve_on_conflict=("first_seen_at",),
                )
            )

    # -- canonical documents and immutable payload versions ------------------

    def upsert_document(
        self,
        record: Mapping[str, Any] | None = None,
        *,
        seen_at: str | datetime | None = None,
        run_id: int | None = None,
        **fields: Any,
    ) -> int:
        source = _record(record, fields)
        _apply_aliases(
            source,
            {
                "document_id": "source_id",
                "source_document_id": "source_id",
                "source_numeric_id": "source_id",
                "document_title": "title",
                "download_url": "canonical_download_url",
                "position": "testimony_position",
                "on_behalf": "on_behalf_of",
            },
        )
        timestamp = _utc_timestamp(seen_at)
        with self.database.transaction() as connection:
            bill_id = self._bill_id(connection, source)
            bill = connection.execute(
                "SELECT session_key, bill_id_compact FROM bills WHERE id = ?", (bill_id,)
            ).fetchone()
            assert bill is not None
            source_entity_type = _required_text(source, "source_entity_type")
            values = _filtered(source, _DOCUMENT_FIELDS)
            values.update(
                bill_id=bill_id,
                session_key=str(bill["session_key"]),
                bill_id_compact=str(bill["bill_id_compact"]),
                document_kind=str(source.get("document_kind") or "unknown"),
                source_section=str(source.get("source_section") or source_entity_type),
                source_entity_type=source_entity_type,
                source_id=_required_text(source, "source_id"),
                first_seen_at=timestamp,
                last_seen_at=timestamp,
                raw_json=_json_text(source.get("raw_json", source)),
            )
            if run_id is not None:
                values["last_seen_run_id"] = run_id
            if values["document_kind"] not in DOCUMENT_KINDS:
                raise ValueError(f"unknown document_kind: {values['document_kind']!r}")
            return int(
                _upsert_row(
                    connection,
                    "documents",
                    ("session_key", "bill_id_compact", "source_entity_type", "source_id"),
                    values,
                    preserve_on_conflict=_DOCUMENT_DOWNLOAD_FIELDS | {"first_seen_at"},
                )
            )

    def queue_document(self, document_id: int) -> bool:
        """Queue a retryable document without disturbing a valid completed one."""

        placeholders = ", ".join("?" for _ in RETRYABLE_DOWNLOAD_STATUSES)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE documents SET download_status = 'queued', last_error = NULL "
                f"WHERE id = ? AND download_status IN ({placeholders})",
                (document_id, *sorted(RETRYABLE_DOWNLOAD_STATUSES)),
            )
            return cursor.rowcount == 1

    def claim_document(
        self,
        document_id: int,
        *,
        attempted_at: str | datetime | None = None,
    ) -> bool:
        """Atomically claim one document; concurrent workers cannot both win."""

        timestamp = _utc_timestamp(attempted_at)
        placeholders = ", ".join("?" for _ in RETRYABLE_DOWNLOAD_STATUSES)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE documents
                SET download_status = 'downloading',
                    attempt_count = attempt_count + 1,
                    last_attempt_at = ?,
                    last_error = NULL
                WHERE id = ? AND download_status IN ({placeholders})
                """,
                (timestamp, document_id, *sorted(RETRYABLE_DOWNLOAD_STATUSES)),
            )
            return cursor.rowcount == 1

    def claim_next_queued_document(
        self,
        *,
        run_id: int | None = None,
        attempted_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Claim and return the oldest queued document in one write transaction."""

        timestamp = _utc_timestamp(attempted_at)
        with self.database.transaction() as connection:
            params: tuple[Any, ...]
            if run_id is None:
                sql = "SELECT d.id FROM documents d WHERE d.download_status = 'queued' ORDER BY d.id LIMIT 1"
                params = ()
            else:
                sql = """
                    SELECT d.id
                    FROM documents d
                    JOIN collection_run_items i ON i.document_id = d.id
                    WHERE i.run_id = ? AND i.status = 'queued' AND d.download_status = 'queued'
                    ORDER BY i.id
                    LIMIT 1
                """
                params = (run_id,)
            candidate = connection.execute(sql, params).fetchone()
            if candidate is None:
                return None
            document_id = int(candidate["id"])
            changed = connection.execute(
                """
                UPDATE documents
                SET download_status = 'downloading', attempt_count = attempt_count + 1,
                    last_attempt_at = ?, last_error = NULL
                WHERE id = ? AND download_status = 'queued'
                """,
                (timestamp, document_id),
            ).rowcount
            if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE makes this defensive
                return None
            if run_id is not None:
                connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        updated_at = ?, attempt_count = attempt_count + 1
                    WHERE run_id = ? AND document_id = ? AND status = 'queued'
                    """,
                    (timestamp, timestamp, run_id, document_id),
                )
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            return dict(row) if row is not None else None

    def update_document_download_state(
        self,
        document_id: int,
        status: str,
        *,
        changed_at: str | datetime | None = None,
        **fields: Any,
    ) -> None:
        if status not in DOWNLOAD_STATUSES:
            raise ValueError(f"invalid download status: {status!r}")
        allowed = _DOCUMENT_DOWNLOAD_FIELDS - {"download_status", "attempt_count", "current_version_id"}
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported document state fields: {', '.join(sorted(unexpected))}")
        values = {key: _db_scalar(value) for key, value in fields.items()}
        values["download_status"] = status
        if "sha256" in values and values["sha256"] is not None:
            values["sha256"] = _normalized_sha256(str(values["sha256"]))
        if status == "downloaded":
            values.setdefault("downloaded_at", _utc_timestamp(changed_at))
        assignments = ", ".join(f"{_ident(key)} = ?" for key in values)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",
                (*values.values(), document_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"document {document_id} does not exist")

    def create_document_version(
        self,
        document_id: int,
        record: Mapping[str, Any] | None = None,
        *,
        observed_at: str | datetime | None = None,
        **fields: Any,
    ) -> int:
        values = _record(record, fields)
        timestamp = _utc_timestamp(observed_at or values.pop("observed_at", None))
        with self.database.transaction() as connection:
            return self._create_document_version(connection, document_id, values, timestamp)

    # Friendly alias used by some collectors/downloader adapters.
    add_document_version = create_document_version

    def update_document_version(self, version_id: int, **fields: Any) -> None:
        unexpected = set(fields) - _DOCUMENT_VERSION_MUTABLE_FIELDS
        if unexpected:
            raise ValueError(f"unsupported document-version fields: {', '.join(sorted(unexpected))}")
        values = {key: _db_scalar(value) for key, value in fields.items()}
        if "sha256" in values and values["sha256"] is not None:
            values["sha256"] = _normalized_sha256(str(values["sha256"]))
        if "status" in values and values["status"] not in DOWNLOAD_STATUSES:
            raise ValueError(f"invalid document-version status: {values['status']!r}")
        if "completed_at" in values and values["completed_at"] is not None:
            values["completed_at"] = _utc_timestamp(values["completed_at"])
        if not values:
            return
        assignments = ", ".join(f"{_ident(key)} = ?" for key in values)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE document_versions SET {assignments} WHERE id = ?",
                (*values.values(), version_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"document version {version_id} does not exist")

    def complete_document_download(
        self,
        document_id: int,
        *,
        sha256: str,
        local_relative_path: str,
        downloaded_bytes: int,
        mime_type: str | None = None,
        local_filename: str | None = None,
        remote_filename: str | None = None,
        advertised_bytes: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        source_modified_at: str | None = None,
        source_url: str | None = None,
        run_id: int | None = None,
        validation_status: str = "valid",
        http_status: int | None = None,
        downloaded_at: str | datetime | None = None,
    ) -> int:
        """Record a validated payload and select it as the logical document's current version."""

        digest = _normalized_sha256(sha256)
        timestamp = _utc_timestamp(downloaded_at)
        if downloaded_bytes < 0:
            raise ValueError("downloaded_bytes must not be negative")
        version_values = {
            "collection_run_id": run_id,
            "source_url": source_url,
            "sha256": digest,
            "local_relative_path": local_relative_path,
            "downloaded_bytes": downloaded_bytes,
            "mime_type": mime_type,
            "local_filename": local_filename,
            "remote_filename": remote_filename,
            "advertised_bytes": advertised_bytes,
            "etag": etag,
            "last_modified": last_modified,
            "source_modified_at": source_modified_at,
            "validation_status": validation_status,
            "http_status": http_status,
            "status": "downloaded",
            "completed_at": timestamp,
        }
        with self.database.transaction() as connection:
            version_id = self._create_document_version(
                connection, document_id, version_values, timestamp
            )
            version = connection.execute(
                "SELECT * FROM document_versions WHERE id = ?", (version_id,)
            ).fetchone()
            assert version is not None
            cursor = connection.execute(
                """
                UPDATE documents
                SET download_status = 'downloaded', last_error = NULL, http_status = ?,
                    remote_filename = ?, local_filename = ?, local_relative_path = ?,
                    mime_type = ?, advertised_bytes = ?, downloaded_bytes = ?, sha256 = ?,
                    downloaded_at = ?, validation_status = ?, current_version_id = ?
                WHERE id = ?
                """,
                (
                    version["http_status"],
                    version["remote_filename"],
                    version["local_filename"],
                    version["local_relative_path"],
                    version["mime_type"],
                    version["advertised_bytes"],
                    version["downloaded_bytes"],
                    version["sha256"],
                    version["completed_at"] or timestamp,
                    version["validation_status"],
                    version_id,
                    document_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"document {document_id} does not exist")
            return version_id

    def _create_document_version(
        self,
        connection: sqlite3.Connection,
        document_id: int,
        source: Mapping[str, Any],
        timestamp: str,
    ) -> int:
        document = connection.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            raise KeyError(f"document {document_id} does not exist")
        values = _filtered(source, _DOCUMENT_VERSION_FIELDS)
        digest = values.get("sha256")
        if digest is not None:
            digest = _normalized_sha256(str(digest))
            values["sha256"] = digest
            existing = connection.execute(
                "SELECT id FROM document_versions WHERE document_id = ? AND sha256 = ?",
                (document_id, digest),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
        status = str(values.get("status") or "downloading")
        if status not in DOWNLOAD_STATUSES:
            raise ValueError(f"invalid document-version status: {status!r}")
        row = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version "
            "FROM document_versions WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        values.update(
            document_id=document_id,
            version_number=int(row["next_version"]),
            observed_at=timestamp,
            status=status,
            validation_status=str(values.get("validation_status") or "not_validated"),
            created_at=timestamp,
        )
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        cursor = connection.execute(
            f"INSERT INTO document_versions ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )
        return int(cursor.lastrowid)

    # -- durable collection runs, progress items, and errors -----------------

    def create_collection_run(
        self,
        run_type: str,
        *,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        requested_scope: Mapping[str, Any] | None = None,
        config_snapshot: Mapping[str, Any] | None = None,
        run_uuid: str | None = None,
        queued_at: str | datetime | None = None,
    ) -> int:
        if run_type not in {"collect_bill", "collect_session", "retry_failures"}:
            raise ValueError(f"invalid collection run type: {run_type!r}")
        timestamp = _utc_timestamp(queued_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_runs(
                    run_uuid, run_type, requested_session_key, requested_bill_id_compact,
                    requested_scope_json, status, stage, queued_at, updated_at,
                    config_snapshot_json, summary_json
                ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, '{}')
                """,
                (
                    run_uuid or str(uuid4()),
                    run_type,
                    session_key.upper() if session_key else None,
                    bill_id_compact.replace(" ", "").upper() if bill_id_compact else None,
                    _json_text(requested_scope or {}),
                    timestamp,
                    timestamp,
                    _json_text(config_snapshot or {}),
                ),
            )
            return int(cursor.lastrowid)

    # Short names make the shared CLI/route orchestration less noisy.
    create_run = create_collection_run

    def claim_collection_run(
        self,
        run_id: int,
        *,
        started_at: str | datetime | None = None,
    ) -> bool:
        """Atomically claim a queued run for one collection worker."""

        timestamp = _utc_timestamp(started_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?,
                    current_activity = COALESCE(current_activity, 'Starting collection')
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, timestamp, run_id),
            )
            return cursor.rowcount == 1

    claim_run = claim_collection_run

    def claim_next_collection_run(
        self,
        *,
        started_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued run without allowing a duplicate worker claim."""

        timestamp = _utc_timestamp(started_at)
        with self.database.transaction() as connection:
            candidate = connection.execute(
                "SELECT id FROM collection_runs WHERE status = 'queued' ORDER BY queued_at, id LIMIT 1"
            ).fetchone()
            if candidate is None:
                return None
            run_id = int(candidate["id"])
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?,
                    current_activity = COALESCE(current_activity, 'Starting collection')
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by BEGIN IMMEDIATE
                return None
            return dict(connection.execute("SELECT * FROM collection_runs WHERE id = ?", (run_id,)).fetchone())

    def requeue_interrupted_run(
        self,
        run_id: int,
        *,
        queued_at: str | datetime | None = None,
    ) -> bool:
        """Explicitly make an interrupted run claimable again."""

        timestamp = _utc_timestamp(queued_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET status = 'queued', queued_at = ?, updated_at = ?, interrupted_at = NULL,
                    finished_at = NULL, current_activity = 'Queued to resume interrupted work'
                WHERE id = ? AND status = 'interrupted'
                """,
                (timestamp, timestamp, run_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status = 'queued', updated_at = ?, interrupted_at = NULL,
                        current_activity = 'Queued to resume interrupted work'
                    WHERE run_id = ? AND status = 'interrupted'
                    """,
                    (timestamp, run_id),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET download_status = 'queued', last_error = NULL
                    WHERE id IN (
                        SELECT document_id FROM collection_run_items
                        WHERE run_id = ? AND document_id IS NOT NULL
                    ) AND download_status = 'interrupted'
                    """,
                    (run_id,),
                )
            return cursor.rowcount == 1

    def update_collection_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        stage: str | None = None,
        changed_at: str | datetime | None = None,
        **fields: Any,
    ) -> None:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"invalid collection run status: {status!r}")
        allowed = {
            "current_activity", "bills_total", "bills_completed", "documents_discovered",
            "documents_queued", "documents_downloaded", "documents_skipped", "documents_failed",
            "bytes_downloaded", "error_count", "started_at", "finished_at", "interrupted_at",
            "summary_json",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported collection-run fields: {', '.join(sorted(unexpected))}")
        timestamp = _utc_timestamp(changed_at)
        values = {key: _db_scalar(value) for key, value in fields.items()}
        if "summary_json" in values:
            values["summary_json"] = _json_text(values["summary_json"])
        if status is not None:
            values["status"] = status
            if status == "running":
                values.setdefault("started_at", timestamp)
            if status in {"completed", "completed_with_errors", "failed", "canceled"}:
                values.setdefault("finished_at", timestamp)
            if status == "interrupted":
                values.setdefault("interrupted_at", timestamp)
        if stage is not None:
            values["stage"] = stage
        values["updated_at"] = timestamp
        assignments = ", ".join(f"{_ident(key)} = ?" for key in values)
        with self.database.transaction() as connection:
            if status == "running" and "started_at" in values:
                # Preserve the actual initial start time when a running row receives
                # more than one progress update.
                assignments = assignments.replace("started_at = ?", "started_at = COALESCE(started_at, ?)")
            cursor = connection.execute(
                f"UPDATE collection_runs SET {assignments} WHERE id = ?",
                (*values.values(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"collection run {run_id} does not exist")

    update_run = update_collection_run

    def increment_run_counters(self, run_id: int, **deltas: int) -> None:
        allowed = {
            "bills_total", "bills_completed", "documents_discovered", "documents_queued",
            "documents_downloaded", "documents_skipped", "documents_failed", "bytes_downloaded",
            "error_count",
        }
        if not deltas or set(deltas) - allowed:
            unexpected = set(deltas) - allowed
            if unexpected:
                raise ValueError(f"unsupported counters: {', '.join(sorted(unexpected))}")
            return
        for name, delta in deltas.items():
            if not isinstance(delta, int):
                raise TypeError(f"counter delta {name} must be an integer")
        timestamp = utc_now()
        assignments = ", ".join(f"{_ident(name)} = {_ident(name)} + ?" for name in deltas)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE collection_runs SET {assignments}, updated_at = ? WHERE id = ?",
                (*deltas.values(), timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"collection run {run_id} does not exist")

    def upsert_collection_run_item(
        self,
        run_id: int,
        item_type: str,
        item_key: str,
        *,
        stage: str,
        status: str = "queued",
        changed_at: str | datetime | None = None,
        **fields: Any,
    ) -> int:
        if status not in RUN_ITEM_STATUSES:
            raise ValueError(f"invalid collection run item status: {status!r}")
        timestamp = _utc_timestamp(changed_at)
        allowed = {
            "bill_id", "document_id", "current_activity", "progress_current", "progress_total",
            "attempt_count", "started_at", "finished_at", "interrupted_at", "details_json",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported run-item fields: {', '.join(sorted(unexpected))}")
        values = {key: _db_scalar(value) for key, value in fields.items()}
        if "details_json" in values:
            values["details_json"] = _json_text(values["details_json"])
        values.update(
            run_id=run_id,
            item_type=str(item_type),
            item_key=str(item_key),
            stage=stage,
            status=status,
            queued_at=timestamp,
            updated_at=timestamp,
        )
        values.setdefault("details_json", "{}")
        with self.database.transaction() as connection:
            return int(
                _upsert_row(
                    connection,
                    "collection_run_items",
                    ("run_id", "item_type", "item_key"),
                    values,
                    preserve_on_conflict=("queued_at", "attempt_count"),
                )
            )

    upsert_run_item = upsert_collection_run_item

    def update_collection_run_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        stage: str | None = None,
        changed_at: str | datetime | None = None,
        increment_attempt: bool = False,
        **fields: Any,
    ) -> None:
        if status is not None and status not in RUN_ITEM_STATUSES:
            raise ValueError(f"invalid collection run item status: {status!r}")
        allowed = {
            "bill_id", "document_id", "current_activity", "progress_current", "progress_total",
            "started_at", "finished_at", "interrupted_at", "details_json",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported run-item fields: {', '.join(sorted(unexpected))}")
        timestamp = _utc_timestamp(changed_at)
        values = {key: _db_scalar(value) for key, value in fields.items()}
        if "details_json" in values:
            values["details_json"] = _json_text(values["details_json"])
        if status is not None:
            values["status"] = status
            if status == "running":
                values.setdefault("started_at", timestamp)
            if status in {"completed", "skipped", "failed_retryable", "failed_terminal", "canceled"}:
                values.setdefault("finished_at", timestamp)
            if status == "interrupted":
                values.setdefault("interrupted_at", timestamp)
        if stage is not None:
            values["stage"] = stage
        values["updated_at"] = timestamp
        assignments = [f"{_ident(key)} = ?" for key in values]
        if increment_attempt:
            assignments.append("attempt_count = attempt_count + 1")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE collection_run_items SET {', '.join(assignments)} WHERE id = ?",
                (*values.values(), item_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"collection run item {item_id} does not exist")

    update_run_item = update_collection_run_item

    def record_collection_error(
        self,
        run_id: int,
        *,
        stage: str,
        error_class: str,
        message: str,
        retryable: bool,
        occurred_at: str | datetime | None = None,
        run_item_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        source_entity_type: str | None = None,
        source_id: str | None = None,
        document_id: int | None = None,
        source_url: str | None = None,
        details: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> int:
        timestamp = _utc_timestamp(occurred_at)
        if fingerprint is None:
            fingerprint_parts = (
                stage, error_class, message, session_key or "", bill_id_compact or "",
                source_entity_type or "", source_id or "", str(document_id or ""), source_url or "",
            )
            fingerprint = hash_sha256("\x1f".join(fingerprint_parts).encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_errors(
                    run_id, run_item_id, error_fingerprint, stage, session_key, bill_id_compact,
                    source_entity_type, source_id, document_id, source_url, error_class,
                    retryable, message, first_occurred_at, last_occurred_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, error_fingerprint) DO UPDATE SET
                    run_item_id = COALESCE(excluded.run_item_id, collection_errors.run_item_id),
                    retryable = excluded.retryable,
                    message = excluded.message,
                    last_occurred_at = excluded.last_occurred_at,
                    attempt_count = collection_errors.attempt_count + 1,
                    details_json = excluded.details_json,
                    resolved_at = NULL
                """,
                (
                    run_id, run_item_id, fingerprint, stage, session_key, bill_id_compact,
                    source_entity_type, source_id, document_id, source_url, error_class,
                    int(retryable), message, timestamp, timestamp, _json_text(details or {}),
                ),
            )
            connection.execute(
                "UPDATE collection_runs SET error_count = error_count + 1, updated_at = ? WHERE id = ?",
                (timestamp, run_id),
            )
            row = connection.execute(
                "SELECT id FROM collection_errors WHERE run_id = ? AND error_fingerprint = ?",
                (run_id, fingerprint),
            ).fetchone()
            return int(row["id"])

    record_error = record_collection_error

    def resolve_collection_error(
        self,
        error_id: int,
        *,
        resolved_at: str | datetime | None = None,
    ) -> None:
        timestamp = _utc_timestamp(resolved_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE collection_errors SET resolved_at = ? WHERE id = ?", (timestamp, error_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"collection error {error_id} does not exist")

    def record_source_fetch(
        self,
        *,
        source_kind: str,
        source_url: str,
        fetched_at: str | datetime | None = None,
        run_id: int | None = None,
        run_item_id: int | None = None,
        entity_set: str | None = None,
        request_params: Mapping[str, Any] | None = None,
        response_json: Any = None,
        **fields: Any,
    ) -> int:
        allowed = {
            "completed_at", "succeeded", "http_status", "retry_count", "elapsed_ms", "etag",
            "last_modified", "response_sha256", "item_count", "continuation_url", "error_class",
            "error_message",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported source-fetch fields: {', '.join(sorted(unexpected))}")
        values = {key: _db_scalar(value) for key, value in fields.items()}
        if values.get("response_sha256") is not None:
            values["response_sha256"] = _normalized_sha256(str(values["response_sha256"]))
        values.update(
            run_id=run_id,
            run_item_id=run_item_id,
            source_kind=source_kind,
            entity_set=entity_set,
            source_url=source_url,
            request_params_json=_json_text(request_params or {}),
            fetched_at=_utc_timestamp(fetched_at),
            response_json=None if response_json is None else _json_text(response_json),
        )
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO source_fetches ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )
            return int(cursor.lastrowid)

    # -- restart recovery -----------------------------------------------------

    def normalize_interrupted_work(
        self,
        *,
        interrupted_at: str | datetime | None = None,
    ) -> dict[str, int]:
        """Atomically change falsely active work to recoverable interrupted states.

        Queued work remains queued. Completed and paused work is untouched.  The
        method is idempotent, so applications may call it on every process start.
        """

        timestamp = _utc_timestamp(interrupted_at)
        message = "Interrupted when the previous LegiView process stopped."
        with self.database.transaction() as connection:
            run_count = connection.execute(
                """
                UPDATE collection_runs
                SET status = 'interrupted', interrupted_at = ?, updated_at = ?,
                    current_activity = COALESCE(current_activity, ?)
                WHERE status = 'running'
                """,
                (timestamp, timestamp, message),
            ).rowcount
            item_count = connection.execute(
                """
                UPDATE collection_run_items
                SET status = 'interrupted', interrupted_at = ?, updated_at = ?,
                    current_activity = COALESCE(current_activity, ?)
                WHERE status = 'running'
                """,
                (timestamp, timestamp, message),
            ).rowcount
            document_count = connection.execute(
                """
                UPDATE documents
                SET download_status = 'interrupted', last_error = COALESCE(last_error, ?)
                WHERE download_status = 'downloading'
                """,
                (message,),
            ).rowcount
            version_count = connection.execute(
                """
                UPDATE document_versions
                SET status = 'interrupted', error = COALESCE(error, ?)
                WHERE status = 'downloading'
                """,
                (message,),
            ).rowcount
        return {
            "collection_runs": run_count,
            "collection_run_items": item_count,
            "documents": document_count,
            "document_versions": version_count,
        }

    # -- read models used by CLI and Flask -----------------------------------

    def get_bill(self, session_key: str, bill_id_compact: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM bills WHERE session_key = ? AND bill_id_compact = ?",
            (session_key.upper(), bill_id_compact.replace(" ", "").upper()),
        )

    def get_bill_by_id(self, bill_id: int) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM bills WHERE id = ?", (bill_id,))

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM documents WHERE id = ?", (document_id,))

    def get_document_by_identity(
        self,
        session_key: str,
        bill_id_compact: str,
        source_entity_type: str,
        source_id: str | int,
    ) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM documents
            WHERE session_key = ? AND bill_id_compact = ?
              AND source_entity_type = ? AND source_id = ?
            """,
            (
                session_key.upper(), bill_id_compact.replace(" ", "").upper(),
                source_entity_type, str(source_id),
            ),
        )

    def get_collection_run(self, run_id: int) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM collection_runs WHERE id = ?", (run_id,))

    get_run = get_collection_run

    def list_bill_sponsors(self, bill_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM bill_sponsors
            WHERE bill_id = ?
            ORDER BY CASE normalized_category WHEN 'chief' THEN 0 WHEN 'regular' THEN 1 ELSE 2 END,
                     COALESCE(print_order, 2147483647), id
            """,
            (bill_id,),
        )

    def list_bill_documents(self, bill_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM documents
            WHERE bill_id = ?
            ORDER BY document_kind, COALESCE(meeting_date, letter_date, ''), id
            """,
            (bill_id,),
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._fetch_all(
            "SELECT * FROM sessions ORDER BY COALESCE(session_year, 0) DESC, session_key DESC", ()
        )

    def list_legislators(self, session_key: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM legislators
            WHERE session_key=?
            ORDER BY COALESCE(last_name,''), COALESCE(first_name,''), legislator_code
            """,
            (session_key.upper(),),
        )

    def list_committees(self, session_key: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM committees
            WHERE session_key=?
            ORDER BY COALESCE(committee_name,''), committee_code
            """,
            (session_key.upper(),),
        )

    def reference_source_watermark(self, entity: str, session_key: str) -> str | None:
        """Return the newest retained official source date for a reference set."""

        table = {"legislators": "legislators", "committees": "committees"}.get(entity)
        if table is None:
            raise ValueError(f"unsupported reference entity: {entity!r}")
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT MAX(source_date) AS watermark
                FROM (
                    SELECT source_created_at AS source_date FROM {table} WHERE session_key=?
                    UNION ALL
                    SELECT source_modified_at AS source_date FROM {table} WHERE session_key=?
                )
                WHERE source_date IS NOT NULL AND trim(source_date) != ''
                """,
                (session_key.upper(), session_key.upper()),
            ).fetchone()
        return str(row["watermark"]) if row and row["watermark"] else None

    def list_bills(
        self,
        *,
        session_key: str | None = None,
        chamber: str | None = None,
        query: str | None = None,
        enacted: bool | None = None,
        sort: str = "bill",
        descending: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        sort_columns = {
            "bill": "b.session_key DESC, b.measure_prefix, CAST(b.measure_number AS INTEGER), b.measure_number",
            "session": "b.session_key",
            "title": "COALESCE(b.bill_title, '')",
            "chapter": "COALESCE(b.chapter_number, '')",
            "last_synced": "b.last_synced_at",
            "documents": "document_count",
        }
        if sort not in sort_columns:
            raise ValueError(f"unsupported bill sort: {sort!r}")
        where: list[str] = []
        params: list[Any] = []
        if session_key:
            where.append("b.session_key = ?")
            params.append(session_key.upper())
        if chamber:
            where.append("b.bill_chamber = ?")
            params.append(chamber)
        if query:
            where.append("(b.bill_id_compact LIKE ? OR b.bill_title LIKE ? OR b.at_the_request_of LIKE ?)")
            needle = f"%{query.strip()}%"
            params.extend((needle, needle, needle))
        if enacted is True:
            where.append("(b.enacted = 1 OR NULLIF(trim(b.chapter_number), '') IS NOT NULL)")
        elif enacted is False:
            where.append("COALESCE(b.enacted, 0) = 0 AND NULLIF(trim(b.chapter_number), '') IS NULL")
        direction = "DESC" if descending else "ASC"
        sql = """
            SELECT b.*,
                   COUNT(DISTINCT d.id) AS document_count,
                   GROUP_CONCAT(DISTINCT s.resolved_display_name) AS sponsor_summary
            FROM bills b
            LEFT JOIN documents d ON d.bill_id = b.id
            LEFT JOIN bill_sponsors s ON s.bill_id = b.id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" GROUP BY b.id ORDER BY {sort_columns[sort]} {direction}, b.id {direction} LIMIT ? OFFSET ?"
        params.extend((limit, max(0, offset)))
        return self._fetch_all(sql, params)

    def list_documents(
        self,
        *,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        document_kind: str | None = None,
        committee: str | None = None,
        submitter: str | None = None,
        testimony_position: str | None = None,
        download_status: str | None = None,
        failed_only: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        where: list[str] = []
        params: list[Any] = []
        exact = {
            "d.session_key": session_key.upper() if session_key else None,
            "d.bill_id_compact": bill_id_compact.replace(" ", "").upper() if bill_id_compact else None,
            "d.document_kind": document_kind,
            "d.testimony_position": testimony_position,
            "d.download_status": download_status,
        }
        for column, value in exact.items():
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if committee:
            where.append("(d.committee_code LIKE ? OR d.committee_name LIKE ?)")
            needle = f"%{committee.strip()}%"
            params.extend((needle, needle))
        if submitter:
            where.append("d.submitter LIKE ?")
            params.append(f"%{submitter.strip()}%")
        if failed_only:
            where.append("d.download_status IN ('failed_retryable', 'failed_terminal')")
        sql = """
            SELECT d.*, b.bill_title
            FROM documents d
            JOIN bills b ON b.id = d.bill_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.session_key DESC, d.bill_id_compact, d.document_kind, d.id LIMIT ? OFFSET ?"
        params.extend((limit, max(0, offset)))
        return self._fetch_all(sql, params)

    def list_documents_for_retry(
        self,
        *,
        run_id: int | None = None,
        include_terminal: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        retry_statuses = (
            "failed_retryable",
            "interrupted",
            "missing_local",
            "paused_low_space",
            *(("failed_terminal",) if include_terminal else ()),
        )
        placeholders = ", ".join("?" for _ in retry_statuses)
        params: list[Any] = list(retry_statuses)
        sql = f"SELECT DISTINCT d.* FROM documents d"
        if run_id is not None:
            sql += " JOIN collection_run_items i ON i.document_id = d.id"
        sql += f" WHERE d.download_status IN ({placeholders})"
        if run_id is not None:
            sql += " AND i.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY d.last_attempt_at, d.id LIMIT ?"
        params.append(max(0, limit))
        return self._fetch_all(sql, params)

    def list_document_versions(self, document_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_number DESC",
            (document_id,),
        )

    def list_collection_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        return self._fetch_all(
            "SELECT * FROM collection_runs ORDER BY queued_at DESC, id DESC LIMIT ?", (limit,)
        )

    list_runs = list_collection_runs

    def list_run_items(self, run_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            "SELECT * FROM collection_run_items WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def list_run_errors(self, run_id: int) -> list[dict[str, Any]]:
        return self._fetch_all(
            "SELECT * FROM collection_errors WHERE run_id = ? ORDER BY last_occurred_at DESC, id DESC",
            (run_id,),
        )

    def archive_stats(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            scalar_queries = {
                "sessions_stored": "SELECT COUNT(*) FROM sessions",
                "bills_stored": "SELECT COUNT(*) FROM bills",
                "sponsors_stored": "SELECT COUNT(*) FROM bill_sponsors",
                "documents_discovered": "SELECT COUNT(*) FROM documents",
                "documents_downloaded": "SELECT COUNT(*) FROM documents WHERE download_status = 'downloaded'",
                "download_failures": "SELECT COUNT(*) FROM documents WHERE download_status IN ('failed_retryable', 'failed_terminal')",
                "archive_bytes": "SELECT COALESCE(SUM(downloaded_bytes), 0) FROM document_versions WHERE status = 'downloaded'",
                "last_completed_collection": "SELECT MAX(finished_at) FROM collection_runs WHERE status IN ('completed', 'completed_with_errors')",
            }
            return {
                name: connection.execute(query).fetchone()[0]
                for name, query in scalar_queries.items()
            }

    def _fetch_one(self, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        return None if row is None else dict(row)

    def _fetch_all(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def _bill_id(connection: sqlite3.Connection, values: Mapping[str, Any]) -> int:
        if values.get("bill_id") is not None:
            bill_id = int(values["bill_id"])
            if connection.execute("SELECT 1 FROM bills WHERE id = ?", (bill_id,)).fetchone() is None:
                raise KeyError(f"bill {bill_id} does not exist")
            return bill_id
        session_key = _required_text(values, "session_key").upper()
        compact = _required_text(values, "bill_id_compact").replace(" ", "").upper()
        row = connection.execute(
            "SELECT id FROM bills WHERE session_key = ? AND bill_id_compact = ?",
            (session_key, compact),
        ).fetchone()
        if row is None:
            raise KeyError(f"bill {session_key}/{compact} does not exist")
        return int(row["id"])


_DOCUMENT_FIELDS = {
    "document_kind", "source_section", "source_entity_type", "source_id", "raw_document_type",
    "classification_method", "classification_confidence", "title", "exhibit_reference", "submitter",
    "on_behalf_of", "testimony_position", "city_organization", "meeting_date", "committee_code",
    "committee_name", "chamber", "letter_date", "description", "committee_meeting_id",
    "committee_agenda_item_id", "source_url", "canonical_download_url", "source_created_at",
    "source_modified_at", "download_status", "attempt_count", "last_attempt_at", "last_error",
    "http_status", "remote_filename", "local_filename", "local_relative_path", "mime_type",
    "advertised_bytes", "downloaded_bytes", "sha256", "downloaded_at", "validation_status",
    "current_version_id",
}

_DOCUMENT_DOWNLOAD_FIELDS = {
    "download_status", "attempt_count", "last_attempt_at", "last_error", "http_status",
    "remote_filename", "local_filename", "local_relative_path", "mime_type", "advertised_bytes",
    "downloaded_bytes", "sha256", "downloaded_at", "validation_status", "current_version_id",
}

_DOCUMENT_VERSION_FIELDS = {
    "collection_run_id", "source_url", "source_modified_at", "etag", "last_modified", "remote_filename", "local_filename",
    "local_relative_path", "advertised_bytes", "downloaded_bytes", "mime_type", "sha256", "status",
    "validation_status", "http_status", "error", "completed_at",
}

_DOCUMENT_VERSION_MUTABLE_FIELDS = _DOCUMENT_VERSION_FIELDS


def _normalized_sha256(value: str) -> str:
    digest = value.strip().lower()
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("sha256 must be exactly 64 hexadecimal characters")
    return digest


# Backward-friendly short name for call sites that prefer ``Storage``.
Storage = StorageService


__all__ = [
    "DOCUMENT_KINDS",
    "DOWNLOAD_STATUSES",
    "RETRYABLE_DOWNLOAD_STATUSES",
    "RUN_ITEM_STATUSES",
    "RUN_STATUSES",
    "Storage",
    "StorageService",
    "utc_now",
]
