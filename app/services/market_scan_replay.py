from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cmp_to_key
import math

from app.services.market_scan_rank_refinement import (
    MARKET_SCAN_CONTINUOUS_TREND_ALGORITHM_VERSION,
    MARKET_SCAN_CONTINUOUS_TREND_MAX_ADJUSTMENT,
    MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
    MARKET_SCAN_RANK_REFINEMENT_BOUNDS,
    MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT,
    MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
    MARKET_SCAN_RANK_REFINEMENT_WEIGHTS,
    market_scan_continuous_trend_spec,
    market_scan_rank_refinement_spec,
)
from app.services.market_scan_score_contract import MarketScanReplayError, stable_score_spec_hash


_LEGACY_SCORE_SPEC_SCHEMA_VERSION = 2
_V3_SCORE_SPEC_SCHEMA_VERSION = 3
_V4_SCORE_SPEC_SCHEMA_VERSION = 4
_CURRENT_SCORE_SPEC_SCHEMA_VERSION = 5
_SUPPORTED_ALGORITHMS_BY_SCHEMA = {
    _LEGACY_SCORE_SPEC_SCHEMA_VERSION: {
        "trend_score": "trend-score-v1",
        "volume_ratio": "recent-volume-ratio-v1",
        "data_quality": "data-quality-v1",
        "leader_score": "leader-score-additive-v1",
        "final_score": "weighted-leader-quality-v1",
    },
    _V3_SCORE_SPEC_SCHEMA_VERSION: {
        "trend_score": "trend-score-v1",
        "volume_ratio": "recent-volume-ratio-v2-explicit-windows",
        "data_quality": "data-quality-v2-cache-neutral",
        "leader_score": "leader-score-additive-v1",
        "final_score": "weighted-trend-quality-v2",
    },
    _V4_SCORE_SPEC_SCHEMA_VERSION: {
        "trend_score": "trend-score-v2-continuous-soft-clip",
        "volume_ratio": "recent-volume-ratio-v2-explicit-windows",
        "data_quality": "data-quality-v2-cache-neutral",
        "leader_score": "leader-score-additive-v1",
        "final_score": "trend-quality-penalty-v3",
        "rank_refinement": MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
    },
    _CURRENT_SCORE_SPEC_SCHEMA_VERSION: {
        "trend_score": "trend-score-v2-continuous-soft-clip",
        "volume_ratio": "recent-volume-ratio-v2-explicit-windows",
        "data_quality": "data-quality-v2-cache-neutral",
        "leader_score": "leader-score-additive-v1",
        "final_score": "trend-quality-continuous-component-v4",
        "continuous_trend": MARKET_SCAN_CONTINUOUS_TREND_ALGORITHM_VERSION,
    },
}
_SUPPORTED_ROUNDING_MODE = "python-round-half-to-even"
_SUPPORTED_TIE_BREAK_FIELDS = frozenset({"score", "raw_score", "trend_score", "change_pct", "amount", "symbol"})
_CURRENT_LEADER_PROFILE_ID = "full-market-trend-only-v1"
_V3_TIE_BREAK = (
    ("score", "desc"),
    ("raw_score", "desc"),
    ("trend_score", "desc"),
    ("change_pct", "desc"),
    ("amount", "desc"),
    ("symbol", "asc"),
)
_CURRENT_TIE_BREAK = (("raw_score", "desc"), ("symbol", "asc"))
_CURRENT_VOLUME_RATIO_SPEC = {
    "recent_window": 5,
    "base_window": 20,
    "min_count": 6,
    "precision": 2,
}
_CURRENT_DATA_QUALITY_POLICY = {
    "cached_quote": "neutral",
    "fallback_quote": "penalize",
    "stale_quote": "penalize",
    "quote_field_anomalies": "penalize",
    "kline_anomalies": "penalize",
}
_V3_FINAL_SCORE_FORMULA = "leader_score * leader_weight + data_quality_score * quality_weight"
_V4_FINAL_SCORE_FORMULA = (
    "leader_score - (100 - data_quality_score) * quality_penalty_per_missing_point"
)
_CURRENT_FINAL_SCORE_FORMULA = (
    "leader_score - quality_penalty + continuous_trend_adjustment"
)
_CURRENT_QUALITY_PENALTY_PER_MISSING_POINT = 0.15
_CURRENT_CLOSE_CONSISTENCY = {
    "max_relative_gap_pct": 0.5,
    "max_absolute_gap": 0.02,
    "accept_when": "within-either-limit",
}
_CURRENT_SINGLE_PRICE_SESSION_EXCLUDED = True
_CURRENT_VALID_QUOTE_FIELDS_REQUIRED = True
_CURRENT_MAX_CHANGE_PCT_GAP = 0.3


@dataclass(frozen=True)
class _LeaderReplayBreakdown:
    base: int
    trend_delta: int
    rule_deltas: dict[str, int]
    unclamped_score: int
    score: int


@dataclass(frozen=True)
class _RankRefinementReplay:
    normalized_inputs: dict[str, float]
    components: dict[str, float]
    weighted_terms: dict[str, float]
    score: float


@dataclass(frozen=True)
class _FinalReplayBreakdown:
    raw_score: float
    final_score: int
    rounded_score: int
    base_score: float
    quality_penalty: float | None = None
    rank_discount: float | None = None
    continuous_trend_adjustment: float | None = None
    rank_refinement: _RankRefinementReplay | None = None
    weighted_terms: dict[str, float] | None = None


@dataclass(frozen=True)
class MarketScanScoreReplay:
    score_spec_schema_version: int
    score_spec_hash: str
    leader_score: int
    raw_score: float
    final_score: int
    tie_break: tuple[tuple[str, str], ...]
    tie_break_values: dict[str, int | float | str]
    inputs: dict[str, float]


