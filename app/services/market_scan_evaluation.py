from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
from statistics import fmean, median, pstdev
from typing import Literal, cast

from app.models.market import Kline, KlineAdjustmentMode
from app.db.market_scan_integrity import verify_market_scan_snapshot
from app.models.market_scan import MarketScanMode
from app.models.paper_trading import (
    CostProfileName,
    PaperCostProfile,
    PaperInstrumentMetadata,
    PaperTradeRuleProfile,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.paper_trading_rules import assess_daily_tradeability, resolve_trade_rule_profile
from app.services.market_scan_evaluation_exposure import (
    ExposureItem as _ExposureItem,
    board as _board,
    exposure_audit as _exposure_audit,
    exposure_item as _exposure_item,
    liquidity_bucket as _liquidity_bucket,
    market_regime as _market_regime,
    normalize_industry as _normalize_industry,
    quality_bucket as _quality_bucket,
    regime_overlay as _regime_overlay,
    scan_time_bucket as _scan_time_bucket,
)
from app.services.market_scan_evaluation_metrics import (
    calibration_bucket as _calibration_bucket,  # noqa: F401 - compatibility re-export
    calibration_metrics as _calibration_metrics,
    calibration_record as _calibration_record,  # noqa: F401 - compatibility re-export
)
from app.services.market_scan_evaluation_statistics import (
    benjamini_hochberg,
    moving_block_bootstrap_p_value,
)
from app.services.market_scan_shadow_scoring import (
    SHADOW_SCORE_MIN_HISTORY_ROWS,
    SHADOW_SCORE_VARIANTS,
    ShadowScoreBatch,
    ShadowScoreInput,
    ShadowScoreVariant,
    market_scan_shadow_score_spec,
    score_shadow_market,
    stable_shadow_spec_hash,
)
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from app.services.market_scan_probability_labels import (
    PROBABILITY_DEFAULT_HORIZONS,
    ProbabilityLabelConfig,
    ProbabilityLabelOutcome,
    build_probability_label_outcomes,
    probability_label_contract,
)
from app.services.market_scan_probability_research import (
    ProbabilityResearchRow,
    build_probability_research,
    probability_feature_vector,
)
from app.services.trading_calendar import next_trade_dates
from app.repositories.market_scan_mapping import decode_result_payload
from app.utils.clock import utc_now


_CALIBRATION_COMPATIBILITY_EXPORTS = (
    _calibration_bucket,
    _calibration_record,
)


EVALUATION_SCHEMA_VERSION = "market-scan-forward-evaluation-v2"
SHADOW_RECONSTRUCTION_INTEGRITY = "unverified-overwrite-cache-reconstruction"
DEFAULT_TOP_SIZES = (20, 50, 100)
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
DEFAULT_MINIMUM_SESSION_COUNT = 20
DEFAULT_MINIMUM_MULTIPLE_TEST_SESSION_COUNT = 40
DEFAULT_BOOTSTRAP_SAMPLES = 1_000
DEFAULT_EXECUTION_NOTIONAL = 100_000.0
DEFAULT_MAX_EXIT_DELAY_SESSIONS = 5
DEFAULT_MAX_DAILY_PARTICIPATION_RATE = 0.01
PROMOTION_GATE_VERSION = "full-market-shadow-promotion-gate-v2"
PROMOTION_PRIMARY_HORIZON = 5
PROMOTION_PRIMARY_TOP_N = 100
PROMOTION_MINIMUM_MEAN_RANK_IC = 0.02
PROMOTION_MINIMUM_NET_EXCESS_RETURN = 0.0
PROMOTION_MINIMUM_ITEM_COVERAGE = 0.95
PROMOTION_MAXIMUM_DRAWDOWN = -0.25
PROMOTION_MAXIMUM_HYSTERESIS_TURNOVER = 0.80
PROMOTION_MAXIMUM_EXPOSURE_SHARE_DIFFERENCE = 0.20
PROMOTION_MINIMUM_STRESS_CAPACITY_COVERAGE = 0.80
MULTIPLE_TESTING_ALPHA = 0.05
EvaluationStatus = Literal["ok", "insufficient_data"]
ExecutionStatus = Literal["modelled", "unfilled", "data_unavailable"]


@dataclass(frozen=True)
class EvaluationConfig:
    top_sizes: tuple[int, ...] = DEFAULT_TOP_SIZES
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    minimum_sample_size: int = 30
    minimum_session_count: int = DEFAULT_MINIMUM_SESSION_COUNT
    complete_day_coverage: float = 0.95
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES
    cost_profile: CostProfileName = "base"
    execution_notional: float = DEFAULT_EXECUTION_NOTIONAL
    max_exit_delay_sessions: int = DEFAULT_MAX_EXIT_DELAY_SESSIONS
    max_daily_participation_rate: float = DEFAULT_MAX_DAILY_PARTICIPATION_RATE
    hysteresis_buffer_ratio: float = 0.20

    def __post_init__(self) -> None:
        _require_positive_sequence(self.top_sizes, "top_sizes")
        _require_positive_sequence(self.horizons, "horizons")
        _require_positive(self.minimum_sample_size, "minimum_sample_size")
        _require_positive(self.minimum_session_count, "minimum_session_count")
        _require_unit_interval(self.complete_day_coverage, "complete_day_coverage")
        _require_minimum(self.bootstrap_samples, 100, "bootstrap_samples")
        _require_positive(self.execution_notional, "execution_notional")
        _require_minimum(self.max_exit_delay_sessions, 0, "max_exit_delay_sessions")
        _require_unit_interval(self.max_daily_participation_rate, "max_daily_participation_rate")
        if not 0 <= self.hysteresis_buffer_ratio <= 1:
            raise ValueError("hysteresis_buffer_ratio 必须在 [0, 1] 范围内")


def _require_positive_sequence(values: Sequence[int], label: str) -> None:
    if not values or any(value <= 0 for value in values):
        raise ValueError(f"{label} 必须是正整数")


def _require_positive(value: float, label: str) -> None:
    if value <= 0:
        raise ValueError(f"{label} 必须大于 0")


def _require_unit_interval(value: float, label: str) -> None:
    if not 0 < value <= 1:
        raise ValueError(f"{label} 必须在 (0, 1] 范围内")


def _require_minimum(value: int, minimum: int, label: str) -> None:
    if value < minimum:
        raise ValueError(f"{label} 不能小于 {minimum}")


@dataclass(frozen=True)
class _ExecutionOutcome:
    status: ExecutionStatus
    reason: str
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    exit_delay_sessions: int = 0
    model_limited: bool = False
    buy_amount: float | None = None
    sell_amount: float | None = None


@dataclass(frozen=True)
class _ExecutionEntry:
    by_date: dict[str, sqlite3.Row]
    metadata: PaperInstrumentMetadata
    entry_date: str
    entry_price: float
    quantity: int
    buy_amount: float
    buy_cost: float
    entry_model_limited: bool
    cost_profile: PaperCostProfile


@dataclass(frozen=True)
class _Observation:
    run_id: int
    quote_date: str
    mode: str
    scope: str
    rule_version: str
    symbol: str
    market: str
    board: str
    industry: str
    segment: str
    liquidity_bucket: str
    scan_time_bucket: str
    rank: int
    raw_score: float
    amount: float
    turnover_rate: float | None
    quality_bucket: str
    regime: str
    returns: dict[int, float]
    adverse: dict[int, float]
    execution: dict[int, _ExecutionOutcome]
    probability_labels: dict[int, ProbabilityLabelOutcome]
    factor_values: dict[str, float]
    source_evidence_digest: str | None


@dataclass(frozen=True)
class _RunSnapshot:
    id: int
    mode: str
    scope: str
    rule_version: str
    quote_date: str
    data_date: str
    observations: tuple[_Observation, ...]
    rankings: tuple[tuple[str, int], ...]
    eligible_dates: tuple[str, ...]
    expected_ranking_count: int = 0
    exclusions: tuple[dict[str, object], ...] = ()
    point_in_time_integrity_verified: bool = False
    exposures: tuple[_ExposureItem, ...] = ()
    regime: str = "unknown"
    reference_rankings: tuple[tuple[str, int], ...] = ()


@dataclass
class _RankDeltaAccumulator:
    deltas: list[float]
    overlap_values: dict[int, list[float]]
    candidate_count: int = 0
    reference_count: int = 0
    common_count: int = 0
    missing_candidate: int = 0
    missing_reference: int = 0


@dataclass(frozen=True)
class _PromotionAssessment:
    multiple_testing: dict[str, object]
    candidate_gates: dict[ShadowScoreVariant, dict[str, object]]
    integrity_verified: bool
    multiple_testing_ready: bool
    eligible_candidates: list[ShadowScoreVariant]
    promotable: bool


@dataclass(frozen=True)
class _RobustnessComponents:
    regime_slices: list[dict[str, object]]
    cost_scenarios: list[dict[str, object]]
    capacity_scenarios: list[dict[str, object]]
    stability: dict[str, object]


def evaluate_market_scan_rankings(
    database_path: Path,
    *,
    config: EvaluationConfig | None = None,
    mode: MarketScanMode | None = None,
    run_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    settings = config or EvaluationConfig()
    path = Path(database_path).resolve()
    with _readonly_connection(path) as conn:
        runs = _published_runs(conn, mode=mode, run_ids=run_ids)
        snapshots_list: list[_RunSnapshot] = []
        run_failures: list[dict[str, object]] = []
        for run in runs:
            try:
                snapshot = _evaluate_run(conn, run, settings)
            except Exception as exc:
                run_failures.append(_evaluation_failure(run, "production-evaluation", exc))
                continue
            if snapshot is not None:
                snapshots_list.append(snapshot)
    return _build_report(path, runs, tuple(snapshots_list), settings, run_failures=tuple(run_failures))


def evaluate_market_scan_shadow_rankings(
    database_path: Path,
    *,
    variant: ShadowScoreVariant = "v5_full",
    config: EvaluationConfig | None = None,
    mode: MarketScanMode | None = None,
    run_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    """Evaluate a candidate ranking reconstructed without changing production rows."""
    settings = config or EvaluationConfig()
    path = Path(database_path).resolve()
    with _readonly_connection(path) as conn:
        production_runs = _published_runs(conn, mode=mode, run_ids=run_ids)
        runs = _deduplicate_shadow_sessions(production_runs)
        evaluated, run_failures = _evaluate_shadow_runs(conn, runs, settings, variant)
    snapshots = tuple(item[0] for item in evaluated)
    batches = tuple(item[1] for item in evaluated)
    report = _build_report(
        path,
        runs,
        snapshots,
        settings,
        ranking_source="reconstructed-read-only-shadow-score",
        run_failures=tuple(run_failures),
    )
    report["shadow"] = _shadow_report_metadata(variant, snapshots, batches)
    rank_delta = _rank_delta_summary(snapshots)
    report["rank_delta_vs_production"] = rank_delta
    report["production_comparison"] = {"rank_delta": rank_delta}
    return report


def _evaluate_shadow_runs(
    conn: sqlite3.Connection,
    runs: Sequence[sqlite3.Row],
    settings: EvaluationConfig,
    variant: ShadowScoreVariant,
) -> tuple[tuple[tuple[_RunSnapshot, ShadowScoreBatch], ...], list[dict[str, object]]]:
    evaluated: list[tuple[_RunSnapshot, ShadowScoreBatch]] = []
    failures: list[dict[str, object]] = []
    for run in runs:
        try:
            value = _evaluate_shadow_run(conn, run, settings, variant)
        except Exception as exc:
            failures.append(_evaluation_failure(run, "shadow-evaluation", exc))
            continue
        if value is not None:
            evaluated.append(value)
    return tuple(evaluated), failures


def _shadow_report_metadata(
    variant: ShadowScoreVariant,
    snapshots: Sequence[_RunSnapshot],
    batches: Sequence[ShadowScoreBatch],
) -> dict[str, object]:
    spec = market_scan_shadow_score_spec(variant=variant)
    integrity_verified = bool(snapshots) and all(item.point_in_time_integrity_verified for item in snapshots)
    return {
        "variant": variant,
        "spec": spec,
        "spec_hash": stable_shadow_spec_hash(spec),
        "production_mutation": False,
        "input_integrity": {
            "status": (
                "verified-persisted-point-in-time-features"
                if integrity_verified
                else SHADOW_RECONSTRUCTION_INTEGRITY
            ),
            "eligible_for_promotion_evidence": integrity_verified,
            "reason": (
                "候选输入来自扫描事务内持久化且摘要校验通过的61日特征证据"
                if integrity_verified
                else "部分批次只能使用会被覆盖的 kline_daily 重建，不能证明当前历史K线就是扫描时点可见版本"
            ),
        },
        "run_evidence": [
            {
                "run_id": snapshot.id,
                "candidate_id": batch.candidate_id,
                "scored_count": len(batch.results),
                "normalization": batch.normalization,
                "ranking_digest": _ranking_digest(batch),
            }
            for snapshot, batch in zip(snapshots, batches, strict=True)
        ],
        "reconstruction_limit": (
            "使用冻结扫描报价/元数据与当前只读数据库中 date<=data_date 的前复权日K；"
            "不写回生产榜单；当前缓存重建结果仅供探索，不能作为晋级证据。"
        ),
    }


def _rank_delta_summary(snapshots: Sequence[_RunSnapshot]) -> dict[str, object]:
    aggregate = _rank_delta_aggregate(snapshots)
    absolute = [abs(value) for value in aggregate.deltas]
    overlaps = {
        top_n: fmean(values) if values else None
        for top_n, values in aggregate.overlap_values.items()
    }
    return _rank_delta_payload(
        snapshots,
        aggregate,
        absolute,
        overlaps,
        _rank_delta_unavailable_reasons(snapshots, aggregate),
    )


def _rank_delta_aggregate(snapshots: Sequence[_RunSnapshot]) -> _RankDeltaAccumulator:
    aggregate = _RankDeltaAccumulator([], {20: [], 50: [], 100: []})
    for snapshot in snapshots:
        candidate = dict(snapshot.rankings)
        reference = dict(snapshot.reference_rankings)
        aggregate.candidate_count += len(candidate)
        aggregate.reference_count += len(reference)
        common = candidate.keys() & reference.keys()
        aggregate.common_count += len(common)
        aggregate.deltas.extend(float(candidate[symbol] - reference[symbol]) for symbol in common)
        aggregate.missing_candidate += len(reference.keys() - candidate.keys())
        aggregate.missing_reference += len(candidate.keys() - reference.keys())
        _append_rank_overlaps(aggregate.overlap_values, candidate, reference)
    return aggregate


def _append_rank_overlaps(
    overlap_values: dict[int, list[float]],
    candidate: Mapping[str, int],
    reference: Mapping[str, int],
) -> None:
    for top_n, values in overlap_values.items():
        candidate_top = {symbol for symbol, rank in candidate.items() if rank <= top_n}
        reference_top = {symbol for symbol, rank in reference.items() if rank <= top_n}
        denominator = min(top_n, len(candidate_top), len(reference_top))
        if denominator:
            values.append(len(candidate_top & reference_top) / denominator)


def _rank_delta_unavailable_reasons(
    snapshots: Sequence[_RunSnapshot],
    aggregate: _RankDeltaAccumulator,
) -> list[str]:
    unavailable_reasons: list[str] = []
    if not snapshots:
        unavailable_reasons.append("no_evaluated_shadow_sessions")
    if not aggregate.reference_count:
        unavailable_reasons.append("production_reference_rankings_unavailable")
    if not aggregate.deltas:
        unavailable_reasons.append("no_common_ranked_symbols")
    return unavailable_reasons


def _rank_delta_payload(
    snapshots: Sequence[_RunSnapshot],
    aggregate: _RankDeltaAccumulator,
    absolute: Sequence[float],
    overlaps: Mapping[int, float | None],
    unavailable_reasons: list[str],
) -> dict[str, object]:
    deltas = aggregate.deltas
    return {
        "status": "ok" if deltas else "unavailable",
        "compared_run_count": len(snapshots),
        "compared_item_count": aggregate.common_count,
        "candidate_ranking_count": aggregate.candidate_count,
        "production_ranking_count": aggregate.reference_count,
        "common_symbol_count": aggregate.common_count,
        "missing_from_candidate_count": aggregate.missing_candidate,
        "missing_from_production_count": aggregate.missing_reference,
        "mean_rank_delta": fmean(deltas) if deltas else None,
        "median_rank_delta": median(deltas) if deltas else None,
        "mean_absolute_rank_delta": fmean(absolute) if absolute else None,
        "maximum_absolute_rank_delta": max(absolute) if absolute else None,
        "mean_absolute": fmean(absolute) if absolute else None,
        "max_absolute": max(absolute) if absolute else None,
        "top20_overlap": overlaps[20],
        "top50_overlap": overlaps[50],
        "top100_overlap": overlaps[100],
        "top20_overlap_ratio": overlaps[20],
        "top50_overlap_ratio": overlaps[50],
        "top100_overlap_ratio": overlaps[100],
        "unavailable_reasons": unavailable_reasons,
    }


def evaluate_market_scan_shadow_comparison(
    database_path: Path,
    *,
    config: EvaluationConfig | None = None,
    mode: MarketScanMode | None = None,
    run_ids: Sequence[int] | None = None,
    variants: Sequence[ShadowScoreVariant] = SHADOW_SCORE_VARIANTS,
) -> dict[str, object]:
    settings = config or EvaluationConfig()
    normalized_variants = tuple(dict.fromkeys(variants))
    if not normalized_variants:
        raise ValueError("至少需要一个影子评分版本")
    production = evaluate_market_scan_rankings(
        database_path,
        config=settings,
        mode=mode,
        run_ids=run_ids,
    )
    candidates = {
        variant: evaluate_market_scan_shadow_rankings(
            database_path,
            variant=variant,
            config=settings,
            mode=mode,
            run_ids=run_ids,
        )
        for variant in normalized_variants
    }
    observed_sessions = max(
        [_maximum_contract_session_count(production)]
        + [_maximum_contract_session_count(report) for report in candidates.values()]
    )
    status, promotion = _shadow_promotion_assessment(
        production,
        candidates,
        observed_sessions,
        settings.minimum_session_count,
        candidate_count=len(candidates),
    )
    return {
        "schema_version": "market-scan-shadow-comparison-v2",
        "generated_at": utc_now().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": status,
        "production": production,
        "candidates": candidates,
        "promotion": promotion,
    }


def _shadow_promotion_assessment(
    production: dict[str, object],
    candidates: Mapping[ShadowScoreVariant, dict[str, object]],
    observed_sessions: int,
    minimum_sessions: int,
    *,
    candidate_count: int,
) -> tuple[str, dict[str, object]]:
    if candidate_count != len(candidates):
        raise ValueError("candidate_count 与候选集合不一致")
    assessment = _promotion_assessment_context(
        production,
        candidates,
        minimum_sessions,
    )
    return (
        "eligible_for_human_review" if assessment.promotable else "insufficient_data",
        _promotion_assessment_payload(
            assessment,
            observed_sessions=observed_sessions,
            minimum_sessions=minimum_sessions,
        ),
    )


def _promotion_assessment_context(
    production: Mapping[str, object],
    candidates: Mapping[ShadowScoreVariant, dict[str, object]],
    minimum_sessions: int,
) -> _PromotionAssessment:
    multiple_testing = _candidate_multiple_testing_control(
        production,
        candidates,
        minimum_sessions=max(
            minimum_sessions,
            DEFAULT_MINIMUM_MULTIPLE_TEST_SESSION_COUNT,
        ),
    )
    candidate_tests = cast(dict[str, dict[str, object]], multiple_testing["candidate_results"])
    candidate_gates = {
        variant: _candidate_promotion_gate(
            report,
            minimum_sessions,
            multiple_testing_result=candidate_tests.get(variant),
        )
        for variant, report in candidates.items()
    }
    integrity_verified = all(_shadow_input_integrity_verified(report) for report in candidates.values())
    multiple_testing_ready = multiple_testing["status"] == "ok"
    eligible_candidates = [
        variant
        for variant, gate in candidate_gates.items()
        if gate["passed"] is True
    ]
    promotable = (
        production["status"] == "ok"
        and bool(eligible_candidates)
        and multiple_testing_ready
    )
    return _PromotionAssessment(
        multiple_testing=multiple_testing,
        candidate_gates=candidate_gates,
        integrity_verified=integrity_verified,
        multiple_testing_ready=multiple_testing_ready,
        eligible_candidates=eligible_candidates,
        promotable=promotable,
    )


def _promotion_assessment_payload(
    assessment: _PromotionAssessment,
    *,
    observed_sessions: int,
    minimum_sessions: int,
) -> dict[str, object]:
    return {
        "automatic_promotion": False,
        "eligible_for_human_review": assessment.promotable,
        "required_independent_session_count": minimum_sessions,
        "observed_independent_session_count": observed_sessions,
        "point_in_time_input_integrity_verified": assessment.integrity_verified,
        "gate_version": PROMOTION_GATE_VERSION,
        "eligible_candidates": assessment.eligible_candidates,
        "candidate_gates": assessment.candidate_gates,
        "multiple_testing_control": assessment.multiple_testing,
        "blocking_reasons": _shadow_promotion_blockers(
            assessment.promotable,
            observed_sessions,
            minimum_sessions,
            assessment.integrity_verified,
            assessment.multiple_testing_ready,
            bool(assessment.eligible_candidates),
        ),
        "conclusion": (
            "样本门槛已满足，仅可进入人工晋级评审；不得自动替换生产评分。"
            if assessment.promotable
            else "候选评分已实现并可持续积累影子证据，但暂不晋级生产。"
        ),
    }


def _candidate_multiple_testing_control(
    production: Mapping[str, object],
    candidates: Mapping[ShadowScoreVariant, dict[str, object]],
    *,
    minimum_sessions: int,
) -> dict[str, object]:
    production_values = _promotion_session_values(production, "net_excess_return")
    variants = list(candidates)
    evaluated = [
        _candidate_test_record(
            variant,
            candidates[variant],
            production_values,
            minimum_sessions,
        )
        for variant in variants
    ]
    candidate_records = {
        variant: record
        for variant, (record, _raw_p_value) in zip(variants, evaluated, strict=True)
    }
    raw_p_values = [raw_p_value for _record, raw_p_value in evaluated]
    _apply_candidate_multiple_test_adjustments(variants, candidate_records, raw_p_values)
    available_count = sum(value is not None for value in raw_p_values)
    status = "ok" if variants and available_count == len(variants) else "insufficient_data"
    return _candidate_multiple_testing_payload(
        candidate_records,
        candidate_count=len(variants),
        available_count=available_count,
        minimum_sessions=minimum_sessions,
        status=status,
    )


def _candidate_test_record(
    variant: ShadowScoreVariant,
    report: Mapping[str, object],
    production_values: Mapping[str, float],
    minimum_sessions: int,
) -> tuple[dict[str, object], float | None]:
    candidate_values = _promotion_session_values(report, "net_excess_return")
    shared_sessions = sorted(production_values.keys() & candidate_values.keys())
    deltas = [
        candidate_values[session] - production_values[session]
        for session in shared_sessions
    ]
    raw_p_value = moving_block_bootstrap_p_value(
        deltas,
        samples=_candidate_bootstrap_samples(report),
        block_length=PROMOTION_PRIMARY_HORIZON,
        seed_text=f"candidate-vs-production:{variant}:top100:5d-net-excess",
        minimum_count=minimum_sessions,
    )
    return (
        {
            "status": "ok" if raw_p_value is not None else "insufficient_data",
            "null_hypothesis": "candidate mean paired 5d top100 net-excess improvement <= 0",
            "alternative": "greater",
            "paired_independent_session_count": len(shared_sessions),
            "minimum_paired_independent_session_count": minimum_sessions,
            "mean_paired_net_excess_delta": fmean(deltas) if deltas else None,
            "raw_p_value_one_sided": raw_p_value,
            "adjusted_p_value": None,
            "rejected_at_alpha": None,
            "insufficient_reasons": _candidate_test_reasons(
                len(shared_sessions), minimum_sessions, raw_p_value,
            ),
        },
        raw_p_value,
    )


def _candidate_bootstrap_samples(report: Mapping[str, object]) -> int:
    config = report.get("config")
    if not isinstance(config, dict):
        return DEFAULT_BOOTSTRAP_SAMPLES
    return int(str(config.get("bootstrap_samples", DEFAULT_BOOTSTRAP_SAMPLES)))


def _candidate_test_reasons(
    session_count: int,
    minimum_sessions: int,
    raw_p_value: float | None,
) -> list[str]:
    if session_count < minimum_sessions:
        return ["minimum_paired_independent_session_count"]
    if raw_p_value is None:
        return ["session_level_test_unavailable"]
    return []


def _apply_candidate_multiple_test_adjustments(
    variants: Sequence[ShadowScoreVariant],
    candidate_records: Mapping[ShadowScoreVariant, dict[str, object]],
    raw_p_values: Sequence[float | None],
) -> None:
    adjusted, rejected = benjamini_hochberg(raw_p_values, alpha=MULTIPLE_TESTING_ALPHA)
    for variant, adjusted_value, rejected_value in zip(
        variants, adjusted, rejected, strict=True,
    ):
        candidate_records[variant]["adjusted_p_value"] = adjusted_value
        candidate_records[variant]["rejected_at_alpha"] = rejected_value


def _candidate_multiple_testing_payload(
    candidate_records: Mapping[ShadowScoreVariant, dict[str, object]],
    *,
    candidate_count: int,
    available_count: int,
    minimum_sessions: int,
    status: str,
) -> dict[str, object]:
    return {
        "status": status,
        "ready": status == "ok",
        "method": "benjamini-hochberg-fdr",
        "alpha": MULTIPLE_TESTING_ALPHA,
        "family": "preregistered-shadow-candidate-paired-5d-top100-net-excess-vs-production",
        "candidate_count": candidate_count,
        "tested_hypothesis_count": available_count,
        "minimum_paired_independent_session_count": minimum_sessions,
        "session_resampling": {
            "method": "deterministic-circular-moving-block-bootstrap-under-null",
            "block_length_sessions": PROMOTION_PRIMARY_HORIZON,
            "reason": "5日远期标签重叠，不能把相邻扫描日当作完全独立观测",
        },
        "candidate_results": candidate_records,
        "pbo": {
            "status": "not_computed",
            "value": None,
            "reason": "当前报告未执行组合切分与选择路径枚举，不得把样本数门槛称为PBO结果",
        },
        "deflated_sharpe_ratio": {
            "status": "not_computed",
            "value": None,
            "reason": "当前晋级主统计量是配对净超额差异，不伪造DSR",
        },
    }


def _promotion_session_values(
    report: Mapping[str, object],
    metric: str,
) -> dict[str, float]:
    evidence = report.get("promotion_evidence")
    if not isinstance(evidence, dict):
        return {}
    sessions = evidence.get("sessions")
    if not isinstance(sessions, list):
        return {}
    values: dict[str, float] = {}
    for item in sessions:
        if not isinstance(item, dict):
            continue
        quote_date = item.get("quote_date")
        parsed = _optional_float(item.get(metric))
        if isinstance(quote_date, str) and parsed is not None:
            values[quote_date] = parsed
    return values


def _shadow_promotion_blockers(
    promotable: bool,
    observed_sessions: int,
    minimum_sessions: int,
    integrity_verified: bool,
    multiple_testing_ready: bool,
    has_eligible_candidate: bool,
) -> list[str]:
    if promotable:
        return []
    blockers: list[str] = []
    if observed_sessions < minimum_sessions:
        blockers.append("独立交易日样本不足")
    if not integrity_verified:
        blockers.append("候选评分历史输入缺少可验证的扫描时点快照")
    if not multiple_testing_ready:
        blockers.append("候选相对生产评分的配对交易日证据不足，BH-FDR未形成可用拒绝结论")
    if not has_eligible_candidate:
        blockers.append("没有候选同时通过预注册的IC、净超额、单调性、回撤、换手与暴露门槛")
    return blockers


def _candidate_promotion_gate(
    report: dict[str, object],
    minimum_sessions: int,
    *,
    multiple_testing_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    contract = _primary_promotion_contract(report)
    criteria = _base_promotion_criteria(report)
    if contract is None:
        criteria["primary_contract"] = _promotion_criterion(None, False, "full-market top100 5d")
        criteria.update(_research_promotion_criteria(report, multiple_testing_result))
        return _promotion_gate_payload(criteria, None)
    dimensions = cast(dict[str, str], contract["dimensions"])
    criteria.update(_contract_promotion_criteria(report, contract, dimensions, minimum_sessions))
    criteria.update(_research_promotion_criteria(report, multiple_testing_result))
    return _promotion_gate_payload(criteria, dimensions)


def _base_promotion_criteria(report: Mapping[str, object]) -> dict[str, dict[str, object]]:
    integrity = _shadow_input_integrity_verified(report)
    quality = report.get("evaluation_quality")
    coverage = (
        float(quality.get("item_coverage_ratio", 0.0))
        if isinstance(quality, dict)
        else 0.0
    )
    return {
        "report_status": _promotion_criterion(report.get("status"), report.get("status") == "ok", "ok"),
        "point_in_time_integrity": _promotion_criterion(integrity, integrity, True),
        "item_coverage": _promotion_criterion(
            coverage,
            coverage >= PROMOTION_MINIMUM_ITEM_COVERAGE,
            {"minimum": PROMOTION_MINIMUM_ITEM_COVERAGE},
        ),
    }


def _research_promotion_criteria(
    report: Mapping[str, object],
    multiple_testing_result: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    test = multiple_testing_result or {}
    adjusted_p_value = _optional_float(test.get("adjusted_p_value"))
    paired_delta = _optional_float(test.get("mean_paired_net_excess_delta"))
    fdr_passed = (
        test.get("status") == "ok"
        and test.get("rejected_at_alpha") is True
        and adjusted_p_value is not None
        and paired_delta is not None
        and paired_delta > 0
    )
    robustness_value = report.get("robustness")
    robustness = robustness_value if isinstance(robustness_value, dict) else {}
    cost_scenarios = robustness.get("cost_scenarios")
    stress_cost = _named_scenario(cost_scenarios, "profile", "stress")
    stress_net_excess = _optional_float(stress_cost.get("mean_net_excess_return"))
    capacity_scenarios = robustness.get("capacity_scenarios")
    stress_capacity = _named_scenario(capacity_scenarios, "scenario", "stress")
    stress_coverage = _optional_float(stress_capacity.get("capacity_coverage_ratio"))
    return {
        "bh_fdr_primary_net_excess_improvement": _promotion_criterion(
            {
                "adjusted_p_value": adjusted_p_value,
                "mean_paired_net_excess_delta": paired_delta,
                "rejected": test.get("rejected_at_alpha"),
                "status": test.get("status", "insufficient_data"),
            },
            fdr_passed,
            {
                "alpha": MULTIPLE_TESTING_ALPHA,
                "alternative": "paired candidate-production mean delta > 0",
            },
        ),
        "regime_cost_capacity_robustness": _promotion_criterion(
            robustness.get("status"),
            robustness.get("promotion_ready") is True,
            "ok",
        ),
        "stress_cost_net_excess_5d": _promotion_criterion(
            stress_net_excess,
            stress_cost.get("status") == "ok"
            and stress_net_excess is not None
            and stress_net_excess > PROMOTION_MINIMUM_NET_EXCESS_RETURN,
            {"minimum_exclusive": PROMOTION_MINIMUM_NET_EXCESS_RETURN},
        ),
        "stress_capacity_coverage_top100": _promotion_criterion(
            stress_coverage,
            stress_capacity.get("status") == "ok"
            and stress_coverage is not None
            and stress_coverage >= PROMOTION_MINIMUM_STRESS_CAPACITY_COVERAGE,
            {"minimum": PROMOTION_MINIMUM_STRESS_CAPACITY_COVERAGE},
        ),
    }


def _named_scenario(
    value: object,
    field: str,
    expected: str,
) -> Mapping[str, object]:
    if not isinstance(value, list):
        return {}
    return next(
        (
            item
            for item in value
            if isinstance(item, dict) and item.get(field) == expected
        ),
        {},
    )


def _contract_promotion_criteria(
    report: Mapping[str, object],
    contract: Mapping[str, object],
    dimensions: Mapping[str, str],
    minimum_sessions: int,
) -> dict[str, dict[str, object]]:
    sessions = int(str(contract.get("independent_session_count", 0)))
    rank_ic = _matching_metric(report.get("rank_ic"), dimensions, PROMOTION_PRIMARY_HORIZON)
    monotonicity = _matching_metric(report.get("monotonicity"), dimensions, PROMOTION_PRIMARY_HORIZON)
    execution_value = contract.get("execution")
    execution: Mapping[str, object] = execution_value if isinstance(execution_value, dict) else {}
    mean_ic = _optional_float(rank_ic.get("mean_rank_ic")) if rank_ic else None
    net_excess = _optional_float(execution.get("average_net_excess_return"))
    drawdown = _optional_float(contract.get("session_maximum_drawdown"))
    turnover_values = _matching_hysteresis_turnover(report.get("hysteresis"), dimensions)
    exposure_values = _matching_exposure_differences(report.get("exposure_audit"), dimensions)
    return {
        "independent_sessions": _promotion_criterion(
            sessions, sessions >= minimum_sessions, {"minimum": minimum_sessions},
        ),
        "mean_rank_ic_5d": _promotion_criterion(
            mean_ic,
            mean_ic is not None and mean_ic >= PROMOTION_MINIMUM_MEAN_RANK_IC,
            {"minimum": PROMOTION_MINIMUM_MEAN_RANK_IC},
        ),
        "top100_net_excess_5d": _promotion_criterion(
            net_excess,
            net_excess is not None and net_excess > PROMOTION_MINIMUM_NET_EXCESS_RETURN,
            {"minimum_exclusive": PROMOTION_MINIMUM_NET_EXCESS_RETURN},
        ),
        "quantile_monotonicity_5d": _promotion_criterion(
            monotonicity.get("monotonic") if monotonicity else None,
            monotonicity is not None and monotonicity.get("monotonic") is True,
            True,
        ),
        "maximum_drawdown_5d": _promotion_criterion(
            drawdown, drawdown is not None and drawdown >= PROMOTION_MAXIMUM_DRAWDOWN,
            {"minimum": PROMOTION_MAXIMUM_DRAWDOWN},
        ),
        "hysteresis_turnover_top100": _promotion_criterion(
            fmean(turnover_values) if turnover_values else None,
            bool(turnover_values) and max(turnover_values) <= PROMOTION_MAXIMUM_HYSTERESIS_TURNOVER,
            {"maximum": PROMOTION_MAXIMUM_HYSTERESIS_TURNOVER},
        ),
        "board_industry_liquidity_exposure": _promotion_criterion(
            max(exposure_values) if exposure_values else None,
            bool(exposure_values) and max(exposure_values) <= PROMOTION_MAXIMUM_EXPOSURE_SHARE_DIFFERENCE,
            {"maximum_absolute_share_difference": PROMOTION_MAXIMUM_EXPOSURE_SHARE_DIFFERENCE},
        ),
    }


def _promotion_gate_payload(
    criteria: Mapping[str, Mapping[str, object]],
    contract: Mapping[str, str] | None,
) -> dict[str, object]:
    failed = [name for name, value in criteria.items() if value.get("passed") is not True]
    return {
        "gate_version": PROMOTION_GATE_VERSION,
        "primary_contract": dict(contract) if contract else None,
        "criteria": dict(criteria),
        "failed_criteria": failed,
        "passed": not failed,
        "decision": "eligible-for-human-review-only" if not failed else "remain-shadow",
    }


def _promotion_criterion(observed: object, passed: bool, threshold: object) -> dict[str, object]:
    return {"observed": observed, "threshold": threshold, "passed": bool(passed)}


def _primary_promotion_contract(report: Mapping[str, object]) -> dict[str, object] | None:
    cohorts = report.get("cohorts")
    if not isinstance(cohorts, list):
        return None
    candidates: list[dict[str, object]] = []
    for value in cohorts:
        if not isinstance(value, dict):
            continue
        dimensions = value.get("dimensions")
        if not isinstance(dimensions, dict) or len(dimensions) != 3:
            continue
        if value.get("top_n") != PROMOTION_PRIMARY_TOP_N:
            continue
        if value.get("horizon_trading_days") != PROMOTION_PRIMARY_HORIZON:
            continue
        if (
            dimensions.get("mode") == "official"
            and dimensions.get("scope") != "TOP100快速更新评分"
        ):
            candidates.append(value)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(str(item.get("independent_session_count", 0))),
            cast(dict[str, object], item["dimensions"]).get("mode") == "official",
        ),
    )


def _matching_metric(
    value: object,
    dimensions: Mapping[str, str],
    horizon: int,
) -> dict[str, object] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict) or item.get("horizon_trading_days") != horizon:
            continue
        if all(item.get(name) == expected for name, expected in dimensions.items()):
            return item
    return None


def _matching_hysteresis_turnover(
    value: object,
    dimensions: Mapping[str, str],
) -> list[float]:
    if not isinstance(value, list):
        return []
    return [
        parsed
        for item in value
        if isinstance(item, dict)
        and item.get("top_n") == PROMOTION_PRIMARY_TOP_N
        and all(item.get(name) == expected for name, expected in dimensions.items())
        and (parsed := _optional_float(item.get("hysteresis_turnover_rate"))) is not None
    ]


def _matching_exposure_differences(
    value: object,
    dimensions: Mapping[str, str],
) -> list[float]:
    if not isinstance(value, list):
        return []
    records = [
        item
        for item in value
        if isinstance(item, dict)
        and item.get("top_n") == PROMOTION_PRIMARY_TOP_N
        and item.get("rule_version") == dimensions.get("rule_version")
    ]
    differences: list[float] = []
    for record in records:
        for dimension in ("board", "industry", "liquidity"):
            groups = record.get(dimension)
            if not isinstance(groups, list):
                continue
            differences.extend(
                abs(parsed)
                for group in groups
                if isinstance(group, dict)
                and (parsed := _optional_float(group.get("share_difference"))) is not None
            )
    return differences


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _maximum_contract_session_count(report: dict[str, object]) -> int:
    cohorts = cast(list[dict[str, object]], report["cohorts"])
    return max(
        (
            int(str(item["independent_session_count"]))
            for item in cohorts
            if len(cast(dict[str, str], item["dimensions"])) == 3
        ),
        default=0,
    )


def _shadow_input_integrity_verified(report: Mapping[str, object]) -> bool:
    shadow = report.get("shadow")
    if not isinstance(shadow, dict):
        return False
    integrity = shadow.get("input_integrity")
    return isinstance(integrity, dict) and integrity.get("eligible_for_promotion_evidence") is True


def _build_report(
    path: Path,
    runs: Sequence[sqlite3.Row],
    snapshots: tuple[_RunSnapshot, ...],
    settings: EvaluationConfig,
    *,
    ranking_source: str = "persisted_market_scan_result",
    run_failures: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    generated_at = utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")
    observations = tuple(item for snapshot in snapshots for item in snapshot.observations)
    cohorts = _cohort_metrics(observations, settings)
    monotonicity = _monotonicity_metrics(observations, settings)
    deciles = _decile_metrics(observations, settings)
    rank_ic = _rank_ic_metrics(observations, settings)
    stability = _stability_metrics(snapshots, settings)
    eligible_runs = sum(bool(snapshot.observations) for snapshot in snapshots)
    status: EvaluationStatus = "ok" if any(item["status"] == "ok" for item in cohorts) else "insufficient_data"
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "config": _report_config(settings),
        "source": _report_source(path, runs, snapshots, observations, eligible_runs, ranking_source),
        "runs": [_run_summary(snapshot, settings) for snapshot in snapshots],
        "cohorts": cohorts,
        "monotonicity": monotonicity,
        "deciles": deciles,
        "rank_ic": rank_ic,
        "factor_diagnostics": _factor_diagnostics(observations, settings),
        "calibration": _calibration_metrics(observations, settings),
        "stability": stability,
        "hysteresis": _hysteresis_metrics(snapshots, settings),
        "exposure_audit": _exposure_audit(snapshots, settings),
        "regime_overlay": _regime_overlay(snapshots),
        "promotion_evidence": _primary_session_evidence(observations, settings),
        "robustness": _robustness_summary(observations, snapshots, settings),
        "evaluation_quality": _evaluation_quality(runs, snapshots, run_failures),
        "probability_research": build_probability_research(
            _probability_research_rows(snapshots),
            generated_at=generated_at,
            bootstrap_samples=settings.bootstrap_samples,
            label_contract=probability_label_contract(_probability_label_settings(settings)),
        ),
        "limitations": _report_limitations(),
    }


def _evaluation_failure(run: sqlite3.Row, stage: str, exc: Exception) -> dict[str, object]:
    return {
        "run_id": int(run["id"]),
        "stage": stage,
        "reason_code": type(exc).__name__,
        "message": " ".join(str(exc).split())[:300] or "unknown evaluation error",
    }


def _evaluation_quality(
    runs: Sequence[sqlite3.Row],
    snapshots: Sequence[_RunSnapshot],
    run_failures: Sequence[dict[str, object]],
) -> dict[str, object]:
    exclusions = [item for snapshot in snapshots for item in snapshot.exclusions]
    expected = sum(snapshot.expected_ranking_count for snapshot in snapshots)
    scored = sum(len(snapshot.rankings) for snapshot in snapshots)
    reason_counts = Counter(str(item.get("reason_code") or "unknown") for item in exclusions)
    return {
        "attempted_run_count": len(runs),
        "evaluated_run_count": len(snapshots),
        "rejected_run_count": len(run_failures),
        "expected_item_count": expected,
        "scored_item_count": scored,
        "excluded_item_count": len(exclusions),
        "item_coverage_ratio": scored / expected if expected > 0 else 0.0,
        "exclusion_reason_counts": dict(sorted(reason_counts.items())),
        "run_failures": list(run_failures),
        "item_exclusions": exclusions[:100],
        "truncated_item_exclusions": max(0, len(exclusions) - 100),
    }


def _probability_research_rows(
    snapshots: Sequence[_RunSnapshot],
) -> tuple[ProbabilityResearchRow, ...]:
    rows: list[ProbabilityResearchRow] = []
    for snapshot in snapshots:
        market_strength, board_strength, industry_strength = _probability_strength_context(snapshot.observations)
        mature = frozenset(
            horizon
            for horizon in PROBABILITY_DEFAULT_HORIZONS
            if horizon < len(snapshot.eligible_dates)
        )
        rows.extend(
            ProbabilityResearchRow(
                run_id=item.run_id,
                symbol=item.symbol,
                session_date=item.quote_date,
                features=probability_feature_vector(
                    item.factor_values,
                    market=item.market,
                    board=item.board,
                    liquidity=item.liquidity_bucket,
                    regime=item.regime,
                    industry=item.industry,
                    segment=item.segment,
                    market_strength=market_strength,
                    board_relative_strength=board_strength.get(item.board, 0.0),
                    industry_relative_strength=industry_strength.get(item.industry, 0.0),
                ),
                labels=item.probability_labels,
                mature_horizons=mature,
                dimensions=_probability_dimensions(item),
                source_evidence_digest=item.source_evidence_digest,
                mode=item.mode,
                scope=item.scope,
                rule_version=item.rule_version,
            )
            for item in snapshot.observations
        )
    return tuple(rows)


def _probability_strength_context(
    observations: Sequence[_Observation],
) -> tuple[float, dict[str, float], dict[str, float]]:
    market_strength = fmean(item.raw_score for item in observations) if observations else 50.0
    return (
        market_strength,
        _relative_group_strength(observations, "board", market_strength),
        _relative_group_strength(observations, "industry", market_strength),
    )


def _relative_group_strength(
    observations: Sequence[_Observation], attribute: Literal["board", "industry"], market_strength: float,
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in observations:
        grouped[str(getattr(item, attribute))].append(item.raw_score)
    return {
        key: fmean(values) - market_strength
        for key, values in sorted(grouped.items())
        if values
    }


def _probability_dimensions(item: _Observation) -> dict[str, str]:
    return {
        "mode": item.mode,
        "scope": item.scope,
        "rule_version": item.rule_version,
        "market": item.market,
        "board": item.board,
        "industry": item.industry,
        "liquidity": item.liquidity_bucket,
        "regime": item.regime,
        "segment": item.segment,
    }


def _primary_observation_contract(
    observations: Sequence[_Observation],
) -> tuple[dict[str, str], tuple[_Observation, ...]] | None:
    grouped: dict[tuple[str, str, str], list[_Observation]] = defaultdict(list)
    for item in observations:
        if item.mode == "official" and item.scope != "TOP100快速更新评分":
            grouped[(item.mode, item.scope, item.rule_version)].append(item)
    if not grouped:
        return None
    key, values = max(
        grouped.items(),
        key=lambda item: (
            len({row.quote_date for row in item[1]}),
            len(item[1]),
            item[0],
        ),
    )
    return {"mode": key[0], "scope": key[1], "rule_version": key[2]}, tuple(values)


def _primary_session_evidence(
    observations: Sequence[_Observation],
    config: EvaluationConfig,
) -> dict[str, object]:
    selected = _primary_observation_contract(observations)
    if selected is None:
        return {
            "status": "insufficient_data",
            "dimensions": None,
            "top_n": PROMOTION_PRIMARY_TOP_N,
            "horizon_trading_days": PROMOTION_PRIMARY_HORIZON,
            "sessions": [],
            "insufficient_reasons": ["official_full_market_contract_unavailable"],
        }
    dimensions, rows = selected
    sessions = _session_research_records(
        rows,
        top_n=PROMOTION_PRIMARY_TOP_N,
        horizon=PROMOTION_PRIMARY_HORIZON,
    )
    complete = [
        item
        for item in sessions
        if item["rank_ic"] is not None and item["net_excess_return"] is not None
    ]
    reasons: list[str] = []
    if PROMOTION_PRIMARY_HORIZON not in config.horizons:
        reasons.append("primary_horizon_not_requested")
    if len(complete) < config.minimum_session_count:
        reasons.append("minimum_session_count")
    return {
        "status": "ok" if not reasons else "insufficient_data",
        "dimensions": dimensions,
        "top_n": PROMOTION_PRIMARY_TOP_N,
        "horizon_trading_days": PROMOTION_PRIMARY_HORIZON,
        "independent_session_count": len(complete),
        "sessions": sessions,
        "insufficient_reasons": reasons,
        "semantics": (
            "one-record-per-scan-session; rank IC uses the session cross-section; "
            "net excess uses next-complete-session-open,T+1,next-sellable-open"
        ),
    }


def _session_research_records(
    rows: Sequence[_Observation],
    *,
    top_n: int,
    horizon: int,
    cost_profile: CostProfileName | None = None,
) -> list[dict[str, object]]:
    grouped: dict[str, list[_Observation]] = defaultdict(list)
    for item in rows:
        grouped[item.quote_date].append(item)
    return [
        _session_research_record(
            quote_date,
            session_rows,
            top_n=top_n,
            horizon=horizon,
            cost_profile=cost_profile,
        )
        for quote_date, session_rows in sorted(grouped.items())
    ]


def _session_research_record(
    quote_date: str,
    session_rows: Sequence[_Observation],
    *,
    top_n: int,
    horizon: int,
    cost_profile: CostProfileName | None,
) -> dict[str, object]:
    forward_rows = [item for item in session_rows if horizon in item.returns]
    benchmark = fmean(item.returns[horizon] for item in forward_rows) if forward_rows else None
    rank_ic = _spearman(
        [(item.raw_score, item.returns[horizon]) for item in forward_rows]
    )
    modelled = _session_modelled_returns(
        session_rows,
        top_n=top_n,
        horizon=horizon,
        cost_profile=cost_profile,
    )
    net_return = fmean(modelled) if modelled else None
    return {
        "quote_date": quote_date,
        "rank_ic": rank_ic,
        "net_return": net_return,
        "net_excess_return": (
            net_return - benchmark
            if net_return is not None and benchmark is not None
            else None
        ),
        "modelled_top_n_count": len(modelled),
        "cross_section_count": len(forward_rows),
    }


def _session_modelled_returns(
    session_rows: Sequence[_Observation],
    *,
    top_n: int,
    horizon: int,
    cost_profile: CostProfileName | None,
) -> list[float]:
    modelled: list[float] = []
    for item in session_rows:
        if item.rank > top_n:
            continue
        outcome = item.execution.get(horizon)
        if outcome is None or outcome.status != "modelled":
            continue
        value = _execution_scenario_value(outcome, cost_profile)
        if value is not None:
            modelled.append(value)
    return modelled


def _execution_scenario_value(
    outcome: _ExecutionOutcome,
    cost_profile: CostProfileName | None,
) -> float | None:
    if cost_profile is None:
        return outcome.net_return
    return _scenario_net_return(outcome, cost_profile)


def _scenario_net_return(
    outcome: _ExecutionOutcome,
    cost_profile: CostProfileName,
) -> float | None:
    if outcome.buy_amount is None or outcome.sell_amount is None:
        return None
    profile = resolve_cost_profile(cost_profile)
    buy_cost = trade_costs(profile, side="buy", gross_amount=outcome.buy_amount).total
    sell_cost = trade_costs(profile, side="sell", gross_amount=outcome.sell_amount).total
    return (
        outcome.sell_amount - sell_cost - outcome.buy_amount - buy_cost
    ) / (outcome.buy_amount + buy_cost)


def _robustness_summary(
    observations: Sequence[_Observation],
    snapshots: Sequence[_RunSnapshot],
    config: EvaluationConfig,
) -> dict[str, object]:
    selected = _primary_observation_contract(observations)
    if selected is None:
        return _missing_robustness_summary()
    dimensions, rows = selected
    components = _build_robustness_components(rows, snapshots, dimensions, config)
    reasons = _robustness_reasons(components)
    return _robustness_payload(dimensions, components, reasons)


def _missing_robustness_summary() -> dict[str, object]:
    return {
        "status": "insufficient_data",
        "promotion_ready": False,
        "insufficient_reasons": ["official_full_market_contract_unavailable"],
        "regime_slices": [],
        "cost_scenarios": [],
        "capacity_scenarios": [],
        "stability": {"status": "insufficient_data"},
    }


def _build_robustness_components(
    rows: Sequence[_Observation],
    snapshots: Sequence[_RunSnapshot],
    dimensions: Mapping[str, str],
    config: EvaluationConfig,
) -> _RobustnessComponents:
    profiles = cast(tuple[CostProfileName, ...], ("base", "conservative", "stress"))
    return _RobustnessComponents(
        regime_slices=[
            _regime_robustness_slice(rows, regime, config)
            for regime in ("strong", "neutral", "weak")
        ],
        cost_scenarios=[
            _cost_robustness_scenario(rows, profile, config)
            for profile in profiles
        ],
        capacity_scenarios=_capacity_robustness_scenarios(rows, config),
        stability=_primary_stability_summary(snapshots, dimensions, config),
    )


def _robustness_reasons(components: _RobustnessComponents) -> list[str]:
    observed_regimes = [
        item for item in components.regime_slices if item["observation_count"]
    ]
    reasons: list[str] = []
    if not observed_regimes or not all(item["status"] == "ok" for item in observed_regimes):
        reasons.append("regime_session_coverage_insufficient")
    if not all(item["status"] == "ok" for item in components.cost_scenarios):
        reasons.append("cost_scenario_evidence_insufficient")
    if not all(item["status"] == "ok" for item in components.capacity_scenarios):
        reasons.append("capacity_scenario_evidence_insufficient")
    if components.stability["status"] != "ok":
        reasons.append("ranking_stability_sessions_insufficient")
    return reasons


def _robustness_payload(
    dimensions: Mapping[str, str],
    components: _RobustnessComponents,
    reasons: list[str],
) -> dict[str, object]:
    return {
        "status": "ok" if not reasons else "insufficient_data",
        "promotion_ready": not reasons,
        "dimensions": dict(dimensions),
        "regime_slices": components.regime_slices,
        "cost_scenarios": components.cost_scenarios,
        "capacity_scenarios": components.capacity_scenarios,
        "stability": components.stability,
        "insufficient_reasons": reasons,
        "constraints": {
            "point_in_time": True,
            "fixed_session": True,
            "entry": "next-complete-session-open",
            "exit": "T+1-next-sellable-open",
            "capacity_semantics": "scan-day-amount-screen-only; no order-book queue reconstruction",
        },
    }


def _regime_robustness_slice(
    rows: Sequence[_Observation],
    regime: str,
    config: EvaluationConfig,
) -> dict[str, object]:
    selected = [item for item in rows if item.regime == regime]
    sessions = _session_research_records(
        selected,
        top_n=PROMOTION_PRIMARY_TOP_N,
        horizon=PROMOTION_PRIMARY_HORIZON,
    )
    rank_ics = [
        parsed
        for item in sessions
        if (parsed := _optional_float(item.get("rank_ic"))) is not None
    ]
    net_excess = [
        parsed
        for item in sessions
        if (parsed := _optional_float(item.get("net_excess_return"))) is not None
    ]
    independent = len({item.quote_date for item in selected})
    return {
        "regime": regime,
        "status": "ok" if independent >= config.minimum_session_count else "insufficient_data",
        "observation_count": len(selected),
        "independent_session_count": independent,
        "mean_rank_ic": fmean(rank_ics) if rank_ics else None,
        "mean_net_excess_return": fmean(net_excess) if net_excess else None,
        "positive_net_excess_session_rate": (
            sum(value > 0 for value in net_excess) / len(net_excess)
            if net_excess
            else None
        ),
    }


def _cost_robustness_scenario(
    rows: Sequence[_Observation],
    profile: CostProfileName,
    config: EvaluationConfig,
) -> dict[str, object]:
    sessions = _session_research_records(
        rows,
        top_n=PROMOTION_PRIMARY_TOP_N,
        horizon=PROMOTION_PRIMARY_HORIZON,
        cost_profile=profile,
    )
    values = [
        parsed
        for item in sessions
        if (parsed := _optional_float(item.get("net_excess_return"))) is not None
    ]
    return {
        "profile": profile,
        "profile_contract": resolve_cost_profile(profile).model_dump(mode="json"),
        "status": "ok" if len(values) >= config.minimum_session_count else "insufficient_data",
        "independent_session_count": len(values),
        "mean_net_excess_return": fmean(values) if values else None,
        "positive_session_rate": (
            sum(value > 0 for value in values) / len(values) if values else None
        ),
    }


def _capacity_robustness_scenarios(
    rows: Sequence[_Observation],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    selected = [item for item in rows if item.rank <= PROMOTION_PRIMARY_TOP_N]
    definitions = (
        ("base", config.execution_notional, config.max_daily_participation_rate),
        (
            "conservative",
            config.execution_notional * 5,
            config.max_daily_participation_rate / 2,
        ),
        (
            "stress",
            config.execution_notional * 10,
            config.max_daily_participation_rate / 4,
        ),
    )
    session_count = len({item.quote_date for item in selected})
    records: list[dict[str, object]] = []
    for name, notional, participation in definitions:
        eligible = [
            item
            for item in selected
            if item.amount > 0 and notional / item.amount <= participation
        ]
        records.append(
            {
                "scenario": name,
                "execution_notional": notional,
                "maximum_daily_participation_rate": participation,
                "status": (
                    "ok" if selected and session_count >= config.minimum_session_count
                    else "insufficient_data"
                ),
                "independent_session_count": session_count,
                "selected_observation_count": len(selected),
                "capacity_eligible_count": len(eligible),
                "capacity_coverage_ratio": len(eligible) / len(selected) if selected else None,
            }
        )
    return records


def _primary_stability_summary(
    snapshots: Sequence[_RunSnapshot],
    dimensions: Mapping[str, str],
    config: EvaluationConfig,
) -> dict[str, object]:
    selected = tuple(
        item
        for item in snapshots
        if item.mode == dimensions["mode"]
        and item.scope == dimensions["scope"]
        and item.rule_version == dimensions["rule_version"]
    )
    records = [
        item
        for item in _stability_metrics(selected, config)
        if item["top_n"] == PROMOTION_PRIMARY_TOP_N
    ]
    turnovers = [
        parsed
        for item in records
        if (parsed := _optional_float(item.get("turnover_rate"))) is not None
    ]
    rank_stability = [
        parsed
        for item in records
        if (parsed := _optional_float(item.get("rank_stability"))) is not None
    ]
    return {
        "status": "ok" if len(records) >= config.minimum_session_count - 1 else "insufficient_data",
        "transition_count": len(records),
        "mean_turnover_rate": fmean(turnovers) if turnovers else None,
        "maximum_turnover_rate": max(turnovers) if turnovers else None,
        "mean_spearman_rank_stability": fmean(rank_stability) if rank_stability else None,
    }


def _report_config(settings: EvaluationConfig) -> dict[str, object]:
    return {
        "top_sizes": list(settings.top_sizes),
        "horizons": list(settings.horizons),
        "minimum_sample_size": settings.minimum_sample_size,
        "minimum_session_count": settings.minimum_session_count,
        "complete_day_coverage": settings.complete_day_coverage,
        "bootstrap_samples": settings.bootstrap_samples,
        "execution_notional": settings.execution_notional,
        "max_exit_delay_sessions": settings.max_exit_delay_sessions,
        "max_daily_participation_rate": settings.max_daily_participation_rate,
        "hysteresis_buffer_ratio": settings.hysteresis_buffer_ratio,
        "cost_profile": resolve_cost_profile(settings.cost_profile).model_dump(mode="json"),
    }


def _report_source(
    path: Path,
    runs: Sequence[sqlite3.Row],
    snapshots: Sequence[_RunSnapshot],
    observations: Sequence[_Observation],
    eligible_runs: int,
    ranking_source: str,
) -> dict[str, object]:
    return {
        "database": _portable_database_label(path),
        "published_run_count": len(runs),
        "eligible_run_count": eligible_runs,
        "independent_session_count": len({item.quote_date for item in snapshots if item.observations}),
        "observation_count": len(observations),
        "read_only": True,
        "ranking_source": ranking_source,
        "forward_price_source": "persisted_qfq_kline_daily",
        "execution_model": "next-complete-session-open,T+1,next-sellable-open",
    }


def _report_limitations() -> list[str]:
    return [
        "只读取已发布批次及之后实际持久化的完整交易日数据，不重算历史生产排名。",
        "收益、IC与单调性 cohort 按 mode、scope、rule_version 隔离，不跨规则版本汇总。",
        "同一 mode、scope、rule_version、quote_date 只保留最后发布的快照，避免重复扫描放大样本。",
        "充分性同时要求股票观察数和独立交易日数；同一天的多只股票不视为独立时间样本。",
        "置信区间先按扫描交易日聚合，再以交易日为区块进行确定性 bootstrap。",
        "净收益是固定名义本金、下一完整交易日开盘入场、T+1后目标日或下一可卖日开盘退出的日K情景。",
        "日K无法复原盘口排队与盘中先后顺序，model_limited 与 unfilled 状态必须保留。",
        "市场环境由扫描快照当日全市场涨跌幅均值分层，不使用未来信息。",
        "生产排名的板块、行业和流动性暴露只做审计；v5.4仅对质量可接受的具体行业组做收缩残差化。",
        "迟滞换仓仅报告 buy/hold 阈值下的估算换手变化，不改写冻结排名。",
        "多候选比较使用独立交易日的配对净超额差异并实际执行BH-FDR；PBO与DSR未计算时明确标记不可用。",
        "报告不会自动修改生产评分权重；规则调整必须创建新的 rule_version。",
    ]


def _portable_database_label(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def _published_runs(
    conn: sqlite3.Connection,
    *,
    mode: str | None,
    run_ids: Sequence[int] | None,
) -> list[sqlite3.Row]:
    clauses = ["status IN ('success', 'degraded')"]
    parameters: list[object] = []
    if mode is not None:
        clauses.append("mode = ?")
        parameters.append(mode)
    normalized_ids = tuple(dict.fromkeys(int(value) for value in run_ids or () if int(value) > 0))
    if run_ids is not None:
        if not normalized_ids:
            return []
        clauses.append(f"id IN ({','.join('?' for _value in normalized_ids)})")
        parameters.extend(normalized_ids)
    rows = conn.execute(
        f"""
        SELECT id, mode, scope, rule_version, quote_date, data_date, as_of
        FROM market_scan_run
        WHERE {' AND '.join(clauses)}
        ORDER BY data_date ASC, as_of ASC, id ASC
        """,
        parameters,
    ).fetchall()
    by_session: dict[tuple[str, str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["mode"] or "official"),
            str(row["scope"]),
            str(row["rule_version"]),
            str(row["quote_date"] or row["data_date"]),
        )
        by_session[key] = row
    return list(by_session.values())


def _deduplicate_shadow_sessions(runs: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    by_session: dict[tuple[str, str, str], sqlite3.Row] = {}
    for run in runs:
        key = (
            str(run["mode"] or "official"),
            str(run["scope"]),
            str(run["quote_date"] or run["data_date"]),
        )
        by_session[key] = run
    return list(by_session.values())


def _evaluate_shadow_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    config: EvaluationConfig,
    variant: ShadowScoreVariant,
) -> tuple[_RunSnapshot, ShadowScoreBatch] | None:
    verify_market_scan_snapshot(conn, int(run["id"]))
    result_rows = _shadow_result_rows(conn, int(run["id"]))
    if not result_rows:
        return None
    data_date = str(run["data_date"])
    quote_date = str(run["quote_date"] or run["data_date"])
    history = _shadow_history_bars(conn, int(run["id"]), data_date)
    inputs, exclusions, evidence_count = _shadow_score_inputs(
        result_rows,
        history,
        quote_date,
        data_date,
        variant,
        mode=cast(MarketScanMode, str(run["mode"] or "official")),
        run_id=int(run["id"]),
    )
    if not inputs:
        return None
    batch = score_shadow_market(inputs, variant=variant)
    forward = _forward_bars(conn, int(run["id"]), data_date)
    eligible_dates = _eligible_trading_dates(forward, len(batch.results), quote_date, config)
    snapshot = _shadow_snapshot(
        run,
        result_rows,
        batch,
        forward,
        eligible_dates,
        quote_date,
        data_date,
        config,
        expected_ranking_count=len(result_rows),
        exclusions=exclusions,
        point_in_time_integrity_verified=(
            evidence_count == len(result_rows) and not exclusions
        ),
    )
    return snapshot, batch


def _shadow_result_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT symbol, market, industry, rank, score, raw_score, price, change_pct, data_quality_score,
               amount, turnover_rate, volume_ratio, list_date, is_st, is_new,
               quote_timestamp, quote_fallback_used, kline_fallback_used, metadata_degraded,
               COALESCE(NULLIF(adjustment_mode, ''), 'qfq') AS adjustment_mode,
               metrics_json
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL AND price > 0
        ORDER BY rank ASC, symbol ASC
        """,
        (run_id,),
    ).fetchall()


def _shadow_score_inputs(
    result_rows: Sequence[sqlite3.Row],
    history: dict[str, tuple[Kline, ...]],
    quote_date: str,
    data_date: str,
    variant: ShadowScoreVariant,
    *,
    mode: MarketScanMode,
    run_id: int,
) -> tuple[list[ShadowScoreInput], tuple[dict[str, object], ...], int]:
    inputs: list[ShadowScoreInput] = []
    exclusions: list[dict[str, object]] = []
    evidence_count = 0
    for row in result_rows:
        symbol = str(row["symbol"])
        evidence = _persisted_shadow_history(row, symbol, data_date)
        evidence_rows = evidence[0] if evidence is not None else None
        rows = evidence_rows or history.get(symbol, ())
        if len(rows) < SHADOW_SCORE_MIN_HISTORY_ROWS:
            exclusions.append(
                _item_exclusion(run_id, symbol, "insufficient_history", f"仅有{len(rows)}根可用日K")
            )
            continue
        candidate = _shadow_score_input(row, rows, quote_date, data_date, mode=mode)
        try:
            score_shadow_market((candidate,), variant=variant)
        except (TypeError, ValueError) as exc:
            exclusions.append(_item_exclusion(run_id, symbol, "invalid_shadow_input", str(exc)))
            continue
        inputs.append(candidate)
        evidence_count += bool(
            evidence is not None
            and _point_in_time_payload_attests_shadow_input(
                evidence[1], row, quote_date=quote_date, data_date=data_date, mode=mode,
            )
        )
    return inputs, tuple(exclusions), evidence_count


def _persisted_shadow_history(
    row: sqlite3.Row,
    symbol: str,
    data_date: str,
) -> tuple[tuple[Kline, ...], dict[str, object]] | None:
    _metrics, details = decode_result_payload(row["metrics_json"])
    payload = _verified_point_in_time_payload(details)
    if payload is None:
        return None
    if payload.get("symbol") != symbol or payload.get("data_date") != data_date:
        return None
    quote_price = payload.get("quote_price")
    if isinstance(quote_price, bool) or not isinstance(quote_price, int | float):
        return None
    try:
        if not math.isclose(float(quote_price), float(row["price"]), rel_tol=0, abs_tol=1e-8):
            return None
    except (TypeError, ValueError):
        return None
    contracts = payload.get("bar_contract_61")
    if not isinstance(contracts, list):
        return None
    rows: list[Kline] = []
    try:
        for item in contracts:
            rows.append(_kline_from_evidence_contract(item))
    except (TypeError, ValueError):
        return None
    return tuple(rows), payload


def _point_in_time_payload_attests_shadow_input(
    payload: Mapping[str, object],
    row: sqlite3.Row,
    *,
    quote_date: str,
    data_date: str,
    mode: MarketScanMode,
) -> bool:
    text_fields = {
        "symbol": row["symbol"],
        "market": row["market"],
        "quote_date": quote_date,
        "data_date": data_date,
        "mode": mode,
        "quote_timestamp": row["quote_timestamp"],
    }
    if any(str(payload.get(name) or "") != str(expected or "") for name, expected in text_fields.items()):
        return False
    optional_text = {"industry": row["industry"], "list_date": row["list_date"]}
    if any(
        str(payload.get(name) or "").strip() != str(expected or "").strip()
        for name, expected in optional_text.items()
    ):
        return False
    numeric_fields = {
        "quote_price": (row["price"], 1e-8),
        "quote_change_pct": (row["change_pct"], 1e-8),
        "quote_turnover_rate": (row["turnover_rate"], 1e-8),
        "quote_amount": (row["amount"], 1e-4),
        "reported_volume_ratio": (row["volume_ratio"], 1e-8),
        "data_quality_score": (row["data_quality_score"], 0.0),
    }
    if any(
        not _attested_number_matches(payload.get(name), expected, tolerance)
        for name, (expected, tolerance) in numeric_fields.items()
    ):
        return False
    boolean_fields = {
        "is_st": row["is_st"],
        "is_new": row["is_new"],
        "quote_fallback_used": row["quote_fallback_used"],
        "kline_fallback_used": row["kline_fallback_used"],
        "metadata_degraded": row["metadata_degraded"],
    }
    return all(
        isinstance(payload.get(name), bool) and payload[name] is bool(expected)
        for name, expected in boolean_fields.items()
    )


def _attested_number_matches(value: object, expected: object, tolerance: float) -> bool:
    if value is None or expected is None or isinstance(value, bool) or isinstance(expected, bool):
        return value is None and expected is None
    if not isinstance(value, (int, float, str)) or not isinstance(expected, (int, float, str)):
        return False
    try:
        left, right = float(value), float(expected)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left) and math.isfinite(right) and math.isclose(
        left, right, rel_tol=0, abs_tol=tolerance,
    )


def _verified_point_in_time_payload(details: Mapping[str, object]) -> dict[str, object] | None:
    components = details.get("components")
    if not isinstance(components, dict):
        return None
    dimensions = components.get("score_dimensions")
    if not isinstance(dimensions, dict):
        return None
    evidence = dimensions.get("point_in_time_evidence")
    if not isinstance(evidence, dict) or not verify_market_scan_point_in_time_evidence(evidence):
        return None
    payload = evidence.get("payload")
    return payload if isinstance(payload, dict) else None


def _kline_from_evidence_contract(value: object) -> Kline:
    if not isinstance(value, list) or len(value) != 9:
        raise ValueError("invalid persisted bar contract")
    return Kline(
        date=str(value[0]),
        open=float(value[1]),
        close=float(value[2]),
        high=float(value[3]),
        low=float(value[4]),
        volume=float(value[5]),
        adjustment_mode=cast(KlineAdjustmentMode, str(value[6])),
        data_version=str(value[7]),
        contract_version=str(value[8]),
        source="persisted-market-scan-point-in-time-evidence",
        from_cache=True,
    )


def _item_exclusion(run_id: int, symbol: str, reason_code: str, message: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "symbol": symbol,
        "reason_code": reason_code,
        "message": " ".join(message.split())[:240],
    }


def _shadow_score_input(
    row: sqlite3.Row,
    rows: tuple[Kline, ...],
    quote_date: str,
    data_date: str,
    *,
    mode: MarketScanMode,
) -> ShadowScoreInput:
    return ShadowScoreInput(
        symbol=str(row["symbol"]), market=str(row["market"]), quote_date=quote_date,
        data_date=data_date, price=float(row["price"]), change_pct=float(row["change_pct"] or 0),
        turnover_rate=float(row["turnover_rate"]) if row["turnover_rate"] is not None else None,
        amount=float(row["amount"] or 0), volume_ratio=float(row["volume_ratio"] or 1),
        data_quality_score=int(row["data_quality_score"] or 0), rows=rows,
        list_date=str(row["list_date"]) if row["list_date"] else None,
        is_st=bool(row["is_st"]), is_new=bool(row["is_new"]),
        quote_fallback_used=bool(row["quote_fallback_used"]),
        kline_fallback_used=bool(row["kline_fallback_used"]), metadata_degraded=bool(row["metadata_degraded"]),
        mode=mode,
        industry=str(row["industry"]) if row["industry"] else None,
    )


def _shadow_snapshot(
    run: sqlite3.Row,
    result_rows: Sequence[sqlite3.Row],
    batch: ShadowScoreBatch,
    forward: dict[str, tuple[sqlite3.Row, ...]],
    eligible_dates: tuple[str, ...],
    quote_date: str,
    data_date: str,
    config: EvaluationConfig,
    *,
    expected_ranking_count: int,
    exclusions: tuple[dict[str, object], ...],
    point_in_time_integrity_verified: bool,
) -> _RunSnapshot:
    shadow_run = _shadow_run_proxy(run, batch.candidate_id, quote_date, data_date)
    rows_by_symbol = {str(row["symbol"]): row for row in result_rows}
    observations = _shadow_observations(
        shadow_run, rows_by_symbol, batch, forward, eligible_dates, _market_regime(result_rows), config,
    )
    return _RunSnapshot(
        id=int(run["id"]),
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=batch.candidate_id,
        quote_date=quote_date,
        data_date=data_date,
        observations=observations,
        rankings=tuple((item.symbol, item.rank) for item in batch.results),
        eligible_dates=eligible_dates,
        expected_ranking_count=expected_ranking_count,
        exclusions=exclusions,
        point_in_time_integrity_verified=point_in_time_integrity_verified,
        exposures=tuple(
            _exposure_item(rows_by_symbol[item.symbol], rank=item.rank)
            for item in batch.results
        ),
        regime=_market_regime(result_rows),
        reference_rankings=tuple(
            (str(row["symbol"]), int(row["rank"]))
            for row in result_rows
        ),
    )


def _shadow_run_proxy(
    run: sqlite3.Row,
    candidate_id: str,
    quote_date: str,
    data_date: str,
) -> sqlite3.Row:
    value = {
        "id": run["id"], "mode": run["mode"], "scope": run["scope"],
        "rule_version": candidate_id, "quote_date": quote_date, "data_date": data_date,
        "as_of": run["as_of"],
    }
    return cast(sqlite3.Row, value)


def _shadow_observations(
    shadow_run: sqlite3.Row,
    rows_by_symbol: dict[str, sqlite3.Row],
    batch: ShadowScoreBatch,
    forward: dict[str, tuple[sqlite3.Row, ...]],
    eligible_dates: tuple[str, ...],
    regime: str,
    config: EvaluationConfig,
) -> tuple[_Observation, ...]:
    observations: list[_Observation] = []
    for scored in batch.results:
        persisted = rows_by_symbol[scored.symbol]
        proxy = {key: persisted[key] for key in persisted.keys()}
        proxy.update(rank=scored.rank, raw_score=scored.raw_score)
        observation = _observation_from_rows(
            shadow_run, cast(sqlite3.Row, proxy), forward.get(scored.symbol, ()),
            eligible_dates, regime, config,
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def _shadow_history_bars(
    conn: sqlite3.Connection,
    run_id: int,
    data_date: str,
) -> dict[str, tuple[Kline, ...]]:
    rows = conn.execute(
        """
        SELECT k.symbol, k.date, k.open, k.close, k.high, k.low, k.volume,
               k.adjustment_mode, k.as_of, k.data_version, k.contract_version,
               k.source, k.fetched_at, k.fallback_used
        FROM kline_daily AS k
        JOIN market_scan_result AS r
          ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
        WHERE k.date <= ? AND k.adjustment_mode = 'qfq'
        ORDER BY k.symbol ASC, k.date ASC
        """,
        (run_id, data_date),
    ).fetchall()
    grouped: dict[str, list[Kline]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(_to_kline(row))
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _ranking_digest(batch: ShadowScoreBatch) -> str:
    payload = [[item.symbol, item.rank, item.raw_score] for item in batch.results]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _evaluate_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    config: EvaluationConfig,
) -> _RunSnapshot | None:
    verify_market_scan_snapshot(conn, int(run["id"]))
    result_rows = conn.execute(
        """
        SELECT symbol, market, industry, rank, score, raw_score, price, change_pct, data_quality_score,
               amount, turnover_rate, volume_ratio, list_date, is_st, is_new,
               COALESCE(NULLIF(adjustment_mode, ''), 'qfq') AS adjustment_mode,
               metrics_json
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL AND price > 0
        ORDER BY rank ASC, symbol ASC
        """,
        (run["id"],),
    ).fetchall()
    if not result_rows:
        return None
    quote_date = str(run["quote_date"] or run["data_date"])
    data_date = str(run["data_date"])
    bars = _forward_bars(conn, int(run["id"]), data_date)
    eligible_dates = _eligible_trading_dates(
        bars,
        len(result_rows),
        quote_date,
        config,
    )
    regime = _market_regime(result_rows)
    observations = tuple(
        observation
        for row in result_rows
        if (
            observation := _observation_from_rows(
                run,
                row,
                bars.get(str(row["symbol"]), ()),
                eligible_dates,
                regime,
                config,
            )
        ) is not None
    )
    return _RunSnapshot(
        id=int(run["id"]),
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=str(run["rule_version"]),
        quote_date=quote_date,
        data_date=data_date,
        observations=observations,
        rankings=tuple((str(row["symbol"]), int(row["rank"])) for row in result_rows),
        eligible_dates=eligible_dates,
        expected_ranking_count=len(result_rows),
        exposures=tuple(_exposure_item(row) for row in result_rows),
        regime=regime,
    )


def _forward_bars(
    conn: sqlite3.Connection,
    run_id: int,
    data_date: str,
) -> dict[str, tuple[sqlite3.Row, ...]]:
    rows = conn.execute(
        """
        SELECT k.symbol, k.date, k.open, k.close, k.high, k.low, k.volume,
               k.adjustment_mode, k.as_of, k.data_version, k.contract_version,
               k.source, k.fetched_at, k.fallback_used
        FROM kline_daily AS k
        JOIN market_scan_result AS r
          ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
        WHERE k.date >= ?
          AND k.adjustment_mode = CASE
              WHEN r.adjustment_mode IN ('qfq', 'hfq', 'none') THEN r.adjustment_mode
              ELSE 'qfq'
          END
        ORDER BY k.date ASC, k.symbol ASC
        """,
        (run_id, data_date),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _eligible_trading_dates(
    bars: dict[str, tuple[sqlite3.Row, ...]],
    snapshot_count: int,
    quote_date: str,
    config: EvaluationConfig,
) -> tuple[str, ...]:
    # A horizon is an exchange-session contract, not a property of whichever
    # dates happen to have broad K-line coverage in the local cache.  Inferring
    # sessions from coverage could silently move D+1/H forward when an entire
    # market day is missing.  Keep the legacy arguments for the evaluator API,
    # but bind labels and forward paths to the trusted calendar; a missing
    # per-symbol bar remains explicitly unavailable downstream.
    del bars, snapshot_count
    limit = max((*config.horizons, *PROBABILITY_DEFAULT_HORIZONS)) + config.max_exit_delay_sessions + 1
    signal_date = date.fromisoformat(quote_date)
    return tuple(item.isoformat() for item in next_trade_dates(signal_date, limit))


def _observation_from_rows(
    run: sqlite3.Row,
    result: sqlite3.Row,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    regime: str,
    config: EvaluationConfig,
) -> _Observation | None:
    entry = float(result["price"])
    returns, adverse = _forward_performance(bars, eligible_dates, entry, config.horizons)
    quote_date = str(run["quote_date"] or run["data_date"])
    symbol = str(result["symbol"])
    market = str(result["market"])
    is_st = bool(result["is_st"])
    is_new = bool(result["is_new"])
    amount = float(result["amount"] or 0)
    execution, probability_labels = _observation_outcomes(
        result, symbol, market, is_st, is_new, quote_date, amount, bars, eligible_dates, config,
    )
    return _Observation(
        run_id=int(run["id"]),
        quote_date=quote_date,
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=str(run["rule_version"]),
        symbol=symbol,
        market=market,
        board=_board(symbol, market),
        industry=_normalize_industry(result["industry"]),
        segment="st" if is_st else "new" if is_new else "regular",
        liquidity_bucket=_liquidity_bucket(result["amount"]),
        scan_time_bucket=_scan_time_bucket(run["as_of"], str(run["mode"] or "official")),
        rank=int(result["rank"]),
        raw_score=_result_raw_score(result),
        amount=amount,
        turnover_rate=float(result["turnover_rate"]) if result["turnover_rate"] is not None else None,
        quality_bucket=_quality_bucket(result["data_quality_score"]),
        regime=regime,
        returns=returns,
        adverse=adverse,
        execution=execution,
        probability_labels=probability_labels,
        factor_values=_probability_factor_values(
            result,
            symbol=symbol,
            market=market,
            quote_date=quote_date,
            is_st=is_st,
            is_new=is_new,
        ),
        source_evidence_digest=_source_evidence_digest(result),
    )


def _observation_outcomes(
    result: sqlite3.Row,
    symbol: str,
    market: str,
    is_st: bool,
    is_new: bool,
    quote_date: str,
    amount: float,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    config: EvaluationConfig,
) -> tuple[dict[int, _ExecutionOutcome], dict[int, ProbabilityLabelOutcome]]:
    execution = _execution_outcomes(
        symbol=symbol, market=market, list_date=result["list_date"], is_st=is_st,
        is_new=is_new, quote_date=quote_date, amount=amount, bars=bars,
        eligible_dates=eligible_dates, config=config,
    )
    labels = _probability_label_outcomes(
        result=result, symbol=symbol, market=market, is_st=is_st, quote_date=quote_date,
        amount=amount, bars=bars, eligible_dates=eligible_dates, config=config,
    )
    return execution, labels


def _forward_performance(
    bars: Sequence[sqlite3.Row],
    eligible_dates: Sequence[str],
    entry: float,
    horizons: Sequence[int],
) -> tuple[dict[int, float], dict[int, float]]:
    by_date = {str(row["date"]): row for row in bars}
    returns: dict[int, float] = {}
    adverse: dict[int, float] = {}
    lows: list[float] = []
    for index, row_date in enumerate(eligible_dates, start=1):
        row = by_date.get(row_date)
        if row is None:
            continue
        lows.append(float(row["low"]))
        if index in horizons:
            returns[index] = float(row["close"]) / entry - 1
            adverse[index] = min(lows) / entry - 1
    return returns, adverse


def _probability_label_outcomes(
    *,
    result: sqlite3.Row,
    symbol: str,
    market: str,
    is_st: bool,
    quote_date: str,
    amount: float,
    bars: Sequence[sqlite3.Row],
    eligible_dates: Sequence[str],
    config: EvaluationConfig,
) -> dict[int, ProbabilityLabelOutcome]:
    settings = _probability_label_settings(config)
    try:
        rows = tuple(_to_kline(row) for row in bars)
        return build_probability_label_outcomes(
            symbol=symbol,
            market=market,
            list_date=str(result["list_date"]) if result["list_date"] else None,
            is_st=is_st,
            quote_date=quote_date,
            amount=amount,
            rows=rows,
            eligible_dates=eligible_dates,
            config=settings,
        )
    except (KeyError, TypeError, ValueError) as exc:
        reason = f"label_input_invalid:{type(exc).__name__}"
        return {
            horizon: ProbabilityLabelOutcome(horizon, "data_unavailable", reason)
            for horizon in settings.horizons
        }


def _probability_label_settings(config: EvaluationConfig) -> ProbabilityLabelConfig:
    return ProbabilityLabelConfig(
        horizons=PROBABILITY_DEFAULT_HORIZONS,
        cost_profile=config.cost_profile,
        execution_notional=config.execution_notional,
        max_daily_participation_rate=config.max_daily_participation_rate,
    )


def _result_raw_score(result: sqlite3.Row) -> float:
    if result["raw_score"] is not None:
        return float(result["raw_score"])
    if result["score"] is not None:
        return float(result["score"])
    return -float(result["rank"])


def _execution_outcomes(
    *,
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    is_new: bool,
    quote_date: str,
    amount: float,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    config: EvaluationConfig,
) -> dict[int, _ExecutionOutcome]:
    if not eligible_dates:
        return {}
    prepared = _prepare_execution_entry(
        symbol=symbol, market=market, list_date=list_date, is_st=is_st, is_new=is_new,
        quote_date=quote_date, amount=amount, bars=bars, eligible_dates=eligible_dates, config=config,
    )
    if isinstance(prepared, _ExecutionOutcome):
        return _repeat_outcome(config.horizons, prepared)
    outcomes: dict[int, _ExecutionOutcome] = {}
    for horizon in config.horizons:
        if horizon < len(eligible_dates):
            outcomes[horizon] = _execution_horizon(
                prepared, horizon, symbol, market, is_st, is_new, bars, eligible_dates, config,
            )
    return outcomes


def _prepare_execution_entry(
    *,
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    is_new: bool,
    quote_date: str,
    amount: float,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    config: EvaluationConfig,
) -> _ExecutionEntry | _ExecutionOutcome:
    by_date = {str(row["date"]): row for row in bars}
    entry_date = eligible_dates[0]
    entry_row = by_date.get(entry_date)
    previous = _previous_row(bars, entry_date)
    if entry_row is None or previous is None:
        return _ExecutionOutcome("data_unavailable", "entry_or_previous_bar_missing")
    if amount <= 0 or config.execution_notional / amount > config.max_daily_participation_rate:
        return _ExecutionOutcome("unfilled", "daily_capacity_limit", entry_date=entry_date)
    metadata = _execution_metadata(symbol, market, list_date, is_st, quote_date)
    entry_bar = _to_kline(entry_row)
    try:
        entry_profile = _evaluation_trade_profile(
            symbol,
            market,
            date.fromisoformat(entry_date),
            metadata,
            is_st=is_st,
            is_new=is_new,
        )
        entry_tradeability = assess_daily_tradeability(
            entry_bar,
            previous_close=float(previous["close"]),
            profile=entry_profile,
        )
    except (KeyError, ValueError):
        return _ExecutionOutcome("data_unavailable", "entry_rule_unavailable", entry_date=entry_date)
    if not entry_tradeability.can_buy:
        return _ExecutionOutcome("unfilled", entry_tradeability.code, entry_date=entry_date)
    entry_price = float(entry_row["open"])
    quantity = _model_quantity(config.execution_notional, entry_price, entry_profile.min_buy_quantity, entry_profile.buy_quantity_step)
    if quantity <= 0:
        return _ExecutionOutcome("unfilled", "minimum_quantity_unaffordable", entry_date=entry_date)
    cost_profile = resolve_cost_profile(config.cost_profile)
    buy_amount = entry_price * quantity
    buy_cost = trade_costs(cost_profile, side="buy", gross_amount=buy_amount).total
    return _ExecutionEntry(
        by_date=by_date, metadata=metadata, entry_date=entry_date, entry_price=entry_price,
        quantity=quantity, buy_amount=buy_amount, buy_cost=buy_cost,
        entry_model_limited=entry_tradeability.model_limited or entry_profile.quality != "ok",
        cost_profile=cost_profile,
    )


def _execution_metadata(
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    quote_date: str,
) -> PaperInstrumentMetadata:
    return PaperInstrumentMetadata(
        symbol=symbol,
        market=market,
        list_date=str(list_date) if list_date else None,
        is_st=is_st,
        status_effective_date=quote_date,
        source="market_scan_result",
    )


def _repeat_outcome(horizons: Sequence[int], outcome: _ExecutionOutcome) -> dict[int, _ExecutionOutcome]:
    return {horizon: outcome for horizon in horizons}


def _execution_horizon(
    entry: _ExecutionEntry,
    horizon: int,
    symbol: str,
    market: str,
    is_st: bool,
    is_new: bool,
    bars: Sequence[sqlite3.Row],
    eligible_dates: Sequence[str],
    config: EvaluationConfig,
) -> _ExecutionOutcome:
    for delay in range(config.max_exit_delay_sessions + 1):
        exit_index = horizon + delay
        if exit_index >= len(eligible_dates):
            break
        exit_date = eligible_dates[exit_index]
        sellable = _sellable_exit(entry, symbol, market, is_st, is_new, bars, exit_date)
        if sellable is None:
            continue
        exit_price, exit_model_limited = sellable
        return _modelled_execution(entry, exit_date, exit_price, delay, exit_model_limited)
    return _ExecutionOutcome(
        "unfilled", "exit_not_sellable_within_delay", entry_date=entry.entry_date,
        exit_delay_sessions=config.max_exit_delay_sessions,
    )


def _sellable_exit(
    entry: _ExecutionEntry,
    symbol: str,
    market: str,
    is_st: bool,
    is_new: bool,
    bars: Sequence[sqlite3.Row],
    exit_date: str,
) -> tuple[float, bool] | None:
    exit_row = entry.by_date.get(exit_date)
    previous = _previous_row(bars, exit_date)
    if exit_row is None or previous is None:
        return None
    try:
        profile = _evaluation_trade_profile(
            symbol, market, date.fromisoformat(exit_date), entry.metadata, is_st=is_st, is_new=is_new,
        )
        tradeability = assess_daily_tradeability(
            _to_kline(exit_row), previous_close=float(previous["close"]), profile=profile,
        )
    except (KeyError, ValueError):
        return None
    if not tradeability.can_sell:
        return None
    model_limited = tradeability.model_limited or profile.quality != "ok"
    return float(exit_row["open"]), model_limited


def _modelled_execution(
    entry: _ExecutionEntry,
    exit_date: str,
    exit_price: float,
    delay: int,
    exit_model_limited: bool,
) -> _ExecutionOutcome:
    sell_amount = exit_price * entry.quantity
    sell_cost = trade_costs(entry.cost_profile, side="sell", gross_amount=sell_amount).total
    gross_return = exit_price / entry.entry_price - 1
    net_return = (sell_amount - sell_cost - entry.buy_amount - entry.buy_cost) / (entry.buy_amount + entry.buy_cost)
    return _ExecutionOutcome(
        status="modelled", reason="exit_delayed" if delay else "next_open_t1",
        gross_return=gross_return, net_return=net_return, cost_drag=gross_return - net_return,
        entry_date=entry.entry_date, exit_date=exit_date, exit_delay_sessions=delay,
        model_limited=entry.entry_model_limited or exit_model_limited,
        buy_amount=entry.buy_amount,
        sell_amount=sell_amount,
    )


def _previous_row(rows: Sequence[sqlite3.Row], row_date: str) -> sqlite3.Row | None:
    candidates = [row for row in rows if str(row["date"]) < row_date]
    return candidates[-1] if candidates else None


def _to_kline(row: sqlite3.Row) -> Kline:
    return Kline(
        date=str(row["date"]),
        open=float(row["open"]),
        close=float(row["close"]),
        high=float(row["high"]),
        low=float(row["low"]),
        volume=float(row["volume"]),
        adjustment_mode=cast(KlineAdjustmentMode, str(row["adjustment_mode"])),
        as_of=row["as_of"],
        data_version=str(row["data_version"] or "unknown"),
        contract_version=str(row["contract_version"] or "daily-kline.v1"),
        source=row["source"],
        fetched_at=row["fetched_at"],
        fallback_used=bool(row["fallback_used"]),
    )


def _model_quantity(notional: float, price: float, minimum: int, step: int) -> int:
    if price <= 0 or step <= 0:
        return 0
    affordable = math.floor(notional / price)
    quantity = (affordable // step) * step
    return quantity if quantity >= minimum else 0


def _evaluation_trade_profile(
    symbol: str,
    market: str,
    trade_date: date,
    metadata: PaperInstrumentMetadata,
    *,
    is_st: bool,
    is_new: bool,
) -> PaperTradeRuleProfile:
    if is_st or is_new:
        return resolve_trade_rule_profile(symbol, trade_date, metadata)
    return _standard_evaluation_trade_profile(_board(symbol, market), trade_date.isoformat())


@lru_cache(maxsize=256)
def _standard_evaluation_trade_profile(board: str, trade_date: str) -> PaperTradeRuleProfile:
    canonical = {
        "SH_MAIN": ("600001.SH", "SH"),
        "STAR": ("688001.SH", "SH"),
        "SZ_MAIN": ("000001.SZ", "SZ"),
        "CHINEXT": ("300001.SZ", "SZ"),
        "BSE": ("920001.BJ", "BJ"),
    }[board]
    symbol, market = canonical
    metadata = PaperInstrumentMetadata(
        symbol=symbol,
        market=market,
        list_date="2022-01-04",
        is_st=False,
        status_effective_date=trade_date,
        source="market-scan-evaluation-standard-profile",
    )
    return resolve_trade_rule_profile(symbol, date.fromisoformat(trade_date), metadata)


def _cohort_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for dimensions, rows in _cohort_slices(observations):
        metrics.extend(_cohort_slice_metrics(dimensions, rows, config))
    return metrics


def _cohort_slices(
    observations: tuple[_Observation, ...],
) -> list[tuple[dict[str, str], tuple[_Observation, ...]]]:
    cohorts: list[tuple[dict[str, str], tuple[_Observation, ...]]] = []
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    for mode, scope, rule_version in contracts:
        contract_rows = tuple(
            item
            for item in observations
            if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
        )
        contract = {"mode": mode, "scope": scope, "rule_version": rule_version}
        cohorts.extend(_contract_cohort_slices(contract, contract_rows))
    return cohorts


def _contract_cohort_slices(
    contract: dict[str, str],
    rows: tuple[_Observation, ...],
) -> list[tuple[dict[str, str], tuple[_Observation, ...]]]:
    cohorts = [(contract, rows)]
    dimensions = {
        "market": ("SH", "SZ", "BJ"),
        "board": ("SH_MAIN", "STAR", "SZ_MAIN", "CHINEXT", "BSE"),
        "regime": ("strong", "neutral", "weak"),
        "quality": ("unknown", "low", "medium", "high"),
        "segment": ("regular", "st", "new"),
        "liquidity": ("low", "medium", "high"),
        "scan_time": ("preopen", "morning", "afternoon", "after_close", "unknown"),
    }
    attributes = {
        "market": "market",
        "board": "board",
        "regime": "regime",
        "quality": "quality_bucket",
        "segment": "segment",
        "liquidity": "liquidity_bucket",
        "scan_time": "scan_time_bucket",
    }
    for dimension, values in dimensions.items():
        attribute = attributes[dimension]
        for value in values:
            cohorts.append(
                (
                    {**contract, dimension: value},
                    tuple(item for item in rows if getattr(item, attribute) == value),
                )
            )
    return cohorts


def _cohort_slice_metrics(
    dimensions: dict[str, str],
    rows: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for top_n in config.top_sizes:
        selected = tuple(item for item in rows if item.rank <= top_n)
        for horizon in config.horizons:
            metrics.append(_metric_record(dimensions, selected, rows, top_n, horizon, config))
    return metrics


def _metric_record(
    dimensions: dict[str, str],
    selected: tuple[_Observation, ...],
    benchmark_rows: tuple[_Observation, ...],
    top_n: int,
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    values = [(item, item.returns[horizon]) for item in selected if horizon in item.returns]
    grouped = _returns_by_run(values)
    independent_sessions = len(grouped)
    enough_samples = len(values) >= config.minimum_sample_size
    enough_sessions = independent_sessions >= config.minimum_session_count
    status: EvaluationStatus = "ok" if enough_samples and enough_sessions else "insufficient_data"
    record: dict[str, object] = {
        "dimensions": dimensions,
        "top_n": top_n,
        "horizon_trading_days": horizon,
        "status": status,
        "sample_size": len(values),
        "independent_session_count": independent_sessions,
        "insufficient_reasons": _insufficient_reasons(enough_samples, enough_sessions),
    }
    if not values:
        record["execution"] = _execution_summary(selected, horizon)
        return record
    record.update(_return_statistics(dimensions, selected, benchmark_rows, values, top_n, horizon, config))
    return record


def _return_statistics(
    dimensions: dict[str, str],
    selected: tuple[_Observation, ...],
    benchmark_rows: tuple[_Observation, ...],
    values: list[tuple[_Observation, float]],
    top_n: int,
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    grouped = _returns_by_run(values)
    returns = [value for _item, value in values]
    adverse = [item.adverse[horizon] for item, _value in values if horizon in item.adverse]
    benchmark_by_run = _benchmark_returns(benchmark_rows, horizon)
    daily_returns = {run_id: fmean(items) for run_id, items in grouped.items() if items}
    daily_excess = {
        run_id: value - benchmark_by_run[run_id]
        for run_id, value in daily_returns.items()
        if run_id in benchmark_by_run
    }
    daily_values = list(daily_returns.values())
    daily_excess_values = list(daily_excess.values())
    execution = _execution_summary(selected, horizon)
    daily_net = _execution_net_returns_by_run(selected, horizon)
    daily_net_excess = {
        run_id: value - benchmark_by_run[run_id]
        for run_id, value in daily_net.items()
        if run_id in benchmark_by_run
    }
    execution["average_net_excess_return"] = (
        fmean(daily_net_excess.values()) if daily_net_excess else None
    )
    execution["net_excess_independent_session_count"] = len(daily_net_excess)
    seed = json.dumps({"dimensions": dimensions, "top_n": top_n, "horizon": horizon}, sort_keys=True)
    return {
        "average_return": fmean(returns),
        "median_return": median(returns),
        "positive_return_rate": sum(value > 0 for value in returns) / len(returns),
        "session_average_return": fmean(daily_values),
        "session_median_return": median(daily_values),
        "session_positive_rate": sum(value > 0 for value in daily_values) / len(daily_values),
        "session_return_confidence_interval_95": _cluster_bootstrap_ci(
            daily_values, seed + ":return", config.bootstrap_samples,
        ),
        "equal_weight_market_return": fmean(benchmark_by_run.values()) if benchmark_by_run else None,
        "equal_weight_market_excess_return": fmean(daily_excess_values) if daily_excess_values else None,
        "session_excess_confidence_interval_95": _cluster_bootstrap_ci(
            daily_excess_values, seed + ":excess", config.bootstrap_samples,
        ),
        "session_maximum_drawdown": _compounded_maximum_drawdown(daily_values),
        "maximum_adverse_excursion": min(adverse) if adverse else None,
        "execution": execution,
    }


def _compounded_maximum_drawdown(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1)
    return worst


def _execution_summary(rows: Iterable[_Observation], horizon: int) -> dict[str, object]:
    materialized = tuple(rows)
    outcomes = [item.execution[horizon] for item in materialized if horizon in item.execution]
    statuses = Counter(item.status for item in outcomes)
    modelled = [item for item in outcomes if item.status == "modelled" and item.net_return is not None]
    by_run, cost_drag, delayed, model_limited = _execution_aggregates(materialized, horizon)
    daily_net = [fmean(values) for values in by_run.values() if values]
    return {
        "status_counts": dict(sorted(statuses.items())),
        "modelled_sample_size": len(modelled),
        "independent_session_count": len(daily_net),
        "average_net_return": fmean(daily_net) if daily_net else None,
        "median_net_return": median(daily_net) if daily_net else None,
        "average_cost_drag": fmean(cost_drag) if cost_drag else None,
        "delayed_exit_count": delayed,
        "model_limited_count": model_limited,
    }


def _execution_aggregates(
    rows: Sequence[_Observation],
    horizon: int,
) -> tuple[dict[int, list[float]], list[float], int, int]:
    by_run: dict[int, list[float]] = defaultdict(list)
    cost_drag: list[float] = []
    delayed = 0
    model_limited = 0
    for observation in rows:
        outcome = observation.execution.get(horizon)
        if outcome is None:
            continue
        if outcome.status == "modelled" and outcome.net_return is not None:
            by_run[observation.run_id].append(outcome.net_return)
        if outcome.cost_drag is not None:
            cost_drag.append(outcome.cost_drag)
        delayed += outcome.exit_delay_sessions > 0
        model_limited += outcome.model_limited
    return by_run, cost_drag, delayed, model_limited


def _execution_net_returns_by_run(
    rows: Sequence[_Observation],
    horizon: int,
) -> dict[int, float]:
    by_run, _cost_drag, _delayed, _model_limited = _execution_aggregates(rows, horizon)
    return {run_id: fmean(values) for run_id, values in by_run.items() if values}


def _returns_by_run(values: Iterable[tuple[_Observation, float]]) -> dict[int, list[float]]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for item, value in values:
        grouped[item.run_id].append(value)
    return grouped


def _insufficient_reasons(enough_samples: bool, enough_sessions: bool) -> list[str]:
    reasons: list[str] = []
    if not enough_samples:
        reasons.append("minimum_sample_size")
    if not enough_sessions:
        reasons.append("minimum_session_count")
    return reasons


def _benchmark_returns(rows: Iterable[_Observation], horizon: int) -> dict[int, float]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for item in rows:
        if horizon in item.returns:
            grouped[item.run_id].append(item.returns[horizon])
    return {run_id: fmean(values) for run_id, values in grouped.items() if values}


def _cluster_bootstrap_ci(values: Sequence[float], seed_text: str, samples: int) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    means = sorted(
        fmean(values[generator.randrange(len(values))] for _index in range(len(values)))
        for _sample in range(samples)
    )
    return [_percentile(means, 0.025), _percentile(means, 0.975)]


def _monotonicity_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        records.extend(
            _monotonicity_record(mode, scope, rule_version, rows, horizon, config)
            for horizon in config.horizons
        )
    return records


def _monotonicity_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    summaries = [
        _band_return_summary("1-20", rows, horizon, 1, 20),
        _band_return_summary("21-50", rows, horizon, 21, 50),
        _band_return_summary("51-100", rows, horizon, 51, 100),
    ]
    enough = all(
        cast(int, item["sample_size"]) >= config.minimum_sample_size
        and cast(int, item["independent_session_count"]) >= config.minimum_session_count
        for item in summaries
    )
    monotonic = _band_means_descend(summaries) if enough else None
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "horizon_trading_days": horizon,
        "status": "ok" if enough else "insufficient_data",
        "monotonic": monotonic,
        "bands": summaries,
    }


def _band_return_summary(
    label: str,
    rows: tuple[_Observation, ...],
    horizon: int,
    minimum_rank: int,
    maximum_rank: int,
) -> dict[str, object]:
    selected = [
        item
        for item in rows
        if minimum_rank <= item.rank <= maximum_rank and horizon in item.returns
    ]
    values = [item.returns[horizon] for item in selected]
    by_run = _returns_by_run((item, item.returns[horizon]) for item in selected)
    daily = [fmean(items) for items in by_run.values() if items]
    return {
        "band": label,
        "sample_size": len(values),
        "independent_session_count": len(daily),
        "average_return": fmean(daily) if daily else None,
    }


def _band_means_descend(summaries: list[dict[str, object]]) -> bool:
    means = [cast(float, item["average_return"]) for item in summaries]
    return all(left >= right for left, right in zip(means, means[1:], strict=False))


def _decile_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        run_sizes = Counter(item.run_id for item in rows)
        for horizon in config.horizons:
            records.append(_decile_record(mode, scope, rule_version, rows, run_sizes, horizon, config))
    return records


def _decile_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    run_sizes: Counter[int],
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    bands = [_decile_band(rows, run_sizes, horizon, decile) for decile in range(1, 11)]
    enough = all(_decile_band_sufficient(item, config) for item in bands)
    values = [cast(float, item["average_return"]) for item in bands] if enough else []
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "horizon_trading_days": horizon,
        "status": "ok" if enough else "insufficient_data",
        "monotonic": _descending(values) if enough else None,
        "bands": bands,
    }


def _decile_band(
    rows: Sequence[_Observation],
    run_sizes: Counter[int],
    horizon: int,
    decile: int,
) -> dict[str, object]:
    selected = [
        item
        for item in rows
        if horizon in item.returns
        and min(10, math.ceil(item.rank / max(1, run_sizes[item.run_id]) * 10)) == decile
    ]
    by_run = _returns_by_run((item, item.returns[horizon]) for item in selected)
    daily = [fmean(values) for values in by_run.values() if values]
    return {
        "decile": decile,
        "sample_size": len(selected),
        "independent_session_count": len(daily),
        "average_return": fmean(daily) if daily else None,
    }


def _decile_band_sufficient(item: dict[str, object], config: EvaluationConfig) -> bool:
    return (
        cast(int, item["sample_size"]) >= config.minimum_sample_size
        and cast(int, item["independent_session_count"]) >= config.minimum_session_count
    )


def _descending(values: Sequence[float]) -> bool:
    return all(left >= right for left, right in zip(values, values[1:], strict=False))


def _rank_ic_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        for horizon in config.horizons:
            grouped: dict[int, list[_Observation]] = defaultdict(list)
            for item in rows:
                if horizon in item.returns:
                    grouped[item.run_id].append(item)
            daily_ic = [
                value
                for run_rows in grouped.values()
                if (value := _spearman([(item.raw_score, item.returns[horizon]) for item in run_rows])) is not None
            ]
            enough = len(daily_ic) >= config.minimum_session_count
            mean_ic = fmean(daily_ic) if daily_ic else None
            deviation = pstdev(daily_ic) if len(daily_ic) >= 2 else None
            records.append(
                {
                    "mode": mode,
                    "scope": scope,
                    "rule_version": rule_version,
                    "horizon_trading_days": horizon,
                    "status": "ok" if enough else "insufficient_data",
                    "independent_session_count": len(daily_ic),
                    "mean_rank_ic": mean_ic,
                    "icir": mean_ic / deviation if mean_ic is not None and deviation and deviation > 0 else None,
                    "confidence_interval_95": _cluster_bootstrap_ci(
                        daily_ic,
                        f"{mode}:{scope}:{rule_version}:{horizon}:ic",
                        config.bootstrap_samples,
                    ),
                }
            )
    return records


def _contract_rows(
    observations: tuple[_Observation, ...],
) -> list[tuple[str, str, str, tuple[_Observation, ...]]]:
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    return [
        (
            mode,
            scope,
            rule_version,
            tuple(
                item
                for item in observations
                if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
            ),
        )
        for mode, scope, rule_version in contracts
    ]


def _factor_values(result: sqlite3.Row) -> dict[str, float]:
    values = _finite_row_values(
        result,
        (
        "raw_score", "trend_score", "change_pct", "data_quality_score",
        "amount", "turnover_rate", "volume_ratio",
        ),
    )
    if "metrics_json" not in result.keys():
        return values
    _metrics, details = decode_result_payload(result["metrics_json"])
    components = details.get("components")
    if not isinstance(components, dict):
        return values
    values.update(_production_score_component_values(components))
    continuous_trend = _continuous_trend_component(components)
    continuous_score = continuous_trend.get("score") if continuous_trend is not None else None
    if isinstance(continuous_score, int | float):
        # Keep the historical factor name as a stable model-feature alias while
        # sourcing the material v5 continuous-trend component when present.
        values["rank_refinement"] = float(continuous_score)
    dimensions = components.get("score_dimensions")
    if not isinstance(dimensions, dict):
        return values
    scores = dimensions.get("scores")
    if isinstance(scores, dict):
        values.update(_finite_mapping_values(
            scores,
            ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"),
        ))
    raw_features = dimensions.get("raw_features")
    if isinstance(raw_features, dict):
        values.update(_finite_mapping_values(raw_features, tuple(raw_features), prefix="feature_"))
    return values


def _probability_factor_values(
    result: sqlite3.Row,
    *,
    symbol: str,
    market: str,
    quote_date: str,
    is_st: bool,
    is_new: bool,
) -> dict[str, float]:
    """Add point-in-time status and effective price-limit facts to frozen factors."""
    values = _factor_values(result)
    values.update({"is_st": float(is_st), "is_new": float(is_new)})
    metadata = _execution_metadata(symbol, market, result["list_date"], is_st, quote_date)
    try:
        profile = resolve_trade_rule_profile(symbol, date.fromisoformat(quote_date), metadata)
    except (KeyError, TypeError, ValueError):
        values["price_limit_profile_uncertain"] = 1.0
        return values
    verified = profile.quality == "ok"
    values.update(
        {
            "price_limit_pct": float(profile.price_limit_pct or 0.0),
            "price_limit_profile_verified": float(verified),
            "price_limit_profile_uncertain": float(not verified),
            "price_limit_absent": float(verified and profile.price_limit_pct is None),
            "new_stock_no_limit_phase": float(
                is_new and verified and profile.price_limit_pct is None
            ),
        }
    )
    return values


def _production_score_component_values(components: Mapping[str, object]) -> dict[str, float]:
    values: dict[str, float] = {}
    groups = (
        ("leader_score", ("base", "trend_delta", "unclamped", "score"), "leader_"),
        (
            "final_score",
            (
                "quality_penalty", "base", "continuous_trend_adjustment",
                "rank_discount", "raw", "rounded", "score",
            ),
            "final_",
        ),
    )
    for group_name, names, prefix in groups:
        group = components.get(group_name)
        if isinstance(group, Mapping):
            values.update(_finite_mapping_values(group, names, prefix=prefix))
    continuous_trend = _continuous_trend_component(components)
    if continuous_trend is not None:
        normalized = continuous_trend.get("normalized_inputs")
        if isinstance(normalized, Mapping):
            values.update(_finite_mapping_values(normalized, tuple(normalized), prefix="refinement_"))
    return values


def _continuous_trend_component(
    components: Mapping[str, object],
) -> Mapping[str, object] | None:
    current = components.get("continuous_trend")
    if isinstance(current, Mapping):
        return current
    legacy = components.get("rank_refinement")
    return legacy if isinstance(legacy, Mapping) else None


def _source_evidence_digest(result: sqlite3.Row) -> str | None:
    if "metrics_json" not in result.keys():
        return None
    _metrics, details = decode_result_payload(result["metrics_json"])
    components = details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, dict) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, dict) else None
    if not isinstance(evidence, dict) or not verify_market_scan_point_in_time_evidence(evidence):
        return None
    digest = evidence.get("payload_digest") if isinstance(evidence, dict) else None
    return digest if isinstance(digest, str) and len(digest) == 64 else None


def _finite_row_values(row: sqlite3.Row, names: Sequence[str]) -> dict[str, float]:
    available = set(row.keys())
    return {
        name: parsed
        for name in names
        if name in available
        and row[name] is not None
        and not isinstance(row[name], bool)
        and math.isfinite(parsed := float(row[name]))
    }


def _finite_mapping_values(
    values: Mapping[object, object],
    names: Sequence[object],
    *,
    prefix: str = "",
) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for name in names:
        value = values.get(name)
        if isinstance(name, str) and isinstance(value, int | float) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                parsed[f"{prefix}{name}"] = number
    return parsed


def _factor_diagnostics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        factors = sorted({name for item in rows for name in item.factor_values})
        for horizon in config.horizons:
            for factor in factors:
                records.append(
                    _factor_diagnostic_record(
                        mode, scope, rule_version, rows, factor, horizon, config,
                    )
                )
    return _apply_factor_fdr(records)


def _factor_diagnostic_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: Sequence[_Observation],
    factor: str,
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    daily_ic, daily_partial_ic = _daily_factor_ics(rows, factor, horizon)
    inference_minimum = max(config.minimum_session_count, DEFAULT_MINIMUM_SESSION_COUNT)
    raw_p_value = moving_block_bootstrap_p_value(
        daily_ic,
        samples=config.bootstrap_samples,
        block_length=max(1, horizon),
        seed_text=f"factor:{mode}:{scope}:{rule_version}:{factor}:{horizon}",
        minimum_count=inference_minimum,
    )
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "factor": factor,
        "horizon_trading_days": horizon,
        "status": "ok" if len(daily_ic) >= config.minimum_session_count else "insufficient_data",
        "independent_session_count": len(daily_ic),
        "mean_rank_ic": fmean(daily_ic) if daily_ic else None,
        "mean_partial_rank_ic_controlling_raw_score": (
            fmean(daily_partial_ic) if daily_partial_ic else None
        ),
        "partial_ic_session_count": len(daily_partial_ic),
        "hypothesis": "H0: mean session rank IC <= 0",
        "raw_p_value_one_sided": raw_p_value,
        "multiple_testing": {
            "method": "benjamini-hochberg-fdr",
            "family": "same-contract-and-horizon-factor-diagnostics",
            "alpha": MULTIPLE_TESTING_ALPHA,
            "status": "pending_family_adjustment" if raw_p_value is not None else "insufficient_data",
            "adjusted_p_value": None,
            "rejected": None,
            "minimum_independent_session_count": inference_minimum,
        },
    }


def _apply_factor_fdr(records: list[dict[str, object]]) -> list[dict[str, object]]:
    families: dict[tuple[str, str, str, int], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        families[
            (
                str(record["mode"]),
                str(record["scope"]),
                str(record["rule_version"]),
                int(str(record["horizon_trading_days"])),
            )
        ].append(record)
    for family_records in families.values():
        raw = [_optional_float(record.get("raw_p_value_one_sided")) for record in family_records]
        adjusted, rejected = benjamini_hochberg(raw, alpha=MULTIPLE_TESTING_ALPHA)
        available = sum(value is not None for value in raw)
        family_status = "ok" if available == len(raw) else "insufficient_data"
        for record, adjusted_value, rejected_value in zip(
            family_records, adjusted, rejected, strict=True,
        ):
            control = cast(dict[str, object], record["multiple_testing"])
            control.update(
                {
                    "status": family_status if adjusted_value is not None else "insufficient_data",
                    "family_size": len(raw),
                    "tested_hypothesis_count": available,
                    "adjusted_p_value": adjusted_value,
                    "rejected": rejected_value,
                }
            )
    return records


def _daily_factor_ics(
    rows: Sequence[_Observation],
    factor: str,
    horizon: int,
) -> tuple[list[float], list[float]]:
    by_session: dict[str, list[_Observation]] = defaultdict(list)
    for item in rows:
        if horizon in item.returns and factor in item.factor_values:
            by_session[item.quote_date].append(item)
    daily_ic: list[float] = []
    daily_partial_ic: list[float] = []
    for session_rows in by_session.values():
        factor_ic = _spearman(
            [(item.factor_values[factor], item.returns[horizon]) for item in session_rows]
        )
        if factor_ic is not None:
            daily_ic.append(factor_ic)
        partial = None if factor == "raw_score" else _partial_rank_ic(session_rows, factor, horizon)
        if partial is not None:
            daily_partial_ic.append(partial)
    return daily_ic, daily_partial_ic


def _partial_rank_ic(
    rows: Sequence[_Observation],
    factor: str,
    horizon: int,
) -> float | None:
    materialized = [
        item
        for item in rows
        if factor in item.factor_values
        and "raw_score" in item.factor_values
        and horizon in item.returns
    ]
    if len(materialized) < 3:
        return None
    factor_return = _spearman(
        [(item.factor_values[factor], item.returns[horizon]) for item in materialized]
    )
    factor_base = _spearman(
        [(item.factor_values[factor], item.factor_values["raw_score"]) for item in materialized]
    )
    base_return = _spearman(
        [(item.factor_values["raw_score"], item.returns[horizon]) for item in materialized]
    )
    if factor_return is None or factor_base is None or base_return is None:
        return None
    denominator = math.sqrt(max(0.0, (1 - factor_base**2) * (1 - base_return**2)))
    return (factor_return - factor_base * base_return) / denominator if denominator > 1e-12 else None


def _stability_metrics(
    snapshots: tuple[_RunSnapshot, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    by_mode: dict[str, list[_RunSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_mode[snapshot.mode].append(snapshot)
    for mode, mode_runs in by_mode.items():
        for previous, current in zip(mode_runs, mode_runs[1:], strict=False):
            comparable = previous.scope == current.scope and previous.rule_version == current.rule_version
            for top_n in config.top_sizes:
                records.append(_stability_record(mode, previous, current, top_n, comparable, config))
    return records


def _stability_record(
    mode: str,
    previous: _RunSnapshot,
    current: _RunSnapshot,
    top_n: int,
    comparable: bool,
    config: EvaluationConfig,
) -> dict[str, object]:
    previous_ranks = {symbol: rank for symbol, rank in previous.rankings if rank <= top_n}
    current_ranks = {symbol: rank for symbol, rank in current.rankings if rank <= top_n}
    common = sorted(previous_ranks.keys() & current_ranks.keys())
    denominator = min(len(previous_ranks), len(current_ranks), top_n)
    has_rankings = denominator > 0
    overlap = len(common) / denominator if has_rankings else None
    return {
        "mode": mode,
        "previous_run_id": previous.id,
        "current_run_id": current.id,
        "top_n": top_n,
        "status": "ok" if comparable and denominator >= config.minimum_sample_size else "insufficient_data",
        "comparable": comparable,
        "ranking_evidence_available": has_rankings,
        "overlap_rate": overlap if comparable else None,
        "turnover_rate": 1 - overlap if comparable and overlap is not None else None,
        "rank_stability": _spearman_rank_stability(previous_ranks, current_ranks, common) if comparable else None,
    }


def _hysteresis_metrics(
    snapshots: tuple[_RunSnapshot, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    by_contract: dict[tuple[str, str, str], list[_RunSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_contract[(snapshot.mode, snapshot.scope, snapshot.rule_version)].append(snapshot)
    for contract, values in sorted(by_contract.items()):
        ordered = sorted(values, key=lambda item: (item.quote_date, item.id))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            current_ranks = dict(current.rankings)
            for top_n in config.top_sizes:
                previous_members = {symbol for symbol, rank in previous.rankings if rank <= top_n}
                baseline_members = [symbol for symbol, rank in current.rankings if rank <= top_n]
                hold_rank = max(top_n, math.ceil(top_n * (1 + config.hysteresis_buffer_ratio)))
                retained = [
                    symbol
                    for symbol in previous_members
                    if current_ranks.get(symbol, hold_rank + 1) <= hold_rank
                ]
                hysteresis_members = retained + [
                    symbol for symbol in baseline_members if symbol not in retained
                ][: max(0, top_n - len(retained))]
                baseline_overlap = len(previous_members & set(baseline_members))
                hysteresis_overlap = len(previous_members & set(hysteresis_members))
                denominator = min(top_n, len(previous_members), len(baseline_members))
                baseline_turnover = 1 - baseline_overlap / denominator if denominator else None
                hysteresis_turnover = 1 - hysteresis_overlap / denominator if denominator else None
                records.append(
                    {
                        "mode": contract[0],
                        "scope": contract[1],
                        "rule_version": contract[2],
                        "previous_run_id": previous.id,
                        "current_run_id": current.id,
                        "top_n": top_n,
                        "buy_rank_threshold": top_n,
                        "hold_rank_threshold": hold_rank,
                        "buffer_ratio": config.hysteresis_buffer_ratio,
                        "baseline_turnover_rate": baseline_turnover,
                        "hysteresis_turnover_rate": hysteresis_turnover,
                        "estimated_turnover_reduction": (
                            baseline_turnover - hysteresis_turnover
                            if baseline_turnover is not None and hysteresis_turnover is not None
                            else None
                        ),
                        "status": "diagnostic-only-not-applied-to-production-ranking",
                    }
                )
    return records


def _spearman_rank_stability(
    previous: dict[str, int],
    current: dict[str, int],
    common: list[str],
) -> float | None:
    return _spearman([(float(previous[symbol]), float(current[symbol])) for symbol in common])


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = _midranks([item[0] for item in pairs])
    right = _midranks([item[1] for item in pairs])
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator > 0 else None


def _midranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        midrank = (index + end - 1) / 2 + 1
        for position in range(index, end):
            ranks[ordered[position][0]] = midrank
        index = end
    return ranks


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _run_summary(snapshot: _RunSnapshot, config: EvaluationConfig) -> dict[str, object]:
    available_horizons = sorted(
        {horizon for item in snapshot.observations for horizon in item.returns if horizon in config.horizons}
    )
    execution_horizons = sorted(
        {
            horizon
            for item in snapshot.observations
            for horizon, outcome in item.execution.items()
            if outcome.status == "modelled"
        }
    )
    return {
        "run_id": snapshot.id,
        "mode": snapshot.mode,
        "scope": snapshot.scope,
        "rule_version": snapshot.rule_version,
        "quote_date": snapshot.quote_date,
        "data_date": snapshot.data_date,
        "eligible_trading_day_count": len(snapshot.eligible_dates),
        "observation_count": len(snapshot.observations),
        "ranking_count": len(snapshot.rankings),
        "available_horizons": available_horizons,
        "execution_horizons": execution_horizons,
    }


__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_MINIMUM_SESSION_COUNT",
    "DEFAULT_TOP_SIZES",
    "EVALUATION_SCHEMA_VERSION",
    "EvaluationConfig",
    "evaluate_market_scan_rankings",
    "evaluate_market_scan_shadow_comparison",
    "evaluate_market_scan_shadow_rankings",
]
