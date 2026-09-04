from __future__ import annotations

import json

from olis_archive.services.reconciliation import (
    DisplayCheck,
    ExistingSourcePresence,
    detect_document_type_drift,
    reconcile_historical_presentations,
    reconcile_modern_public_testimony,
    reconcile_source_presence,
)
from olis_archive.services.testimony_parser import inspect_testimony_page


MODERN_URL = "https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501"
LEGACY_URL = "https://olis.oregonlegislature.gov/liz/2014R1/Measures/Testimony/HB4111"


def _modern_row(source_id: int, *, pdf_created: str = "Y"):
    return {
        "CommTestId": source_id,
        "SessionKey": "2026R1",
        "MeasurePrefix": "SB",
        "MeasureNumber": 1501,
        "CommitteeCode": "SRULES",
        "MeetingDate": "2026-02-11T08:00:00",
        "SubmitterFirstName": "Sample",
        "SubmitterLastName": "Person",
        "DocumentDescription": "Testimony",
        "PositionOnMeasureId": 3983,
        "PdfCreatedFlag": pdf_created,
        # The live source's DocumentUrl is not a payload URL.
        "DocumentUrl": "https://olis.oregonlegislature.gov/liz/2026R1",
        "CreatedDate": "2026-02-10T01:02:03",
        "ModifiedDate": None,
    }


