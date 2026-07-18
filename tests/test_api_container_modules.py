from __future__ import annotations

import pytest

from app.api.container import build_container
from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub
from app.services.scheduler import LocalDataScheduler
from tests.factories import make_quote


def test_container_reuses_datahub_workbench_context_cache(tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "cache.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)

    container = build_container(settings=settings, cache=cache)

    assert container.workbench_contexts is container.datahub.workbench_contexts
    assert container.settings is settings
    assert container.datahub.settings is settings
    assert container.datahub.cache is cache
    assert cache.watchlist_repo.settings is settings
    assert cache.advice_repo.settings is settings
    assert cache.maintenance_repo.settings is settings


def test_explicit_cache_path_does_not_read_global_settings(monkeypatch, tmp_path) -> None:
    def fail_settings():
        raise AssertionError("不应读取全局 Settings")

    monkeypatch.setattr("app.services.cache.get_settings", fail_settings)

    cache = SQLiteCache(tmp_path / "isolated.sqlite3")

    assert cache.path == (tmp_path / "isolated.sqlite3").resolve()
    assert cache.settings is None
    assert cache.watchlist_repo.settings is cache.advice_repo.settings
    assert cache.advice_repo.settings is cache.maintenance_repo.settings


def test_build_container_binds_path_only_cache_repositories_to_settings(tmp_path) -> None:
    settings = Settings(
        cache_path=tmp_path / "path-only.sqlite3",
        quote_cache_seconds=27,
        advice_history_dedupe_seconds=43,
        scheduler_enabled=False,
    )
    cache = SQLiteCache(settings.cache_path)

    container = build_container(settings=settings, cache=cache)

    assert container.settings is settings
    assert cache.settings is settings
    assert cache.watchlist_repo.settings is settings
    assert cache.advice_repo.settings is settings
    assert cache.maintenance_repo.settings is settings


def test_datahub_direct_injection_binds_settings_and_applies_repository_ttl(tmp_path) -> None:
    settings = Settings(
        cache_path=tmp_path / "direct.sqlite3",
        quote_cache_seconds=0,
        scheduler_enabled=False,
    )
    cache = SQLiteCache(settings.cache_path)
    quote = make_quote()

    hub = DataHub(cache=cache, settings=settings)
    cache.save_watchlist_item(quote)
    cache.save_quotes([quote])

    assert hub.settings is settings
    assert cache.settings is settings
    assert cache.watchlist_repo.settings is settings
    assert cache.advice_repo.settings is settings
    assert cache.maintenance_repo.settings is settings
    assert cache.watchlist_item("600519.SH").latest_price is None


def test_datahub_path_cache_derives_global_settings_with_matching_path(monkeypatch, tmp_path) -> None:
    global_settings = Settings(
        cache_path=tmp_path / "global.sqlite3",
        quote_cache_seconds=31,
        scheduler_enabled=False,
    )
    cache = SQLiteCache(tmp_path / "injected.sqlite3")
    monkeypatch.setattr("app.services.cache.get_settings", lambda: global_settings)

    hub = DataHub(cache=cache)

    assert hub.settings.cache_path == cache.path
    assert hub.settings.quote_cache_seconds == global_settings.quote_cache_seconds
    assert cache.settings is hub.settings
    assert cache.watchlist_repo.settings is hub.settings
    assert cache.advice_repo.settings is hub.settings
    assert cache.maintenance_repo.settings is hub.settings


def test_datahub_direct_injection_rejects_configuration_and_path_conflicts(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    cache_settings = Settings(cache_path=path, quote_cache_seconds=8, scheduler_enabled=False)
    cache = SQLiteCache(settings=cache_settings)
    conflicting_settings = Settings(cache_path=path, quote_cache_seconds=9, scheduler_enabled=False)

    with pytest.raises(ValueError, match=r"cache\.settings.*配置不一致"):
        DataHub(cache=cache, settings=conflicting_settings)

    path_only_cache = SQLiteCache(tmp_path / "cache.sqlite3")
    another_path_settings = Settings(cache_path=tmp_path / "other.sqlite3", scheduler_enabled=False)
    with pytest.raises(ValueError, match=r"cache\.path.*Settings\.cache_path"):
        DataHub(cache=path_only_cache, settings=another_path_settings)


def test_build_container_uses_injected_settings_without_global_lookup(monkeypatch, tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "isolated.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)

    def fail_settings():
        raise AssertionError("不应读取全局 Settings")

    monkeypatch.setattr("app.api.container.get_settings", fail_settings)
    monkeypatch.setattr("app.services.cache.get_settings", fail_settings)

    container = build_container(settings=settings, cache=cache)

    assert container.settings is settings
    assert container.datahub.cache.path == settings.cache_path


def test_build_container_infers_settings_from_injected_cache(monkeypatch, tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "cache-owned.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=settings)

    def fail_settings():
        raise AssertionError("不应读取全局 Settings")

    monkeypatch.setattr("app.api.container.get_settings", fail_settings)
    monkeypatch.setattr("app.services.cache.get_settings", fail_settings)

    container = build_container(cache=cache)

    assert container.settings is settings
    assert container.datahub.settings is settings
    assert container.datahub.cache is cache


def test_build_container_rebinds_equivalent_cache_settings_to_single_owner(tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "shared.sqlite3", scheduler_enabled=False)
    equivalent_settings = Settings(cache_path=tmp_path / "shared.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=equivalent_settings)

    container = build_container(settings=settings, cache=cache)

    assert container.settings is settings
    assert container.datahub.settings is settings
    assert cache.settings is settings
    assert cache.watchlist_repo.settings is settings
    assert cache.advice_repo.settings is settings
    assert cache.maintenance_repo.settings is settings


def test_build_container_rejects_cache_configuration_conflict(tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "container.sqlite3", scheduler_enabled=False)
    cache_settings = Settings(cache_path=tmp_path / "cache.sqlite3", scheduler_enabled=False)
    cache = SQLiteCache(settings=cache_settings)

    with pytest.raises(ValueError, match=r"cache\.path.*Settings\.cache_path"):
        build_container(settings=settings, cache=cache)


def test_build_container_rejects_injected_datahub_with_another_settings_owner(tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "same.sqlite3", scheduler_enabled=False)
    separate_owner = Settings(cache_path=tmp_path / "same.sqlite3", scheduler_enabled=False)
    datahub = DataHub(settings=separate_owner)

    with pytest.raises(ValueError, match=r"datahub\.settings.*同一 Settings 实例"):
        build_container(settings=settings, datahub=datahub)


def test_build_container_rejects_scheduler_bound_to_conflicting_settings(tmp_path) -> None:
    scheduler_settings = Settings(cache_path=tmp_path / "scheduler.sqlite3", scheduler_enabled=False)
    explicit_settings = Settings(cache_path=tmp_path / "container.sqlite3", scheduler_enabled=False)
    scheduler_datahub = DataHub(settings=scheduler_settings)
    scheduler = LocalDataScheduler(scheduler_datahub)

    with pytest.raises(ValueError, match=r"datahub\.settings.*同一 Settings 实例"):
        build_container(settings=explicit_settings, scheduler=scheduler)


def test_build_container_infers_datahub_from_injected_scheduler(tmp_path) -> None:
    settings = Settings(cache_path=tmp_path / "scheduler-owned.sqlite3", scheduler_enabled=False)
    datahub = DataHub(settings=settings)
    scheduler = LocalDataScheduler(datahub)

    container = build_container(scheduler=scheduler)

    assert container.settings is settings
    assert container.datahub is datahub
    assert container.scheduler is scheduler
