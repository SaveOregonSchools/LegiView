import pytest

from olis_archive.services.testimony_parser import (
    extract_numeric_document_id,
    inspect_testimony_page,
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


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://user@olis.oregonlegislature.gov/liz/2026R1/Downloads/PublicTestimonyDocument/244133",
        "https://olis.oregonlegislature.gov:8443/liz/2026R1/Downloads/PublicTestimonyDocument/244133",
    ],
)
def test_document_links_require_the_exact_olis_https_origin(fixture_dir, unsafe_url):
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text()
    html = html.replace(
        'href="/liz/2026R1/Downloads/PublicTestimonyDocument/244133"',
        f'href="{unsafe_url}"',
        1,
    )

    with pytest.raises(ValueError, match="OLIS HTTPS host"):
        parse_testimony_page(
            html,
            page_url="https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501",
            expected_session="2026R1",
        )


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://olis.oregonlegislature.gov/other/liz/2026R1/Downloads/PublicTestimonyDocument/244133",
        "https://olis.oregonlegislature.gov/liz/2026R1/Downloads/PublicTestimonyDocument/244133-extra",
    ],
)
def test_document_links_require_the_exact_numeric_download_route(fixture_dir, unsafe_url):
    html = (fixture_dir / "modern_testimony_2026_sb1501.html").read_text()
    html = html.replace(
        'href="/liz/2026R1/Downloads/PublicTestimonyDocument/244133"',
        f'href="{unsafe_url}"',
        1,
    )

    with pytest.raises(ValueError, match="canonical OLIS route"):
        parse_testimony_page(
            html,
            page_url="https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/SB1501",
            expected_session="2026R1",
        )


def test_unrelated_empty_marker_without_a_recognized_section_is_anomalous():
    parsed = inspect_testimony_page(
        "<html><body><h2>Unrelated results</h2><p>No items to display.</p></body></html>",
        page_url="https://olis.oregonlegislature.gov/liz/2026R1/Measures/Testimony/HB2001",
        expected_session="2026R1",
    )

    assert parsed.status == "parser_anomalous"
    assert not parsed.successful
