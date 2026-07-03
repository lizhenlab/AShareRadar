from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import (
    DataSourcePlan,
    ProviderCapability,
    ProviderCapabilityStatus,
    ProviderDecision,
    ProviderStatus,
)
from app.services.datahub_status import (
    _capability_labels,
    _capability_status_map,
    _clean_status_text,
    _first_healthy_provider,
    _normalize_provider_name,
    _provider_display_name,
    _provider_capability_state,
    _provider_cooling_kinds,
    _provider_recovery_action,
    _provider_role,
    _safe_count,
    _provider_success_rate,
    _source_plan_summary,
    _unhealthy_capability_labels,
)


@dataclass(frozen=True)
class SourcePlanContext:
    providers: list[ProviderStatus]
    capabilities: list[ProviderCapability]
    capability_statuses: list[ProviderCapabilityStatus]
    by_name: dict[str, ProviderStatus]
    by_capability: dict[tuple[str, str], ProviderCapabilityStatus]
    caps_by_name: dict[str, ProviderCapability]
    quote_names: list[str]
    kline_names: list[str]
    minute_names: list[str]


@dataclass(frozen=True)
class PrimarySources:
    quote: str | None
    kline: str | None
    minute: str | None


@dataclass(frozen=True)
class ProviderDecisionContext:
    name: str
    status: ProviderStatus | None
    state: str
    cooling: list[str]


@dataclass(frozen=True)
class ProviderDecisionRule:
    name: str
    matches: Callable[[ProviderDecisionContext], bool]
    decide: Callable[[ProviderDecisionContext], tuple[str, str]]


@dataclass(frozen=True)
class SourceHealthWarningRule:
    name: str
    labels: Callable[[SourcePlanContext], list[str]]
    label_limit: int
    warning_prefix: str
    suggestion: str


class SourcePlanBuilder:
    def __init__(
        self,
        provider_names: Callable[[], list[str]],
        priority: Callable[[str], list[tuple[int, str]]],
        provider_index: Callable[[str], int],
        is_cooling: Callable[[str, str], bool],
    ) -> None:
        self._provider_names = provider_names
        self._priority = priority
        self._provider_index = provider_index
        self._is_cooling = is_cooling

    def build(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus] | None = None,
    ) -> DataSourcePlan:
        context = self._context(providers, capabilities, capability_statuses or [])
        primaries = _primary_sources(context)
        decisions = [
            self._provider_decision_from_context(name, context)
            for name in _normalized_provider_names(self._provider_names())
        ]
        warnings, suggestions = _plan_warnings_and_suggestions(context, primaries)
        health_level = _plan_health_level(warnings, primaries)
        summary = _source_plan_summary(health_level, primaries.quote, primaries.kline, primaries.minute)
        return DataSourcePlan(
            primary_quote_source=primaries.quote,
            primary_kline_source=primaries.kline,
            primary_minute_source=primaries.minute,
            health_level=health_level,
            summary=summary,
            decisions=decisions,
            warnings=warnings,
            suggestions=suggestions,
        )

    def provider_decision(
        self,
        name: str,
        status: ProviderStatus | None,
        capability: ProviderCapability | None,
        quote_names: list[str],
        kline_names: list[str],
        minute_names: list[str],
        capability_statuses: dict[tuple[str, str], ProviderCapabilityStatus],
    ) -> ProviderDecision:
        name = _normalize_provider_name(name)
        capabilities = _capability_labels(capability)
        role = _provider_role(name, quote_names, kline_names, minute_names)
        state = _provider_capability_state(name, quote_names, kline_names, minute_names, capability_statuses)
        cooling = _provider_cooling_kinds(name, quote_names, kline_names, minute_names, self._is_cooling) if status and status.enabled else []
        state, action = _provider_decision_state_action(name, status, state, cooling)
        return ProviderDecision(
            name=name,
            role=role,
            state=state,
            priority=status.priority if status else self._provider_index(name),
            capabilities=capabilities,
            success_rate=_provider_success_rate(status),
            last_success=(_clean_status_text(status.last_success) or None) if status else None,
            last_error=(_clean_status_text(status.last_error) or None) if status else None,
            action=action,
        )

    def _context(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus],
    ) -> SourcePlanContext:
        return SourcePlanContext(
            providers=providers,
            capabilities=capabilities,
            capability_statuses=capability_statuses,
            by_name=_providers_by_name(providers),
            by_capability=_capability_status_map(capability_statuses),
            caps_by_name=_capabilities_by_name(capabilities),
            quote_names=_priority_provider_names(self._priority("quote")),
            kline_names=_priority_provider_names(self._priority("kline")),
            minute_names=_priority_provider_names(self._priority("minute")),
        )

    def _provider_decision_from_context(self, name: str, context: SourcePlanContext) -> ProviderDecision:
        return self.provider_decision(
            name,
            context.by_name.get(name),
            context.caps_by_name.get(name),
            context.quote_names,
            context.kline_names,
            context.minute_names,
            context.by_capability,
        )


