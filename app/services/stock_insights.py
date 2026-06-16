from __future__ import annotations

from app.models.schemas import (
    AbnormalEventItem,
    AbnormalEventSummary,
    AnalysisResult,
    FactorScore,
    FinancialHealth,
    FinancialMetric,
    FundFlowAnalysis,
    FundFlowWindow,
    KeyPriceLevel,
    LhbSummary,
    OrderBook,
    OrderPressure,
    RuleDefinition,
    RuleMatch,
    SignalItem,
    StockEventItem,
    StockEventSummary,
    StockInsightBundle,
    StockOverview,
    StockRuleMatchSummary,
    StrategyCard,
    ValuationAnalysis,
)
from app.services.indicators import max_drawdown, pct_change, volatility
from app.utils.time import now_text


RULE_VERSION = "rules.v2"
SCORE_VERSION = "score.v2"
MIN_VALUATION_HISTORY_ROWS = 30
MIN_PEER_VALUATION_ROWS = 15
RULE_CONFIG: dict[str, dict[str, float | str]] = {
    "volume_breakout_20d": {"near_breakout_pct": 0.985, "volume_ratio": 1.35, "window": 20},
    "break_ma20_risk": {"trend_score": 50, "near_ma20_pct": 1.015},
    "support_rebound_watch": {"near_support_pct": 1.03, "fund_score": 58},
    "fund_tech_divergence": {"trend_weak": 48, "trend_strong": 65, "fund_strong": 62, "fund_weak": 48, "gap": 18},
    "high_valuation_chase_risk": {"trend_hit": 68, "trend_close": 62, "valuation_hit": 45, "valuation_close": 52},
    "abnormal_risk_event": {"risk_event_min": 1},
}


def build_stock_insight_bundle(
    analysis: AnalysisResult,
    *,
    order_book: OrderBook | None = None,
    order_book_error: str | None = None,
) -> StockInsightBundle:
    fund_flow = build_fund_flow_analysis(analysis)
    order_pressure = build_order_pressure(analysis, order_book=order_book, order_book_error=order_book_error)
    abnormal_events = build_abnormal_events(analysis)
    lhb = build_lhb_summary(analysis, abnormal_events)
    events = build_event_summary(analysis, abnormal_events=abnormal_events, lhb=lhb)
    strategy_cards = build_strategy_cards(analysis, fund_flow, order_pressure)
    overview = build_stock_overview(analysis, fund_flow, order_pressure, events)
    financial_health = build_financial_health(analysis)
    valuation = build_valuation_analysis(analysis)
    rule_matches = build_rule_match_summary(analysis, fund_flow, order_pressure, valuation, abnormal_events)
    return StockInsightBundle(
        overview=overview,
        fund_flow=fund_flow,
        order_pressure=order_pressure,
        events=events,
        strategy_cards=strategy_cards,
        financial_health=financial_health,
        valuation=valuation,
        lhb=lhb,
        abnormal_events=abnormal_events,
        rule_matches=rule_matches,
    )


def build_stock_overview(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    events: StockEventSummary,
) -> StockOverview:
    quote = analysis.quote
    factors = [
        _technical_factor(analysis),
        _fund_factor(fund_flow),
        _fundamental_factor(analysis),
        _event_factor(events),
        _risk_factor(analysis, order_pressure),
    ]
    factor_score = round(sum(item.score for item in factors) / len(factors))
    signal_quality_score = round(analysis.signal_snapshot.confidence * 0.7 + analysis.data_quality.score * 0.3)
    total_score = round(factor_score * 0.68 + signal_quality_score * 0.32)
    if analysis.data_quality.score < 70:
        total_score = min(total_score, round((factor_score + analysis.data_quality.score) / 2))
    main_conflict = _main_conflict(analysis, fund_flow, order_pressure)
    if analysis.data_quality.score < 70:
        main_conflict = f"数据质量{analysis.data_quality.level}，{main_conflict}"
    return StockOverview(
        symbol=f"{quote.code}.{quote.market}",
        code=quote.code,
        market=quote.market,
        name=quote.name,
        total_score=total_score,
        total_level=_score_level(total_score),
        main_conflict=main_conflict,
        beginner_takeaways=[
            f"本次信号可信度 {analysis.signal_snapshot.confidence}%，结论已按数据质量 {analysis.data_quality.level} 自动降权。",
            f"先看 {analysis.support:.2f} 支撑是否守住，再看 {analysis.resistance:.2f} 压力能否放量突破。",
            f"当前建议是「{analysis.action_advice.action}」，信心 {analysis.action_advice.confidence}%。",
            main_conflict,
        ],
        key_prices=[
            KeyPriceLevel(label="支撑位", price=analysis.support, note="跌破后当前趋势判断需要降级。"),
            KeyPriceLevel(label="压力位", price=analysis.resistance, note="放量突破后才算右侧确认。"),
            KeyPriceLevel(label="5日线", price=analysis.ma5, note="短线强弱的第一观察线。"),
            KeyPriceLevel(label="20日线", price=analysis.ma20, note="波段风控的重要参考线。"),
        ],
        risk_triggers=_risk_triggers(analysis, order_pressure),
        factors=factors,
        action_advice=analysis.action_advice,
        updated_at=quote.timestamp,
    )


def build_fund_flow_analysis(analysis: AnalysisResult) -> FundFlowAnalysis:
    quote = analysis.quote
    rows = analysis.klines[-10:]
    amount = quote.amount or 0
    turnover = quote.turnover_rate or 0
    amount_score = _clamp(int(min(amount / 100_000_000, 80)))
    turnover_score = 60 if 2 <= turnover <= 8 else 45 if turnover < 2 else 50
    direction_score = 62 if quote.change_pct > 0 else 42 if quote.change_pct < 0 else 50
    volume_score = _volume_score(rows)
    overall = _clamp(round(amount_score * 0.25 + turnover_score * 0.25 + direction_score * 0.3 + volume_score * 0.2))
    if analysis.data_quality.score < 70:
        overall = _clamp(round(overall * 0.8 + analysis.data_quality.score * 0.2))
    relation = _price_volume_relation(analysis)
    windows = [
        FundFlowWindow(
            label="今日量价热度",
            score=overall,
            estimated_net_inflow=None,
            summary=relation,
        ),
        FundFlowWindow(
            label="5日连续性",
            score=_recent_momentum_score(analysis.klines, 5),
            estimated_net_inflow=None,
            summary=_recent_window_summary(analysis.klines, 5),
        ),
        FundFlowWindow(
            label="10日连续性",
            score=_recent_momentum_score(analysis.klines, 10),
            estimated_net_inflow=None,
            summary=_recent_window_summary(analysis.klines, 10),
        ),
    ]
    return FundFlowAnalysis(
        symbol=f"{quote.code}.{quote.market}",
        available=bool(amount),
        source=f"{quote.source}·量价热度估算",
        updated_at=quote.timestamp,
        overall_score=overall,
        level=_score_level(overall),
        estimated_main_net_inflow=None,
        price_volume_relation=relation,
        windows=windows,
        notes=[
            "当前为量价资金热度估算，使用成交额、涨跌幅、换手率和量价关系，不等同于真实主力净流入。",
            "未接入逐笔成交或正式资金流前，不输出大单/特大单净流入结论。",
            "接入东方财富资金流或 Futu 逐笔/盘口后，可替换为更精确的大单、特大单拆分。",
            *([f"数据质量为{analysis.data_quality.level}，量价热度评分已降权。"] if analysis.data_quality.score < 70 else []),
        ],
    )


