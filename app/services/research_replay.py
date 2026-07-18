from __future__ import annotations

from collections.abc import Callable
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite

from app.models.reviews import AdviceReviewEvaluationDraft, AdviceReviewPlan
from app.models.schemas import AnalysisResult, Kline, ReplayCase, ReplayPatternStat, StockReplayAnalysis
from app.services.indicators import pct_change
from app.services.trading_calendar import (
    DAILY_KLINE_PUBLISH_TIME,
    is_trading_day,
)
from app.utils.market_data import valid_kline


MIN_REPLAY_KLINES = 30
PATTERN_LOOKBACK_DAYS = 20
MAX_FORWARD_DAYS = 10
REPLAY_EVALUATION_DAYS = 5
VOLUME_LOOKBACK_DAYS = 5
DISPLAY_CASE_LIMIT = 8
STABLE_SAMPLE_THRESHOLD = 5
EFFECTIVE_5D_RETURN = 2
RISK_5D_RETURN = -3
OUTCOME_EFFECTIVE = "有效"
OUTCOME_RISK = "风险"
OUTCOME_PENDING = "待确认"
OUTCOME_RANGE = "震荡"


@dataclass(frozen=True)
class ForwardReturns:
    day3: float | None
    day5: float | None
    day10: float | None


@dataclass(frozen=True)
class ReplayPatternContext:
    current: Kline
    previous: Kline
    high_20: float
    low_20: float
    change_pct: float
    volume_ratio: float


@dataclass(frozen=True)
class ReplayPatternRows:
    current: Kline
    previous: Kline
    previous_window: list[Kline]


@dataclass(frozen=True)
class ReplayVolumeContext:
    average_volume_5: float
    volume_ratio: float


@dataclass(frozen=True)
class ReplayPatternRule:
    label: str
    matches: Callable[[ReplayPatternContext], bool]


@dataclass(frozen=True)
class ReplayPatternNoteContext:
    pattern: str
    sample_count: int
    win_rate: float
    avg_return: float
    completed_count: int
    pending_suffix: str


@dataclass(frozen=True)
class ReplayPatternNoteRule:
    matches: Callable[[ReplayPatternNoteContext], bool]
    message: Callable[[ReplayPatternNoteContext], str]


def build_replay_analysis(analysis: AnalysisResult, window_days: int = 120) -> StockReplayAnalysis:
    rows = _replay_window(analysis.klines, window_days)
    symbol = f"{analysis.quote.code}.{analysis.quote.market}"
    if len(rows) < MIN_REPLAY_KLINES:
        return _insufficient_replay_report(symbol, analysis.quote.timestamp, len(rows))
    cases = _replay_cases(rows)
    stats = _replay_stats(cases)
    success_rate = _replay_success_rate(cases)
    return StockReplayAnalysis(
        symbol=symbol,
        updated_at=analysis.quote.timestamp,
        window_days=len(rows),
        sample_count=len(cases),
        success_rate=success_rate,
        summary=_replay_summary(len(rows), cases, success_rate),
        pattern_stats=stats,
        cases=cases[-DISPLAY_CASE_LIMIT:],
        notes=_replay_notes(),
    )


def _replay_window(rows: list[Kline], window_days: int) -> list[Kline]:
    if window_days <= 0:
        return []
    return rows[-window_days:]


def _insufficient_replay_report(symbol: str, updated_at: str, row_count: int) -> StockReplayAnalysis:
    return StockReplayAnalysis(
        symbol=symbol,
        updated_at=updated_at,
        window_days=row_count,
        sample_count=0,
        success_rate=0,
        summary="历史K线不足，暂不能做信号回放。",
        notes=[f"至少需要{MIN_REPLAY_KLINES}根日K才能做基本回放。"],
    )


def _replay_cases(rows: list[Kline]) -> list[ReplayCase]:
    cases: list[ReplayCase] = []
    for index in _replay_candidate_indices(rows):
        case = _replay_case(rows, index)
        if case:
            cases.append(case)
    return cases