def replay_score_details(details: Mapping[str, object]) -> MarketScanScoreReplay:
    payload = _mapping(details, "score_details")
    if payload.get("schema_version") != 1:
        raise MarketScanReplayError(f"未知 score_details schema：{payload.get('schema_version')!r}")
    score_spec, score_spec_schema, actual_hash, algorithms = _verified_score_spec(payload)
    inputs = _score_inputs(payload, schema_version=score_spec_schema)
    leader_breakdown = _replay_leader_score(
        score_spec,
        inputs,
        leader_algorithm=algorithms["leader_score"],
    )
    leader_score = leader_breakdown.score
    final_breakdown = _replay_final_score(
        score_spec,
        inputs,
        leader_score,
        final_algorithm=algorithms["final_score"],
    )
    tie_break = _tie_break_contract(score_spec, payload)
    tie_break_values = _tie_break_values(payload, tie_break)
    _verify_tie_break_values(
        tie_break_values,
        inputs,
        final_breakdown.raw_score,
        final_breakdown.final_score,
    )
    _verify_persisted_components(
        payload,
        score_spec,
        inputs,
        leader_breakdown,
        final_breakdown,
    )
    return MarketScanScoreReplay(
        score_spec_schema_version=score_spec_schema,
        score_spec_hash=actual_hash,
        leader_score=leader_score,
        raw_score=final_breakdown.raw_score,
        final_score=final_breakdown.final_score,
        tie_break=tie_break,
        tie_break_values=tie_break_values,
        inputs=dict(inputs),
    )


def _verified_score_spec(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], int, str, Mapping[str, str]]:
    score_spec = _mapping(payload.get("score_spec"), "score_spec")
    schema = _integer(score_spec.get("schema_version"), "score_spec.schema_version")
    if schema not in _SUPPORTED_ALGORITHMS_BY_SCHEMA:
        raise MarketScanReplayError(
            f"未知 score_spec schema：{schema!r}；仅兼容版本 2、3、4 和当前版 5"
        )
    expected_hash = _text(payload.get("score_spec_hash"), "score_spec_hash")
    actual_hash = stable_score_spec_hash(score_spec)
    if expected_hash != actual_hash:
        raise MarketScanReplayError("评分规范 hash 不一致，持久化明细已损坏")
    algorithms = _require_supported_algorithms(score_spec, schema_version=schema)
    _require_supported_rounding(score_spec)
    if schema == _V3_SCORE_SPEC_SCHEMA_VERSION:
        _require_v3_score_contract(score_spec)
    elif schema == _V4_SCORE_SPEC_SCHEMA_VERSION:
        _require_v4_score_contract(score_spec)
    elif schema == _CURRENT_SCORE_SPEC_SCHEMA_VERSION:
        _require_current_score_contract(score_spec)
    return score_spec, schema, actual_hash, algorithms


def verify_score_details(
    details: Mapping[str, object],
    *,
    expected_leader_score: int | None,
    expected_final_score: int | None,
) -> MarketScanScoreReplay:
    replay = replay_score_details(details)
    if expected_leader_score is None or expected_final_score is None:
        raise MarketScanReplayError("SQLite 评分为空，无法验证重放结果")
    if replay.leader_score != expected_leader_score:
        raise MarketScanReplayError(f"leader score 重放不一致：{replay.leader_score} != {expected_leader_score}")
    if replay.final_score != expected_final_score:
        raise MarketScanReplayError(f"final score 重放不一致：{replay.final_score} != {expected_final_score}")
    return replay


def rank_score_details(
    rows: Iterable[tuple[str, Mapping[str, object]]],
) -> dict[str, int]:
    replayed: list[tuple[str, MarketScanScoreReplay]] = []
    seen: set[str] = set()
    for symbol, details in rows:
        normalized_symbol = _text(symbol, "symbol")
        if normalized_symbol in seen:
            raise MarketScanReplayError(f"重放排序包含重复股票：{normalized_symbol}")
        replay = replay_score_details(details)
        if replay.tie_break_values.get("symbol") != normalized_symbol:
            raise MarketScanReplayError(f"排序股票与持久化 symbol 不一致：{normalized_symbol}")
        seen.add(normalized_symbol)
        replayed.append((normalized_symbol, replay))
    if not replayed:
        return {}
    reference_hash = replayed[0][1].score_spec_hash
    reference_tie_break = replayed[0][1].tie_break
    if any(replay.score_spec_hash != reference_hash or replay.tie_break != reference_tie_break for _symbol, replay in replayed[1:]):
        raise MarketScanReplayError("重放排序混入不同评分规范或 tie-break")

    def compare(
        left: tuple[str, MarketScanScoreReplay],
        right: tuple[str, MarketScanScoreReplay],
    ) -> int:
        return _compare_replays(left[1], right[1], reference_tie_break)

    ordered = sorted(
        replayed,
        key=cmp_to_key(compare),
    )
    return {symbol: rank for rank, (symbol, _replay) in enumerate(ordered, start=1)}


def _require_supported_algorithms(
    score_spec: Mapping[str, object],
    *,
    schema_version: int,
) -> Mapping[str, str]:
    algorithms = _mapping(score_spec.get("algorithms"), "score_spec.algorithms")
    supported_algorithms = _SUPPORTED_ALGORITHMS_BY_SCHEMA[schema_version]
    if set(algorithms) != set(supported_algorithms):
        raise MarketScanReplayError("评分算法集合不完整或包含未知条目")
    for name, supported in supported_algorithms.items():
        configured = algorithms.get(name)
        if configured != supported:
            raise MarketScanReplayError(f"未知评分算法 {name}：{configured!r}，仅支持 {supported}")
    return supported_algorithms


def _require_supported_rounding(score_spec: Mapping[str, object]) -> None:
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    mode = rounding.get("mode")
    if mode != _SUPPORTED_ROUNDING_MODE:
        raise MarketScanReplayError(f"未知舍入算法：{mode!r}")


def _require_v3_score_contract(score_spec: Mapping[str, object]) -> None:
    _require_current_leader_profile(score_spec)
    _require_v3_data_contract(score_spec)
    _require_v3_final_score_contract(score_spec)
    _require_v3_rounding_contract(score_spec)
    _require_v3_ranking_contract(score_spec)


