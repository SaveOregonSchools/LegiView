"""Conservative content detection and validation for untrusted OLIS files.

The detector intentionally treats response headers and filenames as hints.  A
strong byte signature (and, for OOXML, package contents) wins when the hints
disagree.  Nothing in this module executes or renders a downloaded payload.
"""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from .hashing import sha256_file


GENERIC_BINARY_MIME_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
)

MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}

EXTENSION_ALIASES = {
    ".jpg": frozenset({".jpg", ".jpeg", ".jpe"}),
    ".tiff": frozenset({".tif", ".tiff"}),
    ".html": frozenset({".htm", ".html"}),
    ".xml": frozenset({".xml"}),
}

KNOWN_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".zip",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".ppt",
        ".pptx",
        ".rtf",
        ".txt",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".png",
        ".gif",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    }
)

ZIP_MAX_ENTRIES = 10_000
ZIP_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 250


@dataclass(frozen=True, slots=True)
class FileTypeDetection:
    extension: str
    mime_type: str
    confidence: str
    evidence: str

    @property
    def strong(self) -> bool:
        return self.confidence == "strong"


@dataclass(frozen=True, slots=True)
class FileValidation:
    status: str
    details: str
    detection: FileTypeDetection

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def normalize_mime_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().casefold()


normalized_media_type = normalize_mime_type


def extension_for_mime_type(mime_type: str | None) -> str:
    normalized = normalize_mime_type(mime_type)
    if normalized in GENERIC_BINARY_MIME_TYPES:
        return ""
    if normalized in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[normalized]
    return (mimetypes.guess_extension(normalized) or "").casefold()


def has_compatible_extension(filename: str, extension: str) -> bool:
    suffix = Path(filename).suffix.casefold()
    expected = extension.casefold()
    return bool(expected) and suffix in EXTENSION_ALIASES.get(expected, frozenset({expected}))


def has_recognized_extension(filename: str) -> bool:
    return Path(filename).suffix.casefold() in KNOWN_EXTENSIONS


def filename_with_extension(filename: str, extension: str) -> str:
    """Append a confidently detected extension without hiding the old suffix."""

    normalized = extension.casefold()
    if not normalized or has_compatible_extension(filename, normalized):
        return filename
    return f"{filename}{normalized}"


def looks_like_html(data: bytes) -> bool:
    lowered = data.lstrip(b"\xef\xbb\xbf\x00 \t\r\n").lower()
    return lowered.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body", b"<title")
    )


def detect_file_type(
    path: str | Path,
    declared_mime_type: str = "",
    *,
    logical_filename: str = "",
) -> FileTypeDetection:
    """Detect common OLIS document types from bytes, MIME, then filename."""

    source = Path(path)
    try:
        with source.open("rb") as handle:
            first = handle.read(64 * 1024)
    except OSError:
        return FileTypeDetection("", "", "none", "File could not be read.")

    if first.startswith(b"%PDF-"):
        return FileTypeDetection(".pdf", "application/pdf", "strong", "PDF signature")
    if first.startswith(b"\xff\xd8\xff"):
        return FileTypeDetection(".jpg", "image/jpeg", "strong", "JPEG signature")
    if first.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileTypeDetection(".png", "image/png", "strong", "PNG signature")
    if first.startswith((b"GIF87a", b"GIF89a")):
        return FileTypeDetection(".gif", "image/gif", "strong", "GIF signature")
    if first.startswith((b"II*\x00", b"MM\x00*")):
        return FileTypeDetection(".tiff", "image/tiff", "strong", "TIFF signature")
    if first.startswith(b"BM"):
        return FileTypeDetection(".bmp", "image/bmp", "strong", "BMP signature")
    if first.startswith(b"RIFF") and first[8:12] == b"WEBP":
        return FileTypeDetection(".webp", "image/webp", "strong", "WebP RIFF signature")
    if first.startswith(b"{\\rtf"):
        return FileTypeDetection(".rtf", "application/rtf", "strong", "RTF signature")
    if first.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        declared_extension = extension_for_mime_type(declared_mime_type)
        suffix = Path(logical_filename).suffix.casefold()
        extension = declared_extension if declared_extension in {".doc", ".xls", ".ppt"} else suffix
        if extension not in {".doc", ".xls", ".ppt"}:
            extension = ".doc"
        mime_type = mimetypes.guess_type(f"file{extension}")[0] or "application/x-ole-storage"
        return FileTypeDetection(extension, mime_type, "strong", "OLE compound-document signature")
    if first.startswith(b"PK\x03\x04"):
        package = _detect_zip_package(source)
        if package is not None:
            return package
        return FileTypeDetection(".zip", "application/zip", "strong", "ZIP signature")

    textual = first.lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n").lower()
    if looks_like_html(first):
        return FileTypeDetection(".html", "text/html", "strong", "HTML document signature")
    if textual.startswith(b"<?xml"):
        return FileTypeDetection(".xml", "application/xml", "strong", "XML declaration")

    normalized_mime = normalize_mime_type(declared_mime_type)
    extension = extension_for_mime_type(normalized_mime)
    if extension:
        return FileTypeDetection(extension, normalized_mime, "declared", "HTTP Content-Type")

    suffix = Path(logical_filename or source.name).suffix.casefold()
    if suffix in KNOWN_EXTENSIONS:
        return FileTypeDetection(
            suffix,
            mimetypes.guess_type(f"file{suffix}")[0] or "",
            "filename",
            "Recognized filename extension",
        )
    return FileTypeDetection("", "", "none", "No recognized signature, MIME type, or extension")


