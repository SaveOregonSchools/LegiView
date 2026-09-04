from __future__ import annotations

from dataclasses import dataclass

import pytest

from olis_archive.services.historical_sources import (
    HistoricalSourceError,
    build_session_entity_plan,
    resolve_historical_session_scope,
    resolve_historical_session_scope_from_odata,
    stream_session_entity,
    testimony_reconciliation_candidate as choose_testimony_reconciliation_candidate,
    validate_odata_metadata,
)
from olis_archive.services.odata import ODataPage
from olis_archive.services.source_mapping import measure_scope_filter


def _session(key: str, begin: str, name: str | None = None):
    return {
        "SessionKey": key,
        "SessionName": name or key,
        "BeginDate": begin,
        "EndDate": None,
        "CreatedDate": begin,
        "ModifiedDate": None,
        "DefaultSession": key == "2026R1",
    }


def test_official_session_scope_uses_boundary_chronology_and_keeps_special_sessions():
    rows = [
        _session("2026R1", "2026-02-02T00:00:00"),
        _session("2014S1", "2014-09-15T00:00:00", "2014 Special Session"),
        _session("2013R1", "2013-02-04T00:00:00"),
        _session("2015I1", "2015-12-01T00:00:00", "2015 Interim Session"),
        _session("2015R1", "2015-02-02T00:00:00"),
        _session("2014R1", "2014-02-03T00:00:00"),
    ]

    scope = resolve_historical_session_scope(rows)

    assert scope.all_session_keys == (
        "2013R1",
        "2014R1",
        "2014S1",
        "2015R1",
        "2015I1",
        "2026R1",
    )
    assert tuple(row.session_key for row in scope.unsupported_sessions) == ("2013R1",)
    assert scope.session_keys == (
        "2014R1",
        "2014S1",
        "2015R1",
        "2015I1",
        "2026R1",
    )
    assert "2013R1" not in scope.session_keys
    assert scope.session_keys[-1] == "2026R1"
    assert scope.requested_scope()["session_keys"] == list(scope.session_keys)
    assert scope.selected(["2026R1", "2014S1"]).session_keys == ("2014S1", "2026R1")
    assert scope.selected_range("2014S1", "2015I1").session_keys == (
        "2014S1",
        "2015R1",
        "2015I1",
    )
    assert scope.is_at_or_after("2026R1", "2014R1")
    assert not scope.is_at_or_after("2014R1", "2015R1")


def test_session_range_rejects_reversal_unknown_keys_and_older_official_rows():
    scope = resolve_historical_session_scope(
        [
            _session("2013R1", "2013-02-04T00:00:00"),
            _session("2014R1", "2014-02-03T00:00:00"),
            _session("2014S1", "2014-09-15T00:00:00"),
            _session("2015I1", "2015-12-01T00:00:00"),
        ]
    )

    with pytest.raises(HistoricalSourceError, match="newer than To session"):
        scope.selected_range("2015I1", "2014R1")
    with pytest.raises(HistoricalSourceError, match="official catalogue.*2099R1"):
        scope.selected_range("2014R1", "2099R1")
    with pytest.raises(HistoricalSourceError, match="predate.*2014R1.*2013R1"):
        scope.selected_range("2013R1", "2015I1")
    with pytest.raises(HistoricalSourceError, match="official catalogue.*2099R1"):
        scope.selected(["2014R1", "2099R1"])


def test_official_session_scope_requires_exact_boundary_record():
    with pytest.raises(HistoricalSourceError, match="2014R1"):
        resolve_historical_session_scope([_session("2014S1", "2014-09-15T00:00:00")])


def test_official_session_scope_retains_but_disables_incompatible_legacy_rows():
    malformed_date = _session("LEGACY-A", "not-a-date", "Legacy import")
    missing_key = _session("2001R1", "2001-01-01T00:00:00", "Missing key")
    missing_key.pop("SessionKey")
    scope = resolve_historical_session_scope(
        [
            malformed_date,
            _session("2025R1", "not-a-date", "Malformed modern row"),
            missing_key,
            _session("1999R1", "1999-01-11T00:00:00", "1999 Regular Session"),
            _session("2014R1", "2014-02-03T00:00:00"),
            _session("2026R1", "2026-02-02T00:00:00"),
        ]
    )

    assert scope.supported_session_keys == ("2014R1", "2026R1")
    unsupported = {row.session_key: row for row in scope.unsupported_sessions}
    assert "1999R1" in unsupported
    assert "SessionKey" in unsupported["1999R1"].compatibility_issue
    assert "LEGACY-A" in unsupported
    assert "BeginDate" in unsupported["LEGACY-A"].compatibility_issue
    assert "BeginDate" in unsupported["2025R1"].compatibility_issue
    assert any("unusable official row" in key for key in unsupported)
    guardrails = scope.requested_scope()["catalogue_guardrails"]
    assert {row["session_key"] for row in guardrails} == set(unsupported)
    with pytest.raises(
        HistoricalSourceError,
        match=r"incompatible.*2025R1.*BeginDate",
    ):
        scope.selected(["2025R1"])


