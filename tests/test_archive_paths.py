from __future__ import annotations

from pathlib import Path

import pytest

from olis_archive.services.archive_paths import (
    ARCHIVE_OWNERSHIP_MARKER,
    UnsafeArchivePath,
    archive_document_path,
    collision_safe_destination,
    ensure_archive_root_owned,
    normalize_bill_id,
    relative_document_directory,
    resolve_stored_path,
    sanitize_windows_filename,
    stored_relative_path,
    validate_archive_root_candidate,
    versioned_filename,
)


def test_windows_filename_sanitization_blocks_devices_separators_and_trailing_dots():
    assert sanitize_windows_filename("CON.txt") == "_CON.txt"
    assert sanitize_windows_filename("con.tar.gz") == "_con.tar.gz"
    sanitized = sanitize_windows_filename('../../bad<name>?\\report.pdf. ')
    assert "/" not in sanitized and "\\" not in sanitized
    assert "<" not in sanitized and ">" not in sanitized and "?" not in sanitized
    assert not sanitized.endswith((".", " "))
    assert sanitize_windows_filename("", fallback="document-244133.pdf") == "document-244133.pdf"


def test_archive_path_uses_official_hierarchy_and_stored_forward_slashes(tmp_path: Path):
    path = archive_document_path(
        tmp_path,
        "2026r1",
        "SB 1501",
        "public_testimony",
        "00244133",
        "Testimony.pdf",
        create_directory=True,
    )

    assert path == tmp_path / "2026R1" / "SB1501" / "public_testimony" / "244133" / "Testimony.pdf"
    assert path.parent.is_dir()
    assert stored_relative_path(tmp_path, path) == "2026R1/SB1501/public_testimony/244133/Testimony.pdf"
    assert resolve_stored_path(tmp_path, stored_relative_path(tmp_path, path)) == path


def test_archive_components_reject_traversal_unknown_kinds_and_non_numeric_source_ids(tmp_path: Path):
    with pytest.raises(UnsafeArchivePath):
        relative_document_directory("../../2026R1", "SB1501", "public_testimony", 1)
    with pytest.raises(UnsafeArchivePath):
        relative_document_directory("2026R1", "SB1501", "unknown", 1)
    with pytest.raises(UnsafeArchivePath):
        relative_document_directory("2026R1", "SB1501", "public_testimony", "../../1")
    with pytest.raises(UnsafeArchivePath):
        resolve_stored_path(tmp_path, "../outside.pdf")
    with pytest.raises(UnsafeArchivePath):
        resolve_stored_path(tmp_path, "C:/outside.pdf")


def test_bill_normalization_and_version_names_are_stable():
    assert normalize_bill_id(" hb 4100 ") == "HB4100"
    assert versioned_filename("Evidence.pdf", 1) == "Evidence.pdf"
    assert versioned_filename("Evidence.pdf", 2) == "Evidence__v0002.pdf"


def test_collision_paths_are_deterministic_and_notice_part_files(tmp_path: Path):
    direct = collision_safe_destination(tmp_path, "Evidence.pdf")
    assert direct == tmp_path / "Evidence.pdf"
    direct.write_bytes(b"first")

    second = collision_safe_destination(tmp_path, "Evidence.pdf")
    assert second == tmp_path / "Evidence__v0002.pdf"
    second.with_name(f"{second.name}.part").write_bytes(b"in progress")

    third = collision_safe_destination(tmp_path, "Evidence.pdf")
    assert third == tmp_path / "Evidence__v0003.pdf"


def test_empty_archive_candidate_is_validated_without_mutation_then_marked(tmp_path: Path):
    archive = tmp_path / "archive"
    archive.mkdir()

    assert validate_archive_root_candidate(archive) == archive
    assert list(archive.iterdir()) == []

    assert ensure_archive_root_owned(archive) == archive
    marker = archive / ARCHIVE_OWNERSHIP_MARKER
    assert marker.is_file()
    assert ensure_archive_root_owned(archive) == archive


def test_recognizable_legacy_archive_tree_can_be_adopted(tmp_path: Path):
    archive = tmp_path / "archive"
    staged = (
        archive
        / "2026R1"
        / "SB1501"
        / "public_testimony"
        / "244133"
        / "Evidence.pdf.part"
    )
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"incomplete")
    (archive / "2026R1" / "SB1501" / "public_testimony" / "target").mkdir()

    assert validate_archive_root_candidate(archive) == archive
    ensure_archive_root_owned(archive)

    assert staged.exists()
    assert (archive / ARCHIVE_OWNERSHIP_MARKER).is_file()


def test_nonempty_unowned_root_and_invalid_marker_are_rejected(tmp_path: Path):
    unowned = tmp_path / "Documents"
    unowned.mkdir()
    unrelated = unowned / "unrelated.part"
    unrelated.write_bytes(b"personal data")

    with pytest.raises(UnsafeArchivePath, match="not owned by LegiView"):
        ensure_archive_root_owned(unowned)
    assert unrelated.read_bytes() == b"personal data"
    assert not (unowned / ARCHIVE_OWNERSHIP_MARKER).exists()

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / ARCHIVE_OWNERSHIP_MARKER).write_text("not a LegiView marker")
    with pytest.raises(UnsafeArchivePath, match="marker is invalid"):
        validate_archive_root_candidate(invalid)


def test_empty_archive_shaped_tree_is_not_enough_for_legacy_adoption(tmp_path: Path):
    lookalike = (
        tmp_path
        / "lookalike"
        / "2026R1"
        / "SB1501"
        / "public_testimony"
        / "244133"
    )
    lookalike.mkdir(parents=True)
    root = tmp_path / "lookalike"

    with pytest.raises(UnsafeArchivePath, match="not owned by LegiView"):
        ensure_archive_root_owned(root)

    assert not (root / ARCHIVE_OWNERSHIP_MARKER).exists()


def test_archive_ownership_marker_symlink_is_rejected(tmp_path: Path):
    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "outside-marker"
    outside.write_text("not trusted")
    marker = archive / ARCHIVE_OWNERSHIP_MARKER
    try:
        marker.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks are not available for this test user")

    with pytest.raises(UnsafeArchivePath, match="must not be a link"):
        validate_archive_root_candidate(archive)


def test_archive_root_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-archive"
    try:
        linked_root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not available for this test user")

    with pytest.raises(UnsafeArchivePath, match="root must not be a link"):
        validate_archive_root_candidate(linked_root)
