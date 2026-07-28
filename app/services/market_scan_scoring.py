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
    FULL_MARKET_LEADER_PROFILE,
    LEADER_SCORE_ALGORITHM_VERSION,
    LEADER_SCORE_ROUNDING_MODE,
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
from app.services.market_scan_rank_refinement import (
    MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
    MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT,
    MarketScanRankRefinement,
    market_scan_rank_refinement,
    market_scan_rank_refinement_spec,
)
from app.services.scoring import clamp_score
from app.services.trading_calendar import is_trading_day
from app.utils.market_data import valid_kline
from app.utils.market_time import market_local_naive
from app.utils.symbols import standard_symbol


FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION = 4
FULL_MARKET_SCORE_RULE_VERSION = "full-market-score-v4"
FULL_MARKET_SCORE_ALGORITHM_VERSION = "trend-quality-penalty-v3"
FULL_MARKET_TREND_ALGORITHM_VERSION = "trend-score-v2-continuous-soft-clip"
FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION = "recent-volume-ratio-v2-explicit-windows"
FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION = "data-quality-v2-cache-neutral"
FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT = 0.15
FULL_MARKET_METRIC_DECIMALS = 4
FULL_MARKET_RAW_SCORE_DECIMALS = 6
FULL_MARKET_MAX_CLOSE_GAP_PCT = 0.5
FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE = 0.02
FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW = 5
FULL_MARKET_VOLUME_RATIO_BASE_WINDOW = 20
FULL_MARKET_VOLUME_RATIO_MIN_COUNT = FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW + 1
FULL_MARKET_VOLUME_RATIO_PRECISION = 2
FULL_MARKET_SCORE_TIE_BREAK = MARKET_SCAN_RANK_TIE_BREAK


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
    rank_refinement: MarketScanRankRefinement
    quality_penalty: float
    base_score: float
    rank_discount: float
    raw_score: float
    rounded_score: int
    score_spec: dict[str, object]


