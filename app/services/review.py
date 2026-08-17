from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from app.models.analysis import (
    IndividualReview,
    ReviewEvent,
    ReviewPoint,
)
from app.models.market import (
    Kline,
    Quote,
)
from app.services.indicators import max_drawdown, pct_change, trend_days, volatility
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.research_replay import completed_daily_bar_cutoff
from app.utils.clock import market_now_naive
from app.utils.market_time import market_local_naive, normalize_market_datetime


MIN_REVIEW_ROWS = 2
MAX_REVIEW_EVENTS = 8
REVIEW_WICK_EVENT_THRESHOLD = 6
REVIEW_VOLUME_SURGE_RATIO = 1.5
REVIEW_VOLUME_LOOKBACK_DAYS = 5


@dataclass(frozen=True)
class ReviewMetrics:
    rows: list[Kline]
    closes: list[float]
    returns: list[float]
    return_pct: float
    max_drawdown_pct: float
    volatility_pct: float
    positive_days: int
    negative_days: int
    days_above_ma5: int
    label: str


@dataclass(frozen=True)
class ReviewEventContext:
    current: Kline
    change_pct: float
    amplitude_pct: float
    volume_ratio_5d: float | None


@dataclass(frozen=True)
class ReviewEventRule:
    title: str
    level: str
    matches: Callable[[ReviewEventContext], bool]
    description: Callable[[ReviewEventContext], str]


REVIEW_EVENT_RULES: tuple[ReviewEventRule, ...] = (
    ReviewEventRule(
        title="放量上攻日",
        level="积极",
        matches=lambda context: (
            context.change_pct >= 4
            and context.volume_ratio_5d is not None
            and context.volume_ratio_5d >= REVIEW_VOLUME_SURGE_RATIO
        ),
        description=lambda context: (
            f"收盘上涨 {context.change_pct:.2f}%，成交量为前5日均量的 {context.volume_ratio_5d:.2f} 倍。"
        ),
    ),
    ReviewEventRule(
        title="明显上涨日",
        level="积极",
        matches=lambda context: context.change_pct >= 4,
        description=lambda context: f"收盘上涨 {context.change_pct:.2f}%，但未达到放量确认口径。",
    ),
    ReviewEventRule(
        title="明显回撤日",
        level="风险",
        matches=lambda context: context.change_pct <= -4,
        description=lambda context: f"收盘下跌 {context.change_pct:.2f}%，需要关注后续是否修复。",
    ),
    ReviewEventRule(
        title="高波动日",
        level="观察",
        matches=lambda context: context.amplitude_pct >= REVIEW_WICK_EVENT_THRESHOLD,
        description=lambda context: f"日内振幅 {context.amplitude_pct:.2f}%，适合复盘支撑压力是否有效。",
    ),
)


def build_individual_review(
    quote: Quote,
    klines: list[Kline],
    period_days: int = 60,
    *,
    as_of: datetime | None = None,
    now: datetime | None = None,
) -> IndividualReview:
    trusted_as_of = _review_as_of(quote, as_of, now=now)
    if trusted_as_of is None:
        return _insufficient_review(quote, 0, "复盘截止时间不可验证，暂不计算历史表现。")
    rows = _review_rows(klines, period_days, as_of=trusted_as_of)
    if len(rows) < MIN_REVIEW_ROWS:
        return _insufficient_review(quote, len(rows))
    if issue := factor_calibration_evidence_issue(rows):
        return _insufficient_review(quote, 0, f"历史日K执行证据不可用：{issue}。")
    metrics = _review_metrics(rows)
    return IndividualReview(
        symbol=f"{quote.code}.{quote.market}",
        code=quote.code,
        market=quote.market,
        name=quote.name,
        period_days=len(rows),
        latest_close=metrics.closes[-1],
        return_pct=round(metrics.return_pct, 2),
        max_drawdown_pct=round(metrics.max_drawdown_pct, 2),
        volatility_pct=round(metrics.volatility_pct, 2),
        positive_days=metrics.positive_days,
        negative_days=metrics.negative_days,
        trend_days=metrics.days_above_ma5,
        review_label=metrics.label,
        review_summary=_review_summary(quote, metrics),
        key_points=_review_key_points(metrics),
        events=_review_events(rows),
    )


def _review_rows(
    klines: list[Kline],
    period_days: int,
    *,
    as_of: datetime,
) -> list[Kline]:
    if period_days <= 0:
        return []
    cutoff = completed_daily_bar_cutoff(as_of)
    rows = [
        item
        for item in klines
        if (row_date := _strict_review_date(item.date)) is None or row_date <= cutoff
    ]
    rows = rows[-period_days:] if len(rows) > period_days else rows
    return rows


def _strict_review_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def _review_as_of(
    quote: Quote,
    value: datetime | None,
    *,
    now: datetime | None,
) -> datetime | None:
    current = market_local_naive(now) if now is not None else market_now_naive()
    normalized = normalize_market_datetime(quote.timestamp)
    if normalized is None:
        return None
    quote_time = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
    decision_time = market_local_naive(value) if value is not None else quote_time
    if quote_time > decision_time or decision_time > current:
        return None
    return decision_time


def _valid_review_bar(row: Kline) -> bool:
    return min(row.open, row.close, row.high, row.low) > 0 and row.low <= row.close <= row.high