def test_official_session_scope_follows_all_odata_pages():
    class Client:
        def iter_pages(self, entity_set, **params):
            assert entity_set == "LegislativeSessions"
            assert params == {"orderby": "BeginDate,SessionKey"}
            yield ODataPage(
                (_session("2013R1", "2013-02-04T00:00:00"),), "next", None, None
            )
            yield ODataPage(
                (
                    _session("2014R1", "2014-02-03T00:00:00"),
                    _session("2014S1", "2014-09-15T00:00:00"),
                ),
                None,
                None,
                None,
            )

    scope = resolve_historical_session_scope_from_odata(Client())
    assert scope.session_keys == ("2014R1", "2014S1")
    assert scope.all_session_keys == ("2013R1", "2014R1", "2014S1")


def test_session_entity_plans_use_all_supported_prefixes_and_inclusive_watermark():
    plan = build_session_entity_plan(
        "CommitteePublicTestimonies",
        "2026R1",
        source_watermark="2026-05-15T12:28:47",
    )
    assert plan.strategy == "watermark"
    assert plan.authoritative_presence is False
    assert "SessionKey eq '2026R1'" in plan.filter_expression
    assert measure_scope_filter() in plan.filter_expression
    assert "CreatedDate ge datetime'2026-05-15T12:28:47'" in plan.filter_expression
    assert "ModifiedDate ge datetime'2026-05-15T12:28:47'" in plan.filter_expression
    assert " gt " not in plan.filter_expression


def test_floor_letters_never_invent_a_source_date_cursor():
    plan = build_session_entity_plan(
        "FloorLetters",
        "2026R1",
        source_watermark="2026-05-15T12:28:47",
    )
    assert plan.strategy == "full_session"
    assert plan.source_watermark is None
    assert plan.authoritative_presence is True
    assert "CreatedDate" not in plan.filter_expression
    assert "ModifiedDate" not in plan.filter_expression


@dataclass
class _FakePagedClient:
    pages: tuple[ODataPage, ...]
    failure_after: int | None = None

    def iter_pages(self, entity_set, **params):
        assert entity_set == "Measures"
        assert params["orderby"] == "MeasurePrefix,MeasureNumber"
        for index, page in enumerate(self.pages):
            if self.failure_after is not None and index >= self.failure_after:
                raise RuntimeError("page two failed")
            yield page


def _measure(number: int, created: str, modified: str | None = None):
    return {
        "SessionKey": "2014R1",
        "MeasurePrefix": "HB" if number < 4000 else "SB",
        "MeasureNumber": number,
        "CreatedDate": created,
        "ModifiedDate": modified,
    }


def test_session_entity_stream_delivers_pages_and_returns_commit_ready_cursor():
    pages = (
        ODataPage(
            (_measure(2001, "2014-01-01T01:00:00"),),
            "https://api.oregonlegislature.gov/odata/odataservice.svc/Measures?$skiptoken=x",
            2,
            "metadata",
        ),
        ODataPage(
            (_measure(4001, "2014-01-02T01:00:00", "2014-02-03T04:05:06"),),
            None,
            2,
            "metadata",
        ),
    )
    batches = []

    result = stream_session_entity(
        _FakePagedClient(pages),
        build_session_entity_plan("Measures", "2014R1"),
        batches.append,
    )

    assert [batch.page_number for batch in batches] == [1, 2]
    assert result.page_count == 2
    assert result.returned_count == 2
    assert result.next_source_watermark == "2014-02-03T04:05:06"
    assert result.authoritative_presence is True


def test_failed_paged_query_cannot_return_or_advance_a_cursor():
    pages = (
        ODataPage((_measure(2001, "2014-01-01T01:00:00"),), "next", None, None),
        ODataPage((_measure(2002, "2014-02-01T01:00:00"),), None, None, None),
    )
    committed_cursor = {"value": "2013-12-01T00:00:00"}

    with pytest.raises(RuntimeError, match="page two"):
        result = stream_session_entity(
            _FakePagedClient(pages, failure_after=1),
            build_session_entity_plan(
                "Measures", "2014R1", source_watermark=committed_cursor["value"]
            ),
            lambda batch: None,
        )
        committed_cursor["value"] = result.next_source_watermark

    assert committed_cursor["value"] == "2013-12-01T00:00:00"


def test_stream_passes_control_only_to_explicitly_compatible_client():
    observed = {}
    callback = lambda: False

    class ControlAwareClient:
        def iter_pages(
            self,
            entity_set,
            *,
            cancellation_requested=None,
            **params,
        ):
            observed["entity_set"] = entity_set
            observed["callback"] = cancellation_requested
            observed["params"] = params
            yield ODataPage((), None, 0, None)

    stream_session_entity(
        ControlAwareClient(),
        build_session_entity_plan("Measures", "2014R1"),
        lambda batch: None,
        cancellation_requested=callback,
    )

    assert observed["callback"] is callback
    assert "cancellation_requested" not in observed["params"]


