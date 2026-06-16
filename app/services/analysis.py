from __future__ import annotations

from app.models.schemas import (
    ActionAdvice,
    AnalysisResult,
    DataQuality,
    IndividualReview,
    Kline,
    PlateItem,
    Quote,
    SignalContribution,
    SignalItem,
    SignalSnapshot,
    StockInfo,
    StrongStockItem,
)
from app.services.data_quality import build_data_quality
from app.services.indicators import moving_average, recent_volume_ratio, support_resistance, trend_score, trend_score_snapshot


def build_analysis(
    quote: Quote,
    klines: list[Kline],
    stock_profile: StockInfo | None = None,
    industry_context: PlateItem | None = None,
    review: IndividualReview | None = None,
    data_quality: DataQuality | None = None,
    quote_history: list[dict[str, float | str | None]] | None = None,
    peer_quotes: list[Quote] | None = None,
) -> AnalysisResult:
    score, label, contributions = trend_score_snapshot(quote, klines)
    ma5 = moving_average(klines, 5)
    ma10 = moving_average(klines, 10)
    ma20 = moving_average(klines, 20)
    support, resistance = support_resistance(klines, current_price=quote.price)
    quality = data_quality or build_data_quality(quote, klines)
    risk_level = _risk_level(quote, score, support, quality)

    buy_points = _gate_signal_items(_buy_points(quote, score, ma5, ma10, support, resistance), quality, "buy")
    sell_points = _gate_signal_items(_sell_points(quote, score, ma5, ma20, support, resistance), quality, "sell")
    t_plan = _gate_signal_items(_t_plan(quote, support, resistance), quality, "t")
    strength_tags = _strength_tags(quote, score)
    action_advice = _action_advice(quote, score, risk_level, support, resistance, quality)
    signal_snapshot = _signal_snapshot(score, label, contributions, quality, risk_level)
    summary = _beginner_summary(
        quote,
        score,
        label,
        risk_level,
        support,
        resistance,
        stock_profile,
        industry_context,
        action_advice,
    )

    return AnalysisResult(
        quote=quote,
        stock_profile=stock_profile,
        industry_context=industry_context,
        action_advice=action_advice,
        data_quality=quality,
        signal_snapshot=signal_snapshot,
        review=review,
        trend_score=score,
        trend_label=label,
        support=support,
        resistance=resistance,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        risk_level=risk_level,
        beginner_summary=summary,
        buy_points=buy_points,
        sell_points=sell_points,
        t_plan=t_plan,
        strength_tags=strength_tags,
        klines=klines,
        quote_history=quote_history or [],
        peer_quotes=peer_quotes or [],
    )


