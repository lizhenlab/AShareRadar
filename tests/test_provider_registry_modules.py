from __future__ import annotations

from app.config import Settings
from app.models.schemas import ProviderCapability
from app.services.provider_registry import (
    build_providers,
    provider_capabilities,
    provider_enabled_for,
    provider_priority,
    supported_provider_kinds,
)


class _ProviderWithoutCapability:
    source_name = "测试公开源"


class _ProviderWithCapability:
    def __init__(self, capability: ProviderCapability) -> None:
        self._capability = capability

    def capability(self) -> ProviderCapability:
        return self._capability


def test_provider_priority_deduplicates_names_filters_missing_and_appends_demo() -> None:
    settings = Settings(
        demo_provider_enabled=True,
        quote_provider_priority=("custom", "custom", "missing"),
    )
    providers = {
        "custom": _ProviderWithCapability(_capability("custom", realtime_quote=True)),
        "demo": _ProviderWithCapability(_capability("demo", realtime_quote=True)),
    }

    names = [name for _, name in provider_priority(settings, providers, "quote")]

    assert names == ["custom", "demo"]


def test_build_providers_injects_tushare_token_from_settings() -> None:
    providers = build_providers(Settings(tushare_token=" injected-token "))

    assert getattr(providers["tushare"], "token") == "injected-token"


def test_build_providers_injects_tencent_timeout_from_settings() -> None:
    providers = build_providers(Settings(request_timeout_seconds=1.25))

    assert getattr(providers["tencent"], "timeout") == 1.25


def test_provider_priority_returns_empty_for_unknown_kind() -> None:
    settings = Settings(quote_provider_priority=("custom",))
    providers = {"custom": _ProviderWithoutCapability()}

    assert provider_priority(settings, providers, "unknown") == []


def test_provider_enabled_for_unknown_kind_is_false() -> None:
    provider = _ProviderWithCapability(_capability("custom", realtime_quote=True))

    assert provider_enabled_for(provider, "quote") is True
    assert provider_enabled_for(provider, "quot") is False
    assert provider_enabled_for(_ProviderWithoutCapability(), "stock") is False


def test_provider_capabilities_fallback_preserves_provider_name() -> None:
    capabilities = provider_capabilities({"custom": _ProviderWithoutCapability()})

    assert [item.name for item in capabilities] == ["custom"]
    assert capabilities[0].realtime_quote is True
    assert capabilities[0].daily_kline is True
    assert "测试公开源" in capabilities[0].note


def test_provider_capabilities_normalizes_mismatched_declared_name() -> None:
    provider = _ProviderWithCapability(_capability("declared", realtime_quote=True))

    capabilities = provider_capabilities({"actual": provider})

    assert [item.name for item in capabilities] == ["actual"]


def test_supported_provider_kinds_uses_capability_fields() -> None:
    provider = _ProviderWithCapability(_capability("custom", minute_kline=True, order_book=True))

    assert supported_provider_kinds(provider) == ["minute", "order_book"]


def _capability(
    name: str,
    *,
    enabled: bool = True,
    realtime_quote: bool = False,
    daily_kline: bool = False,
    minute_kline: bool = False,
    order_book: bool = False,
) -> ProviderCapability:
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=enabled,
        realtime_quote=realtime_quote,
        daily_kline=daily_kline,
        minute_kline=minute_kline,
        order_book=order_book,
        note="测试能力",
    )
