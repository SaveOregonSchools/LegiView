"""Deterministic extraction of the server-rendered OLIS testimony tables."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag


_DOWNLOAD_RE = re.compile(
    r"/liz/(?P<session>[A-Za-z0-9]+)/Downloads/"
    r"(?P<family>PublicTestimonyDocument|CommitteeMeetingDocument)/(?P<id>\d+)",
    re.IGNORECASE,
)
_COMMITTEE_RE = re.compile(r"/Committees/(?P<code>[A-Za-z0-9]+)/", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedTestimonyDocument:
    source_entity_type: str
    source_document_id: str
    source_section: str
    title: str | None
    submitter: str | None
    on_behalf_of: str | None
    position: str | None
    city_or_organization: str | None
    meeting_date: str | None
    committee_code: str | None
    committee_name: str | None
    download_url: str


def extract_numeric_document_id(url: str, expected_family: str | None = None) -> str:
    match = _DOWNLOAD_RE.search(urlsplit(url).path)
    if not match:
        raise ValueError(f"No supported numeric OLIS document ID in {url!r}")
    family = match.group("family")
    if expected_family and family.casefold() != expected_family.casefold():
        raise ValueError(f"Expected {expected_family}, found {family}")
    return match.group("id")


def parse_testimony_page(
    html: str,
    *,
    page_url: str,
    expected_session: str | None = None,
) -> list[ParsedTestimonyDocument]:
    soup = BeautifulSoup(html, "html.parser")
    by_identity: dict[tuple[str, str], ParsedTestimonyDocument] = {}
    for table in soup.find_all("table"):
        headers = [_clean(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
        if not headers:
            first = table.find("tr")
            headers = [_clean(cell.get_text(" ", strip=True)) for cell in first.find_all(["th", "td"]) ] if first else []
        lowered = {_canonical_header(value): index for index, value in enumerate(headers)}
        if "title" not in lowered:
            continue
        for row in table.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            title_cell = _cell(cells, lowered.get("title", 0))
            link, match = _document_link(title_cell, page_url)
            if not link or not match:
                continue
            session = match.group("session").upper()
            if expected_session and session != expected_session.upper():
                raise ValueError(
                    f"Testimony page contained a document for unexpected session {session}"
                )
            family = match.group("family")
            if family.casefold() == "publictestimonydocument":
                entity_type = "CommitteePublicTestimony"
                section = "submitted_written_testimony"
            else:
                entity_type = "CommitteeMeetingDocument"
                section = "presentations_displayed_in_committee"
            committee_cell = _cell(cells, lowered.get("committee", -1))
            committee_link = committee_cell.find("a", href=True) if committee_cell else None
            committee_match = (
                _COMMITTEE_RE.search(str(committee_link.get("href"))) if committee_link else None
            )
            parsed = ParsedTestimonyDocument(
                source_entity_type=entity_type,
                source_document_id=match.group("id"),
                source_section=section,
                title=_text_from_cell(title_cell),
                submitter=_text_from_cell(_cell(cells, lowered.get("submitter", -1))),
                on_behalf_of=_text_from_cell(_cell(cells, lowered.get("onbehalfof", -1))),
                position=_text_from_cell(_cell(cells, lowered.get("position", -1))),
                city_or_organization=_text_from_cell(
                    _cell(cells, lowered.get("cityororganization", -1))
                ),
                meeting_date=_text_from_cell(_cell(cells, lowered.get("meeting", -1))),
                committee_code=committee_match.group("code") if committee_match else None,
                committee_name=_text_from_cell(committee_cell),
                download_url=link,
            )
            key = (entity_type, parsed.source_document_id)
            if key in by_identity and by_identity[key] != parsed:
                raise ValueError(f"Conflicting duplicate testimony row: {key}")
            by_identity[key] = parsed
    return list(by_identity.values())


def _document_link(cell: Tag | None, page_url: str):
    if not cell:
        return None, None
    for anchor in cell.find_all("a", href=True):
        absolute = urljoin(page_url, str(anchor.get("href")))
        match = _DOWNLOAD_RE.search(urlsplit(absolute).path)
        if match:
            parsed = urlsplit(absolute)
            if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != "olis.oregonlegislature.gov":
                raise ValueError("Testimony download URL left the OLIS HTTPS host")
            return absolute, match
    return None, None


def _canonical_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _cell(cells: Iterable[Tag], index: int) -> Tag | None:
    values = list(cells)
    if index < 0 or index >= len(values):
        return None
    return values[index]


def _text_from_cell(cell: Tag | None) -> str | None:
    if cell is None:
        return None
    value = _clean(cell.get_text(" ", strip=True))
    return value or None


__all__ = ["ParsedTestimonyDocument", "extract_numeric_document_id", "parse_testimony_page"]

