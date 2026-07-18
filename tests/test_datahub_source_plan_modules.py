from __future__ import annotations

from app.models.schemas import ProviderCapability, ProviderCapabilityStatus, ProviderStatus
from app.services.datahub_source_plan import PROVIDER_DECISION_RULES, SOURCE_HEALTH_WARNING_RULES, SourcePlanBuilder


def test_source_plan_action_handles_statusless_failed_capability() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[],
        capabilities=[_capability("akshare")],
        capability_statuses=[
            ProviderCapabilityStatus(
                name="akshare",
                kind="quote",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="quote down",
                failure_count=1,
            )
        ],
    )

    decision = plan.decisions[0]
    assert plan.primary_quote_source is None
    assert "报价最近失败" in decision.state
    assert "检查" in decision.action
    assert "无需处理" not in decision.action
    assert any("最近失败能力" in item for item in plan.warnings)


def test_source_plan_action_distinguishes_unprobed_capability_from_failure() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True)],
        capabilities=[_capability("akshare")],
        capability_statuses=[
            ProviderCapabilityStatus(
                name="akshare",
                kind="quote",
                enabled=True,
                priority=2,
                healthy=False,
                updated_at="2026-05-13 09:35:00",
            )
        ],
    )

    decision = plan.decisions[0]
    assert plan.primary_quote_source == "akshare"
    assert decision.state == "报价未探测"
    assert "首次调用后会刷新状态" in decision.action
    assert all("最近失败能力" not in item for item in plan.warnings)


def test_source_plan_marks_disabled_provider_without_recovery_noise() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[ProviderStatus(name="akshare", enabled=False, priority=2, healthy=False, last_error="old error")],
        capabilities=[_capability("akshare", enabled=False)],
        capability_statuses=[],
    )

    decision = plan.decisions[0]
    assert decision.state == "未启用"
    assert decision.action == "无需处理，当前不参与分析。"


def test_provider_decision_rule_order_keeps_cooling_ahead_of_health() -> None:
    assert [rule.name for rule in PROVIDER_DECISION_RULES] == [
        "cooling",
        "healthy",
        "enabled_failed",
        "disabled",
        "statusless",
    ]


def test_source_health_warning_rule_order_prefers_capability_failures() -> None:
    assert [rule.name for rule in SOURCE_HEALTH_WARNING_RULES] == [
        "capability_failures",
        "provider_failures",
    ]


def test_source_plan_cooling_action_takes_priority_over_healthy_status() -> None:
    builder = SourcePlanBuilder(
        provider_names=lambda: ["akshare"],
        priority=lambda kind: [(2, "akshare")] if kind == "quote" else [],
        provider_index=lambda name: 2,
        is_cooling=lambda name, kind: True,
    )
    plan = builder.build(
        providers=[ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True)],
        capabilities=[_capability("akshare")],
        capability_statuses=[],
    )

    decision = plan.decisions[0]
    assert decision.state == "冷却中"
    assert "短暂跳过" in decision.action


def test_source_plan_normalizes_dirty_provider_names_and_deduplicates_decisions() -> None:
    builder = SourcePlanBuilder(
        provider_names=lambda: [" AKShare ", "akshare", " \n "],
        priority=lambda kind: [(2, "akshare")] if kind == "quote" else [],
        provider_index=lambda name: 2,
        is_cooling=lambda name, kind: False,
    )
    plan = builder.build(
        providers=[
            ProviderStatus(
                name=" AKShare ",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="old failure",
                failure_count=1,
                updated_at="2026-05-13 09:30:00",
            ),
            ProviderStatus(
                name="akshare",
                enabled=True,
                priority=2,
                healthy=True,
                last_success="2026-05-13 09:31:00",
                success_count=1,
                updated_at="2026-05-13 09:31:00",
            ),
        ],
        capabilities=[_capability(" AKShare ")],
        capability_statuses=[],
    )

    assert plan.primary_quote_source == "akshare"
    assert [decision.name for decision in plan.decisions] == ["akshare"]
    assert plan.decisions[0].state == "正常"


def test_source_plan_warning_deduplicates_dirty_capability_failures() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[],
        capabilities=[_capability("akshare")],
        capability_statuses=[
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
                last_error="quote down again",
                failure_count=1,
            ),
        ],
    )

    capability_warning = next(item for item in plan.warnings if item.startswith("最近失败能力："))
    assert capability_warning.count("AKShare 报价") == 1
    assert "Quote" not in capability_warning