def _require_v4_score_contract(score_spec: Mapping[str, object]) -> None:
    _require_current_leader_profile(score_spec)
    _require_v4_data_contract(score_spec)
    _require_v4_final_score_contract(score_spec)
    _require_v4_rounding_contract(score_spec)
    _require_v4_ranking_contract(score_spec)


def _require_current_score_contract(score_spec: Mapping[str, object]) -> None:
    _require_current_leader_profile(score_spec)
    _require_current_data_contract(score_spec)
    _require_current_final_score_contract(score_spec)
    _require_current_rounding_contract(score_spec)
    _require_current_ranking_contract(score_spec)


def _require_current_leader_profile(score_spec: Mapping[str, object]) -> None:
    profile = _mapping(score_spec.get("leader_profile"), "score_spec.leader_profile")
    if set(profile) != {"algorithm", "profile_id", "base", "trend_weight", "rules"}:
        raise MarketScanReplayError("当前 leader profile 字段不完整或包含未知条目")
    if profile.get("profile_id") != _CURRENT_LEADER_PROFILE_ID:
        raise MarketScanReplayError(f"未知全市场 leader profile：{profile.get('profile_id')!r}")
    if _integer(profile.get("base"), "leader_profile.base") != 50:
        raise MarketScanReplayError("全市场 leader profile base 必须为 50")
    if _number(profile.get("trend_weight"), "leader_profile.trend_weight") != 1.0:
        raise MarketScanReplayError("全市场 leader profile trend_weight 必须为 1.0")
    if _list(profile.get("rules"), "leader_profile.rules"):
        raise MarketScanReplayError("全市场 leader profile 不允许重复叠加涨幅、量比、换手率或成交额规则")


def _require_shared_data_contract(score_spec: Mapping[str, object]) -> Mapping[str, object]:
    volume_ratio = _mapping(score_spec.get("volume_ratio"), "score_spec.volume_ratio")
    if dict(volume_ratio) != _CURRENT_VOLUME_RATIO_SPEC:
        raise MarketScanReplayError("未知全市场量比窗口规范")
    quality_policy = _mapping(
        score_spec.get("data_quality_policy"),
        "score_spec.data_quality_policy",
    )
    if dict(quality_policy) != _CURRENT_DATA_QUALITY_POLICY:
        raise MarketScanReplayError("未知全市场数据质量策略")
    return _mapping(score_spec.get("eligibility"), "score_spec.eligibility")


def _require_quality_floor(eligibility: Mapping[str, object]) -> None:
    minimum_quality = _integer(
        eligibility.get("min_data_quality_score"),
        "eligibility.min_data_quality_score",
    )
    if not 0 <= minimum_quality <= 100:
        raise MarketScanReplayError("数据质量排名门槛必须位于 0 到 100")


def _require_v3_data_contract(score_spec: Mapping[str, object]) -> None:
    eligibility = _require_shared_data_contract(score_spec)
    if set(eligibility) != {"min_data_quality_score"}:
        raise MarketScanReplayError("v3 eligibility 字段不完整或包含未知条目")
    _require_quality_floor(eligibility)


def _require_v4_data_contract(score_spec: Mapping[str, object]) -> None:
    eligibility = _require_shared_data_contract(score_spec)
    if set(eligibility) != {
        "min_data_quality_score",
        "valid_quote_fields_required",
        "max_change_pct_gap",
        "quote_timestamp_not_after_as_of",
        "single_price_session_excluded",
        "quote_kline_close_consistency",
    }:
        raise MarketScanReplayError("v4 eligibility 字段不完整或包含未知条目")
    _require_v4_and_current_data_values(eligibility)


def _require_current_data_contract(score_spec: Mapping[str, object]) -> None:
    eligibility = _require_shared_data_contract(score_spec)
    if set(eligibility) != {
        "min_data_quality_score",
        "valid_quote_fields_required",
        "max_change_pct_gap",
        "quote_timestamp_not_after_as_of",
        "single_price_session_excluded",
        "official_contiguous_session_coverage_required",
        "quote_kline_close_consistency",
    }:
        raise MarketScanReplayError("当前 eligibility 字段不完整或包含未知条目")
    if eligibility.get("official_contiguous_session_coverage_required") is not True:
        raise MarketScanReplayError("v5 盘后成功结果必须具备连续可信交易会话")
    _require_v4_and_current_data_values(eligibility)


def _require_v4_and_current_data_values(eligibility: Mapping[str, object]) -> None:
    _require_quality_floor(eligibility)
    if eligibility.get("valid_quote_fields_required") is not _CURRENT_VALID_QUOTE_FIELDS_REQUIRED:
        raise MarketScanReplayError("排名准入必须校验报价 OHLC 与成交字段")
    if _number(eligibility.get("max_change_pct_gap"), "eligibility.max_change_pct_gap") != _CURRENT_MAX_CHANGE_PCT_GAP:
        raise MarketScanReplayError("未知报价涨跌幅一致性阈值")
    if eligibility.get("quote_timestamp_not_after_as_of") is not True:
        raise MarketScanReplayError("报价时间必须不晚于批次截止时点")
    if eligibility.get("single_price_session_excluded") is not _CURRENT_SINGLE_PRICE_SESSION_EXCLUDED:
        raise MarketScanReplayError("全天单一价格的股票必须排除出可交易榜单")
    consistency = _mapping(
        eligibility.get("quote_kline_close_consistency"),
        "eligibility.quote_kline_close_consistency",
    )
    if dict(consistency) != _CURRENT_CLOSE_CONSISTENCY:
        raise MarketScanReplayError("未知报价与日K收盘价一致性规范")


