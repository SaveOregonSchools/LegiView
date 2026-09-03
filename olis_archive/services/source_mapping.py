"""Observed OLIS/OData-to-local normalization rules.

Only values confirmed during the 2026-09-03 source spike are given semantic
meaning. Unknown values remain visible to callers and are never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_BILL_RE = re.compile(r"^\s*(HB|SB)\s*[- ]?\s*(\d{1,6})\s*$", re.IGNORECASE)
POSITION_MAP = {3981: "Neutral", 3982: "Oppose", 3983: "Support"}


class InvalidBillId(ValueError):
    pass


def normalize_session_key(value: str) -> str:
    key = re.sub(r"\s+", "", str(value or "")).upper()
    if not re.fullmatch(r"20\d{2}[A-Z][A-Z0-9]{0,4}", key):
        raise ValueError("Session must look like 2026R1 or 2020S2")
    return key


def normalize_bill_id(value: str) -> tuple[str, int, str, str]:
    match = _BILL_RE.fullmatch(str(value or ""))
    if not match:
        raise InvalidBillId("Only House Bills and Senate Bills are supported (for example, SB1501)")
    prefix = match.group(1).upper()
    number = int(match.group(2))
    compact = f"{prefix}{number}"
    return prefix, number, compact, f"{prefix} {number}"


def chamber_for_prefix(prefix: str) -> str:
    normalized = str(prefix or "").upper()
    if normalized == "HB":
        return "House"
    if normalized == "SB":
        return "Senate"
    raise InvalidBillId(f"Unsupported measure prefix: {prefix!r}")


def chamber_for_code(value: Any) -> str | None:
    code = str(value or "").strip().upper()
    return {"H": "House", "HOUSE": "House", "S": "Senate", "SENATE": "Senate"}.get(code)


@dataclass(frozen=True, slots=True)
class SponsorMapping:
    category: str
    kind: str
    display_code: str | None
    known: bool


def map_sponsor(raw: Mapping[str, Any]) -> SponsorMapping:
    sponsor_type = str(raw.get("SponsorType") or "").strip()
    level = str(raw.get("SponsorLevel") or "").strip()
    legislator_code = _text(raw.get("LegislatoreCode"))
    committee_code = _text(raw.get("CommitteeCode"))
    category = {"chief": "chief", "regular": "regular"}.get(level.casefold(), "unknown")
    if sponsor_type.casefold() == "member" and legislator_code:
        return SponsorMapping(category, "legislator", legislator_code, category != "unknown")
    if sponsor_type.casefold() == "committee" and committee_code:
        return SponsorMapping(category, "committee", committee_code, category != "unknown")
    if sponsor_type.casefold() == "presession":
        return SponsorMapping(category, "other", None, category != "unknown")
    return SponsorMapping(category, "other", legislator_code or committee_code, False)


def testimony_position(value: Any) -> str | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return _text(value)
    return POSITION_MAP.get(numeric, f"Unknown ({numeric})")


def classify_committee_document(document_type: Any) -> tuple[str, str]:
    """Return (kind, method) without guessing testimony from a title/filename."""
    raw = str(document_type or "").strip().casefold()
    if raw == "presentation":
        return "committee_presentation", "raw_document_type"
    if raw in {"testimony", "public testimony", "written testimony"}:
        return "legacy_testimony", "raw_document_type"
    if raw:
        return "committee_document_other", "raw_document_type"
    return "unknown", "unclassified"


def map_measure(raw: Mapping[str, Any]) -> dict[str, Any]:
    prefix = str(raw["MeasurePrefix"]).upper()
    number = int(raw["MeasureNumber"])
    _, _, compact, display = normalize_bill_id(f"{prefix}{number}")
    return {
        "session_key": str(raw["SessionKey"]),
        "measure_prefix": prefix,
        "measure_number": number,
        "bill_id_compact": compact,
        "bill_id_display": display,
        "bill_chamber": chamber_for_prefix(prefix),
        "at_the_request_of": _text(raw.get("AtTheRequestOf")),
        "bill_title_source": "Measure.RelatingTo",
        "bill_title": _text(raw.get("RelatingTo")),
        "catchline": _text(raw.get("CatchLine")),
        "measure_summary": _text(raw.get("MeasureSummary")),
        "chapter_number": _text(raw.get("ChapterNumber")),
        "effective_date": _text(raw.get("EffectiveDate")),
        "vetoed": _bool_int(raw.get("Vetoed")),
        "emergency_clause": _bool_int(raw.get("EmergencyClause")),
        "current_version": _text(raw.get("CurrentVersion")),
        "current_location": _text(raw.get("CurrentLocation")),
        "current_committee_code": _text(raw.get("CurrentCommitteeCode")),
        "current_subcommittee_code": _text(raw.get("CurrentSubCommittee")),
        "relating_to": _text(raw.get("RelatingTo")),
        "relating_to_full": _text(raw.get("RelatingToFull")),
        "minority_catchline": _text(raw.get("MinorityCatchLine")),
        "fiscal_impact": _text(raw.get("FiscalImpact")),
        "revenue_impact": _text(raw.get("RevenueImpact")),
        "lc_number": raw.get("LCNumber"),
        "prefix_meaning": _text(raw.get("PrefixMeaning")),
        "source_created_at": _text(raw.get("CreatedDate")),
        "source_modified_at": _text(raw.get("ModifiedDate")),
        "source_url": (
            f"https://olis.oregonlegislature.gov/liz/{raw['SessionKey']}"
            f"/Measures/Overview/{compact}"
        ),
        "raw_json": dict(raw),
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    lowered = str(value).strip().casefold()
    if lowered in {"true", "1", "yes"}:
        return 1
    if lowered in {"false", "0", "no"}:
        return 0
    return None

