"""Shared normalization for session, reference, and committee-context rows."""

from __future__ import annotations

from typing import Any, Mapping

from .source_mapping import chamber_for_code


def map_session(raw: Mapping[str, Any]) -> dict[str, Any]:
    key = str(raw["SessionKey"])
    return {
        "session_key": key,
        "source_session_id": key,
        "session_name": text(raw.get("SessionName")),
        "session_type": text(raw.get("SessionType")),
        "session_year": int(key[:4]),
        "begin_date": text(raw.get("BeginDate")),
        "end_date": text(raw.get("EndDate")),
        "source_url": f"https://olis.oregonlegislature.gov/liz/{key}",
        "source_created_at": text(raw.get("CreatedDate")),
        "source_modified_at": text(raw.get("ModifiedDate")),
        "raw_json": dict(raw),
    }


def map_legislator(raw: Mapping[str, Any]) -> dict[str, Any]:
    first = text(raw.get("FirstName"))
    last = text(raw.get("LastName"))
    return {
        "session_key": str(raw["SessionKey"]),
        "legislator_code": str(raw["LegislatorCode"]),
        "source_legislator_id": text(raw.get("LegislatorId")),
        "first_name": first,
        "middle_name": text(raw.get("MiddleName")),
        "last_name": last,
        "suffix": text(raw.get("Suffix")),
        "display_name": " ".join(part for part in (first, last) if part),
        "chamber": chamber_for_code(raw.get("Chamber")),
        "party": text(raw.get("Party")),
        "district": text(raw.get("DistrictNumber")),
        "email": text(raw.get("EmailAddress")),
        "active": bool_value(raw.get("Active")),
        "source_created_at": text(raw.get("CreatedDate")),
        "source_modified_at": text(raw.get("ModifiedDate")),
        "raw_json": dict(raw),
    }


def map_committee(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_key": str(raw["SessionKey"]),
        "committee_code": str(raw["CommitteeCode"]),
        "source_committee_id": text(raw.get("CommitteeId")),
        "committee_name": text(raw.get("CommitteeName")),
        "house_of_action": text(raw.get("HouseOfAction")),
        "chamber": chamber_for_code(raw.get("HouseOfAction")),
        "committee_type": text(raw.get("CommitteeType")),
        "source_created_at": text(raw.get("CreatedDate")),
        "source_modified_at": text(raw.get("ModifiedDate")),
        "raw_json": dict(raw),
    }


def committee_display(raw: Mapping[str, Any]) -> str:
    kind = text(raw.get("CommitteeType")) or ""
    name = text(raw.get("CommitteeName")) or str(raw.get("CommitteeCode") or "")
    if kind and kind.casefold() not in name.casefold():
        return f"{kind} {name}".strip()
    return name


def meeting_source_id(raw: Mapping[str, Any]) -> str:
    return f"{raw['CommitteeCode']}|{raw['MeetingDate']}"


def map_meeting(
    raw: Mapping[str, Any],
    committee_id: int | None,
    committee_name: str | None,
) -> dict[str, Any]:
    return {
        "session_key": str(raw["SessionKey"]),
        "source_meeting_id": meeting_source_id(raw),
        "committee_id": committee_id,
        "committee_code": text(raw.get("CommitteeCode")),
        "committee_name": committee_name,
        "meeting_date": text(raw.get("MeetingDate")),
        "location": text(raw.get("Location")) or text(raw.get("AlternateLocation")),
        "meeting_type": text(raw.get("MeetingStatus")) or text(raw.get("MeetingType")),
        "agenda_url": text(raw.get("AgendaUrl")),
        "source_url": text(raw.get("AgendaUrl")),
        "source_created_at": text(raw.get("CreatedDate")),
        "source_modified_at": text(raw.get("ModifiedDate")),
        "raw_json": dict(raw),
    }


def text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def integer(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def bool_value(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        return int(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y"}:
        return 1
    if normalized in {"0", "false", "no", "n"}:
        return 0
    return None


__all__ = [
    "bool_value",
    "committee_display",
    "integer",
    "map_committee",
    "map_legislator",
    "map_meeting",
    "map_session",
    "meeting_source_id",
    "text",
]
