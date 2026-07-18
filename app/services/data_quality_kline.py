from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from app.models.schemas import Kline, KlineQuality
from app.services.data_quality_time import (
    expected_quote_date,
    latest_expected_daily_kline_date,
    market_local_datetime,
    weekday_gap,
)


@dataclass(frozen=True)
class KlineQualityContext:
    source: str | None
    last_date: date | None
    latest_expected: date
    latest_allowed: date
    from_cache: bool
    fallback_used: bool


@dataclass(frozen=True)
class KlineLevelRule:
    name: str
    applies: Callable[[KlineQualityContext, int], bool]
    level: str


@dataclass(frozen=True)
class KlinePenaltyRule:
    name: str
    applies: Callable[[KlineQuality], bool]
    penalty: int
    anomaly: str
    terminal: bool = False


KLINE_LEVEL_RULES = (
    KlineLevelRule("demo_source", lambda context, days: is_demo_kline_source(context.source), "较弱"),
    KlineLevelRule("severely_stale", lambda context, days: days >= 3, "较弱"),
    KlineLevelRule("stale", lambda context, days: days >= 1, "一般"),
    KlineLevelRule("fallback_cache", lambda context, days: context.fallback_used, "一般"),
)
KLINE_PENALTY_RULES = (
    KlinePenaltyRule("missing", lambda quality: quality.level == "缺失", 25, "K线缺失", terminal=True),
    KlinePenaltyRule("invalid_date", lambda quality: quality.last_date is None, 25, "K线日期异常", terminal=True),
    KlinePenaltyRule("future_date", lambda quality: _quality_has_future_date(quality), 25, "K线日期超前"),
    KlinePenaltyRule("severely_stale", lambda quality: _quality_days_behind(quality) >= 5, 30, "K线严重滞后"),
    KlinePenaltyRule("stale", lambda quality: 2 <= _quality_days_behind(quality) < 5, 18, "K线滞后"),
    KlinePenaltyRule("slightly_stale", lambda quality: 1 <= _quality_days_behind(quality) < 2, 8, "K线轻微滞后"),
    KlinePenaltyRule("fallback_cache", lambda quality: quality.fallback_used, 12, "K线兜底缓存"),
    KlinePenaltyRule("demo_source", lambda quality: is_demo_kline_source(quality.source), 35, "演示K线"),
)


def assess_kline_quality(klines: list[Kline], *, now: datetime | None = None) -> KlineQuality:
    context = kline_quality_context(klines, now=now)
    if not klines:
        return _missing_kline_quality(context)
    if context.last_date is None:
        return _invalid_kline_date_quality(context, klines[-1].date)
    if context.last_date > context.latest_allowed:
        return _future_kline_date_quality(context)

    days_behind = _kline_days_behind(context)
    notes = _kline_quality_notes(context, days_behind)
    return _build_kline_quality(
        context,
        level=_kline_quality_level(context, days_behind),
        last_date=context.last_date.isoformat(),
        days_behind=days_behind,
        notes=notes or ["K线日期与当前预期交易日匹配。"],
    )


def kline_quality_context(klines: list[Kline], *, now: datetime | None = None) -> KlineQualityContext:
    current = market_local_datetime(now)
    return KlineQualityContext(
        source=kline_source(klines),
        last_date=latest_kline_date(klines),
        latest_expected=latest_expected_daily_kline_date(current),
        latest_allowed=expected_quote_date(current),
        from_cache=any(item.from_cache for item in klines),
        fallback_used=any(item.fallback_used for item in klines),
    )


def _missing_kline_quality(context: KlineQualityContext) -> KlineQuality:
    return _build_kline_quality(
        context,
        level="缺失",
        last_date=None,
        days_behind=None,
        notes=["缺少K线数据，趋势、买卖点和做T参考都需要降级。"],
    )


def _invalid_kline_date_quality(context: KlineQualityContext, raw_last_date: str | None) -> KlineQuality:
    return _build_kline_quality(
        context,
        level="较弱",
        last_date=raw_last_date,
        days_behind=None,
        notes=["最新K线日期无法识别，趋势参考需要人工复核。"],
    )


