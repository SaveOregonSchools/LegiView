from olis_archive.services.testimony_parser import (
    extract_numeric_document_id,
    parse_testimony_page,
)


def test_modern_fixture_extracts_every_server_rendered_row(fixture_dir):
    rows = parse_testimony_page(
        (fixture_dir / "modern_testimony_2026_sb1501.html").read_text(),
        page_url="https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501",
        expected_session="2026R1",
    )
    assert [row.source_document_id for row in rows] == ["244133", "244244", "248220"]
    assert rows[0].source_section == "submitted_written_testimony"
    assert rows[0].committee_code == "SRULES"
    assert rows[1].on_behalf_of == "Community Group"


def test_legacy_fixture_keeps_presentation_identity(fixture_dir):
    rows = parse_testimony_page(
        (fixture_dir / "legacy_testimony_2014_hb4111.html").read_text(),
        page_url="https://olis.oregonlegislature.gov/liz/2014R1/Measures/Testimony/HB4111",
        expected_session="2014R1",
    )
    assert len(rows) == 2
    assert {row.source_entity_type for row in rows} == {"CommitteeMeetingDocument"}
    assert all(row.source_section == "presentations_displayed_in_committee" for row in rows)


def test_successful_zero_result_page_is_not_an_error():
    assert parse_testimony_page(
        "<html><body><p>No items to display.</p></body></html>",
        page_url="https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/HB2001",
        expected_session="2026R1",
    ) == []


def test_numeric_id_extraction_enforces_family():
    url = "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/PublicTestimonyDocument/244133"
    assert extract_numeric_document_id(url, "PublicTestimonyDocument") == "244133"