def _require_v3_final_score_contract(score_spec: Mapping[str, object]) -> None:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    if final.get("formula") != _V3_FINAL_SCORE_FORMULA:
        raise MarketScanReplayError("未知 final score 公式")
    weights = _mapping(final.get("weights"), "final_score.weights")
    if dict(weights) != {"leader_score": 0.85, "data_quality_score": 0.15}:
        raise MarketScanReplayError("未知 final score 权重")
    if _list(final.get("clamp"), "final_score.clamp") != [0, 100]:
        raise MarketScanReplayError("未知 final score clamp")


def _require_v4_final_score_contract(score_spec: Mapping[str, object]) -> None:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    if set(final) != {"formula", "quality_policy", "quality_penalty_per_missing_point", "clamp"}:
        raise MarketScanReplayError("v4 final score 字段不完整或包含未知条目")
    if final.get("formula") != _V4_FINAL_SCORE_FORMULA:
        raise MarketScanReplayError("未知 final score 公式")
    _require_v4_and_current_final_values(final)


def _require_current_final_score_contract(score_spec: Mapping[str, object]) -> None:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    if set(final) != {"formula", "quality_policy", "quality_penalty_per_missing_point", "clamp"}:
        raise MarketScanReplayError("当前 final score 字段不完整或包含未知条目")
    if final.get("formula") != _CURRENT_FINAL_SCORE_FORMULA:
        raise MarketScanReplayError("未知 final score 公式")
    _require_v4_and_current_final_values(final)


def _require_v4_and_current_final_values(final: Mapping[str, object]) -> None:
    if final.get("quality_policy") != "penalty-only":
        raise MarketScanReplayError("数据质量只能作为评分惩罚，不能作为强度加分")
    if (
        _number(
            final.get("quality_penalty_per_missing_point"),
            "final_score.quality_penalty_per_missing_point",
        )
        != _CURRENT_QUALITY_PENALTY_PER_MISSING_POINT
    ):
        raise MarketScanReplayError("未知数据质量惩罚系数")
    if _list(final.get("clamp"), "final_score.clamp") != [0, 100]:
        raise MarketScanReplayError("未知 final score clamp")


def _require_v3_rounding_contract(score_spec: Mapping[str, object]) -> None:
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    if rounding.get("component_stage") != "after-trend-weight-and-final-weighted-sum":
        raise MarketScanReplayError("未知评分舍入阶段")
    if _integer(rounding.get("raw_score_decimals"), "rounding.raw_score_decimals") != 4:
        raise MarketScanReplayError("未知 raw score 精度")
    if _integer(rounding.get("metric_decimals"), "rounding.metric_decimals") != 4:
        raise MarketScanReplayError("未知指标精度")


def _require_v4_rounding_contract(score_spec: Mapping[str, object]) -> None:
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    if rounding.get("component_stage") != "after-quality-penalty-before-rank-refinement":
        raise MarketScanReplayError("未知 v4 评分舍入阶段")
    _require_v4_and_current_rounding_values(rounding)


def _require_current_rounding_contract(score_spec: Mapping[str, object]) -> None:
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    if (
        rounding.get("component_stage")
        != "after-quality-penalty-and-continuous-trend-adjustment"
    ):
        raise MarketScanReplayError("未知 v5 评分舍入阶段")
    _require_v4_and_current_rounding_values(rounding)


def _require_v4_and_current_rounding_values(rounding: Mapping[str, object]) -> None:
    if _integer(rounding.get("raw_score_decimals"), "rounding.raw_score_decimals") != 6:
        raise MarketScanReplayError("未知 raw score 精度")
    if _integer(rounding.get("metric_decimals"), "rounding.metric_decimals") != 4:
        raise MarketScanReplayError("未知指标精度")


def _require_v3_ranking_contract(score_spec: Mapping[str, object]) -> None:
    ranking = _mapping(score_spec.get("ranking"), "score_spec.ranking")
    if _parse_tie_break(ranking.get("tie_break")) != _V3_TIE_BREAK:
        raise MarketScanReplayError("v3 评分规范必须按 score、raw_score 及稳定决胜字段排序")


def _require_v4_ranking_contract(score_spec: Mapping[str, object]) -> None:
    ranking = _mapping(score_spec.get("ranking"), "score_spec.ranking")
    if set(ranking) != {"refinement", "raw_score_formula", "base_score_minimum_step", "tie_break"}:
        raise MarketScanReplayError("v4 ranking 字段不完整或包含未知条目")
    if dict(_mapping(ranking.get("refinement"), "ranking.refinement")) != market_scan_rank_refinement_spec():
        raise MarketScanReplayError("未知全市场连续趋势精排规范")
    if ranking.get("raw_score_formula") != "base_score - (1 - refinement_score) * max_rank_discount":
        raise MarketScanReplayError("未知 raw score 精排公式")
    minimum_step = _number(ranking.get("base_score_minimum_step"), "ranking.base_score_minimum_step")
    if minimum_step != 0.05 or MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT >= minimum_step:
        raise MarketScanReplayError("连续精排折扣必须小于基础分最小步长")
    if _parse_tie_break(ranking.get("tie_break")) != _CURRENT_TIE_BREAK:
        raise MarketScanReplayError("v4 评分规范必须按 raw_score 和 symbol 稳定排序")


def _require_current_ranking_contract(score_spec: Mapping[str, object]) -> None:
    ranking = _mapping(score_spec.get("ranking"), "score_spec.ranking")
    if set(ranking) != {
        "continuous_trend",
        "base_score_formula",
        "raw_score_formula",
        "tie_break",
    }:
        raise MarketScanReplayError("当前 ranking 字段不完整或包含未知条目")
    continuous = _mapping(ranking.get("continuous_trend"), "ranking.continuous_trend")
    if dict(continuous) != market_scan_continuous_trend_spec():
        raise MarketScanReplayError("未知全市场连续中期趋势规范")
    if ranking.get("base_score_formula") != _CURRENT_FINAL_SCORE_FORMULA:
        raise MarketScanReplayError("未知 v5 基础分公式")
    if ranking.get("raw_score_formula") != "base_score":
        raise MarketScanReplayError("v5 raw_score 必须等于真实基础分")
    if _parse_tie_break(ranking.get("tie_break")) != _CURRENT_TIE_BREAK:
        raise MarketScanReplayError("当前评分规范必须按 raw_score 和 symbol 稳定排序")


