from __future__ import annotations

from app.models.schemas import IndividualReview, Kline, Quote, ReviewEvent, ReviewPoint
from app.services.indicators import max_drawdown, pct_change, trend_days, volatility


def build_individual_review(quote: Quote, klines: list[Kline], period_days: int = 60) -> IndividualReview:
    rows = klines[-period_days:] if len(klines) > period_days else klines
    if len(rows) < 2:
        return IndividualReview(
            symbol=f"{quote.code}.{quote.market}",
            code=quote.code,
            market=quote.market,
            name=quote.name,
            period_days=len(rows),
            latest_close=quote.price,
            return_pct=0,
            max_drawdown_pct=0,
            volatility_pct=0,
            positive_days=0,
            negative_days=0,
            trend_days=0,
            review_label="数据不足",
            review_summary="历史K线不足，暂不做复盘判断。",
            key_points=[],
            events=[],
        )

    closes = [item.close for item in rows]
    returns = [pct_change(closes[index], closes[index - 1]) for index in range(1, len(closes))]
    return_pct = pct_change(closes[-1], closes[0])
    max_drawdown_pct = max_drawdown(closes)
    volatility_pct = volatility(returns)
    positive_days = sum(1 for item in returns if item > 0)
    negative_days = sum(1 for item in returns if item < 0)
    days_above_ma5 = trend_days(rows)
    label = _review_label(return_pct, max_drawdown_pct, days_above_ma5)
    events = _review_events(rows)
    key_points = [
        ReviewPoint(label="区间涨跌", value=f"{return_pct:.2f}%", level=_level_by_return(return_pct)),
        ReviewPoint(label="最大回撤", value=f"{max_drawdown_pct:.2f}%", level="风险" if max_drawdown_pct < -12 else "观察"),
        ReviewPoint(label="日波动", value=f"{volatility_pct:.2f}%", level="风险" if volatility_pct > 3.5 else "观察"),
        ReviewPoint(label="站上5日线天数", value=f"{days_above_ma5}天", level="积极" if days_above_ma5 >= 8 else "观察"),
    ]
    summary = (
        f"近{len(rows)}个交易日，{quote.name}区间涨跌 {return_pct:.2f}%，最大回撤 {max_drawdown_pct:.2f}%，"
        f"上涨天数 {positive_days} 天、下跌天数 {negative_days} 天。复盘标签为「{label}」。"
    )
    return IndividualReview(
        symbol=f"{quote.code}.{quote.market}",
        code=quote.code,
        market=quote.market,
        name=quote.name,
        period_days=len(rows),
        latest_close=closes[-1],
        return_pct=round(return_pct, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        volatility_pct=round(volatility_pct, 2),
        positive_days=positive_days,
        negative_days=negative_days,
        trend_days=days_above_ma5,
        review_label=label,
        review_summary=summary,
        key_points=key_points,
        events=events,
    )


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
    if len(rows) < 2:
        return events
    for index in range(1, len(rows)):
        prev = rows[index - 1]
        curr = rows[index]
        change = pct_change(curr.close, prev.close)
        amplitude = pct_change(curr.high, curr.low)
        if change >= 4:
            events.append(
                ReviewEvent(
                    date=curr.date,
                    title="放量上攻日",
                    description=f"收盘上涨 {change:.2f}%，可回看当日是否伴随成交放大。",
                    level="积极",
                )
            )
        elif change <= -4:
            events.append(
                ReviewEvent(
                    date=curr.date,
                    title="明显回撤日",
                    description=f"收盘下跌 {change:.2f}%，需要关注后续是否修复。",
                    level="风险",
                )
            )
        elif amplitude >= 6:
            events.append(
                ReviewEvent(
                    date=curr.date,
                    title="高波动日",
                    description=f"日内振幅 {amplitude:.2f}%，适合复盘支撑压力是否有效。",
                    level="观察",
                )
            )
    return events[-8:]
