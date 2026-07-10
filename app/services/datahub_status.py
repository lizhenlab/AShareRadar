from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import math
import re
import socket

from app.models.schemas import ProviderCapability, ProviderCapabilityStatus, ProviderStatus
from app.services.provider_failure_status import capability_recently_failed as provider_capability_recently_failed

KIND_LABELS = {
    "quote": "报价",
    "kline": "日K",
    "minute": "分钟",
    "stock": "股票池",
    "plate": "板块",
    "concept": "概念",
    "order_book": "盘口",
}
DEFAULT_PROVIDER_RECOVERY_ACTION = "稍后自动重试；若持续失败，建议检查依赖安装和网络。"
UNKNOWN_PROVIDER_KEY = "unknown"
UNKNOWN_PROVIDER_LABEL = "未知源"
UNKNOWN_CAPABILITY_LABEL = "未知能力"


@dataclass(frozen=True)
class ProviderRecoveryActionRule:
    name: str
    action: str
    matches: Callable[[str, str], bool]


@dataclass(frozen=True)
class ProviderSourceKeyRule:
    key: str
    needles: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityLabelField:
    kind: str
    field: str
    label: str


@dataclass(frozen=True)
class SourceSummaryTemplate:
    text: str


@dataclass(frozen=True)
class CapabilityStateRule:
    suffix: str
    matches: Callable[[ProviderCapabilityStatus], bool]


def _clean_status_text(value: object | None) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _normalize_provider_name(name: object | None) -> str:
    return _clean_status_text(name).lower()


def _provider_display_name(name: object | None) -> str:
    return _clean_status_text(name) or UNKNOWN_PROVIDER_LABEL


def _normalize_capability_kind(kind: object | None) -> str:
    return _clean_status_text(kind).lower()


def _capability_kind_label(kind: object | None) -> str:
    normalized = _normalize_capability_kind(kind)
    if not normalized:
        return UNKNOWN_CAPABILITY_LABEL
    return KIND_LABELS.get(normalized, normalized)


def _safe_count(value: object | None) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number):
        return 0
    return max(int(number), 0)


def _unique_texts(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique_items: list[str] = []
    for item in items:
        text = _clean_status_text(item)
        if text and text not in seen:
            unique_items.append(text)
            seen.add(text)
    return unique_items


def _provider_source_key(source: str) -> str:
    lowered = _clean_status_text(source).lower()
    if not lowered:
        return UNKNOWN_PROVIDER_KEY
    for rule in PROVIDER_SOURCE_KEY_RULES:
        if any(needle in lowered for needle in rule.needles):
            return rule.key
    return _unknown_provider_source_key(lowered)


def _unknown_provider_source_key(source: str) -> str:
    prefix = source.split("·", 1)[0].strip()
    key = re.sub(r"[^0-9a-z]+", "_", prefix).strip("_")
    return key or UNKNOWN_PROVIDER_KEY


def _capability_status_map(items: list[ProviderCapabilityStatus]) -> dict[tuple[str, str], ProviderCapabilityStatus]:
    statuses: dict[tuple[str, str], ProviderCapabilityStatus] = {}
    for item in items:
        key = (_normalize_provider_name(item.name), _normalize_capability_kind(item.kind))
        current = statuses.get(key)
        if current is None or _capability_status_rank(item) >= _capability_status_rank(current):
            statuses[key] = item
    return statuses


def _capability_status_rank(item: ProviderCapabilityStatus) -> tuple[str, int, int]:
    return (
        _clean_status_text(item.updated_at),
        int(_capability_has_activity(item)),
        _safe_count(item.success_count) + _safe_count(item.failure_count),
    )


def _first_healthy_provider(
    names: list[str],
    providers: dict[str, ProviderStatus],
    capabilities: dict[tuple[str, str], ProviderCapabilityStatus],
    kind: str,
) -> str | None:
    for name in names:
        if _provider_can_be_primary(name, kind, providers, capabilities):
            return name
    return None


def _provider_can_be_primary(
    name: str,
    kind: str,
    providers: dict[str, ProviderStatus],
    capabilities: dict[tuple[str, str], ProviderCapabilityStatus],
) -> bool:
    capability = _capability_status_lookup(capabilities, name, kind)
    if capability is not None and _capability_has_activity(capability):
        return capability.enabled and capability.healthy
    return _provider_status_healthy(_provider_status_lookup(providers, name))


def _provider_status_lookup(providers: dict[str, ProviderStatus], name: str) -> ProviderStatus | None:
    normalized = _normalize_provider_name(name)
    return providers.get(normalized) or providers.get(name) or next(
        (status for provider_name, status in providers.items() if _normalize_provider_name(provider_name) == normalized),
        None,
    )


def _capability_status_lookup(
    capabilities: dict[tuple[str, str], ProviderCapabilityStatus],
    name: str,
    kind: str,
) -> ProviderCapabilityStatus | None:
    normalized_key = (_normalize_provider_name(name), _normalize_capability_kind(kind))
    return capabilities.get(normalized_key) or capabilities.get((name, kind)) or next(
        (
            status
            for (provider_name, capability_kind), status in capabilities.items()
            if (
                _normalize_provider_name(provider_name),
                _normalize_capability_kind(capability_kind),
            )
            == normalized_key
        ),
        None,
    )


def _provider_capability_state(
    name: str,
    quote_names: list[str],
    kline_names: list[str],
    minute_names: list[str],
    statuses: dict[tuple[str, str], ProviderCapabilityStatus],
) -> str:
    kinds = _decision_kinds(name, quote_names, kline_names, minute_names)
    pieces: list[str] = []
    for kind in kinds:
        piece = _capability_state_piece(_capability_status_lookup(statuses, name, kind), kind)
        if piece:
            pieces.append(piece)
    return " / ".join(_unique_texts(pieces))


def _capability_state_piece(status: ProviderCapabilityStatus | None, kind: str) -> str | None:
    if status is None or not status.enabled:
        return None
    label = _capability_kind_label(kind)
    suffix = _capability_state_suffix(status)
    return f"{label}{suffix}" if suffix else None


def _capability_state_suffix(status: ProviderCapabilityStatus) -> str | None:
    for rule in CAPABILITY_STATE_RULES:
        if rule.matches(status):
            return rule.suffix
    return None


def _provider_cooling_kinds(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str], checker) -> list[str]:
    normalized_name = _normalize_provider_name(name)
    return _unique_texts(
        _capability_kind_label(kind)
        for kind in _decision_kinds(name, quote_names, kline_names, minute_names)
        if checker(normalized_name, kind)
    )