def validate_file(
    path: str | Path,
    declared_mime_type: str = "",
    expected_length: int | None = None,
    *,
    expected_mime_type: str = "",
    expected_sha256: str = "",
    logical_filename: str = "",
    allow_html: bool = False,
) -> FileValidation:
    """Validate a completed transfer without trusting its name or headers."""

    source = Path(path)
    empty_detection = FileTypeDetection("", "", "none", "File is unavailable.")
    if source.is_symlink() or not source.is_file():
        return FileValidation("invalid", "Completed file is missing or is not a regular file.", empty_detection)
    try:
        size = source.stat().st_size
    except OSError:
        return FileValidation("invalid", "Completed file metadata could not be read.", empty_detection)
    if expected_length is not None:
        if expected_length < 0:
            raise ValueError("expected_length cannot be negative")
        if size != expected_length:
            return FileValidation(
                "invalid",
                f"Byte count {size} does not match the expected Content-Length {expected_length}.",
                empty_detection,
            )
    if size == 0:
        return FileValidation("invalid", "Downloaded file is empty.", empty_detection)

    detection = detect_file_type(
        source,
        declared_mime_type or expected_mime_type,
        logical_filename=logical_filename,
    )
    try:
        with source.open("rb") as handle:
            first = handle.read(64 * 1024)
            if size > 8192:
                handle.seek(-8192, 2)
            else:
                handle.seek(0)
            tail = handle.read(8192)
    except OSError:
        return FileValidation("invalid", "Completed file could not be read.", detection)

    if looks_like_html(first) or detection.extension == ".html":
        if not allow_html:
            return FileValidation(
                "invalid",
                "Response is HTML, not an archival document (possibly an access, consent, or error page).",
                detection,
            )

    # HTTP Content-Type and filename extensions are attacker-controlled hints,
    # not validation.  Phase 1 archives only formats for which we can recognize
    # a byte signature (including validated ZIP/OOXML packages).  This also
    # prevents arbitrary octet-stream data from becoming a completed record.
    if not detection.strong:
        return FileValidation(
            "invalid",
            "Downloaded bytes do not have a recognized archival document signature.",
            detection,
        )

    expected_extension = extension_for_mime_type(expected_mime_type)
    if expected_extension == ".pdf" and detection.extension != ".pdf":
        return FileValidation("invalid", "Expected a PDF, but its PDF signature is missing.", detection)
    if detection.extension == ".pdf":
        if not first.startswith(b"%PDF-"):
            return FileValidation("invalid", "PDF signature is missing.", detection)
        if b"%%EOF" not in tail:
            return FileValidation("invalid", "PDF end-of-file marker is missing.", detection)

    if detection.extension in {".doc", ".xls", ".ppt"} and not first.startswith(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    ):
        return FileValidation("invalid", "Legacy Office compound-document signature is missing.", detection)
    if detection.extension == ".rtf" and not first.startswith(b"{\\rtf"):
        return FileValidation("invalid", "RTF signature is missing.", detection)

    if detection.extension in {".zip", ".docx", ".xlsx", ".xlsm", ".pptx"}:
        valid, details, names = _validate_zip(source)
        if not valid:
            return FileValidation("invalid", details, detection)
        required = {
            ".docx": "word/document.xml",
            ".xlsx": "xl/workbook.xml",
            ".xlsm": "xl/workbook.xml",
            ".pptx": "ppt/presentation.xml",
        }.get(detection.extension)
        if required and required not in names:
            return FileValidation("invalid", f"Office package is missing {required}.", detection)

    expected_hash = expected_sha256.strip().casefold()
    if expected_hash:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("expected_sha256 must be a 64-character hexadecimal digest")
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            return FileValidation(
                "invalid",
                f"SHA-256 mismatch: expected {expected_hash}, received {actual_hash}.",
                detection,
            )

    expected_mime = normalize_mime_type(expected_mime_type)
    if (
        expected_mime not in GENERIC_BINARY_MIME_TYPES
        and detection.confidence == "none"
    ):
        return FileValidation("invalid", f"Could not verify expected MIME type {expected_mime}.", detection)

    if detection.extension in {".txt", ".csv", ".tsv", ".xml", ".html", ".htm"}:
        for encoding in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                first.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return FileValidation("invalid", "No plausible text encoding was found.", detection)

    mismatch = ""
    declared = normalize_mime_type(declared_mime_type)
    if detection.strong and declared not in GENERIC_BINARY_MIME_TYPES and declared != detection.mime_type:
        mismatch = f" Declared MIME {declared} differed from detected {detection.mime_type}."
    return FileValidation(
        "valid",
        f"Byte count and {detection.evidence.lower()} checks passed.{mismatch}".strip(),
        detection,
    )


