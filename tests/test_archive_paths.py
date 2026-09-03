from __future__ import annotations

from pathlib import Path

import pytest

from olis_archive.services.archive_paths import (
    UnsafeArchivePath,
    archive_document_path,
    collision_safe_destination,
    normalize_bill_id,
    relative_document_directory,
    resolve_stored_path,
    sanitize_windows_filename,
    stored_relative_path,
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
