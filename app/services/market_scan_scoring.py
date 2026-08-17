from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
import math
import re
from typing import TYPE_CHECKING, Any, cast

from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MARKET_SCAN_MIN_HISTORY_ROWS,
    MARKET_SCAN_RANK_TIE_BREAK,
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanMode,
    MarketScanResultItem,
    MarketScanResultWrite,
    MarketScanRun,
)
from app.models.analysis import (
    DataQuality,
)
from app.models.market import (
    DAILY_KLINE_CONTRACT_VERSION,
    Kline,
    Quote,
    UNKNOWN_KLINE_DATA_VERSION,
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
from app.services.market_scan_score_contract import MarketScanReplayError, stable_score_spec_hash
from app.services.market_scan_rank_refinement import (
    MARKET_SCAN_CONTINUOUS_TREND_ALGORITHM_VERSION,
    MARKET_SCAN_CONTINUOUS_TREND_MAX_ADJUSTMENT,
    MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
    MarketScanRankRefinement,
    market_scan_continuous_trend_spec,
    market_scan_rank_refinement,
    market_scan_rank_refinement_spec,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_DIMENSION_ALGORITHM_VERSION,
    MarketScanScoreDimensions,
    build_market_scan_score_dimensions,
    verify_market_scan_point_in_time_evidence_context,
)
from app.services.market_scan_session_coverage import build_market_scan_session_coverage
from app.services.market_scan_skip_contract import (
    MarketScanSkipped,
    new_listing_insufficient_history_facts,
    session_gap_skip_facts,
)
from app.services.market_scan_skip_pit import build_market_scan_skip_pit
from app.services.scoring import clamp_score
from app.services.trading_calendar import is_trading_day
from app.utils.market_data import valid_kline, valid_quote
from app.utils.market_time import market_datetime_epoch, market_local_naive
from app.utils.symbols import standard_symbol


if TYPE_CHECKING:
    from app.services.market_scan_replay import MarketScanScoreReplay


FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION = 5
FULL_MARKET_SCORE_RULE_VERSION = "full-market-score-v5"
FULL_MARKET_SCORE_ALGORITHM_VERSION = "trend-quality-continuous-component-v4"
FULL_MARKET_LEGACY_V4_SCORE_SPEC_SCHEMA_VERSION = 4
FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION = "full-market-score-v4"
FULL_MARKET_LEGACY_V4_SCORE_ALGORITHM_VERSION = "trend-quality-penalty-v3"
FULL_MARKET_TREND_ALGORITHM_VERSION = "trend-score-v2-continuous-soft-clip"
FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION = "recent-volume-ratio-v2-explicit-windows"
FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION = "data-quality-v2-cache-neutral"
FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT = 0.15
FULL_MARKET_METRIC_DECIMALS = 4
FULL_MARKET_RAW_SCORE_DECIMALS = 6
FULL_MARKET_MAX_CLOSE_GAP_PCT = 0.5
FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE = 0.02
FULL_MARKET_MAX_CHANGE_PCT_GAP = 0.3
FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW = 5
FULL_MARKET_VOLUME_RATIO_BASE_WINDOW = 20
FULL_MARKET_VOLUME_RATIO_MIN_COUNT = FULL_MARKET_VOLUME_RATIO_RECENT_WINDOW + 1
FULL_MARKET_VOLUME_RATIO_PRECISION = 2
FULL_MARKET_SCORE_TIE_BREAK = MARKET_SCAN_RANK_TIE_BREAK
MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS = {
    "kind": "ordinal-cross-sectional-ranking",
    "expected_return": False,
    "probability": False,
    "benchmark": "none-in-production-score",
    "transaction_cost_model": "none-in-production-score",
    "execution_model": "none-in-production-score",
    "actionable": False,
}


class MarketScanDataMissing(ValueError):
    pass


def replay_score_details(details: Mapping[str, object]) -> MarketScanScoreReplay:
    from app.services.market_scan_replay import replay_score_details as replay

    return replay(details)


def verify_score_details(
    details: Mapping[str, object],
    *,
    expected_leader_score: int | None,
    expected_final_score: int | None,
) -> MarketScanScoreReplay:
    from app.services.market_scan_replay import verify_score_details as verify

    return verify(
        details,
        expected_leader_score=expected_leader_score,
        expected_final_score=expected_final_score,
    )


def verify_persisted_market_scan_result(
    item: MarketScanResultItem,
    run: MarketScanRun,
    *,
    expected_score_rule_version: str | None = None,
    expected_score_spec_hash: str | None = None,
) -> None:
    """Fail closed when a current production row no longer matches its score evidence."""
    if (
        item.status != "success"
        or not re.fullmatch(r"full-market-scan-v6:[0-9a-f]{64}", run.rule_version)
        or run.scope not in {MARKET_SCAN_FULL_MARKET_SCOPE, MARKET_SCAN_TOP100_REFRESH_SCOPE}
    ):
        return
    _verify_persisted_result_time_contract(item, run)
    replay = verify_score_details(
        item.score_details,
        expected_leader_score=item.leader_score,
        expected_final_score=item.score,
    )
    _verify_persisted_score_spec_contract(
        item,
        replay,
        expected_score_rule_version=expected_score_rule_version,
        expected_score_spec_hash=expected_score_spec_hash,
    )
    _verify_persisted_score_fields(item, run, replay)


def _verify_persisted_score_spec_contract(
    item: MarketScanResultItem,
    replay: MarketScanScoreReplay,
    *,
    expected_score_rule_version: str | None,
    expected_score_spec_hash: str | None,
) -> None:
    if expected_score_rule_version is not None or expected_score_spec_hash is not None:
        score_spec = item.score_details.get("score_spec")
        score_rule = score_spec.get("rule_version") if isinstance(score_spec, Mapping) else None
        if (
            replay.score_spec_schema_version != FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION
            or score_rule != expected_score_rule_version
            or item.score_details.get("score_spec_hash") != expected_score_spec_hash
            or not is_current_market_scan_score_spec(
                score_spec,
                item.score_details.get("score_spec_hash"),
            )
        ):
            raise MarketScanReplayError("当前生产榜单不是批次封存的 v5 评分规范")
    elif replay.score_spec_schema_version not in {
        FULL_MARKET_LEGACY_V4_SCORE_SPEC_SCHEMA_VERSION,
        FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION,
    }:
        raise MarketScanReplayError("生产榜单不是已注册的 v4/v5 评分规范")


def _verify_persisted_score_fields(
    item: MarketScanResultItem,
    run: MarketScanRun,
    replay: MarketScanScoreReplay,
) -> None:
    expected = {
        "trend_score": item.trend_score,
        "change_pct": item.change_pct,
        "volume_ratio": item.volume_ratio,
        "amount": item.amount,
        "turnover_rate": item.turnover_rate,
        "data_quality_score": item.data_quality_score,
    }
    if any(not _same_contract_number(replay.inputs.get(name), value) for name, value in expected.items()):
        raise MarketScanReplayError("生产榜单 outer fields 与评分输入不一致")
    if not _same_contract_number(replay.raw_score, item.raw_score):
        raise MarketScanReplayError("生产榜单 outer raw_score 与评分重放不一致")
    if replay.tie_break_values.get("symbol") != item.symbol:
        raise MarketScanReplayError("生产榜单 outer symbol 与评分明细不一致")
    if item.score_details.get("run_rule_version") != run.rule_version:
        raise MarketScanReplayError("生产榜单评分明细与批次规则版本不一致")
    if item.score_details.get("semantics") != MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS:
        raise MarketScanReplayError("生产榜单缺少明确成本、基准与可执行语义")
    evidence = _persisted_point_in_time_evidence(item)
    if evidence is None or not verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=item,
        expected_data_date=run.data_date,
        expected_quote_date=run.quote_date,
        expected_as_of=run.as_of,
        expected_mode=run.mode,
        require_action_eligible=False,
    ):
        raise MarketScanReplayError("生产榜单逐时点证据与 outer fields 不一致")


