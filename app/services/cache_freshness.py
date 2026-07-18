from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.models.system import CacheStats
from app.services import trading_calendar
from app.services.data_quality_time import normalize_quote_event_time


ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
QUOTE_FETCH_RECENT_SECONDS = 15 * 60
DAILY_KLINE_FETCH_RECENT_SECONDS = 24 * 60 * 60
MINUTE_KLINE_FETCH_RECENT_SECONDS = 15 * 60
DEFAULT_STOCK_POOL_CACHE_SECONDS = 24 * 60 * 60
DEFAULT_PLATE_RANK_CACHE_SECONDS = 10 * 60


@dataclass(frozen=True)
class FreshnessObservation:
    status: str
    observed_at: str | None
    age_seconds: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FreshnessIssue:
    category: str
    semantic: str
    status: str
    message: str
    suggestion: str


@dataclass(frozen=True)
class DomainFreshness:
    key: str
    label: str
    category: str
    fetch_activity: FreshnessObservation
    market_freshness: FreshnessObservation
    suggestion: str
    checked: bool = True


@dataclass(frozen=True)
class CacheFreshnessAssessment:
    domains: tuple[DomainFreshness, ...]
    availability_issues: tuple[FreshnessIssue, ...]
    checked_domains: tuple[str, ...]

    @property
    def issues(self) -> tuple[FreshnessIssue, ...]:
        issues: list[FreshnessIssue] = []
        for domain in self.domains:
            fetch = domain.fetch_activity
            if fetch.status in {"future", "invalid"} and fetch.detail:
                issues.append(
                    FreshnessIssue(
                        category=domain.category,
                        semantic="fetch_activity",
                        status=fetch.status,
                        message=fetch.detail,
                        suggestion="检查系统时间、抓取时间字段或清理异常缓存。",
                    )
                )
            market = domain.market_freshness
            if domain.checked and market.status != "fresh" and market.detail:
                issues.append(
                    FreshnessIssue(
                        category=domain.category,
                        semantic="market_freshness",
                        status=market.status,
                        message=market.detail,
                        suggestion=domain.suggestion,
                    )
                )
        return (*issues, *self.availability_issues)

    @property
    def fetch_activity(self) -> dict[str, FreshnessObservation]:
        return {domain.key: domain.fetch_activity for domain in self.domains if domain.checked}

    @property
    def market_freshness(self) -> dict[str, FreshnessObservation]:
        return {domain.key: domain.market_freshness for domain in self.domains if domain.checked}


def assess_cache_freshness(
    cache: CacheStats,
    *,
    now: datetime,
    stock_pool_cache_seconds: int = DEFAULT_STOCK_POOL_CACHE_SECONDS,
    plate_rank_cache_seconds: int = DEFAULT_PLATE_RANK_CACHE_SECONDS,
) -> CacheFreshnessAssessment:
    current = _local_naive_datetime(now)
    domains = (
        *_market_data_domains(cache, current),
        *_metadata_domains(
            cache,
            current,
            stock_pool_cache_seconds=stock_pool_cache_seconds,
            plate_rank_cache_seconds=plate_rank_cache_seconds,
        ),
    )
    availability_issues = _metadata_availability_issues(cache)
    checked_domains = tuple(domain.label for domain in domains if domain.checked) + (
        "股票池可用性",
        "行业背景可用性",
    )
    return CacheFreshnessAssessment(
        domains=domains,
        availability_issues=availability_issues,
        checked_domains=checked_domains,
    )


