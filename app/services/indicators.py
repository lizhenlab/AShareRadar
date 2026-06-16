from __future__ import annotations

from statistics import mean

from app.models.schemas import Kline, Quote, SignalContribution


def moving_average(klines: list[Kline], window: int) -> float:
    if not klines:
        return 0
    values = [item.close for item in klines[-window:]]
    return round(mean(values), 2)


def trend_score(quote: Quote, klines: list[Kline]) -> tuple[int, str]:
    score, label, _ = trend_score_snapshot(quote, klines)
    return score, label


def trend_score_snapshot(quote: Quote, klines: list[Kline]) -> tuple[int, str, list[SignalContribution]]:
    if len(klines) < 20:
        return (
            50,
            "数据不足",
            [
                SignalContribution(
                    category="数据",
                    name="K线样本不足",
                    impact=0,
                    level="谨慎",
                    reason="少于20根日K，趋势评分暂按中性处理。",
                )
            ],
        )
    ma5 = moving_average(klines, 5)
    ma10 = moving_average(klines, 10)
    ma20 = moving_average(klines, 20)
    prev_ma5 = moving_average(klines[:-5], 5) if len(klines) >= 10 else ma5
    prev_ma20 = moving_average(klines[:-5], 20) if len(klines) >= 25 else ma20
    score = 50
    contributions: list[SignalContribution] = []

    score += _add_contribution(
        contributions,
        "均线",
        "现价与5日线",
        8 if quote.price > ma5 else -8,
        f"现价 {quote.price:.2f} {'高于' if quote.price > ma5 else '低于'} 5日线 {ma5:.2f}。",
    )
    score += _add_contribution(
        contributions,
        "均线",
        "短线均线排列",
        10 if ma5 > ma10 else -6,
        f"5日线 {ma5:.2f} {'高于' if ma5 > ma10 else '未高于'} 10日线 {ma10:.2f}。",
    )
    score += _add_contribution(
        contributions,
        "均线",
        "波段均线排列",
        12 if ma10 > ma20 else -8,
        f"10日线 {ma10:.2f} {'高于' if ma10 > ma20 else '未高于'} 20日线 {ma20:.2f}。",
    )
    score += _add_contribution(
        contributions,
        "斜率",
        "短线斜率",
        7 if ma5 > prev_ma5 else -5,
        f"5日线较前5日 {'抬升' if ma5 > prev_ma5 else '走弱'}。",
    )
    score += _add_contribution(
        contributions,
        "斜率",
        "波段斜率",
        6 if ma20 >= prev_ma20 else -6,
        f"20日线较前5日 {'稳定或抬升' if ma20 >= prev_ma20 else '下行'}。",
    )

    if quote.change_pct > 3:
        change_impact = 10
    elif quote.change_pct > 1:
        change_impact = 6
    elif quote.change_pct < -3:
        change_impact = -12
    elif quote.change_pct < -1:
        change_impact = -6
    else:
        change_impact = 0
    score += _add_contribution(
        contributions,
        "价格",
        "日内涨跌",
        change_impact,
        f"当前涨跌幅 {quote.change_pct:.2f}%。",
    )

    recent_high = max(item.high for item in klines[-20:])
    recent_low = min(item.low for item in klines[-20:])
    if quote.price >= recent_high * 0.985:
        score += _add_contribution(
            contributions,
            "位置",
            "接近20日高位",
            12,
            f"现价接近20日高点 {recent_high:.2f}，右侧强度较好。",
        )
    if quote.price <= recent_low * 1.03:
        score += _add_contribution(
            contributions,
            "位置",
            "接近20日低位",
            -10,
            f"现价接近20日低点 {recent_low:.2f}，需要防止弱势延续。",
        )

    if quote.turnover_rate:
        if 2 <= quote.turnover_rate <= 8:
            turnover_impact = 8
            turnover_reason = f"换手率 {quote.turnover_rate:.2f}% 处于相对活跃区间。"
        elif quote.turnover_rate > 15:
            turnover_impact = -5
            turnover_reason = f"换手率 {quote.turnover_rate:.2f}% 偏高，短线分歧较大。"
        else:
            turnover_impact = 0
            turnover_reason = f"换手率 {quote.turnover_rate:.2f}% 暂未形成明显加分。"
        score += _add_contribution(contributions, "活跃度", "换手率", turnover_impact, turnover_reason)
    else:
        _add_contribution(contributions, "活跃度", "换手率", 0, "缺少换手率字段，活跃度不加分也不扣分。")
    volume_ratio = recent_volume_ratio(klines)
    if quote.change_pct > 0 and volume_ratio >= 1.25:
        volume_impact = 6
        volume_reason = f"近5日量能约为20日均量 {volume_ratio:.2f} 倍，上涨有量能确认。"
    elif quote.change_pct < 0 and volume_ratio >= 1.25:
        volume_impact = -7
        volume_reason = f"近5日量能约为20日均量 {volume_ratio:.2f} 倍，下跌放量需要谨慎。"
    elif volume_ratio < 0.65 and abs(quote.change_pct) > 2:
        volume_impact = -4
        volume_reason = f"近5日量能约为20日均量 {volume_ratio:.2f} 倍，价格波动缺少量能配合。"
    else:
        volume_impact = 0
        volume_reason = f"近5日量能约为20日均量 {volume_ratio:.2f} 倍，量价暂未明显偏离。"
    score += _add_contribution(contributions, "量能", "量价确认", volume_impact, volume_reason)

    score = max(0, min(100, score))
    return score, _trend_label(score), contributions


