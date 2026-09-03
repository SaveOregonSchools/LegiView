"""Normalize document records from OData and the narrowly parsed OLIS page."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .source_mapping import chamber_for_code, classify_committee_document, testimony_position
from .testimony_parser import ParsedTestimonyDocument


def canonical_public_testimony_url(session_key: str, source_id: Any) -> str:
    return (
        f"https://olis.oregonlegislature.gov/liz/{session_key}"
        f"/Downloads/PublicTestimonyDocument/{int(source_id)}"
    )


def public_testimony_documents(
    rows: Iterable[Mapping[str, Any]],
    html_rows: Iterable[ParsedTestimonyDocument],
    *,
    committees: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile preferred OData metadata with the complete server-rendered list."""
    committee_names = committees or {}
    html_by_id = {
        str(item.source_document_id): item
        for item in html_rows
        if item.source_entity_type == "CommitteePublicTestimony"
    }
    normalized: dict[str, dict[str, Any]] = {}
    for raw_value in rows:
        raw = dict(raw_value)
        source_id = str(raw["CommTestId"])
        displayed = html_by_id.get(source_id)
        first = _text(raw.get("SubmitterFirstName"))
        last = _text(raw.get("SubmitterLastName"))
        submitter = " ".join(part for part in (first, last) if part) or None
        committee_code = _text(raw.get("CommitteeCode"))
        normalized[source_id] = {
            "document_kind": "public_testimony",
            "source_section": (
                "submitted_written_testimony"
                if displayed
                else "odata_public_testimony_not_displayed"
            ),
            "source_entity_type": "CommitteePublicTestimony",
            "source_id": source_id,
            "raw_document_type": _text(raw.get("DocumentDescription")),
            "classification_method": "source_entity_type",
            "classification_confidence": "confirmed",
            "title": (displayed.title if displayed else None)
            or _text(raw.get("DocumentDescription"))
            or "Public testimony",
            "exhibit_reference": None,
            "submitter": (displayed.submitter if displayed else None) or submitter,
            "on_behalf_of": (displayed.on_behalf_of if displayed else None)
            or _text(raw.get("BehalfOf")),
            "testimony_position": (displayed.position if displayed else None)
            or testimony_position(raw.get("PositionOnMeasureId")),
            "city_organization": (displayed.city_or_organization if displayed else None)
            or _text(raw.get("Organization")),
            "meeting_date": _text(raw.get("MeetingDate"))
            or (displayed.meeting_date if displayed else None),
            "committee_code": committee_code or (displayed.committee_code if displayed else None),
            "committee_name": (
                displayed.committee_name if displayed else committee_names.get(committee_code or "")
            ),
            "chamber": None,
            "letter_date": None,
            "description": _text(raw.get("Topic")),
            "source_url": (
                displayed.download_url
                if displayed
                else _text(raw.get("DocumentUrl"))
            ),
            "canonical_download_url": canonical_public_testimony_url(raw["SessionKey"], source_id),
            "source_created_at": _text(raw.get("CreatedDate")),
            "source_modified_at": _text(raw.get("ModifiedDate")),
            "download_status": "discovered",
            "raw_json": raw,
        }
    # Preserve a page-only row instead of silently losing it if the two sources drift.
    for source_id, item in html_by_id.items():
        if source_id in normalized:
            continue
        normalized[source_id] = html_testimony_document(item)
    return list(normalized.values())