def test_stream_accepts_supported_resolutions_and_rejects_unknown_prefixes():
    supported = {
        "SessionKey": "2014R1",
        "MeasurePrefix": "HJR",
        "MeasureNumber": 1,
        "CreatedDate": "2014-01-01T00:00:00",
    }
    stream_session_entity(
        _FakePagedClient((ODataPage((supported,), None, None, None),)),
        build_session_entity_plan("Measures", "2014R1"),
        lambda batch: None,
    )

    bad = {
        "SessionKey": "2014R1",
        "MeasurePrefix": "XYZ",
        "MeasureNumber": 1,
        "CreatedDate": "2014-01-01T00:00:00",
    }
    page = ODataPage((bad,), None, None, None)
    with pytest.raises(HistoricalSourceError, match="MeasurePrefix"):
        stream_session_entity(
            _FakePagedClient((page,)),
            build_session_entity_plan("Measures", "2014R1"),
            lambda batch: None,
        )


def test_candidate_strategy_is_narrow_but_conservative_for_unknown_types():
    assert choose_testimony_reconciliation_candidate(
        public_testimony_rows=[{"CommTestId": 1}]
    ).reasons == ("odata_public_testimony",)
    assert choose_testimony_reconciliation_candidate(
        committee_document_rows=[{"DocumentType": "Presentation"}]
    ).candidate
    unknown = choose_testimony_reconciliation_candidate(
        committee_document_rows=[{"DocumentType": "Future Exhibit Type"}]
    )
    assert unknown.reasons == ("unknown_committee_document_type",)
    assert choose_testimony_reconciliation_candidate(
        committee_document_rows=[{"DocumentType": "Meeting Material"}]
    ).candidate is False
    assert choose_testimony_reconciliation_candidate(
        agenda_item_rows=[{"MeetingType": "Public Hearing"}]
    ).reasons == ("public_hearing_agenda",)


def test_metadata_contract_preserves_official_typos_and_flags_drift():
    entity_sets = {
        "LegislativeSessions": ["SessionKey", "SessionName", "BeginDate", "EndDate", "CreatedDate", "ModifiedDate"],
        "Measures": ["SessionKey", "MeasurePrefix", "MeasureNumber", "RelatingTo", "RelatingToFull", "CreatedDate", "ModifiedDate"],
        "Legislators": ["SessionKey", "LegislatorCode", "FirstName", "LastName", "CreatedDate", "ModifiedDate"],
        "Committees": ["SessionKey", "CommitteeCode", "CommitteeName", "CreatedDate", "ModifiedDate"],
        "MeasureSponsors": ["SessionKey", "MeasurePrefix", "MeasureNumber", "MeasureSponsorId", "LegislatoreCode", "CreatedDate", "ModifiedDate"],
        "CommitteeMeetings": ["SessionKey", "CommitteeCode", "MeetingDate", "CreatedDate", "ModifiedDate"],
        "CommitteeAgendaItems": ["SessionKey", "MeasurePrefix", "MeasureNumber", "CommitteeAgendaItemId", "CommitteCode", "CreatedDate", "ModifiedDate"],
        "CommitteeMeetingDocuments": ["SessionKey", "MeasurePrefix", "MeasureNumber", "CommitteeMeetingDocumentId", "DocumentType", "CreatedDate", "ModifiedDate"],
        "CommitteePublicTestimonies": ["SessionKey", "MeasurePrefix", "MeasureNumber", "CommTestId", "PdfCreatedFlag", "CreatedDate", "ModifiedDate"],
        "FloorLetters": ["SessionKey", "MeasurePrefix", "MeasureNumber", "FloorLetterId", "FloorLetterUrl"],
    }
    type_xml = []
    set_xml = []
    for index, (entity_set, properties) in enumerate(entity_sets.items()):
        type_xml.append(
            f'<EntityType Name="T{index}">'
            + "".join(f'<Property Name="{name}" Type="Edm.String" />' for name in properties)
            + "</EntityType>"
        )
        set_xml.append(f'<EntitySet Name="{entity_set}" EntityType="Model.T{index}" />')
    xml = (
        '<edmx:Edmx xmlns:edmx="urn:edmx"><edmx:DataServices>'
        '<Schema xmlns="urn:edm">'
        + "".join(type_xml)
        + '<EntityContainer Name="Default">'
        + "".join(set_xml)
        + "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    )

    report = validate_odata_metadata(xml)
    assert report.compatible
    assert not report.issues
    drifted = validate_odata_metadata(xml.replace(' Name="LegislatoreCode"', ' Name="LegislatorCode"'))
    assert any(
        issue.entity_set == "MeasureSponsors"
        and issue.property_name == "LegislatoreCode"
        for issue in drifted.issues
    )