def build_order_pressure(
    analysis: AnalysisResult,
    *,
    order_book: OrderBook | None = None,
    order_book_error: str | None = None,
) -> OrderPressure:
    quote = analysis.quote
    symbol = f"{quote.code}.{quote.market}"
    if order_book:
        bid_amount = sum(item.price * item.volume for item in order_book.bid)
        ask_amount = sum(item.price * item.volume for item in order_book.ask)
        best_bid = order_book.bid[0].price if order_book.bid else None
        best_ask = order_book.ask[0].price if order_book.ask else None
        spread_pct = (best_ask - best_bid) / quote.price * 100 if best_bid and best_ask and quote.price else None
        ratio = bid_amount / ask_amount if ask_amount else None
        level = "买盘偏强" if ratio and ratio > 1.25 else "卖压偏强" if ratio and ratio < 0.8 else "盘口均衡"
        if analysis.data_quality.score < 70:
            level = f"{level}（降权）"
        return OrderPressure(
            symbol=symbol,
            available=True,
            source=order_book.source,
            updated_at=order_book.updated_at,
            pressure_level=level,
            spread_pct=round(spread_pct, 4) if spread_pct is not None else None,
            bid_ask_ratio=round(ratio, 2) if ratio is not None else None,
            bid_amount=round(bid_amount, 2),
            ask_amount=round(ask_amount, 2),
            summary=f"{level}，买卖盘金额比约 {ratio:.2f}。" if ratio else "盘口深度不足，暂不能判断买卖盘强弱。",
            notes=[
                "盘口来自实时深度数据，仅反映当前时点挂单压力。",
                *([f"数据质量为{analysis.data_quality.level}，盘口结论仅作低置信参考。"] if analysis.data_quality.score < 70 else []),
            ],
        )
    intraday_range_pct = (quote.high - quote.low) / quote.price * 100 if quote.price else 0
    distance_to_high = (quote.high - quote.price) / quote.price * 100 if quote.price else 0
    distance_to_low = (quote.price - quote.low) / quote.price * 100 if quote.price else 0
    if distance_to_high < distance_to_low and quote.change_pct < 0:
        level = "上方卖压待消化"
    elif distance_to_low < distance_to_high and quote.change_pct > 0:
        level = "下方承接较近"
    else:
        level = "盘口需实时源确认"
    notes = ["Futu OpenAPI 未启用或盘口不可用，当前用日内高低价位置估算压力。"]
    if order_book_error:
        notes.append(order_book_error[:160])
    if analysis.data_quality.score < 70:
        level = f"{level}（降权）"
    return OrderPressure(
        symbol=symbol,
        available=False,
        source=f"{quote.source}·区间估算",
        updated_at=quote.timestamp,
        pressure_level=level,
        spread_pct=None,
        bid_ask_ratio=None,
        summary=f"{level}，日内振幅约 {intraday_range_pct:.2f}%。",
        notes=notes + ([f"数据质量为{analysis.data_quality.level}，盘口估算结论已降权。"] if analysis.data_quality.score < 70 else []),
    )


def build_financial_health(analysis: AnalysisResult) -> FinancialHealth:
    quote = analysis.quote
    symbol = f"{quote.code}.{quote.market}"
    score = 54
    metrics: list[FinancialMetric] = []
    highlights: list[str] = []
    risk_notes: list[str] = []
    missing = ["ROE", "营收增速", "净利润增速", "经营现金流", "资产负债率", "分红记录"]

    if quote.pe is not None:
        pe_score, pe_level, pe_summary = _pe_view(quote.pe)
        score += round((pe_score - 50) * 0.18)
        metrics.append(
            FinancialMetric(
                name="市盈率",
                value=f"{quote.pe:.2f}",
                level=pe_level,
                summary=pe_summary,
                source=quote.source,
            )
        )
        if pe_level in {"偏强", "强"}:
            highlights.append("市盈率处在相对可解释区间，估值压力暂不突出。")
        if pe_level in {"偏弱", "弱"}:
            risk_notes.append(pe_summary)
    else:
        metrics.append(_missing_metric("市盈率", "行情源暂未返回 PE，无法判断利润对应估值。"))

    if quote.pb is not None:
        pb_score, pb_level, pb_summary = _pb_view(quote.pb)
        score += round((pb_score - 50) * 0.14)
        metrics.append(
            FinancialMetric(
                name="市净率",
                value=f"{quote.pb:.2f}",
                level=pb_level,
                summary=pb_summary,
                source=quote.source,
            )
        )
        if pb_level in {"偏强", "强"}:
            highlights.append("市净率没有明显脱离净资产锚。")
        if pb_level in {"偏弱", "弱"}:
            risk_notes.append(pb_summary)
    else:
        metrics.append(_missing_metric("市净率", "行情源暂未返回 PB，资产估值锚不足。"))

    if quote.market_cap is not None:
        cap_score, cap_level, cap_summary = _market_cap_view(quote.market_cap)
        score += round((cap_score - 50) * 0.12)
        metrics.append(
            FinancialMetric(
                name="总市值",
                value=_format_amount_text(quote.market_cap),
                level=cap_level,
                summary=cap_summary,
                source=quote.source,
            )
        )
        highlights.append(cap_summary)
    else:
        metrics.append(_missing_metric("总市值", "行情源暂未返回总市值，规模和流动性判断需降权。"))

    if analysis.stock_profile and analysis.stock_profile.industry:
        metrics.append(
            FinancialMetric(
                name="所属行业",
                value=analysis.stock_profile.industry,
                level="观察",
                summary="行业用于横向估值比较，后续接入行业分位会更有参考价值。",
                source=analysis.stock_profile.source,
            )
        )
    else:
        metrics.append(_missing_metric("所属行业", "行业字段缺失，暂不能做同行比较。"))
        missing.append("行业估值分位")

    liquidity_score, liquidity_level, liquidity_summary = _liquidity_view(quote.amount, quote.turnover_rate)
    score += round((liquidity_score - 50) * 0.12)
    metrics.append(
        FinancialMetric(
            name="交易活跃度",
            value=f"成交额 {_format_amount_text(quote.amount)} / 换手 {quote.turnover_rate:.2f}%" if quote.turnover_rate is not None else f"成交额 {_format_amount_text(quote.amount)}",
            level=liquidity_level,
            summary=liquidity_summary,
            source=quote.source,
        )
    )
    if liquidity_level in {"偏弱", "弱"}:
        risk_notes.append(liquidity_summary)

    score = _clamp(score)
    if not highlights:
        highlights.append("当前只能从行情估值字段做基础体检，完整财报源接入后需要重新校验。")
    if not risk_notes:
        risk_notes.append("尚未接入资产负债、现金流和利润增长，基本面风险只能做初筛。")
    return FinancialHealth(
        symbol=symbol,
        updated_at=quote.timestamp,
        score=score,
        level=_score_level(score),
        summary=_financial_summary(score, missing),
        metrics=metrics,
        highlights=highlights[:4],
        risk_notes=risk_notes[:4],
        missing_data=missing,
        source=f"{quote.source}·行情字段体检",
    )


