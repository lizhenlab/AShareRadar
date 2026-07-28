from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.market_scan import (
    MARKET_SCAN_RANK_TIE_BREAK,
    MarketScanResultItem,
    MarketScanResultWrite,
)
from app.models.analysis import (
    DataQuality,
)
from app.models.market import (
    Kline,
    Quote,
)
from app.services.data_quality import build_data_quality
from app.services.data_quality_time import parse_quote_time
from app.services.indicators import recent_volume_ratio, trend_score
from app.services.leader_scoring import (
    LEADER_SCORE_ALGORITHM_VERSION,
    LEADER_SCORE_ROUNDING_MODE,
    STRONG_STOCK_LEADER_PROFILE,
    STRONG_STOCK_TAG_RULES,
    LeaderScoreInput,
    LeaderScoreBreakdown,
    leader_profile_spec,
    leader_score_breakdown,
    leader_tag_rules_spec,
    leader_tags,
)
from app.services.market_scan_replay import (
    MarketScanReplayError,
    MarketScanScoreReplay,
    rank_score_details,
    replay_score_details,
    stable_score_spec_hash,
    verify_score_details,
)
from app.services.scoring import clamp_score
from app.services.trading_calendar import is_trading_day
from app.utils.market_data import valid_kline
from app.utils.symbols import standard_symbol


FULL_MARKET_SCORE_RULE_VERSION = "full-market-score-v2"
FULL_MARKET_SCORE_ALGORITHM_VERSION = "weighted-leader-quality-v1"
FULL_MARKET_TREND_ALGORITHM_VERSION = "trend-score-v1"
FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION = "recent-volume-ratio-v1"
FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION = "data-quality-v1"
FULL_MARKET_LEADER_WEIGHT = 0.85
FULL_MARKET_QUALITY_WEIGHT = 0.15
FULL_MARKET_METRIC_DECIMALS = 4


class MarketScanDataMissing(ValueError):
    pass


class MarketScanSkipped(ValueError):
    pass


@dataclass(frozen=True)
class _MarketScanScore:
    score: int
    trend: int
    leadership: int
    quality: DataQuality
    volume_ratio: float
    tags: tuple[str, ...]
    leader_inputs: LeaderScoreInput
    leader_breakdown: LeaderScoreBreakdown
    raw_score: float
    rounded_score: int
    score_spec: dict[str, object]


@dataclass(frozen=True)
class _MarketScanProvenance:
    quote_fallback_used: bool
    kline_fallback_used: bool
    metadata_degraded: bool
    degradation_reasons: tuple[str, ...]


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
    rule_version: str | None = None,
) -> MarketScanResultWrite:
    _require_matching_quote(item, quote)
    _require_quote_date(quote, expected_data_date)
    completed_rows, latest_date = _rankable_completed_rows(
        rows,
        quote=quote,
        completed_cutoff=completed_cutoff,
        expected_data_date=expected_data_date,
        min_history_rows=min_history_rows,
    )
    _require_rankable_liquidity(quote, completed_rows)
    calculated = _calculate_market_scan_score(
        quote,
        completed_rows,
        as_of=as_of,
        min_data_quality_score=min_data_quality_score,
    )
    return _market_scan_result(
        item=item,
        quote=quote,
        rows=completed_rows,
        latest_date=latest_date,
        calculated=calculated,
        rule_version=rule_version,
    )


def _calculate_market_scan_score(
    quote: Quote,
    rows: list[Kline],
    *,
    as_of: datetime,
    min_data_quality_score: int,
) -> _MarketScanScore:
    quality = _market_scan_quality(
        quote,
        rows,
        as_of=as_of,
        minimum_score=min_data_quality_score,
    )
    trend, _trend_label = trend_score(quote, rows)
    volume_ratio = recent_volume_ratio(rows)
    leader_inputs = LeaderScoreInput(
        trend_score=trend,
        change_pct=quote.change_pct,
        volume_ratio=volume_ratio,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        data_quality_score=quality.score,
    )
    leader_breakdown = leader_score_breakdown(leader_inputs, STRONG_STOCK_LEADER_PROFILE)
    leadership = leader_breakdown.score
    raw_score = leadership * FULL_MARKET_LEADER_WEIGHT + quality.score * FULL_MARKET_QUALITY_WEIGHT
    rounded_score = round(raw_score)
    score = clamp_score(rounded_score)
    return _MarketScanScore(
        score=score,
        trend=trend,
        leadership=leadership,
        quality=quality,
        volume_ratio=volume_ratio,
        tags=tuple(leader_tags(leader_inputs, leadership, STRONG_STOCK_TAG_RULES, "观察")),
        leader_inputs=leader_inputs,
        leader_breakdown=leader_breakdown,
        raw_score=raw_score,
        rounded_score=rounded_score,
        score_spec=market_scan_score_spec(min_data_quality_score=min_data_quality_score),
    )