def test_modern_odata_is_primary_and_successful_html_check_sets_nullable_display_state(
    fixture_dir,
):
    parsed = inspect_testimony_page(
        (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(),
        page_url=MODERN_URL,
        expected_session="2026R1",
    )
    check = DisplayCheck.from_parse_result(parsed, checked_at="2026-09-03T10:00:00Z")
    result = reconcile_modern_public_testimony(
        [_modern_row(244133), _modern_row(255890)],
        check,
    )

    by_id = {document["source_id"]: document for document in result.documents}
    assert by_id["244133"]["displayed_in_olis"] is True
    assert by_id["244133"]["reconciliation_origin"] == "odata_and_olis"
    assert by_id["255890"]["displayed_in_olis"] is False
    assert by_id["255890"]["reconciliation_origin"] == "odata_only"
    assert by_id["244244"]["source_presence"] == "unknown"
    assert by_id["244244"]["reconciliation_origin"] == "olis_only"
    assert result.matched_count == 1
    assert result.odata_only_count == 1
    assert result.page_only_count == 2
    assert any(anomaly.source_id == "255890" for anomaly in result.anomalies)
    persisted = result.storage_display_values("CommitteePublicTestimony")
    assert persisted["status"] == "checked_with_records"
    assert set(persisted["displayed_source_ids"]) == {"244133", "244244", "248220"}
    assert persisted["odata_only_count"] == 1
    # PdfCreatedFlag is metadata, never proof that the known zero-byte payload
    # was downloaded or validated.
    assert by_id["255890"]["download_status"] == "discovered"
    assert by_id["255890"]["canonical_download_url"].endswith(
        "/PublicTestimonyDocument/255890"
    )


def test_failed_modern_page_leaves_display_state_unknown_and_affects_completeness():
    result = reconcile_modern_public_testimony(
        [_modern_row(255890)],
        DisplayCheck.failed(TimeoutError("timed out"), checked_at="2026-09-03T10:00:00Z"),
    )
    assert result.documents[0]["displayed_in_olis"] is None
    assert result.documents[0]["display_reconciled_at"] is None
    assert result.material_completeness_gap
    assert any(anomaly.anomaly_type == "olis_display_fetch_failed" for anomaly in result.anomalies)


def test_explicit_zero_result_page_is_successful_not_parser_anomaly():
    parsed = inspect_testimony_page(
        "<html><body><h5>Submitted Written Public Testimony</h5><p>No items to display.</p></body></html>",
        page_url=MODERN_URL,
        expected_session="2026R1",
    )
    assert parsed.status == "checked_zero"
    check = DisplayCheck.from_parse_result(parsed, checked_at="2026-09-03T10:00:00Z")
    result = reconcile_modern_public_testimony([], check)
    assert not result.documents
    assert not result.material_completeness_gap


def test_recognizable_changed_markup_is_parser_anomalous_and_not_false_zero():
    parsed = inspect_testimony_page(
        """
        <html><body><h5>Submitted Written Public Testimony</h5>
        <table><thead><tr><th>New Heading</th></tr></thead><tbody><tr><td>
        <a href="/liz/2026R1/Downloads/PublicTestimonyDocument/244133">Testimony</a>
        </td></tr></tbody></table></body></html>
        """,
        page_url=MODERN_URL,
        expected_session="2026R1",
    )
    assert parsed.status == "parser_anomalous"
    assert parsed.recognized_document_count == 1
    check = DisplayCheck.from_parse_result(parsed, checked_at="2026-09-03T10:00:00Z")
    result = reconcile_modern_public_testimony([_modern_row(244133)], check)
    assert result.documents[0]["displayed_in_olis"] is None
    assert result.material_completeness_gap


def test_historical_reconciliation_marks_only_presentation_mismatches(fixture_dir):
    parsed = inspect_testimony_page(
        (fixture_dir / "legacy_testimony_2014_hb4111.html").read_text(),
        page_url=LEGACY_URL,
        expected_session="2014R1",
    )
    check = DisplayCheck.from_parse_result(parsed, checked_at="2026-09-03T10:00:00Z")
    rows = json.loads((fixture_dir / "committee_documents_2014_hb4111.json").read_text())
    # The fixture's first row is Presentation; its second is Witness
    # Registration and is intentionally not expected in the OLIS section.
    result = reconcile_historical_presentations(rows[:2], check)

    by_id = {document["source_id"]: document for document in result.documents}
    assert by_id["32769"]["document_kind"] == "committee_presentation"
    assert by_id["32769"]["displayed_in_olis"] is True
    assert by_id["32770"]["source_presence"] == "unknown"  # page-only fixture row
    witness_id = str(rows[1]["CommitteeMeetingDocumentId"])
    assert by_id[witness_id]["document_kind"] == "committee_document_other"
    assert by_id[witness_id]["displayed_in_olis"] is False
    assert not any(
        anomaly.anomaly_type == "odata_only_display_candidate"
        and anomaly.source_id == witness_id
        for anomaly in result.anomalies
    )


def test_presence_missing_requires_successful_authoritative_full_comparison():
    existing = [
        ExistingSourcePresence("1", "active"),
        ExistingSourcePresence("2", "active"),
    ]
    failed = reconcile_source_presence(
        existing,
        ["1"],
        query_succeeded=False,
        authoritative_full=True,
        reconciled_at="2026-09-03T10:00:00Z",
    )
    assert {decision.source_id: decision.source_presence for decision in failed.decisions} == {
        "1": "active",
        "2": "active",
    }
    incremental = reconcile_source_presence(
        existing,
        ["1"],
        query_succeeded=True,
        authoritative_full=False,
        reconciled_at="2026-09-03T10:00:00Z",
    )
    assert {decision.source_id: decision.source_presence for decision in incremental.decisions}[
        "2"
    ] == "active"
    complete = reconcile_source_presence(
        existing,
        ["1"],
        query_succeeded=True,
        authoritative_full=True,
        reconciled_at="2026-09-03T10:00:00Z",
    )
    by_id = {decision.source_id: decision for decision in complete.decisions}
    assert by_id["2"].source_presence == "missing"
    assert by_id["2"].missing_from_source_since == "2026-09-03T10:00:00Z"
    assert complete.newly_missing_count == 1


def test_missing_record_can_return_without_losing_missing_history_signal():
    result = reconcile_source_presence(
        [ExistingSourcePresence("2", "missing", "2026-08-01T00:00:00Z")],
        ["2"],
        query_succeeded=True,
        authoritative_full=False,
        reconciled_at="2026-09-03T10:00:00Z",
    )
    decision = result.decisions[0]
    assert decision.source_presence == "active"
    assert decision.missing_from_source_since is None
    assert decision.transition == "reappeared"
    assert result.reappeared_count == 1


def test_raw_and_normalized_type_drift_is_durable_and_unknown_type_is_retained():
    anomalies = detect_document_type_drift(
        source_entity_type="CommitteeMeetingDocument",
        source_id="32769",
        previous_raw_type="Presentation",
        previous_normalized_kind="committee_presentation",
        incoming_raw_type="Future Exhibit Type",
    )
    kinds = {anomaly.anomaly_type for anomaly in anomalies}
    assert kinds == {
        "raw_document_type_changed",
        "normalized_document_kind_changed",
        "unknown_document_type",
    }
    unknown = next(anomaly for anomaly in anomalies if anomaly.anomaly_type == "unknown_document_type")
    assert unknown.details["raw_document_type"] == "Future Exhibit Type"
    assert unknown.details["retained_kind"] == "committee_document_other"
