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
from .source_mapping import (
    chamber_for_prefix,
    measure_type_for_prefix,
    normalize_bill_id,
    normalize_measure_prefix,
)


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
RUN_TYPES = frozenset(
    {
        "collect_bill",
        "collect_session",
        "retry_failures",
        "inventory_backfill",
        "download_archive",
    }
)
HISTORICAL_RUN_TYPES = frozenset({"inventory_backfill", "download_archive"})
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
ARCHIVE_CLAIMABLE_DOWNLOAD_STATUSES = frozenset(
    set(RETRYABLE_DOWNLOAD_STATUSES) | {"failed_terminal"}
)
_ARCHIVE_WALK_STATUS_SQL = ",".join(
    f"'{status}'" for status in sorted(ARCHIVE_CLAIMABLE_DOWNLOAD_STATUSES)
)
_ARCHIVE_QUEUED_ITEM_SKIPPED = object()
MATCHING_RETRY_DOWNLOAD_STATUSES = frozenset(
    {
        "failed_retryable",
        "failed_terminal",
        "interrupted",
        "missing_local",
        "paused_low_space",
    }
)
RETRY_PAYLOAD_DOCUMENT_KINDS = frozenset(
    {
        "public_testimony",
        "legacy_testimony",
        "committee_presentation",
        "floor_letter",
    }
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
SOURCE_PRESENCE_STATES = frozenset({"active", "missing", "unknown"})
INVENTORY_STATUSES = frozenset(
    {
        "not_started",
        "inventory_running",
        "inventory_complete",
        "inventory_complete_with_errors",
        "inventory_incomplete",
        "inventory_failed",
        "interrupted",
    }
)
DISPLAY_RECONCILIATION_STATUSES = frozenset(
    {
        "checked_with_records",
        "checked_zero",
        "not_applicable",
        "failed_fetch",
        "parser_anomalous",
    }
)
ANOMALY_SEVERITIES = frozenset({"info", "warning", "error", "critical"})

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
        prefix = normalize_measure_prefix(_required_text(source, "measure_prefix"))
        supplied_number = _required_text(source, "measure_number")
        _, canonical_number, compact, display = normalize_bill_id(
            f"{prefix}{supplied_number}"
        )
        number = str(canonical_number)
        for identity_field in ("bill_id_compact", "bill_id_display"):
            supplied_identity = source.get(identity_field)
            if supplied_identity is None:
                continue
            supplied_prefix, supplied_identity_number, _, _ = normalize_bill_id(
                str(supplied_identity)
            )
            if (supplied_prefix, supplied_identity_number) != (
                prefix,
                canonical_number,
            ):
                raise ValueError(
                    f"{identity_field} does not match measure_prefix/measure_number"
                )
        expected_chamber = chamber_for_prefix(prefix)
        chamber = str(source.get("bill_chamber") or expected_chamber).strip()
        if chamber != expected_chamber:
            raise ValueError(
                f"{prefix} originates in the {expected_chamber}, not {chamber or 'an unknown chamber'}"
            )
        expected_measure_type = measure_type_for_prefix(prefix)
        measure_type = str(source.get("measure_type") or expected_measure_type).strip()
        if measure_type != expected_measure_type:
            raise ValueError(
                f"{prefix} has measure type {expected_measure_type!r}, not {measure_type!r}"
            )
        timestamp = _utc_timestamp(seen_at)
        allowed = {
            "session_key", "measure_id", "measure_prefix", "measure_number", "bill_id_compact",
            "bill_id_display", "bill_chamber", "measure_type", "at_the_request_of", "title_source_field", "bill_title",
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
            measure_type=measure_type,
            first_collected_at=timestamp,
            last_seen_at=timestamp,
            last_synced_at=timestamp,
            source_presence="active",
            missing_from_source_since=None,
            raw_json=_json_text(source.get("raw_json", source)),
        )
        if run_id is not None:
            values["last_collected_run_id"] = run_id
        with self.database.transaction() as connection:
            previous = connection.execute(
                """
                SELECT id, measure_id, source_presence
                FROM bills WHERE session_key=? AND bill_id_compact=?
                """,
                (session_key, compact),
            ).fetchone()
            bill_id = int(
                _upsert_row(
                    connection,
                    "bills",
                    ("session_key", "bill_id_compact"),
                    values,
                    preserve_on_conflict=("first_collected_at",),
                )
            )
            if previous is not None and previous["source_presence"] != "active":
                self._record_presence_event(
                    connection,
                    entity_type="bill",
                    session_key=session_key,
                    bill_id=bill_id,
                    source_entity_type="Measure",
                    source_id=str(previous["measure_id"] or compact),
                    previous_presence=str(previous["source_presence"]),
                    new_presence="active",
                    changed_at=timestamp,
                    run_id=run_id,
                )
            return bill_id

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
            source_id = _required_text(source, "source_id")
            previous = connection.execute(
                """
                SELECT id, raw_document_type, document_kind, source_section,
                       source_presence, canonical_download_url, source_modified_at,
                       download_status
                FROM documents
                WHERE session_key=? AND bill_id_compact=?
                  AND source_entity_type=? AND source_id=?
                """,
                (
                    str(bill["session_key"]),
                    str(bill["bill_id_compact"]),
                    source_entity_type,
                    source_id,
                ),
            ).fetchone()
            values = _filtered(source, _DOCUMENT_FIELDS)
            document_kind = source.get("document_kind")
            if document_kind is None and previous is not None:
                document_kind = previous["document_kind"]
            source_section = source.get("source_section")
            if source_section is None and previous is not None:
                source_section = previous["source_section"]
            observed_presence = str(source.get("source_presence") or "active")
            if observed_presence not in SOURCE_PRESENCE_STATES:
                raise ValueError(f"invalid source_presence: {observed_presence!r}")
            values.update(
                bill_id=bill_id,
                session_key=str(bill["session_key"]),
                bill_id_compact=str(bill["bill_id_compact"]),
                document_kind=str(document_kind or "unknown"),
                source_section=str(source_section or source_entity_type),
                source_entity_type=source_entity_type,
                source_id=source_id,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
                source_presence=observed_presence,
                missing_from_source_since=(
                    None if observed_presence != "missing" else timestamp
                ),
                raw_json=_json_text(source.get("raw_json", source)),
            )
            if run_id is not None:
                values["last_seen_run_id"] = run_id
            if values["document_kind"] not in DOCUMENT_KINDS:
                raise ValueError(f"unknown document_kind: {values['document_kind']!r}")
            document_id = int(
                _upsert_row(
                    connection,
                    "documents",
                    ("session_key", "bill_id_compact", "source_entity_type", "source_id"),
                    values,
                    preserve_on_conflict=_DOCUMENT_DOWNLOAD_FIELDS | {"first_seen_at"},
                )
            )
            if previous is not None and previous["download_status"] == "downloaded":
                incoming_url = source.get("canonical_download_url")
                incoming_modified = source.get("source_modified_at")
                url_changed = (
                    incoming_url is not None
                    and str(incoming_url).strip()
                    != str(previous["canonical_download_url"] or "").strip()
                )
                source_date_changed = (
                    incoming_modified is not None
                    and str(incoming_modified).strip()
                    != str(previous["source_modified_at"] or "").strip()
                )
                if url_changed or source_date_changed:
                    # Keep the current version pointer and retained bytes.  The
                    # next explicit archive run fetches a candidate, dedupes by
                    # SHA-256, and only promotes a new immutable version if the
                    # bytes actually differ.
                    connection.execute(
                        """
                        UPDATE documents
                        SET download_status='changed_remote',last_error=NULL
                        WHERE id=? AND download_status='downloaded'
                        """,
                        (document_id,),
                    )
            if previous is not None:
                previous_presence = str(previous["source_presence"])
                if previous_presence != observed_presence:
                    self._record_presence_event(
                        connection,
                        entity_type="document",
                        session_key=str(bill["session_key"]),
                        bill_id=bill_id,
                        document_id=document_id,
                        source_entity_type=source_entity_type,
                        source_id=source_id,
                        previous_presence=previous_presence,
                        new_presence=observed_presence,
                        changed_at=timestamp,
                        run_id=run_id,
                    )
                old_raw_type = previous["raw_document_type"]
                new_raw_type = source.get("raw_document_type")
                if (
                    new_raw_type is not None
                    and old_raw_type is not None
                    and str(new_raw_type) != str(old_raw_type)
                ):
                    self._record_anomaly(
                        connection,
                        anomaly_type="raw_document_type_changed",
                        severity="warning",
                        affects_completeness=False,
                        message="The official raw document type changed for a stable document identity.",
                        session_key=str(bill["session_key"]),
                        bill_id=bill_id,
                        bill_id_compact=str(bill["bill_id_compact"]),
                        document_id=document_id,
                        source_entity_type=source_entity_type,
                        source_id=source_id,
                        previous_value={"raw_document_type": old_raw_type},
                        current_value={"raw_document_type": new_raw_type},
                        run_id=run_id,
                        observed_at=timestamp,
                    )
                old_kind = str(previous["document_kind"])
                new_kind = str(values["document_kind"])
                if old_kind != new_kind:
                    self._record_anomaly(
                        connection,
                        anomaly_type="normalized_document_kind_changed",
                        severity="warning",
                        affects_completeness=False,
                        message="The normalized document kind changed for a stable document identity.",
                        session_key=str(bill["session_key"]),
                        bill_id=bill_id,
                        bill_id_compact=str(bill["bill_id_compact"]),
                        document_id=document_id,
                        source_entity_type=source_entity_type,
                        source_id=source_id,
                        previous_value={"document_kind": old_kind},
                        current_value={"document_kind": new_kind},
                        run_id=run_id,
                        observed_at=timestamp,
                    )
            if (
                str(values["document_kind"]) == "unknown"
                or source.get("classification_method")
                in {"unrecognized_raw_document_type", "unclassified"}
            ):
                self._record_anomaly(
                    connection,
                    anomaly_type="unknown_document_type",
                    severity="warning",
                    affects_completeness=False,
                    message="An unknown official document type was retained without classification.",
                    session_key=str(bill["session_key"]),
                    bill_id=bill_id,
                    bill_id_compact=str(bill["bill_id_compact"]),
                    document_id=document_id,
                    source_entity_type=source_entity_type,
                    source_id=source_id,
                    current_value={
                        "raw_document_type": source.get("raw_document_type"),
                        "document_kind": values["document_kind"],
                    },
                    run_id=run_id,
                    observed_at=timestamp,
                )
            return document_id

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
        sha256: str | None = None,
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

        digest = _normalized_sha256(sha256) if sha256 else None
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
        session_keys: Iterable[str] | None = None,
        scope_cutoff_at: str | datetime | None = None,
        probe_remote_sizes: bool | None = None,
    ) -> int:
        if run_type not in RUN_TYPES:
            raise ValueError(f"invalid collection run type: {run_type!r}")
        timestamp = _utc_timestamp(queued_at)
        frozen_scope = dict(requested_scope or {})
        selected_sessions: tuple[str, ...] = ()
        cutoff: str | None = None
        if run_type in HISTORICAL_RUN_TYPES:
            requested_sessions = session_keys
            if requested_sessions is None:
                requested_sessions = frozen_scope.get("session_keys", ())
            selected_sessions = _normalized_session_keys(requested_sessions)
            if not selected_sessions:
                raise ValueError(f"{run_type} requires at least one selected session")
            frozen_scope["session_keys"] = list(selected_sessions)
            if run_type == "inventory_backfill":
                if probe_remote_sizes is None:
                    probe_remote_sizes = bool(frozen_scope.get("probe_remote_sizes", False))
                frozen_scope["probe_remote_sizes"] = bool(probe_remote_sizes)
            else:
                candidate_cutoff = scope_cutoff_at or frozen_scope.get("scope_cutoff_at") or timestamp
                cutoff = _utc_timestamp(candidate_cutoff)
                frozen_scope["scope_cutoff_at"] = cutoff
        elif session_keys is not None:
            raise ValueError("session_keys is only valid for historical runs")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_runs(
                    run_uuid, run_type, requested_session_key, requested_bill_id_compact,
                    requested_scope_json, scope_cutoff_at, status, stage, queued_at, updated_at,
                    sessions_total, config_snapshot_json, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?, '{}')
                """,
                (
                    run_uuid or str(uuid4()),
                    run_type,
                    session_key.upper() if session_key else (
                        selected_sessions[0] if len(selected_sessions) == 1 else None
                    ),
                    bill_id_compact.replace(" ", "").upper() if bill_id_compact else None,
                    _json_text(frozen_scope),
                    cutoff,
                    timestamp,
                    timestamp,
                    len(selected_sessions),
                    _json_text(config_snapshot or {}),
                ),
            )
            run_id = int(cursor.lastrowid)
            if selected_sessions:
                stage = "sync_session" if run_type == "inventory_backfill" else "download_archive"
                activity = (
                    "Waiting for session inventory"
                    if run_type == "inventory_backfill"
                    else "Waiting for session archive download"
                )
                connection.executemany(
                    """
                    INSERT INTO collection_run_items(
                        run_id, item_type, item_key, session_key, stage, status,
                        current_activity, queued_at, updated_at, details_json
                    ) VALUES (?, 'session', ?, ?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (
                        (
                            run_id,
                            selected,
                            selected,
                            stage,
                            activity,
                            timestamp,
                            timestamp,
                            _json_text({"session_key": selected}),
                        )
                        for selected in selected_sessions
                    ),
                )
            return run_id

    # Short names make the shared CLI/route orchestration less noisy.
    create_run = create_collection_run

    def claim_collection_run(
        self,
        run_id: int,
        *,
        started_at: str | datetime | None = None,
    ) -> bool:
        """Atomically claim a queued run while preserving the single-run invariant."""

        timestamp = _utc_timestamp(started_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?,
                    current_activity = COALESCE(current_activity, 'Starting collection')
                WHERE id = ? AND status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM collection_runs active
                      WHERE active.status = 'running' AND active.id <> ?
                  )
                """,
                (timestamp, timestamp, run_id, run_id),
            )
            return cursor.rowcount == 1

    claim_run = claim_collection_run

    def claim_next_collection_run(
        self,
        *,
        started_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued run only when no durable run is already active."""

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
                  AND NOT EXISTS (
                      SELECT 1 FROM collection_runs active
                      WHERE active.status = 'running' AND active.id <> ?
                  )
                """,
                (timestamp, timestamp, run_id, run_id),
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
        if status == "running":
            raise ValueError(
                "use claim_collection_run() for the guarded transition to running"
            )
        allowed = {
            "current_activity", "sessions_total", "sessions_completed", "sessions_incomplete",
            "sessions_failed", "bills_total", "bills_completed", "documents_discovered",
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
            "sessions_total", "sessions_completed", "sessions_incomplete", "sessions_failed",
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
            "session_key", "bill_id", "document_id", "current_activity", "progress_current", "progress_total",
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
            "session_key", "bill_id", "document_id", "current_activity", "progress_current", "progress_total",
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

    # -- Phase 2 source synchronization and completeness ---------------------

    def get_source_sync_state(
        self, session_key: str, entity_set: str
    ) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM source_sync_state WHERE session_key=? AND entity_set=?",
            (session_key.strip().upper(), entity_set.strip()),
        )

    def record_source_sync_success(
        self,
        session_key: str,
        entity_set: str,
        *,
        strategy: str,
        run_id: int,
        source_count: int,
        source_watermark: str | None = None,
        full_session: bool = False,
        incremental: bool = False,
        reconciliation_outcome: str | None = None,
        completed_at: str | datetime | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a successful source cursor.

        The watermark is only writable through this success path.  Callers must
        use :meth:`record_source_sync_failure` for failed requests, which retains
        the last known-good cursor.
        """

        key = _required_nonempty(session_key, "session_key").upper()
        entity = _required_nonempty(entity_set, "entity_set")
        sync_strategy = _required_nonempty(strategy, "strategy")
        if source_count < 0:
            raise ValueError("source_count must not be negative")
        if full_session and incremental:
            raise ValueError("a sync cannot be both full-session and incremental")
        timestamp = _utc_timestamp(completed_at)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_sync_state(
                    session_key,entity_set,sync_strategy,last_attempted_at,
                    last_successful_sync_at,last_full_session_sync_at,
                    last_incremental_sync_at,source_watermark,last_successful_run_id,
                    last_returned_source_count,last_reconciliation_outcome,is_incomplete,
                    details_json,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(session_key,entity_set) DO UPDATE SET
                    sync_strategy=excluded.sync_strategy,
                    last_attempted_at=excluded.last_attempted_at,
                    last_successful_sync_at=excluded.last_successful_sync_at,
                    last_full_session_sync_at=CASE
                        WHEN excluded.last_full_session_sync_at IS NOT NULL
                        THEN excluded.last_full_session_sync_at
                        ELSE source_sync_state.last_full_session_sync_at END,
                    last_incremental_sync_at=CASE
                        WHEN excluded.last_incremental_sync_at IS NOT NULL
                        THEN excluded.last_incremental_sync_at
                        ELSE source_sync_state.last_incremental_sync_at END,
                    source_watermark=CASE
                        WHEN excluded.source_watermark IS NOT NULL
                        THEN excluded.source_watermark
                        ELSE source_sync_state.source_watermark END,
                    last_successful_run_id=excluded.last_successful_run_id,
                    last_returned_source_count=excluded.last_returned_source_count,
                    last_reconciliation_outcome=excluded.last_reconciliation_outcome,
                    is_incomplete=0,last_failure_at=NULL,last_error_class=NULL,
                    last_error_message=NULL,details_json=excluded.details_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    entity,
                    sync_strategy,
                    timestamp,
                    timestamp,
                    timestamp if full_session else None,
                    timestamp if incremental else None,
                    str(source_watermark).strip() if source_watermark else None,
                    run_id,
                    int(source_count),
                    reconciliation_outcome,
                    _json_text(details or {}),
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM source_sync_state WHERE session_key=? AND entity_set=?",
                (key, entity),
            ).fetchone()
            assert row is not None
            return dict(row)

    def record_source_sync_failure(
        self,
        session_key: str,
        entity_set: str,
        *,
        strategy: str,
        error: BaseException | str,
        failed_at: str | datetime | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an incomplete attempt without advancing its successful cursor."""

        key = _required_nonempty(session_key, "session_key").upper()
        entity = _required_nonempty(entity_set, "entity_set")
        sync_strategy = _required_nonempty(strategy, "strategy")
        timestamp = _utc_timestamp(failed_at)
        error_class = type(error).__name__ if isinstance(error, BaseException) else "Error"
        message = str(error).strip()[:2000] or error_class
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_sync_state(
                    session_key,entity_set,sync_strategy,last_attempted_at,is_incomplete,
                    last_failure_at,last_error_class,last_error_message,details_json,updated_at
                ) VALUES (?,?,?,?,1,?,?,?,?,?)
                ON CONFLICT(session_key,entity_set) DO UPDATE SET
                    sync_strategy=excluded.sync_strategy,
                    last_attempted_at=excluded.last_attempted_at,
                    is_incomplete=1,last_failure_at=excluded.last_failure_at,
                    last_error_class=excluded.last_error_class,
                    last_error_message=excluded.last_error_message,
                    details_json=excluded.details_json,updated_at=excluded.updated_at
                """,
                (
                    key,
                    entity,
                    sync_strategy,
                    timestamp,
                    timestamp,
                    error_class,
                    message,
                    _json_text(details or {}),
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM source_sync_state WHERE session_key=? AND entity_set=?",
                (key, entity),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_session_archive_state(self, session_key: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM session_archive_state WHERE session_key=?",
            (session_key.strip().upper(),),
        )

    def mark_session_inventory_started(
        self,
        session_key: str,
        run_id: int,
        *,
        started_at: str | datetime | None = None,
    ) -> None:
        key = _required_nonempty(session_key, "session_key").upper()
        timestamp = _utc_timestamp(started_at)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO session_archive_state(
                    session_key,inventory_status,last_inventory_started_at,
                    last_inventory_run_id,updated_at
                ) VALUES (?,'inventory_running',?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    inventory_status='inventory_running',
                    last_inventory_started_at=excluded.last_inventory_started_at,
                    last_inventory_run_id=excluded.last_inventory_run_id,
                    updated_at=excluded.updated_at
                """,
                (key, timestamp, run_id, timestamp),
            )

    def finish_session_inventory(
        self,
        session_key: str,
        run_id: int,
        status: str,
        *,
        completed_at: str | datetime | None = None,
        display_reconciliation_status: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in INVENTORY_STATUSES - {"not_started", "inventory_running"}:
            raise ValueError(f"invalid finished inventory status: {status!r}")
        key = _required_nonempty(session_key, "session_key").upper()
        timestamp = _utc_timestamp(completed_at)
        successful = status in {"inventory_complete", "inventory_complete_with_errors"}
        with self.database.transaction() as connection:
            anomaly_counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN affects_completeness=1 THEN 1 ELSE 0 END),0)
                           AS material
                FROM source_anomalies
                WHERE session_key=? AND resolved_at IS NULL
                """,
                (key,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO session_archive_state(
                    session_key,inventory_status,last_inventory_completed_at,
                    last_inventory_run_id,last_successful_inventory_run_id,
                    display_reconciliation_status,source_anomaly_count,
                    material_anomaly_count,completeness_details_json,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    inventory_status=excluded.inventory_status,
                    last_inventory_completed_at=excluded.last_inventory_completed_at,
                    last_inventory_run_id=excluded.last_inventory_run_id,
                    last_successful_inventory_run_id=CASE
                        WHEN excluded.last_successful_inventory_run_id IS NOT NULL
                        THEN excluded.last_successful_inventory_run_id
                        ELSE session_archive_state.last_successful_inventory_run_id END,
                    display_reconciliation_status=COALESCE(
                        excluded.display_reconciliation_status,
                        session_archive_state.display_reconciliation_status
                    ),
                    source_anomaly_count=excluded.source_anomaly_count,
                    material_anomaly_count=excluded.material_anomaly_count,
                    completeness_details_json=excluded.completeness_details_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    status,
                    timestamp,
                    run_id,
                    run_id if successful else None,
                    display_reconciliation_status,
                    int(anomaly_counts["total"]),
                    int(anomaly_counts["material"]),
                    _json_text(details or {}),
                    timestamp,
                ),
            )

    def stop_session_inventory_run(self, run_id: int, run_status: str) -> int:
        """Remove stale ``inventory_running`` state after pause/cancel/interruption."""

        if run_status not in {"paused", "canceled", "interrupted"}:
            raise ValueError(f"unsupported stopped inventory run status: {run_status!r}")
        timestamp = _utc_timestamp(None)
        inventory_status = (
            "inventory_incomplete" if run_status == "canceled" else "interrupted"
        )
        details = _json_text(
            {
                "run_status": run_status,
                "message": f"Inventory run {run_status} before this session completed",
            }
        )
        with self.database.transaction() as connection:
            return connection.execute(
                """
                UPDATE session_archive_state
                SET inventory_status=?,completeness_details_json=?,updated_at=?
                WHERE inventory_status='inventory_running' AND last_inventory_run_id=?
                """,
                (inventory_status, details, timestamp, int(run_id)),
            ).rowcount

    def record_session_download_activity(
        self,
        session_key: str,
        run_id: int,
        *,
        completed: bool = False,
        changed_at: str | datetime | None = None,
    ) -> None:
        key = _required_nonempty(session_key, "session_key").upper()
        timestamp = _utc_timestamp(changed_at)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO session_archive_state(
                    session_key,last_download_started_at,last_download_completed_at,
                    last_download_run_id,updated_at
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    last_download_started_at=CASE
                        WHEN ?=0 THEN excluded.last_download_started_at
                        ELSE session_archive_state.last_download_started_at END,
                    last_download_completed_at=CASE
                        WHEN ?=1 THEN excluded.last_download_completed_at
                        ELSE session_archive_state.last_download_completed_at END,
                    last_download_run_id=excluded.last_download_run_id,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    None if completed else timestamp,
                    timestamp if completed else None,
                    run_id,
                    timestamp,
                    int(completed),
                    int(completed),
                ),
            )

    # -- source presence and drift diagnostics ------------------------------

    def reconcile_source_presence(
        self,
        entity_type: str,
        session_key: str,
        run_id: int,
        *,
        source_entity_type: str | None = None,
        authoritative_complete: bool,
        reconciled_at: str | datetime | None = None,
    ) -> dict[str, int]:
        """Reconcile a complete session result without deleting archival rows.

        ``authoritative_complete`` is deliberately mandatory.  A failed or
        incremental query must pass false (or simply not call this method), in
        which case no presence state changes are made.
        """

        if entity_type not in {"bill", "document"}:
            raise ValueError("entity_type must be 'bill' or 'document'")
        if entity_type == "document" and not str(source_entity_type or "").strip():
            raise ValueError("source_entity_type is required for document reconciliation")
        key = _required_nonempty(session_key, "session_key").upper()
        if not authoritative_complete:
            return {
                "records_reconciled": 0,
                "active": 0,
                "missing": 0,
                "marked_missing": 0,
                "restored_active": 0,
            }
        timestamp = _utc_timestamp(reconciled_at)
        event_details = _json_text({"authoritative_full_session": True})
        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM collection_runs WHERE id=?", (run_id,)).fetchone() is None:
                raise KeyError(f"collection run {run_id} does not exist")
            if entity_type == "bill":
                transitions = connection.execute(
                    """
                    SELECT COALESCE(SUM(CASE
                               WHEN last_collected_run_id=? THEN 0
                               WHEN source_presence!='missing' THEN 1 ELSE 0 END), 0)
                               AS marked_missing,
                           COALESCE(SUM(CASE
                               WHEN last_collected_run_id=? AND source_presence!='active'
                               THEN 1 ELSE 0 END), 0) AS restored_active
                    FROM bills WHERE session_key=?
                    """,
                    (run_id, run_id, key),
                ).fetchone()
                marked_missing = int(transitions["marked_missing"] or 0)
                restored_active = int(transitions["restored_active"] or 0)
                connection.execute(
                    """
                    INSERT INTO source_presence_events(
                        entity_type,session_key,bill_id,document_id,source_entity_type,
                        source_id,previous_presence,new_presence,changed_at,run_id,details_json
                    )
                    SELECT 'bill',b.session_key,b.id,NULL,'Measure',
                           COALESCE(NULLIF(b.measure_id,''),b.bill_id_compact),
                           b.source_presence,
                           CASE WHEN b.last_collected_run_id=? THEN 'active' ELSE 'missing' END,
                           ?,?,?
                    FROM bills b
                    WHERE b.session_key=?
                      AND b.source_presence != CASE
                          WHEN b.last_collected_run_id=? THEN 'active' ELSE 'missing' END
                    """,
                    (run_id, timestamp, run_id, event_details, key, run_id),
                )
                connection.execute(
                    """
                    UPDATE bills
                    SET source_presence=CASE
                            WHEN last_collected_run_id=? THEN 'active' ELSE 'missing' END,
                        missing_from_source_since=CASE
                            WHEN last_collected_run_id=? THEN NULL
                            ELSE COALESCE(missing_from_source_since, ?) END,
                        last_source_reconciled_at=?
                    WHERE session_key=?
                    """,
                    (run_id, run_id, timestamp, timestamp, key),
                )
                counts = connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN source_presence='active' THEN 1 ELSE 0 END) AS active,
                           SUM(CASE WHEN source_presence='missing' THEN 1 ELSE 0 END) AS missing
                    FROM bills WHERE session_key=?
                    """,
                    (key,),
                ).fetchone()
            else:
                source_type = str(source_entity_type).strip()
                transitions = connection.execute(
                    """
                    SELECT COALESCE(SUM(CASE
                               WHEN last_seen_run_id=? THEN 0
                               WHEN source_presence!='missing' THEN 1 ELSE 0 END), 0)
                               AS marked_missing,
                           COALESCE(SUM(CASE
                               WHEN last_seen_run_id=? AND source_presence!='active'
                               THEN 1 ELSE 0 END), 0) AS restored_active
                    FROM documents
                    WHERE session_key=? AND source_entity_type=?
                      AND COALESCE(reconciliation_origin,'') != 'olis_only'
                    """,
                    (run_id, run_id, key, source_type),
                ).fetchone()
                marked_missing = int(transitions["marked_missing"] or 0)
                restored_active = int(transitions["restored_active"] or 0)
                connection.execute(
                    """
                    INSERT INTO source_presence_events(
                        entity_type,session_key,bill_id,document_id,source_entity_type,
                        source_id,previous_presence,new_presence,changed_at,run_id,details_json
                    )
                    SELECT 'document',d.session_key,d.bill_id,d.id,?,d.source_id,
                           d.source_presence,
                           CASE WHEN d.last_seen_run_id=? THEN 'active' ELSE 'missing' END,
                           ?,?,?
                    FROM documents d
                    WHERE d.session_key=? AND d.source_entity_type=?
                      AND COALESCE(d.reconciliation_origin,'') != 'olis_only'
                      AND d.source_presence != CASE
                          WHEN d.last_seen_run_id=? THEN 'active' ELSE 'missing' END
                    """,
                    (
                        source_type,
                        run_id,
                        timestamp,
                        run_id,
                        event_details,
                        key,
                        source_type,
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET source_presence=CASE
                            WHEN last_seen_run_id=? THEN 'active' ELSE 'missing' END,
                        missing_from_source_since=CASE
                            WHEN last_seen_run_id=? THEN NULL
                            ELSE COALESCE(missing_from_source_since, ?) END,
                        last_source_reconciled_at=?
                    WHERE session_key=? AND source_entity_type=?
                      AND COALESCE(reconciliation_origin,'') != 'olis_only'
                    """,
                    (run_id, run_id, timestamp, timestamp, key, source_type),
                )
                counts = connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN source_presence='active' THEN 1 ELSE 0 END) AS active,
                           SUM(CASE WHEN source_presence='missing' THEN 1 ELSE 0 END) AS missing
                    FROM documents WHERE session_key=? AND source_entity_type=?
                      AND COALESCE(reconciliation_origin,'') != 'olis_only'
                    """,
                    (key, source_type),
                ).fetchone()
            return {
                "records_reconciled": int(counts["total"] or 0),
                "active": int(counts["active"] or 0),
                "missing": int(counts["missing"] or 0),
                "marked_missing": marked_missing,
                "restored_active": restored_active,
            }

    def record_source_anomaly(
        self,
        anomaly_type: str,
        *,
        severity: str = "warning",
        affects_completeness: bool = False,
        message: str,
        session_key: str | None = None,
        bill_id: int | None = None,
        bill_id_compact: str | None = None,
        document_id: int | None = None,
        source_entity_type: str | None = None,
        source_id: str | int | None = None,
        source_url: str | None = None,
        previous_value: Any = None,
        current_value: Any = None,
        run_id: int | None = None,
        observed_at: str | datetime | None = None,
        details: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> int:
        timestamp = _utc_timestamp(observed_at)
        with self.database.transaction() as connection:
            return self._record_anomaly(
                connection,
                anomaly_type=anomaly_type,
                severity=severity,
                affects_completeness=affects_completeness,
                message=message,
                session_key=session_key,
                bill_id=bill_id,
                bill_id_compact=bill_id_compact,
                document_id=document_id,
                source_entity_type=source_entity_type,
                source_id=None if source_id is None else str(source_id),
                source_url=source_url,
                previous_value=previous_value,
                current_value=current_value,
                run_id=run_id,
                observed_at=timestamp,
                details=details,
                fingerprint=fingerprint,
            )

    def resolve_source_anomaly(
        self, anomaly_id: int, *, resolved_at: str | datetime | None = None
    ) -> None:
        timestamp = _utc_timestamp(resolved_at)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT session_key FROM source_anomalies WHERE id=?", (anomaly_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"source anomaly {anomaly_id} does not exist")
            connection.execute(
                "UPDATE source_anomalies SET resolved_at=? WHERE id=?", (timestamp, anomaly_id)
            )
            if row["session_key"]:
                self._refresh_session_anomaly_counts(
                    connection, str(row["session_key"]), timestamp
                )

    def resolve_source_anomalies_for_bill(
        self,
        bill_id: int,
        *,
        anomaly_types: Iterable[str],
        resolved_at: str | datetime | None = None,
    ) -> int:
        """Resolve stale bill-scoped diagnostics after a later successful check."""

        types = tuple(
            dict.fromkeys(
                str(value).strip() for value in anomaly_types if str(value).strip()
            )
        )
        if not types:
            return 0
        timestamp = _utc_timestamp(resolved_at)
        placeholders = ",".join("?" for _ in types)
        with self.database.transaction() as connection:
            bill = connection.execute(
                "SELECT session_key FROM bills WHERE id=?", (bill_id,)
            ).fetchone()
            if bill is None:
                raise KeyError(f"bill {bill_id} does not exist")
            changed = connection.execute(
                f"""
                UPDATE source_anomalies SET resolved_at=?
                WHERE bill_id=? AND resolved_at IS NULL
                  AND anomaly_type IN ({placeholders})
                """,
                (timestamp, bill_id, *types),
            ).rowcount
            self._refresh_session_anomaly_counts(
                connection, str(bill["session_key"]), timestamp
            )
            return int(changed)

    def resolve_source_anomalies_for_session(
        self,
        session_key: str,
        *,
        anomaly_types: Iterable[str],
        resolved_at: str | datetime | None = None,
    ) -> int:
        """Resolve stale session/source diagnostics after a successful recheck."""

        key = _required_nonempty(session_key, "session_key").upper()
        types = tuple(
            dict.fromkeys(
                str(value).strip() for value in anomaly_types if str(value).strip()
            )
        )
        if not types:
            return 0
        timestamp = _utc_timestamp(resolved_at)
        placeholders = ",".join("?" for _ in types)
        with self.database.transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE source_anomalies SET resolved_at=?
                WHERE session_key=? AND bill_id IS NULL AND resolved_at IS NULL
                  AND anomaly_type IN ({placeholders})
                """,
                (timestamp, key, *types),
            ).rowcount
            self._refresh_session_anomaly_counts(connection, key, timestamp)
            return int(changed)

    def list_source_anomalies(
        self,
        *,
        session_key: str | None = None,
        anomaly_type: str | None = None,
        severity: str | None = None,
        affects_completeness: bool | None = None,
        unresolved_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        where: list[str] = []
        params: list[Any] = []
        if session_key:
            where.append("session_key=?")
            params.append(session_key.strip().upper())
        if anomaly_type:
            where.append("anomaly_type=?")
            params.append(anomaly_type)
        if severity:
            where.append("severity=?")
            params.append(severity)
        if affects_completeness is not None:
            where.append("affects_completeness=?")
            params.append(int(affects_completeness))
        if unresolved_only:
            where.append("resolved_at IS NULL")
        sql = "SELECT * FROM source_anomalies"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_observed_at DESC,id DESC LIMIT ? OFFSET ?"
        params.extend((min(int(limit), 1000), max(0, int(offset))))
        return self._fetch_all(sql, params)

    # -- OLIS display reconciliation and remote probes ----------------------

    def record_olis_display_reconciliation(
        self,
        bill_id: int,
        status: str,
        *,
        source_entity_type: str,
        displayed_source_ids: Iterable[str | int] = (),
        run_id: int | None = None,
        checked_at: str | datetime | None = None,
        odata_record_count: int | None = None,
        displayed_record_count: int | None = None,
        page_only_count: int | None = None,
        odata_only_count: int | None = None,
        source_url: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one narrow OLIS page outcome and, only on success, display flags."""

        if status not in DISPLAY_RECONCILIATION_STATUSES:
            raise ValueError(f"invalid OLIS display reconciliation status: {status!r}")
        source_type = _required_nonempty(source_entity_type, "source_entity_type")
        counts = {
            "odata_record_count": odata_record_count,
            "displayed_record_count": displayed_record_count,
            "page_only_count": page_only_count,
            "odata_only_count": odata_only_count,
        }
        for name, value in counts.items():
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} must not be negative")
        displayed_ids = tuple(
            dict.fromkeys(
                str(value).strip() for value in displayed_source_ids if str(value).strip()
            )
        )
        timestamp = _utc_timestamp(checked_at)
        successful = status in {"checked_with_records", "checked_zero"}
        if successful and displayed_record_count is None:
            displayed_record_count = len(displayed_ids)
        with self.database.transaction() as connection:
            bill = connection.execute(
                "SELECT id,session_key,bill_id_compact FROM bills WHERE id=?", (bill_id,)
            ).fetchone()
            if bill is None:
                raise KeyError(f"bill {bill_id} does not exist")
            session_key = str(bill["session_key"])
            if successful:
                # A successfully parsed page establishes an explicit negative for
                # retained OData records not displayed.  Failures intentionally do
                # not enter this branch, preserving NULL/previous state.
                connection.execute(
                    """
                    UPDATE documents SET displayed_in_olis=0,display_reconciled_at=?
                    WHERE bill_id=? AND source_entity_type=?
                    """,
                    (timestamp, bill_id, source_type),
                )
                for chunk in _chunks(displayed_ids, 400):
                    placeholders = ",".join("?" for _ in chunk)
                    connection.execute(
                        f"""
                        UPDATE documents SET displayed_in_olis=1,display_reconciled_at=?
                        WHERE bill_id=? AND source_entity_type=?
                          AND source_id IN ({placeholders})
                        """,
                        (timestamp, bill_id, source_type, *chunk),
                    )
            connection.execute(
                """
                INSERT INTO olis_display_reconciliations(
                    bill_id,source_entity_type,session_key,status,checked_at,run_id,
                    odata_record_count,displayed_record_count,page_only_count,
                    odata_only_count,source_url,details_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bill_id,source_entity_type) DO UPDATE SET
                    session_key=excluded.session_key,status=excluded.status,
                    checked_at=excluded.checked_at,run_id=excluded.run_id,
                    odata_record_count=excluded.odata_record_count,
                    displayed_record_count=excluded.displayed_record_count,
                    page_only_count=excluded.page_only_count,
                    odata_only_count=excluded.odata_only_count,
                    source_url=excluded.source_url,details_json=excluded.details_json
                """,
                (
                    bill_id,
                    source_type,
                    session_key,
                    status,
                    timestamp,
                    run_id,
                    odata_record_count,
                    displayed_record_count,
                    page_only_count,
                    odata_only_count,
                    source_url,
                    _json_text(details or {}),
                ),
            )
            connection.execute(
                """
                INSERT INTO session_archive_state(
                    session_key,display_reconciliation_status,last_testimony_reconciled_at,
                    updated_at
                ) VALUES (?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    display_reconciliation_status=excluded.display_reconciliation_status,
                    last_testimony_reconciled_at=CASE
                        WHEN excluded.last_testimony_reconciled_at IS NOT NULL
                        THEN excluded.last_testimony_reconciled_at
                        ELSE session_archive_state.last_testimony_reconciled_at END,
                    updated_at=excluded.updated_at
                """,
                (session_key, status, timestamp if successful else None, timestamp),
            )
            row = connection.execute(
                """
                SELECT * FROM olis_display_reconciliations
                WHERE bill_id=? AND source_entity_type=?
                """,
                (bill_id, source_type),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_olis_display_reconciliation(
        self,
        bill_id: int,
        source_entity_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one family result, retaining deterministic legacy lookup behavior."""

        if source_entity_type is not None:
            source_type = _required_nonempty(
                source_entity_type, "source_entity_type"
            )
            return self._fetch_one(
                """
                SELECT * FROM olis_display_reconciliations
                WHERE bill_id=? AND source_entity_type=?
                """,
                (bill_id, source_type),
            )
        return self._fetch_one(
            """
            SELECT * FROM olis_display_reconciliations
            WHERE bill_id=?
            ORDER BY CASE source_entity_type
                WHEN 'CommitteePublicTestimony' THEN 0 ELSE 1 END,
                source_entity_type
            LIMIT 1
            """,
            (bill_id,),
        )

    def list_olis_display_reconciliations(
        self, bill_id: int
    ) -> list[dict[str, Any]]:
        """Return every independently retained source-family result for a bill."""

        return self._fetch_all(
            """
            SELECT * FROM olis_display_reconciliations
            WHERE bill_id=?
            ORDER BY source_entity_type
            """,
            (bill_id,),
        )

    def record_document_probe(
        self,
        document_id: int,
        *,
        status: str,
        run_id: int | None = None,
        probed_at: str | datetime | None = None,
        http_status: int | None = None,
        final_url: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        probe_status = _required_nonempty(status, "status")
        if content_length is not None and int(content_length) < 0:
            raise ValueError("content_length must not be negative")
        timestamp = _utc_timestamp(probed_at)
        with self.database.transaction() as connection:
            if connection.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
                raise KeyError(f"document {document_id} does not exist")
            connection.execute(
                """
                INSERT INTO document_remote_probes(
                    document_id,run_id,probe_status,probed_at,http_status,final_url,
                    content_type,content_length,etag,last_modified,error_class,error_message,
                    details_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                    run_id=excluded.run_id,probe_status=excluded.probe_status,
                    probed_at=excluded.probed_at,http_status=excluded.http_status,
                    final_url=excluded.final_url,content_type=excluded.content_type,
                    content_length=excluded.content_length,etag=excluded.etag,
                    last_modified=excluded.last_modified,error_class=excluded.error_class,
                    error_message=excluded.error_message,details_json=excluded.details_json
                """,
                (
                    document_id,
                    run_id,
                    probe_status,
                    timestamp,
                    http_status,
                    final_url,
                    content_type,
                    content_length,
                    etag,
                    last_modified,
                    error_class,
                    error_message,
                    _json_text(details or {}),
                ),
            )

    def get_document_probe(self, document_id: int) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM document_remote_probes WHERE document_id=?", (document_id,)
        )

    # -- bounded database-backed retry/archive download claims --------------

    def snapshot_retry_matching_items(
        self,
        run_id: int,
        *,
        source_run_id: int | None = None,
        session_key: str | None = None,
        bill_id_compact: str | None = None,
        include_terminal: bool = True,
        queued_at: str | datetime | None = None,
    ) -> int:
        """Freeze all filtered retry candidates as durable run items in SQL."""

        timestamp = _utc_timestamp(queued_at)
        statuses = set(MATCHING_RETRY_DOWNLOAD_STATUSES)
        if not include_terminal:
            statuses.discard("failed_terminal")
        status_marks = ",".join("?" for _ in statuses)
        kind_marks = ",".join("?" for _ in RETRY_PAYLOAD_DOCUMENT_KINDS)
        where = [
            # Documents have normalized session keys. Keep legacy rows out of
            # an unfiltered retry snapshot even when no UI/CLI filter is given.
            "CAST(substr(d.session_key,1,4) AS INTEGER) >= 2014",
            f"d.download_status IN ({status_marks})",
            f"d.document_kind IN ({kind_marks})",
            "NULLIF(trim(d.canonical_download_url),'') IS NOT NULL",
        ]
        params: list[Any] = [*sorted(statuses), *sorted(RETRY_PAYLOAD_DOCUMENT_KINDS)]
        if source_run_id is not None:
            where.append(
                "EXISTS ("
                "SELECT 1 FROM collection_run_items source_item "
                "WHERE source_item.run_id=? "
                "AND source_item.item_type='document' "
                "AND source_item.document_id=d.id"
                ")"
            )
            params.append(int(source_run_id))
        if session_key:
            where.append("d.session_key=?")
            params.append(str(session_key).strip().upper())
        if bill_id_compact:
            where.append("d.bill_id_compact=?")
            params.append(str(bill_id_compact).replace(" ", "").strip().upper())

        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT run_type,status FROM collection_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"collection run {run_id} does not exist")
            if run["run_type"] != "retry_failures" or run["status"] != "queued":
                raise ValueError("matching retry items require a queued retry_failures run")
            connection.execute(
                f"""
                INSERT INTO collection_run_items(
                    run_id,item_type,item_key,session_key,bill_id,document_id,stage,
                    status,current_activity,queued_at,updated_at,details_json
                )
                SELECT ?, 'document', 'document:' || d.id, d.session_key, d.bill_id,
                       d.id, 'download_documents', 'queued',
                       'Waiting for matching retry', ?, ?,
                       '{{"selection":"all_matching"}}'
                FROM documents d
                WHERE {' AND '.join(where)}
                ORDER BY d.id
                """,
                (run_id, timestamp, timestamp, *params),
            )
            matching_count = int(connection.execute("SELECT changes()").fetchone()[0])
            connection.execute(
                """
                UPDATE collection_runs
                SET documents_discovered=?,documents_queued=?,updated_at=?
                WHERE id=?
                """,
                (matching_count, matching_count, timestamp, run_id),
            )
        return matching_count

    def claim_next_retry_document(
        self,
        run_id: int,
        *,
        attempted_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one item from an exact all-matching retry snapshot."""

        timestamp = _utc_timestamp(attempted_at)
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT run_type,status,requested_scope_json FROM collection_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"collection run {run_id} does not exist")
            if run["run_type"] != "retry_failures":
                raise ValueError("retry document claims require a retry_failures run")
            if run["status"] != "running":
                return None
            scope = _json_object(run["requested_scope_json"])
            if scope.get("selection") != "all_matching":
                raise ValueError("retry run does not contain an all-matching snapshot")
            retry_match = _json_object(scope.get("retry_match"))
            requested_statuses = retry_match.get("eligible_statuses", ())
            if isinstance(requested_statuses, str):
                requested_statuses = (requested_statuses,)
            eligible_statuses = {str(value) for value in requested_statuses}
            invalid_statuses = eligible_statuses - MATCHING_RETRY_DOWNLOAD_STATUSES
            if invalid_statuses or not eligible_statuses:
                raise ValueError("retry run has invalid frozen eligible statuses")
            claimable_statuses = {*eligible_statuses, "queued"}

            while True:
                candidate = connection.execute(
                    """
                    SELECT d.*,i.id AS retry_run_item_id
                    FROM collection_run_items i
                    JOIN documents d ON d.id=i.document_id
                    WHERE i.run_id=? AND i.item_type='document' AND i.status='queued'
                    ORDER BY i.document_id
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if candidate is None:
                    return None
                item_id = int(candidate["retry_run_item_id"])
                previous_status = str(candidate["download_status"])
                no_longer_eligible = (
                    previous_status not in claimable_statuses
                    or str(candidate["document_kind"]) not in RETRY_PAYLOAD_DOCUMENT_KINDS
                    or not str(candidate["canonical_download_url"] or "").strip()
                )
                if no_longer_eligible:
                    connection.execute(
                        """
                        UPDATE collection_run_items
                        SET status='skipped',current_activity=?,started_at=COALESCE(started_at,?),
                            finished_at=?,updated_at=?
                        WHERE id=? AND status='queued'
                        """,
                        (
                            "Skipped because the document is no longer eligible "
                            f"(status: {previous_status})",
                            timestamp,
                            timestamp,
                            timestamp,
                            item_id,
                        ),
                    )
                    continue
                changed = connection.execute(
                    """
                    UPDATE documents
                    SET download_status='downloading',attempt_count=attempt_count+1,
                        last_attempt_at=?,last_error=NULL
                    WHERE id=? AND download_status=?
                    """,
                    (timestamp, int(candidate["id"]), previous_status),
                ).rowcount
                if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE is defensive
                    continue
                connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status='running',current_activity='Downloading and validating payload',
                        attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),
                        finished_at=NULL,interrupted_at=NULL,updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (timestamp, timestamp, item_id),
                )
                result = dict(candidate)
                result.pop("retry_run_item_id", None)
                result["download_status"] = "downloading"
                result["run_item_id"] = item_id
                return result

    def claim_next_archive_document(
        self,
        run_id: int,
        *,
        attempted_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Lazily claim one document from a frozen Download Archive scope.

        Invalid durable queued items are finalized one transaction at a time.
        That keeps each writer-lock hold bounded while this public call continues
        to the next queued item or fresh keyset candidate.
        """

        timestamp = _utc_timestamp(attempted_at)
        while True:
            result = self._claim_next_archive_document_once(
                run_id,
                attempted_at=timestamp,
            )
            if result is _ARCHIVE_QUEUED_ITEM_SKIPPED:
                continue
            assert result is None or isinstance(result, dict)
            return result

    def _claim_next_archive_document_once(
        self,
        run_id: int,
        *,
        attempted_at: str | datetime | None = None,
    ) -> dict[str, Any] | None | object:
        """Perform one bounded archive claim or queued-item finalization."""

        timestamp = _utc_timestamp(attempted_at)
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM collection_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"collection run {run_id} does not exist")
            if run["run_type"] != "download_archive":
                raise ValueError("archive document claims require a download_archive run")
            if run["status"] != "running":
                return None
            scope = _json_object(run["requested_scope_json"])
            sessions = _normalized_session_keys(scope.get("session_keys", ()))
            cutoff = run["scope_cutoff_at"] or scope.get("scope_cutoff_at")
            if not sessions or not cutoff:
                raise ValueError("download_archive run has no frozen session scope/cutoff")
            cutoff = _utc_timestamp(cutoff)
            requested_statuses = scope.get("eligible_statuses")
            if requested_statuses is None:
                eligible_statuses = set(RETRYABLE_DOWNLOAD_STATUSES)
                if scope.get("retryable_failures_only"):
                    eligible_statuses = {
                        "failed_retryable",
                        "paused_low_space",
                        "interrupted",
                        "missing_local",
                        "changed_remote",
                    }
                if scope.get("include_terminal"):
                    eligible_statuses.add("failed_terminal")
            else:
                if isinstance(requested_statuses, str):
                    requested_statuses = (requested_statuses,)
                eligible_statuses = {str(value) for value in requested_statuses}
            invalid_statuses = eligible_statuses - ARCHIVE_CLAIMABLE_DOWNLOAD_STATUSES
            if invalid_statuses:
                raise ValueError(
                    f"unclaimable download statuses in frozen scope: {sorted(invalid_statuses)}"
                )
            if not eligible_statuses:
                raise ValueError(
                    "download_archive run has no frozen eligible download statuses"
                )
            requested_kinds = scope.get("document_kinds")
            document_kinds: tuple[str, ...] | None = None
            if requested_kinds is not None:
                if isinstance(requested_kinds, str):
                    requested_kinds = (requested_kinds,)
                document_kinds = tuple(dict.fromkeys(str(value) for value in requested_kinds))
                invalid_kinds = set(document_kinds) - DOCUMENT_KINDS
                if invalid_kinds:
                    raise ValueError(
                        f"invalid document kinds in frozen scope: {sorted(invalid_kinds)}"
                    )
                if not document_kinds:
                    raise ValueError(
                        "download_archive run has no frozen document kinds"
                    )
            eligible_statuses = tuple(sorted(eligible_statuses))
            status_marks = ",".join("?" for _ in eligible_statuses)
            # A control transition may temporarily rewrite an already-owned
            # document to queued/interrupted/low-space even when that value was
            # not in a narrow frozen failure scope. Other status changes must
            # still honor the run's frozen eligible-status selection.
            resumable_statuses = tuple(
                sorted(
                    set(eligible_statuses)
                    | {"queued", "interrupted", "paused_low_space"}
                )
            )
            presence_states = tuple(
                sorted(
                    SOURCE_PRESENCE_STATES
                    if scope.get("include_source_missing")
                    else {"active", "unknown"}
                )
            )
            presence_marks = ",".join("?" for _ in presence_states)
            # Resume durable queued claims first. Select them without document
            # filters so a row whose source/status changed while paused cannot
            # remain queued forever. An ineligible row receives a durable skip;
            # the public wrapper then continues in a new bounded transaction.
            cursor_position: tuple[int, int] | None = None
            queued = connection.execute(
                """
                SELECT d.*,i.id AS existing_run_item_id,
                       i.document_id AS queued_document_id,
                       i.details_json AS existing_run_item_details_json
                FROM collection_run_items i
                LEFT JOIN documents d ON d.id=i.document_id
                WHERE i.run_id=? AND i.item_type='document' AND i.status='queued'
                ORDER BY i.document_id,i.id
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            candidate = queued
            if queued is not None:
                skip_reason = _archive_queued_skip_reason(
                    dict(queued),
                    sessions=sessions,
                    presence_states=presence_states,
                    cutoff=cutoff,
                    document_kinds=document_kinds,
                    resumable_statuses=resumable_statuses,
                )
                if skip_reason is not None:
                    details = _json_object(
                        queued["existing_run_item_details_json"] or "{}"
                    )
                    details.update(
                        {
                            "archive_resume_skip": skip_reason,
                            "skipped_at": timestamp,
                        }
                    )
                    skipped = connection.execute(
                        """
                        UPDATE collection_run_items
                        SET status='skipped',current_activity=?,
                            started_at=COALESCE(started_at,?),finished_at=?,
                            interrupted_at=NULL,updated_at=?,
                            details_json=?
                        WHERE id=? AND run_id=? AND status='queued'
                        """,
                        (
                            f"Skipped queued archive item because {skip_reason}",
                            timestamp,
                            timestamp,
                            timestamp,
                            _json_text(details),
                            int(queued["existing_run_item_id"]),
                            run_id,
                        ),
                    ).rowcount
                    if skipped != 1:  # pragma: no cover - BEGIN IMMEDIATE owns the row
                        raise RuntimeError("queued archive item lost its skip transition")
                    return _ARCHIVE_QUEUED_ITEM_SKIPPED

            if candidate is None:
                kind_where = ""
                kind_params: tuple[str, ...] = ()
                if document_kinds is not None:
                    kind_marks = ",".join("?" for _ in document_kinds)
                    kind_where = f"AND d.document_kind IN ({kind_marks})"
                    kind_params = document_kinds

                open_cursor_sql = """
                    SELECT session_ordinal,session_key,after_document_id
                    FROM archive_claim_cursors
                    WHERE run_id=? AND exhausted=0
                    ORDER BY session_ordinal
                    LIMIT 1
                """
                cursor = connection.execute(open_cursor_sql, (run_id,)).fetchone()
                if cursor is None:
                    initialized = connection.execute(
                        """
                        SELECT 1 FROM archive_claim_cursors
                        WHERE run_id=? LIMIT 1
                        """,
                        (run_id,),
                    ).fetchone()
                    if initialized is not None:
                        return None
                    # Initialize the frozen sessions exactly once. This lazy path
                    # keeps runs created before migration 005 resumable without
                    # repeating one uniqueness probe per session for every claim.
                    connection.executemany(
                        """
                        INSERT INTO archive_claim_cursors(
                            run_id,session_ordinal,session_key,after_document_id,
                            exhausted,updated_at
                        ) VALUES (?,?,?,0,0,?)
                        """,
                        (
                            (run_id, ordinal, session_key, timestamp)
                            for ordinal, session_key in enumerate(sessions)
                        ),
                    )
                    cursor = connection.execute(open_cursor_sql, (run_id,)).fetchone()
                    if cursor is None:  # pragma: no cover - sessions is non-empty
                        raise RuntimeError("archive claim cursor initialization failed")

                while cursor is not None:
                    session_ordinal = int(cursor["session_ordinal"])
                    session_key = str(cursor["session_key"])
                    after_document_id = int(cursor["after_document_id"])
                    if (
                        session_ordinal >= len(sessions)
                        or sessions[session_ordinal] != session_key
                    ):
                        raise ValueError(
                            "archive claim cursor does not match the frozen session scope"
                        )
                    candidate = connection.execute(
                        f"""
                        SELECT d.*,NULL AS existing_run_item_id,
                               NULL AS existing_run_item_details_json
                        FROM documents d
                        WHERE d.session_key=? AND d.id>?
                          AND d.canonical_download_url IS NOT NULL
                          AND trim(d.canonical_download_url)<>''
                          AND d.download_status IN ({_ARCHIVE_WALK_STATUS_SQL})
                          AND d.source_presence IN ({presence_marks})
                          AND d.download_status IN ({status_marks})
                          AND d.first_seen_at<=?
                          {kind_where}
                          AND NOT EXISTS (
                              SELECT 1 FROM collection_run_items existing
                              WHERE existing.run_id=?
                                AND existing.item_type='document'
                                AND existing.document_id=d.id
                          )
                        ORDER BY d.id
                        LIMIT 1
                        """,
                        (
                            session_key,
                            after_document_id,
                            *presence_states,
                            *eligible_statuses,
                            cutoff,
                            *kind_params,
                            run_id,
                        ),
                    ).fetchone()
                    if candidate is not None:
                        cursor_position = (session_ordinal, int(candidate["id"]))
                        break
                    connection.execute(
                        """
                        UPDATE archive_claim_cursors
                        SET exhausted=1,updated_at=?
                        WHERE run_id=? AND session_ordinal=? AND exhausted=0
                        """,
                        (timestamp, run_id, session_ordinal),
                    )
                    cursor = connection.execute(open_cursor_sql, (run_id,)).fetchone()
                if candidate is None:
                    return None
            document_id = int(candidate["id"])
            previous_status = str(candidate["download_status"])
            changed = connection.execute(
                """
                UPDATE documents
                SET download_status='downloading',attempt_count=attempt_count+1,
                    last_attempt_at=?,last_error=NULL
                WHERE id=? AND download_status=?
                """,
                (timestamp, document_id, previous_status),
            ).rowcount
            if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE is defensive
                return None
            existing_item_id = candidate["existing_run_item_id"]
            if existing_item_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO collection_run_items(
                        run_id,item_type,item_key,session_key,bill_id,document_id,stage,
                        status,current_activity,attempt_count,queued_at,started_at,updated_at,
                        details_json
                    ) VALUES (?, 'document', ?, ?, ?, ?, 'download_archive', 'running',
                              'Downloading and validating payload', 1, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        f"document:{document_id}",
                        candidate["session_key"],
                        candidate["bill_id"],
                        document_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        _json_text({"previous_download_status": previous_status}),
                    ),
                )
                run_item_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    UPDATE collection_runs
                    SET documents_queued=documents_queued+1,updated_at=? WHERE id=?
                    """,
                    (timestamp, run_id),
                )
            else:
                run_item_id = int(existing_item_id)
                changed_item = connection.execute(
                    """
                    UPDATE collection_run_items
                    SET status='running',stage='download_archive',
                        current_activity='Downloading and validating payload',
                        attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),
                        finished_at=NULL,interrupted_at=NULL,updated_at=?
                    WHERE id=? AND run_id=? AND status='queued'
                    """,
                    (timestamp, timestamp, run_item_id, run_id),
                ).rowcount
                if changed_item != 1:  # pragma: no cover - selected predicate guarantees it
                    raise RuntimeError("archive document run item lost its claim")
            if cursor_position is not None:
                session_ordinal, after_document_id = cursor_position
                advanced = connection.execute(
                    """
                    UPDATE archive_claim_cursors
                    SET after_document_id=?,updated_at=?
                    WHERE run_id=? AND session_ordinal=?
                      AND exhausted=0 AND after_document_id<?
                    """,
                    (
                        after_document_id,
                        timestamp,
                        run_id,
                        session_ordinal,
                        after_document_id,
                    ),
                ).rowcount
                if advanced != 1:  # pragma: no cover - BEGIN IMMEDIATE owns the cursor
                    raise RuntimeError("archive document cursor lost its claim position")
            result = dict(candidate)
            result.pop("existing_run_item_id", None)
            result.pop("queued_document_id", None)
            result.pop("existing_run_item_details_json", None)
            result["download_status"] = "downloading"
            result["run_item_id"] = run_item_id
            return result

    def release_archive_document_claim(
        self,
        run_id: int,
        document_id: int,
        *,
        document_status: str,
        item_status: str,
        message: str | None = None,
        changed_at: str | datetime | None = None,
    ) -> bool:
        """Atomically release an owned active claim after failure/pause/interruption."""

        if document_status not in DOWNLOAD_STATUSES - {"downloading", "downloaded"}:
            raise ValueError(f"invalid released document status: {document_status!r}")
        if item_status not in RUN_ITEM_STATUSES - {"running", "completed", "skipped"}:
            raise ValueError(f"invalid released run-item status: {item_status!r}")
        timestamp = _utc_timestamp(changed_at)
        with self.database.transaction() as connection:
            item = connection.execute(
                """
                SELECT id FROM collection_run_items
                WHERE run_id=? AND item_type='document' AND document_id=?
                  AND status IN ('running','paused','interrupted','canceled')
                """,
                (run_id, document_id),
            ).fetchone()
            if item is None:
                return False
            changed = connection.execute(
                """
                UPDATE documents SET download_status=?,last_error=?
                WHERE id=? AND download_status='downloading'
                """,
                (document_status, message, document_id),
            ).rowcount
            if changed != 1:
                return False
            connection.execute(
                """
                UPDATE collection_run_items
                SET status=?,current_activity=COALESCE(?,current_activity),
                    finished_at=CASE WHEN ?='paused' THEN NULL ELSE ? END,updated_at=?
                WHERE id=?
                """,
                (item_status, message, item_status, timestamp, timestamp, int(item["id"])),
            )
            return True

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
            connection.execute(
                """
                UPDATE session_archive_state
                SET inventory_status='interrupted',updated_at=?
                WHERE inventory_status='inventory_running'
                  AND last_inventory_run_id IN (
                      SELECT id FROM collection_runs WHERE status='interrupted'
                  )
                """,
                (timestamp,),
            )
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
        sponsor: str | None = None,
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
        if sponsor:
            where.append(
                "EXISTS (SELECT 1 FROM bill_sponsors sx WHERE sx.bill_id=b.id "
                "AND (sx.resolved_display_name LIKE ? OR sx.legislator_code LIKE ? "
                "OR sx.committee_code LIKE ?))"
            )
            needle = f"%{sponsor.strip()}%"
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
        organization: str | None = None,
        testimony_position: str | None = None,
        download_status: str | None = None,
        source_presence: str | None = None,
        displayed_in_olis: bool | None = None,
        display_unknown: bool = False,
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
            "d.source_presence": source_presence,
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
        if organization:
            where.append("d.city_organization LIKE ?")
            params.append(f"%{organization.strip()}%")
        if display_unknown:
            where.append("d.displayed_in_olis IS NULL")
        elif displayed_in_olis is not None:
            where.append("d.displayed_in_olis = ?")
            params.append(int(displayed_in_olis))
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

    def _record_presence_event(
        self,
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        session_key: str,
        bill_id: int,
        source_entity_type: str,
        source_id: str,
        previous_presence: str,
        new_presence: str,
        changed_at: str,
        document_id: int | None = None,
        run_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        if entity_type not in {"bill", "document"}:
            raise ValueError("invalid source-presence entity type")
        if previous_presence not in SOURCE_PRESENCE_STATES:
            raise ValueError(f"invalid previous source presence: {previous_presence!r}")
        if new_presence not in SOURCE_PRESENCE_STATES:
            raise ValueError(f"invalid new source presence: {new_presence!r}")
        cursor = connection.execute(
            """
            INSERT INTO source_presence_events(
                entity_type,session_key,bill_id,document_id,source_entity_type,source_id,
                previous_presence,new_presence,changed_at,run_id,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entity_type,
                session_key,
                bill_id,
                document_id,
                source_entity_type,
                source_id,
                previous_presence,
                new_presence,
                changed_at,
                run_id,
                _json_text(details or {}),
            ),
        )
        return int(cursor.lastrowid)

    def _record_anomaly(
        self,
        connection: sqlite3.Connection,
        *,
        anomaly_type: str,
        severity: str,
        affects_completeness: bool,
        message: str,
        observed_at: str,
        session_key: str | None = None,
        bill_id: int | None = None,
        bill_id_compact: str | None = None,
        document_id: int | None = None,
        source_entity_type: str | None = None,
        source_id: str | None = None,
        source_url: str | None = None,
        previous_value: Any = None,
        current_value: Any = None,
        run_id: int | None = None,
        details: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> int:
        kind = _required_nonempty(anomaly_type, "anomaly_type")
        if severity not in ANOMALY_SEVERITIES:
            raise ValueError(f"invalid anomaly severity: {severity!r}")
        text = _required_nonempty(message, "message")[:2000]
        if document_id is not None:
            document = connection.execute(
                """
                SELECT session_key,bill_id,bill_id_compact,source_entity_type,source_id
                FROM documents WHERE id=?
                """,
                (document_id,),
            ).fetchone()
            if document is None:
                raise KeyError(f"document {document_id} does not exist")
            session_key = session_key or str(document["session_key"])
            bill_id = bill_id or int(document["bill_id"])
            bill_id_compact = bill_id_compact or str(document["bill_id_compact"])
            source_entity_type = source_entity_type or str(document["source_entity_type"])
            source_id = source_id or str(document["source_id"])
        if bill_id is not None and (session_key is None or bill_id_compact is None):
            bill = connection.execute(
                "SELECT session_key,bill_id_compact FROM bills WHERE id=?", (bill_id,)
            ).fetchone()
            if bill is None:
                raise KeyError(f"bill {bill_id} does not exist")
            session_key = session_key or str(bill["session_key"])
            bill_id_compact = bill_id_compact or str(bill["bill_id_compact"])
        if session_key is not None:
            session_key = session_key.strip().upper()
        previous_json = None if previous_value is None else _json_text(previous_value)
        current_json = None if current_value is None else _json_text(current_value)
        if fingerprint is None:
            fingerprint = hash_sha256(
                "\x1f".join(
                    str(value or "")
                    for value in (
                        kind,
                        session_key,
                        bill_id_compact,
                        document_id,
                        source_entity_type,
                        source_id,
                        previous_json,
                        current_json,
                        text,
                    )
                ).encode("utf-8")
            ).hexdigest()
        else:
            fingerprint = _normalized_sha256(fingerprint)
        connection.execute(
            """
            INSERT INTO source_anomalies(
                anomaly_fingerprint,anomaly_type,severity,affects_completeness,
                session_key,bill_id,bill_id_compact,document_id,source_entity_type,
                source_id,source_url,message,previous_value_json,current_value_json,
                first_run_id,last_run_id,first_observed_at,last_observed_at,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(anomaly_fingerprint) DO UPDATE SET
                severity=excluded.severity,
                affects_completeness=excluded.affects_completeness,
                source_url=COALESCE(excluded.source_url,source_anomalies.source_url),
                message=excluded.message,last_run_id=excluded.last_run_id,
                last_observed_at=excluded.last_observed_at,
                occurrence_count=source_anomalies.occurrence_count+1,
                details_json=excluded.details_json,resolved_at=NULL
            """,
            (
                fingerprint,
                kind,
                severity,
                int(affects_completeness),
                session_key,
                bill_id,
                bill_id_compact,
                document_id,
                source_entity_type,
                source_id,
                source_url,
                text,
                previous_json,
                current_json,
                run_id,
                run_id,
                observed_at,
                observed_at,
                _json_text(details or {}),
            ),
        )
        row = connection.execute(
            "SELECT id FROM source_anomalies WHERE anomaly_fingerprint=?", (fingerprint,)
        ).fetchone()
        assert row is not None
        if session_key:
            self._refresh_session_anomaly_counts(connection, session_key, observed_at)
        return int(row["id"])

    @staticmethod
    def _refresh_session_anomaly_counts(
        connection: sqlite3.Connection, session_key: str, changed_at: str
    ) -> None:
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN affects_completeness=1 THEN 1 ELSE 0 END),0)
                       AS material
            FROM source_anomalies
            WHERE session_key=? AND resolved_at IS NULL
            """,
            (session_key,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO session_archive_state(
                session_key,source_anomaly_count,material_anomaly_count,updated_at
            ) VALUES (?,?,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                source_anomaly_count=excluded.source_anomaly_count,
                material_anomaly_count=excluded.material_anomaly_count,
                updated_at=excluded.updated_at
            """,
            (session_key, int(counts["total"]), int(counts["material"]), changed_at),
        )

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
    "current_version_id", "source_presence", "missing_from_source_since",
    "last_source_reconciled_at", "displayed_in_olis", "display_reconciled_at",
    "reconciliation_origin",
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