def _replay_candidate_indices(rows: list[Kline]) -> range:
    return range(PATTERN_LOOKBACK_DAYS, len(rows))


def _replay_case(rows: list[Kline], index: int) -> ReplayCase | None:
    pattern = _detect_replay_pattern(rows, index)
    if not pattern:
        return None
    entry = rows[index].close
    if not _is_positive_finite(entry):
        return None
    returns = _forward_returns(rows, index, entry)
    outcome = _replay_outcome(returns.day5)
    return ReplayCase(
        date=rows[index].date,
        pattern=pattern,
        entry_price=round(entry, 2),
        forward_3d_return=_round_optional(returns.day3),
        forward_5d_return=_round_optional(returns.day5),
        forward_10d_return=_round_optional(returns.day10),
        outcome=outcome,
        note=_replay_case_note(pattern, outcome),
    )


def _forward_returns(rows: list[Kline], index: int, entry: float) -> ForwardReturns:
    return ForwardReturns(
        day3=_forward_return(rows, index, entry, 3),
        day5=_forward_return(rows, index, entry, REPLAY_EVALUATION_DAYS),
        day10=_forward_return(rows, index, entry, MAX_FORWARD_DAYS),
    )


def _forward_return(rows: list[Kline], index: int, entry: float, days: int) -> float | None:
    target_index = index + days
    if target_index >= len(rows) or not _is_positive_finite(entry):
        return None
    if not _valid_price_bar(rows[target_index]):
        return None
    return pct_change(rows[target_index].close, entry)


def _replay_outcome(forward_5d: float | None) -> str:
    if not _is_finite_number(forward_5d):
        return OUTCOME_PENDING
    if forward_5d > EFFECTIVE_5D_RETURN:
        return OUTCOME_EFFECTIVE
    if forward_5d < RISK_5D_RETURN:
        return OUTCOME_RISK
    return OUTCOME_RANGE


def _round_optional(value: float | None) -> float | None:
    if not _is_finite_number(value):
        return None
    return round(value, 2)


def _detect_replay_pattern(rows: list[Kline], index: int) -> str | None:
    context = _replay_pattern_context(rows, index)
    if context is None:
        return None
    return next((rule.label for rule in REPLAY_PATTERN_RULES if rule.matches(context)), None)


def _replay_pattern_context(rows: list[Kline], index: int) -> ReplayPatternContext | None:
    pattern_rows = _replay_pattern_rows(rows, index)
    if pattern_rows is None:
        return None
    volume_context = _replay_volume_context(rows, index, pattern_rows.current)
    if volume_context is None:
        return None
    return _build_replay_pattern_context(pattern_rows, volume_context)


def _replay_pattern_rows(rows: list[Kline], index: int) -> ReplayPatternRows | None:
    if not _replay_pattern_index_is_valid(rows, index):
        return None
    window = rows[index - PATTERN_LOOKBACK_DAYS : index + 1]
    previous_window = window[:-1]
    if len(previous_window) < PATTERN_LOOKBACK_DAYS:
        return None
    if not _valid_price_window(window):
        return None
    return ReplayPatternRows(current=rows[index], previous=rows[index - 1], previous_window=previous_window)


def _replay_pattern_index_is_valid(rows: list[Kline], index: int) -> bool:
    return PATTERN_LOOKBACK_DAYS <= index < len(rows)


def _valid_price_window(rows: list[Kline]) -> bool:
    return all(_valid_price_bar(item) for item in rows)


def _replay_volume_context(rows: list[Kline], index: int, current: Kline) -> ReplayVolumeContext | None:
    average_volume_5 = _average_positive_volume(rows[index - VOLUME_LOOKBACK_DAYS : index])
    if not _is_positive_finite(current.volume):
        return None
    if not _is_positive_finite(average_volume_5):
        return None
    return ReplayVolumeContext(average_volume_5=average_volume_5, volume_ratio=current.volume / average_volume_5)