def build_valuation_analysis(analysis: AnalysisResult) -> ValuationAnalysis:
    quote = analysis.quote
    score = 52
    evidence: list[str] = []
    watch_points: list[str] = []
    missing: list[str] = []
    price_percentile = _price_percentile_from_klines(analysis)
    pe_percentile = _valuation_percentile_from_history(analysis, "pe")
    pb_percentile = _valuation_percentile_from_history(analysis, "pb")
    peer_pe_percentile = _peer_valuation_percentile(analysis, "pe")
    peer_pb_percentile = _peer_valuation_percentile(analysis, "pb")
    peer_sample_count = len(
        [
            item
            for item in getattr(analysis, "peer_quotes", [])
            if (getattr(item, "pe", None) and getattr(item, "pe", 0) > 0)
            or (getattr(item, "pb", None) and getattr(item, "pb", 0) > 0)
        ]
    )
    valuation_anchor_label = _valuation_anchor_label(price_percentile, pe_percentile, pb_percentile, peer_pe_percentile, peer_pb_percentile)
    if price_percentile is not None:
        evidence.append(f"价格历史锚：近120日价格分位 {price_percentile:.1f}%，只用于位置提醒，不直接替代估值分位。")
        if price_percentile >= 82:
            watch_points.append("价格处于自身近期高分位，任何估值偏高信号都需要更严格的失效条件。")
        elif price_percentile <= 25:
            watch_points.append("价格处于自身近期低分位，但仍需确认趋势止跌，不能只因位置低就提前乐观。")
    else:
        missing.append("价格历史分位")

    if pe_percentile is not None:
        score += _valuation_percentile_score_delta(pe_percentile, quote.pe)
        evidence.append(f"PE历史锚：本地历史PE分位 {pe_percentile:.1f}%，用于衡量自身估值压力。")
        if pe_percentile >= 82:
            watch_points.append("PE处于自身历史高分位，趋势越强越要关注估值兑现风险。")
        elif pe_percentile <= 25 and quote.pe and quote.pe > 0:
            watch_points.append("PE处于自身历史低分位，可作为安全边际线索，但仍需趋势确认。")
    elif quote.pe is not None:
        missing.append("PE历史分位")

    if pb_percentile is not None:
        score += round(_valuation_percentile_score_delta(pb_percentile, quote.pb) * 0.65)
        evidence.append(f"PB历史锚：本地历史PB分位 {pb_percentile:.1f}%，用于观察资产估值压力。")
        if pb_percentile >= 82:
            watch_points.append("PB处于自身历史高分位，回撤时估值压缩会更敏感。")
    elif quote.pb is not None:
        missing.append("PB历史分位")

    if quote.pe is not None:
        pe_score, _, pe_summary = _pe_view(quote.pe)
        score += round((pe_score - 50) * 0.35)
        evidence.append(f"PE {quote.pe:.2f}：{pe_summary}")
        if quote.pe <= 0:
            watch_points.append("PE 为负或无意义时，应优先检查盈利是否亏损或一次性扰动。")
        elif quote.pe > 60:
            watch_points.append("PE 偏高时，需要确认业绩增长能否兑现。")
    else:
        missing.append("PE")

    if quote.pb is not None:
        pb_score, _, pb_summary = _pb_view(quote.pb)
        score += round((pb_score - 50) * 0.25)
        evidence.append(f"PB {quote.pb:.2f}：{pb_summary}")
        if quote.pb > 8:
            watch_points.append("PB 偏高时，回撤中估值压缩会更敏感。")
    else:
        missing.append("PB")

    if peer_pe_percentile is not None:
        score += round(_peer_percentile_score_delta(peer_pe_percentile, quote.pe) * 0.8)
        evidence.append(f"同行PE分位：在同行缓存样本中约处于 {peer_pe_percentile:.1f}% 分位。")
        if peer_pe_percentile >= 80:
            watch_points.append("相对同行PE已偏高，若缺少业绩兑现，估值压力会先于趋势修正。")
    elif analysis.stock_profile and analysis.stock_profile.industry and quote.pe is not None:
        missing.append("同行PE分位")

    if peer_pb_percentile is not None:
        score += round(_peer_percentile_score_delta(peer_pb_percentile, quote.pb) * 0.55)
        evidence.append(f"同行PB分位：在同行缓存样本中约处于 {peer_pb_percentile:.1f}% 分位。")
    elif analysis.stock_profile and analysis.stock_profile.industry and quote.pb is not None:
        missing.append("同行PB分位")

    if quote.market_cap is not None:
        cap_score, _, cap_summary = _market_cap_view(quote.market_cap)
        score += round((cap_score - 50) * 0.12)
        evidence.append(f"总市值 {_format_amount_text(quote.market_cap)}：{cap_summary}")
    else:
        missing.append("总市值")

    if analysis.industry_context:
        evidence.append(f"行业背景 {analysis.industry_context.name} 涨跌幅 {analysis.industry_context.change_pct:.2f}%。")
        if analysis.industry_context.change_pct < -1:
            watch_points.append("行业当日偏弱，估值修复可能缺少板块配合。")
    else:
        missing.append("行业估值分位")

    if analysis.trend_score < 45 and score >= 58:
        watch_points.append("估值看起来不贵，但趋势偏弱，不能只因便宜而提前判断止跌。")
    if analysis.trend_score >= 70 and score < 45:
        watch_points.append("趋势较强但估值压力偏大，追高时要更依赖风控线。")
    if not watch_points:
        watch_points.append("估值仅作安全边际观察，买卖仍需结合趋势、资金和风险触发。")

    score = _clamp(score)
    return ValuationAnalysis(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        score=score,
        level=_score_level(score),
        summary=_valuation_summary(score, missing),
        pe=quote.pe,
        pb=quote.pb,
        market_cap=quote.market_cap,
        market_cap_text=_format_amount_text(quote.market_cap) if quote.market_cap is not None else None,
        price_percentile=price_percentile,
        pe_percentile=pe_percentile,
        pb_percentile=pb_percentile,
        peer_pe_percentile=peer_pe_percentile,
        peer_pb_percentile=peer_pb_percentile,
        peer_sample_count=peer_sample_count,
        valuation_anchor_label=valuation_anchor_label,
        evidence=evidence or ["估值字段不足，暂不能形成有效判断。"],
        watch_points=watch_points,
        missing_data=missing,
        source=f"{quote.source}·估值字段",
    )


def build_lhb_summary(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary | None = None) -> LhbSummary:
    quote = analysis.quote
    reasons: list[str] = []
    action_items: list[str] = []
    if abs(quote.change_pct) >= 7:
        reasons.append(f"当日涨跌幅 {quote.change_pct:.2f}%，接近龙虎榜常见异动观察区。")
        action_items.append("收盘后核查是否进入龙虎榜异动名单。")
    if quote.turnover_rate is not None and quote.turnover_rate >= 12:
        reasons.append(f"换手率 {quote.turnover_rate:.2f}%，短线资金博弈强。")
        action_items.append("重点看买一/卖一席位是否集中，机构与游资是否同向。")
    if abnormal_events:
        reasons.extend(item.title for item in abnormal_events.events[:3])
    if quote.amount >= 1_000_000_000:
        action_items.append("成交额较大时，核查榜单净买入额占成交额比例，避免只看绝对金额。")
    if analysis.trend_score < 45 and reasons:
        action_items.append("趋势偏弱时，即使上榜也先判断是修复还是出货。")
    score = _clamp(42 + len(reasons) * 10 + (8 if abs(quote.change_pct) >= 9 else 0))
    level = _score_level(score)
    summary = "龙虎榜正式席位数据源待接入，当前先根据涨跌幅、换手和异动强度提示关注价值。"
    if reasons:
        summary = "存在短线异动特征，适合在正式龙虎榜源接入后重点核查买卖席位。"
    return LhbSummary(
        symbol=f"{quote.code}.{quote.market}",
        available=False,
        updated_at=quote.timestamp,
        score=score,
        level=level,
        summary=summary,
        reasons=reasons or ["未触发明显龙虎榜前置观察条件。"],
        seats=[],
        missing_data=["龙虎榜上榜日期", "买入席位", "卖出席位", "净买入额", "游资/机构标签"],
        action_items=action_items or ["未触发强异动时，龙虎榜不是当前分析主线。"],
        reliability="正式榜单待接入，当前为前置候选判断",
        source="预留接口·本地异动前置判断",
    )


