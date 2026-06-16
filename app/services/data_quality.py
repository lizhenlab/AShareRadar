from __future__ import annotations

from datetime import date, datetime

from app.models.schemas import DataQuality, Kline, KlineQuality, Quote
from app.services import trading_calendar
from app.utils.time import now_text, seconds_since_text


def build_data_quality(
    quote: Quote,
    klines: list[Kline],
    *,
    consistency_level: str = "未校验",
    consistency_notes: list[str] | None = None,
    consistency_penalty: int = 0,
    require_kline: bool = True,
    now: datetime | None = None,
) -> DataQuality:
    notes = []
    anomalies: list[str] = []
    score = 100
    current = now or datetime.now()
    quote_delay_seconds = _quote_delay_seconds(quote.timestamp, now=current)
    kline_quality = assess_kline_quality(klines, now=current) if require_kline or klines else None

    if "缓存" in quote.source:
        notes.append("当前报价来自短时缓存。")
        score -= 8
    if "演示" in quote.source:
        notes.append("当前报价来自演示数据，不能作为真实行情依据。")
        anomalies.append("演示行情")
        score -= 45
    if require_kline:
        if len(klines) < 60:
            notes.append("K线数量偏少，趋势判断可靠性下降。")
            anomalies.append("K线数量不足")
            score -= 20
        kline_penalty, kline_anomalies = _kline_quality_penalty(kline_quality)
        score -= kline_penalty
        anomalies.extend(kline_anomalies)
        notes.extend(kline_quality.notes)
    if quote.price <= 0:
        notes.append("报价价格异常。")
        anomalies.append("报价价格异常")
        score -= 35
    if quote.prev_close <= 0:
        notes.append("昨收价缺失，涨跌幅校验能力下降。")
        anomalies.append("昨收价缺失")
        score -= 10
    if quote.high and quote.low and quote.high < quote.low:
        notes.append("最高价低于最低价，行情字段异常。")
        anomalies.append("高低价倒挂")
        score -= 30
    if quote.high and quote.price > quote.high * 1.01:
        notes.append("现价明显高于最高价，行情字段可能不同步。")
        anomalies.append("现价高于最高价")
        score -= 20
    if quote.low and quote.price < quote.low * 0.99:
        notes.append("现价明显低于最低价，行情字段可能不同步。")
        anomalies.append("现价低于最低价")
        score -= 20
    if quote.prev_close > 0:
        expected_change_pct = (quote.price - quote.prev_close) / quote.prev_close * 100
        if abs(expected_change_pct - quote.change_pct) > 0.3:
            notes.append("现价、昨收和涨跌幅之间存在偏差。")
            anomalies.append("涨跌幅口径偏差")
            score -= 12
    freshness_penalty, freshness_notes, freshness_anomalies = _quote_freshness_penalty(quote.timestamp, current)
    score -= freshness_penalty
    notes.extend(freshness_notes)
    anomalies.extend(freshness_anomalies)
    if consistency_notes:
        notes.extend(consistency_notes)
        if consistency_level in {"存在差异", "字段异常"}:
            anomalies.extend(consistency_notes)
    score -= consistency_penalty
    score = max(0, min(100, score))
    if score >= 85:
        level = "优秀"
    elif score >= 70:
        level = "良好"
    elif score >= 50:
        level = "一般"
    else:
        level = "较弱"
    if not notes:
        notes.append("报价和K线数据可用于当前个股分析。" if require_kline else "报价数据可用于当前提醒评估。")
    return DataQuality(
        level=level,
        source=quote.source,
        quote_time=quote.timestamp,
        kline_count=len(klines),
        score=score,
        checked_at=now_text(),
        quote_delay_seconds=quote_delay_seconds,
        consistency_level=consistency_level,
        kline_quality=kline_quality,
        anomalies=anomalies,
        notes=notes,
    )


def assess_kline_quality(klines: list[Kline], *, now: datetime | None = None) -> KlineQuality:
    source = _kline_source(klines)
    last_date = _latest_kline_date(klines)
    latest_expected = latest_expected_trade_date(now)
    from_cache = any(item.from_cache for item in klines)
    fallback_used = any(item.fallback_used for item in klines)
    days_behind = None
    notes: list[str] = []

    if not klines:
        return KlineQuality(
            level="缺失",
            source=source,
            last_date=None,
            latest_expected_date=latest_expected.isoformat(),
            days_behind_expected=None,
            from_cache=from_cache,
            fallback_used=fallback_used,
            notes=["缺少K线数据，趋势、买卖点和做T参考都需要降级。"],
        )
    if last_date is None:
        return KlineQuality(
            level="较弱",
            source=source,
            last_date=klines[-1].date if klines else None,
            latest_expected_date=latest_expected.isoformat(),
            days_behind_expected=None,
            from_cache=from_cache,
            fallback_used=fallback_used,
            notes=["最新K线日期无法识别，趋势参考需要人工复核。"],
        )

    if last_date < latest_expected:
        days_behind = _weekday_gap(last_date, latest_expected)
        notes.append(f"K线最新日期为 {last_date.isoformat()}，落后预期交易日约 {days_behind} 个交易日。")
    else:
        days_behind = 0
    if fallback_used:
        notes.append("K线来自兜底缓存，说明实时K线源本轮不可用。")
    elif from_cache:
        notes.append("K线来自本地缓存，已按最新预期交易日校验。")
    if source and ("演示" in source or "demo" in source.lower()):
        notes.append("K线来源为演示数据，不能作为真实行情依据。")

    if source and ("演示" in source or "demo" in source.lower()):
        level = "较弱"
    elif days_behind and days_behind >= 3:
        level = "较弱"
    elif days_behind and days_behind >= 1:
        level = "一般"
    elif fallback_used:
        level = "一般"
    else:
        level = "良好"
    return KlineQuality(
        level=level,
        source=source,
        last_date=last_date.isoformat(),
        latest_expected_date=latest_expected.isoformat(),
        days_behind_expected=days_behind,
        from_cache=from_cache,
        fallback_used=fallback_used,
        notes=notes or ["K线日期与当前预期交易日匹配。"],
    )