def _verify_persisted_result_time_contract(
    item: MarketScanResultItem,
    run: MarketScanRun,
) -> None:
    decision_epoch = market_datetime_epoch(run.as_of)
    event_epoch = market_datetime_epoch(item.quote_timestamp)
    observed_epoch = market_datetime_epoch(item.quote_observed_at)
    available_epoch = market_datetime_epoch(run.quote_capture_finished_at)
    updated_epoch = market_datetime_epoch(run.updated_at)
    if (
        decision_epoch is None
        or event_epoch is None
        or observed_epoch is None
        or available_epoch is None
        or updated_epoch is None
        or event_epoch > observed_epoch
        or observed_epoch > decision_epoch
        or decision_epoch > available_epoch
        or decision_epoch > updated_epoch
    ):
        raise MarketScanReplayError("生产榜单报价事件/观测/决策/可用时点顺序无效")


def _persisted_point_in_time_evidence(
    item: MarketScanResultItem,
) -> Mapping[str, object] | None:
    components = item.score_details.get("components")
    if not isinstance(components, Mapping):
        return None
    dimensions = components.get("score_dimensions")
    if not isinstance(dimensions, Mapping):
        return None
    evidence = dimensions.get("point_in_time_evidence")
    return evidence if isinstance(evidence, Mapping) else None