def _insufficient_review(
    quote: Quote,
    row_count: int,
    reason: str = "有效历史K线不足，暂不做复盘判断。",
) -> IndividualReview:
    return IndividualReview(
        symbol=f"{quote.code}.{quote.market}",
        code=quote.code,
        market=quote.market,
        name=quote.name,
        period_days=row_count,
        latest_close=quote.price,
        return_pct=0,
        max_drawdown_pct=0,
        volatility_pct=0,
        positive_days=0,
        negative_days=0,
        trend_days=0,
        review_label="数据不足",
        review_summary=reason,
        key_points=[],
        events=[],
    )


def _review_metrics(rows: list[Kline]) -> ReviewMetrics:
    closes = [item.close for item in rows]
    returns = _review_returns(closes)
    return_pct = pct_change(closes[-1], closes[0])
    max_drawdown_pct = max_drawdown(closes)
    volatility_pct = volatility(returns)
    positive_days = sum(1 for item in returns if item > 0)
    negative_days = sum(1 for item in returns if item < 0)
    days_above_ma5 = trend_days(rows)
    return ReviewMetrics(
        rows=rows,
        closes=closes,
        returns=returns,
        return_pct=return_pct,
        max_drawdown_pct=max_drawdown_pct,
        volatility_pct=volatility_pct,
        positive_days=positive_days,
        negative_days=negative_days,
        days_above_ma5=days_above_ma5,
        label=_review_label(return_pct, max_drawdown_pct, days_above_ma5),
    )


def _review_returns(closes: list[float]) -> list[float]:
    return [pct_change(closes[index], closes[index - 1]) for index in range(1, len(closes)) if closes[index - 1] > 0]


def _review_summary(quote: Quote, metrics: ReviewMetrics) -> str:
    return (
        f"近{len(metrics.rows)}个有效交易日，{quote.name}区间涨跌 {metrics.return_pct:.2f}%，最大回撤 {metrics.max_drawdown_pct:.2f}%，"
        f"上涨天数 {metrics.positive_days} 天、下跌天数 {metrics.negative_days} 天。复盘标签为「{metrics.label}」。"
    )


def _review_key_points(metrics: ReviewMetrics) -> list[ReviewPoint]:
    return [
        ReviewPoint(label="区间涨跌", value=f"{metrics.return_pct:.2f}%", level=_level_by_return(metrics.return_pct)),
        ReviewPoint(label="最大回撤", value=f"{metrics.max_drawdown_pct:.2f}%", level="风险" if metrics.max_drawdown_pct < -12 else "观察"),
        ReviewPoint(label="日波动", value=f"{metrics.volatility_pct:.2f}%", level="风险" if metrics.volatility_pct > 3.5 else "观察"),
        ReviewPoint(label="站上5日线天数", value=f"{metrics.days_above_ma5}天", level="积极" if metrics.days_above_ma5 >= 8 else "观察"),
    ]


def _review_label(return_pct: float, max_drawdown_pct: float, days_above_ma5: int) -> str:
    if return_pct > 8 and max_drawdown_pct > -10 and days_above_ma5 >= 10:
        return "趋势复盘较好"
    if max_drawdown_pct < -18:
        return "回撤压力较大"
    if return_pct < -8:
        return "阶段偏弱"
    return "震荡观察"


def _level_by_return(value: float) -> str:
    if value > 5:
        return "积极"
    if value < -5:
        return "风险"
    return "观察"


def _review_events(rows: list[Kline]) -> list[ReviewEvent]:
    events: list[ReviewEvent] = []
    valid_rows = [item for item in rows if _valid_review_bar(item)]
    if len(valid_rows) < MIN_REVIEW_ROWS:
        return events
    for index in range(1, len(valid_rows)):
        if event := _review_event_at(valid_rows, index):
            events.append(event)
    return events[-MAX_REVIEW_EVENTS:]


def _review_event_at(rows: list[Kline], index: int) -> ReviewEvent | None:
    prev = rows[index - 1]
    current = rows[index]
    context = ReviewEventContext(
        current=current,
        change_pct=pct_change(current.close, prev.close),
        amplitude_pct=pct_change(current.high, current.low),
        volume_ratio_5d=_review_volume_ratio(rows, index),
    )
    return next((_event_from_rule(context, rule) for rule in REVIEW_EVENT_RULES if rule.matches(context)), None)


def _review_volume_ratio(rows: list[Kline], index: int) -> float | None:
    start = index - REVIEW_VOLUME_LOOKBACK_DAYS
    if start < 0 or _invalid_review_volume(rows[index].volume):
        return None
    volumes = [row.volume for row in rows[start:index]]
    if len(volumes) != REVIEW_VOLUME_LOOKBACK_DAYS or any(_invalid_review_volume(value) for value in volumes):
        return None
    average = sum(volumes) / len(volumes)
    return rows[index].volume / average if average > 0 else None


def _invalid_review_volume(value: object) -> bool:
    return (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not isfinite(value)
        or value <= 0
    )


def _event_from_rule(context: ReviewEventContext, rule: ReviewEventRule) -> ReviewEvent:
    return ReviewEvent(
        date=context.current.date,
        title=rule.title,
        description=rule.description(context),
        level=rule.level,
    )
