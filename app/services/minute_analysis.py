from __future__ import annotations

from statistics import mean

from app.models.schemas import MinuteAnalysisReport, MinuteKline, MinuteSupportResistance, MinuteTPlan
from app.services.indicators import pct_change
from app.utils.time import now_text


def build_minute_analysis_report(symbol: str, rows: list[MinuteKline], interval: str = "5m") -> MinuteAnalysisReport:
    normalized_interval = interval.lower()
    if len(rows) < 8:
        return _empty_report(symbol, rows, normalized_interval, "分钟K线样本不足，暂不能形成做T参考。")

    clean_rows = [item for item in rows if item.close > 0 and item.high >= item.low]
    if len(clean_rows) < 8:
        return _empty_report(symbol, clean_rows, normalized_interval, "有效分钟K线不足，暂不能形成做T参考。")

    latest = clean_rows[-1]
    first = clean_rows[0]
    latest_price = latest.close
    intraday_change_pct = round(pct_change(latest.close, first.open), 2)
    low = min(item.low for item in clean_rows)
    high = max(item.high for item in clean_rows)
    intraday_range_pct = round((high - low) / latest_price * 100, 2) if latest_price else 0
    short_ma = _ma(clean_rows, 6)
    long_ma = _ma(clean_rows, 18)
    prev_short_ma = _ma(clean_rows[:-3], 6) if len(clean_rows) >= 12 else short_ma
    trend_label = _minute_trend_label(latest.close, short_ma, long_ma, prev_short_ma)
    momentum_label = _momentum_label(clean_rows)
    volume_pulse = _volume_pulse(clean_rows)
    supports = _minute_levels(clean_rows, latest_price, "support")
    resistances = _minute_levels(clean_rows, latest_price, "resistance")
    t_plan = _minute_t_plan(clean_rows, latest_price, supports, resistances, trend_label, volume_pulse, intraday_range_pct)
    warnings = _minute_warnings(clean_rows, t_plan, intraday_range_pct, volume_pulse)
    source = latest.source or clean_rows[0].source or "分钟源待确认"
    return MinuteAnalysisReport(
        symbol=symbol,
        updated_at=latest.timestamp or now_text(),
        interval=normalized_interval,
        source=source,
        sample_count=len(clean_rows),
        latest_price=latest_price,
        intraday_change_pct=intraday_change_pct,
        intraday_range_pct=intraday_range_pct,
        volume_pulse=volume_pulse,
        trend_label=trend_label,
        momentum_label=momentum_label,
        summary=f"{normalized_interval} 分钟分析：盘中趋势「{trend_label}」，动量「{momentum_label}」，量能「{volume_pulse}」。做T结论：{t_plan.suitability}。",
        supports=supports,
        resistances=resistances,
        t_plan=t_plan,
        warnings=warnings,
        missing_data=[],
    )


def build_unavailable_minute_analysis_report(symbol: str, interval: str = "5m", reason: str | None = None) -> MinuteAnalysisReport:
    user_reason = "分钟K线数据源暂不可用，已暂停盘中做T判断。"
    if reason:
        user_reason = f"{user_reason}原因：{_compact_unavailable_reason(reason)}。"
    return _empty_report(symbol, [], interval.lower(), user_reason)


def _empty_report(symbol: str, rows: list[MinuteKline], interval: str, reason: str) -> MinuteAnalysisReport:
    source = rows[-1].source if rows else "分钟源待确认"
    return MinuteAnalysisReport(
        symbol=symbol,
        updated_at=rows[-1].timestamp if rows else now_text(),
        interval=interval,
        source=source or "分钟源待确认",
        sample_count=len(rows),
        latest_price=rows[-1].close if rows else None,
        summary=reason,
        t_plan=MinuteTPlan(
            low_zone="待确认",
            high_zone="待确认",
            suitability="不适合主动做T",
            style="数据不足",
            confidence=20,
            summary=reason,
            execution_steps=["等待分钟K线样本补齐后再判断盘中区间。"],
            stop_conditions=["分钟K线缺失时，不按盘中区间做T。"],
        ),
        warnings=[reason],
        missing_data=["分钟K线"],
    )


def _compact_unavailable_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        return "数据源连接失败"
    if "ProxyError" in text or "Unable to connect to proxy" in text:
        return "网络代理连接失败"
    if "Max retries exceeded" in text or "HTTPSConnectionPool" in text:
        return "行情接口连接失败"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "行情接口超时"
    if "返回为空" in text:
        return "行情接口返回为空"
    return text[:80]


def _ma(rows: list[MinuteKline], window: int) -> float:
    if not rows:
        return 0
    values = [item.close for item in rows[-window:]]
    return round(mean(values), 3)


def _minute_trend_label(latest_price: float, short_ma: float, long_ma: float, prev_short_ma: float) -> str:
    if latest_price >= short_ma >= long_ma and short_ma >= prev_short_ma:
        return "盘中偏强"
    if latest_price <= short_ma <= long_ma and short_ma < prev_short_ma:
        return "盘中转弱"
    if latest_price >= long_ma and short_ma >= long_ma:
        return "震荡偏强"
    if latest_price <= long_ma and short_ma <= long_ma:
        return "震荡偏弱"
    return "盘中震荡"