def _market_data_domains(cache: CacheStats, current: datetime) -> tuple[DomainFreshness, ...]:
    quote_fetch = _fetch_value(cache, "latest_quote_fetched_at", "latest_quote_at")
    daily_fetch = _fetch_value(
        cache,
        "latest_daily_kline_fetched_at",
        "latest_daily_kline_at",
        "latest_kline_at",
    )
    minute_fetch = _fetch_value(cache, "latest_minute_kline_fetched_at", "latest_minute_kline_at")
    quote_event = getattr(cache, "latest_quote_timestamp", None)
    daily_event = getattr(cache, "latest_daily_kline_date", None)
    minute_event = getattr(cache, "latest_minute_kline_timestamp", None)
    minute_checked = bool(getattr(cache, "minute_kline_count", 0) or minute_event or minute_fetch)
    return (
        DomainFreshness(
            key="quote",
            label="报价市场时效",
            category="quote",
            fetch_activity=_fetch_activity(quote_fetch, current, QUOTE_FETCH_RECENT_SECONDS, "报价"),
            market_freshness=_quote_market_freshness(quote_event, current),
            suggestion="刷新报价并检查行情源返回的市场事件时间。",
        ),
        DomainFreshness(
            key="daily_kline",
            label="日K市场时效",
            category="kline",
            fetch_activity=_fetch_activity(daily_fetch, current, DAILY_KLINE_FETCH_RECENT_SECONDS, "日K"),
            market_freshness=_daily_kline_market_freshness(daily_event, current),
            suggestion="执行关键个股日K刷新，并检查数据源交易日期。",
        ),
        DomainFreshness(
            key="minute_kline",
            label="分钟K市场时效",
            category="minute",
            fetch_activity=_fetch_activity(minute_fetch, current, MINUTE_KLINE_FETCH_RECENT_SECONDS, "分钟K"),
            market_freshness=(
                _minute_kline_market_freshness(minute_event, current)
                if minute_checked
                else FreshnessObservation("not_checked", None)
            ),
            suggestion="刷新分钟K线，并检查数据源返回的市场事件时间。",
            checked=minute_checked,
        ),
    )


def _metadata_domains(
    cache: CacheStats,
    current: datetime,
    *,
    stock_pool_cache_seconds: int,
    plate_rank_cache_seconds: int,
) -> tuple[DomainFreshness, ...]:
    stock_updated = getattr(cache, "latest_stock_at", None)
    plate_updated = getattr(cache, "latest_plate_at", None)
    # A non-empty cache without an aggregate timestamp must be reported as
    # indeterminate instead of being silently excluded from health checks.
    stock_checked = getattr(cache, "stock_count", 0) > 0
    plate_checked = getattr(cache, "plate_count", 0) > 0
    stock_ttl = _positive_seconds(stock_pool_cache_seconds, DEFAULT_STOCK_POOL_CACHE_SECONDS)
    plate_ttl = _positive_seconds(plate_rank_cache_seconds, DEFAULT_PLATE_RANK_CACHE_SECONDS)
    return (
        DomainFreshness(
            key="stock",
            label="股票池缓存时效",
            category="stock",
            fetch_activity=_fetch_activity(stock_updated, current, stock_ttl, "股票池"),
            market_freshness=_cache_entry_freshness(stock_updated, current, stock_ttl, "股票池"),
            suggestion="刷新股票池，并检查数据源返回的更新时间。",
            checked=stock_checked,
        ),
        DomainFreshness(
            key="plate",
            label="行业背景缓存时效",
            category="plate",
            fetch_activity=_fetch_activity(plate_updated, current, plate_ttl, "行业背景"),
            market_freshness=_cache_entry_freshness(plate_updated, current, plate_ttl, "行业背景"),
            suggestion="执行行业背景刷新，并检查板块数据源。",
            checked=plate_checked,
        ),
    )


def _fetch_activity(value: object, now: datetime, recent_seconds: int, label: str) -> FreshnessObservation:
    text = _optional_text(value)
    if text is None:
        return FreshnessObservation("missing", None)
    parsed = _parse_datetime(text)
    if parsed is None:
        return FreshnessObservation("invalid", text, detail=f"{label}抓取时间无法解析。")
    age = int((now - parsed).total_seconds())
    if age < 0:
        return FreshnessObservation(
            "future",
            text,
            detail=f"{label}抓取时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')} 晚于检查时间。",
        )
    status = "recent" if age <= recent_seconds else "idle"
    return FreshnessObservation(status, text, age_seconds=age)


def _cache_entry_freshness(value: object, now: datetime, max_age_seconds: int, label: str) -> FreshnessObservation:
    text = _optional_text(value)
    if text is None:
        return FreshnessObservation("missing", None, detail=f"{label}缓存存在但缺少更新时间，无法判断缓存时效。")
    parsed = _parse_datetime(text)
    if parsed is None:
        return FreshnessObservation("invalid", text, detail=f"{label}缓存更新时间无法解析。")
    age = int((now - parsed).total_seconds())
    if age < 0:
        return FreshnessObservation(
            "future",
            text,
            detail=f"{label}缓存更新时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')} 晚于检查时间。",
        )
    if age > max_age_seconds:
        return FreshnessObservation(
            "stale",
            text,
            age_seconds=age,
            detail=f"{label}缓存已过期：最近更新时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')}。",
        )
    return FreshnessObservation("fresh", text, age_seconds=age)


