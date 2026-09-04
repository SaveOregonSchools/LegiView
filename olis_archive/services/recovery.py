"""Filesystem-side recovery for interrupted or externally removed downloads."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Iterable

from .archive_paths import (
    UnsafeArchivePath,
    ensure_within_archive,
    part_path_for,
    require_owned_archive_root,
    resolve_stored_path,
    stored_relative_path,
)
from .downloads import DestinationConflict, atomic_promote_no_replace
from .file_types import FileValidation, validate_file
from .hashing import sha256_file


@dataclass(frozen=True, slots=True)
class RecoveryExpectation:
    relative_path: str
    expected_bytes: int | None = None
    expected_sha256: str = ""
    mime_type: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    action: str
    relative_path: str
    path: Path
    part_path: Path
    details: str
    byte_count: int = 0
    sha256: str = ""
    validation: FileValidation | None = None

    @property
    def adopted(self) -> bool:
        return self.action in {"adopted_final", "adopted_part"}

    @property
    def needs_retry(self) -> bool:
        return self.action in {"missing", "discarded_part", "invalid_final", "conflict"}


def validate_completed_download(
    archive_root: str | Path,
    relative_path: str | Path,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str = "",
    mime_type: str = "",
) -> RecoveryResult:
    """Revalidate the exact local expectations used by an idempotent skip."""

    root = _safe_recovery_root(archive_root)
    final = resolve_stored_path(root, relative_path)
    part = part_path_for(final)
    relative = stored_relative_path(root, final)
    if not final.is_file() or final.is_symlink():
        return RecoveryResult(
            "missing",
            relative,
            final,
            part,
            "Completed record has no regular local file.",
        )
    validation = validate_file(
        final,
        mime_type,
        expected_bytes,
        expected_mime_type=mime_type,
        expected_sha256=expected_sha256,
        logical_filename=final.name,
    )
    if not validation.valid:
        return RecoveryResult(
            "invalid_final",
            relative,
            final,
            part,
            validation.details,
            final.stat().st_size,
            "",
            validation,
        )
    digest = expected_sha256.casefold() or sha256_file(final)
    return RecoveryResult(
        "adopted_final",
        relative,
        final,
        part,
        "Existing completed file passed byte, type, and SHA-256 expectations.",
        final.stat().st_size,
        digest,
        validation,
    )


def recover_incomplete_download(
    archive_root: str | Path,
    relative_path: str | Path,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str = "",
    mime_type: str = "",
    adopt_complete_part: bool = True,
) -> RecoveryResult:
    """Adopt provably complete bytes; otherwise remove only the registered part."""

    root = _safe_recovery_root(archive_root)
    final = resolve_stored_path(root, relative_path)
    # Keep the lexical .part path.  Resolving the file itself would follow an
    # attacker-created symlink and could make cleanup unlink its target.  The
    # already-resolved parent is inside the archive and no path component is
    # supplied by an untrusted filename here.
    ensure_within_archive(root, final.parent)
    part = part_path_for(final)
    relative = stored_relative_path(root, final)

    if final.exists():
        completed = validate_completed_download(
            root,
            relative,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            mime_type=mime_type,
        )
        if completed.adopted:
            if part.exists():
                _unlink_part(part)
            return completed
        return RecoveryResult(
            "conflict" if part.exists() else "invalid_final",
            relative,
            final,
            part,
            "An invalid or unexpected final file is present; it was not overwritten or deleted.",
            completed.byte_count,
            completed.sha256,
            completed.validation,
        )

    if not part.exists():
        return RecoveryResult(
            "missing",
            relative,
            final,
            part,
            "Neither a completed file nor its staged .part file exists.",
        )
    if part.is_symlink() or not part.is_file():
        _unlink_part(part)
        return RecoveryResult(
            "discarded_part",
            relative,
            final,
            part,
            "Unsafe staged path was removed; download must be retried.",
        )
    if not adopt_complete_part or (expected_bytes is None and not expected_sha256):
        _unlink_part(part)
        return RecoveryResult(
            "discarded_part",
            relative,
            final,
            part,
            "Staged bytes lacked a durable length or hash expectation and were safely discarded.",
        )

    validation = validate_file(
        part,
        mime_type,
        expected_bytes,
        expected_mime_type=mime_type,
        expected_sha256=expected_sha256,
        logical_filename=final.name,
    )
    if not validation.valid:
        byte_count = part.stat().st_size
        _unlink_part(part)
        return RecoveryResult(
            "discarded_part",
            relative,
            final,
            part,
            f"Incomplete staged bytes were removed: {validation.details}",
            byte_count,
            "",
            validation,
        )

    digest = expected_sha256.casefold() or sha256_file(part)
    byte_count = part.stat().st_size
    try:
        atomic_promote_no_replace(part, final)
    except DestinationConflict:
        return RecoveryResult(
            "conflict",
            relative,
            final,
            part,
            "A final file appeared during recovery; neither copy was overwritten.",
            byte_count,
            digest,
            validation,
        )
    return RecoveryResult(
        "adopted_part",
        relative,
        final,
        part,
        "Complete staged bytes were validated and atomically adopted.",
        byte_count,
        digest,
        validation,
    )


def recover_interrupted_downloads(
    archive_root: str | Path,
    expectations: Iterable[RecoveryExpectation],
) -> list[RecoveryResult]:
    return [
        recover_incomplete_download(
            archive_root,
            item.relative_path,
            expected_bytes=item.expected_bytes,
            expected_sha256=item.expected_sha256,
            mime_type=item.mime_type,
        )
        for item in expectations
    ]


def cleanup_stale_parts(
    archive_root: str | Path,
    *,
    keep_relative_paths: Iterable[str] = (),
    older_than_seconds: float = 0,
) -> list[str]:
    """Remove unregistered ``.part`` files beneath one explicit archive root.

    The function never follows directory links and never recursively removes
    directories.  Callers must stop workers first or pass every active part path
    in ``keep_relative_paths``.
    """

    # Recursive maintenance is authorized only for an explicitly marked,
    # dedicated LegiView archive. Targeted recovery above remains constrained
    # to database-registered relative paths and does not need broad ownership.
    root = require_owned_archive_root(archive_root)
    keep = {
        stored_relative_path(root, resolve_stored_path(root, relative))
        for relative in keep_relative_paths
    }
    removed: list[str] = []
    cutoff = time.time() - max(0.0, older_than_seconds)
    for current_root, directories, files in os.walk(root, followlinks=False):
        directory = Path(current_root)
        directories[:] = [
            name
            for name in directories
            if not _is_directory_link(directory / name)
        ]
        for name in files:
            if not name.endswith(".part"):
                continue
            # Do not resolve a file-level symlink: unlinking the resolved path
            # could remove its target rather than the staged link itself.
            safe_directory = ensure_within_archive(root, directory)
            part = safe_directory / name
            relative = part.relative_to(root).as_posix()
            if relative in keep:
                continue
            try:
                if older_than_seconds > 0 and part.lstat().st_mtime > cutoff:
                    continue
                part.unlink()
                removed.append(relative)
            except OSError:
                continue
    return removed


def _safe_recovery_root(archive_root: str | Path) -> Path:
    raw = Path(archive_root).expanduser()
    root = raw.resolve(strict=False)
    if root == Path(root.anchor):
        raise UnsafeArchivePath("Recovery cannot operate on a filesystem root.")
    if not root.exists() or not root.is_dir() or _is_directory_link(raw):
        raise UnsafeArchivePath("Recovery requires an existing, non-linked archive root.")
    return root


def _unlink_part(part: Path) -> None:
    try:
        part.unlink(missing_ok=True)
    except OSError as exc:
        raise UnsafeArchivePath(f"Could not remove staged path safely: {part}") from exc


def _is_directory_link(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


__all__ = [
    "RecoveryExpectation",
    "RecoveryResult",
    "cleanup_stale_parts",
    "recover_incomplete_download",
    "recover_interrupted_downloads",
    "validate_completed_download",
]