def build_abnormal_events(analysis: AnalysisResult) -> AbnormalEventSummary:
    quote = analysis.quote
    rows = analysis.klines[-25:]
    events: list[AbnormalEventItem] = []
    latest_date = quote.timestamp
    prev_close = quote.prev_close or (rows[-2].close if len(rows) >= 2 else quote.open)
    change_pct = quote.change_pct
    avg_volume = _avg_volume(rows[:-1], 5)
    latest_volume = rows[-1].volume if rows else quote.volume
    volume_ratio = latest_volume / avg_volume if avg_volume else None
    amplitude_pct = (quote.high - quote.low) / prev_close * 100 if prev_close else 0

    if volume_ratio and volume_ratio >= 1.8 and change_pct > 1:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="放量上涨",
                level="积极" if change_pct < 7 else "观察",
                direction="向上",
                description=f"成交量约为近5日均量 {volume_ratio:.2f} 倍，价格上涨 {change_pct:.2f}%。",
                evidence=[f"量比估算 {volume_ratio:.2f}", f"涨跌幅 {change_pct:.2f}%"],
                watch_points=["次日若缩量跌回突破位，信号需要降级。"],
            )
        )
    if volume_ratio and volume_ratio >= 1.8 and change_pct < -1:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="放量下跌",
                level="风险",
                direction="向下",
                description=f"成交量约为近5日均量 {volume_ratio:.2f} 倍，同时价格下跌 {abs(change_pct):.2f}%。",
                evidence=[f"量比估算 {volume_ratio:.2f}", f"跌幅 {abs(change_pct):.2f}%"],
                watch_points=["先观察是否止跌放缓，避免把放量下跌误判成洗盘。"],
            )
        )
    if prev_close and quote.open >= prev_close * 1.015:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="向上跳空",
                level="积极" if quote.price >= quote.open else "观察",
                direction="向上",
                description=f"开盘价较昨收高开 {pct_change(quote.open, prev_close):.2f}%。",
                evidence=[f"开盘 {quote.open:.2f}", f"昨收 {prev_close:.2f}"],
                watch_points=["观察缺口是否被快速回补。"],
            )
        )
    if prev_close and quote.open <= prev_close * 0.985:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="向下跳空",
                level="风险",
                direction="向下",
                description=f"开盘价较昨收低开 {abs(pct_change(quote.open, prev_close)):.2f}%。",
                evidence=[f"开盘 {quote.open:.2f}", f"昨收 {prev_close:.2f}"],
                watch_points=["向下缺口未回补前，短线反弹质量要打折。"],
            )
        )
    body_high = max(quote.open, quote.price)
    body_low = min(quote.open, quote.price)
    upper_shadow_pct = (quote.high - body_high) / prev_close * 100 if prev_close else 0
    lower_shadow_pct = (body_low - quote.low) / prev_close * 100 if prev_close else 0
    if upper_shadow_pct >= 2.5 and upper_shadow_pct > lower_shadow_pct * 1.4:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="长上影压力",
                level="风险" if change_pct <= 0 else "观察",
                direction="压力",
                description=f"上影线约 {upper_shadow_pct:.2f}%，盘中冲高后承压。",
                evidence=[f"最高 {quote.high:.2f}", f"现价 {quote.price:.2f}"],
                watch_points=["若后续不能重新站回上影线中部，压力仍在。"],
            )
        )
    if lower_shadow_pct >= 2.5 and lower_shadow_pct > upper_shadow_pct * 1.4:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="长下影承接",
                level="积极" if change_pct >= 0 else "观察",
                direction="承接",
                description=f"下影线约 {lower_shadow_pct:.2f}%，低位出现承接迹象。",
                evidence=[f"最低 {quote.low:.2f}", f"现价 {quote.price:.2f}"],
                watch_points=["承接需要后续放量站稳短期均线确认。"],
            )
        )
    if change_pct >= 9:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="接近涨停",
                level="积极",
                direction="向上",
                description=f"涨幅 {change_pct:.2f}%，短线情绪很强。",
                evidence=[f"涨跌幅 {change_pct:.2f}%"],
                watch_points=["高情绪日后要关注开板、放量滞涨和次日承接。"],
            )
        )
    if change_pct <= -9:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="接近跌停",
                level="风险",
                direction="向下",
                description=f"跌幅 {abs(change_pct):.2f}%，短线风险释放剧烈。",
                evidence=[f"涨跌幅 {change_pct:.2f}%"],
                watch_points=["先等待流动性和承接恢复，不急于判断反转。"],
            )
        )
    if amplitude_pct >= 6:
        events.append(
            AbnormalEventItem(
                date=latest_date,
                title="日内大振幅",
                level="观察",
                direction="波动",
                description=f"日内振幅约 {amplitude_pct:.2f}%，多空分歧较大。",
                evidence=[f"最高 {quote.high:.2f}", f"最低 {quote.low:.2f}"],
                watch_points=["振幅放大时，策略参考价要留出更宽容错。"],
            )
        )

    risk_count = sum(1 for item in events if item.level == "风险")
    positive_count = sum(1 for item in events if item.level == "积极")
    score = _clamp(50 + positive_count * 10 - risk_count * 12 + min(len(events), 4) * 3)
    level = "风险" if risk_count > positive_count else _score_level(score)
    main_signal = events[0].title if events else "暂无明显异动"
    return AbnormalEventSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        score=score,
        level=level,
        main_signal=main_signal,
        events=events[:8],
        notes=[
            "异动基于本地行情和K线估算，用于解释当天发生了什么。",
            "正式公告、龙虎榜和逐笔成交接入后，可进一步确认异动来源。",
        ],
    )


def build_event_summary(
    analysis: AnalysisResult,
    *,
    abnormal_events: AbnormalEventSummary | None = None,
    lhb: LhbSummary | None = None,
) -> StockEventSummary:
    quote = analysis.quote
    events: list[StockEventItem] = []
    if analysis.review:
        for item in analysis.review.events:
            events.append(
                StockEventItem(
                    date=item.date,
                    title=item.title,
                    category="历史复盘",
                    level=item.level,
                    description=item.description,
                source="本地K线复盘",
                reliability="本地复盘",
                action_hint="用于理解历史波动，不等同于最新消息。",
                )
            )
    if analysis.industry_context:
        level = "积极" if analysis.industry_context.change_pct > 1 else "风险" if analysis.industry_context.change_pct < -1 else "观察"
        events.append(
            StockEventItem(
                date=analysis.industry_context.updated_at,
                title="行业背景变化",
                category="行业",
                level=level,
                description=f"{analysis.industry_context.name} 当前涨跌幅 {analysis.industry_context.change_pct:.2f}%。",
                source=analysis.industry_context.source,
                reliability="公开板块数据",
                action_hint="结合个股强弱判断是否跟随行业。",
            )
        )
    if abnormal_events:
        for item in abnormal_events.events[:4]:
            events.append(
                StockEventItem(
                    date=item.date,
                    title=item.title,
                    category="异动",
                    level=item.level,
                    description=item.description,
                    source="行情异动识别",
                    reliability="行情推断",
                    action_hint=(item.watch_points or ["观察后续确认。"])[0],
                )
            )
    if lhb and lhb.available:
        events.append(
            StockEventItem(
                date=lhb.updated_at,
                title="龙虎榜信号",
                category="龙虎榜",
                level=lhb.level,
                description=lhb.summary,
                source=lhb.source,
                reliability=lhb.reliability,
                action_hint=(lhb.action_items or ["核查正式龙虎榜。"])[0],
            )
        )
    for note in analysis.data_quality.anomalies[:3]:
        events.append(
            StockEventItem(
                date=analysis.data_quality.checked_at or quote.timestamp,
                title="数据质量提醒",
                category="数据",
                level="观察",
                description=note,
                source=analysis.data_quality.source,
                reliability="系统检测",
                action_hint="数据质量下降时，所有策略结论自动降权。",
            )
        )
    for item in _external_event_placeholders(analysis, lhb):
        events.append(item)
    if not events:
        events.append(
            StockEventItem(
                date=quote.timestamp,
                title="暂无高强度事件",
                category="观察",
                level="观察",
                description="当前未从K线、行业和数据质量中识别出明显事件，公告/研报源接入后会补充。",
                source="本地分析",
                reliability="本地推断",
                action_hint="继续观察行情、行业和数据质量变化。",
            )
        )
    return StockEventSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=now_text(),
        events=events[-8:],
        notes=[
            "事件层已区分正式数据、公开板块和本地行情推断；缺正式源时自动标注可靠性。",
            "公告、研报、龙虎榜、融资融券接入后可替换候选事件。",
        ],
        missing_sources=["交易所公告", "龙虎榜席位", "融资融券余额", "研报摘要"],
        next_steps=_event_next_steps(analysis, lhb),
    )