def _score_inputs(payload: Mapping[str, object], *, schema_version: int) -> dict[str, float]:
    raw = _mapping(payload.get("inputs"), "score_details.inputs")
    inputs = {
        "trend_score": _number(raw.get("trend_score"), "inputs.trend_score"),
        "change_pct": _number(raw.get("change_pct"), "inputs.change_pct"),
        "volume_ratio": _number(raw.get("volume_ratio"), "inputs.volume_ratio"),
        "amount": _number(raw.get("amount"), "inputs.amount"),
        "turnover_rate": _optional_number(
            raw.get("turnover_rate"),
            "inputs.turnover_rate",
        ),
        "data_quality_score": _number(
            raw.get("data_quality_score"),
            "inputs.data_quality_score",
        ),
    }
    if schema_version in {
        _V4_SCORE_SPEC_SCHEMA_VERSION,
        _CURRENT_SCORE_SPEC_SCHEMA_VERSION,
    }:
        prefix = (
            "rank_"
            if schema_version == _V4_SCORE_SPEC_SCHEMA_VERSION
            else "continuous_trend_"
        )
        inputs.update(
            {
                f"rank_{name}": _number(
                    raw.get(f"{prefix}{name}"),
                    f"inputs.{prefix}{name}",
                )
                for name in MARKET_SCAN_RANK_REFINEMENT_BOUNDS
            }
        )
    for field in ("trend_score", "data_quality_score"):
        if not 0 <= inputs[field] <= 100:
            raise MarketScanReplayError(f"评分明细损坏：inputs.{field} 必须位于 0 到 100")
    for field in ("volume_ratio", "amount", "turnover_rate"):
        if inputs[field] < 0:
            raise MarketScanReplayError(f"评分明细损坏：inputs.{field} 不能为负数")
    if schema_version in {
        _V4_SCORE_SPEC_SCHEMA_VERSION,
        _CURRENT_SCORE_SPEC_SCHEMA_VERSION,
    }:
        position = inputs["rank_range_position_20d"]
        if not 0 <= position <= 1:
            raise MarketScanReplayError("评分明细损坏：inputs.rank_range_position_20d 必须位于 0 到 1")
    return inputs


def _replay_leader_score(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    *,
    leader_algorithm: str,
) -> _LeaderReplayBreakdown:
    profile = _mapping(score_spec.get("leader_profile"), "score_spec.leader_profile")
    if profile.get("algorithm") != leader_algorithm:
        raise MarketScanReplayError(f"未知 leader profile 算法：{profile.get('algorithm')!r}")
    base = _integer(profile.get("base"), "leader_profile.base")
    trend_weight = _number(
        profile.get("trend_weight"),
        "leader_profile.trend_weight",
    )
    trend_delta = round((inputs["trend_score"] - 50) * trend_weight)
    score = base + trend_delta
    rules = _list(profile.get("rules"), "leader_profile.rules")
    seen: set[str] = set()
    rule_deltas: dict[str, int] = {}
    for index, raw_rule in enumerate(rules):
        rule = _mapping(raw_rule, f"leader_profile.rules[{index}]")
        name = _text(rule.get("name"), f"leader_profile.rules[{index}].name")
        if name in seen:
            raise MarketScanReplayError(f"leader rule 重复：{name}")
        seen.add(name)
        delta = _rule_delta(rule, inputs)
        rule_deltas[name] = delta
        score += delta
    return _LeaderReplayBreakdown(
        base=base,
        trend_delta=trend_delta,
        rule_deltas=rule_deltas,
        unclamped_score=score,
        score=_clamp_score(score),
    )


def _rule_delta(rule: Mapping[str, object], inputs: Mapping[str, float]) -> int:
    kind = _text(rule.get("kind"), "leader rule.kind")
    if kind == "high-low-threshold":
        return _high_low_rule_delta(rule, inputs)
    if kind == "signed-volume-threshold":
        return _signed_volume_rule_delta(rule, inputs)
    if kind == "bounded-active-with-overheat":
        return _bounded_active_rule_delta(rule, inputs)
    if kind == "high-threshold":
        value = _input_number(rule, inputs)
        delta = _threshold_delta(value, rule.get("high_steps"), high=True)
        return delta if delta != 0 else _integer(rule.get("default"), "leader rule.default")
    raise MarketScanReplayError(f"未知 leader rule 算法：{kind}")


def _high_low_rule_delta(rule: Mapping[str, object], inputs: Mapping[str, float]) -> int:
    value = _input_number(rule, inputs)
    high = _threshold_delta(value, rule.get("high_steps"), high=True)
    return high if high != 0 else _threshold_delta(value, rule.get("low_steps"), high=False)


def _signed_volume_rule_delta(rule: Mapping[str, object], inputs: Mapping[str, float]) -> int:
    value = _input_number(rule, inputs)
    if value < _number(rule.get("threshold"), "leader rule.threshold"):
        return 0
    direction = inputs[_text(rule.get("direction_input"), "leader rule.direction_input")]
    if direction > 0:
        return _integer(rule.get("positive_delta"), "leader rule.positive_delta")
    if direction < 0:
        return _integer(rule.get("negative_delta"), "leader rule.negative_delta")
    return 0


def _bounded_active_rule_delta(rule: Mapping[str, object], inputs: Mapping[str, float]) -> int:
    value = _input_number(rule, inputs)
    if value == 0:
        return 0
    active_min = _number(rule.get("active_min"), "leader rule.active_min")
    active_max = _number(rule.get("active_max"), "leader rule.active_max")
    if active_min <= value <= active_max:
        return _integer(rule.get("active_delta"), "leader rule.active_delta")
    if value > _number(rule.get("overheated_above"), "leader rule.overheated_above"):
        return _integer(rule.get("overheated_delta"), "leader rule.overheated_delta")
    return 0