def html_testimony_document(item: ParsedTestimonyDocument) -> dict[str, Any]:
    if item.source_entity_type == "CommitteePublicTestimony":
        kind = "public_testimony"
        raw_type = "HTML testimony row"
    else:
        # OLIS's historical section is named presentations; a title alone is not
        # strong enough evidence to relabel an individual record as testimony.
        kind = "committee_presentation"
        raw_type = "Presentation"
    return {
        "document_kind": kind,
        "source_section": item.source_section,
        "source_entity_type": item.source_entity_type,
        "source_id": str(item.source_document_id),
        "raw_document_type": raw_type,
        "classification_method": "olis_section",
        "classification_confidence": "confirmed",
        "title": item.title,
        "exhibit_reference": None,
        "submitter": item.submitter,
        "on_behalf_of": item.on_behalf_of,
        "testimony_position": item.position,
        "city_organization": item.city_or_organization,
        "meeting_date": item.meeting_date,
        "committee_code": item.committee_code,
        "committee_name": item.committee_name,
        "chamber": None,
        "letter_date": None,
        "description": None,
        "source_url": item.download_url,
        "canonical_download_url": item.download_url,
        "source_created_at": None,
        "source_modified_at": None,
        "download_status": "discovered",
        "raw_json": {"parsed_from": "OLIS testimony HTML", "download_url": item.download_url},
    }


def committee_document(
    raw_value: Mapping[str, Any],
    *,
    displayed_ids: set[str] | None = None,
    committee_name: str | None = None,
) -> dict[str, Any]:
    raw = dict(raw_value)
    source_id = str(raw["CommitteeMeetingDocumentId"])
    kind, method = classify_committee_document(raw.get("DocumentType"))
    displayed = source_id in (displayed_ids or set())
    source_section = (
        "presentations_displayed_in_committee"
        if displayed
        else "odata_committee_meeting_document"
    )
    return {
        "document_kind": kind,
        "source_section": source_section,
        "source_entity_type": "CommitteeMeetingDocument",
        "source_id": source_id,
        "raw_document_type": _text(raw.get("DocumentType")),
        "classification_method": method,
        "classification_confidence": "confirmed" if kind != "unknown" else "unknown",
        "title": _text(raw.get("ExhibitTitle")),
        "exhibit_reference": _text(raw.get("ExhibitReference")),
        "submitter": _text(raw.get("Submitter")),
        "on_behalf_of": None,
        "testimony_position": None,
        "city_organization": None,
        "meeting_date": _text(raw.get("MeetingDate")),
        "committee_code": _text(raw.get("CommitteeCode")),
        "committee_name": committee_name,
        "chamber": None,
        "letter_date": None,
        "description": _text(raw.get("ExhibitTitle")),
        "source_url": _text(raw.get("DocumentUrl")),
        "canonical_download_url": _text(raw.get("DocumentUrl")),
        "source_created_at": _text(raw.get("CreatedDate")),
        "source_modified_at": _text(raw.get("ModifiedDate")),
        # Phase 1 downloads testimony/presentations/floor letters. Other
        # committee context remains browseable and auditable, but is out of the
        # payload scope unless reclassified by stronger future evidence.
        "download_status": "discovered" if kind in {"committee_presentation", "legacy_testimony"} else "not_applicable",
        "raw_json": raw,
    }


def floor_letter_document(raw_value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(raw_value)
    return {
        "document_kind": "floor_letter",
        "source_section": "floor_letters",
        "source_entity_type": "FloorLetter",
        "source_id": str(raw["FloorLetterId"]),
        "raw_document_type": "Floor Letter",
        "classification_method": "source_entity_type",
        "classification_confidence": "confirmed",
        "title": _text(raw.get("LetterTitle")) or _text(raw.get("LetterDescription")),
        "exhibit_reference": None,
        "submitter": None,
        "on_behalf_of": None,
        "testimony_position": None,
        "city_organization": None,
        "meeting_date": None,
        "committee_code": None,
        "committee_name": None,
        "chamber": chamber_for_code(raw.get("Chamber")),
        "letter_date": _text(raw.get("LetterDate")),
        "description": _text(raw.get("LetterDescription")),
        "source_url": _text(raw.get("FloorLetterUrl")),
        "canonical_download_url": _text(raw.get("FloorLetterUrl")),
        "source_created_at": None,
        "source_modified_at": None,
        "download_status": "discovered" if raw.get("FloorLetterUrl") else "not_applicable",
        "raw_json": raw,
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = " ".join(str(value).split())
    return result or None


__all__ = [
    "canonical_public_testimony_url",
    "committee_document",
    "floor_letter_document",
    "html_testimony_document",
    "public_testimony_documents",
]