def test_source_plan_ignores_stale_failures_outside_current_provider_names() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[
            ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True),
            ProviderStatus(
                name="old_source",
                enabled=True,
                priority=9,
                healthy=False,
                last_error="removed provider down",
                failure_count=1,
                updated_at="2026-05-13 09:35:00",
            ),
        ],
        capabilities=[_capability("akshare")],
        capability_statuses=[
            ProviderCapabilityStatus(
                name="old_source",
                kind="quote",
                enabled=True,
                priority=9,
                healthy=False,
                last_error="old quote down",
                failure_count=1,
                updated_at="2026-05-13 09:35:00",
            )
        ],
    )

    assert plan.primary_quote_source == "akshare"
    assert [decision.name for decision in plan.decisions] == ["akshare"]
    assert all("old_source" not in warning for warning in plan.warnings)
    assert all("最近失败" not in warning for warning in plan.warnings)


def test_source_plan_does_not_label_old_failures_as_recent_warnings() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[
            ProviderStatus(
                name="akshare",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="old provider failure",
                failure_count=1,
                updated_at="2026-05-13 09:35:00",
            )
        ],
        capabilities=[_capability("akshare")],
        capability_statuses=[
            ProviderCapabilityStatus(
                name="akshare",
                kind="quote",
                enabled=True,
                priority=2,
                healthy=False,
                last_error="old quote failure",
                failure_count=1,
                updated_at="2026-05-13 09:35:00",
            )
        ],
    )

    assert all("最近失败" not in warning for warning in plan.warnings)


def test_source_plan_marks_missing_kline_primary_as_degraded() -> None:
    builder = SourcePlanBuilder(
        provider_names=lambda: ["akshare"],
        priority=lambda kind: [(2, "akshare")] if kind in {"quote", "minute"} else [],
        provider_index=lambda name: 2,
        is_cooling=lambda name, kind: False,
    )
    plan = builder.build(
        providers=[ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True)],
        capabilities=[_capability("akshare", minute_kline=True)],
        capability_statuses=[],
    )

    assert plan.primary_quote_source == "akshare"
    assert plan.primary_minute_source == "akshare"
    assert plan.primary_kline_source is None
    assert plan.health_level == "降级可用"
    assert any("日K主源不可用" in item for item in plan.warnings)


def test_source_plan_realtime_redundancy_counts_unique_non_demo_providers() -> None:
    builder = _builder()
    plan = builder.build(
        providers=[ProviderStatus(name="akshare", enabled=True, priority=2, healthy=True)],
        capabilities=[
            _capability("akshare"),
            _capability(" AKShare "),
            _capability("demo", reliability_level=" 演示 "),
        ],
        capability_statuses=[],
    )

    assert any("至少保留两个实时报价源" in item for item in plan.suggestions)


def test_source_plan_sanitizes_status_errors_before_return() -> None:
    provider = ProviderStatus(
        name="akshare",
        enabled=True,
        priority=2,
        healthy=False,
        last_error="GET https://alice:secret@example.test/quote?token=raw-token failed",
        failure_count=1,
    )

    plan = _builder().build(
        providers=[provider],
        capabilities=[_capability("akshare")],
        capability_statuses=[],
    )

    returned_text = plan.decisions[0].last_error or ""
    assert "alice" not in returned_text
    assert "secret" not in returned_text
    assert "raw-token" not in returned_text
    assert "<redacted>" in returned_text
    assert provider.last_error == "GET https://alice:secret@example.test/quote?token=raw-token failed"


def _builder() -> SourcePlanBuilder:
    return SourcePlanBuilder(
        provider_names=lambda: ["akshare"],
        priority=lambda kind: [(2, "akshare")] if kind == "quote" else [],
        provider_index=lambda name: 2,
        is_cooling=lambda name, kind: False,
    )


def _capability(
    name: str,
    *,
    enabled: bool = True,
    reliability_level: str = "公开源",
    daily_kline: bool = False,
    minute_kline: bool = False,
) -> ProviderCapability:
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=enabled,
        reliability_level=reliability_level,
        realtime_quote=True,
        daily_kline=daily_kline,
        minute_kline=minute_kline,
        note="测试能力",
    )
