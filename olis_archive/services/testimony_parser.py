"""Deterministic extraction of the server-rendered OLIS testimony tables."""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag


_DOWNLOAD_RE = re.compile(
    r"^/liz/(?P<session>[A-Za-z0-9]+)/Downloads/"
    r"(?P<family>PublicTestimonyDocument|CommitteeMeetingDocument)/(?P<id>\d+)/?$",
    re.IGNORECASE,
)
_DOWNLOAD_ROUTE_CANDIDATE_RE = re.compile(
    r"/liz/[A-Za-z0-9]+/Downloads/"
    r"(?:PublicTestimonyDocument|CommitteeMeetingDocument)/",
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


@dataclass(frozen=True, slots=True)
class TestimonyPageParseResult:
    """Outcome that distinguishes a real empty page from parser uncertainty."""

    status: str
    documents: tuple[ParsedTestimonyDocument, ...]
    recognized_sections: tuple[str, ...]
    recognized_document_count: int
    anomalies: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.status in {"checked_with_records", "checked_zero"}


def extract_numeric_document_id(url: str, expected_family: str | None = None) -> str:
    match = _DOWNLOAD_RE.fullmatch(_normalized_path(urlsplit(url).path))
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


def inspect_testimony_page(
    html: str,
    *,
    page_url: str,
    expected_session: str | None = None,
) -> TestimonyPageParseResult:
    """Parse an OLIS display page and report whether an empty result is proven.

    Phase 1's :func:`parse_testimony_page` intentionally returns a simple list.
    Historical reconciliation also needs to know whether ``[]`` means OLIS
    explicitly displayed no items or whether the markup changed underneath the
    parser.  This wrapper preserves the original API while adding that state.
    """

    soup = BeautifulSoup(html, "html.parser")
    headings = tuple(
        _clean(node.get_text(" ", strip=True))
        for node in soup.find_all(re.compile(r"^h[1-6]$", re.IGNORECASE))
        if _clean(node.get_text(" ", strip=True))
    )
    recognized_sections = tuple(
        heading
        for heading in headings
        if (
            "submitted written public testimony" in heading.casefold()
            or "presentations displayed in committee" in heading.casefold()
        )
    )
    recognized_ids: set[tuple[str, str]] = set()
    # Inspect document links inside tables only.  OLIS can repeat responsive
    # links elsewhere on the page, and global href counts are not row counts.
    for table in soup.find_all("table"):
        for anchor in table.find_all("a", href=True):
            absolute = urljoin(page_url, str(anchor.get("href")))
            try:
                match = _DOWNLOAD_RE.fullmatch(_normalized_path(urlsplit(absolute).path))
            except ValueError:
                match = None
            if not match:
                continue
            if expected_session and match.group("session").upper() != expected_session.upper():
                continue
            recognized_ids.add((match.group("family").casefold(), match.group("id")))
    try:
        documents = tuple(
            parse_testimony_page(
                html,
                page_url=page_url,
                expected_session=expected_session,
            )
        )
    except (TypeError, ValueError) as exc:
        return TestimonyPageParseResult(
            status="parser_anomalous",
            documents=(),
            recognized_sections=recognized_sections,
            recognized_document_count=len(recognized_ids),
            anomalies=(str(exc),),
        )

    parsed_ids = {
        (document.source_entity_type, document.source_document_id)
        for document in documents
    }
    expected_parsed_ids = {
        (
            "CommitteePublicTestimony"
            if family == "publictestimonydocument"
            else "CommitteeMeetingDocument",
            source_id,
        )
        for family, source_id in recognized_ids
    }
    if expected_parsed_ids - parsed_ids:
        return TestimonyPageParseResult(
            status="parser_anomalous",
            documents=documents,
            recognized_sections=recognized_sections,
            recognized_document_count=len(recognized_ids),
            anomalies=(
                "Recognizable OLIS document links were not represented by parsed table rows",
            ),
        )
    if documents:
        return TestimonyPageParseResult(
            status="checked_with_records",
            documents=documents,
            recognized_sections=recognized_sections,
            recognized_document_count=len(recognized_ids),
        )

    visible_text = _clean(soup.get_text(" ", strip=True)).casefold()
    if recognized_sections and "no items to display" in visible_text:
        return TestimonyPageParseResult(
            status="checked_zero",
            documents=(),
            recognized_sections=recognized_sections,
            recognized_document_count=0,
        )
    detail = (
        "Recognized OLIS testimony/presentation section had no parsed rows and no explicit empty marker"
        if recognized_sections
        else "OLIS page did not contain a recognized testimony/presentation section or empty marker"
    )
    return TestimonyPageParseResult(
        status="parser_anomalous",
        documents=(),
        recognized_sections=recognized_sections,
        recognized_document_count=0,
        anomalies=(detail,),
    )


def _document_link(cell: Tag | None, page_url: str):
    if not cell:
        return None, None
    for anchor in cell.find_all("a", href=True):
        absolute = urljoin(page_url, str(anchor.get("href")))
        parsed = urlsplit(absolute)
        normalized_path = _normalized_path(parsed.path)
        match = _DOWNLOAD_RE.fullmatch(normalized_path)
        if match is None and _DOWNLOAD_ROUTE_CANDIDATE_RE.search(normalized_path):
            raise ValueError("Testimony download URL did not use the canonical OLIS route")
        if match:
            try:
                port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
            except ValueError as exc:
                raise ValueError("Testimony download URL had an invalid port") from exc
            if (
                parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold().rstrip(".")
                != "olis.oregonlegislature.gov"
                or port != 443
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("Testimony download URL left the OLIS HTTPS host")
            return absolute, match
    return None, None


def _normalized_path(path: str) -> str:
    decoded = path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "\x00" in decoded:
        raise ValueError("Testimony download URL contained a null byte")
    return posixpath.normpath(decoded.replace("\\", "/"))


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


__all__ = [
    "ParsedTestimonyDocument",
    "TestimonyPageParseResult",
    "extract_numeric_document_id",
    "inspect_testimony_page",
    "parse_testimony_page",
]
