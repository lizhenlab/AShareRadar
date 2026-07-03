from __future__ import annotations

import pytest

from app.models.schemas import ProviderCapabilityStatus, ProviderStatus
from app.services.datahub_status import (
    DEFAULT_PROVIDER_RECOVERY_ACTION,
    PROVIDER_RECOVERY_ACTION_RULES,
    _capability_status_map,
    _first_healthy_provider,
    _provider_error_text,
    _provider_recovery_action,
    _provider_source_key,
    _provider_success_rate,
    _unhealthy_capability_labels,
)


def test_provider_recovery_action_rule_order_is_explicit() -> None:
    assert [rule.name for rule in PROVIDER_RECOVERY_ACTION_RULES] == [
        "proxy_or_network",
        "remote_disconnected",
        "timeout",
        "baostock_backup",
        "futu_opend",
        "tushare_token",
    ]


@pytest.mark.parametrize(
    ("provider", "error", "expected"),
    [
        ("akshare", "ProxyError('Unable to connect to proxy')", "检查网络代理或源站连通性；冷却期内系统会先使用其他源或缓存。"),
        ("eastmoney", "RemoteDisconnected: peer closed", "东方财富源站主动断开连接；系统会先使用 Tencent、BaoStock 或缓存，稍后自动重试。"),
        ("tencent", "TimeoutError: read timeout", "源站响应超时；系统会先使用其他源或缓存，稍后自动重试。"),
        ("baostock", None, "BaoStock 偏历史备份，失败时先依赖 Tencent/AKShare 日K缓存。"),
        ("futu", None, "确认 Futu OpenD 已启动，并设置 ASHARE_RADAR_FUTU_ENABLED=1。"),
        ("tushare", None, "配置 ASHARE_RADAR_TUSHARE_TOKEN 后再启用。"),
    ],
)
def test_provider_recovery_action_matches_error_and_provider_rules(provider: str, error: str | None, expected: str) -> None:
    assert _provider_recovery_action(provider, error) == expected


def test_provider_recovery_action_prioritizes_error_over_provider_default() -> None:
    assert _provider_recovery_action("futu", "TimeoutError: ping timeout") == "源站响应超时；系统会先使用其他源或缓存，稍后自动重试。"


def test_provider_recovery_action_uses_default_for_unknown_provider_and_error() -> None:
    assert _provider_recovery_action("unknown", "strange failure") == DEFAULT_PROVIDER_RECOVERY_ACTION


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("腾讯行情·缓存", "tencent"),
        ("AKShare·东方财富直连", "akshare"),
        ("baostock historical", "baostock"),
        ("富途OpenD", "futu"),
        ("本地演示数据", "demo"),
        ("CustomFeed·缓存", "customfeed"),
        ("  Custom Feed · 缓存  ", "custom_feed"),
        (" \n\t ", "unknown"),
    ],
)
def test_provider_source_key_normalizes_known_sources_and_keeps_unknown_prefix(source: str, expected: str) -> None:
    assert _provider_source_key(source) == expected


def test_provider_recovery_action_cleans_provider_name_and_error_text() -> None:
    assert _provider_recovery_action(" FUTU ", " read timeout \n") == "源站响应超时；系统会先使用其他源或缓存，稍后自动重试。"
    assert _provider_recovery_action(" FUTU ", " \n\t ") == "确认 Futu OpenD 已启动，并设置 ASHARE_RADAR_FUTU_ENABLED=1。"


def test_provider_error_text_collapses_whitespace_and_falls_back_for_blank_messages() -> None:
    assert _provider_error_text(ValueError("  bad\n  thing\t")) == "bad thing"
    assert _provider_error_text(RuntimeError(" \n\t ")) == "RuntimeError"


def test_provider_success_rate_ignores_non_finite_and_negative_counts() -> None:
    status = ProviderStatus.model_construct(
        name="akshare",
        enabled=True,
        priority=2,
        healthy=False,
        success_count=float("nan"),
        failure_count=float("inf"),
    )
    assert _provider_success_rate(status) is None

    status = ProviderStatus.model_construct(
        name="akshare",
        enabled=True,
        priority=2,
        healthy=False,
        success_count=3,
        failure_count=-5,
    )
    assert _provider_success_rate(status) == 100.0


def test_capability_status_map_normalizes_dirty_keys_and_keeps_latest_duplicate() -> None:
    items = [
        ProviderCapabilityStatus(
            name=" AKShare ",
            kind=" Quote ",
            enabled=True,
            priority=2,
            healthy=False,
            last_error="old failure",
            failure_count=1,
            updated_at="2026-05-13 09:30:00",
        ),
        ProviderCapabilityStatus(
            name="akshare",
            kind="quote",
            enabled=True,
            priority=2,
            healthy=True,
            last_success="2026-05-13 09:31:00",
            success_count=1,
            updated_at="2026-05-13 09:31:00",
        ),
    ]

    statuses = _capability_status_map(items)

    assert list(statuses) == [("akshare", "quote")]
    assert statuses[("akshare", "quote")].healthy is True


def test_unhealthy_capability_labels_deduplicates_and_labels_unknowns() -> None:
    labels = _unhealthy_capability_labels(
        [
            ProviderCapabilityStatus(
                name=" AKShare ",
                kind=" Quote ",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="quote down",
                failure_count=1,
            ),
            ProviderCapabilityStatus(
                name="akshare",
                kind="quote",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="quote still down",
                failure_count=1,
            ),
            ProviderCapabilityStatus(
                name=" \n ",
                kind=" \t ",
                enabled=True,
                priority=99,
                healthy=False,
                last_error="unknown down",
                failure_count=1,
            ),
        ],
    )

    assert labels == ["AKShare 报价", "未知源 未知能力"]


def test_first_healthy_provider_uses_provider_status_when_capability_is_unprobed() -> None:
    providers = {"akshare": ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True)}
    capabilities = {
        ("akshare", "quote"): ProviderCapabilityStatus(
            name="akshare",
            kind="quote",
            enabled=True,
            priority=2,
            healthy=False,
        )
    }

    assert _first_healthy_provider(["akshare"], providers, capabilities, "quote") == "akshare"


def test_first_healthy_provider_matches_dirty_capability_status_keys() -> None:
    capabilities = _capability_status_map(
        [
            ProviderCapabilityStatus(
                name=" AKShare ",
                kind=" Quote ",
                enabled=True,
                priority=2,
                healthy=True,
                last_success="2026-05-13 09:35:00",
                success_count=1,
            )
        ],
    )

    assert _first_healthy_provider(["akshare"], {}, capabilities, "quote") == "akshare"


def test_first_healthy_provider_skips_active_failed_capability_even_when_provider_is_healthy() -> None:
    providers = {
        "akshare": ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True),
        "tencent": ProviderStatus(name="tencent", enabled=True, priority=1, healthy=True),
    }
    capabilities = {
        ("akshare", "quote"): ProviderCapabilityStatus(
            name="akshare",
            kind="quote",
            enabled=True,
            priority=2,
            healthy=False,
            last_error="quote down",
            failure_count=1,
        )
    }

    assert _first_healthy_provider(["akshare", "tencent"], providers, capabilities, "quote") == "tencent"