def _quote_market_freshness(value: object, now: datetime) -> FreshnessObservation:
    text = _optional_text(value)
    if text is None:
        return FreshnessObservation(
            "missing",
            None,
            detail="尚未形成报价缓存或市场事件时间，无法判断报价业务新鲜度。",
        )
    parsed = _parse_quote_datetime(text)
    if parsed is None:
        return FreshnessObservation("invalid", text, detail="报价市场事件时间无法解析。")
    age = int((now - parsed).total_seconds())
    if age < 0:
        return FreshnessObservation(
            "future",
            text,
            detail=f"报价市场事件时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')} 晚于检查时间。",
        )
    if not trading_calendar.is_trading_day(parsed.date()):
        return FreshnessObservation(
            "invalid",
            text,
            age_seconds=age,
            detail=f"报价市场事件日期 {parsed.date().isoformat()} 不是 A 股交易日。",
        )
    expected = trading_calendar.expected_quote_date(now)
    if parsed.date() > expected:
        return FreshnessObservation(
            "future",
            text,
            age_seconds=age,
            detail=f"报价市场事件日期 {parsed.date().isoformat()} 晚于当前应参考交易日 {expected.isoformat()}。",
        )
    if parsed.date() < expected:
        return FreshnessObservation(
            "stale",
            text,
            age_seconds=age,
            detail=(
                f"报价市场数据过期：最新日期 {parsed.date().isoformat()}，"
                f"应覆盖 {expected.isoformat()}。"
            ),
        )
    if not _market_event_snapshot_is_fresh(parsed, now, expected):
        return FreshnessObservation(
            "stale",
            text,
            age_seconds=age,
            detail=f"报价市场数据过期：最新事件时间为 {parsed.strftime('%Y-%m-%d %H:%M:%S')}。",
        )
    return FreshnessObservation("fresh", text, age_seconds=age)


def _daily_kline_market_freshness(value: object, now: datetime) -> FreshnessObservation:
    text = _optional_text(value)
    if text is None:
        return FreshnessObservation(
            "missing",
            None,
            detail="尚未形成日K缓存或市场日期，无法判断日K业务新鲜度。",
        )
    market_date = _parse_date(text)
    if market_date is None:
        return FreshnessObservation("invalid", text, detail="日K市场日期无法解析。")
    observed_at = market_date.isoformat()
    age = int((now - datetime.combine(market_date, datetime.min.time())).total_seconds())
    if market_date > now.date():
        return FreshnessObservation("future", observed_at, detail=f"日K市场日期 {observed_at} 晚于检查日期。")
    if not trading_calendar.is_trading_day(market_date):
        return FreshnessObservation(
            "invalid",
            observed_at,
            age_seconds=max(0, age),
            detail=f"日K市场日期 {observed_at} 不是 A 股交易日。",
        )
    expected = trading_calendar.expected_quote_date(now)
    latest_required = trading_calendar.latest_expected_daily_kline_date(now)
    if market_date > expected:
        return FreshnessObservation(
            "future",
            observed_at,
            age_seconds=max(0, age),
            detail=f"日K市场日期 {observed_at} 晚于当前应参考交易日 {expected.isoformat()}。",
        )
    if market_date < latest_required:
        return FreshnessObservation(
            "stale",
            observed_at,
            age_seconds=max(0, age),
            detail=f"日K市场数据过期：最新日期 {observed_at}，应至少覆盖 {latest_required.isoformat()}。",
        )
    return FreshnessObservation("fresh", observed_at, age_seconds=max(0, age))


def _minute_kline_market_freshness(value: object, now: datetime) -> FreshnessObservation:
    text = _optional_text(value)
    if text is None:
        return FreshnessObservation(
            "missing",
            None,
            detail="分钟K缓存存在但缺少市场事件时间，无法判断分钟K业务新鲜度。",
        )
    parsed = _parse_quote_datetime(text)
    if parsed is None:
        return FreshnessObservation("invalid", text, detail="分钟K市场事件时间无法解析。")
    age = int((now - parsed).total_seconds())
    if age < 0:
        return FreshnessObservation(
            "future",
            text,
            detail=f"分钟K市场事件时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')} 晚于检查时间。",
        )
    if not trading_calendar.is_trading_day(parsed.date()):
        return FreshnessObservation(
            "invalid",
            text,
            age_seconds=age,
            detail=f"分钟K市场事件日期 {parsed.date().isoformat()} 不是 A 股交易日。",
        )
    expected = trading_calendar.expected_quote_date(now)
    if parsed.date() > expected:
        return FreshnessObservation(
            "future",
            text,
            age_seconds=age,
            detail=f"分钟K市场事件日期 {parsed.date().isoformat()} 晚于当前应参考交易日 {expected.isoformat()}。",
        )
    if parsed.date() < expected:
        return FreshnessObservation(
            "stale",
            text,
            age_seconds=age,
            detail=f"分钟K市场数据过期：最新日期 {parsed.date().isoformat()}，应覆盖 {expected.isoformat()}。",
        )
    if _market_event_snapshot_is_fresh(parsed, now, expected):
        return FreshnessObservation("fresh", text, age_seconds=age)
    return FreshnessObservation(
        "stale",
        text,
        age_seconds=age,
        detail=f"分钟K市场数据过期：最新事件时间为 {parsed.strftime('%Y-%m-%d %H:%M:%S')}。",
    )