def _primary_sources(context: SourcePlanContext) -> PrimarySources:
    return PrimarySources(
        quote=_first_healthy_provider(context.quote_names, context.by_name, context.by_capability, "quote"),
        kline=_first_healthy_provider(context.kline_names, context.by_name, context.by_capability, "kline"),
        minute=_first_healthy_provider(context.minute_names, context.by_name, context.by_capability, "minute"),
    )


def _plan_warnings_and_suggestions(context: SourcePlanContext, primaries: PrimarySources) -> tuple[list[str], list[str]]:
    warnings, suggestions = _source_health_warnings(context)
    _append_primary_source_warnings(warnings, suggestions, primaries)
    if _non_demo_realtime_count(context.capabilities) < 2:
        _append_unique(suggestions, "建议至少保留两个实时报价源，用于价格一致性校验。")
    return warnings, suggestions


def _source_health_warnings(context: SourcePlanContext) -> tuple[list[str], list[str]]:
    for rule in SOURCE_HEALTH_WARNING_RULES:
        labels = rule.labels(context)
        if labels:
            return [rule.warning_prefix + "、".join(labels[: rule.label_limit])], [rule.suggestion]
    return [], []


def _append_primary_source_warnings(warnings: list[str], suggestions: list[str], primaries: PrimarySources) -> None:
    if not primaries.quote:
        _append_warning_pair(
            warnings,
            suggestions,
            "没有健康的实时报价主源，实时分析会依赖缓存。",
            "修复 Tencent/AKShare/Futu 任一实时报价源。",
        )
    if not primaries.kline:
        _append_warning_pair(
            warnings,
            suggestions,
            "日K主源不可用，趋势和历史分析会降级。",
            "修复 Tencent/AKShare/BaoStock 任一日K源。",
        )
    if not primaries.minute:
        _append_warning_pair(
            warnings,
            suggestions,
            "分钟线主源不可用，盘中做T判断会降级。",
            "启用 Futu OpenD 或修复 AKShare 分钟线接口。",
        )


def _non_demo_realtime_count(capabilities: list[ProviderCapability]) -> int:
    names = {
        _normalize_provider_name(item.name)
        for item in capabilities
        if item.enabled
        and item.realtime_quote
        and _clean_status_text(item.reliability_level) != "演示"
        and _normalize_provider_name(item.name)
    }
    return len(names)


def _plan_health_level(warnings: list[str], primaries: PrimarySources) -> str:
    if not warnings:
        return "健康"
    return "降级可用" if primaries.quote or primaries.kline else "高风险"


def _provider_decision_state_action(
    name: str,
    status: ProviderStatus | None,
    state: str,
    cooling: list[str],
) -> tuple[str, str]:
    context = ProviderDecisionContext(name=name, status=status, state=state, cooling=cooling)
    for rule in PROVIDER_DECISION_RULES:
        if rule.matches(context):
            return rule.decide(context)
    return _statusless_provider_decision(state)


def _cooling_provider_decision(context: ProviderDecisionContext) -> tuple[str, str]:
    return context.state or "冷却中", "刚发生失败的能力会短暂跳过：" + "、".join(context.cooling) + "。其他能力可继续使用。"


def _healthy_provider_decision_from_context(context: ProviderDecisionContext) -> tuple[str, str]:
    return _healthy_provider_decision(context.state)


def _failed_provider_decision_from_context(context: ProviderDecisionContext) -> tuple[str, str]:
    assert context.status is not None
    return _failed_provider_decision(context.name, context.status, context.state)


def _disabled_provider_decision(context: ProviderDecisionContext) -> tuple[str, str]:
    return context.state or "未启用", "无需处理，当前不参与分析。"


def _statusless_provider_decision_from_context(context: ProviderDecisionContext) -> tuple[str, str]:
    return _statusless_provider_decision(context.state)


def _healthy_provider_decision(state: str) -> tuple[str, str]:
    if not state:
        return "正常", "继续作为当前可用数据源。"
    if "最近失败" in state:
        return state, "仅继续使用正常能力；最近失败的能力会由其他源或缓存兜底。"
    if "未探测" in state:
        return state, "已启用但部分能力未探测，首次调用后会刷新状态。"
    return state, "继续作为当前可用数据源。"


