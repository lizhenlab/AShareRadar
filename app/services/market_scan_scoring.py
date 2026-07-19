from __future__ import annotations

from datetime import date, datetime

from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite
from app.models.schemas import DataQuality, Kline, Quote
from app.services.data_quality import build_data_quality
from app.services.data_quality_time import parse_quote_time
from app.services.indicators import recent_volume_ratio, trend_score
from app.services.leader_scoring import (
    STRONG_STOCK_LEADER_PROFILE,
    STRONG_STOCK_TAG_RULES,
    LeaderScoreInput,
    leader_score,
    leader_tags,
)
from app.services.scoring import clamp_score
from app.services.trading_calendar import is_trading_day
from app.utils.market_data import valid_kline
from app.utils.symbols import standard_symbol


FULL_MARKET_SCORE_RULE_VERSION = "full-market-score-v1"
FULL_MARKET_LEADER_WEIGHT = 0.85
FULL_MARKET_QUALITY_WEIGHT = 0.15


class MarketScanDataMissing(ValueError):
    pass


class MarketScanSkipped(ValueError):
    pass


def score_market_scan_item(
    item: MarketScanResultItem,
    quote: Quote,
    rows: list[Kline],
    *,
    as_of: datetime,
    completed_cutoff: date,
    expected_data_date: date,
    min_history_rows: int,
    min_data_quality_score: int,
) -> MarketScanResultWrite:
    _require_matching_quote(item, quote)
    completed_rows, latest_date = _rankable_completed_rows(
        rows,
        completed_cutoff=completed_cutoff,
        expected_data_date=expected_data_date,
        min_history_rows=min_history_rows,
    )
    _require_quote_date(quote, expected_data_date)
    _require_rankable_liquidity(quote, completed_rows)
    quality = _market_scan_quality(
        quote,
        completed_rows,
        as_of=as_of,
        minimum_score=min_data_quality_score,
    )
    trend, _trend_label = trend_score(quote, completed_rows)
    volume_ratio = recent_volume_ratio(completed_rows)
    leader_inputs = LeaderScoreInput(
        trend_score=trend,
        change_pct=quote.change_pct,
        volume_ratio=volume_ratio,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        data_quality_score=quality.score,
    )
    leadership = leader_score(leader_inputs, STRONG_STOCK_LEADER_PROFILE)
    score = clamp_score(round(leadership * FULL_MARKET_LEADER_WEIGHT + quality.score * FULL_MARKET_QUALITY_WEIGHT))
    tags = leader_tags(
        leader_inputs,
        leadership,
        STRONG_STOCK_TAG_RULES,
        "观察",
    )
    return _market_scan_result(
        item=item,
        quote=quote,
        rows=completed_rows,
        latest_date=latest_date,
        score=score,
        trend=trend,
        leadership=leadership,
        quality=quality,
        volume_ratio=volume_ratio,
        tags=tags,
    )


def _market_scan_result(
    *,
    item: MarketScanResultItem,
    quote: Quote,
    rows: list[Kline],
    latest_date: date,
    score: int,
    trend: int,
    leadership: int,
    quality: DataQuality,
    volume_ratio: float,
    tags: list[str],
) -> MarketScanResultWrite:
    quote_fallback_used = bool(quote.fallback_used)
    kline_fallback_used = any(row.fallback_used for row in rows)
    metadata_degraded = item.list_date is None
    degradation_reasons = _degradation_reasons(
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
    )
    tags.extend(
        _metadata_tags(
            item,
            quality.score,
            quote_fallback_used=quote_fallback_used,
            kline_fallback_used=kline_fallback_used,
            metadata_degraded=metadata_degraded,
        )
    )
    return MarketScanResultWrite(
        symbol=item.symbol,
        status="success",
        score=score,
        trend_score=trend,
        leader_score=leadership,
        data_quality_score=quality.score,
        price=quote.price,
        change_pct=quote.change_pct,
        turnover_rate=quote.turnover_rate,
        volume_ratio=volume_ratio,
        amount=quote.amount,
        tags=tuple(dict.fromkeys(tags)),
        metrics=_scan_metrics(rows, volume_ratio),
        reason=_score_reason(score, trend, quality.score, volume_ratio),
        data_date=latest_date.isoformat(),
        quote_timestamp=quote.timestamp,
        quote_source=quote.source,
        kline_source=rows[-1].source,
        adjustment_mode=rows[-1].adjustment_mode,
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
        degradation_reasons=degradation_reasons,
    )


def _rankable_completed_rows(
    rows: list[Kline],
    *,
    completed_cutoff: date,
    expected_data_date: date,
    min_history_rows: int,
) -> tuple[list[Kline], date]:
    completed_rows = completed_market_scan_klines(rows, completed_cutoff)
    _require_qfq_rows(completed_rows)
    if len(completed_rows) < min_history_rows:
        raise MarketScanSkipped(f"完整前复权日K不足：需要 {min_history_rows} 根，当前 {len(completed_rows)} 根")
    latest_date = date.fromisoformat(completed_rows[-1].date)
    if latest_date < expected_data_date:
        raise MarketScanSkipped(f"日K停留在 {latest_date.isoformat()}，早于应有交易日 {expected_data_date.isoformat()}，可能停牌")
    if latest_date > expected_data_date:
        raise MarketScanDataMissing(f"日K日期 {latest_date.isoformat()} 晚于应有交易日 {expected_data_date.isoformat()}")
    return completed_rows, latest_date