def _momentum_label(rows: list[MinuteKline]) -> str:
    if len(rows) < 4:
        return "待确认"
    recent = pct_change(rows[-1].close, rows[-4].close)
    if recent >= 0.8:
        return "短线加速"
    if recent <= -0.8:
        return "短线走弱"
    if recent >= 0.25:
        return "温和转强"
    if recent <= -0.25:
        return "温和转弱"
    return "动量平稳"


def _volume_pulse(rows: list[MinuteKline]) -> str:
    volumes = [item.volume for item in rows if item.volume > 0]
    if len(volumes) < 8:
        return "量能待确认"
    recent = mean(volumes[-3:])
    base = mean(volumes[-18:-3] or volumes[:-3] or volumes)
    ratio = recent / base if base else 1
    price_change = pct_change(rows[-1].close, rows[-4].close) if len(rows) >= 4 else 0
    if ratio >= 1.8 and price_change > 0:
        return "放量上攻"
    if ratio >= 1.8 and price_change < 0:
        return "放量回落"
    if ratio <= 0.55:
        return "明显缩量"
    return "量能平稳"


def _minute_levels(rows: list[MinuteKline], latest_price: float, level_type: str) -> list[MinuteSupportResistance]:
    if not rows or latest_price <= 0:
        return []
    candidates = [item.low for item in rows if item.low <= latest_price] if level_type == "support" else [item.high for item in rows if item.high >= latest_price]
    if not candidates:
        return []
    sorted_values = sorted(candidates)
    ratios = (0.35, 0.18) if level_type == "support" else (0.65, 0.82)
    labels = ("近端支撑", "防守支撑") if level_type == "support" else ("近端压力", "强压力")
    result = []
    for label, ratio in zip(labels, ratios):
        price = _quantile(sorted_values, ratio)
        touches = sum(1 for item in candidates if abs(item - price) / latest_price <= 0.0025)
        strength = max(20, min(92, 38 + touches * 12))
        result.append(
            MinuteSupportResistance(
                label=label,
                price=round(price, 2),
                strength=strength,
                reason=f"{label}来自近 {len(rows)} 根分钟K的价格密集区，触达次数约 {touches} 次。",
            )
        )
    deduped: list[MinuteSupportResistance] = []
    for item in result:
        if all(abs(item.price - old.price) / latest_price > 0.001 for old in deduped):
            deduped.append(item)
    return deduped[:2]


def _minute_t_plan(
    rows: list[MinuteKline],
    latest_price: float,
    supports: list[MinuteSupportResistance],
    resistances: list[MinuteSupportResistance],
    trend_label: str,
    volume_pulse: str,
    intraday_range_pct: float,
) -> MinuteTPlan:
    support = supports[0].price if supports else min(item.low for item in rows[-8:])
    resistance = resistances[0].price if resistances else max(item.high for item in rows[-8:])
    low_zone = _zone_text(support, latest_price, lower=True)
    high_zone = _zone_text(resistance, latest_price, lower=False)
    width_pct = (resistance - support) / latest_price * 100 if latest_price else 0
    if volume_pulse == "放量回落" or trend_label == "盘中转弱":
        suitability = "不适合主动做T"
        style = "防守型"
    elif width_pct >= 0.75 and intraday_range_pct >= 1.0:
        suitability = "仅底仓可做T"
        style = "区间型" if "震荡" in trend_label else "趋势滚动型"
    else:
        suitability = "等待更大区间"
        style = "窄幅等待型"
    confidence = 40 + min(24, len(rows) // 4) + (12 if supports and resistances else 0)
    if suitability == "不适合主动做T":
        confidence -= 8
    return MinuteTPlan(
        low_zone=low_zone,
        high_zone=high_zone,
        suitability=suitability,
        style=style,
        confidence=max(25, min(88, confidence)),
        summary=f"{style}，{suitability}。参考低吸区 {low_zone}，高抛区 {high_zone}，区间宽度约 {width_pct:.2f}%。",
        execution_steps=[
            "只使用已有可卖底仓，今日新增买入部分不参与当日T。",
            f"低吸只看 {low_zone} 附近缩量止跌或快速收回，不接放量下跌。",
            f"高抛只看 {high_zone} 附近冲高乏力、量能背离或接近压力。",
        ],
        stop_conditions=[
            f"有效跌破 {support:.2f} 后不能快速收回。",
            "出现放量回落、盘中转弱或盘口卖压明显增强。",
            "低吸区和高抛区间距不足以覆盖交易成本和滑点。",
        ],
    )


def _minute_warnings(rows: list[MinuteKline], t_plan: MinuteTPlan, intraday_range_pct: float, volume_pulse: str) -> list[str]:
    warnings: list[str] = []
    if rows[-1].from_cache or rows[-1].fallback_used:
        warnings.append("当前分钟K线来自缓存或兜底结果，做T区间需要降权。")
    if intraday_range_pct < 0.8:
        warnings.append("盘中振幅偏窄，做T空间可能不足。")
    if volume_pulse == "放量回落":
        warnings.append("分钟量能显示放量回落，先防守再考虑高抛低吸。")
    if t_plan.suitability == "不适合主动做T":
        warnings.append("当前不适合主动做T，避免为了交易而交易。")
    return warnings


def _zone_text(price: float, latest_price: float, *, lower: bool) -> str:
    band = max(latest_price * 0.0015, 0.01)
    start = price - band if lower else price
    end = price if lower else price + band
    return f"{start:.2f}-{end:.2f}"


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