def build_strong_stock_watch(quotes: list[Quote], kline_map: dict[str, list[Kline]]) -> list[StrongStockItem]:
    items: list[StrongStockItem] = []
    for quote in quotes:
        klines = kline_map.get(quote.code, [])
        score, _ = trend_score(quote, klines)
        volume_ratio = recent_volume_ratio(klines)
        leader_score = _strong_stock_leader_score(quote, score, volume_ratio)
        reason = _strength_reason(quote, score)
        items.append(
            StrongStockItem(
                rank=0,
                code=quote.code,
                name=quote.name,
                price=quote.price,
                change_pct=quote.change_pct,
                trend_score=score,
                reason=reason,
                leader_score=leader_score,
                tags=_strong_stock_tags(quote, score, volume_ratio, leader_score),
            )
        )
    ranked = sorted(items, key=lambda item: (item.trend_score, item.change_pct), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ranked


def _risk_level(quote: Quote, score: int, support: float, quality: DataQuality | None = None) -> str:
    if quality and quality.score < 50:
        return "高风险"
    if quality and quality.score < 70:
        return "中等风险"
    if quote.change_pct <= -5 or (support and quote.price < support):
        return "高风险"
    if score < 45 or quote.change_pct < -2:
        return "中等风险"
    if score >= 75 and quote.change_pct > 0:
        return "低风险"
    return "可控观察"


def _buy_points(
    quote: Quote,
    score: int,
    ma5: float,
    ma10: float,
    support: float,
    resistance: float,
) -> list[SignalItem]:
    items: list[SignalItem] = []
    if score >= 70 and quote.price > ma5 > ma10:
        items.append(
            SignalItem(
                title="趋势试仓点",
                level="积极",
                reason=f"价格站上5日线，5日线高于10日线，可等回踩不破 {ma5:.2f} 再考虑分批参与。",
            )
        )
    if resistance and quote.price >= resistance * 0.99 and quote.change_pct > 1:
        items.append(
            SignalItem(
                title="突破观察点",
                level="观察",
                reason=f"价格接近20日压力位 {resistance:.2f}，若放量突破并收稳，可作为右侧确认信号。",
            )
        )
    if support and support < quote.price < ma10 and score >= 45:
        items.append(
            SignalItem(
                title="支撑低吸点",
                level="谨慎",
                reason=f"当前靠近支撑区 {support:.2f}，只适合小仓位观察，跌破支撑应停止低吸。",
            )
        )
    if not items:
        items.append(
            SignalItem(
                title="暂不追买",
                level="谨慎",
                reason="趋势和价格位置没有形成清晰共振，新手更适合等待回踩确认或突破确认。",
            )
        )
    return items


def _sell_points(
    quote: Quote,
    score: int,
    ma5: float,
    ma20: float,
    support: float,
    resistance: float,
) -> list[SignalItem]:
    items: list[SignalItem] = []
    if quote.price < ma5:
        items.append(
            SignalItem(
                title="短线减仓点",
                level="观察",
                reason=f"价格跌破5日线 {ma5:.2f}，短线强度下降，可考虑降低仓位。",
            )
        )
    if support and quote.price <= support * 1.01:
        items.append(
            SignalItem(
                title="止损保护点",
                level="风险",
                reason=f"价格贴近20日支撑 {support:.2f}，若有效跌破，原趋势判断失效。",
            )
        )
    if resistance and quote.price >= resistance * 0.985 and score < 70:
        items.append(
            SignalItem(
                title="压力止盈点",
                level="谨慎",
                reason=f"价格接近压力区 {resistance:.2f}，但趋势分不足，适合先保护利润。",
            )
        )
    if quote.price < ma20:
        items.append(
            SignalItem(
                title="波段风控点",
                level="风险",
                reason=f"价格低于20日线 {ma20:.2f}，中短期趋势偏弱，避免扩大亏损。",
            )
        )
    if not items:
        items.append(
            SignalItem(
                title="持有观察",
                level="观察",
                reason="暂未触发明显卖出信号，继续关注5日线和量能变化。",
            )
        )
    return items


def _t_plan(quote: Quote, support: float, resistance: float) -> list[SignalItem]:
    price = quote.price
    intraday_range = max(quote.high - quote.low, price * 0.012)
    low_area = _t_low_area(price, support, intraday_range)
    high_area = _t_high_area(price, resistance, intraday_range)
    width_pct = (high_area - low_area) / price * 100 if price else 0
    style = _t_style(quote, support, resistance, width_pct)
    return [
        SignalItem(
            title="已有底仓才做T",
            level="观察",
            reason="A股普通股票通常是T+1，今日买入部分不能当日卖出；做T应只使用已有可卖底仓。",
        ),
        SignalItem(
            title=f"{style}低吸区",
            level="谨慎" if style != "趋势型" else "观察",
            reason=f"参考 {low_area:.2f} 附近；只有缩量止跌、跌速放缓且未有效跌破支撑时，才考虑用已卖出部分小额接回。",
        ),
        SignalItem(
            title=f"{style}高抛区",
            level="积极" if quote.change_pct > 0 else "观察",
            reason=f"参考 {high_area:.2f} 附近；冲高量能不足、接近压力或分时转弱时，优先卖出可卖底仓降低成本。",
        ),
        SignalItem(
            title="做T失效条件",
            level="风险",
            reason=f"若区间宽度不足约 {max(width_pct, 0):.2f}%、跌破 {support:.2f} 后不能快速收回，或冲高回落放量转弱，当天停止做T。",
        ),
    ]


def _t_low_area(price: float, support: float, intraday_range: float) -> float:
    volatility_floor = max(0, price - intraday_range * 0.4)
    if support and support < price:
        return round(max(support, volatility_floor), 2)
    return round(volatility_floor, 2)


def _t_high_area(price: float, resistance: float, intraday_range: float) -> float:
    volatility_ceiling = price + intraday_range * 0.4
    if resistance and resistance > price:
        return round(min(resistance, volatility_ceiling), 2)
    return round(volatility_ceiling, 2)


def _t_style(quote: Quote, support: float, resistance: float, width_pct: float) -> str:
    if width_pct < 1.2:
        return "窄幅"
    if quote.change_pct >= 2 and resistance and quote.price >= resistance * 0.97:
        return "趋势型"
    if support and resistance and support < quote.price < resistance:
        return "区间型"
    return "波动型"


def _strength_tags(quote: Quote, score: int) -> list[str]:
    tags: list[str] = []
    if score >= 80:
        tags.append("趋势强")
    if quote.change_pct >= 5:
        tags.append("涨幅强")
    if quote.turnover_rate and quote.turnover_rate >= 3:
        tags.append("换手活跃")
    if quote.amount >= 5_000_000_000:
        tags.append("成交额大")
    if not tags:
        tags.append("观察中")
    return tags


def _strong_stock_leader_score(quote: Quote, trend_score: int, volume_ratio: float) -> int:
    score = 38
    score += round((trend_score - 50) * 0.48)
    score += 10 if quote.change_pct >= 5 else 5 if quote.change_pct >= 2 else -5 if quote.change_pct <= -3 else 0
    score += 8 if volume_ratio >= 1.4 and quote.change_pct > 0 else -6 if volume_ratio >= 1.4 and quote.change_pct < 0 else 0
    score += 6 if quote.turnover_rate and 2 <= quote.turnover_rate <= 10 else -4 if quote.turnover_rate and quote.turnover_rate > 15 else 0
    if quote.amount >= 1_000_000_000:
        score += 8
    elif quote.amount >= 300_000_000:
        score += 3
    return max(0, min(100, score))


def _strong_stock_tags(quote: Quote, trend_score: int, volume_ratio: float, leader_score: int) -> list[str]:
    tags: list[str] = []
    if leader_score >= 70:
        tags.append("龙头候选")
    if trend_score >= 75:
        tags.append("趋势强")
    if quote.change_pct >= 5:
        tags.append("涨幅强")
    if volume_ratio >= 1.4:
        tags.append("量能放大")
    if quote.turnover_rate and quote.turnover_rate >= 6:
        tags.append("换手活跃")
    if not tags:
        tags.append("观察")
    return tags


def _gate_signal_items(items: list[SignalItem], quality: DataQuality, kind: str) -> list[SignalItem]:
    if quality.score >= 70:
        return items
    reason = _quality_reason(quality)
    if _quality_blocks_active_signals(quality):
        if kind == "buy":
            return [
                SignalItem(
                    title="暂停新增买点",
                    level="风险",
                    reason=f"当前数据质量为{quality.level}，存在{reason}；新增买点先暂停，等报价和K线重新确认后再判断。",
                )
            ]
        if kind == "t":
            return [
                SignalItem(
                    title="暂停做T",
                    level="风险",
                    reason=f"当前数据质量为{quality.level}，存在{reason}；分时价格和关键价位可信度不足，不建议做T。",
                ),
                SignalItem(
                    title="已有底仓才做T",
                    level="观察",
                    reason="A股普通股票通常是T+1，今日买入部分不能当日卖出；恢复做T前也只能使用已有可卖底仓。",
                ),
            ]
        return [
            SignalItem(
                title="先收紧风控",
                level="风险",
                reason=f"当前数据质量为{quality.level}，存在{reason}；不要按单个实时价机械卖出，优先等待支撑位或20日线收盘确认。",
            )
        ]

    gated: list[SignalItem] = []
    for item in items:
        level = item.level
        if level in {"积极", "观察"}:
            level = "谨慎"
        gated.append(
            SignalItem(
                title=item.title,
                level=level,
                reason=f"数据质量为{quality.level}（{reason}），该信号仅作低置信观察；{item.reason}",
            )
        )
    return gated


def _quality_blocks_active_signals(quality: DataQuality) -> bool:
    hard_anomalies = {"K线严重滞后", "K线兜底缓存", "演示K线", "演示行情", "报价严重滞后"}
    return quality.score < 50 or any(item in hard_anomalies for item in quality.anomalies)


def _signal_snapshot(
    score: int,
    label: str,
    contributions: list[SignalContribution],
    quality: DataQuality,
    risk_level: str,
) -> SignalSnapshot:
    positive = sorted([item for item in contributions if item.impact > 0], key=lambda item: item.impact, reverse=True)
    negative = sorted([item for item in contributions if item.impact < 0], key=lambda item: item.impact)
    neutral = [item for item in contributions if item.impact == 0]
    confidence = _signal_confidence(score, quality)
    risk_notes = []
    if risk_level in {"中等风险", "高风险"}:
        risk_notes.append(f"当前风险级别为{risk_level}。")
    if quality.score < 70:
        risk_notes.append(f"数据质量{quality.level}，结论已自动降权。")
    risk_notes.extend(item.reason for item in negative[:2])
    return SignalSnapshot(
        score=score,
        label=label,
        confidence=confidence,
        summary=_signal_summary(score, label, positive, negative, quality),
        contributions=contributions,
        positive=positive[:5],
        negative=negative[:5],
        neutral=neutral[:5],
        data_quality_notes=quality.notes[:5],
        risk_notes=risk_notes[:5],
    )


def _signal_confidence(score: int, quality: DataQuality) -> int:
    signal_clarity = min(100, 50 + abs(score - 50))
    confidence = round(quality.score * 0.65 + signal_clarity * 0.35)
    return max(20, min(95, confidence))


def _signal_summary(
    score: int,
    label: str,
    positive: list[SignalContribution],
    negative: list[SignalContribution],
    quality: DataQuality,
) -> str:
    drivers = []
    if positive:
        drivers.append(f"主要加分来自{positive[0].name}")
    if negative:
        drivers.append(f"主要扣分来自{negative[0].name}")
    driver_text = "，".join(drivers) if drivers else "暂无明显单项驱动"
    return f"趋势评分 {score}/100，状态为{label}；{driver_text}；数据质量{quality.level}{quality.score}分。"


def _strength_reason(quote: Quote, score: int) -> str:
    pieces = []
    if score >= 75:
        pieces.append("趋势评分靠前")
    if quote.change_pct > 0:
        pieces.append(f"今日上涨 {quote.change_pct:.2f}%")
    if quote.turnover_rate:
        pieces.append(f"换手 {quote.turnover_rate:.2f}%")
    if quote.amount:
        pieces.append(f"成交额 {quote.amount / 100000000:.1f} 亿")
    return "，".join(pieces) or "等待更多行情确认"


def _beginner_summary(
    quote: Quote,
    score: int,
    label: str,
    risk_level: str,
    support: float,
    resistance: float,
    stock_profile: StockInfo | None,
    industry_context: PlateItem | None,
    action_advice: ActionAdvice,
) -> str:
    industry = ""
    if stock_profile and stock_profile.industry:
        industry = f" 所属行业为「{stock_profile.industry}」。"
    if industry_context:
        industry += f" 行业背景参考「{industry_context.name}」当前涨跌幅 {industry_context.change_pct:.2f}%。"
    return (
        f"{quote.name} 当前属于「{label}」，趋势分 {score}/100，风险级别为「{risk_level}」。"
        f"{industry} 新手可记住两个价位：支撑 {support:.2f}，压力 {resistance:.2f}；"
        f"当前建议「{action_advice.action}」，理由是{action_advice.reason}"
    )


def _action_advice(
    quote: Quote,
    score: int,
    risk_level: str,
    support: float,
    resistance: float,
    quality: DataQuality | None = None,
) -> ActionAdvice:
    if quality and quality.score < 50:
        return ActionAdvice(
            action="控制风险",
            confidence=max(35, quality.score),
            reason=f"当前数据质量为{quality.level}，存在{_quality_reason(quality)}，先暂停新增买点和做T，按控制风险口径等待行情重新确认。",
        )
    if quality and quality.score < 70:
        if score <= 42 or risk_level in {"中等风险", "高风险"}:
            return ActionAdvice(
                action="控制风险",
                confidence=min(70, max(55, 100 - score)),
                reason=f"趋势偏弱且数据质量只有{quality.level}，先按低置信风控处理；等新行情确认后再考虑买点或做T。",
            )
        return ActionAdvice(
            action="轻仓观察",
            confidence=min(58, max(42, quality.score)),
            reason=f"数据质量只有{quality.level}，建议先观察支撑压力是否被新数据确认。",
        )
    if score >= 78 and risk_level in {"低风险", "可控观察"}:
        return ActionAdvice(
            action="回踩关注",
            confidence=min(90, score),
            reason="趋势结构较强，但仍建议等回踩或突破确认后分批处理。",
        )
    if score >= 58 and quote.price > support:
        return ActionAdvice(
            action="持有观察",
            confidence=score,
            reason="趋势没有明显破坏，重点跟踪支撑位和量能变化。",
        )
    if score <= 42 or risk_level in {"中等风险", "高风险"}:
        return ActionAdvice(
            action="控制风险",
            confidence=max(55, 100 - score),
            reason="趋势偏弱或风险信号较多，优先看支撑是否有效，避免盲目加仓。",
        )
    return ActionAdvice(
        action="等待信号",
        confidence=55,
        reason="价格位置和趋势强度暂未形成清晰共振。",
    )


def _quality_reason(quality: DataQuality) -> str:
    if quality.anomalies:
        return "、".join(quality.anomalies[:3])
    if quality.notes:
        return "、".join(quality.notes[:2])
    return "数据可靠性不足"