def _external_event_placeholders(analysis: AnalysisResult, lhb: LhbSummary | None) -> list[StockEventItem]:
    quote = analysis.quote
    events: list[StockEventItem] = []
    if lhb and lhb.reasons:
        events.append(
            StockEventItem(
                date=quote.timestamp,
                title="龙虎榜候选核查",
                category="龙虎榜",
                level=lhb.level,
                description="已触发龙虎榜前置候选条件，但尚未接入正式席位明细。",
                source=lhb.source,
                reliability=lhb.reliability,
                action_hint=(lhb.action_items or ["收盘后核查正式榜单。"])[0],
            )
        )
    if abs(quote.change_pct) >= 5 or analysis.risk_level == "高风险":
        events.append(
            StockEventItem(
                date=quote.timestamp,
                title="公告事件待核查",
                category="公告",
                level="观察" if quote.change_pct >= 0 else "风险",
                description="价格波动较大，建议核查是否有公告、监管问询、业绩预告或重大事项影响。",
                source="预留接口·公告核查清单",
                reliability="待接入",
                action_hint="正式公告源接入前，不把消息面作为确定性结论。",
            )
        )
    if quote.turnover_rate is not None and quote.turnover_rate >= 8:
        events.append(
            StockEventItem(
                date=quote.timestamp,
                title="融资融券待核查",
                category="融资融券",
                level="观察",
                description="换手活跃，若融资余额快速上升，需警惕杠杆资金追涨；若融券增加，需关注分歧。",
                source="预留接口·两融核查清单",
                reliability="待接入",
                action_hint="两融数据未接入前，仅作为下一步核查项。",
            )
        )
    return events[:3]


def _event_next_steps(analysis: AnalysisResult, lhb: LhbSummary | None) -> list[str]:
    steps = ["优先确认数据质量，低质量行情下事件结论自动降权。"]
    if lhb and lhb.reasons:
        steps.append("收盘后核查龙虎榜席位、净买入额和机构/游资方向。")
    if abs(analysis.quote.change_pct) >= 5:
        steps.append("核查交易所公告、业绩预告、监管问询和行业新闻。")
    if analysis.quote.turnover_rate is not None and analysis.quote.turnover_rate >= 8:
        steps.append("补充融资融券余额变化，判断活跃成交是否带有杠杆资金。")
    return steps[:4]