def _market_scan_quality(
    quote: Quote,
    rows: list[Kline],
    *,
    as_of: datetime,
    minimum_score: int,
) -> DataQuality:
    quality = build_data_quality(
        quote,
        rows,
        consistency_level="批量扫描未执行多源一致性校验",
        consistency_notes=["全市场批量扫描按单一可用行情快照计算。"],
        now=as_of,
    )
    if quality.score < minimum_score:
        raise MarketScanSkipped(f"数据质量 {quality.score} 分，低于排名门槛 {minimum_score} 分")
    return quality


def completed_market_scan_klines(rows: list[Kline], cutoff: date) -> list[Kline]:
    by_date: dict[date, Kline] = {}
    for row in rows:
        row_date = _strict_date(row.date)
        if row_date is not None and row_date <= cutoff and is_trading_day(row_date) and valid_kline(row):
            by_date[row_date] = row
    return [row for _row_date, row in sorted(by_date.items(), key=lambda entry: entry[0])]


def _require_matching_quote(item: MarketScanResultItem, quote: Quote) -> None:
    try:
        quote_symbol = standard_symbol(f"{quote.code}.{quote.market}")
    except ValueError as exc:
        raise MarketScanDataMissing("行情返回了无法识别的股票代码") from exc
    if quote_symbol != item.symbol:
        raise MarketScanDataMissing(f"行情代码不匹配：请求 {item.symbol}，返回 {quote_symbol}")


def _require_qfq_rows(rows: list[Kline]) -> None:
    if not rows:
        raise MarketScanDataMissing("截止时点之前没有有效完整日K")
    modes = {row.adjustment_mode for row in rows}
    if modes != {"qfq"}:
        raise MarketScanDataMissing("日K不是一致的前复权序列")


def _require_quote_date(quote: Quote, expected_data_date: date) -> None:
    quote_time = parse_quote_time(quote.timestamp)
    if quote_time is None:
        raise MarketScanDataMissing("报价时间无法解析")
    if quote_time.date() != expected_data_date:
        raise MarketScanSkipped(f"报价日期 {quote_time.date().isoformat()} 与完整交易日 {expected_data_date.isoformat()} 不一致")


def _require_rankable_liquidity(quote: Quote, rows: list[Kline]) -> None:
    if quote.volume <= 0 or quote.amount <= 0:
        raise MarketScanDataMissing("报价缺少有效成交量或成交额")
    if quote.turnover_rate is None:
        raise MarketScanDataMissing("报价缺少换手率")
    recent_volumes = [row.volume for row in rows[-20:]]
    if len(recent_volumes) < 6 or any(volume <= 0 for volume in recent_volumes):
        raise MarketScanDataMissing("日K缺少连续有效成交量，无法计算量比")


def _metadata_tags(
    item: MarketScanResultItem,
    quality_score: int,
    *,
    quote_fallback_used: bool,
    kline_fallback_used: bool,
    metadata_degraded: bool,
) -> list[str]:
    tags: list[str] = []
    if item.is_st:
        tags.append("ST")
    if item.is_new:
        tags.append("新股")
    if metadata_degraded:
        tags.append("上市日期未知")
    if quality_score < 70:
        tags.append("数据降权")
    if quote_fallback_used:
        tags.append("兜底行情")
    if kline_fallback_used:
        tags.append("兜底K线")
    return tags


def _degradation_reasons(
    *,
    quote_fallback_used: bool,
    kline_fallback_used: bool,
    metadata_degraded: bool,
) -> tuple[str, ...]:
    return tuple(
        reason
        for enabled, reason in (
            (quote_fallback_used, "quote_fallback"),
            (kline_fallback_used, "kline_fallback"),
            (metadata_degraded, "metadata_incomplete"),
        )
        if enabled
    )


def _scan_metrics(rows: list[Kline], volume_ratio: float) -> dict[str, float]:
    closes = [row.close for row in rows]
    recent_20 = rows[-20:]
    return {
        "close": round(closes[-1], 4),
        "ma5": round(sum(closes[-5:]) / 5, 4),
        "ma20": round(sum(closes[-20:]) / 20, 4),
        "ma60": round(sum(closes[-60:]) / 60, 4),
        "high20": round(max(row.high for row in recent_20), 4),
        "low20": round(min(row.low for row in recent_20), 4),
        "volume_ratio": round(volume_ratio, 4),
    }


def _score_reason(score: int, trend: int, quality: int, volume_ratio: float) -> str:
    return f"综合分 {score}，趋势 {trend}，数据质量 {quality}，" f"近5日量比 {volume_ratio:.2f}"


def _strict_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


__all__ = [
    "FULL_MARKET_SCORE_RULE_VERSION",
    "MarketScanDataMissing",
    "MarketScanSkipped",
    "completed_market_scan_klines",
    "score_market_scan_item",
]