def _threshold_delta(value: float, raw_steps: object, *, high: bool) -> int:
    steps = _list(raw_steps, "leader rule steps")
    for index, raw_step in enumerate(steps):
        step = _list(raw_step, f"leader rule steps[{index}]")
        if len(step) != 2:
            raise MarketScanReplayError("leader rule threshold step 损坏")
        threshold = _number(step[0], "leader rule threshold")
        delta = _integer(step[1], "leader rule delta")
        if (high and value >= threshold) or (not high and value <= threshold):
            return delta
    return 0


def _input_number(
    rule: Mapping[str, object],
    inputs: Mapping[str, float],
) -> float:
    name = _text(rule.get("input"), "leader rule.input")
    try:
        return inputs[name]
    except KeyError as exc:
        raise MarketScanReplayError(f"leader rule 引用了未知输入：{name}") from exc


def _replay_final_score(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_score: int,
    *,
    final_algorithm: str,
) -> _FinalReplayBreakdown:
    if final_algorithm in {"weighted-leader-quality-v1", "weighted-trend-quality-v2"}:
        return _replay_legacy_final_score(score_spec, inputs, leader_score)
    if final_algorithm == "trend-quality-penalty-v3":
        return _replay_v4_final_score(score_spec, inputs, leader_score)
    if final_algorithm == "trend-quality-continuous-component-v4":
        return _replay_current_final_score(score_spec, inputs, leader_score)
    raise MarketScanReplayError(f"未知 final score 算法：{final_algorithm}")


def _replay_v4_final_score(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_score: int,
) -> _FinalReplayBreakdown:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    penalty_per_point = _number(
        final.get("quality_penalty_per_missing_point"),
        "final_score.quality_penalty_per_missing_point",
    )
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    metric_decimals = _integer(rounding.get("metric_decimals"), "rounding.metric_decimals")
    raw_decimals = _integer(rounding.get("raw_score_decimals"), "rounding.raw_score_decimals")
    quality_penalty = round((100 - inputs["data_quality_score"]) * penalty_per_point, metric_decimals)
    base_score = round(min(100.0, max(0.0, leader_score - quality_penalty)), metric_decimals)
    rank_refinement = _replay_rank_refinement(inputs)
    rank_discount = round(
        (1 - rank_refinement.score) * MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT,
        raw_decimals + 2,
    )
    raw_score = round(max(0.0, base_score - rank_discount), raw_decimals)
    rounded_score = round(base_score)
    return _FinalReplayBreakdown(
        raw_score=raw_score,
        final_score=_clamp_score(rounded_score),
        rounded_score=rounded_score,
        base_score=base_score,
        quality_penalty=quality_penalty,
        rank_discount=rank_discount,
        rank_refinement=rank_refinement,
    )


def _replay_current_final_score(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_score: int,
) -> _FinalReplayBreakdown:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    penalty_per_point = _number(
        final.get("quality_penalty_per_missing_point"),
        "final_score.quality_penalty_per_missing_point",
    )
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    metric_decimals = _integer(
        rounding.get("metric_decimals"),
        "rounding.metric_decimals",
    )
    raw_decimals = _integer(
        rounding.get("raw_score_decimals"),
        "rounding.raw_score_decimals",
    )
    quality_penalty = round(
        (100 - inputs["data_quality_score"]) * penalty_per_point,
        metric_decimals,
    )
    continuous_trend = _replay_rank_refinement(inputs)
    adjustment = round(
        (2 * continuous_trend.score - 1)
        * MARKET_SCAN_CONTINUOUS_TREND_MAX_ADJUSTMENT,
        raw_decimals,
    )
    base_score = round(
        min(100.0, max(0.0, leader_score - quality_penalty + adjustment)),
        metric_decimals,
    )
    raw_score = round(base_score, raw_decimals)
    rounded_score = round(base_score)
    return _FinalReplayBreakdown(
        raw_score=raw_score,
        final_score=_clamp_score(rounded_score),
        rounded_score=rounded_score,
        base_score=base_score,
        quality_penalty=quality_penalty,
        continuous_trend_adjustment=adjustment,
        rank_refinement=continuous_trend,
    )


def _replay_legacy_final_score(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_score: int,
) -> _FinalReplayBreakdown:
    final = _mapping(score_spec.get("final_score"), "score_spec.final_score")
    weights = _mapping(final.get("weights"), "final_score.weights")
    leader_weight = _number(weights.get("leader_score"), "weights.leader_score")
    quality_weight = _number(
        weights.get("data_quality_score"),
        "weights.data_quality_score",
    )
    clamp = _list(final.get("clamp"), "final_score.clamp")
    if clamp != [0, 100]:
        raise MarketScanReplayError(f"未知 final score clamp：{clamp!r}")
    raw = leader_score * leader_weight + inputs["data_quality_score"] * quality_weight
    rounding = _mapping(score_spec.get("rounding"), "score_spec.rounding")
    raw_decimals = rounding.get("raw_score_decimals")
    if raw_decimals is not None:
        raw = round(raw, _integer(raw_decimals, "rounding.raw_score_decimals"))
    rounded = round(raw)
    return _FinalReplayBreakdown(
        raw_score=raw,
        final_score=_clamp_score(rounded),
        rounded_score=rounded,
        base_score=raw,
        weighted_terms={
            "leader_score": leader_score * leader_weight,
            "data_quality_score": inputs["data_quality_score"] * quality_weight,
        },
    )