def build_strategy_cards(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> list[StrategyCard]:
    cards = [
        _strategy_from_signal(
            "趋势回踩策略",
            analysis.buy_points[0],
            status=_quality_strategy_status(
                "满足" if analysis.trend_score >= 65 and analysis.quote.price >= analysis.ma5 else "等待",
                analysis,
            ),
            reference_price=f"5日线 {analysis.ma5:.2f}",
            invalidation=f"跌破支撑 {analysis.support:.2f}",
            suitable_for="能接受等待确认的新手和波段观察者",
        ),
        _strategy_from_signal(
            "突破确认策略",
            SignalItem(
                title="压力突破确认",
                level=_quality_signal_level("积极" if analysis.quote.price >= analysis.resistance * 0.99 and fund_flow.overall_score >= 60 else "观察", analysis),
                reason=f"关注 {analysis.resistance:.2f} 附近是否放量站稳，资金面评分 {fund_flow.overall_score}。",
            ),
            status=_quality_strategy_status("接近触发" if analysis.quote.price >= analysis.resistance * 0.985 else "等待", analysis),
            reference_price=f"压力位 {analysis.resistance:.2f}",
            invalidation="突破后快速跌回压力位下方",
            suitable_for="偏右侧确认的使用者",
        ),
        _strategy_from_signal(
            "支撑低吸策略",
            SignalItem(
                title="支撑区小仓观察",
                level=_quality_signal_level("谨慎", analysis),
                reason=f"价格靠近 {analysis.support:.2f} 时只适合观察承接，不能越跌越加。",
            ),
            status=_quality_strategy_status("接近触发" if analysis.support and analysis.quote.price <= analysis.support * 1.03 else "等待", analysis),
            reference_price=f"支撑位 {analysis.support:.2f}",
            invalidation=f"有效跌破 {analysis.support:.2f}",
            suitable_for="只做小仓位试错的使用者",
        ),
        _strategy_from_signal(
            "做T区间策略",
            _t_plan_signal(analysis.t_plan),
            status=_quality_strategy_status("仅底仓适用", analysis),
            reference_price=f"{analysis.support:.2f} - {analysis.resistance:.2f}",
            invalidation="当日波动过小或盘口卖压明显增强",
            suitable_for="已有可卖底仓的使用者",
            extra_evidence=[order_pressure.summary, f"数据质量 {analysis.data_quality.level}，信号已自动降权。"],
        ),
        _strategy_from_signal(
            "风险止损策略",
            analysis.sell_points[0],
            status=_quality_strategy_status("触发" if analysis.risk_level in {"中等风险", "高风险"} else "备用", analysis),
            reference_price=f"20日线 {analysis.ma20:.2f}",
            invalidation="重新站回5日线且资金面改善",
            suitable_for="优先控制回撤的使用者",
        ),
    ]
    return cards


def _t_plan_signal(items: list[SignalItem]) -> SignalItem:
    for item in items:
        if "高抛" in item.title:
            return item
    return items[-1]


def rule_definitions() -> list[RuleDefinition]:
    return [
        RuleDefinition(
            id="volume_breakout_20d",
            name="放量突破20日高点",
            category="趋势",
            description="价格接近或突破近20日高点，同时量能明显高于近5日均量。",
            beginner_hint="这是右侧确认信号，重点看突破后是否站稳，而不是盘中一冲就追。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["volume_breakout_20d"],
        ),
        RuleDefinition(
            id="break_ma20_risk",
            name="跌破20日线风险",
            category="风控",
            description="现价低于20日均线且趋势评分偏弱。",
            beginner_hint="20日线是波段风控线，跌破后先降低乐观预期。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["break_ma20_risk"],
        ),
        RuleDefinition(
            id="support_rebound_watch",
            name="支撑位止跌观察",
            category="买点观察",
            description="价格接近支撑位，下影或资金表现出现承接迹象。",
            beginner_hint="这是观察信号，不是越跌越买；必须有止跌证据。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["support_rebound_watch"],
        ),
        RuleDefinition(
            id="fund_tech_divergence",
            name="资金技术背离",
            category="资金",
            description="趋势与资金评分出现明显分歧。",
            beginner_hint="分歧阶段不要只看一个指标，等待价格或资金给出一致方向。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["fund_tech_divergence"],
        ),
        RuleDefinition(
            id="high_valuation_chase_risk",
            name="高估值追高风险",
            category="估值",
            description="趋势强但估值压力偏高，容易出现波动放大。",
            beginner_hint="强势股也需要风控线，估值越贵越不能忽略失效条件。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["high_valuation_chase_risk"],
        ),
        RuleDefinition(
            id="abnormal_risk_event",
            name="风险异动降级",
            category="事件",
            description="出现放量下跌、跌停附近、长上影等风险异动。",
            beginner_hint="风险异动先解释原因，再决定是否继续观察。",
            version=RULE_VERSION,
            parameters=RULE_CONFIG["abnormal_risk_event"],
        ),
    ]


def build_rule_match_summary(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    valuation: ValuationAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> StockRuleMatchSummary:
    rows = analysis.klines[-25:]
    latest_high_20 = max((item.high for item in rows[-20:]), default=analysis.resistance)
    volume_ratio = None
    avg_volume = _avg_volume(rows[:-1], 5)
    if rows and avg_volume:
        volume_ratio = rows[-1].volume / avg_volume
    quote = analysis.quote
    definitions = rule_definitions()
    matches = [
        _apply_quality_gate(_rule_volume_breakout(analysis, latest_high_20, volume_ratio), analysis),
        _apply_quality_gate(_rule_break_ma20(analysis), analysis),
        _apply_quality_gate(_rule_support_rebound(analysis, fund_flow, abnormal_events), analysis),
        _apply_quality_gate(_rule_fund_tech_divergence(analysis, fund_flow, order_pressure), analysis),
        _apply_quality_gate(_rule_high_valuation_chase(analysis, valuation), analysis),
        _apply_quality_gate(_rule_abnormal_risk(analysis, abnormal_events), analysis),
    ]
    matches = sorted(matches, key=_rule_sort_key)
    top_level = "观察"
    if any(item.level == "风险" and item.status == "命中" for item in matches):
        top_level = "风险"
    elif any(item.level == "积极" and item.status == "命中" for item in matches):
        top_level = "积极"
    return StockRuleMatchSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        matched_count=sum(1 for item in matches if item.status == "命中"),
        top_level=top_level,
        matches=matches,
        definitions=definitions,
    )


def _strategy_from_signal(
    name: str,
    signal: SignalItem,
    *,
    status: str,
    reference_price: str,
    invalidation: str,
    suitable_for: str,
    extra_evidence: list[str] | None = None,
) -> StrategyCard:
    return StrategyCard(
        name=name,
        status=status,
        level=signal.level,
        trigger_conditions=[signal.title],
        current_evidence=[signal.reason, *(extra_evidence or [])],
        reference_price=reference_price,
        invalidation=invalidation,
        suitable_for=suitable_for,
        risk_note="策略卡只用于个股研究辅助，不代表确定性买卖点。",
    )


def _quality_strategy_status(status: str, analysis: AnalysisResult) -> str:
    score = analysis.data_quality.score
    if score < 50:
        if status in {"满足", "触发", "接近触发"}:
            return "暂停观察"
        if status == "仅底仓适用":
            return "暂停做T"
        return "暂停"
    if score < 70:
        if status in {"满足", "触发"}:
            return "等待确认"
        if status == "接近触发":
            return "观察"
        if status == "仅底仓适用":
            return "仅底仓适用（降权）"
    return status


def _quality_signal_level(level: str, analysis: AnalysisResult) -> str:
    score = analysis.data_quality.score
    if score < 50:
        return "风险" if level != "风险" else level
    if score < 70 and level in {"积极", "观察"}:
        return "谨慎"
    return level


def _apply_quality_gate(match: RuleMatch, analysis: AnalysisResult) -> RuleMatch:
    score = analysis.data_quality.score
    if score >= 70:
        return match
    confidence = match.confidence
    evidence = list(match.evidence)
    reason_suffix = f"数据质量{analysis.data_quality.level}，该规则结论已降权。"
    if match.level == "风险":
        confidence = max(20, round(confidence * 0.85))
        evidence.append(reason_suffix)
        return match.model_copy(update={"confidence": confidence, "evidence": evidence})
    if match.status == "命中":
        status = "接近"
        confidence = max(28, confidence - 18)
    elif match.status == "接近":
        status = "未触发" if score < 50 else "接近"
        confidence = max(24, confidence - 10)
    else:
        status = match.status
        confidence = max(20, confidence - 6)
    level = "观察"
    if match.level == "积极" and score < 50:
        level = "谨慎"
    evidence.append(reason_suffix)
    return match.model_copy(update={"status": status, "level": level, "confidence": confidence, "evidence": evidence})


def _rule_sort_key(item: RuleMatch) -> tuple[int, int, int]:
    status_rank = {"命中": 0, "接近": 1, "未触发": 2}.get(item.status, 3)
    level_rank = {"风险": 0, "积极": 1, "观察": 2, "谨慎": 3, "中性": 4}.get(item.level, 5)
    return status_rank, level_rank, -item.confidence


def _rule_volume_breakout(analysis: AnalysisResult, latest_high_20: float, volume_ratio: float | None) -> RuleMatch:
    quote = analysis.quote
    config = RULE_CONFIG["volume_breakout_20d"]
    near_breakout = bool(latest_high_20 and quote.price >= latest_high_20 * float(config["near_breakout_pct"]))
    enough_volume = volume_ratio is not None and volume_ratio >= float(config["volume_ratio"])
    status = "命中" if near_breakout and enough_volume else "接近" if near_breakout or enough_volume else "未触发"
    confidence = 78 if status == "命中" else 56 if status == "接近" else 35
    evidence = [f"现价 {quote.price:.2f} / 20日高点 {latest_high_20:.2f}"]
    if volume_ratio is not None:
        evidence.append(f"量比估算 {volume_ratio:.2f}")
    missing = [] if volume_ratio is not None else ["近5日成交量"]
    return RuleMatch(
        rule_id="volume_breakout_20d",
        name="放量突破20日高点",
        category="趋势",
        status=status,
        level="积极" if status == "命中" else "观察",
        confidence=confidence,
        reason="；".join(evidence),
        actions=["只把站稳压力位后的回踩作为确认点。", "突破当日若放量过猛，次日承接更关键。"],
        invalidation=f"跌回压力位 {analysis.resistance:.2f} 下方或量能快速萎缩。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
        missing_data=missing,
    )


def _rule_break_ma20(analysis: AnalysisResult) -> RuleMatch:
    quote = analysis.quote
    config = RULE_CONFIG["break_ma20_risk"]
    broken = quote.price < analysis.ma20 and analysis.trend_score < float(config["trend_score"])
    close = quote.price < analysis.ma20 * float(config["near_ma20_pct"])
    status = "命中" if broken else "接近" if close else "未触发"
    evidence = [f"现价 {quote.price:.2f}", f"20日线 {analysis.ma20:.2f}", f"趋势评分 {analysis.trend_score}"]
    return RuleMatch(
        rule_id="break_ma20_risk",
        name="跌破20日线风险",
        category="风控",
        status=status,
        level="风险" if status == "命中" else "观察",
        confidence=82 if status == "命中" else 58 if status == "接近" else 38,
        reason="，".join(evidence) + "。",
        actions=["跌破后先观察能否快速收回20日线。", "若同时跌破支撑位，当前建议需要降级。"],
        invalidation=f"重新站上20日线 {analysis.ma20:.2f} 且趋势评分回到50以上。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
    )


def _rule_support_rebound(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> RuleMatch:
    quote = analysis.quote
    config = RULE_CONFIG["support_rebound_watch"]
    near_support = bool(analysis.support and quote.price <= analysis.support * float(config["near_support_pct"]))
    has_rebound = any(item.title == "长下影承接" for item in abnormal_events.events) or fund_flow.overall_score >= float(config["fund_score"])
    status = "命中" if near_support and has_rebound else "接近" if near_support else "未触发"
    evidence = [f"现价 {quote.price:.2f}", f"支撑 {analysis.support:.2f}", f"资金评分 {fund_flow.overall_score}"]
    return RuleMatch(
        rule_id="support_rebound_watch",
        name="支撑位止跌观察",
        category="买点观察",
        status=status,
        level="观察" if status != "未触发" else "中性",
        confidence=72 if status == "命中" else 54 if status == "接近" else 32,
        reason="，".join(evidence) + "。",
        actions=["只适合作为观察点，等待短周期止跌确认。", "若跌破支撑，不做摊低成本式加仓建议。"],
        invalidation=f"有效跌破支撑 {analysis.support:.2f}。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
    )


def _rule_fund_tech_divergence(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> RuleMatch:
    config = RULE_CONFIG["fund_tech_divergence"]
    positive_divergence = analysis.trend_score < float(config["trend_weak"]) and fund_flow.overall_score >= float(config["fund_strong"])
    negative_divergence = analysis.trend_score >= float(config["trend_strong"]) and fund_flow.overall_score < float(config["fund_weak"])
    status = "命中" if positive_divergence or negative_divergence else "接近" if abs(analysis.trend_score - fund_flow.overall_score) >= float(config["gap"]) else "未触发"
    level = "观察"
    if negative_divergence or "卖压" in order_pressure.pressure_level:
        level = "风险" if status == "命中" else "观察"
    evidence = [f"趋势评分 {analysis.trend_score}", f"资金评分 {fund_flow.overall_score}", f"盘口 {order_pressure.pressure_level}"]
    return RuleMatch(
        rule_id="fund_tech_divergence",
        name="资金技术背离",
        category="资金",
        status=status,
        level=level,
        confidence=74 if status == "命中" else 55 if status == "接近" else 34,
        reason="，".join(evidence) + "。",
        actions=["等趋势和资金至少一方完成修复后再提高信号权重。", "出现背离时，不把单一强项当作完整买卖依据。"],
        invalidation="趋势评分和资金评分重新回到同向区间。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
        missing_data=[] if fund_flow.available else ["逐笔资金流"],
    )


def _rule_high_valuation_chase(analysis: AnalysisResult, valuation: ValuationAnalysis) -> RuleMatch:
    config = RULE_CONFIG["high_valuation_chase_risk"]
    hit = analysis.trend_score >= float(config["trend_hit"]) and valuation.score < float(config["valuation_hit"])
    close = analysis.trend_score >= float(config["trend_close"]) and valuation.score < float(config["valuation_close"])
    status = "命中" if hit else "接近" if close else "未触发"
    evidence = [f"趋势评分 {analysis.trend_score}", f"估值评分 {valuation.score}", valuation.summary]
    return RuleMatch(
        rule_id="high_valuation_chase_risk",
        name="高估值追高风险",
        category="估值",
        status=status,
        level="风险" if status == "命中" else "观察",
        confidence=76 if status == "命中" else 55 if status == "接近" else 30,
        reason=f"趋势评分 {analysis.trend_score}，估值评分 {valuation.score}。{valuation.summary}",
        actions=["强趋势里更重视失效价，不用估值 alone 逆势判断顶部。", "若放量滞涨或跌破5日线，及时降低信号等级。"],
        invalidation="估值评分改善，或趋势从急涨进入健康整理后重新评估。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
        missing_data=valuation.missing_data,
    )


def _rule_abnormal_risk(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary) -> RuleMatch:
    risk_events = [item for item in abnormal_events.events if item.level == "风险"]
    status = "命中" if risk_events else "接近" if abnormal_events.events else "未触发"
    reason = "；".join(item.title for item in risk_events[:3]) if risk_events else abnormal_events.main_signal
    evidence = [item.title for item in risk_events[:3]] or [abnormal_events.main_signal]
    return RuleMatch(
        rule_id="abnormal_risk_event",
        name="风险异动降级",
        category="事件",
        status=status,
        level="风险" if risk_events else "观察",
        confidence=78 if risk_events else 52 if abnormal_events.events else 28,
        reason=f"{reason}。当前风险等级：{analysis.risk_level}。",
        actions=["先解释异动来源，再看关键价位是否失守。", "风险异动叠加数据质量异常时，建议结论自动降权。"],
        invalidation="风险异动后的2到3个交易日内重新站稳短期均线且量能恢复正常。",
        rule_version=RULE_VERSION,
        score_version=SCORE_VERSION,
        evidence=evidence,
    )


def _technical_factor(analysis: AnalysisResult) -> FactorScore:
    evidence = [
        f"趋势评分 {analysis.trend_score}/100，状态为{analysis.trend_label}。",
        f"现价 {analysis.quote.price:.2f}，5日线 {analysis.ma5:.2f}，20日线 {analysis.ma20:.2f}。",
    ]
    evidence.extend(
        f"{item.name}{item.impact:+d}：{item.reason}"
        for item in [*analysis.signal_snapshot.positive[:2], *analysis.signal_snapshot.negative[:2]]
    )
    return FactorScore(
        name="技术面",
        score=analysis.trend_score,
        level=_score_level(analysis.trend_score),
        summary=analysis.trend_label,
        evidence=evidence,
    )


def _fund_factor(fund_flow: FundFlowAnalysis) -> FactorScore:
    return FactorScore(
        name="资金面",
        score=fund_flow.overall_score,
        level=fund_flow.level,
        summary=fund_flow.price_volume_relation,
        evidence=[item.summary for item in fund_flow.windows],
        missing_data=[] if fund_flow.available else ["逐笔大单/特大单资金流"],
    )


def _fundamental_factor(analysis: AnalysisResult) -> FactorScore:
    quote = analysis.quote
    score = 55
    evidence = []
    missing = []
    if quote.pe:
        score += 8 if quote.pe < 25 else -8 if quote.pe > 60 else 0
        evidence.append(f"PE {quote.pe:.2f}")
    else:
        missing.append("PE")
    if quote.pb:
        score += 6 if quote.pb < 3 else -6 if quote.pb > 8 else 0
        evidence.append(f"PB {quote.pb:.2f}")
    else:
        missing.append("PB")
    if quote.market_cap:
        evidence.append(f"总市值 {quote.market_cap / 100000000:.1f} 亿")
    else:
        missing.append("市值")
    if analysis.stock_profile and analysis.stock_profile.industry:
        evidence.append(f"行业：{analysis.stock_profile.industry}")
    else:
        missing.append("行业/财务明细")
    score = _clamp(score)
    return FactorScore(
        name="基本面",
        score=score,
        level=_score_level(score),
        summary="估值字段可用" if evidence else "基础财务数据待接入",
        evidence=evidence or ["当前只有行情字段，财报指标待接入。"],
        missing_data=missing,
    )


def _event_factor(events: StockEventSummary) -> FactorScore:
    risk_count = sum(1 for item in events.events if item.level == "风险")
    positive_count = sum(1 for item in events.events if item.level == "积极")
    score = _clamp(58 + positive_count * 8 - risk_count * 10)
    return FactorScore(
        name="事件面",
        score=score,
        level=_score_level(score),
        summary="事件偏积极" if positive_count > risk_count else "事件需观察" if events.events else "暂无事件",
        evidence=[f"{item.category}：{item.title}" for item in events.events[:4]],
        missing_data=["公告全文", "研报摘要", "龙虎榜"] if events.notes else [],
    )


def _risk_factor(analysis: AnalysisResult, order_pressure: OrderPressure) -> FactorScore:
    risk_score = 100 - max(0, min(100, analysis.data_quality.score))
    if analysis.risk_level == "高风险":
        risk_score += 30
    elif analysis.risk_level == "中等风险":
        risk_score += 18
    if "卖压" in order_pressure.pressure_level:
        risk_score += 10
    if analysis.signal_snapshot.confidence < 60:
        risk_score += 10
    score = _clamp(100 - risk_score)
    return FactorScore(
        name="风险面",
        score=score,
        level=_score_level(score),
        summary=analysis.risk_level,
        evidence=[
            analysis.action_advice.reason,
            order_pressure.summary,
            f"信号可信度 {analysis.signal_snapshot.confidence}%，数据质量 {analysis.data_quality.score} 分。",
        ],
        missing_data=[] if order_pressure.available else ["实时五档盘口"],
    )


def _main_conflict(analysis: AnalysisResult, fund_flow: FundFlowAnalysis, order_pressure: OrderPressure) -> str:
    if analysis.data_quality.score < 50:
        return "数据质量较弱，当前所有买卖点、做T和规则命中都只能低置信观察。"
    if analysis.signal_snapshot.confidence < 60:
        return "趋势证据和数据可信度都不够强，先降低操作频率，等待更清晰的确认。"
    if analysis.trend_score < 45 and fund_flow.overall_score >= 60:
        return "资金面有尝试修复，但技术趋势仍偏弱，先等价格重新站稳短期均线。"
    if analysis.trend_score >= 65 and fund_flow.overall_score < 50:
        return "技术面尚可，但资金跟随不足，突破信号需要继续确认。"
    if "卖压" in order_pressure.pressure_level:
        return "盘口或价格位置显示上方压力，短线不宜追高。"
    return "当前主要矛盾是趋势确认和风险控制，优先观察关键价位是否有效。"


def _risk_triggers(analysis: AnalysisResult, order_pressure: OrderPressure) -> list[str]:
    triggers = [
        f"有效跌破支撑位 {analysis.support:.2f}",
        f"收盘跌破20日线 {analysis.ma20:.2f}",
        "数据质量降为“一般”以下",
    ]
    if "卖压" in order_pressure.pressure_level:
        triggers.append("盘口卖压持续强于买盘")
    if analysis.data_quality.anomalies:
        triggers.append("行情字段异常未修复")
    return triggers


def _volume_score(rows) -> int:
    if len(rows) < 6:
        return 50
    latest = rows[-1].volume
    avg = sum(item.volume for item in rows[-6:-1]) / 5
    if avg <= 0:
        return 50
    ratio = latest / avg
    if 1.2 <= ratio <= 2.5:
        return 68
    if ratio > 3:
        return 45
    if ratio < 0.7:
        return 42
    return 56


def _recent_momentum_score(klines, window: int) -> int:
    rows = klines[-window:]
    if len(rows) < 2:
        return 50
    positive = sum(1 for index in range(1, len(rows)) if rows[index].close >= rows[index - 1].close)
    return _clamp(round(35 + positive / (len(rows) - 1) * 45))


def _recent_window_summary(klines, window: int) -> str:
    rows = klines[-window:]
    if len(rows) < 2:
        return f"{window}日数据不足。"
    change = pct_change(rows[-1].close, rows[0].close)
    return f"近{len(rows)}日区间涨跌 {change:.2f}%。"


def _avg_volume(rows, window: int) -> float | None:
    sample = rows[-window:]
    if not sample:
        return None
    total = sum(item.volume for item in sample)
    return total / len(sample) if total > 0 else None


def _missing_metric(name: str, summary: str) -> FinancialMetric:
    return FinancialMetric(name=name, value="待接入", level="观察", summary=summary, source="数据缺失")


def _pe_view(pe: float) -> tuple[int, str, str]:
    if pe <= 0:
        return 28, "弱", "PE 为负或无有效意义，需检查盈利质量。"
    if pe < 12:
        return 68, "偏强", "PE 较低，可能有安全边际，也可能反映增长预期不足。"
    if pe <= 35:
        return 62, "偏强", "PE 处在较容易解释的区间。"
    if pe <= 60:
        return 48, "中性", "PE 偏高，需要业绩增长继续配合。"
    return 30, "偏弱", "PE 明显偏高，估值压缩风险需要重点关注。"


def _pb_view(pb: float) -> tuple[int, str, str]:
    if pb <= 0:
        return 30, "弱", "PB 异常，需确认净资产或行情字段。"
    if pb < 1.2:
        return 66, "偏强", "PB 较低，资产价格锚相对清晰。"
    if pb <= 4:
        return 60, "偏强", "PB 处在较常见区间。"
    if pb <= 8:
        return 46, "中性", "PB 偏高，需要盈利能力或成长性支撑。"
    return 32, "偏弱", "PB 较高，市场对盈利和成长要求更苛刻。"


def _market_cap_view(market_cap: float) -> tuple[int, str, str]:
    yi = market_cap / 100_000_000
    if yi >= 2000:
        return 64, "偏强", "超大市值，流动性和机构关注度通常更好。"
    if yi >= 500:
        return 60, "偏强", "中大型市值，交易容量相对友好。"
    if yi >= 100:
        return 52, "中性", "中等市值，弹性和波动都需要兼顾。"
    if yi >= 30:
        return 45, "观察", "小市值弹性较高，但波动和流动性风险也更高。"
    return 36, "偏弱", "微小市值更容易受流动性和情绪冲击。"


def _liquidity_view(amount: float, turnover_rate: float | None) -> tuple[int, str, str]:
    score = 45
    if amount >= 1_000_000_000:
        score += 22
    elif amount >= 300_000_000:
        score += 14
    elif amount >= 80_000_000:
        score += 7
    else:
        score -= 6
    if turnover_rate is not None:
        if 1 <= turnover_rate <= 8:
            score += 8
        elif turnover_rate > 15:
            score -= 4
        elif turnover_rate < 0.5:
            score -= 5
    score = _clamp(score)
    if score >= 65:
        return score, "偏强", "交易活跃度较好，个股分析参考性更高。"
    if score >= 50:
        return score, "中性", "交易活跃度尚可，需要结合盘口和成交连续性。"
    return score, "偏弱", "交易活跃度偏弱，价格信号可能更容易失真。"


def _financial_summary(score: int, missing: list[str]) -> str:
    base = "基础财务体检偏稳" if score >= 65 else "基础财务体检中性" if score >= 50 else "基础财务体检偏弱"
    if missing:
        return f"{base}，但仍缺少{missing[0]}等正式财报字段。"
    return base


def _valuation_summary(score: int, missing: list[str]) -> str:
    if missing and len(missing) >= 3:
        return "估值字段不足，暂只能做低置信度观察。"
    if score >= 65:
        return "估值压力相对可控，但仍需和趋势确认一起使用。"
    if score >= 50:
        return "估值处在中性区间，重点看业绩和行业背景能否配合。"
    return "估值压力偏高或字段质量不足，追高需要更严格的失效条件。"


def _price_percentile_from_klines(analysis: AnalysisResult) -> float | None:
    closes = [item.close for item in analysis.klines[-120:] if item.close > 0]
    if len(closes) < 20 or analysis.quote.price <= 0:
        return None
    below_or_equal = sum(1 for item in closes if item <= analysis.quote.price)
    return round(below_or_equal / len(closes) * 100, 1)


def _valuation_percentile_from_history(analysis: AnalysisResult, field: str) -> float | None:
    rows = _daily_quote_history_rows(getattr(analysis, "quote_history", []) or [])
    current = getattr(analysis.quote, field, None)
    if current is None or current <= 0:
        return None
    values = [float(row.get(field) or 0) for row in rows if float(row.get(field) or 0) > 0]
    if len(values) < MIN_VALUATION_HISTORY_ROWS:
        return None
    below_or_equal = sum(1 for item in values if item <= current)
    return round(below_or_equal / len(values) * 100, 1)


def _daily_quote_history_rows(rows: list[dict[str, float | str | None]]) -> list[dict[str, float | str | None]]:
    by_day: dict[str, dict[str, float | str | None]] = {}
    for index, row in enumerate(rows):
        raw_date = str(row.get("quote_timestamp") or row.get("fetched_at") or f"row-{index}")
        day = raw_date[:10]
        by_day[day] = row
    return list(by_day.values())


def _peer_valuation_percentile(analysis: AnalysisResult, field: str) -> float | None:
    current = getattr(analysis.quote, field, None)
    if current is None or current <= 0:
        return None
    peers = getattr(analysis, "peer_quotes", []) or []
    values = [float(getattr(item, field, 0) or 0) for item in peers if float(getattr(item, field, 0) or 0) > 0]
    if len(values) < MIN_PEER_VALUATION_ROWS:
        return None
    below_or_equal = sum(1 for item in values if item <= current)
    return round(below_or_equal / len(values) * 100, 1)


def _valuation_anchor_label(
    price_percentile: float | None,
    pe_percentile: float | None = None,
    pb_percentile: float | None = None,
    peer_pe_percentile: float | None = None,
    peer_pb_percentile: float | None = None,
) -> str:
    valuation_percentiles = [item for item in [pe_percentile, pb_percentile, peer_pe_percentile, peer_pb_percentile] if item is not None]
    if valuation_percentiles:
        percentile = sum(valuation_percentiles) / len(valuation_percentiles)
        prefix = "估值"
    elif price_percentile is not None:
        percentile = price_percentile
        prefix = "价格位置"
    else:
        return "历史锚待确认"
    if percentile >= 85:
        return f"高位{prefix}锚"
    if percentile >= 65:
        return f"偏高{prefix}锚"
    if percentile <= 20:
        return f"低位{prefix}锚"
    if percentile <= 35:
        return f"偏低{prefix}锚"
    return f"中性{prefix}锚"


def _valuation_percentile_score_delta(percentile: float, value: float | None) -> int:
    if value is not None and value <= 0:
        return -10
    if percentile >= 88:
        return -10
    if percentile >= 72:
        return -5
    if percentile <= 18:
        return 6
    if percentile <= 32:
        return 3
    return 0


def _peer_percentile_score_delta(percentile: float, value: float | None) -> int:
    if value is not None and value <= 0:
        return -8
    if percentile >= 85:
        return -8
    if percentile >= 72:
        return -4
    if percentile <= 18:
        return 5
    if percentile <= 32:
        return 2
    return 0


def _format_amount_text(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:.0f}"


def _price_volume_relation(analysis: AnalysisResult) -> str:
    rows = analysis.klines[-6:]
    quote = analysis.quote
    volume_score = _volume_score(rows)
    if quote.change_pct > 1 and volume_score >= 60:
        return "量价配合偏积极。"
    if quote.change_pct > 1 and volume_score < 50:
        return "价格上涨但量能跟随不足。"
    if quote.change_pct < -1 and volume_score >= 60:
        return "放量下跌，资金承压。"
    if quote.change_pct < -1:
        return "价格回落，量能未明显放大。"
    return "量价关系中性，等待更明确方向。"


def _score_level(score: int) -> str:
    if score >= 80:
        return "强"
    if score >= 65:
        return "偏强"
    if score >= 50:
        return "中性"
    if score >= 35:
        return "偏弱"
    return "弱"


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