def _same_contract_number(left: object, right: object) -> bool:
    if (
        isinstance(left, bool)
        or isinstance(right, bool)
        or not isinstance(left, int | float)
        or not isinstance(right, int | float)
    ):
        return False
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-8)


def rank_score_details(
    rows: Iterable[tuple[str, Mapping[str, object]]],
) -> dict[str, int]:
    from app.services.market_scan_replay import rank_score_details as rank

    return rank(rows)


def __getattr__(name: str) -> Any:
    if name == "MarketScanScoreReplay":
        from app.services.market_scan_replay import MarketScanScoreReplay

        return MarketScanScoreReplay
    raise AttributeError(name)


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
    continuous_trend_adjustment: float
    base_score: float
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
    expected_quote_date: date | None = None,
    min_history_rows: int,
    min_data_quality_score: int,
    mode: MarketScanMode = "official",
    rule_version: str | None = None,
    quote_observed_at: str | None = None,
    new_stock_days: int = 120,
) -> MarketScanResultWrite:
    try:
        return _score_market_scan_item(
            item,
            quote,
            rows,
            as_of=as_of,
            completed_cutoff=completed_cutoff,
            expected_data_date=expected_data_date,
            expected_quote_date=expected_quote_date or expected_data_date,
            min_history_rows=min_history_rows,
            min_data_quality_score=min_data_quality_score,
            mode=mode,
            rule_version=rule_version,
            quote_observed_at=quote_observed_at,
            new_stock_days=new_stock_days,
        )
    except MarketScanSkipped as exc:
        exc.bind(
            symbol=item.symbol,
            code=item.code,
            market=item.market,
            name=item.name,
            metadata_source=item.metadata_source,
            is_new=item.is_new,
            list_date=item.list_date,
            mode=mode,
            run_rule_version=rule_version,
            as_of=as_of,
            expected_data_date=expected_data_date,
            expected_quote_date=expected_quote_date or expected_data_date,
            required_history_rows=max(MARKET_SCAN_MIN_HISTORY_ROWS, min_history_rows),
            new_stock_days=new_stock_days,
        )
        raise


def _score_market_scan_item(
    item: MarketScanResultItem,
    quote: Quote,
    rows: list[Kline],
    *,
    as_of: datetime,
    completed_cutoff: date,
    expected_data_date: date,
    expected_quote_date: date,
    min_history_rows: int,
    min_data_quality_score: int,
    mode: MarketScanMode,
    rule_version: str | None,
    quote_observed_at: str | None,
    new_stock_days: int,
) -> MarketScanResultWrite:
    _require_matching_quote(item, quote)
    _require_rankable_quote_fields(quote)
    _require_quote_return_consistency(quote)
    _require_quote_date(quote, expected_quote_date, as_of=as_of)
    completed_rows, latest_date = _rankable_completed_rows(
        item,
        rows,
        quote_observed_at=quote_observed_at,
        quote=quote,
        completed_cutoff=completed_cutoff,
        expected_data_date=expected_data_date,
        min_history_rows=min_history_rows,
        mode=mode,
        as_of=as_of,
        new_stock_days=new_stock_days,
    )
    _require_rankable_liquidity(quote, completed_rows)
    _require_official_session_coverage(
        completed_rows,
        quote=quote,
        quote_observed_at=quote_observed_at,
        mode=mode,
    )
    calculated = _calculate_market_scan_score(
        quote,
        completed_rows,
        as_of=as_of,
        min_data_quality_score=min_data_quality_score,
        mode=mode,
    )
    return _market_scan_result(
        item=item,
        quote=quote,
        rows=completed_rows,
        latest_date=latest_date,
        calculated=calculated,
        mode=mode,
        rule_version=rule_version,
    )