def _future_kline_date_quality(context: KlineQualityContext) -> KlineQuality:
    assert context.last_date is not None
    return _build_kline_quality(
        context,
        level="较弱",
        last_date=context.last_date.isoformat(),
        days_behind=None,
        notes=[
            f"K线最新日期为 {context.last_date.isoformat()}，晚于当前可接受交易日 {context.latest_allowed.isoformat()}，需核对数据源日期。"
        ],
    )


def _build_kline_quality(
    context: KlineQualityContext,
    *,
    level: str,
    last_date: str | None,
    days_behind: int | None,
    notes: list[str],
) -> KlineQuality:
    return KlineQuality(
        level=level,
        source=context.source,
        last_date=last_date,
        latest_expected_date=context.latest_expected.isoformat(),
        latest_allowed_date=context.latest_allowed.isoformat(),
        days_behind_expected=days_behind,
        from_cache=context.from_cache,
        fallback_used=context.fallback_used,
        notes=notes,
    )


def _kline_days_behind(context: KlineQualityContext) -> int:
    if context.last_date is not None and context.last_date < context.latest_expected:
        return weekday_gap(context.last_date, context.latest_expected)
    return 0


def _kline_quality_notes(context: KlineQualityContext, days_behind: int) -> list[str]:
    notes: list[str] = []
    if context.last_date is not None and context.last_date < context.latest_expected:
        notes.append(f"K线最新日期为 {context.last_date.isoformat()}，落后预期交易日约 {days_behind} 个交易日。")
    elif context.last_date is not None and context.last_date > context.latest_expected:
        notes.append("K线包含当前交易日盘中数据，收盘前仍需结合实时行情校验。")
    if context.fallback_used:
        notes.append("K线来自兜底缓存，说明实时K线源本轮不可用。")
    elif context.from_cache:
        notes.append("K线来自本地缓存，已按最新预期交易日校验。")
    if is_demo_kline_source(context.source):
        notes.append("K线来源为演示数据，不能作为真实行情依据。")
    return notes


def _kline_quality_level(context: KlineQualityContext, days_behind: int) -> str:
    return next((rule.level for rule in KLINE_LEVEL_RULES if rule.applies(context, days_behind)), "良好")


def kline_quality_penalty(kline_quality: KlineQuality) -> tuple[int, list[str]]:
    penalty = 0
    anomalies: list[str] = []
    for rule in KLINE_PENALTY_RULES:
        if not rule.applies(kline_quality):
            continue
        if rule.terminal:
            return rule.penalty, [rule.anomaly]
        penalty += rule.penalty
        anomalies.append(rule.anomaly)
    return penalty, anomalies


def _quality_days_behind(kline_quality: KlineQuality) -> int:
    return kline_quality.days_behind_expected or 0


def _quality_has_future_date(kline_quality: KlineQuality) -> bool:
    allowed_date_text = kline_quality.latest_allowed_date or kline_quality.latest_expected_date
    if not kline_quality.last_date or not allowed_date_text:
        return False
    try:
        last_date = date.fromisoformat(kline_quality.last_date[:10])
        latest_allowed = date.fromisoformat(allowed_date_text[:10])
    except ValueError:
        return False
    return last_date > latest_allowed


def latest_kline_date(klines: list[Kline]) -> date | None:
    latest = _latest_dated_kline(klines)
    return latest[0] if latest else None


def parse_kline_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def kline_source(klines: list[Kline]) -> str | None:
    latest = _latest_dated_kline(klines)
    if latest and latest[1].source:
        return latest[1].source
    for item in reversed(klines):
        if item.source:
            return item.source
    return None


def is_demo_kline_source(source: str | None) -> bool:
    return bool(source and ("演示" in source or "demo" in source.lower()))


def _latest_dated_kline(klines: list[Kline]) -> tuple[date, Kline] | None:
    latest: tuple[date, Kline] | None = None
    for item in klines:
        parsed = parse_kline_date(item.date)
        if parsed is None:
            continue
        if latest is None or parsed >= latest[0]:
            latest = (parsed, item)
    return latest


__all__ = [
    "assess_kline_quality",
    "is_demo_kline_source",
    "kline_quality_penalty",
    "kline_quality_context",
    "kline_source",
    "latest_kline_date",
    "parse_kline_date",
]