def _build_replay_pattern_context(
    pattern_rows: ReplayPatternRows,
    volume_context: ReplayVolumeContext,
) -> ReplayPatternContext:
    current = pattern_rows.current
    return ReplayPatternContext(
        current=current,
        previous=pattern_rows.previous,
        high_20=max(item.high for item in pattern_rows.previous_window),
        low_20=min(item.low for item in pattern_rows.previous_window),
        change_pct=pct_change(current.close, pattern_rows.previous.close),
        volume_ratio=volume_context.volume_ratio,
    )


def _valid_price_bar(row: Kline) -> bool:
    prices = (row.open, row.close, row.high, row.low)
    if not all(_is_positive_finite(value) for value in prices):
        return False
    return row.high >= max(row.open, row.close) and row.low <= min(row.open, row.close)


def _average_positive_volume(rows: list[Kline]) -> float:
    if len(rows) != VOLUME_LOOKBACK_DAYS:
        return 0
    if not all(_is_positive_finite(item.volume) for item in rows):
        return 0
    values = [item.volume for item in rows]
    return sum(values) / len(values) if values else 0


def _is_finite_number(value: float | None) -> bool:
    return value is not None and isfinite(value)


def _is_positive_finite(value: float | None) -> bool:
    return _is_finite_number(value) and value > 0


def _is_volume_breakout(context: ReplayPatternContext) -> bool:
    return context.current.close >= context.high_20 * 0.995 and context.change_pct > 1 and context.volume_ratio >= 1.3


def _is_support_rebound(context: ReplayPatternContext) -> bool:
    current = context.current
    closes_off_low = current.close >= (current.high + current.low) / 2
    holds_support = current.close >= context.low_20 * 0.98
    return current.low <= context.low_20 * 1.03 and current.close > current.open and closes_off_low and holds_support and context.volume_ratio >= 1.05


def _is_volume_pullback(context: ReplayPatternContext) -> bool:
    return context.change_pct <= -3 and context.volume_ratio >= 1.4


REPLAY_PATTERN_RULES = (
    ReplayPatternRule("放量突破", _is_volume_breakout),
    ReplayPatternRule("支撑反弹", _is_support_rebound),
    ReplayPatternRule("放量回撤", _is_volume_pullback),
)


def _replay_stats(cases: list[ReplayCase]) -> list[ReplayPatternStat]:
    grouped: dict[str, list[ReplayCase]] = defaultdict(list)
    for item in cases:
        grouped[item.pattern].append(item)
    stats: list[ReplayPatternStat] = []
    for pattern, rows in grouped.items():
        valid_returns = _valid_forward_5d_returns(rows)
        avg_return = sum(valid_returns) / len(valid_returns) if valid_returns else 0
        win_rate = _forward_return_win_rate(valid_returns)
        evaluated_count = len(valid_returns)
        stats.append(
            ReplayPatternStat(
                pattern=pattern,
                sample_count=len(rows),
                win_rate=round(win_rate, 1),
                avg_forward_5d_return=round(avg_return, 2),
                note=_replay_pattern_note(
                    pattern,
                    len(rows),
                    win_rate,
                    avg_return,
                    evaluated_count=evaluated_count,
                ),
            )
        )
    return sorted(stats, key=lambda item: (item.sample_count, item.win_rate), reverse=True)


def _forward_return_win_rate(valid_returns: list[float]) -> float:
    finite_returns = [value for value in valid_returns if _is_finite_number(value)]
    if not finite_returns:
        return 0
    return sum(1 for value in finite_returns if value > 0) / len(finite_returns) * 100


def _replay_success_rate(cases: list[ReplayCase]) -> float:
    evaluated_cases = _evaluated_replay_cases(cases)
    if not evaluated_cases:
        return 0
    return round(
        sum(1 for item in evaluated_cases if item.outcome == OUTCOME_EFFECTIVE) / len(evaluated_cases) * 100,
        1,
    )