def _calculate_market_scan_score(
    quote: Quote,
    rows: list[Kline],
    *,
    as_of: datetime,
    min_data_quality_score: int,
    mode: MarketScanMode,
) -> _MarketScanScore:
    quality = _market_scan_quality(
        quote,
        rows,
        as_of=as_of,
        minimum_score=min_data_quality_score,
    )
    trend, _trend_label = trend_score(quote, rows, mode=mode)
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
    rank_refinement = market_scan_rank_refinement(quote, rows, mode=mode)
    quality_penalty, continuous_trend_adjustment, base_score, raw_score, score = (
        _continuous_final_score(
            leadership,
            quality_score=quality.score,
            continuous_trend_score=rank_refinement.score,
        )
    )
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
        continuous_trend_adjustment=continuous_trend_adjustment,
        base_score=base_score,
        raw_score=raw_score,
        rounded_score=round(base_score),
        score_spec=market_scan_score_spec(min_data_quality_score=min_data_quality_score),
    )


def _continuous_final_score(
    leadership: int,
    *,
    quality_score: int,
    continuous_trend_score: float,
) -> tuple[float, float, float, float, int]:
    quality_penalty = round(
        (100 - quality_score) * FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT,
        FULL_MARKET_METRIC_DECIMALS,
    )
    adjustment = round(
        (2 * continuous_trend_score - 1) * MARKET_SCAN_CONTINUOUS_TREND_MAX_ADJUSTMENT,
        FULL_MARKET_RAW_SCORE_DECIMALS,
    )
    base_score = round(
        min(100.0, max(0.0, leadership - quality_penalty + adjustment)),
        FULL_MARKET_METRIC_DECIMALS,
    )
    raw_score = round(base_score, FULL_MARKET_RAW_SCORE_DECIMALS)
    return quality_penalty, adjustment, base_score, raw_score, clamp_score(round(base_score))


def _require_official_session_coverage(
    rows: list[Kline],
    *,
    quote: Quote,
    quote_observed_at: str | None,
    mode: MarketScanMode,
) -> None:
    """Keep the official success cohort identical to the promotable PIT cohort."""
    if mode != "official":
        return
    coverage = build_market_scan_session_coverage(rows)
    if coverage.action_eligible:
        return
    _require_justified_skip_liquidity(quote, rows)
    raise MarketScanSkipped(
        "盘后正式排名要求连续可信交易会话："
        f"61根真实日K之间缺失 {len(coverage.missing_session_dates)} 个预期会话，"
        f"最大连续缺口 {coverage.max_gap_sessions}；已跳过且不会补造K线",
        reason_code="official_session_gap",
        facts=session_gap_skip_facts(
            pit=build_market_scan_skip_pit(
                quote,
                rows[-MARKET_SCAN_MIN_HISTORY_ROWS:],
                quote_observed_at=quote_observed_at or quote.timestamp,
            ),
            observed_session_dates=[
                row.date for row in rows[-MARKET_SCAN_MIN_HISTORY_ROWS:]
            ],
            coverage=coverage.as_dict(),
        ),
    )


