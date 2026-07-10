from __future__ import annotations

from app.models.schemas import CacheStats, ProviderCapability, ProviderCapabilityStatus, ProviderStatus
from app.config import Settings
from app.services.provider_registry import all_provider_names
from app.services.datahub_source_plan import SourcePlanBuilder
from app.services.datahub_status_service import DataStatusService


def test_data_status_service_builds_status_without_sync_write_side_effects() -> None:
    cache = _StatusCache(
        provider_rows=[ProviderStatus(name="live", enabled=True, priority=1, healthy=True)],
        capability_rows=[],
    )
    providers = {
        "live": _CapabilityProvider(_capability("live", realtime_quote=True, stock_pool=True)),
        "disabled": _CapabilityProvider(_capability("disabled", enabled=False, realtime_quote=True)),
    }
    service = _service(
        cache,
        providers,
        provider_names=lambda: ["live", "disabled", "missing"],
        provider_index=lambda name: {"live": 1, "disabled": 2}.get(name, 99),
        priority=lambda kind: [(1, "live"), (2, "disabled")] if kind == "quote" else [],
    )

    status = service.status()

    assert cache.ensure_provider_calls == []
    assert cache.ensure_capability_calls == []
    assert [item.name for item in status.capabilities] == ["live", "disabled"]
    assert status.source_plan is not None
    assert status.source_plan.primary_quote_source == "live"


def test_data_status_service_sync_provider_flags_updates_cache() -> None:
    cache = _StatusCache(provider_rows=[], capability_rows=[])
    providers = {
        "live": _CapabilityProvider(_capability("live", realtime_quote=True, stock_pool=True)),
        "disabled": _CapabilityProvider(_capability("disabled", enabled=False, realtime_quote=True)),
    }
    service = _service(
        cache,
        providers,
        provider_names=lambda: ["live", "disabled", "missing"],
        provider_index=lambda name: {"live": 1, "disabled": 2}.get(name, 99),
        priority=lambda _kind: [],
    )

    service.sync_provider_enabled_flags()

    assert cache.ensure_provider_calls == [("live", 1, True), ("disabled", 2, False)]
    assert cache.ensure_capability_calls == [
        ("live", "quote", 1, True),
        ("live", "stock", 1, True),
        ("disabled", "quote", 2, False),
    ]


def test_data_status_service_sync_disables_stale_capabilities_no_longer_supported() -> None:
    cache = _StatusCache(
        provider_rows=[],
        capability_rows=[
            ProviderCapabilityStatus(
                name="futu",
                kind="order_book",
                enabled=True,
                priority=1,
                healthy=False,
                last_error="OpenD down",
                failure_count=1,
            )
        ],
    )
    providers = {
        "futu": _CapabilityProvider(_capability("futu", enabled=False, realtime_quote=True, order_book=True)),
    }
    service = _service(
        cache,
        providers,
        provider_names=lambda: ["futu"],
        provider_index=lambda _name: 1,
        priority=lambda _kind: [],
    )

    service.sync_provider_enabled_flags()

    assert cache.ensure_provider_calls == [("futu", 1, False)]
    assert cache.ensure_capability_calls == [
        ("futu", "quote", 1, False),
        ("futu", "order_book", 1, False),
    ]


def test_data_status_service_sync_disables_historical_capabilities_removed_from_provider() -> None:
    cache = _StatusCache(
        provider_rows=[],
        capability_rows=[
            ProviderCapabilityStatus(
                name="akshare",
                kind="plate",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="old plate error",
                failure_count=1,
            )
        ],
    )
    providers = {
        "akshare": _CapabilityProvider(_capability("akshare", realtime_quote=True)),
    }
    service = _service(
        cache,
        providers,
        provider_names=lambda: ["akshare"],
        provider_index=lambda _name: 2,
        priority=lambda _kind: [],
    )

    service.sync_provider_enabled_flags()

    assert cache.ensure_capability_calls == [
        ("akshare", "quote", 2, True),
        ("akshare", "plate", 2, False),
    ]


def test_all_provider_names_includes_minute_only_priority_provider() -> None:
    settings = Settings()
    settings.quote_provider_priority = ()
    settings.kline_provider_priority = ()
    settings.stock_provider_priority = ()
    settings.plate_provider_priority = ()
    settings.minute_provider_priority = ("minute_only",)

    names = all_provider_names(settings, {"minute_only": _CapabilityProvider(_capability("minute_only", minute_kline=True))})

    assert names == ["minute_only"]


def test_data_status_service_keeps_legacy_provider_capability_fallback() -> None:
    cache = _StatusCache(provider_rows=[], capability_rows=[])
    service = _service(
        cache,
        {"legacy": _LegacyProvider()},
        provider_names=lambda: ["legacy"],
        provider_index=lambda _name: 5,
        priority=lambda kind: [(5, "legacy")] if kind in {"quote", "kline"} else [],
    )

    service.sync_provider_enabled_flags()

    assert cache.ensure_provider_calls == [("legacy", 5, True)]
    assert cache.ensure_capability_calls == [
        ("legacy", "quote", 5, True),
        ("legacy", "kline", 5, True),
    ]
    assert service.capabilities()[0].name == "legacy"


class _StatusCache:
    def __init__(
        self,
        *,
        provider_rows: list[ProviderStatus],
        capability_rows: list[ProviderCapabilityStatus],
    ) -> None:
        self.provider_rows = provider_rows
        self.capability_rows = capability_rows
        self.ensure_provider_calls: list[tuple[str, int, bool]] = []
        self.ensure_capability_calls: list[tuple[str, str, int, bool]] = []

    def ensure_provider(self, name: str, priority: int, enabled: bool = True) -> None:
        self.ensure_provider_calls.append((name, priority, enabled))

    def ensure_provider_capability(self, name: str, kind: str, priority: int, enabled: bool = True) -> None:
        self.ensure_capability_calls.append((name, kind, priority, enabled))

    def provider_statuses(self) -> list[ProviderStatus]:
        return self.provider_rows

    def provider_capability_statuses(self) -> list[ProviderCapabilityStatus]:
        return self.capability_rows

    def stats(self) -> CacheStats:
        return CacheStats(
            path=":memory:",
            quote_count=0,
            quote_history_count=0,
            kline_count=0,
            stock_count=0,
            plate_count=0,
            provider_count=0,
        )


class _CapabilityProvider:
    source_name = "测试源"

    def __init__(self, capability: ProviderCapability) -> None:
        self._capability = capability

    def capability(self) -> ProviderCapability:
        return self._capability


class _LegacyProvider:
    source_name = "旧行情源"


def _service(
    cache: _StatusCache,
    providers: dict,
    *,
    provider_names,
    provider_index,
    priority,
) -> DataStatusService:
    return DataStatusService(
        cache=cache,
        providers=providers,
        provider_names=provider_names,
        provider_index=provider_index,
        source_plan_builder=SourcePlanBuilder(
            provider_names=provider_names,
            priority=priority,
            provider_index=provider_index,
            is_cooling=lambda _name, _kind: False,
        ),
    )


def _capability(
    name: str,
    *,
    enabled: bool = True,
    realtime_quote: bool = False,
    daily_kline: bool = False,
    minute_kline: bool = False,
    stock_pool: bool = False,
    order_book: bool = False,
) -> ProviderCapability:
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=enabled,
        reliability_level="测试",
        realtime_quote=realtime_quote,
        daily_kline=daily_kline,
        minute_kline=minute_kline,
        stock_pool=stock_pool,
        order_book=order_book,
        note="测试能力",
    )