def _decision_kinds(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str]) -> list[str]:
    normalized_name = _normalize_provider_name(name)
    priority_names = {
        "quote": {_normalize_provider_name(item) for item in quote_names},
        "kline": {_normalize_provider_name(item) for item in kline_names},
        "minute": {_normalize_provider_name(item) for item in minute_names},
    }
    kinds = [kind for kind in PRIORITY_DECISION_KINDS if normalized_name in priority_names[kind]]
    kinds.extend(EXTRA_DECISION_KINDS.get(normalized_name, []))
    return _unique_texts(kinds)


def _unhealthy_capability_labels(items: list[ProviderCapabilityStatus]) -> list[str]:
    labels: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if provider_capability_recently_failed(item):
            key = (_normalize_provider_name(item.name) or UNKNOWN_PROVIDER_KEY, _normalize_capability_kind(item.kind))
            if key in seen:
                continue
            labels.append(f"{_provider_display_name(item.name)} {_capability_kind_label(item.kind)}")
            seen.add(key)
    return labels


def _capability_has_activity(item: ProviderCapabilityStatus) -> bool:
    return bool(
        _clean_status_text(item.last_success)
        or _clean_status_text(item.last_error)
        or _safe_count(item.success_count)
        or _safe_count(item.failure_count)
    )


def _source_plan_summary(health_level: str, quote: str | None, kline: str | None, minute: str | None) -> str:
    quote_text = _clean_status_text(quote) or "缺失"
    kline_text = _clean_status_text(kline) or "缺失"
    minute_text = _clean_status_text(minute) or "缺失"
    template = SOURCE_SUMMARY_TEMPLATES.get(health_level, SOURCE_SUMMARY_TEMPLATES["高风险"])
    return template.text.format(quote=quote_text, kline=kline_text, minute=minute_text)


def _capability_labels(capability: ProviderCapability | None) -> list[str]:
    if capability is None:
        return []
    return [item.label for item in CAPABILITY_LABEL_FIELDS if getattr(capability, item.field, False)]


def _provider_role(name: str, quote_names: list[str], kline_names: list[str], minute_names: list[str]) -> str:
    normalized_name = _normalize_provider_name(name)
    priority_names = {
        "quote": {_normalize_provider_name(item) for item in quote_names},
        "kline": {_normalize_provider_name(item) for item in kline_names},
        "minute": {_normalize_provider_name(item) for item in minute_names},
    }
    roles = [KIND_LABELS[kind] for kind in PRIORITY_DECISION_KINDS if normalized_name in priority_names[kind]]
    return " / ".join(roles) if roles else "辅助"


def _provider_success_rate(status: ProviderStatus | None) -> float | None:
    if status is None:
        return None
    success_count = _safe_count(status.success_count)
    failure_count = _safe_count(status.failure_count)
    total = success_count + failure_count
    if total <= 0:
        return None
    return round(success_count / total * 100, 1)


def _provider_status_healthy(status: ProviderStatus | None) -> bool:
    return bool(status and status.enabled and status.healthy)


