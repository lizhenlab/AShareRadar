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


def test_default_quote_priority_adds_enabled_installed_futu_between_public_sources(monkeypatch) -> None:
    monkeypatch.setattr("app.services.futu_provider.is_installed", lambda _name: True)
    settings = Settings(futu_enabled=True)
    providers = build_providers(settings)
    providers["tencent"] = _ProviderWithCapability(_capability("tencent", realtime_quote=True))
    providers["akshare"] = _ProviderWithCapability(_capability("akshare", realtime_quote=True))

    names = [name for _, name in provider_priority(settings, providers, "quote")]

    assert names == ["tencent", "futu", "akshare"]


def test_default_quote_priority_skips_futu_when_disabled_or_not_installed(monkeypatch) -> None:
    monkeypatch.setattr("app.services.futu_provider.is_installed", lambda _name: True)
    disabled_settings = Settings(futu_enabled=False)
    disabled_providers = build_providers(disabled_settings)
    disabled_providers["tencent"] = _ProviderWithCapability(_capability("tencent", realtime_quote=True))
    disabled_providers["akshare"] = _ProviderWithCapability(_capability("akshare", realtime_quote=True))
    expected = ["tencent", "akshare"]
    assert [name for _, name in provider_priority(disabled_settings, disabled_providers, "quote")] == expected

    monkeypatch.setattr("app.services.futu_provider.is_installed", lambda _name: False)
    missing_settings = Settings(futu_enabled=True)
    missing_providers = build_providers(missing_settings)
    missing_providers["tencent"] = _ProviderWithCapability(_capability("tencent", realtime_quote=True))
    missing_providers["akshare"] = _ProviderWithCapability(_capability("akshare", realtime_quote=True))
    assert [name for _, name in provider_priority(missing_settings, missing_providers, "quote")] == expected


def test_default_kline_priority_adds_configured_installed_tushare_before_baostock(monkeypatch) -> None:
    monkeypatch.setattr("app.services.tushare_provider.is_installed", lambda _name: True)
    settings = Settings(tushare_token="configured-token")
    providers = build_providers(settings)
    providers["tencent"] = _ProviderWithCapability(_capability("tencent", daily_kline=True))
    providers["akshare"] = _ProviderWithCapability(_capability("akshare", daily_kline=True))
    providers["baostock"] = _ProviderWithCapability(_capability("baostock", daily_kline=True))

    names = [name for _, name in provider_priority(settings, providers, "kline")]

    assert names == ["tencent", "akshare", "tushare", "baostock"]


def test_default_kline_priority_skips_tushare_without_token_or_installation(monkeypatch) -> None:
    monkeypatch.setattr("app.services.tushare_provider.is_installed", lambda _name: True)
    no_token_settings = Settings(tushare_token=None)
    no_token_providers = build_providers(no_token_settings)
    for name in ("tencent", "akshare", "baostock"):
        no_token_providers[name] = _ProviderWithCapability(_capability(name, daily_kline=True))
    expected = ["tencent", "akshare", "baostock"]
    assert [name for _, name in provider_priority(no_token_settings, no_token_providers, "kline")] == expected

    monkeypatch.setattr("app.services.tushare_provider.is_installed", lambda _name: False)
    missing_settings = Settings(tushare_token="configured-token")
    missing_providers = build_providers(missing_settings)
    for name in ("tencent", "akshare", "baostock"):
        missing_providers[name] = _ProviderWithCapability(_capability(name, daily_kline=True))
    assert [name for _, name in provider_priority(missing_settings, missing_providers, "kline")] == expected


def test_environment_quote_priority_is_an_exact_override(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY", "akshare,tencent")
    monkeypatch.setattr("app.services.futu_provider.is_installed", lambda _name: True)
    settings = Settings(futu_enabled=True)
    monkeypatch.delenv("ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY")
    providers = build_providers(settings)
    providers["tencent"] = _ProviderWithCapability(_capability("tencent", realtime_quote=True))
    providers["akshare"] = _ProviderWithCapability(_capability("akshare", realtime_quote=True))

    names = [name for _, name in provider_priority(settings, providers, "quote")]

    assert names == ["akshare", "tencent"]


def test_environment_kline_priority_can_front_tushare(monkeypatch) -> None:
    monkeypatch.setenv(
        "ASHARE_RADAR_KLINE_PROVIDER_PRIORITY",
        "tushare,tencent,akshare,baostock",
    )
    monkeypatch.setattr("app.services.tushare_provider.is_installed", lambda _name: True)
    settings = Settings(tushare_token="configured-token")
    providers = build_providers(settings)
    for name in ("tencent", "akshare", "baostock"):
        providers[name] = _ProviderWithCapability(_capability(name, daily_kline=True))

    names = [name for _, name in provider_priority(settings, providers, "kline")]

    assert names == ["tushare", "tencent", "akshare", "baostock"]


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
    stock_pool: bool = False,
) -> ProviderCapability:
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=enabled,
        realtime_quote=realtime_quote,
        daily_kline=daily_kline,
        minute_kline=minute_kline,
        order_book=order_book,
        stock_pool=stock_pool,
        note="测试能力",
    )