def _market_scan_result(
    *,
    item: MarketScanResultItem,
    quote: Quote,
    rows: list[Kline],
    latest_date: date,
    calculated: _MarketScanScore,
    rule_version: str | None,
) -> MarketScanResultWrite:
    provenance = _market_scan_provenance(item, quote, rows)
    tags = tuple(dict.fromkeys((*calculated.tags, *_metadata_tags_for_result(item, calculated.quality.score, provenance))))
    return MarketScanResultWrite(
        symbol=item.symbol,
        status="success",
        score=calculated.score,
        trend_score=calculated.trend,
        leader_score=calculated.leadership,
        data_quality_score=calculated.quality.score,
        price=quote.price,
        change_pct=quote.change_pct,
        turnover_rate=quote.turnover_rate,
        volume_ratio=calculated.volume_ratio,
        amount=quote.amount,
        tags=tags,
        metrics=_scan_metrics(rows, calculated.volume_ratio),
        score_details=_score_details(
            item=item,
            inputs=calculated.leader_inputs,
            leader_breakdown=calculated.leader_breakdown,
            quality_score=calculated.quality.score,
            score=calculated.score,
            raw_score=calculated.raw_score,
            rounded_score=calculated.rounded_score,
            score_spec=calculated.score_spec,
            rule_version=rule_version,
        ),
        reason=_score_reason(
            calculated.score,
            calculated.trend,
            calculated.quality.score,
            calculated.volume_ratio,
        ),
        data_date=latest_date.isoformat(),
        quote_timestamp=quote.timestamp,
        quote_source=quote.source,
        kline_source=rows[-1].source,
        adjustment_mode=rows[-1].adjustment_mode,
        quote_fallback_used=provenance.quote_fallback_used,
        kline_fallback_used=provenance.kline_fallback_used,
        metadata_degraded=provenance.metadata_degraded,
        degradation_reasons=provenance.degradation_reasons,
    )


def _market_scan_provenance(
    item: MarketScanResultItem,
    quote: Quote,
    rows: list[Kline],
) -> _MarketScanProvenance:
    quote_fallback_used = bool(quote.fallback_used)
    kline_fallback_used = any(row.fallback_used for row in rows)
    metadata_degraded = item.list_date is None
    return _MarketScanProvenance(
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
        degradation_reasons=_degradation_reasons(
            quote_fallback_used=quote_fallback_used,
            kline_fallback_used=kline_fallback_used,
            metadata_degraded=metadata_degraded,
        ),
    )


def _metadata_tags_for_result(
    item: MarketScanResultItem,
    quality_score: int,
    provenance: _MarketScanProvenance,
) -> list[str]:
    return _metadata_tags(
        item,
        quality_score,
        quote_fallback_used=provenance.quote_fallback_used,
        kline_fallback_used=provenance.kline_fallback_used,
        metadata_degraded=provenance.metadata_degraded,
    )


def _rankable_completed_rows(
    rows: list[Kline],
    *,
    quote: Quote,
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
        if quote.volume > 0 and quote.amount > 0:
            raise MarketScanDataMissing(f"当日报价存在有效成交，但日K仅到 {latest_date.isoformat()}，" f"早于应有交易日 {expected_data_date.isoformat()}")
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
        if rows[-1].volume <= 0:
            raise MarketScanSkipped("当日报价与日K均无有效成交，可能停牌")
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
        "close": round(closes[-1], FULL_MARKET_METRIC_DECIMALS),
        "ma5": round(sum(closes[-5:]) / 5, FULL_MARKET_METRIC_DECIMALS),
        "ma20": round(sum(closes[-20:]) / 20, FULL_MARKET_METRIC_DECIMALS),
        "ma60": round(sum(closes[-60:]) / 60, FULL_MARKET_METRIC_DECIMALS),
        "high20": round(max(row.high for row in recent_20), FULL_MARKET_METRIC_DECIMALS),
        "low20": round(min(row.low for row in recent_20), FULL_MARKET_METRIC_DECIMALS),
        "volume_ratio": round(volume_ratio, FULL_MARKET_METRIC_DECIMALS),
    }


