"""Bounded source primitives for Phase 2 historical inventory.

This module deliberately has no database or Flask dependency.  It turns the
official session catalogue into a frozen scope and describes/streams one
session-scoped OData entity at a time.  Callers persist each delivered page and
only commit the returned cursor after :func:`stream_session_entity` returns.
An exception therefore cannot produce a successful/cursor-advancing result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol
import xml.etree.ElementTree as ET

from .odata import ODataPage, odata_datetime_literal, odata_literal
from .source_mapping import (
    classify_committee_document_detail,
    normalize_session_key,
)


HISTORICAL_SESSION_BOUNDARY = "2014R1"
MODERN_TESTIMONY_BOUNDARY = "2021R1"


class HistoricalSourceError(ValueError):
    """The official source did not satisfy a required inventory invariant."""


@dataclass(frozen=True, slots=True)
class OfficialSession:
    session_key: str
    session_name: str | None
    begin_date: str
    end_date: str | None
    source_created_at: str | None
    source_modified_at: str | None
    default_session: bool | None
    raw: Mapping[str, Any]

    @property
    def chronology(self) -> datetime:
        return _parse_source_datetime(self.begin_date, field="BeginDate")


@dataclass(frozen=True, slots=True)
class SessionScope:
    """Immutable exact session selection suitable for run-scope JSON."""

    boundary_key: str
    sessions: tuple[OfficialSession, ...]

    @property
    def session_keys(self) -> tuple[str, ...]:
        return tuple(session.session_key for session in self.sessions)

    def selected(self, session_keys: Iterable[str]) -> "SessionScope":
        requested = tuple(dict.fromkeys(normalize_session_key(key) for key in session_keys))
        by_key = {session.session_key: session for session in self.sessions}
        unknown = [key for key in requested if key not in by_key]
        if unknown:
            raise HistoricalSourceError(
                "Selected sessions are outside the resolved historical scope: "
                + ", ".join(unknown)
            )
        requested_set = set(requested)
        # Preserve official chronology, not checkbox/request ordering.
        return SessionScope(
            self.boundary_key,
            tuple(session for session in self.sessions if session.session_key in requested_set),
        )

    def requested_scope(self) -> dict[str, Any]:
        return {
            "boundary_session_key": self.boundary_key,
            "session_keys": list(self.session_keys),
        }

    def is_at_or_after(self, session_key: str, comparison_session_key: str) -> bool:
        """Compare two sessions by official chronology, never key suffixes."""

        by_key = {session.session_key: session for session in self.sessions}
        session = normalize_session_key(session_key)
        comparison = normalize_session_key(comparison_session_key)
        if session not in by_key or comparison not in by_key:
            raise HistoricalSourceError(
                f"Cannot compare unresolved official sessions {session} and {comparison}"
            )
        return by_key[session].chronology >= by_key[comparison].chronology


def resolve_historical_session_scope(
    rows: Iterable[Mapping[str, Any]],
    *,
    boundary_key: str = HISTORICAL_SESSION_BOUNDARY,
) -> SessionScope:
    """Resolve every official session chronologically at/after ``boundary_key``.

    The boundary is found in the official rows and compared using official
    ``BeginDate`` values.  No assumptions are made about regular/special session
    suffixes or odd/even years.
    """

    boundary = normalize_session_key(boundary_key)
    by_key: dict[str, OfficialSession] = {}
    for value in rows:
        raw = dict(value)
        try:
            key = normalize_session_key(str(raw["SessionKey"]))
            begin_date = _required_text(raw.get("BeginDate"), "BeginDate", source_id=key)
        except KeyError as exc:
            raise HistoricalSourceError("LegislativeSessions row has no SessionKey") from exc
        if key in by_key:
            raise HistoricalSourceError(f"Duplicate official legislative session {key}")
        # Parse eagerly so one malformed date cannot silently perturb ordering.
        _parse_source_datetime(begin_date, field=f"{key}.BeginDate")
        by_key[key] = OfficialSession(
            session_key=key,
            session_name=_text(raw.get("SessionName")),
            begin_date=begin_date,
            end_date=_text(raw.get("EndDate")),
            source_created_at=_text(raw.get("CreatedDate")),
            source_modified_at=_text(raw.get("ModifiedDate")),
            default_session=_bool_or_none(raw.get("DefaultSession")),
            raw=MappingProxyType(raw),
        )
    if boundary not in by_key:
        raise HistoricalSourceError(
            f"Official LegislativeSessions did not contain required boundary {boundary}"
        )
    boundary_date = by_key[boundary].chronology
    selected = tuple(
        sorted(
            (session for session in by_key.values() if session.chronology >= boundary_date),
            key=lambda session: (session.chronology, session.session_key),
        )
    )
    if not selected or boundary not in {session.session_key for session in selected}:
        raise HistoricalSourceError(f"Unable to establish historical boundary {boundary}")
    return SessionScope(boundary, selected)


class ODataPageClient(Protocol):
    def iter_pages(self, entity_set: str, **params: Any) -> Iterable[ODataPage]: ...


def resolve_historical_session_scope_from_odata(
    client: ODataPageClient,
    *,
    boundary_key: str = HISTORICAL_SESSION_BOUNDARY,
    cancellation_requested: Callable[[], bool] | None = None,
) -> SessionScope:
    """Read the small official session catalogue across every continuation."""

    rows: list[dict[str, Any]] = []
    for page in _iter_pages_with_control(
        client,
        "LegislativeSessions",
        {"orderby": "BeginDate,SessionKey"},
        cancellation_requested,
    ):
        rows.extend(dict(item) for item in page.items)
    return resolve_historical_session_scope(rows, boundary_key=boundary_key)


@dataclass(frozen=True, slots=True)
class EntitySyncSpec:
    entity_set: str
    orderby: str
    source_id_fields: tuple[str, ...]
    source_date_fields: tuple[str, ...]
    measure_scoped: bool
    required_for_completeness: bool = True

    @property
    def supports_watermark(self) -> bool:
        return bool(self.source_date_fields)


ENTITY_SYNC_SPECS: Mapping[str, EntitySyncSpec] = MappingProxyType(
    {
        "Measures": EntitySyncSpec(
            "Measures", "MeasurePrefix,MeasureNumber", ("MeasurePrefix", "MeasureNumber"),
            ("CreatedDate", "ModifiedDate"), True,
        ),
        "Legislators": EntitySyncSpec(
            "Legislators", "LegislatorCode", ("LegislatorCode",),
            ("CreatedDate", "ModifiedDate"), False,
        ),
        "Committees": EntitySyncSpec(
            "Committees", "CommitteeCode", ("CommitteeCode",),
            ("CreatedDate", "ModifiedDate"), False,
        ),
        "MeasureSponsors": EntitySyncSpec(
            "MeasureSponsors", "MeasureSponsorId", ("MeasureSponsorId",),
            ("CreatedDate", "ModifiedDate"), True,
        ),
        "CommitteeMeetings": EntitySyncSpec(
            "CommitteeMeetings", "CommitteeCode,MeetingDate", ("CommitteeCode", "MeetingDate"),
            ("CreatedDate", "ModifiedDate"), False,
        ),
        "CommitteeAgendaItems": EntitySyncSpec(
            "CommitteeAgendaItems", "CommitteeAgendaItemId", ("CommitteeAgendaItemId",),
            ("CreatedDate", "ModifiedDate"), True,
        ),
        "CommitteeMeetingDocuments": EntitySyncSpec(
            "CommitteeMeetingDocuments", "CommitteeMeetingDocumentId",
            ("CommitteeMeetingDocumentId",), ("CreatedDate", "ModifiedDate"), True,
        ),
        "CommitteePublicTestimonies": EntitySyncSpec(
            "CommitteePublicTestimonies", "CommTestId", ("CommTestId",),
            ("CreatedDate", "ModifiedDate"), True,
        ),
        # Current metadata exposes neither CreatedDate nor ModifiedDate here.
        # Re-fetch and compare the complete session/HB/SB set by FloorLetterId.
        "FloorLetters": EntitySyncSpec(
            "FloorLetters", "FloorLetterId", ("FloorLetterId",), (), True,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class SessionEntityPlan:
    spec: EntitySyncSpec
    session_key: str
    strategy: str
    source_watermark: str | None
    filter_expression: str
    authoritative_presence: bool

    @property
    def request_params(self) -> dict[str, str]:
        return {
            "filter": self.filter_expression,
            "orderby": self.spec.orderby,
        }


def build_session_entity_plan(
    entity_set: str,
    session_key: str,
    *,
    source_watermark: str | None = None,
    force_full: bool = False,
) -> SessionEntityPlan:
    """Build a safe session query and its cursor/presence semantics."""

    try:
        spec = ENTITY_SYNC_SPECS[entity_set]
    except KeyError as exc:
        raise HistoricalSourceError(f"Unsupported historical entity set: {entity_set}") from exc
    session = normalize_session_key(session_key)
    clauses = [f"SessionKey eq {odata_literal(session)}"]
    if spec.measure_scoped:
        clauses.append("(MeasurePrefix eq 'HB' or MeasurePrefix eq 'SB')")

    watermark = _text(source_watermark)
    use_watermark = bool(watermark and spec.supports_watermark and not force_full)
    if use_watermark:
        literal = odata_datetime_literal(watermark or "")
        date_clauses = " or ".join(
            f"{field} ge {literal}" for field in spec.source_date_fields
        )
        clauses.append(f"({date_clauses})")
        strategy = "watermark"
        authoritative = False
    else:
        # FloorLetters always land here and therefore can never accidentally
        # claim a source-date watermark capability the service does not expose.
        strategy = "full_session"
        authoritative = True
        watermark = None
    return SessionEntityPlan(
        spec=spec,
        session_key=session,
        strategy=strategy,
        source_watermark=watermark,
        filter_expression=" and ".join(clauses),
        authoritative_presence=authoritative,
    )


@dataclass(frozen=True, slots=True)
class SourcePageBatch:
    page_number: int
    items: tuple[dict[str, Any], ...]
    continuation_url: str | None
    source_count: int | None
    metadata_url: str | None


@dataclass(frozen=True, slots=True)
class EntitySyncResult:
    entity_set: str
    session_key: str
    strategy: str
    page_count: int
    returned_count: int
    maximum_observed_source_date: str | None
    next_source_watermark: str | None
    authoritative_presence: bool


PageConsumer = Callable[[SourcePageBatch], None]


def stream_session_entity(
    client: ODataPageClient,
    plan: SessionEntityPlan,
    consume_page: PageConsumer,
    *,
    cancellation_requested: Callable[[], bool] | None = None,
) -> EntitySyncResult:
    """Stream bounded pages to ``consume_page`` and return commit-ready state.

    No result is returned if fetching, validation, or page persistence fails.
    In particular, callers must update ``source_sync_state`` only from this
    successful return value, never from a partially delivered page.
    """

    returned_count = 0
    page_count = 0
    maximum = plan.source_watermark
    for page_number, page in enumerate(
        _iter_pages_with_control(
            client,
            plan.spec.entity_set,
            plan.request_params,
            cancellation_requested,
        ),
        1,
    ):
        items = tuple(dict(item) for item in page.items)
        for item in items:
            _validate_scoped_item(plan, item)
        batch = SourcePageBatch(
            page_number=page_number,
            items=items,
            continuation_url=page.next_url,
            source_count=page.count,
            metadata_url=page.metadata_url,
        )
        # Persistence is part of successful page consumption.  If it raises,
        # the function deliberately never yields a cursor result.
        consume_page(batch)
        returned_count += len(items)
        page_count = page_number
        maximum = _maximum_source_date(items, plan.spec.source_date_fields, maximum)
    return EntitySyncResult(
        entity_set=plan.spec.entity_set,
        session_key=plan.session_key,
        strategy=plan.strategy,
        page_count=page_count,
        returned_count=returned_count,
        maximum_observed_source_date=maximum,
        next_source_watermark=maximum if plan.spec.supports_watermark else None,
        authoritative_presence=plan.authoritative_presence,
    )


def _iter_pages_with_control(
    client: ODataPageClient,
    entity_set: str,
    params: Mapping[str, Any],
    cancellation_requested: Callable[[], bool] | None,
) -> Iterable[ODataPage]:
    """Pass run control only to clients that explicitly implement it.

    Older test/integration clients commonly accept arbitrary OData ``**params``.
    Treating that catch-all as cancellation support would accidentally serialize
    a Python callback into the remote query, so support must be explicit.
    """

    iterator = client.iter_pages
    if cancellation_requested is not None:
        try:
            supports_control = "cancellation_requested" in inspect.signature(
                iterator
            ).parameters
        except (TypeError, ValueError):
            supports_control = False
        if supports_control:
            return iterator(
                entity_set,
                cancellation_requested=cancellation_requested,
                **dict(params),
            )
    return iterator(entity_set, **dict(params))


@dataclass(frozen=True, slots=True)
class ReconciliationCandidate:
    candidate: bool
    reasons: tuple[str, ...]


def testimony_reconciliation_candidate(
    *,
    public_testimony_rows: Iterable[Mapping[str, Any]] = (),
    committee_document_rows: Iterable[Mapping[str, Any]] = (),
    agenda_item_rows: Iterable[Mapping[str, Any]] = (),
) -> ReconciliationCandidate:
    """Choose OLIS page candidates from structured source evidence only."""

    reasons: list[str] = []
    if any(True for _ in public_testimony_rows):
        reasons.append("odata_public_testimony")
    for row in committee_document_rows:
        classification = classify_committee_document_detail(row.get("DocumentType"))
        if classification.kind in {"committee_presentation", "legacy_testimony"}:
            reasons.append("presentation_capable_committee_document")
            break
        if not classification.known:
            # At historical scale, checking one narrow page is safer than
            # assuming an unrecognized structured type cannot be displayed.
            reasons.append("unknown_committee_document_type")
            break
    for row in agenda_item_rows:
        structured_values = (
            row.get("MeetingType"),
            row.get("AgendaItemType"),
            row.get("Type"),
        )
        if any(_normalized_text(value) == "public hearing" for value in structured_values):
            reasons.append("public_hearing_agenda")
            break
    return ReconciliationCandidate(bool(reasons), tuple(dict.fromkeys(reasons)))


# Required mapper fields are intentionally explicit, including the two official
# misspellings.  This supports startup drift reporting without guessing new
# field names or mutating the proven mapping automatically.
REQUIRED_METADATA_PROPERTIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "LegislativeSessions": frozenset(
            {"SessionKey", "SessionName", "BeginDate", "EndDate", "CreatedDate", "ModifiedDate"}
        ),
        "Measures": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "RelatingTo", "RelatingToFull", "CreatedDate", "ModifiedDate"}
        ),
        "Legislators": frozenset(
            {"SessionKey", "LegislatorCode", "FirstName", "LastName", "CreatedDate", "ModifiedDate"}
        ),
        "Committees": frozenset(
            {"SessionKey", "CommitteeCode", "CommitteeName", "CreatedDate", "ModifiedDate"}
        ),
        "MeasureSponsors": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "MeasureSponsorId", "LegislatoreCode", "CreatedDate", "ModifiedDate"}
        ),
        "CommitteeMeetings": frozenset(
            {"SessionKey", "CommitteeCode", "MeetingDate", "CreatedDate", "ModifiedDate"}
        ),
        "CommitteeAgendaItems": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "CommitteeAgendaItemId", "CommitteCode", "CreatedDate", "ModifiedDate"}
        ),
        "CommitteeMeetingDocuments": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "CommitteeMeetingDocumentId", "DocumentType", "CreatedDate", "ModifiedDate"}
        ),
        "CommitteePublicTestimonies": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "CommTestId", "PdfCreatedFlag", "CreatedDate", "ModifiedDate"}
        ),
        "FloorLetters": frozenset(
            {"SessionKey", "MeasurePrefix", "MeasureNumber", "FloorLetterId", "FloorLetterUrl"}
        ),
    }
)


@dataclass(frozen=True, slots=True)
class MetadataContractIssue:
    issue_type: str
    entity_set: str
    property_name: str | None
    message: str
    material_to_completeness: bool = True


@dataclass(frozen=True, slots=True)
class MetadataContractReport:
    properties_by_entity_set: Mapping[str, frozenset[str]]
    issues: tuple[MetadataContractIssue, ...]

    @property
    def compatible(self) -> bool:
        return not any(issue.material_to_completeness for issue in self.issues)


def validate_odata_metadata(xml_text: str) -> MetadataContractReport:
    """Inspect live/captured OData metadata for fields required by Phase 2."""

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return MetadataContractReport(
            MappingProxyType({}),
            (
                MetadataContractIssue(
                    "metadata_parse_error", "$metadata", None, f"Invalid OData metadata XML: {exc}"
                ),
            ),
        )
    entity_types: dict[str, frozenset[str]] = {}
    for node in root.iter():
        if _local_name(node.tag) != "EntityType":
            continue
        name = node.attrib.get("Name")
        if not name:
            continue
        properties = frozenset(
            child.attrib["Name"]
            for child in node
            if _local_name(child.tag) == "Property" and child.attrib.get("Name")
        )
        entity_types[name] = properties
    properties_by_set: dict[str, frozenset[str]] = {}
    for node in root.iter():
        if _local_name(node.tag) != "EntitySet":
            continue
        set_name = node.attrib.get("Name")
        type_name = (node.attrib.get("EntityType") or "").rsplit(".", 1)[-1]
        if set_name:
            properties_by_set[set_name] = entity_types.get(type_name, frozenset())

    issues: list[MetadataContractIssue] = []
    for entity_set, required in REQUIRED_METADATA_PROPERTIES.items():
        if entity_set not in properties_by_set:
            issues.append(
                MetadataContractIssue(
                    "missing_entity_set", entity_set, None,
                    f"OData metadata no longer exposes required entity set {entity_set}",
                )
            )
            continue
        for property_name in sorted(required - properties_by_set[entity_set]):
            issues.append(
                MetadataContractIssue(
                    "missing_property", entity_set, property_name,
                    f"OData metadata no longer exposes {entity_set}.{property_name}",
                )
            )
    return MetadataContractReport(MappingProxyType(properties_by_set), tuple(issues))


def _validate_scoped_item(plan: SessionEntityPlan, item: Mapping[str, Any]) -> None:
    actual_session = _text(item.get("SessionKey"))
    if actual_session != plan.session_key:
        raise HistoricalSourceError(
            f"{plan.spec.entity_set} returned unexpected SessionKey {actual_session!r}; "
            f"expected {plan.session_key!r}"
        )
    if plan.spec.measure_scoped:
        prefix = (_text(item.get("MeasurePrefix")) or "").upper()
        if prefix not in {"HB", "SB"}:
            raise HistoricalSourceError(
                f"{plan.spec.entity_set} returned out-of-scope MeasurePrefix {prefix!r}"
            )


def _maximum_source_date(
    items: Iterable[Mapping[str, Any]],
    fields: Iterable[str],
    current: str | None,
) -> str | None:
    maximum = current
    maximum_value = _parse_source_datetime(current, field="source watermark") if current else None
    for item in items:
        for field in fields:
            raw = _text(item.get(field))
            if not raw:
                continue
            parsed = _parse_source_datetime(raw, field=field)
            if maximum_value is None or parsed > maximum_value:
                maximum = raw
                maximum_value = parsed
    return maximum


def _parse_source_datetime(value: str, *, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalSourceError(f"Invalid {field} source date {value!r}") from exc
    # Comparison only: retain the original source text everywhere else.  Aware
    # values are normalized solely so mixed metadata cannot break chronology.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _required_text(value: Any, field: str, *, source_id: str) -> str:
    result = _text(value)
    if not result:
        raise HistoricalSourceError(f"{source_id} has no required {field}")
    return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = [
    "ENTITY_SYNC_SPECS",
    "HISTORICAL_SESSION_BOUNDARY",
    "MODERN_TESTIMONY_BOUNDARY",
    "EntitySyncResult",
    "EntitySyncSpec",
    "HistoricalSourceError",
    "MetadataContractIssue",
    "MetadataContractReport",
    "OfficialSession",
    "ReconciliationCandidate",
    "SessionEntityPlan",
    "SessionScope",
    "SourcePageBatch",
    "build_session_entity_plan",
    "resolve_historical_session_scope",
    "resolve_historical_session_scope_from_odata",
    "stream_session_entity",
    "testimony_reconciliation_candidate",
    "validate_odata_metadata",
]