def _evaluated_replay_cases(cases: list[ReplayCase]) -> list[ReplayCase]:
    return [item for item in cases if _is_finite_number(item.forward_5d_return)]


def _valid_forward_5d_returns(cases: list[ReplayCase]) -> list[float]:
    values: list[float] = []
    for item in cases:
        value = item.forward_5d_return
        if _is_finite_number(value):
            values.append(value)
    return values


def _replay_summary(row_count: int, cases: list[ReplayCase], success_rate: float) -> str:
    if not cases:
        return f"近{row_count}日没有识别到足够清晰的回放信号。"
    evaluated_count = len(_evaluated_replay_cases(cases))
    pending_count = len(cases) - evaluated_count
    pending_text = f"，另有 {pending_count} 个信号待确认" if pending_count else ""
    if evaluated_count < STABLE_SAMPLE_THRESHOLD:
        return (
            f"近{row_count}日识别到 {len(cases)} 个可回放信号，已完成{REPLAY_EVALUATION_DAYS}日回看 {evaluated_count} 个"
            f"{pending_text}，成熟样本偏少，只适合看案例，不宜解读为稳定胜率。"
        )
    return (
        f"近{row_count}日识别到 {len(cases)} 个可回放信号，其中 {evaluated_count} 个已走完{REPLAY_EVALUATION_DAYS}日"
        f"{pending_text}，成熟样本有效率 {success_rate:.1f}%。"
    )


def _replay_notes() -> list[str]:
    return [
        "回放只用于检验该股历史上相似信号的表现，不代表未来收益承诺。",
        f"样本少于{STABLE_SAMPLE_THRESHOLD}个时只作为案例观察，不用于提高策略置信。",
        f"未走完{REPLAY_EVALUATION_DAYS}个交易日，或缺少有效{REPLAY_EVALUATION_DAYS}日后价格的信号，会标记为待确认，不参与成熟样本胜率。",
        "后续可加入信号版本、滑点和成交约束，形成更严谨的单股验证。",
    ]


REPLAY_CASE_NOTE_TEMPLATES = {
    OUTCOME_EFFECTIVE: "{pattern}后{days}日表现偏正，后续可复核当时量能和关键价位。",
    OUTCOME_RISK: "{pattern}后{days}日内出现回撤，说明该信号在本股上需要更严格确认。",
    OUTCOME_PENDING: "{pattern}后{days}日收益样本不足，暂不纳入稳定性判断。",
    OUTCOME_RANGE: "{pattern}后{days}日内震荡，适合作为等待确认案例。",
}


def _replay_case_note(pattern: str, outcome: str) -> str:
    template = REPLAY_CASE_NOTE_TEMPLATES.get(outcome, REPLAY_CASE_NOTE_TEMPLATES[OUTCOME_RANGE])
    return template.format(pattern=pattern, days=REPLAY_EVALUATION_DAYS)


def _replay_pattern_note(
    pattern: str,
    sample_count: int,
    win_rate: float,
    avg_return: float,
    *,
    evaluated_count: int | None = None,
) -> str:
    context = _replay_pattern_note_context(
        pattern,
        sample_count,
        win_rate,
        avg_return,
        evaluated_count=evaluated_count,
    )
    return next(rule.message(context) for rule in REPLAY_PATTERN_NOTE_RULES if rule.matches(context))


def _replay_pattern_note_context(
    pattern: str,
    sample_count: int,
    win_rate: float,
    avg_return: float,
    *,
    evaluated_count: int | None,
) -> ReplayPatternNoteContext:
    completed_count = _completed_replay_count(sample_count, evaluated_count)
    return ReplayPatternNoteContext(
        pattern=pattern,
        sample_count=max(0, sample_count),
        win_rate=_finite_metric(win_rate),
        avg_return=_finite_metric(avg_return),
        completed_count=completed_count,
        pending_suffix=_pending_replay_suffix(sample_count, completed_count),
    )