def _replay_rank_refinement(inputs: Mapping[str, float]) -> _RankRefinementReplay:
    normalized = {
        name: _bounded_linear(
            inputs[f"rank_{name}"],
            *bounds,
            decimals=MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
        )
        for name, bounds in MARKET_SCAN_RANK_REFINEMENT_BOUNDS.items()
    }
    components = {
        "ma_alignment": round(
            (
                normalized["close_vs_ma5_pct"]
                + normalized["ma5_vs_ma20_pct"]
                + normalized["ma20_vs_ma60_pct"]
            )
            / 3,
            MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
        ),
        "range_position_20d": normalized["range_position_20d"],
        "return_20d_pct": normalized["return_20d_pct"],
        "return_5d_pct": normalized["return_5d_pct"],
    }
    weighted_terms = {
        name: round(
            value * MARKET_SCAN_RANK_REFINEMENT_WEIGHTS[name],
            MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
        )
        for name, value in components.items()
    }
    score = round(sum(weighted_terms.values()), MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS)
    return _RankRefinementReplay(
        normalized_inputs=normalized,
        components=components,
        weighted_terms=weighted_terms,
        score=min(1.0, max(0.0, score)),
    )


def _bounded_linear(value: float, lower: float, upper: float, *, decimals: int) -> float:
    if upper <= lower:
        raise MarketScanReplayError("精排归一化边界无效")
    return round(min(1.0, max(0.0, (value - lower) / (upper - lower))), decimals)