def market_scan_score_spec(*, min_data_quality_score: int) -> dict[str, object]:
    return {
        "schema_version": 2,
        "rule_version": FULL_MARKET_SCORE_RULE_VERSION,
        "algorithms": {
            "trend_score": FULL_MARKET_TREND_ALGORITHM_VERSION,
            "volume_ratio": FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION,
            "data_quality": FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION,
            "leader_score": LEADER_SCORE_ALGORITHM_VERSION,
            "final_score": FULL_MARKET_SCORE_ALGORITHM_VERSION,
        },
        "leader_profile": leader_profile_spec(STRONG_STOCK_LEADER_PROFILE),
        "tag_rules": leader_tag_rules_spec(STRONG_STOCK_TAG_RULES, "观察"),
        "eligibility": {
            "min_data_quality_score": int(min_data_quality_score),
        },
        "final_score": {
            "formula": "leader_score * leader_weight + data_quality_score * quality_weight",
            "weights": {
                "leader_score": FULL_MARKET_LEADER_WEIGHT,
                "data_quality_score": FULL_MARKET_QUALITY_WEIGHT,
            },
            "clamp": [0, 100],
        },
        "rounding": {
            "mode": LEADER_SCORE_ROUNDING_MODE,
            "component_stage": "after-trend-weight-and-final-weighted-sum",
            "metric_decimals": FULL_MARKET_METRIC_DECIMALS,
        },
        "ranking": {
            "tie_break": [list(item) for item in MARKET_SCAN_RANK_TIE_BREAK],
        },
    }


def _score_details(
    *,
    item: MarketScanResultItem,
    inputs: LeaderScoreInput,
    leader_breakdown: LeaderScoreBreakdown,
    quality_score: int,
    score: int,
    raw_score: float,
    rounded_score: int,
    score_spec: dict[str, object],
    rule_version: str | None,
) -> dict[str, object]:
    score_spec_hash = stable_score_spec_hash(score_spec)
    return {
        "schema_version": 1,
        "run_rule_version": rule_version or f"{FULL_MARKET_SCORE_RULE_VERSION}:{score_spec_hash}",
        "score_spec_hash": score_spec_hash,
        "score_spec": score_spec,
        "inputs": {
            "trend_score": inputs.trend_score,
            "change_pct": inputs.change_pct,
            "volume_ratio": inputs.volume_ratio,
            "amount": inputs.amount,
            "turnover_rate": inputs.turnover_rate,
            "data_quality_score": inputs.data_quality_score,
        },
        "components": {
            "leader_score": {
                "base": leader_breakdown.base,
                "trend_delta": leader_breakdown.trend_delta,
                "rule_deltas": dict(leader_breakdown.rule_deltas),
                "unclamped": leader_breakdown.unclamped_score,
                "score": leader_breakdown.score,
            },
            "data_quality_score": quality_score,
            "final_score": {
                "weighted_terms": {
                    "leader_score": leader_breakdown.score * FULL_MARKET_LEADER_WEIGHT,
                    "data_quality_score": quality_score * FULL_MARKET_QUALITY_WEIGHT,
                },
                "raw": raw_score,
                "rounded": rounded_score,
                "score": score,
            },
        },
        "ranking": {
            "tie_break": [list(entry) for entry in MARKET_SCAN_RANK_TIE_BREAK],
            "tie_break_values": {
                "score": score,
                "trend_score": inputs.trend_score,
                "change_pct": inputs.change_pct,
                "amount": inputs.amount,
                "symbol": item.symbol,
            },
        },
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
    "FULL_MARKET_SCORE_ALGORITHM_VERSION",
    "FULL_MARKET_SCORE_RULE_VERSION",
    "MarketScanDataMissing",
    "MarketScanReplayError",
    "MarketScanScoreReplay",
    "MarketScanSkipped",
    "completed_market_scan_klines",
    "market_scan_score_spec",
    "rank_score_details",
    "replay_score_details",
    "score_market_scan_item",
    "stable_score_spec_hash",
    "verify_score_details",
]