def _completed_replay_count(sample_count: int, evaluated_count: int | None) -> int:
    observed_count = max(0, sample_count)
    completed_count = observed_count if evaluated_count is None else evaluated_count
    return max(0, min(completed_count, observed_count))


def _pending_replay_suffix(sample_count: int, completed_count: int) -> str:
    observed_count = max(0, sample_count)
    if observed_count <= completed_count:
        return ""
    return f"，另有 {observed_count - completed_count} 次待确认"


def _finite_metric(value: float) -> float:
    return value if _is_finite_number(value) else 0


def _has_no_completed_replay(context: ReplayPatternNoteContext) -> bool:
    return context.completed_count <= 0


def _has_small_completed_sample(context: ReplayPatternNoteContext) -> bool:
    return context.completed_count < STABLE_SAMPLE_THRESHOLD


def _has_strong_replay_history(context: ReplayPatternNoteContext) -> bool:
    return context.win_rate >= 60 and context.avg_return > 1


def _has_weak_replay_history(context: ReplayPatternNoteContext) -> bool:
    return context.win_rate < 45 or context.avg_return < 0


def _always_note(_: ReplayPatternNoteContext) -> bool:
    return True


REPLAY_PATTERN_NOTE_RULES = (
    ReplayPatternNoteRule(
        _has_no_completed_replay,
        lambda context: f"{context.pattern}尚未积累完整5日回看，只保留为待确认案例。",
    ),
    ReplayPatternNoteRule(
        _has_small_completed_sample,
        lambda context: (f"{context.pattern}已完成5日回看样本只有 {context.completed_count} 次" f"{context.pending_suffix}，不宜提高权重。"),
    ),
    ReplayPatternNoteRule(
        _has_strong_replay_history,
        lambda context: f"{context.pattern}在该股历史中相对有效，但仍需结合当前数据质量。",
    ),
    ReplayPatternNoteRule(
        _has_weak_replay_history,
        lambda context: f"{context.pattern}历史稳定性不足，触发时应降低信号权重。",
    ),
    ReplayPatternNoteRule(
        _always_note,
        lambda context: f"{context.pattern}历史表现中性，更适合当作辅助证据。",
    ),
)


ADVICE_REVIEW_RULE_VERSION = "advice-review-v2"


@dataclass(frozen=True)
class AdviceReviewWindow:
    visible_rows: list[Kline]
    forward_rows: list[Kline]
    snapshot_date: date
    as_of_cutoff: date


@dataclass(frozen=True)
class AdviceReviewForwardCoverage:
    rows: list[Kline]
    expected_dates: tuple[date, ...]
    first_missing_date: date | None

    @property
    def complete(self) -> bool:
        return self.first_missing_date is None


@dataclass(frozen=True)
class AdviceReviewBarrierOutcome:
    conclusion: str | None
    terminal_index: int | None
    target_hit: bool
    target_hit_date: str | None
    stop_hit: bool
    stop_hit_date: str | None


@dataclass(frozen=True)
class AdviceReviewPriceContext:
    adjustment_mode: str
    data_version: str
    contract_version: str
    anchor_evaluation_close: float
    scale_factor: float
    entry_price: float
    target_price: float
    stop_price: float