def latest_expected_trade_date(now: datetime | None = None) -> date:
    return trading_calendar.latest_expected_trade_date(now)


def _quote_delay_seconds(value: str, *, now: datetime | None = None) -> int | None:
    if now is None:
        seconds = seconds_since_text(value)
        if seconds is None:
            return None
        return max(0, int(seconds))
    parsed = _parse_quote_time(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _quote_freshness_penalty(value: str, now: datetime) -> tuple[int, list[str], list[str]]:
    parsed = _parse_quote_time(value)
    if parsed is None:
        return 12, ["报价时间无法识别，需确认行情源时间字段。"], ["报价时间异常"]

    expected_date = _expected_quote_date(now)
    quote_date = parsed.date()
    if quote_date < expected_date:
        days = _weekday_gap(quote_date, expected_date)
        if _is_trading_session(now):
            if days >= 5:
                return 30, [f"报价日期为 {quote_date.isoformat()}，落后当前应参考交易日约 {days} 个交易日。"], ["报价严重滞后"]
            if days >= 1:
                return 24, [f"交易时段仍在使用 {quote_date.isoformat()} 的报价，落后当前应参考交易日约 {days} 个交易日。"], ["报价滞后"]
        if days >= 5:
            return 30, [f"报价日期为 {quote_date.isoformat()}，落后当前应参考交易日约 {days} 个交易日。"], ["报价严重滞后"]
        if days >= 2:
            return 20, [f"报价日期为 {quote_date.isoformat()}，落后当前应参考交易日约 {days} 个交易日。"], ["报价滞后"]
        return 8, [f"报价日期为 {quote_date.isoformat()}，落后当前应参考交易日约 {days} 个交易日。"], ["报价轻微滞后"]
    if quote_date > expected_date:
        return 12, [f"报价日期为 {quote_date.isoformat()}，晚于当前应参考交易日，需核对行情时间。"], ["报价时间超前"]

    delay_seconds = max(0, int((now - parsed).total_seconds()))
    if _is_trading_session(now):
        if delay_seconds > 60 * 60:
            return 18, [f"交易时段内报价约 {delay_seconds // 60} 分钟未更新，需确认是否延迟。"], ["交易时段报价滞后"]
        if delay_seconds > 15 * 60:
            return 8, [f"交易时段内报价约 {delay_seconds // 60} 分钟未更新，短线判断需降权。"], []
        return 0, [], []
    if _is_midday_break(now):
        if parsed.hour < 11 or (parsed.hour == 11 and parsed.minute < 25):
            return 8, ["午间休市阶段报价未接近上午收盘时间，需确认是否延迟。"], []
        return 0, ["午间休市阶段使用上午最新行情快照。"], []
    if _is_after_close(now):
        if parsed.hour < 14 or (parsed.hour == 14 and parsed.minute < 55):
            return 8, ["盘后报价时间早于尾盘，收盘参考需要降权。"], []
        return 0, ["报价日期为当前交易日，盘后使用当天行情快照。"], []
    return 0, ["非交易时段使用最近交易日行情快照。"], []


def _kline_quality_penalty(kline_quality: KlineQuality) -> tuple[int, list[str]]:
    penalty = 0
    anomalies: list[str] = []
    if kline_quality.level == "缺失":
        return 25, ["K线缺失"]
    if kline_quality.last_date is None:
        return 25, ["K线日期异常"]
    days = kline_quality.days_behind_expected or 0
    if days >= 5:
        penalty += 30
        anomalies.append("K线严重滞后")
    elif days >= 2:
        penalty += 18
        anomalies.append("K线滞后")
    elif days >= 1:
        penalty += 8
        anomalies.append("K线轻微滞后")
    if kline_quality.fallback_used:
        penalty += 12
        anomalies.append("K线兜底缓存")
    source = kline_quality.source or ""
    if "演示" in source or "demo" in source.lower():
        penalty += 35
        anomalies.append("演示K线")
    return penalty, anomalies


def _latest_kline_date(klines: list[Kline]) -> date | None:
    for item in reversed(klines):
        parsed = _parse_kline_date(item.date)
        if parsed:
            return parsed
    return None


def _parse_kline_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _parse_quote_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _expected_quote_date(now: datetime) -> date:
    return trading_calendar.expected_quote_date(now)


def _is_trading_session(now: datetime) -> bool:
    return trading_calendar.is_trading_session(now)


def _is_midday_break(now: datetime) -> bool:
    return trading_calendar.is_midday_break(now)


def _is_after_close(now: datetime) -> bool:
    return trading_calendar.is_after_close(now)


def _weekday_gap(start: date, end: date) -> int:
    return trading_calendar.trading_day_gap(start, end)


def _kline_source(klines: list[Kline]) -> str | None:
    for item in reversed(klines):
        if item.source:
            return item.source
    return None