def support_resistance(klines: list[Kline], current_price: float | None = None) -> tuple[float, float]:
    if not klines:
        return 0, 0
    recent = klines[-20:] if len(klines) >= 20 else klines
    lows = sorted(item.low for item in recent if item.low > 0)
    highs = sorted(item.high for item in recent if item.high > 0)
    if not lows or not highs:
        return 0, 0
    close = current_price if current_price and current_price > 0 else recent[-1].close
    support_base = _quantile(lows, 0.18)
    resistance_base = _quantile(highs, 0.82)
    support = min(item.low for item in recent[-5:]) if close < support_base else support_base
    resistance = max(item.high for item in recent[-5:]) if close > resistance_base else resistance_base
    return round(support, 2), round(resistance, 2)


def _add_contribution(
    contributions: list[SignalContribution],
    category: str,
    name: str,
    impact: int,
    reason: str,
) -> int:
    contributions.append(
        SignalContribution(
            category=category,
            name=name,
            impact=impact,
            level=_impact_level(impact),
            reason=reason,
        )
    )
    return impact


def _impact_level(impact: int) -> str:
    if impact >= 6:
        return "积极"
    if impact <= -8:
        return "风险"
    if impact < 0:
        return "谨慎"
    return "观察"


def _trend_label(score: int) -> str:
    if score >= 80:
        return "强势上行"
    if score >= 65:
        return "偏强震荡"
    if score >= 45:
        return "中性观察"
    if score >= 30:
        return "偏弱调整"
    return "风险释放中"


def recent_volume_ratio(klines: list[Kline], recent_window: int = 5, base_window: int = 20) -> float:
    if len(klines) < recent_window + 1:
        return 1.0
    recent = [item.volume for item in klines[-recent_window:] if item.volume > 0]
    base = [item.volume for item in klines[-base_window:] if item.volume > 0]
    if not recent or not base:
        return 1.0
    base_avg = mean(base)
    if base_avg <= 0:
        return 1.0
    return round(mean(recent) / base_avg, 2)


def average_true_range(klines: list[Kline], window: int = 14) -> float:
    if len(klines) < 2:
        return 0
    rows = klines[-(window + 1) :]
    ranges: list[float] = []
    for index in range(1, len(rows)):
        current = rows[index]
        previous = rows[index - 1]
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        if true_range > 0:
            ranges.append(true_range)
    if not ranges:
        return 0
    return round(mean(ranges), 2)


def daily_return_volatility(klines: list[Kline], window: int = 20) -> float:
    rows = klines[-(window + 1) :]
    if len(rows) < 2:
        return 0
    returns = [pct_change(rows[index].close, rows[index - 1].close) for index in range(1, len(rows)) if rows[index - 1].close]
    return round(volatility(returns), 2)


def _quantile(values: list[float], ratio: float) -> float:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0
    return (new - old) / old * 100


def max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = pct_change(close, peak)
        worst = min(worst, drawdown)
    return worst


def volatility(returns: list[float]) -> float:
    if not returns:
        return 0
    avg = mean(returns)
    variance = mean([(item - avg) ** 2 for item in returns])
    return variance ** 0.5


def trend_days(rows: list[Kline]) -> int:
    count = 0
    for index in range(len(rows)):
        if index < 4:
            continue
        ma5 = mean([item.close for item in rows[index - 4 : index + 1]])
        if rows[index].close >= ma5:
            count += 1
    return count
