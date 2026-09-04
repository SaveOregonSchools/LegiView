"""Pure Phase 2 reconciliation decisions for source/display/archive state.

The functions here do not write SQLite.  They return normalized documents,
presence transitions, and durable anomaly payloads so both CLI and web workers
can use the same semantics without embedding them in route handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping

from .documents import (
    committee_document,
    html_testimony_document,
    public_testimony_documents,
)
from .source_mapping import classify_committee_document_detail
from .testimony_parser import ParsedTestimonyDocument, TestimonyPageParseResult


DISPLAY_CHECK_STATUSES = frozenset(
    {
        "checked_with_records",
        "checked_zero",
        "not_applicable",
        "failed_fetch",
        "parser_anomalous",
    }
)
SOURCE_PRESENCE_STATES = frozenset({"active", "missing", "unknown"})
_NUMERIC_SOURCE_ID = re.compile(r"^[1-9]\d*$")


@dataclass(frozen=True, slots=True)
class SourceAnomaly:
    anomaly_type: str
    severity: str
    message: str
    source_entity_type: str | None = None
    source_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    material_to_completeness: bool = False

    def as_record(self) -> dict[str, Any]:
        return {
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "message": self.message,
            "source_entity_type": self.source_entity_type,
            "source_id": self.source_id,
            "details": dict(self.details),
            # Matches StorageService.record_source_anomaly's keyword so a
            # worker can pass this payload through and add run/session context.
            "affects_completeness": self.material_to_completeness,
        }


@dataclass(frozen=True, slots=True)
class DisplayCheck:
    status: str
    rows: tuple[ParsedTestimonyDocument, ...] = ()
    checked_at: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    parser_anomalies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in DISPLAY_CHECK_STATUSES:
            raise ValueError(f"Unsupported OLIS display-check status: {self.status}")
        if self.status == "checked_zero" and self.rows:
            raise ValueError("A checked-zero display result cannot contain rows")

    @property
    def complete(self) -> bool:
        return self.status in {"checked_with_records", "checked_zero"}

    @classmethod
    def from_parse_result(
        cls,
        result: TestimonyPageParseResult,
        *,
        checked_at: str,
    ) -> "DisplayCheck":
        return cls(
            status=result.status,
            rows=result.documents,
            checked_at=checked_at,
            parser_anomalies=result.anomalies,
        )

    @classmethod
    def failed(
        cls,
        error: BaseException | str,
        *,
        checked_at: str | None = None,
    ) -> "DisplayCheck":
        return cls(
            status="failed_fetch",
            checked_at=checked_at,
            error_class=error.__class__.__name__ if isinstance(error, BaseException) else "SourceError",
            error_message=str(error),
        )

    @classmethod
    def not_candidate(cls) -> "DisplayCheck":
        return cls(status="not_applicable")


@dataclass(frozen=True, slots=True)
class DocumentReconciliationResult:
    documents: tuple[dict[str, Any], ...]
    anomalies: tuple[SourceAnomaly, ...]
    display_status: str
    odata_count: int
    displayed_count: int
    matched_count: int
    odata_only_count: int
    page_only_count: int
    material_completeness_gap: bool

    @property
    def displayed_source_ids(self) -> tuple[str, ...]:
        return tuple(
            str(document["source_id"])
            for document in self.documents
            if document.get("displayed_in_olis") is True
        )

    def storage_display_values(self, source_entity_type: str) -> dict[str, Any]:
        """Arguments accepted by ``record_olis_display_reconciliation``."""

        return {
            "status": self.display_status,
            "source_entity_type": source_entity_type,
            "displayed_source_ids": self.displayed_source_ids,
            "odata_record_count": self.odata_count,
            "displayed_record_count": self.displayed_count,
            "page_only_count": self.page_only_count,
            "odata_only_count": self.odata_only_count,
            "details": {
                "matched_count": self.matched_count,
                "material_completeness_gap": self.material_completeness_gap,
            },
        }


def reconcile_modern_public_testimony(
    odata_rows: Iterable[Mapping[str, Any]],
    display_check: DisplayCheck,
    *,
    committees: Mapping[str, str] | None = None,
) -> DocumentReconciliationResult:
    """Reconcile OData-primary public testimony by ``CommTestId``.

    A failed or anomalous page never turns absence into
    ``displayed_in_olis=0``.  Positive parsed rows remain useful evidence even
    when another row made the page parser-anomalous.
    """

    anomalies: list[SourceAnomaly] = []
    odata_by_id = _index_odata_rows(
        odata_rows,
        id_field="CommTestId",
        source_entity_type="CommitteePublicTestimony",
        anomalies=anomalies,
    )
    display_by_id = _index_display_rows(
        display_check.rows,
        source_entity_type="CommitteePublicTestimony",
        anomalies=anomalies,
    )
    normalized = public_testimony_documents(
        odata_by_id.values(), display_by_id.values(), committees=committees
    )
    documents: list[dict[str, Any]] = []
    for value in normalized:
        document = dict(value)
        source_id = str(document["source_id"])
        in_odata = source_id in odata_by_id
        in_display = source_id in display_by_id
        displayed = _displayed_value(display_check, in_display)
        document["displayed_in_olis"] = displayed
        document["display_reconciled_at"] = (
            display_check.checked_at if displayed is not None else None
        )
        document["reconciliation_origin"] = _origin(in_odata, in_display)
        document["source_presence"] = "active" if in_odata else "unknown"
        if in_odata and displayed is None:
            document["source_section"] = "odata_public_testimony"
        documents.append(document)

    odata_ids = set(odata_by_id)
    display_ids = set(display_by_id)
    if display_check.complete:
        _append_set_discrepancies(
            anomalies,
            odata_ids,
            display_ids,
            source_entity_type="CommitteePublicTestimony",
            odata_label="CommitteePublicTestimonies",
        )
    elif display_check.status == "failed_fetch":
        anomalies.append(
            SourceAnomaly(
                "olis_display_fetch_failed",
                "error",
                display_check.error_message or "OLIS testimony display request failed",
                details={"error_class": display_check.error_class},
                material_to_completeness=True,
            )
        )
    elif display_check.status == "parser_anomalous":
        anomalies.append(
            SourceAnomaly(
                "olis_display_parser_anomaly",
                "error",
                "; ".join(display_check.parser_anomalies)
                or "OLIS testimony display markup could not be reconciled safely",
                details={"parsed_row_count": len(display_check.rows)},
                material_to_completeness=True,
            )
        )
    elif display_check.status == "not_applicable" and odata_ids:
        anomalies.append(
            SourceAnomaly(
                "testimony_candidate_rule_mismatch",
                "error",
                "A bill with OData public testimony was not selected for OLIS display reconciliation",
                details={"odata_count": len(odata_ids)},
                material_to_completeness=True,
            )
        )
    return _result(documents, anomalies, display_check, odata_ids, display_ids)


def reconcile_historical_presentations(
    odata_rows: Iterable[Mapping[str, Any]],
    display_check: DisplayCheck,
    *,
    committee_names: Mapping[str, str] | None = None,
) -> DocumentReconciliationResult:
    """Reconcile structured committee documents with the legacy OLIS section."""

    anomalies: list[SourceAnomaly] = []
    odata_by_id = _index_odata_rows(
        odata_rows,
        id_field="CommitteeMeetingDocumentId",
        source_entity_type="CommitteeMeetingDocument",
        anomalies=anomalies,
    )
    display_by_id = _index_display_rows(
        display_check.rows,
        source_entity_type="CommitteeMeetingDocument",
        anomalies=anomalies,
    )
    display_ids = set(display_by_id)
    documents: list[dict[str, Any]] = []
    expected_display_ids: set[str] = set()
    for source_id, raw in odata_by_id.items():
        classification = classify_committee_document_detail(raw.get("DocumentType"))
        if classification.kind in {"committee_presentation", "legacy_testimony"}:
            expected_display_ids.add(source_id)
        if not classification.known:
            anomalies.append(
                SourceAnomaly(
                    "unknown_document_type",
                    "warning",
                    f"Unrecognized committee DocumentType {classification.raw_value!r}",
                    "CommitteeMeetingDocument",
                    source_id,
                    {
                        "raw_document_type": classification.raw_value,
                        "retained_kind": classification.kind,
                    },
                )
            )
        code = _text(raw.get("CommitteeCode")) or ""
        document = committee_document(
            raw,
            displayed_ids=display_ids,
            committee_name=(committee_names or {}).get(code),
        )
        in_display = source_id in display_ids
        displayed = _displayed_value(display_check, in_display)
        if in_display and classification.kind not in {
            "committee_presentation",
            "legacy_testimony",
        }:
            # The named OLIS presentation section is stronger per-record
            # evidence than an unknown/other raw type, but retain and diagnose
            # the original value instead of erasing the conflict.
            document["document_kind"] = "committee_presentation"
            document["classification_method"] = "olis_section"
            document["classification_confidence"] = "confirmed"
            anomalies.append(
                SourceAnomaly(
                    "presentation_type_mismatch",
                    "warning",
                    "OLIS displayed a committee document in the presentation section but its raw type was not a presentation",
                    "CommitteeMeetingDocument",
                    source_id,
                    {
                        "raw_document_type": classification.raw_value,
                        "previous_normalized_kind": classification.kind,
                        "display_normalized_kind": "committee_presentation",
                    },
                )
            )
        document["displayed_in_olis"] = displayed
        document["display_reconciled_at"] = (
            display_check.checked_at if displayed is not None else None
        )
        document["reconciliation_origin"] = _origin(True, in_display)
        document["source_presence"] = "active"
        documents.append(document)

    # Preserve a page-only legacy record.  It is not asserted missing merely
    # because one OData response omitted it; source presence remains unknown.
    for source_id, row in display_by_id.items():
        if source_id in odata_by_id:
            continue
        document = html_testimony_document(row)
        document["displayed_in_olis"] = True
        document["display_reconciled_at"] = display_check.checked_at
        document["reconciliation_origin"] = "olis_only"
        document["source_presence"] = "unknown"
        documents.append(document)

    odata_ids = set(odata_by_id)
    if display_check.complete:
        # Meeting Material and Witness Registration are expected not to appear
        # in the presentation section, so only presentation-capable OData rows
        # participate in the mismatch diagnostic.
        _append_set_discrepancies(
            anomalies,
            expected_display_ids,
            display_ids,
            source_entity_type="CommitteeMeetingDocument",
            odata_label="presentation-capable CommitteeMeetingDocuments",
        )
    elif display_check.status == "failed_fetch":
        anomalies.append(
            SourceAnomaly(
                "olis_display_fetch_failed",
                "error",
                display_check.error_message or "OLIS historical presentation request failed",
                details={"error_class": display_check.error_class},
                material_to_completeness=True,
            )
        )
    elif display_check.status == "parser_anomalous":
        anomalies.append(
            SourceAnomaly(
                "olis_display_parser_anomaly",
                "error",
                "; ".join(display_check.parser_anomalies)
                or "OLIS historical presentation markup could not be reconciled safely",
                details={"parsed_row_count": len(display_check.rows)},
                material_to_completeness=True,
            )
        )
    return _result(documents, anomalies, display_check, odata_ids, display_ids)


@dataclass(frozen=True, slots=True)
class ExistingSourcePresence:
    source_id: str
    source_presence: str = "unknown"
    missing_from_source_since: str | None = None

    def __post_init__(self) -> None:
        if self.source_presence not in SOURCE_PRESENCE_STATES:
            raise ValueError(f"Unsupported source presence: {self.source_presence}")
        if not str(self.source_id).strip():
            raise ValueError("source_id must not be blank")


@dataclass(frozen=True, slots=True)
class SourcePresenceDecision:
    source_id: str
    source_presence: str
    missing_from_source_since: str | None
    last_source_reconciled_at: str | None
    transition: str
    observed_in_response: bool


@dataclass(frozen=True, slots=True)
class PresenceReconciliationResult:
    decisions: tuple[SourcePresenceDecision, ...]
    query_succeeded: bool
    authoritative_full: bool

    @property
    def newly_missing_count(self) -> int:
        return sum(decision.transition == "became_missing" for decision in self.decisions)

    @property
    def reappeared_count(self) -> int:
        return sum(decision.transition == "reappeared" for decision in self.decisions)


def reconcile_source_presence(
    existing: Iterable[ExistingSourcePresence],
    observed_source_ids: Iterable[str],
    *,
    query_succeeded: bool,
    authoritative_full: bool,
    reconciled_at: str,
) -> PresenceReconciliationResult:
    """Return non-destructive active/missing decisions for one entity scope."""

    existing_by_id: dict[str, ExistingSourcePresence] = {}
    for record in existing:
        source_id = str(record.source_id).strip()
        if source_id in existing_by_id:
            raise ValueError(f"Duplicate existing source identity {source_id!r}")
        existing_by_id[source_id] = record
    observed = {str(value).strip() for value in observed_source_ids if str(value).strip()}
    decisions: list[SourcePresenceDecision] = []
    for source_id in sorted(existing_by_id.keys() | observed):
        previous = existing_by_id.get(source_id)
        if source_id in observed:
            transition = (
                "new"
                if previous is None
                else "reappeared"
                if previous.source_presence == "missing"
                else "unchanged"
            )
            decisions.append(
                SourcePresenceDecision(
                    source_id,
                    "active",
                    None,
                    reconciled_at if query_succeeded else None,
                    transition,
                    True,
                )
            )
            continue
        assert previous is not None
        if query_succeeded and authoritative_full:
            newly_missing = previous.source_presence != "missing"
            decisions.append(
                SourcePresenceDecision(
                    source_id,
                    "missing",
                    previous.missing_from_source_since or reconciled_at,
                    reconciled_at,
                    "became_missing" if newly_missing else "unchanged",
                    False,
                )
            )
        else:
            # Failed and incremental queries never turn an omission into proof
            # of disappearance.
            decisions.append(
                SourcePresenceDecision(
                    source_id,
                    previous.source_presence,
                    previous.missing_from_source_since,
                    None,
                    "unchanged",
                    False,
                )
            )
    return PresenceReconciliationResult(
        tuple(decisions), bool(query_succeeded), bool(authoritative_full)
    )


def detect_document_type_drift(
    *,
    source_entity_type: str,
    source_id: str,
    previous_raw_type: str | None,
    previous_normalized_kind: str | None,
    incoming_raw_type: str | None,
    incoming_normalized_kind: str | None = None,
) -> tuple[SourceAnomaly, ...]:
    """Describe raw/normalized type changes without deciding to delete bytes."""

    if incoming_normalized_kind is None:
        incoming_classification = classify_committee_document_detail(incoming_raw_type)
        incoming_normalized_kind = incoming_classification.kind
    else:
        incoming_classification = classify_committee_document_detail(incoming_raw_type)
    anomalies: list[SourceAnomaly] = []
    if _text(previous_raw_type) != _text(incoming_raw_type):
        anomalies.append(
            SourceAnomaly(
                "raw_document_type_changed",
                "warning",
                "The source changed the raw document type for an existing document identity",
                source_entity_type,
                str(source_id),
                {
                    "old_raw_document_type": previous_raw_type,
                    "new_raw_document_type": incoming_raw_type,
                },
            )
        )
    if (
        previous_normalized_kind is not None
        and previous_normalized_kind != incoming_normalized_kind
    ):
        anomalies.append(
            SourceAnomaly(
                "normalized_document_kind_changed",
                "warning",
                "The normalized kind changed for an existing document identity",
                source_entity_type,
                str(source_id),
                {
                    "old_normalized_kind": previous_normalized_kind,
                    "new_normalized_kind": incoming_normalized_kind,
                },
            )
        )
    if not incoming_classification.known:
        anomalies.append(
            SourceAnomaly(
                "unknown_document_type",
                "warning",
                f"Unrecognized committee DocumentType {incoming_classification.raw_value!r}",
                source_entity_type,
                str(source_id),
                {
                    "raw_document_type": incoming_classification.raw_value,
                    "retained_kind": incoming_normalized_kind,
                },
            )
        )
    return tuple(anomalies)


def _index_odata_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    id_field: str,
    source_entity_type: str,
    anomalies: list[SourceAnomaly],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw_value in rows:
        raw = dict(raw_value)
        source_id = _numeric_source_id(raw.get(id_field))
        if source_id is None:
            anomalies.append(
                SourceAnomaly(
                    "malformed_source_id",
                    "error",
                    f"{source_entity_type} contained an invalid {id_field}",
                    source_entity_type,
                    _text(raw.get(id_field)),
                    {"id_field": id_field, "raw_value": raw.get(id_field)},
                    material_to_completeness=True,
                )
            )
            continue
        previous = indexed.get(source_id)
        if previous is not None and previous != raw:
            anomalies.append(
                SourceAnomaly(
                    "conflicting_duplicate_source_id",
                    "error",
                    f"Conflicting {source_entity_type} rows shared source ID {source_id}",
                    source_entity_type,
                    source_id,
                    material_to_completeness=True,
                )
            )
            continue
        indexed[source_id] = raw
    return indexed


def _index_display_rows(
    rows: Iterable[ParsedTestimonyDocument],
    *,
    source_entity_type: str,
    anomalies: list[SourceAnomaly],
) -> dict[str, ParsedTestimonyDocument]:
    indexed: dict[str, ParsedTestimonyDocument] = {}
    for row in rows:
        if row.source_entity_type != source_entity_type:
            continue
        source_id = _numeric_source_id(row.source_document_id)
        if source_id is None:
            anomalies.append(
                SourceAnomaly(
                    "malformed_source_id",
                    "error",
                    f"OLIS display contained an invalid {source_entity_type} ID",
                    source_entity_type,
                    str(row.source_document_id),
                    material_to_completeness=True,
                )
            )
            continue
        previous = indexed.get(source_id)
        if previous is not None and previous != row:
            anomalies.append(
                SourceAnomaly(
                    "conflicting_duplicate_display_id",
                    "error",
                    f"Conflicting OLIS display rows shared source ID {source_id}",
                    source_entity_type,
                    source_id,
                    material_to_completeness=True,
                )
            )
            continue
        indexed[source_id] = row
    return indexed


def _append_set_discrepancies(
    anomalies: list[SourceAnomaly],
    odata_ids: set[str],
    display_ids: set[str],
    *,
    source_entity_type: str,
    odata_label: str,
) -> None:
    if odata_ids != display_ids:
        anomalies.append(
            SourceAnomaly(
                "odata_olis_count_mismatch",
                "warning",
                f"{odata_label} and the OLIS display did not contain the same source IDs",
                source_entity_type,
                details={
                    "odata_count": len(odata_ids),
                    "displayed_count": len(display_ids),
                    "odata_only_count": len(odata_ids - display_ids),
                    "page_only_count": len(display_ids - odata_ids),
                },
            )
        )
    for source_id in sorted(odata_ids - display_ids):
        anomalies.append(
            SourceAnomaly(
                "odata_only_display_candidate",
                "warning",
                "Structured OData record was absent from the successfully checked OLIS display",
                source_entity_type,
                source_id,
            )
        )
    for source_id in sorted(display_ids - odata_ids):
        anomalies.append(
            SourceAnomaly(
                "olis_page_only_record",
                "warning",
                "OLIS displayed a record absent from the successful OData result",
                source_entity_type,
                source_id,
            )
        )


def _result(
    documents: list[dict[str, Any]],
    anomalies: list[SourceAnomaly],
    check: DisplayCheck,
    odata_ids: set[str],
    display_ids: set[str],
) -> DocumentReconciliationResult:
    return DocumentReconciliationResult(
        documents=tuple(documents),
        anomalies=tuple(anomalies),
        display_status=check.status,
        odata_count=len(odata_ids),
        displayed_count=len(display_ids),
        matched_count=len(odata_ids & display_ids),
        odata_only_count=len(odata_ids - display_ids),
        page_only_count=len(display_ids - odata_ids),
        material_completeness_gap=any(
            anomaly.material_to_completeness for anomaly in anomalies
        ),
    )


def _displayed_value(check: DisplayCheck, present: bool) -> bool | None:
    if present:
        return True
    if check.complete:
        return False
    return None


def _origin(in_odata: bool, in_display: bool) -> str:
    if in_odata and in_display:
        return "odata_and_olis"
    if in_odata:
        return "odata_only"
    return "olis_only"


def _numeric_source_id(value: Any) -> str | None:
    text = _text(value)
    return text if text and _NUMERIC_SOURCE_ID.fullmatch(text) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


__all__ = [
    "DISPLAY_CHECK_STATUSES",
    "SOURCE_PRESENCE_STATES",
    "DisplayCheck",
    "DocumentReconciliationResult",
    "ExistingSourcePresence",
    "PresenceReconciliationResult",
    "SourceAnomaly",
    "SourcePresenceDecision",
    "detect_document_type_drift",
    "reconcile_historical_presentations",
    "reconcile_modern_public_testimony",
    "reconcile_source_presence",
]
