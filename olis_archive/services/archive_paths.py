"""Deterministic, Windows-safe paths for the local OLIS document archive."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata


DOCUMENT_KINDS = frozenset(
    {
        "public_testimony",
        "legacy_testimony",
        "committee_presentation",
        "floor_letter",
        "committee_document_other",
    }
)

WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)

_SESSION_KEY = re.compile(r"^\d{4}[A-Z][A-Z0-9]*$")
_BILL_ID = re.compile(r"^[A-Z]{1,5}\d+[A-Z0-9]*$")


class UnsafeArchivePath(ValueError):
    """A requested path could escape or alias an unsafe archive location."""


class ArchivePathCollision(FileExistsError):
    """A deterministic destination is already occupied by unrelated bytes."""


def sanitize_windows_filename(
    name: str | None,
    *,
    fallback: str = "document",
    max_length: int = 180,
) -> str:
    """Return one safe filename component while preserving useful source text."""

    if max_length < 16:
        raise ValueError("max_length must be at least 16")
    safe_fallback = unicodedata.normalize("NFKC", str(fallback or "document").strip())
    safe_fallback = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", safe_fallback).rstrip(" .")
    safe_fallback = safe_fallback if safe_fallback not in {"", ".", ".."} else "document"
    value = unicodedata.normalize("NFKC", str(name or "").strip())
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value).rstrip(" .")
    if value in {"", ".", ".."}:
        value = safe_fallback
    suffix = Path(value).suffix
    if _is_windows_reserved_name(value):
        value = f"_{value}"
        suffix = Path(value).suffix
    if len(value) > max_length:
        suffix = suffix[:20]
        stem_length = max(1, max_length - len(suffix))
        value = f"{Path(value).stem[:stem_length].rstrip(' .')}{suffix}"
    value = value.rstrip(" .")
    if not value or _is_windows_reserved_name(value):
        value = f"_{safe_fallback}"
    return value


sanitize_filename = sanitize_windows_filename


def normalize_session_key(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if not _SESSION_KEY.fullmatch(normalized):
        raise UnsafeArchivePath(f"Invalid official session key: {value!r}")
    return normalized


def normalize_bill_id(value: str) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).upper()
    if not _BILL_ID.fullmatch(normalized):
        raise UnsafeArchivePath(f"Invalid compact bill ID: {value!r}")
    return normalized


def normalize_document_kind(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in DOCUMENT_KINDS:
        raise UnsafeArchivePath(f"Unsupported document kind: {value!r}")
    return normalized


def normalize_source_id(value: int | str) -> str:
    normalized = str(value).strip()
    if not normalized.isascii() or not normalized.isdigit() or int(normalized) < 0:
        raise UnsafeArchivePath(f"Source document ID must be numeric: {value!r}")
    return str(int(normalized))


def relative_document_directory(
    session_key: str,
    bill_id: str,
    document_kind: str,
    source_id: int | str,
) -> Path:
    return Path(
        normalize_session_key(session_key),
        normalize_bill_id(bill_id),
        normalize_document_kind(document_kind),
        normalize_source_id(source_id),
    )


def versioned_filename(filename: str, version_number: int = 1) -> str:
    """Keep the first source name and give later payloads stable version names."""

    if version_number < 1:
        raise ValueError("version_number must be positive")
    safe_name = sanitize_windows_filename(filename)
    if version_number == 1:
        return safe_name
    path = Path(safe_name)
    return f"{path.stem}__v{version_number:04d}{path.suffix}"


def archive_document_path(
    archive_root: str | Path,
    session_key: str,
    bill_id: str,
    document_kind: str,
    source_id: int | str,
    filename: str,
    *,
    version_number: int = 1,
    create_directory: bool = False,
) -> Path:
    root = Path(archive_root).expanduser().resolve(strict=False)
    relative_dir = relative_document_directory(session_key, bill_id, document_kind, source_id)
    if create_directory:
        destination_dir = ensure_archive_directory(root, relative_dir)
    else:
        destination_dir = ensure_within_archive(root, root / relative_dir)
    return ensure_within_archive(root, destination_dir / versioned_filename(filename, version_number))


def stored_relative_path(archive_root: str | Path, absolute_path: str | Path) -> str:
    root = Path(archive_root).expanduser().resolve(strict=False)
    path = ensure_within_archive(root, Path(absolute_path).expanduser())
    return path.relative_to(root).as_posix()


def resolve_stored_path(archive_root: str | Path, relative_path: str | Path) -> Path:
    root = Path(archive_root).expanduser().resolve(strict=False)
    raw = str(relative_path).replace("\\", "/")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or raw.startswith("//")
        or re.match(r"^[A-Za-z]:", raw)
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise UnsafeArchivePath("Stored archive path must be a non-traversing relative path.")
    return ensure_within_archive(root, root.joinpath(*pure.parts))


def ensure_within_archive(archive_root: str | Path, candidate: str | Path) -> Path:
    root = Path(archive_root).expanduser().resolve(strict=False)
    path = Path(candidate).expanduser().resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise UnsafeArchivePath(f"Path escapes the configured archive root: {path}") from exc
    return path


def ensure_archive_directory(archive_root: str | Path, relative_directory: str | Path) -> Path:
    """Create a directory tree without following an existing reparse point."""

    raw_root = Path(archive_root).expanduser()
    if raw_root.exists() and _is_link_or_reparse_point(raw_root):
        raise UnsafeArchivePath("Archive root must not be a link or reparse point.")
    root = raw_root.resolve(strict=False)
    relative = Path(relative_directory)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeArchivePath("Archive directory must be relative and non-traversing.")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or _is_link_or_reparse_point(root):
        raise UnsafeArchivePath("Archive root must be a real directory, not a link or reparse point.")
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise UnsafeArchivePath("Archive directory contains an unsafe component.")
        current = current / part
        if current.exists():
            if not current.is_dir() or _is_link_or_reparse_point(current):
                raise UnsafeArchivePath(f"Archive directory component is unsafe: {current}")
        else:
            current.mkdir()
        ensure_within_archive(root, current)
    return current


def collision_safe_destination(directory: str | Path, filename: str, *, limit: int = 10_000) -> Path:
    """Choose a deterministic non-overwriting filename in an existing directory."""

    raw_root = Path(directory)
    if raw_root.exists() and _is_link_or_reparse_point(raw_root):
        raise UnsafeArchivePath(f"Destination is a link or reparse point: {raw_root}")
    root = raw_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or _is_link_or_reparse_point(root):
        raise UnsafeArchivePath(f"Destination is not a safe directory: {root}")
    safe_name = sanitize_windows_filename(filename)
    direct = root / safe_name
    if not direct.exists() and not part_path_for(direct).exists():
        return direct
    name = Path(safe_name)
    for version in range(2, limit + 1):
        candidate = root / f"{name.stem}__v{version:04d}{name.suffix}"
        if not candidate.exists() and not part_path_for(candidate).exists():
            return candidate
    raise ArchivePathCollision(f"No collision-free destination was available for {safe_name!r}.")


def part_path_for(destination: str | Path) -> Path:
    path = Path(destination)
    return path.with_name(f"{path.name}.part")


def _is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except OSError:
        return False


def _is_windows_reserved_name(value: str) -> bool:
    # Windows reserves these device basenames even when an extension (or more
    # than one extension) is present.  NFKC above also converts superscript
    # digits, which Windows treats as digits in names such as COM¹ and LPT¹.
    basename = value.split(".", 1)[0].rstrip(" .").casefold()
    return basename in WINDOWS_RESERVED_NAMES


__all__ = [
    "ArchivePathCollision",
    "DOCUMENT_KINDS",
    "UnsafeArchivePath",
    "archive_document_path",
    "collision_safe_destination",
    "ensure_archive_directory",
    "ensure_within_archive",
    "normalize_bill_id",
    "normalize_document_kind",
    "normalize_session_key",
    "normalize_source_id",
    "part_path_for",
    "relative_document_directory",
    "resolve_stored_path",
    "sanitize_filename",
    "sanitize_windows_filename",
    "stored_relative_path",
    "versioned_filename",
]