def _market_scan_result(
    *,
    item: MarketScanResultItem, quote: Quote,
    rows: list[Kline],
    latest_date: date,
    calculated: _MarketScanScore,
    mode: MarketScanMode,
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
            continuous_trend_adjustment=calculated.continuous_trend_adjustment,
            base_score=calculated.base_score,
            score=calculated.score,
            raw_score=calculated.raw_score,
            rounded_score=calculated.rounded_score,
            dimensions=build_market_scan_score_dimensions(
                item,
                quote,
                rows,
                data_quality_score=calculated.quality.score,
                volume_ratio=calculated.volume_ratio,
                mode=mode,
            ),
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
    item: MarketScanResultItem,
    rows: list[Kline],
    *,
    quote_observed_at: str | None,
    quote: Quote,
    completed_cutoff: date,
    expected_data_date: date,
    min_history_rows: int,
    mode: MarketScanMode,
    as_of: datetime,
    new_stock_days: int,
) -> tuple[list[Kline], date]:
    completed_rows = completed_market_scan_klines(rows, completed_cutoff)
    required_history_rows = max(MARKET_SCAN_MIN_HISTORY_ROWS, min_history_rows)
    if len(completed_rows) < required_history_rows:
        _require_qfq_rows(completed_rows, as_of=as_of)
        message = (
            f"完整前复权日K不足：需要 {required_history_rows} 根，"
            f"当前 {len(completed_rows)} 根"
        )
        if item.is_new and item.list_date:
            _require_justified_skip_liquidity(quote, completed_rows)
            try:
                facts = new_listing_insufficient_history_facts(
                    pit=build_market_scan_skip_pit(
                        quote,
                        completed_rows,
                        quote_observed_at=quote_observed_at or quote.timestamp,
                    ),
                    list_date=item.list_date,
                    expected_data_date=expected_data_date,
                    observed_session_dates=[row.date for row in completed_rows],
                )
            except (RuntimeError, TypeError, ValueError):
                pass
            else:
                raise MarketScanSkipped(
                    message,
                    reason_code="new_listing_insufficient_history",
                    facts=facts,
                )
        raise MarketScanDataMissing(message)
    latest_date = date.fromisoformat(completed_rows[-1].date)
    if latest_date < expected_data_date:
        if quote.volume > 0 and quote.amount > 0:
            raise MarketScanDataMissing(f"当日报价存在有效成交，但日K仅到 {latest_date.isoformat()}，" f"早于应有交易日 {expected_data_date.isoformat()}")
        raise MarketScanDataMissing(f"日K停留在 {latest_date.isoformat()}，早于应有交易日 {expected_data_date.isoformat()}，可能停牌")
    if latest_date > expected_data_date:
        raise MarketScanDataMissing(f"日K日期 {latest_date.isoformat()} 晚于应有交易日 {expected_data_date.isoformat()}")
    _require_qfq_rows(completed_rows, as_of=as_of)
    _require_quote_kline_close_consistency(quote, completed_rows[-1], mode=mode)
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