def _failed_provider_decision(name: str, status: ProviderStatus, state: str) -> tuple[str, str]:
    failed_state = state if state and "最近失败" in state else "最近失败"
    return failed_state, _provider_recovery_action(name, status.last_error)


def _statusless_provider_decision(state: str) -> tuple[str, str]:
    if not state:
        return "未启用", "无需处理，当前不参与分析。"
    if "最近失败" in state:
        return state, "按失败能力检查网络、Token、本地客户端或源站连通性。"
    if "未探测" in state:
        return state, "等待首次探测；未探测能力暂不作为健康主源。"
    return state, "继续作为当前可用数据源。"


def _context_has_cooling(context: ProviderDecisionContext) -> bool:
    return bool(context.cooling)


def _context_status_enabled(context: ProviderDecisionContext) -> bool:
    return bool(context.status and context.status.enabled)


def _context_status_healthy(context: ProviderDecisionContext) -> bool:
    return bool(context.status and context.status.enabled and context.status.healthy)


def _context_status_disabled(context: ProviderDecisionContext) -> bool:
    return bool(context.status and not context.status.enabled)


def _always_matches(_context: ProviderDecisionContext) -> bool:
    return True


def _unhealthy_capability_warning_labels(context: SourcePlanContext) -> list[str]:
    return _unhealthy_capability_labels(context.capability_statuses)


def _unhealthy_provider_warning_labels(context: SourcePlanContext) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for item in context.providers:
        name = _normalize_provider_name(item.name)
        if not (item.enabled and not item.healthy and _provider_has_failure_signal(item)) or name in seen:
            continue
        labels.append(_provider_display_name(item.name))
        seen.add(name)
    return labels


def _provider_has_failure_signal(item: ProviderStatus) -> bool:
    return bool(_clean_status_text(item.last_error) or _safe_count(item.failure_count))


def _append_warning_pair(warnings: list[str], suggestions: list[str], warning: str, suggestion: str) -> None:
    _append_unique(warnings, warning)
    _append_unique(suggestions, suggestion)


def _append_unique(items: list[str], item: str) -> None:
    text = _clean_status_text(item)
    if text and text not in items:
        items.append(text)


def _priority_provider_names(items: list[tuple[int, str]]) -> list[str]:
    return _normalized_provider_names([name for _, name in items])


def _providers_by_name(providers: list[ProviderStatus]) -> dict[str, ProviderStatus]:
    by_name: dict[str, ProviderStatus] = {}
    for provider in providers:
        normalized = _normalize_provider_name(provider.name)
        if not normalized:
            continue
        current = by_name.get(normalized)
        if current is None or _provider_status_rank(provider) >= _provider_status_rank(current):
            by_name[normalized] = provider
    return by_name


def _provider_status_rank(provider: ProviderStatus) -> tuple[str, int, int, int, int]:
    return (
        _clean_status_text(provider.updated_at),
        int(provider.healthy),
        int(bool(_clean_status_text(provider.last_success))),
        int(_provider_has_failure_signal(provider)),
        _safe_count(provider.success_count) + _safe_count(provider.failure_count),
    )


def _capabilities_by_name(capabilities: list[ProviderCapability]) -> dict[str, ProviderCapability]:
    return {
        normalized: capability
        for capability in capabilities
        if (normalized := _normalize_provider_name(capability.name))
    }


def _normalized_provider_names(names: list[str]) -> list[str]:
    provider_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = _normalize_provider_name(name)
        if normalized and normalized not in seen:
            provider_names.append(normalized)
            seen.add(normalized)
    return provider_names


SOURCE_HEALTH_WARNING_RULES = (
    SourceHealthWarningRule(
        "capability_failures",
        _unhealthy_capability_warning_labels,
        6,
        "最近失败能力：",
        "优先检查失败能力对应的网络代理、源站连通性或本地授权。",
    ),
    SourceHealthWarningRule(
        "provider_failures",
        _unhealthy_provider_warning_labels,
        5,
        "最近失败源：",
        "优先检查网络代理、源站连通性，或临时依赖当前正常主源。",
    ),
)
PROVIDER_DECISION_RULES = (
    ProviderDecisionRule("cooling", _context_has_cooling, _cooling_provider_decision),
    ProviderDecisionRule("healthy", _context_status_healthy, _healthy_provider_decision_from_context),
    ProviderDecisionRule("enabled_failed", _context_status_enabled, _failed_provider_decision_from_context),
    ProviderDecisionRule("disabled", _context_status_disabled, _disabled_provider_decision),
    ProviderDecisionRule("statusless", _always_matches, _statusless_provider_decision_from_context),
)