def evaluate_advice_forward_window(
    plan: AdviceReviewPlan,
    rows: list[Kline],
    *,
    as_of: datetime,
    evaluated_at: str,
) -> AdviceReviewEvaluationDraft:
    """Evaluate one frozen advice plan without using same-day or post-as-of bars."""

    snapshot_time = _review_market_datetime(plan.snapshot_market_time)
    if as_of < snapshot_time:
        raise ValueError("as_of 不能早于 advice snapshot 的 market_time")
    window = advice_review_window(rows, snapshot_time=snapshot_time, as_of=as_of)
    coverage = _advice_review_forward_coverage(window, plan.horizon_days)
    prices = _advice_review_price_context(plan, rows)
    barrier = _advice_review_barrier_outcome(coverage.rows, prices)
    evaluation_rows = _terminal_review_rows(coverage.rows, barrier.terminal_index)
    status, conclusion = _advice_review_status_and_conclusion(
        coverage,
        evaluation_rows,
        plan.horizon_days,
        barrier.conclusion,
        prices,
    )
    metrics = _advice_review_metrics(evaluation_rows, prices.entry_price) if prices else (None, None, None)
    return _advice_review_evaluation_draft(plan, window, evaluation_rows, barrier, prices, status, conclusion, metrics, as_of, evaluated_at)


def _advice_review_evaluation_draft(
    plan: AdviceReviewPlan,
    window: AdviceReviewWindow,
    evaluation_rows: list[Kline],
    barrier: AdviceReviewBarrierOutcome,
    prices: AdviceReviewPriceContext | None,
    status: str,
    conclusion: str,
    metrics: tuple[float | None, float | None, float | None],
    as_of: datetime,
    evaluated_at: str,
) -> AdviceReviewEvaluationDraft:
    return AdviceReviewEvaluationDraft(
        plan_id=plan.id,
        plan_revision=plan.revision,
        advice_id=plan.advice_id,
        symbol=plan.symbol,
        snapshot_market_time=plan.snapshot_market_time,
        as_of=as_of.strftime("%Y-%m-%d %H:%M:%S"),
        evaluated_at=evaluated_at,
        status=status,
        conclusion=conclusion,
        rule_version=ADVICE_REVIEW_RULE_VERSION,
        snapshot_adjustment_mode=plan.snapshot_adjustment_mode,
        snapshot_anchor_date=plan.snapshot_anchor_date,
        snapshot_anchor_close=plan.snapshot_anchor_close,
        snapshot_data_version=plan.snapshot_data_version,
        snapshot_contract_version=plan.snapshot_contract_version,
        evaluation_adjustment_mode=prices.adjustment_mode if prices else "unknown",
        evaluation_data_version=prices.data_version if prices else "unknown",
        evaluation_contract_version=prices.contract_version if prices else "unknown",
        anchor_evaluation_close=prices.anchor_evaluation_close if prices else None,
        price_scale_factor=prices.scale_factor if prices else None,
        normalized_entry_price=prices.entry_price if prices else None,
        normalized_target_price=prices.target_price if prices else None,
        normalized_stop_price=prices.stop_price if prices else None,
        entry_price=plan.snapshot_price,
        target_price=plan.target_price,
        stop_price=plan.stop_price,
        horizon_days=plan.horizon_days,
        visible_bar_count=len(window.visible_rows),
        visible_start_date=_first_row_date(window.visible_rows),
        visible_end_date=_last_row_date(window.visible_rows),
        available_forward_days=len(evaluation_rows),
        forward_start_date=_first_row_date(evaluation_rows),
        forward_end_date=_last_row_date(evaluation_rows),
        return_pct=metrics[0],
        max_favorable_excursion_pct=metrics[1],
        max_adverse_excursion_pct=metrics[2],
        target_hit=barrier.target_hit,
        target_hit_date=barrier.target_hit_date,
        stop_hit=barrier.stop_hit,
        stop_hit_date=barrier.stop_hit_date,
    )


def advice_review_window(
    rows: list[Kline],
    *,
    snapshot_time: datetime,
    as_of: datetime,
) -> AdviceReviewWindow:
    if as_of < snapshot_time:
        raise ValueError("as_of 不能早于 advice snapshot 的 market_time")
    dated_rows = _valid_unique_daily_rows(rows)
    snapshot_date = snapshot_time.date()
    visible_cutoff = completed_daily_bar_cutoff(snapshot_time)
    as_of_cutoff = completed_daily_bar_cutoff(as_of)
    visible = [row for row_date, row in dated_rows if row_date <= visible_cutoff]
    forward = [row for row_date, row in dated_rows if snapshot_date < row_date <= as_of_cutoff]
    return AdviceReviewWindow(
        visible_rows=visible,
        forward_rows=forward,
        snapshot_date=snapshot_date,
        as_of_cutoff=as_of_cutoff,
    )