def _tie_break_contract(
    score_spec: Mapping[str, object],
    payload: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    spec_ranking = _mapping(score_spec.get("ranking"), "score_spec.ranking")
    details_ranking = _mapping(payload.get("ranking"), "score_details.ranking")
    spec_tie_break = _parse_tie_break(spec_ranking.get("tie_break"))
    details_tie_break = _parse_tie_break(details_ranking.get("tie_break"))
    if spec_tie_break != details_tie_break:
        raise MarketScanReplayError("持久化 tie-break 与评分规范不一致")
    return spec_tie_break


def _parse_tie_break(value: object) -> tuple[tuple[str, str], ...]:
    entries = _list(value, "ranking.tie_break")
    parsed: list[tuple[str, str]] = []
    for index, raw_entry in enumerate(entries):
        entry = _list(raw_entry, f"ranking.tie_break[{index}]")
        if len(entry) != 2:
            raise MarketScanReplayError("tie-break 条目损坏")
        field = _text(entry[0], "tie-break field")
        direction = _text(entry[1], "tie-break direction")
        if field not in _SUPPORTED_TIE_BREAK_FIELDS or direction not in {"asc", "desc"}:
            raise MarketScanReplayError(f"未知 tie-break：{field} {direction}")
        parsed.append((field, direction))
    if not parsed or len({field for field, _direction in parsed}) != len(parsed):
        raise MarketScanReplayError("tie-break 为空或包含重复字段")
    return tuple(parsed)


def _tie_break_values(
    payload: Mapping[str, object],
    tie_break: tuple[tuple[str, str], ...],
) -> dict[str, int | float | str]:
    ranking = _mapping(payload.get("ranking"), "score_details.ranking")
    values = _mapping(ranking.get("tie_break_values"), "ranking.tie_break_values")
    result: dict[str, int | float | str] = {}
    for field, _direction in tie_break:
        value = values.get(field)
        result[field] = (
            _text(value, f"tie_break_values.{field}")
            if field == "symbol"
            else _number(
                value,
                f"tie_break_values.{field}",
            )
        )
    return result


def _verify_tie_break_values(
    values: Mapping[str, int | float | str],
    inputs: Mapping[str, float],
    raw_score: float,
    final_score: int,
) -> None:
    expected = {
        "score": final_score,
        "raw_score": raw_score,
        "trend_score": inputs["trend_score"],
        "change_pct": inputs["change_pct"],
        "amount": inputs["amount"],
    }
    for field, expected_value in expected.items():
        if field in values and not _same_number(values[field], expected_value):
            raise MarketScanReplayError(f"持久化 tie-break value 与重放输入不一致：{field}")


def _verify_persisted_components(
    payload: Mapping[str, object],
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_breakdown: _LeaderReplayBreakdown,
    final_breakdown: _FinalReplayBreakdown,
) -> None:
    components = _mapping(payload.get("components"), "score_details.components")
    leader = _mapping(components.get("leader_score"), "components.leader_score")
    final = _mapping(components.get("final_score"), "components.final_score")
    _verify_persisted_leader_component(leader, leader_breakdown)
    _verify_persisted_data_quality_component(components, inputs)
    if final_breakdown.rank_refinement is not None:
        component_name = (
            "continuous_trend"
            if final_breakdown.continuous_trend_adjustment is not None
            else "rank_refinement"
        )
        _verify_persisted_continuous_component(
            _mapping(
                components.get(component_name),
                f"components.{component_name}",
            ),
            final_breakdown.rank_refinement,
            component_name=component_name,
        )
    _verify_persisted_final_component(
        final,
        score_spec,
        inputs,
        leader_breakdown,
        final_breakdown,
    )


def _verify_persisted_leader_component(
    leader: Mapping[str, object],
    leader_breakdown: _LeaderReplayBreakdown,
) -> None:
    expected_leader = {
        "base": leader_breakdown.base,
        "trend_delta": leader_breakdown.trend_delta,
        "unclamped": leader_breakdown.unclamped_score,
        "score": leader_breakdown.score,
    }
    for field, expected_value in expected_leader.items():
        if _integer(leader.get(field), f"components.leader_score.{field}") != expected_value:
            raise MarketScanReplayError(f"持久化 leader component 与重放结果不一致：{field}")
    persisted_rule_deltas = _mapping(
        leader.get("rule_deltas"),
        "components.leader_score.rule_deltas",
    )
    parsed_rule_deltas = {
        _text(name, "components.leader_score.rule_deltas key"): _integer(
            delta,
            f"components.leader_score.rule_deltas.{name}",
        )
        for name, delta in persisted_rule_deltas.items()
    }
    if parsed_rule_deltas != leader_breakdown.rule_deltas:
        raise MarketScanReplayError("持久化 leader rule deltas 与重放结果不一致")


def _verify_persisted_data_quality_component(
    components: Mapping[str, object],
    inputs: Mapping[str, float],
) -> None:
    if not _same_number(
        components.get("data_quality_score"),
        inputs["data_quality_score"],
    ):
        raise MarketScanReplayError("持久化 data quality component 与重放输入不一致")


def _verify_persisted_final_component(
    final: Mapping[str, object],
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_breakdown: _LeaderReplayBreakdown,
    final_breakdown: _FinalReplayBreakdown,
) -> None:
    if final_breakdown.weighted_terms is not None:
        expected_weighted_terms = _expected_weighted_terms(
            score_spec,
            inputs,
            leader_breakdown,
        )
        _verify_persisted_weighted_terms(final, expected_weighted_terms)
    else:
        _verify_current_final_fields(final, final_breakdown)
    if not _same_number(final.get("raw"), final_breakdown.raw_score):
        raise MarketScanReplayError("持久化 raw score 与重放结果不一致")
    if (
        _integer(final.get("rounded"), "components.final_score.rounded")
        != final_breakdown.rounded_score
    ):
        raise MarketScanReplayError("持久化 rounded score 与重放结果不一致")
    if (
        _integer(final.get("score"), "components.final_score.score")
        != final_breakdown.final_score
    ):
        raise MarketScanReplayError("持久化 final component 与重放结果不一致")


def _verify_persisted_continuous_component(
    persisted: Mapping[str, object],
    expected: _RankRefinementReplay,
    *,
    component_name: str,
) -> None:
    for field, expected_values in (
        ("normalized_inputs", expected.normalized_inputs),
        ("components", expected.components),
        ("weighted_terms", expected.weighted_terms),
    ):
        values = _mapping(
            persisted.get(field),
            f"components.{component_name}.{field}",
        )
        if set(values) != set(expected_values) or any(
            not _same_number(values.get(name), value) for name, value in expected_values.items()
        ):
            raise MarketScanReplayError(
                f"持久化 {component_name} {field} 与重放结果不一致"
            )
    if not _same_number(persisted.get("score"), expected.score):
        raise MarketScanReplayError(
            f"持久化 {component_name} score 与重放结果不一致"
        )


def _verify_current_final_fields(
    persisted: Mapping[str, object],
    expected: _FinalReplayBreakdown,
) -> None:
    expected_values = (
        {
            "quality_penalty": expected.quality_penalty,
            "continuous_trend_adjustment": expected.continuous_trend_adjustment,
            "base": expected.base_score,
        }
        if expected.continuous_trend_adjustment is not None
        else {
            "quality_penalty": expected.quality_penalty,
            "base": expected.base_score,
            "rank_discount": expected.rank_discount,
        }
    )
    if any(value is None for value in expected_values.values()):
        raise MarketScanReplayError("当前 final score 重放缺少质量惩罚或趋势调整")
    for field, value in expected_values.items():
        assert value is not None
        if not _same_number(persisted.get(field), value):
            raise MarketScanReplayError(f"持久化 final score {field} 与重放结果不一致")


def _expected_weighted_terms(
    score_spec: Mapping[str, object],
    inputs: Mapping[str, float],
    leader_breakdown: _LeaderReplayBreakdown,
) -> dict[str, float]:
    weights = _mapping(
        _mapping(score_spec.get("final_score"), "score_spec.final_score").get("weights"),
        "final_score.weights",
    )
    return {
        "leader_score": leader_breakdown.score
        * _number(
            weights.get("leader_score"),
            "weights.leader_score",
        ),
        "data_quality_score": inputs["data_quality_score"]
        * _number(
            weights.get("data_quality_score"),
            "weights.data_quality_score",
        ),
    }


def _verify_persisted_weighted_terms(
    final: Mapping[str, object],
    expected_weighted_terms: Mapping[str, float],
) -> None:
    weighted_terms = _mapping(
        final.get("weighted_terms"),
        "components.final_score.weighted_terms",
    )
    if set(weighted_terms) != set(expected_weighted_terms) or any(
        not _same_number(weighted_terms.get(name), expected_value) for name, expected_value in expected_weighted_terms.items()
    ):
        raise MarketScanReplayError("持久化 weighted terms 与重放结果不一致")


def _compare_replays(
    left: MarketScanScoreReplay,
    right: MarketScanScoreReplay,
    tie_break: tuple[tuple[str, str], ...],
) -> int:
    for field, direction in tie_break:
        left_value = left.tie_break_values[field]
        right_value = right.tie_break_values[field]
        if left_value == right_value:
            continue
        before = _tie_break_value_before(left_value, right_value, field=field)
        if direction == "desc":
            before = not before
        return -1 if before else 1
    return 0


def _tie_break_value_before(
    left: int | float | str,
    right: int | float | str,
    *,
    field: str,
) -> bool:
    if field == "symbol":
        if not isinstance(left, str) or not isinstance(right, str):
            raise MarketScanReplayError("symbol tie-break value 类型损坏")
        return left < right
    if isinstance(left, str) or isinstance(right, str):
        raise MarketScanReplayError(f"数值 tie-break value 类型损坏：{field}")
    return left < right


def _same_number(value: object, expected: int | float) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    parsed = float(value)
    return math.isfinite(parsed) and math.isclose(parsed, float(expected), rel_tol=0.0, abs_tol=1e-9)


def _clamp_score(value: int) -> int:
    return max(0, min(100, value))


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是 JSON 对象")
    return value


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是 JSON 数组")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是非空字符串")
    return value.strip()


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是数值")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是有限数值")
    return parsed


def _optional_number(value: object, path: str) -> float:
    return 0.0 if value is None else _number(value, path)


def _integer(value: object, path: str) -> int:
    number = _number(value, path)
    if not number.is_integer():
        raise MarketScanReplayError(f"评分明细损坏：{path} 必须是整数")
    return int(number)


__all__ = [
    "MarketScanReplayError",
    "MarketScanScoreReplay",
    "rank_score_details",
    "replay_score_details",
    "stable_score_spec_hash",
    "verify_score_details",
]