def _archive_queued_skip_reason(
    document: Mapping[str, Any],
    *,
    sessions: Sequence[str],
    presence_states: Sequence[str],
    cutoff: str,
    document_kinds: Sequence[str] | None,
    resumable_statuses: Sequence[str],
) -> str | None:
    if document.get("id") is None:
        return "the queued item has no stored document"
    session_key = str(document.get("session_key") or "")
    if session_key not in sessions:
        return f"session {session_key or '(missing)'} is outside the frozen scope"
    if str(document.get("source_presence") or "") not in presence_states:
        return "the source record is no longer in the selected presence scope"
    if _utc_timestamp(document.get("first_seen_at")) > cutoff:
        return "the document was first seen after the frozen scope cutoff"
    if document_kinds is not None and str(document.get("document_kind") or "") not in document_kinds:
        return "the document kind is no longer selected"
    if not str(document.get("canonical_download_url") or "").strip():
        return "the document no longer has an official download URL"
    status = str(document.get("download_status") or "")
    if status not in resumable_statuses:
        return f"document status is now {status or '(missing)'}"
    return None


def _required_nonempty(value: Any, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _normalized_session_keys(values: Iterable[str] | Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        values = (values,)
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        key = str(value).strip().upper()
        if not key:
            raise ValueError("session keys must not be empty")
        if key not in seen:
            seen.add(key)
            result.append(key)
    return tuple(result)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("requested_scope_json is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("requested_scope_json must contain a JSON object")
    return dict(parsed)


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


# Backward-friendly short name for call sites that prefer ``Storage``.
Storage = StorageService


__all__ = [
    "ANOMALY_SEVERITIES",
    "ARCHIVE_CLAIMABLE_DOWNLOAD_STATUSES",
    "DISPLAY_RECONCILIATION_STATUSES",
    "DOCUMENT_KINDS",
    "DOWNLOAD_STATUSES",
    "HISTORICAL_RUN_TYPES",
    "INVENTORY_STATUSES",
    "MATCHING_RETRY_DOWNLOAD_STATUSES",
    "RETRY_PAYLOAD_DOCUMENT_KINDS",
    "RETRYABLE_DOWNLOAD_STATUSES",
    "RUN_ITEM_STATUSES",
    "RUN_STATUSES",
    "RUN_TYPES",
    "SOURCE_PRESENCE_STATES",
    "Storage",
    "StorageService",
    "utc_now",
]