def completed_daily_bar_cutoff(value: datetime) -> date:
    """Return the latest daily bar that is fully visible at a market-local time."""

    if value.time() >= DAILY_KLINE_PUBLISH_TIME:
        return value.date()
    return value.date() - timedelta(days=1)


def _advice_review_forward_coverage(
    window: AdviceReviewWindow,
    horizon_days: int,
) -> AdviceReviewForwardCoverage:
    expected_dates = _expected_review_dates(
        window.snapshot_date,
        window.as_of_cutoff,
        horizon_days,
    )
    rows_by_date = {_strict_daily_date(row.date): row for row in window.forward_rows}
    contiguous_rows: list[Kline] = []
    for expected_date in expected_dates:
        row = rows_by_date.get(expected_date)
        if row is None:
            return AdviceReviewForwardCoverage(contiguous_rows, expected_dates, expected_date)
        contiguous_rows.append(row)
    return AdviceReviewForwardCoverage(contiguous_rows, expected_dates, None)


def _expected_review_dates(
    snapshot_date: date,
    as_of_cutoff: date,
    horizon_days: int,
) -> tuple[date, ...]:
    current = snapshot_date
    expected: list[date] = []
    while current < as_of_cutoff and len(expected) < horizon_days:
        current += timedelta(days=1)
        if is_trading_day(current):
            expected.append(current)
    return tuple(expected)


def _valid_unique_daily_rows(rows: list[Kline]) -> list[tuple[date, Kline]]:
    by_date: dict[date, Kline] = {}
    for row in rows:
        row_date = _strict_daily_date(row.date)
        if row_date is not None and valid_kline(row):
            by_date[row_date] = row
    return sorted(by_date.items(), key=lambda item: item[0])


def _strict_daily_date(value: object) -> date | None:
    text_value = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text_value else None


def _review_market_datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise ValueError("研究计划缺少有效 snapshot_market_time") from exc


def _advice_review_price_context(plan: AdviceReviewPlan, rows: list[Kline]) -> AdviceReviewPriceContext | None:
    snapshot_anchor = _review_snapshot_anchor(plan)
    if snapshot_anchor is None:
        return None
    dated_rows = _valid_unique_daily_rows(rows)
    contract = _review_evaluation_contract(
        dated_rows,
        plan.snapshot_adjustment_mode,
        plan.snapshot_contract_version,
    )
    if contract is None:
        return None
    anchor_close = _review_evaluation_anchor_close(dated_rows, plan.snapshot_anchor_date)
    if anchor_close is None:
        return None
    scale = anchor_close / snapshot_anchor
    if not isfinite(scale) or scale <= 0:
        return None
    adjustment_mode, data_version, contract_version = contract
    return AdviceReviewPriceContext(
        adjustment_mode=adjustment_mode,
        data_version=data_version,
        contract_version=contract_version,
        anchor_evaluation_close=anchor_close,
        scale_factor=scale,
        entry_price=plan.snapshot_price * scale,
        target_price=plan.target_price * scale,
        stop_price=plan.stop_price * scale,
    )


def _review_snapshot_anchor(plan: AdviceReviewPlan) -> float | None:
    value = plan.snapshot_anchor_close
    if plan.snapshot_adjustment_mode != "qfq" or not plan.snapshot_anchor_date:
        return None
    if value is None or not isfinite(value) or value <= 0:
        return None
    return value


