from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


PRIORITY_ENV_NAMES = (
    "ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY",
    "ASHARE_RADAR_KLINE_PROVIDER_PRIORITY",
    "ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY",
    "ASHARE_RADAR_STOCK_PROVIDER_PRIORITY",
    "ASHARE_RADAR_PLATE_PROVIDER_PRIORITY",
)


@pytest.fixture(autouse=True)
def _clear_provider_priority_environment(monkeypatch) -> None:
    for name in PRIORITY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_provider_and_market_scan_recovery_defaults_are_stable() -> None:
    settings = Settings()

    assert settings.quote_provider_priority == ("tencent", "futu", "akshare")
    assert settings.kline_provider_priority == ("tencent", "akshare", "tushare", "baostock")
    assert settings.minute_provider_priority == ("futu", "akshare")
    assert settings.stock_provider_priority == ("akshare", "tushare", "baostock", "local")
    assert settings.plate_provider_priority == ("akshare", "local")
    assert settings.market_scan_preflight_enabled is True
    assert settings.market_scan_preflight_timeout_seconds == 30
    assert settings.market_scan_auto_retry_delays_seconds == (600, 1800, 3600)
    assert settings.market_scan_auto_retry_max_attempts == 3


@pytest.mark.parametrize(
    ("env_name", "field_name", "raw", "expected"),
    [
        (
            "ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY",
            "quote_provider_priority",
            " Futu, tencent, FUTU, akshare ",
            ("futu", "tencent", "akshare"),
        ),
        (
            "ASHARE_RADAR_KLINE_PROVIDER_PRIORITY",
            "kline_provider_priority",
            "tushare,tencent,baostock",
            ("tushare", "tencent", "baostock"),
        ),
        (
            "ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY",
            "minute_provider_priority",
            "akshare,futu",
            ("akshare", "futu"),
        ),
        (
            "ASHARE_RADAR_STOCK_PROVIDER_PRIORITY",
            "stock_provider_priority",
            "tushare,local,akshare",
            ("tushare", "local", "akshare"),
        ),
        (
            "ASHARE_RADAR_PLATE_PROVIDER_PRIORITY",
            "plate_provider_priority",
            "local,akshare",
            ("local", "akshare"),
        ),
    ],
)
def test_provider_priorities_are_environment_configurable_and_deduplicated(
    monkeypatch,
    env_name: str,
    field_name: str,
    raw: str,
    expected: tuple[str, ...],
) -> None:
    monkeypatch.setenv(env_name, raw)

    settings = Settings()

    assert getattr(settings, field_name) == expected


def test_environment_provider_priority_rejects_unknown_name(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY", "tencent,unknown-feed")

    with pytest.raises(ValueError, match="ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY 包含未知数据源 'unknown-feed'"):
        Settings()


def test_code_level_provider_priority_keeps_custom_provider_extension() -> None:
    settings = Settings(quote_provider_priority=(" CustomFeed ", "customfeed", "tencent"))

    assert settings.quote_provider_priority == ("customfeed", "tencent")


def test_market_scan_recovery_settings_read_environment(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED", "off")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS", "120, 600,1800")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS", "2")

    settings = Settings()

    assert settings.market_scan_preflight_enabled is False
    assert settings.market_scan_preflight_timeout_seconds == 12.5
    assert settings.market_scan_auto_retry_delays_seconds == (120, 600, 1800)
    assert settings.market_scan_auto_retry_max_attempts == 2


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED", "sometimes", "必须是布尔值"),
        ("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS", "0", "必须大于等于 0.1"),
        ("ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS", "301", "less than or equal to 300"),
        ("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS", "600,nope", "必须是逗号分隔的整数列表"),
        ("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS", "0,600", "每个值必须在 1 到 86400 之间"),
        ("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS", "600,300", "必须严格递增"),
        ("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS", "-1", "必须大于等于 0"),
        ("ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS", "11", "less than or equal to 10"),
        (
            "ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS",
            "4",
            "不能大于 market_scan_auto_retry_delays_seconds 的数量",
        ),
    ],
)
def test_market_scan_recovery_settings_reject_invalid_environment(
    monkeypatch,
    name: str,
    raw: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises((ValueError, ValidationError), match=message):
        Settings()


@pytest.mark.parametrize(
    "delays",
    [(), (600, 600), (600, 300), (86401,)],
)
def test_market_scan_retry_delays_reject_invalid_code_level_values(delays: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        Settings(market_scan_auto_retry_delays_seconds=delays)