def _require_qfq_rows(rows: list[Kline], *, as_of: datetime) -> None:
    if not rows:
        raise MarketScanDataMissing("截止时点之前没有有效完整日K")
    modes = {row.adjustment_mode for row in rows}
    if modes != {"qfq"}:
        raise MarketScanDataMissing("日K不是一致的前复权序列")
    cutoff = market_local_naive(as_of)
    previous_snapshot_time: datetime | None = None
    for row in rows:
        if row.contract_version != DAILY_KLINE_CONTRACT_VERSION:
            raise MarketScanDataMissing("日K合同版本未知或不一致")
        data_version = str(row.data_version or "").strip()
        if not data_version or data_version == UNKNOWN_KLINE_DATA_VERSION:
            raise MarketScanDataMissing("前复权日K缺少可审计数据版本")
        snapshot_time = _strict_kline_snapshot_time(row)
        if snapshot_time > cutoff:
            raise MarketScanDataMissing(
                f"日K快照时点 {snapshot_time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"晚于批次截止时点 {cutoff.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        if previous_snapshot_time is not None and snapshot_time < previous_snapshot_time:
            raise MarketScanDataMissing("日K数据版本时点随交易日倒退")
        previous_snapshot_time = snapshot_time


def _strict_kline_snapshot_time(row: Kline) -> datetime:
    try:
        snapshot_time = market_local_naive(datetime.fromisoformat(str(row.as_of or "")))
        row_date = date.fromisoformat(row.date)
    except ValueError as exc:
        raise MarketScanDataMissing("前复权日K缺少可解析快照时点") from exc
    if snapshot_time.date() < row_date:
        raise MarketScanDataMissing("日K快照时点早于对应交易日")
    return snapshot_time


def _require_quote_date(quote: Quote, expected_quote_date: date, *, as_of: datetime) -> None:
    quote_time = parse_quote_time(quote.timestamp)
    if quote_time is None:
        raise MarketScanDataMissing("报价时间无法解析")
    if quote_time.date() != expected_quote_date:
        raise MarketScanDataMissing(
            f"报价日期 {quote_time.date().isoformat()} 与完整交易日/应有行情日 "
            f"{expected_quote_date.isoformat()} 不一致"
        )
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
            raise MarketScanDataMissing("当日报价与日K均无有效成交，可能停牌")
        raise MarketScanDataMissing("报价缺少有效成交量或成交额")
    if _is_single_price_session(quote):
        raise MarketScanDataMissing("当日全天单一价格，无法确认开盘可成交性")
    if quote.turnover_rate is None:
        raise MarketScanDataMissing("报价缺少换手率")
    recent_volumes = [row.volume for row in rows[-20:]]
    if len(recent_volumes) < 6 or any(volume <= 0 for volume in recent_volumes):
        raise MarketScanDataMissing("日K缺少连续有效成交量，无法计算量比")


def _require_justified_skip_liquidity(quote: Quote, rows: list[Kline]) -> None:
    if quote.from_cache or quote.fallback_used:
        raise MarketScanDataMissing("跳过证据要求新鲜且非兜底的报价")
    if quote.volume <= 0 or quote.amount <= 0:
        raise MarketScanDataMissing("报价缺少有效成交量或成交额")
    if _is_single_price_session(quote):
        raise MarketScanDataMissing("当日全天单一价格，无法确认开盘可成交性")
    if quote.turnover_rate is None:
        raise MarketScanDataMissing("报价缺少换手率")
    if any(row.volume <= 0 or row.fallback_used for row in rows):
        raise MarketScanDataMissing("跳过证据的日K包含无成交或兜底会话")


def _require_rankable_quote_fields(quote: Quote) -> None:
    if not valid_quote(quote):
        raise MarketScanDataMissing("报价 OHLC、昨收价或成交字段不满足排名准入条件")


def _require_quote_return_consistency(quote: Quote) -> None:
    expected_change_pct = (quote.price - quote.prev_close) / quote.prev_close * 100
    if abs(expected_change_pct - quote.change_pct) > FULL_MARKET_MAX_CHANGE_PCT_GAP:
        raise MarketScanDataMissing(
            f"报价涨跌幅 {quote.change_pct:.4f}% 与现价/昨收推导值 "
            f"{expected_change_pct:.4f}% 偏差过大"
        )


def _require_quote_kline_close_consistency(
    quote: Quote,
    latest: Kline,
    *,
    mode: MarketScanMode,
) -> None:
    if mode == "intraday":
        reference_price = quote.prev_close
        label = "报价昨收价与上一完整日K收盘价"
    elif mode == "preopen":
        reference_price = quote.price
        label = "盘前复盘报价收盘价与上一完成交易日日K收盘价"
    else:
        reference_price = quote.price
        label = "报价收盘价与同日日K收盘价"
    absolute_gap = abs(reference_price - latest.close)
    relative_limit = max(reference_price, latest.close) * FULL_MARKET_MAX_CLOSE_GAP_PCT / 100
    if absolute_gap > max(FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE, relative_limit):
        gap_pct = absolute_gap / latest.close * 100
        raise MarketScanDataMissing(
            f"{label}偏差 {gap_pct:.2f}%，数据快照可能不同步"
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
        "algorithms": _score_algorithms_spec(),
        "leader_profile": leader_profile_spec(FULL_MARKET_LEADER_PROFILE),
        "research_dimensions": {
            "algorithm": MARKET_SCAN_DIMENSION_ALGORITHM_VERSION,
            "ranking_effect": "none",
            "semantics": "ordinal-not-probability",
            "actionable": False,
            "probability": False,
        },
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
        "eligibility": _score_eligibility_spec(min_data_quality_score),
        "final_score": {
            "formula": "leader_score - quality_penalty + continuous_trend_adjustment",
            "quality_policy": "penalty-only",
            "quality_penalty_per_missing_point": FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT,
            "clamp": [0, 100],
        },
        "rounding": {
            "mode": LEADER_SCORE_ROUNDING_MODE,
            "component_stage": "after-quality-penalty-and-continuous-trend-adjustment",
            "raw_score_decimals": FULL_MARKET_RAW_SCORE_DECIMALS,
            "metric_decimals": FULL_MARKET_METRIC_DECIMALS,
        },
        "ranking": _score_ranking_spec(),
    }


def is_current_market_scan_score_spec(
    score_spec: object,
    score_spec_hash: object,
) -> bool:
    """Return whether a write uses one exact, currently writable v5 contract."""
    if not isinstance(score_spec, Mapping) or not isinstance(score_spec_hash, str):
        return False
    eligibility = score_spec.get("eligibility")
    if not isinstance(eligibility, Mapping):
        return False
    minimum = eligibility.get("min_data_quality_score")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 0 <= minimum <= 100:
        return False
    expected = market_scan_score_spec(min_data_quality_score=minimum)
    return score_spec == expected and score_spec_hash == stable_score_spec_hash(expected)


def market_scan_score_spec_v4(*, min_data_quality_score: int) -> dict[str, object]:
    """Rebuild the frozen v4 contract for exact historical hash registration.

    New scans must never call this function.  It exists so read paths can keep
    verifying already published v4 snapshots after production advances to v5.
    """
    spec = market_scan_score_spec(min_data_quality_score=min_data_quality_score)
    spec["schema_version"] = FULL_MARKET_LEGACY_V4_SCORE_SPEC_SCHEMA_VERSION
    spec["rule_version"] = FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION
    algorithms = cast(dict[str, object], spec["algorithms"])
    algorithms["final_score"] = FULL_MARKET_LEGACY_V4_SCORE_ALGORITHM_VERSION
    algorithms["rank_refinement"] = MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION
    algorithms.pop("continuous_trend")
    eligibility = cast(dict[str, object], spec["eligibility"])
    eligibility.pop("official_contiguous_session_coverage_required")
    spec["final_score"] = {
        "formula": (
            "leader_score - (100 - data_quality_score) * "
            "quality_penalty_per_missing_point"
        ),
        "quality_policy": "penalty-only",
        "quality_penalty_per_missing_point": FULL_MARKET_QUALITY_PENALTY_PER_MISSING_POINT,
        "clamp": [0, 100],
    }
    rounding = cast(dict[str, object], spec["rounding"])
    rounding["component_stage"] = "after-quality-penalty-before-rank-refinement"
    spec["ranking"] = {
        "refinement": market_scan_rank_refinement_spec(),
        "raw_score_formula": "base_score - (1 - refinement_score) * max_rank_discount",
        "base_score_minimum_step": 0.05,
        "tie_break": [list(item) for item in FULL_MARKET_SCORE_TIE_BREAK],
    }
    return spec


def _score_algorithms_spec() -> dict[str, str]:
    return {
        "trend_score": FULL_MARKET_TREND_ALGORITHM_VERSION,
        "volume_ratio": FULL_MARKET_VOLUME_RATIO_ALGORITHM_VERSION,
        "data_quality": FULL_MARKET_DATA_QUALITY_ALGORITHM_VERSION,
        "leader_score": LEADER_SCORE_ALGORITHM_VERSION,
        "final_score": FULL_MARKET_SCORE_ALGORITHM_VERSION,
        "continuous_trend": MARKET_SCAN_CONTINUOUS_TREND_ALGORITHM_VERSION,
    }


def _score_eligibility_spec(min_data_quality_score: int) -> dict[str, object]:
    return {
        "min_data_quality_score": int(min_data_quality_score),
        "valid_quote_fields_required": True,
        "max_change_pct_gap": FULL_MARKET_MAX_CHANGE_PCT_GAP,
        "quote_timestamp_not_after_as_of": True,
        "single_price_session_excluded": True,
        "official_contiguous_session_coverage_required": True,
        "quote_kline_close_consistency": {
            "max_relative_gap_pct": FULL_MARKET_MAX_CLOSE_GAP_PCT,
            "max_absolute_gap": FULL_MARKET_MAX_CLOSE_GAP_ABSOLUTE,
            "accept_when": "within-either-limit",
        },
    }


def _score_ranking_spec() -> dict[str, object]:
    return {
        "continuous_trend": market_scan_continuous_trend_spec(),
        "base_score_formula": "leader_score - quality_penalty + continuous_trend_adjustment",
        "raw_score_formula": "base_score",
        "tie_break": [list(item) for item in FULL_MARKET_SCORE_TIE_BREAK],
    }


def _score_details(
    *,
    item: MarketScanResultItem,
    inputs: LeaderScoreInput,
    leader_breakdown: LeaderScoreBreakdown,
    rank_refinement: MarketScanRankRefinement,
    quality_score: int,
    quality_penalty: float,
    continuous_trend_adjustment: float,
    base_score: float,
    score: int,
    raw_score: float,
    rounded_score: int,
    dimensions: MarketScanScoreDimensions,
    score_spec: dict[str, object],
    rule_version: str | None,
) -> dict[str, object]:
    score_spec_hash = stable_score_spec_hash(score_spec)
    return {
        "schema_version": 1,
        "semantics": dict(MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS),
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
            "continuous_trend": _continuous_trend_details(rank_refinement),
            "final_score": _final_score_details(
                quality_penalty,
                continuous_trend_adjustment,
                base_score,
                raw_score,
                rounded_score,
                score,
            ),
            "score_dimensions": dimensions.details(),
        },
        "ranking": {
            "tie_break": [list(entry) for entry in FULL_MARKET_SCORE_TIE_BREAK],
            "tie_break_values": {
                "raw_score": raw_score,
                "symbol": item.symbol,
            },
        },
    }


def _continuous_trend_details(refinement: MarketScanRankRefinement) -> dict[str, object]:
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
        **{
            f"continuous_trend_{name}": value
            for name, value in refinement.raw_inputs.items()
        },
    }


