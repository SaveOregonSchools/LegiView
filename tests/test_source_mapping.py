import json

import pytest

from olis_archive.services.documents import committee_document, floor_letter_document
from olis_archive.services.source_mapping import (
    InvalidBillId,
    chamber_for_prefix,
    classify_committee_document,
    map_measure,
    map_sponsor,
    normalize_bill_id,
    testimony_position as map_testimony_position,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("SB1501", ("SB", 1501, "SB1501", "SB 1501")), (" hb 4111 ", ("HB", 4111, "HB4111", "HB 4111"))],
)
def test_bill_id_normalization(raw, expected):
    assert normalize_bill_id(raw) == expected


@pytest.mark.parametrize("raw", ["HJR1", "HB", "1501", "SB 1x", ""])
def test_bill_id_rejects_out_of_scope_values(raw):
    with pytest.raises(InvalidBillId):
        normalize_bill_id(raw)


def test_chamber_mapping():
    assert chamber_for_prefix("HB") == "House"
    assert chamber_for_prefix("sb") == "Senate"


def test_measure_mapping_preserves_source_dates_and_relating_fields(fixture_dir):
    raw = json.loads((fixture_dir / "measure_2026_sb1501.json").read_text())
    mapped = map_measure(raw)
    assert mapped["bill_title_source"] == "Measure.RelatingTo"
    assert mapped["bill_title"] == raw["RelatingTo"]
    assert mapped["relating_to_full"].startswith("Relating to the Moda Center")
    assert mapped["effective_date"] == "2026-03-31T00:00:00"
    assert mapped["source_modified_at"] == "2026-08-24T12:39:28"


def test_only_observed_sponsor_values_are_semantically_mapped():
    chief = map_sponsor({"SponsorType": "Member", "SponsorLevel": "Chief", "LegislatoreCode": "Sen Example"})
    assert (chief.category, chief.kind, chief.known) == ("chief", "legislator", True)
    committee = map_sponsor({"SponsorType": "Committee", "SponsorLevel": "Regular", "CommitteeCode": "HRULES"})
    assert (committee.category, committee.kind) == ("regular", "committee")
    presession = map_sponsor({"SponsorType": "Presession", "SponsorLevel": "Regular"})
    assert presession.kind == "other"
    unknown = map_sponsor({"SponsorType": "Organization", "SponsorLevel": "Honorary"})
    assert (unknown.category, unknown.kind, unknown.known) == ("unknown", "other", False)


def test_position_ids_are_observed_not_guessed():
    assert map_testimony_position(3981) == "Neutral"
    assert map_testimony_position("3982") == "Oppose"
    assert map_testimony_position(3983) == "Support"
    assert map_testimony_position(9999) == "Unknown (9999)"


def test_old_and_new_document_classification(fixture_dir):
    rows = json.loads((fixture_dir / "committee_documents_2014_hb4111.json").read_text())
    presentation = committee_document(rows[0], displayed_ids={"32769"})
    assert presentation["document_kind"] == "committee_presentation"
    assert presentation["source_section"] == "presentations_displayed_in_committee"
    # "testimony" in the title is deliberately not used to call it legacy_testimony.
    assert presentation["classification_method"] == "raw_document_type"
    other = committee_document(rows[1])
    assert other["document_kind"] == "committee_document_other"
    assert other["download_status"] == "not_applicable"
    assert classify_committee_document("future-type")[0] == "committee_document_other"


def test_floor_letter_mapping(fixture_dir):
    row = json.loads((fixture_dir / "floor_letters_2026_sb1501.json").read_text())[0]
    result = floor_letter_document(row)
    assert result["source_id"] == "4701"
    assert result["chamber"] == "Senate"
    assert result["document_kind"] == "floor_letter"