def _provider_recovery_action(name: str, last_error: str | None) -> str:
    provider_name = _normalize_provider_name(name)
    text = _clean_status_text(last_error)
    for rule in PROVIDER_RECOVERY_ACTION_RULES:
        if rule.matches(provider_name, text):
            return rule.action
    return DEFAULT_PROVIDER_RECOVERY_ACTION


def _error_contains_any(*needles: str) -> Callable[[str, str], bool]:
    return lambda _name, text: any(needle.lower() in text.lower() for needle in needles)


def _provider_is(target_name: str) -> Callable[[str, str], bool]:
    target = _normalize_provider_name(target_name)
    return lambda name, _text: _normalize_provider_name(name) == target


def _capability_is_unprobed(status: ProviderCapabilityStatus) -> bool:
    return not _capability_has_activity(status)


def _capability_is_healthy(status: ProviderCapabilityStatus) -> bool:
    return status.healthy


def _capability_recently_failed(status: ProviderCapabilityStatus) -> bool:
    return bool(_clean_status_text(status.last_error))


PROVIDER_SOURCE_KEY_RULES = (
    ProviderSourceKeyRule("tencent", ("腾讯", "tencent")),
    ProviderSourceKeyRule("akshare", ("akshare",)),
    ProviderSourceKeyRule("tushare", ("tushare",)),
    ProviderSourceKeyRule("baostock", ("baostock",)),
    ProviderSourceKeyRule("futu", ("futu", "富途")),
    ProviderSourceKeyRule("demo", ("演示", "demo")),
)
CAPABILITY_LABEL_FIELDS = (
    CapabilityLabelField("quote", "realtime_quote", "实时报价"),
    CapabilityLabelField("kline", "daily_kline", "日K"),
    CapabilityLabelField("minute", "minute_kline", "分钟线"),
    CapabilityLabelField("stock", "stock_pool", "股票池"),
    CapabilityLabelField("plate", "plate_rank", "行业板块"),
    CapabilityLabelField("concept", "concept_board", "概念"),
    CapabilityLabelField("order_book", "order_book", "盘口"),
)
PRIORITY_DECISION_KINDS = ("quote", "kline", "minute")
EXTRA_DECISION_KINDS = {"futu": ["order_book"]}
SOURCE_SUMMARY_TEMPLATES = {
    "健康": SourceSummaryTemplate(
        text="数据链路健康：报价主源 {quote}，日K主源 {kline}，分钟线主源 {minute}。",
    ),
    "降级可用": SourceSummaryTemplate(
        text="数据链路降级但仍可分析：报价主源 {quote}，日K主源 {kline}，分钟线 {minute}。",
    ),
    "高风险": SourceSummaryTemplate(
        text="数据链路高风险：报价 {quote}，日K {kline}，分钟线 {minute}，结论需要谨慎。",
    ),
}
CAPABILITY_STATE_RULES = (
    CapabilityStateRule("未探测", _capability_is_unprobed),
    CapabilityStateRule("正常", _capability_is_healthy),
    CapabilityStateRule("最近失败", _capability_recently_failed),
)
PROVIDER_RECOVERY_ACTION_RULES = (
    ProviderRecoveryActionRule(
        "proxy_or_network",
        "检查网络代理或源站连通性；冷却期内系统会先使用其他源或缓存。",
        _error_contains_any("ProxyError", "Unable to connect to proxy", "HTTPSConnectionPool"),
    ),
    ProviderRecoveryActionRule(
        "remote_disconnected",
        "东方财富源站主动断开连接；系统会先使用 Tencent、BaoStock 或缓存，稍后自动重试。",
        _error_contains_any("RemoteDisconnected", "Connection aborted"),
    ),
    ProviderRecoveryActionRule(
        "timeout",
        "源站响应超时；系统会先使用其他源或缓存，稍后自动重试。",
        _error_contains_any("TimeoutError", "timeout", "超时"),
    ),
    ProviderRecoveryActionRule(
        "baostock_backup",
        "BaoStock 偏历史备份，失败时先依赖 Tencent/AKShare 日K缓存。",
        _provider_is("baostock"),
    ),
    ProviderRecoveryActionRule(
        "futu_opend",
        "确认 Futu OpenD 已启动，并设置 ASHARE_RADAR_FUTU_ENABLED=1。",
        _provider_is("futu"),
    ),
    ProviderRecoveryActionRule(
        "tushare_token",
        "配置 ASHARE_RADAR_TUSHARE_TOKEN 后再启用。",
        _provider_is("tushare"),
    ),
)


def _provider_error_text(exc: Exception) -> str:
    text = _clean_status_text(exc)
    if text:
        return text
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return "TimeoutError: 数据源响应超时"
    if isinstance(exc, socket.timeout):
        return "TimeoutError: 网络请求超时"
    return exc.__class__.__name__