@dataclass(frozen=True)
class _MarketScanProvenance:
    quote_fallback_used: bool
    kline_fallback_used: bool
    metadata_degraded: bool
    industry_missing: bool
    list_date_missing: bool
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
    _require_quote_date(quote, expected_data_date, as_of=as_of)
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
    volume_ratio = recent_volume_ratio(
        rows,
        recent_window=FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW,
        base_window=FULL_MARKET_VOLUME_RATIO_BASE_WINDOW,
    )
    leader_inputs = LeaderScoreInput(
        trend_score=trend,
        change_pct=quote.change_pct,
        volume_ratio=volume_ratio,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        data_quality_score=quality.score,
    )
    leader_breakdown = leader_score_breakdown(leader_inputs, FULL_MARKET_LEADER_PROFILE)
    leadership = leader_breakdown.score
    rank_refinement = market_scan_rank_refinement(quote, rows)
    quality_penalty = round(
        (100 - quality.score) * FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT,
        FULL_MARKET_METRIC_DECIMALS,
    )
    base_score = round(min(100.0, max(0.0, leadership - quality_penalty)), FULL_MARKET_METRIC_DECIMALS)
    rank_discount = round(
        (1 - rank_refinement.score) * MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT,
        FULL_MARKET_RAW_SCORE_DECIMALS + 2,
    )
    raw_score = round(max(0.0, base_score - rank_discount), FULL_MARKET_RAW_SCORE_DECIMALS)
    rounded_score = round(base_score)
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
        rank_refinement=rank_refinement,
        quality_penalty=quality_penalty,
        base_score=base_score,
        rank_discount=rank_discount,
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
        raw_score=calculated.raw_score,
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
            rank_refinement=calculated.rank_refinement,
            quality_score=calculated.quality.score,
            quality_penalty=calculated.quality_penalty,
            base_score=calculated.base_score,
            rank_discount=calculated.rank_discount,
            score=calculated.score,
            raw_score=calculated.raw_score,
            rounded_score=calculated.rounded_score,
            score_spec=calculated.score_spec,
            rule_version=rule_version,
        ),
        reason=_score_reason(calculated),
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
    industry_missing = not str(item.industry or "").strip()
    list_date_missing = not str(item.list_date or "").strip()
    metadata_degraded = industry_missing or list_date_missing
    return _MarketScanProvenance(
        quote_fallback_used=quote_fallback_used,
        kline_fallback_used=kline_fallback_used,
        metadata_degraded=metadata_degraded,
        industry_missing=industry_missing,
        list_date_missing=list_date_missing,
        degradation_reasons=_degradation_reasons(
            quote_fallback_used=quote_fallback_used,
            kline_fallback_used=kline_fallback_used,
            industry_missing=industry_missing,
            list_date_missing=list_date_missing,
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
        industry_missing=provenance.industry_missing,
        list_date_missing=provenance.list_date_missing,
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
    _require_quote_kline_close_consistency(quote, completed_rows[-1])
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
        penalize_cached_quote=False,
        now=as_of,
    )
    if quality.score < minimum_score:
        raise MarketScanDataMissing(f"数据质量 {quality.score} 分，低于排名门槛 {minimum_score} 分")
    return quality


def completed_market_scan_klines(rows: list[Kline], cutoff: date) -> list[Kline]:
    by_date: dict[date, Kline] = {}
    for row in rows:
        row_date = _strict_date(row.date)
        if row_date is not None and row_date <= cutoff and is_trading_day(row_date) and valid_kline(row):
            existing = by_date.get(row_date)
            if existing is not None and _ranking_bar_signature(existing) != _ranking_bar_signature(row):
                raise MarketScanDataMissing(f"同一交易日 {row_date.isoformat()} 存在冲突日K")
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


def _require_quote_date(quote: Quote, expected_data_date: date, *, as_of: datetime) -> None:
    quote_time = parse_quote_time(quote.timestamp)
    if quote_time is None:
        raise MarketScanDataMissing("报价时间无法解析")
    if quote_time.date() != expected_data_date:
        raise MarketScanDataMissing(f"报价日期 {quote_time.date().isoformat()} 与完整交易日 {expected_data_date.isoformat()} 不一致")
    cutoff = market_local_naive(as_of)
    if quote_time > cutoff:
        raise MarketScanDataMissing(
            f"报价时间 {quote_time.strftime('%Y-%m-%d %H:%M:%S')} 晚于批次截止时点 "
            f"{cutoff.strftime('%Y-%m-%d %H:%M:%S')}"
        )


def _require_rankable_liquidity(quote: Quote, rows: list[Kline]) -> None:
    if quote.price <= 0:
        raise MarketScanDataMissing("报价缺少有效价格")
    if quote.volume <= 0 or quote.amount <= 0:
        if rows[-1].volume <= 0:
            raise MarketScanSkipped("当日报价与日K均无有效成交，可能停牌")
        raise MarketScanDataMissing("报价缺少有效成交量或成交额")
    if _is_single_price_session(quote):
        raise MarketScanSkipped("当日全天单一价格，无法确认开盘可成交性")
    if quote.turnover_rate is None:
        raise MarketScanDataMissing("报价缺少换手率")
    recent_volumes = [row.volume for row in rows[-20:]]
    if len(recent_volumes) < 6 or any(volume <= 0 for volume in recent_volumes):
        raise MarketScanDataMissing("日K缺少连续有效成交量，无法计算量比")


def _require_quote_kline_close_consistency(quote: Quote, latest: Kline) -> None:
    absolute_gap = abs(quote.price - latest.close)
    relative_limit = max(quote.price, latest.close) * FULL_MARKET_MAX_CLOSE_GAP_PCT / 100
    if absolute_gap > max(FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE, relative_limit):
        gap_pct = absolute_gap / latest.close * 100
        raise MarketScanDataMissing(
            f"报价收盘价与同日日K收盘价偏差 {gap_pct:.2f}%，数据快照可能不同步"
        )


def _metadata_tags(
    item: MarketScanResultItem,
    quality_score: int,
    *,
    quote_fallback_used: bool,
    kline_fallback_used: bool,
    industry_missing: bool,
    list_date_missing: bool,
) -> list[str]:
    tags: list[str] = []
    if item.is_st:
        tags.append("ST")
    if item.is_new:
        tags.append("新股")
    if industry_missing:
        tags.append("行业未知")
    if list_date_missing:
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
    industry_missing: bool,
    list_date_missing: bool,
) -> tuple[str, ...]:
    return tuple(
        reason
        for enabled, reason in (
            (quote_fallback_used, "quote_fallback"),
            (kline_fallback_used, "kline_fallback"),
            (industry_missing, "industry_missing"),
            (list_date_missing, "list_date_missing"),
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
        "schema_version": FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION,
        "rule_version": FULL_MARKET_SCORE_RULE_VERSION,
        "algorithms": {
            "trend_score": FULL_MARKET_TREND_ALGORITHM_VERSION,
            "volume_ratio": FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION,
            "data_quality": FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION,
            "leader_score": LEADER_SCORE_ALGORITHM_VERSION,
            "final_score": FULL_MARKET_SCORE_ALGORITHM_VERSION,
            "rank_refinement": MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
        },
        "leader_profile": leader_profile_spec(FULL_MARKET_LEADER_PROFILE),
        "tag_rules": leader_tag_rules_spec(STRONG_STOCK_TAG_RULES, "观察"),
        "volume_ratio": {
            "recent_window": FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW,
            "base_window": FULL_MARKET_VOLUME_RATIO_BASE_WINDOW,
            "min_count": FULL_MARKET_VOLUME_RATIO_MIN_COUNT,
            "precision": FULL_MARKET_VOLUME_RATIO_PRECISION,
        },
        "data_quality_policy": {
            "cached_quote": "neutral",
            "fallback_quote": "penalize",
            "stale_quote": "penalize",
            "quote_field_anomalies": "penalize",
            "kline_anomalies": "penalize",
        },
        "eligibility": {
            "min_data_quality_score": int(min_data_quality_score),
            "quote_timestamp_not_after_as_of": True,
            "single_price_session_excluded": True,
            "quote_kline_close_consistency": {
                "max_relative_gap_pct": FULL_MARKET_MAX_CLOSE_GAP_PCT,
                "max_absolute_gap": FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE,
                "accept_when": "within-either-limit",
            },
        },
        "final_score": {
            "formula": "leader_score - (100 - data_quality_score) * quality_penalty_per_missing_point",
            "quality_policy": "penalty-only",
            "quality_penalty_per_missing_point": FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT,
            "clamp": [0, 100],
        },
        "rounding": {
            "mode": LEADER_SCORE_ROUNDING_MODE,
            "component_stage": "after-quality-penalty-before-rank-refinement",
            "raw_score_decimals": FULL_MARKET_RAW_SCORE_DECIMALS,
            "metric_decimals": FULL_MARKET_METRIC_DECIMALS,
        },
        "ranking": {
            "refinement": market_scan_rank_refinement_spec(),
            "raw_score_formula": "base_score - (1 - refinement_score) * max_rank_discount",
            "base_score_minimum_step": 0.05,
            "tie_break": [list(item) for item in FULL_MARKET_SCORE_TIE_BREAK],
        },
    }


def _score_details(
    *,
    item: MarketScanResultItem,
    inputs: LeaderScoreInput,
    leader_breakdown: LeaderScoreBreakdown,
    rank_refinement: MarketScanRankRefinement,
    quality_score: int,
    quality_penalty: float,
    base_score: float,
    rank_discount: float,
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
        "inputs": _score_input_details(inputs, rank_refinement),
        "components": {
            "leader_score": {
                "base": leader_breakdown.base,
                "trend_delta": leader_breakdown.trend_delta,
                "rule_deltas": dict(leader_breakdown.rule_deltas),
                "unclamped": leader_breakdown.unclamped_score,
                "score": leader_breakdown.score,
            },
            "data_quality_score": quality_score,
            "rank_refinement": _rank_refinement_details(rank_refinement),
            "final_score": _final_score_details(
                quality_penalty,
                base_score,
                rank_discount,
                raw_score,
                rounded_score,
                score,
            ),
        },
        "ranking": {
            "tie_break": [list(entry) for entry in FULL_MARKET_SCORE_TIE_BREAK],
            "tie_break_values": {
                "raw_score": raw_score,
                "symbol": item.symbol,
            },
        },
    }


def _rank_refinement_details(refinement: MarketScanRankRefinement) -> dict[str, object]:
    return {
        "normalized_inputs": refinement.normalized_inputs,
        "components": refinement.components,
        "weighted_terms": refinement.weighted_terms,
        "score": refinement.score,
    }


def _score_input_details(
    inputs: LeaderScoreInput,
    refinement: MarketScanRankRefinement,
) -> dict[str, float | None]:
    return {
        "trend_score": inputs.trend_score,
        "change_pct": inputs.change_pct,
        "volume_ratio": inputs.volume_ratio,
        "amount": inputs.amount,
        "turnover_rate": inputs.turnover_rate,
        "data_quality_score": inputs.data_quality_score,
        **{f"rank_{name}": value for name, value in refinement.raw_inputs.items()},
    }


def _final_score_details(
    quality_penalty: float,
    base_score: float,
    rank_discount: float,
    raw_score: float,
    rounded_score: int,
    score: int,
) -> dict[str, int | float]:
    return {
        "quality_penalty": quality_penalty,
        "base": base_score,
        "rank_discount": rank_discount,
        "raw": raw_score,
        "rounded": rounded_score,
        "score": score,
    }


def _score_reason(calculated: _MarketScanScore) -> str:
    dominant_name, dominant_value = max(
        calculated.rank_refinement.weighted_terms.items(),
        key=lambda item: (item[1], item[0]),
    )
    dominant_labels = {
        "ma_alignment": "均线结构",
        "range_position_20d": "20日区间位置",
        "return_20d_pct": "20日收益",
        "return_5d_pct": "5日收益",
    }
    return (
        f"短线强势分 {calculated.score}（基础 {calculated.base_score:.2f}，趋势 {calculated.trend}，"
        f"质量扣分 {calculated.quality_penalty:.2f}）；中期精排 {calculated.rank_refinement.score:.3f}，"
        f"主要项为{dominant_labels.get(dominant_name, dominant_name)} {dominant_value:.3f}，"
        f"精排扣分 {calculated.rank_discount:.6f}；近5日量比 {calculated.volume_ratio:.2f}。完全同分按代码排序"
    )


def _ranking_bar_signature(row: Kline) -> tuple[float, float, float, float, float, str | None]:
    return (row.open, row.close, row.high, row.low, row.volume, row.adjustment_mode)


def _is_single_price_session(quote: Quote) -> bool:
    prices = (quote.open, quote.high, quote.low, quote.price)
    tolerance = max(0.0001, quote.price * 1e-8)
    return max(prices) - min(prices) <= tolerance


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
    "FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION",
    "FULL_MARKET_SCORE_TIE_BREAK",
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
