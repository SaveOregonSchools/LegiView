from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from olis_archive.config import (
    AppConfig,
    BYTES_PER_GB,
    DEFAULT_USER_AGENT,
    PROJECT_ROOT,
)
from olis_archive.services.collection import CollectionService
from olis_archive.services.odata import ODataClient
from olis_archive.services.olis_http import OLISHTTPClient
from olis_archive.runtime import load_effective_config


CONFIG_ENV_NAMES = (
    "LEGIVIEW_PROJECT_ROOT",
    "LEGIVIEW_DATABASE_PATH",
    "LEGIVIEW_ARCHIVE_ROOT",
    "LEGIVIEW_REQUEST_TIMEOUT",
    "LEGIVIEW_ODATA_WORKERS",
    "LEGIVIEW_DOWNLOAD_WORKERS",
    "LEGIVIEW_HTML_CONCURRENCY",
    "LEGIVIEW_MIN_FREE_SPACE_GB",
    "LEGIVIEW_MIN_FREE_SPACE_BYTES",
    "LEGIVIEW_INTER_REQUEST_DELAY",
    "LEGIVIEW_ODATA_BASE_URL",
    "LEGIVIEW_OLIS_BASE_URL",
    "LEGIVIEW_HOST",
    "LEGIVIEW_PORT",
    "LEGIVIEW_DEBUG",
)


@pytest.fixture(autouse=True)
def _clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_default_paths_use_application_project_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = AppConfig.from_env(tmp_path / "missing.env")

    assert config.project_root == PROJECT_ROOT
    assert config.database_path == PROJECT_ROOT / "data" / "legiview.sqlite3"
    assert config.database_path_configured == "data/legiview.sqlite3"
    assert config.archive_root == PROJECT_ROOT / "archive"
    assert config.archive_root_configured == "archive"
    assert config.minimum_free_space_gb == 5
    assert config.minimum_free_space_bytes == 5 * BYTES_PER_GB
    assert config.snapshot()["project_root"] == str(PROJECT_ROOT)


def test_relative_paths_resolve_against_effective_project_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated_cwd = tmp_path / "caller"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setenv("LEGIVIEW_PROJECT_ROOT", "portable-root")
    monkeypatch.setenv("LEGIVIEW_DATABASE_PATH", "state/legiview.sqlite3")
    monkeypatch.setenv("LEGIVIEW_ARCHIVE_ROOT", "storage/archive")

    config = AppConfig.from_env(tmp_path / "missing.env")

    expected_root = (PROJECT_ROOT / "portable-root").resolve(strict=False)
    assert config.project_root == expected_root
    assert config.database_path == expected_root / "state" / "legiview.sqlite3"
    assert config.archive_root == expected_root / "storage" / "archive"
    assert config.archive_root_configured == "storage/archive"
    assert unrelated_cwd not in config.database_path.parents