def _final_score_details(
    quality_penalty: float,
    continuous_trend_adjustment: float,
    base_score: float,
    raw_score: float,
    rounded_score: int,
    score: int,
) -> dict[str, int | float]:
    return {
        "quality_penalty": quality_penalty,
        "continuous_trend_adjustment": continuous_trend_adjustment,
        "base": base_score,
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
        f"趋势强度 {calculated.score}（序数状态分，非上涨概率；基础 {calculated.base_score:.2f}，趋势 {calculated.trend}，"
        f"质量扣分 {calculated.quality_penalty:.2f}）；连续中期趋势 {calculated.rank_refinement.score:.3f}，"
        f"主要项为{dominant_labels.get(dominant_name, dominant_name)} {dominant_value:.3f}，"
        f"基础分调整 {calculated.continuous_trend_adjustment:+.4f}；"
        f"近5日量比 {calculated.volume_ratio:.2f}。完全同分按代码排序"
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
    "FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION",
    "FULL_MARKET_LEGACY_V4_SCORE_SPEC_SCHEMA_VERSION",
    "FULL_MARKET_SCORE_ALGORITHM_VERSION",
    "FULL_MARKET_SCORE_RULE_VERSION",
    "FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION",
    "FULL_MARKET_SCORE_TIE_BREAK",
    "MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS",
    "MarketScanDataMissing",
    "MarketScanReplayError",
    "MarketScanScoreReplay",
    "MarketScanSkipped",
    "completed_market_scan_klines",
    "is_current_market_scan_score_spec",
    "market_scan_score_spec",
    "market_scan_score_spec_v4",
    "rank_score_details",
    "replay_score_details",
    "score_market_scan_item",
    "stable_score_spec_hash",
    "verify_score_details",
    "verify_persisted_market_scan_result",
]