validate_download = validate_file


def _detect_zip_package(source: Path) -> FileTypeDetection | None:
    valid, _details, names = _validate_zip(source, verify_crc=False)
    if not valid:
        return None
    if "word/document.xml" in names:
        return FileTypeDetection(
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "strong",
            "OOXML Word package contents",
        )
    if "xl/workbook.xml" in names:
        macro = "xl/vbaproject.bin" in names
        return FileTypeDetection(
            ".xlsm" if macro else ".xlsx",
            "application/vnd.ms-excel.sheet.macroenabled.12"
            if macro
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "strong",
            "OOXML spreadsheet package contents",
        )
    if "ppt/presentation.xml" in names:
        return FileTypeDetection(
            ".pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "strong",
            "OOXML presentation package contents",
        )
    return None


def _validate_zip(
    source: Path,
    *,
    verify_crc: bool = True,
) -> tuple[bool, str, set[str]]:
    """Apply metadata bounds before any full archive decompression."""

    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > ZIP_MAX_ENTRIES:
                return False, f"ZIP entry count exceeds the limit of {ZIP_MAX_ENTRIES}.", set()
            expanded = 0
            names: set[str] = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                parts = tuple(part for part in pure.parts if part not in {"", "."})
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    pure.is_absolute()
                    or re.match(r"^[A-Za-z]:", name)
                    or name.startswith("//")
                    or any(part == ".." for part in parts)
                    or (unix_mode and stat.S_ISLNK(unix_mode))
                ):
                    return False, "ZIP contains an unsafe path or symbolic-link entry.", set()
                expanded += int(info.file_size)
                if expanded > ZIP_MAX_EXPANDED_BYTES:
                    return False, "ZIP expanded-size limit would be exceeded.", set()
                if info.file_size and info.file_size / max(1, info.compress_size) > ZIP_MAX_COMPRESSION_RATIO:
                    return False, "ZIP compression-ratio limit would be exceeded.", set()
                names.add(name.casefold())
            if verify_crc:
                bad_member = archive.testzip()
                if bad_member:
                    return False, f"ZIP integrity check failed at member {bad_member!r}.", set()
            return True, "ZIP integrity checks passed.", names
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False, "File is not a readable ZIP/package.", set()


__all__ = [
    "EXTENSION_ALIASES",
    "FileTypeDetection",
    "FileValidation",
    "GENERIC_BINARY_MIME_TYPES",
    "KNOWN_EXTENSIONS",
    "MIME_EXTENSIONS",
    "detect_file_type",
    "extension_for_mime_type",
    "filename_with_extension",
    "has_compatible_extension",
    "has_recognized_extension",
    "looks_like_html",
    "normalize_mime_type",
    "normalized_media_type",
    "validate_download",
    "validate_file",
]