def test_relative_project_root_uses_loaded_env_file_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_directory = tmp_path / "configuration"
    env_directory.mkdir()
    env_file = env_directory / ".env"
    env_file.write_text(
        "LEGIVIEW_PROJECT_ROOT=workspace\n"
        "LEGIVIEW_DATABASE_PATH=data/app.sqlite3\n"
        "LEGIVIEW_ARCHIVE_ROOT=archive\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)

    try:
        config = AppConfig.from_env(env_file)
    finally:
        # _load_simple_env intentionally populates os.environ for application
        # values outside AppConfig (for example the Flask secret).
        for name in CONFIG_ENV_NAMES:
            os.environ.pop(name, None)

    expected_root = env_directory / "workspace"
    assert config.project_root == expected_root
    assert config.database_path == expected_root / "data" / "app.sqlite3"
    assert config.archive_root == expected_root / "archive"


def test_project_root_override_rebases_configured_relative_paths(tmp_path: Path) -> None:
    original_root = tmp_path / "original"
    replacement_root = tmp_path / "replacement"
    config = AppConfig(
        project_root=original_root,
        database_path="data/legiview.sqlite3",
        archive_root="archive",
    )

    effective = config.with_project_root(replacement_root)

    assert effective.project_root == replacement_root
    assert effective.database_path == replacement_root / "data" / "legiview.sqlite3"
    assert effective.database_path_configured == "data/legiview.sqlite3"
    assert effective.archive_root == replacement_root / "archive"


def test_runtime_database_override_replaces_retained_configured_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    base = AppConfig(
        project_root=root,
        database_path="data/default.sqlite3",
        archive_root="archive",
        minimum_free_space_bytes=0,
    )

    effective, database, _storage = load_effective_config(
        base,
        overrides={"database_path": "state/override.sqlite3"},
    )

    assert effective.database_path == root / "state" / "override.sqlite3"
    assert effective.database_path_configured == "state/override.sqlite3"
    assert Path(database.path) == effective.database_path


def test_absolute_database_archive_and_project_paths_remain_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    database_path = tmp_path / "database" / "legiview.sqlite3"
    archive_root = tmp_path / "payloads"
    monkeypatch.setenv("LEGIVIEW_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("LEGIVIEW_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("LEGIVIEW_ARCHIVE_ROOT", str(archive_root))

    config = AppConfig.from_env(tmp_path / "missing.env")

    assert config.project_root == project_root
    assert config.database_path == database_path
    assert config.archive_root == archive_root
    assert config.archive_root_configured == str(archive_root)


def test_new_gb_setting_precedes_legacy_bytes_and_accepts_fractions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGIVIEW_MIN_FREE_SPACE_BYTES", str(2 * BYTES_PER_GB))
    monkeypatch.setenv("LEGIVIEW_MIN_FREE_SPACE_GB", "5.5")

    config = AppConfig.from_env(tmp_path / "missing.env")

    assert config.minimum_free_space_gb == 5.5
    assert config.minimum_free_space_bytes == int(5.5 * BYTES_PER_GB)


def test_legacy_environment_bytes_are_converted_with_deprecation_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LEGIVIEW_MIN_FREE_SPACE_BYTES", str(2 * BYTES_PER_GB))

    with caplog.at_level(logging.WARNING, logger="olis_archive.config"):
        config = AppConfig.from_env(tmp_path / "missing.env")

    assert config.minimum_free_space_gb == 2
    assert config.minimum_free_space_bytes == 2 * BYTES_PER_GB
    assert "deprecated minimum_free_space_bytes" in caplog.text


def test_legacy_persisted_bytes_are_converted_when_new_setting_is_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = AppConfig(
        project_root=tmp_path,
        database_path="data/legiview.sqlite3",
        archive_root="archive",
    )

    with caplog.at_level(logging.WARNING, logger="olis_archive.config"):
        effective = base.with_settings(
            {"minimum_free_space_bytes": int(1.25 * BYTES_PER_GB)}
        )

    assert effective.minimum_free_space_gb == 1.25
    assert effective.minimum_free_space_bytes == int(1.25 * BYTES_PER_GB)
    assert "Persisted application settings" in caplog.text


def test_relative_archive_setting_stays_portable_and_resolves_for_runtime(
    tmp_path: Path,
) -> None:
    base = AppConfig(
        project_root=tmp_path,
        database_path="data/legiview.sqlite3",
        archive_root="archive",
    )

    effective = base.with_settings({"archive_root": "storage/archive"})

    assert effective.archive_root_configured == "storage/archive"
    assert effective.archive_root == tmp_path / "storage" / "archive"


def test_derived_byte_floor_and_legiview_user_agent_reach_network_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        project_root=tmp_path,
        database_path="data/legiview.sqlite3",
        archive_root="archive",
        minimum_free_space_gb=0.5,
    )

    # This is a configuration-wiring test; migration behavior has dedicated
    # coverage elsewhere and no database access is needed to build the clients.
    monkeypatch.setattr("olis_archive.services.collection.Database.initialize", lambda _self: None)
    collection = CollectionService(config)

    assert collection.downloader.minimum_free_space_bytes == BYTES_PER_GB // 2
    assert collection.odata.user_agent == DEFAULT_USER_AGENT
    assert collection.olis_http.user_agent == DEFAULT_USER_AGENT
    assert ODataClient().user_agent.startswith("LegiView/")
    assert OLISHTTPClient().user_agent.startswith("LegiView/")
    assert "saveoregonschools.com" in DEFAULT_USER_AGENT