def _market_event_snapshot_is_fresh(latest: datetime, now: datetime, expected: date) -> bool:
    latest_time = latest.time()
    if expected != now.date():
        return latest_time >= trading_calendar.CLOSING_SNAPSHOT_START_TIME

    phase = trading_calendar.market_session_phase(now)
    live = now - latest <= trading_calendar.LIVE_MARKET_EVENT_MAX_DELAY
    if phase is trading_calendar.MarketSessionPhase.CALL_AUCTION:
        return trading_calendar.CALL_AUCTION_START_TIME <= latest_time and live
    if phase is trading_calendar.MarketSessionPhase.MORNING:
        return (
            trading_calendar.CALL_AUCTION_START_TIME <= latest_time <= trading_calendar.MORNING_SESSION_END_TIME
            and live
        )
    if phase is trading_calendar.MarketSessionPhase.MIDDAY_BREAK:
        return (
            trading_calendar.MORNING_CLOSE_SNAPSHOT_START_TIME
            <= latest_time
            <= trading_calendar.MORNING_SESSION_END_TIME
        )
    if phase is trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE:
        morning_close_snapshot = (
            trading_calendar.MORNING_CLOSE_SNAPSHOT_START_TIME
            <= latest_time
            <= trading_calendar.MORNING_SESSION_END_TIME
        )
        afternoon_live = trading_calendar.AFTERNOON_SESSION_START_TIME <= latest_time and live
        return morning_close_snapshot or afternoon_live
    if phase is trading_calendar.MarketSessionPhase.AFTERNOON:
        return (
            trading_calendar.AFTERNOON_SESSION_START_TIME <= latest_time <= trading_calendar.MARKET_CLOSE_TIME
            and live
        )
    if phase in {
        trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
        trading_calendar.MarketSessionPhase.AFTER_CLOSE,
    }:
        return latest_time >= trading_calendar.CLOSING_SNAPSHOT_START_TIME
    return False


def _metadata_availability_issues(cache: CacheStats) -> tuple[FreshnessIssue, ...]:
    issues: list[FreshnessIssue] = []
    if getattr(cache, "stock_count", 0) <= 0:
        issues.append(
            FreshnessIssue(
                category="stock",
                semantic="availability",
                status="missing",
                message="尚未形成股票池缓存。",
                suggestion="刷新股票池，确认基础资料数据源已写入缓存。",
            )
        )
    if getattr(cache, "plate_count", 0) <= 0:
        issues.append(
            FreshnessIssue(
                category="plate",
                semantic="availability",
                status="missing",
                message="尚未形成行业背景缓存。",
                suggestion="执行行业背景刷新，确认板块或概念数据已写入缓存。",
            )
        )
    return tuple(issues)


def _fetch_value(cache: CacheStats, explicit_attr: str, *legacy_attrs: str) -> object:
    explicit = getattr(cache, explicit_attr, None)
    if explicit is not None:
        return explicit
    fields_set = getattr(cache, "model_fields_set", set())
    if explicit_attr in fields_set:
        return None
    for attr in legacy_attrs:
        value = getattr(cache, attr, None)
        if value is not None:
            return value
    return None


def _positive_seconds(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


def _parse_quote_datetime(value: str) -> datetime | None:
    normalized = normalize_quote_event_time(value)
    return _parse_datetime(normalized) if normalized is not None else None


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    return _local_naive_datetime(parsed)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip().replace("/", "-"))
    except (TypeError, ValueError):
        return None


def _local_naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=None)
    return value.astimezone(ASHARE_TIMEZONE).replace(tzinfo=None)


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


__all__ = [
    "CacheFreshnessAssessment",
    "DomainFreshness",
    "FreshnessIssue",
    "FreshnessObservation",
    "assess_cache_freshness",
]