def _review_evaluation_contract(
    rows: list[tuple[date, Kline]],
    expected_adjustment_mode: str,
    expected_contract_version: str,
) -> tuple[str, str, str] | None:
    metadata = {(row.adjustment_mode, row.data_version, row.contract_version) for _row_date, row in rows}
    if len(metadata) != 1:
        return None
    adjustment_mode, data_version, contract_version = next(iter(metadata))
    invalid_versions = {"", "unknown", "legacy"}
    if adjustment_mode != expected_adjustment_mode:
        return None
    if data_version in invalid_versions or contract_version != expected_contract_version:
        return None
    return adjustment_mode, data_version, contract_version


def _review_evaluation_anchor_close(
    rows: list[tuple[date, Kline]],
    anchor_date: str,
) -> float | None:
    anchor = next((row for _row_date, row in rows if row.date == anchor_date), None)
    if anchor is None or not isfinite(anchor.close) or anchor.close <= 0:
        return None
    return anchor.close


def _advice_review_barrier_outcome(
    rows: list[Kline],
    prices: AdviceReviewPriceContext | None,
) -> AdviceReviewBarrierOutcome:
    if prices is None:
        return AdviceReviewBarrierOutcome(None, None, False, None, False, None)
    for index, row in enumerate(rows):
        target_hit = row.high >= prices.target_price
        stop_hit = row.low <= prices.stop_price
        if target_hit and stop_hit:
            return AdviceReviewBarrierOutcome(
                conclusion="target_stop_ambiguous",
                terminal_index=index,
                target_hit=True,
                target_hit_date=row.date,
                stop_hit=True,
                stop_hit_date=row.date,
            )
        if target_hit:
            return AdviceReviewBarrierOutcome(
                conclusion="target_hit",
                terminal_index=index,
                target_hit=True,
                target_hit_date=row.date,
                stop_hit=False,
                stop_hit_date=None,
            )
        if stop_hit:
            return AdviceReviewBarrierOutcome(
                conclusion="stop_hit",
                terminal_index=index,
                target_hit=False,
                target_hit_date=None,
                stop_hit=True,
                stop_hit_date=row.date,
            )
    return AdviceReviewBarrierOutcome(
        conclusion=None,
        terminal_index=None,
        target_hit=False,
        target_hit_date=None,
        stop_hit=False,
        stop_hit_date=None,
    )


def _terminal_review_rows(rows: list[Kline], terminal_index: int | None) -> list[Kline]:
    return rows if terminal_index is None else rows[: terminal_index + 1]


def _advice_review_status_and_conclusion(
    coverage: AdviceReviewForwardCoverage,
    rows: list[Kline],
    horizon_days: int,
    barrier_conclusion: str | None,
    prices: AdviceReviewPriceContext | None,
) -> tuple[str, str]:
    if barrier_conclusion is not None:
        return "evaluated", barrier_conclusion
    if not coverage.complete:
        return "insufficient", "insufficient_data"
    if not coverage.expected_dates:
        return "pending", "pending"
    if prices is None:
        return "insufficient", "insufficient_data"
    if len(coverage.expected_dates) >= horizon_days:
        return "evaluated", _horizon_review_conclusion(rows[-1].close, prices.entry_price)
    return "pending", "pending"


def _horizon_review_conclusion(close: float, entry_price: float) -> str:
    return_pct = pct_change(close, entry_price)
    if return_pct > 0:
        return "horizon_gain"
    if return_pct < 0:
        return "horizon_loss"
    return "horizon_flat"


def _advice_review_metrics(
    rows: list[Kline],
    entry_price: float,
) -> tuple[float | None, float | None, float | None]:
    if not rows:
        return None, None, None
    return (
        round(pct_change(rows[-1].close, entry_price), 4),
        round(max(pct_change(row.high, entry_price) for row in rows), 4),
        round(min(pct_change(row.low, entry_price) for row in rows), 4),
    )


def _first_row_date(rows: list[Kline]) -> str | None:
    return rows[0].date if rows else None


def _last_row_date(rows: list[Kline]) -> str | None:
    return rows[-1].date if rows else None
