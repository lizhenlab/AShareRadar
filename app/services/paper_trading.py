"""Deterministic, no-lookahead paper trading for frozen advice-review plans."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
import json
from math import floor

from app.models.market import Kline
from app.models.paper_trading import (
    PaperCostProfile,
    PaperEquityPointDraft,
    PaperInstrumentMetadata,
    PaperSimulationDraft,
    PaperSimulationRequest,
    PaperSimulationSummary,
    PaperStrategy,
    PaperStrategyCreate,
    PaperStrategySimulation,
    PaperTradeDraft,
    PaperTradeRuleProfile,
    PaperTradingAccount,
    PaperTradingAccountUpdate,
    PaperTradingDashboard,
    PaperTradingEventDraft,
)
from app.services.advice_review import normalize_review_as_of
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.paper_trading_costs import (
    PaperTradeCosts,
    available_cost_profiles,
    resolve_cost_profile,
    trade_costs,
)
from app.services.paper_trading_rules import (
    PAPER_TRADING_RULE_VERSION,
    DailyTradeability,
    assess_daily_tradeability,
    resolve_trade_rule_profile,
)
from app.services.research_replay import (
    completed_daily_bar_cutoff,
    normalized_advice_review_prices,
)
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME
from app.utils.market_data import valid_kline
from app.utils.market_time import market_local_naive
from app.utils.provider_errors import sanitize_provider_error


PAPER_KLINE_MIN_LIMIT = 120
PAPER_KLINE_MAX_LIMIT = 5_000
PAPER_KLINE_BUFFER_DAYS = 60
DEFAULT_BENCHMARK_SYMBOL = "000300.SH"


@dataclass(frozen=True)
class _PreparedBar:
    row: Kline
    previous_close: float | None
    rule: PaperTradeRuleProfile


@dataclass
class _PaperState:
    source: PaperStrategy
    allocation_order: int
    status: str = "pending"
    target: float | None = None
    stop: float | None = None
    entry_wait_sessions: int = 0
    entry_date: str | None = None
    entry_price: float | None = None
    quantity: int = 0
    buy_friction: float = 0
    held_sessions: int = 0
    last_price: float | None = None
    pending_exit_reason: str | None = None
    pending_exit_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    sell_friction: float = 0
    gross_realized_pnl: float | None = None
    realized_pnl: float | None = None
    return_pct: float | None = None
    rule_profile_id: str | None = None
    rule_data_degraded: bool = False
    error_message: str | None = None
    last_processed_date: str | None = None


@dataclass
class _EventRecorder:
    items: list[PaperTradingEventDraft] = field(default_factory=list)

    def add(
        self,
        state: _PaperState | None,
        event_date: str,
        event_code: str,
        category: str,
        severity: str,
        message: str,
        **details: object,
    ) -> None:
        clean_details = {
            str(key): value
            for key, value in details.items()
            if value is None or isinstance(value, (str, int, float, bool, list, dict))
        }
        self.items.append(
            PaperTradingEventDraft(
                sequence=len(self.items) + 1,
                strategy_id=state.source.id if state is not None else None,
                symbol=state.source.symbol if state is not None else None,
                event_date=event_date,
                event_code=event_code,
                category=category,
                severity=severity,
                message=message,
                details=clean_details,
            )
        )


@dataclass(frozen=True)
class _SimulationProvenance:
    strategy_hash: str
    market_hash: str
    input_fingerprint: str
    configuration: dict[str, object]
    data_start_date: str | None
    data_end_date: str | None
    data_sources: list[str]


@dataclass(frozen=True)
class _PortfolioValuation:
    market_value: float
    exit_friction: float
    realized: float
    unrealized: float


def get_paper_trading_dashboard(cache: object, *, run_id: int | None = None) -> PaperTradingDashboard:
    if run_id is None:
        return cache.paper_trading_dashboard()
    return cache.paper_trading_dashboard(run_id=run_id)


def update_paper_trading_account(
    cache: object,
    payload: PaperTradingAccountUpdate,
) -> PaperTradingAccount:
    return cache.update_paper_trading_account(payload)


def create_paper_strategy(
    cache: object,
    payload: PaperStrategyCreate,
    *,
    now: datetime | None = None,
) -> PaperStrategy:
    plan = cache.advice_review_plan(payload.plan_id)
    if plan is None:
        raise ValueError("复盘计划不存在")
    activation = market_local_naive(now) if now is not None else normalize_review_as_of(None, allow_future=True)
    return cache.create_paper_strategy(
        plan,
        payload,
        activation_market_time=activation.strftime("%Y-%m-%d %H:%M:%S"),
    )


def delete_pending_paper_strategy(cache: object, strategy_id: int) -> None:
    cache.delete_pending_paper_strategy(strategy_id)


async def run_paper_simulation(
    datahub: DataHub,
    payload: PaperSimulationRequest,
    *,
    now: datetime | None = None,
) -> PaperSimulationSummary:
    current = normalize_review_as_of(payload.as_of, now=now)
    cutoff = completed_daily_bar_cutoff(current)
    stable_as_of = datetime.combine(cutoff, DAILY_KLINE_PUBLISH_TIME)
    account, strategies = await asyncio.gather(
        run_cache_io(datahub.cache.paper_trading_account),
        run_cache_io(datahub.cache.paper_strategies),
    )
    rows_by_symbol, errors, metadata = await _paper_market_data(datahub, strategies, stable_as_of)
    benchmark_symbol = str(payload.benchmark_symbol or "").strip().upper() or None
    benchmark_rows, benchmark_error = await _benchmark_market_data(
        datahub,
        benchmark_symbol,
        stable_as_of,
        strategies,
        rows_by_symbol,
        errors,
    )
    cost_profile = resolve_cost_profile(
        payload.cost_profile or account.default_cost_profile,
        payload.cost_overrides,
    )
    draft = simulate_paper_portfolio(
        account,
        strategies,
        rows_by_symbol,
        as_of=stable_as_of,
        data_errors=errors,
        metadata_by_symbol=metadata,
        cost_profile=cost_profile,
        benchmark_symbol=benchmark_symbol,
        benchmark_rows=benchmark_rows,
        benchmark_error=benchmark_error,
    )
    dashboard = await run_cache_io(datahub.cache.save_paper_simulation, draft)
    return PaperSimulationSummary(
        run_id=dashboard.selected_run_id,
        as_of=draft.as_of,
        execution_count=draft.execution_count,
        closed_count=draft.closed_count,
        data_unavailable_count=draft.data_unavailable_count,
        dashboard=dashboard,
    )


async def _paper_market_data(
    datahub: DataHub,
    strategies: list[PaperStrategy],
    as_of: datetime,
) -> tuple[dict[str, list[Kline]], dict[str, str], dict[str, PaperInstrumentMetadata]]:
    symbols = list(dict.fromkeys(item.symbol for item in strategies))

    loaded, metadata_rows = await asyncio.gather(
        asyncio.gather(*(_load_strategy_klines(datahub, strategies, symbol, as_of) for symbol in symbols)),
        asyncio.gather(*(_load_strategy_metadata(datahub, symbol) for symbol in symbols)),
    )
    rows = {symbol: values for symbol, values, _error in loaded}
    errors = {symbol: error for symbol, _values, error in loaded if error}
    metadata = {symbol: item for symbol, item in metadata_rows if item is not None}
    return rows, errors, metadata


async def _load_strategy_klines(
    datahub: DataHub,
    strategies: list[PaperStrategy],
    symbol: str,
    as_of: datetime,
) -> tuple[str, list[Kline], str | None]:
    related = [item for item in strategies if item.symbol == symbol]
    limit = max(_paper_kline_limit(item, as_of) for item in related)
    try:
        return symbol, await datahub.kline(symbol, limit=limit, use_cache=True), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = " ".join(sanitize_provider_error(exc).split()).strip()
        return symbol, [], (message or "日K数据不可用")[:160]


async def _load_strategy_metadata(
    datahub: DataHub,
    symbol: str,
) -> tuple[str, PaperInstrumentMetadata | None]:
    loader = getattr(datahub, "stock_profile", None)
    if not callable(loader):
        return symbol, None
    try:
        profile = await loader(symbol)
    except asyncio.CancelledError:
        raise
    except Exception:
        return symbol, None
    return symbol, _instrument_metadata(symbol, profile)


def _instrument_metadata(symbol: str, profile: object | None) -> PaperInstrumentMetadata | None:
    if profile is None:
        return None
    name = str(getattr(profile, "name", "") or "").strip() or None
    return PaperInstrumentMetadata(
        symbol=symbol,
        name=name,
        market=str(getattr(profile, "market", "") or "").strip().upper() or None,
        list_date=str(getattr(profile, "list_date", "") or "").strip() or None,
        is_st=_is_current_st_name(name),
        source=str(getattr(profile, "source", "") or "").strip() or None,
        status_effective_date=None,
    )


async def _benchmark_market_data(
    datahub: DataHub,
    benchmark_symbol: str | None,
    as_of: datetime,
    strategies: list[PaperStrategy],
    rows_by_symbol: dict[str, list[Kline]],
    errors: dict[str, str],
) -> tuple[list[Kline], str | None]:
    if benchmark_symbol is None:
        return [], "未配置基准"
    if benchmark_symbol in rows_by_symbol and benchmark_symbol not in errors:
        return rows_by_symbol[benchmark_symbol], None
    if strategies:
        earliest = min(_market_date(item.activation_market_time) for item in strategies)
        limit = min(PAPER_KLINE_MAX_LIMIT, max(PAPER_KLINE_MIN_LIMIT, (as_of.date() - earliest).days + PAPER_KLINE_BUFFER_DAYS))
    else:
        limit = PAPER_KLINE_MIN_LIMIT
    try:
        return await datahub.kline(benchmark_symbol, limit=limit, use_cache=True), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = " ".join(sanitize_provider_error(exc).split()).strip()
        return [], (message or "基准日K数据不可用")[:160]


def _paper_kline_limit(strategy: PaperStrategy, as_of: datetime) -> int:
    anchor = _date_or_none(strategy.snapshot_anchor_date) or _market_date(strategy.snapshot_market_time)
    span = max(0, (as_of.date() - anchor).days)
    return min(PAPER_KLINE_MAX_LIMIT, max(PAPER_KLINE_MIN_LIMIT, span + PAPER_KLINE_BUFFER_DAYS))


def simulate_paper_portfolio(
    account: PaperTradingAccount,
    strategies: list[PaperStrategy],
    rows_by_symbol: dict[str, list[Kline]],
    *,
    as_of: datetime,
    data_errors: dict[str, str] | None = None,
    metadata_by_symbol: dict[str, PaperInstrumentMetadata] | None = None,
    cost_profile: PaperCostProfile | None = None,
    benchmark_symbol: str | None = DEFAULT_BENCHMARK_SYMBOL,
    benchmark_rows: list[Kline] | None = None,
    benchmark_error: str | None = None,
) -> PaperSimulationDraft:
    ordered = _ordered_strategies(strategies)
    profile = cost_profile or resolve_cost_profile(account.default_cost_profile)
    metadata = metadata_by_symbol or {}
    benchmark_values = benchmark_rows or []
    recorder = _EventRecorder()
    states, bars, rule_profiles = _prepare_paper_states(
        ordered,
        rows_by_symbol,
        as_of,
        data_errors or {},
        metadata,
        recorder,
    )
    benchmark = _prepare_benchmark(benchmark_values, as_of, benchmark_error)
    trades, equity = _simulate_trade_days(account, states, bars, profile, benchmark, recorder)
    simulations = [_simulation_from_state(states[item.id]) for item in ordered]
    unavailable_count = sum(item.status == "data_unavailable" for item in simulations)
    closed_count = sum(item.status == "closed" for item in simulations)
    provenance = _simulation_provenance(
        ordered,
        rows_by_symbol,
        benchmark_values,
        metadata,
        as_of,
        profile,
        rule_profiles,
        benchmark_symbol,
    )
    return _paper_simulation_draft(
        strategies=strategies,
        simulations=simulations,
        trades=trades,
        equity=equity,
        recorder=recorder,
        profile=profile,
        rule_profiles=rule_profiles,
        benchmark=benchmark,
        benchmark_symbol=benchmark_symbol,
        provenance=provenance,
        as_of=as_of,
        closed_count=closed_count,
        unavailable_count=unavailable_count,
    )


def _paper_simulation_draft(
    *,
    strategies: list[PaperStrategy],
    simulations: list[PaperStrategySimulation],
    trades: list[PaperTradeDraft],
    equity: list[PaperEquityPointDraft],
    recorder: _EventRecorder,
    profile: PaperCostProfile,
    rule_profiles: list[PaperTradeRuleProfile],
    benchmark: _BenchmarkSeries,
    benchmark_symbol: str | None,
    provenance: _SimulationProvenance,
    as_of: datetime,
    closed_count: int,
    unavailable_count: int,
) -> PaperSimulationDraft:
    return PaperSimulationDraft(
        as_of=as_of.strftime("%Y-%m-%d %H:%M:%S"),
        strategy_ids=sorted(item.id for item in strategies),
        strategies=simulations,
        trades=trades,
        events=recorder.items,
        equity_curve=equity,
        cost_profile=profile,
        rule_profiles=rule_profiles,
        benchmark_symbol=benchmark_symbol,
        benchmark_status="available" if benchmark.available else "unavailable",
        benchmark_message=None if benchmark.available else benchmark.message,
        input_fingerprint=provenance.input_fingerprint,
        strategy_snapshot_hash=provenance.strategy_hash,
        market_data_hash=provenance.market_hash,
        data_start_date=provenance.data_start_date,
        data_end_date=provenance.data_end_date,
        data_sources=provenance.data_sources,
        configuration=provenance.configuration,
        execution_count=len(trades),
        closed_count=closed_count,
        data_unavailable_count=unavailable_count,
        message=_simulation_message(len(strategies), len(trades), closed_count, unavailable_count),
    )


def _ordered_strategies(strategies: list[PaperStrategy]) -> list[PaperStrategy]:
    return sorted(
        strategies,
        key=lambda item: (
            item.activation_market_time,
            -item.priority,
            item.plan_id,
            item.id,
        ),
    )


def _simulation_provenance(
    strategies: list[PaperStrategy],
    rows_by_symbol: dict[str, list[Kline]],
    benchmark_rows: list[Kline],
    metadata: dict[str, PaperInstrumentMetadata],
    as_of: datetime,
    profile: PaperCostProfile,
    rule_profiles: list[PaperTradeRuleProfile],
    benchmark_symbol: str | None,
) -> _SimulationProvenance:
    strategy_hash = _stable_hash([_strategy_fingerprint_values(item) for item in strategies])
    market_hash = _stable_hash(_market_fingerprint_values(rows_by_symbol, benchmark_rows, metadata, as_of))
    configuration = _simulation_configuration(profile)
    fingerprint = _stable_hash(
        {
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "rule_version": PAPER_TRADING_RULE_VERSION,
            "strategy_snapshot_hash": strategy_hash,
            "market_data_hash": market_hash,
            "cost_profile": profile.model_dump(mode="json"),
            "rule_profiles": [item.model_dump(mode="json") for item in rule_profiles],
            "configuration": configuration,
            "benchmark_symbol": benchmark_symbol,
        }
    )
    start, end, sources = _data_extent_and_sources(rows_by_symbol, benchmark_rows, as_of)
    return _SimulationProvenance(strategy_hash, market_hash, fingerprint, configuration, start, end, sources)


def _simulation_configuration(profile: PaperCostProfile) -> dict[str, object]:
    return {
        "allocation_order": "activation_market_time ASC, priority DESC, plan_id ASC, strategy_id ASC",
        "entry_fill": "first eligible complete daily bar open",
        "t1": "entry-day target/stop signal is latched; exit at next sellable session open",
        "same_bar": "stop wins when target and stop are both touched",
        "daily_bar_limit": "order-book queue and intraday sequence are not reconstructed",
        "cost_profile_id": profile.profile_id,
        "cost_profile": profile.model_dump(mode="json"),
    }


def _data_extent_and_sources(
    rows_by_symbol: dict[str, list[Kline]],
    benchmark_rows: list[Kline],
    as_of: datetime,
) -> tuple[str | None, str | None, list[str]]:
    rows = [row for values in rows_by_symbol.values() for row in values]
    dates = [row.date for row in rows if _valid_row_through(row, as_of.date())]
    sources = sorted(
        {
            str(row.source or row.data_version or "unknown")
            for row in [*rows, *benchmark_rows]
            if valid_kline(row)
        }
    )
    return (min(dates) if dates else None, max(dates) if dates else None, sources)


def _valid_row_through(row: Kline, cutoff: date) -> bool:
    row_date = _date_or_none(row.date)
    return valid_kline(row) and row_date is not None and row_date <= cutoff


def _prepare_paper_states(
    strategies: list[PaperStrategy],
    rows_by_symbol: dict[str, list[Kline]],
    as_of: datetime,
    errors: dict[str, str],
    metadata: dict[str, PaperInstrumentMetadata],
    recorder: _EventRecorder,
) -> tuple[dict[int, _PaperState], dict[int, dict[str, _PreparedBar]], list[PaperTradeRuleProfile]]:
    states: dict[int, _PaperState] = {}
    bars_by_strategy: dict[int, dict[str, _PreparedBar]] = {}
    profiles: dict[str, PaperTradeRuleProfile] = {}
    for allocation_order, source in enumerate(strategies, start=1):
        state, bars = _prepare_strategy(
            source,
            allocation_order,
            rows_by_symbol.get(source.symbol, []),
            as_of,
            errors.get(source.symbol),
            metadata.get(source.symbol),
            recorder,
        )
        states[source.id] = state
        bars_by_strategy[source.id] = bars
        for item in (bar.rule for bar in bars.values()):
            profiles[item.profile_id] = item
    return states, bars_by_strategy, [profiles[key] for key in sorted(profiles)]


def _prepare_strategy(
    source: PaperStrategy,
    allocation_order: int,
    rows: list[Kline],
    as_of: datetime,
    data_error: str | None,
    metadata: PaperInstrumentMetadata | None,
    recorder: _EventRecorder,
) -> tuple[_PaperState, dict[str, _PreparedBar]]:
    state = _PaperState(source=source, allocation_order=allocation_order)
    activation_date = _market_date(source.activation_market_time)
    _record_strategy_activation(state, activation_date, recorder)
    if data_error:
        _mark_strategy_unavailable(state, as_of.date(), "market_data_unavailable", data_error, recorder)
        return state, {}
    prices = normalized_advice_review_prices(source, rows)
    if prices is None:
        _mark_strategy_unavailable(
            state,
            as_of.date(),
            "adjustment_anchor_mismatch",
            "日K复权基准与冻结计划不一致，未执行模拟撮合",
            recorder,
        )
        return state, {}
    state.target = prices.target_price
    state.stop = prices.stop_price
    cutoff = completed_daily_bar_cutoff(as_of)
    bars, degraded_reasons = _prepared_strategy_bars(source, rows, cutoff, activation_date, metadata)
    _apply_rule_degradation(state, bars, degraded_reasons, activation_date, recorder)
    if not bars and cutoff > activation_date:
        _mark_strategy_unavailable(
            state,
            cutoff,
            "completed_bar_unavailable",
            "激活后没有可用的完整日K",
            recorder,
        )
    return state, bars


def _record_strategy_activation(
    state: _PaperState,
    activation_date: date,
    recorder: _EventRecorder,
) -> None:
    recorder.add(
        state,
        activation_date.isoformat(),
        "strategy_activated",
        "lifecycle",
        "info",
        f"策略已激活，资金分配顺序为 {state.allocation_order}",
        allocation_order=state.allocation_order,
        priority=state.source.priority,
    )


def _mark_strategy_unavailable(
    state: _PaperState,
    event_date: date,
    event_code: str,
    message: str,
    recorder: _EventRecorder,
) -> None:
    state.status = "data_unavailable"
    state.error_message = message
    recorder.add(state, event_date.isoformat(), event_code, "data", "critical", message)


def _prepared_strategy_bars(
    source: PaperStrategy,
    rows: list[Kline],
    cutoff: date,
    activation_date: date,
    metadata: PaperInstrumentMetadata | None,
) -> tuple[dict[str, _PreparedBar], set[str]]:
    by_date = {
        row.date: row
        for row in rows
        if valid_kline(row)
        and (row_date := _date_or_none(row.date)) is not None
        and row_date <= cutoff
    }
    ordered_rows = [by_date[key] for key in sorted(by_date)]
    previous_close_by_date: dict[str, float | None] = {}
    previous_close: float | None = None
    for row in ordered_rows:
        previous_close_by_date[row.date] = previous_close
        previous_close = row.close
    bars: dict[str, _PreparedBar] = {}
    degraded_reasons: set[str] = set()
    for row in ordered_rows:
        row_date = _date_or_none(row.date)
        if row_date is None or row_date <= activation_date:
            continue
        rule = resolve_trade_rule_profile(source.symbol, row_date, metadata)
        bars[row.date] = _PreparedBar(row=row, previous_close=previous_close_by_date[row.date], rule=rule)
        degraded_reasons.update(rule.degradation_reasons)
    return bars, degraded_reasons


def _apply_rule_degradation(
    state: _PaperState,
    bars: dict[str, _PreparedBar],
    degraded_reasons: set[str],
    activation_date: date,
    recorder: _EventRecorder,
) -> None:
    if bars:
        state.rule_profile_id = next(iter(bars.values())).rule.profile_id
    if not degraded_reasons:
        return
    state.rule_data_degraded = True
    recorder.add(
        state,
        next(iter(bars), activation_date.isoformat()),
        "rule_data_degraded",
        "rule",
        "warning",
        "历史交易规则元数据不完整，已保留降级标记",
        reasons=sorted(degraded_reasons),
    )


def _simulate_trade_days(
    account: PaperTradingAccount,
    states: dict[int, _PaperState],
    bars: dict[int, dict[str, _PreparedBar]],
    cost_profile: PaperCostProfile,
    benchmark: _BenchmarkSeries,
    recorder: _EventRecorder,
) -> tuple[list[PaperTradeDraft], list[PaperEquityPointDraft]]:
    trade_dates = sorted({trade_date for strategy_bars in bars.values() for trade_date in strategy_bars})
    cash = account.initial_cash
    peak_equity = account.initial_cash
    trades: list[PaperTradeDraft] = []
    equity: list[PaperEquityPointDraft] = []
    if trade_dates:
        benchmark = benchmark.starting_at(trade_dates[0])
    for trade_date in trade_dates:
        cash = _process_open_positions(states, bars, trade_date, cash, cost_profile, trades, recorder)
        cash = _process_entries(
            states,
            bars,
            trade_date,
            cash,
            account.initial_cash,
            cost_profile,
            trades,
            recorder,
        )
        point, peak_equity = _paper_equity_point(
            trade_date,
            cash,
            account.initial_cash,
            cost_profile,
            states.values(),
            trades,
            benchmark,
            peak_equity,
        )
        equity.append(point)
    return trades, equity


def _process_open_positions(
    states: dict[int, _PaperState],
    bars: dict[int, dict[str, _PreparedBar]],
    trade_date: str,
    cash: float,
    cost_profile: PaperCostProfile,
    trades: list[PaperTradeDraft],
    recorder: _EventRecorder,
) -> float:
    for state in _allocation_ordered_states(states):
        prepared = bars[state.source.id].get(trade_date)
        if state.status != "open" or prepared is None:
            continue
        state.held_sessions += 1
        state.last_price = prepared.row.close
        state.last_processed_date = trade_date
        assessment = assess_daily_tradeability(
            prepared.row,
            previous_close=prepared.previous_close,
            profile=prepared.rule,
        )
        if state.pending_exit_reason:
            if not assessment.can_sell:
                _record_blocked_exit(state, prepared, assessment, recorder)
                continue
            cash += _close_position(
                state,
                prepared.row,
                prepared.row.open,
                state.pending_exit_reason,
                cost_profile,
                trades,
                recorder,
                assessment,
                "上一交易日信号因 T+1 或不可交易被延迟，本日开盘退出",
            )
            continue
        reason, price = _exit_decision(state, prepared.row)
        if reason is None or price is None:
            continue
        if not assessment.can_sell:
            state.pending_exit_reason = reason
            state.pending_exit_date = trade_date
            _record_blocked_exit(state, prepared, assessment, recorder)
            continue
        cash += _close_position(
            state,
            prepared.row,
            price,
            reason,
            cost_profile,
            trades,
            recorder,
            assessment,
            "达到退出条件并在日K模型允许的价格成交",
        )
    return cash


def _process_entries(
    states: dict[int, _PaperState],
    bars: dict[int, dict[str, _PreparedBar]],
    trade_date: str,
    cash: float,
    initial_cash: float,
    cost_profile: PaperCostProfile,
    trades: list[PaperTradeDraft],
    recorder: _EventRecorder,
) -> float:
    for state in _allocation_ordered_states(states):
        prepared = bars[state.source.id].get(trade_date)
        if state.status != "pending" or prepared is None:
            continue
        cash = _process_single_entry(
            state,
            prepared,
            trade_date,
            cash,
            initial_cash,
            cost_profile,
            trades,
            recorder,
        )
    return cash


def _process_single_entry(
    state: _PaperState,
    prepared: _PreparedBar,
    trade_date: str,
    cash: float,
    initial_cash: float,
    cost_profile: PaperCostProfile,
    trades: list[PaperTradeDraft],
    recorder: _EventRecorder,
) -> float:
    row = prepared.row
    state.entry_wait_sessions += 1
    state.last_processed_date = trade_date
    state.last_price = row.close
    open_reason = _open_barrier_reason(state, row)
    if open_reason is not None:
        _skip_entry(state, row, open_reason, recorder)
        return cash
    assessment = assess_daily_tradeability(
        row,
        previous_close=prepared.previous_close,
        profile=prepared.rule,
    )
    if not assessment.can_buy:
        _record_unbuyable_entry(state, row, assessment, recorder)
        return cash
    budget = min(cash, initial_cash * state.source.allocation_pct / 100)
    quantity = _board_lot_quantity(budget, row.open, cost_profile, prepared.rule)
    if quantity < prepared.rule.min_buy_quantity:
        _record_insufficient_cash(state, row, cash, budget, prepared.rule, recorder)
        return cash
    return _fill_entry(
        state,
        prepared,
        assessment,
        quantity,
        cash,
        cost_profile,
        trades,
        recorder,
    )


def _record_unbuyable_entry(
    state: _PaperState,
    row: Kline,
    assessment: DailyTradeability,
    recorder: _EventRecorder,
) -> None:
    recorder.add(
        state,
        row.date,
        assessment.code,
        "execution",
        "warning",
        assessment.message,
        allocation_order=state.allocation_order,
    )
    _finish_waiting_day(state, row, recorder)


def _record_insufficient_cash(
    state: _PaperState,
    row: Kline,
    cash: float,
    budget: float,
    rule: PaperTradeRuleProfile,
    recorder: _EventRecorder,
) -> None:
    state.error_message = f"可用资金不足最小申报数量 {rule.min_buy_quantity} 股，等待资金释放"
    recorder.add(
        state,
        row.date,
        "insufficient_cash",
        "execution",
        "warning",
        state.error_message,
        available_cash=round(cash, 2),
        budget=round(budget, 2),
        min_buy_quantity=rule.min_buy_quantity,
        allocation_order=state.allocation_order,
    )
    _finish_waiting_day(state, row, recorder)


def _fill_entry(
    state: _PaperState,
    prepared: _PreparedBar,
    assessment: DailyTradeability,
    quantity: int,
    cash: float,
    cost_profile: PaperCostProfile,
    trades: list[PaperTradeDraft],
    recorder: _EventRecorder,
) -> float:
    row = prepared.row
    gross = row.open * quantity
    costs = trade_costs(cost_profile, side="buy", gross_amount=gross)
    _open_position(state, row, quantity, costs.total)
    trades.append(_trade(state, "buy", row.date, row.open, quantity, gross, costs, "strategy_entry"))
    recorder.add(
        state,
        row.date,
        "buy_filled",
        "execution",
        "info",
        "在激活后的可交易完整日K开盘价买入",
        price=round(row.open, 4),
        quantity=quantity,
        total_cost=costs.total,
        rule_profile_id=prepared.rule.profile_id,
        tradeability_code=assessment.code,
        daily_bar_model_limited=assessment.model_limited,
    )
    _latch_entry_day_exit_signal(state, row, recorder)
    return cash - gross - costs.total


def _finish_waiting_day(state: _PaperState, row: Kline, recorder: _EventRecorder) -> None:
    barrier_reason = _intraday_barrier_reason(state, row)
    if barrier_reason is not None:
        _skip_entry(state, row, barrier_reason, recorder)
        return
    if state.entry_wait_sessions >= state.source.entry_expiry_sessions:
        state.status = "expired"
        state.exit_date = row.date
        state.exit_reason = "entry_expired"
        state.error_message = f"等待入场已达到 {state.source.entry_expiry_sessions} 个交易日"
        recorder.add(
            state,
            row.date,
            "entry_expired",
            "lifecycle",
            "warning",
            state.error_message,
            waited_sessions=state.entry_wait_sessions,
        )


def _open_position(state: _PaperState, row: Kline, quantity: int, friction: float) -> None:
    state.status = "open"
    state.entry_date = row.date
    state.entry_price = row.open
    state.quantity = quantity
    state.buy_friction = _money(friction)
    state.last_price = row.close
    state.error_message = None


def _latch_entry_day_exit_signal(state: _PaperState, row: Kline, recorder: _EventRecorder) -> None:
    reason = _barrier_reason(state, row)
    if reason is None:
        return
    deferred = {
        "target_hit": "t1_deferred_target",
        "stop_hit": "t1_deferred_stop",
        "target_stop_ambiguous": "t1_deferred_ambiguous",
    }[reason]
    state.pending_exit_reason = deferred
    state.pending_exit_date = row.date
    recorder.add(
        state,
        row.date,
        deferred,
        "risk",
        "critical" if reason != "target_hit" else "warning",
        "买入日触及退出条件；股票受 T+1 约束，当日不卖出，信号锁定至下一可卖交易日",
        original_signal=reason,
        target=state.target,
        stop=state.stop,
    )


def _close_position(
    state: _PaperState,
    row: Kline,
    price: float,
    reason: str,
    cost_profile: PaperCostProfile,
    trades: list[PaperTradeDraft],
    recorder: _EventRecorder,
    assessment: DailyTradeability,
    message: str,
) -> float:
    gross = price * state.quantity
    costs = trade_costs(cost_profile, side="sell", gross_amount=gross)
    proceeds = gross - costs.total
    cost_basis = (state.entry_price or 0) * state.quantity + state.buy_friction
    gross_realized = (price - (state.entry_price or 0)) * state.quantity
    realized = proceeds - cost_basis
    state.status = "closed"
    state.exit_date = row.date
    state.exit_price = price
    state.exit_reason = reason
    state.sell_friction = costs.total
    state.gross_realized_pnl = _money(gross_realized)
    state.realized_pnl = _money(realized)
    state.return_pct = round(realized / cost_basis * 100, 4) if cost_basis else 0
    state.last_price = price
    state.pending_exit_reason = None
    state.pending_exit_date = None
    trades.append(_trade(state, "sell", row.date, price, state.quantity, gross, costs, reason))
    recorder.add(
        state,
        row.date,
        "sell_filled",
        "execution",
        "info",
        message,
        price=round(price, 4),
        quantity=state.quantity,
        reason=reason,
        total_cost=costs.total,
        tradeability_code=assessment.code,
        daily_bar_model_limited=assessment.model_limited,
    )
    return proceeds


def _record_blocked_exit(
    state: _PaperState,
    prepared: _PreparedBar,
    assessment: DailyTradeability,
    recorder: _EventRecorder,
) -> None:
    recorder.add(
        state,
        prepared.row.date,
        f"exit_{assessment.code}",
        "execution",
        "critical",
        f"退出信号已锁定，但{assessment.message}",
        pending_exit_reason=state.pending_exit_reason,
    )


def _exit_decision(state: _PaperState, row: Kline) -> tuple[str | None, float | None]:
    reason = _barrier_reason(state, row)
    if reason in {"target_stop_ambiguous", "stop_hit"}:
        return reason, _stop_exit_price(state, row)
    if reason == "target_hit":
        return reason, _target_exit_price(state, row)
    if state.held_sessions >= state.source.horizon_days:
        return "horizon_close", row.close
    return None, None


def _stop_exit_price(state: _PaperState, row: Kline) -> float:
    stop = state.stop or row.open
    return min(row.open, stop) if row.open <= stop else stop


def _target_exit_price(state: _PaperState, row: Kline) -> float:
    target = state.target or row.open
    return max(row.open, target) if row.open >= target else target


def _barrier_reason(state: _PaperState, row: Kline) -> str | None:
    target_hit = row.high >= (state.target or float("inf"))
    stop_hit = row.low <= (state.stop or 0)
    if target_hit and stop_hit:
        return "target_stop_ambiguous"
    if stop_hit:
        return "stop_hit"
    if target_hit:
        return "target_hit"
    return None


def _open_barrier_reason(state: _PaperState, row: Kline) -> str | None:
    if row.open <= (state.stop or 0):
        return "invalid_before_entry"
    if row.open >= (state.target or float("inf")):
        return "target_before_entry"
    return None


def _intraday_barrier_reason(state: _PaperState, row: Kline) -> str | None:
    reason = _barrier_reason(state, row)
    if reason in {"stop_hit", "target_stop_ambiguous"}:
        return "invalid_before_entry"
    if reason == "target_hit":
        return "target_before_entry"
    return None


def _skip_entry(state: _PaperState, row: Kline, reason: str, recorder: _EventRecorder) -> None:
    messages = {
        "target_before_entry": "等待入场期间已经达到目标价，策略不再迟到追入",
        "invalid_before_entry": "等待入场期间已经触及止损，策略在入场前失效",
    }
    state.status = "skipped"
    state.exit_date = row.date
    state.exit_reason = reason
    state.last_price = row.close
    state.error_message = messages[reason]
    recorder.add(
        state,
        row.date,
        reason,
        "lifecycle",
        "warning",
        state.error_message,
        waited_sessions=state.entry_wait_sessions,
    )


@dataclass(frozen=True)
class _BenchmarkSeries:
    closes: dict[str, float]
    base_close: float | None
    available: bool
    message: str | None

    def starting_at(self, trade_date: str) -> _BenchmarkSeries:
        eligible = [key for key in self.closes if key <= trade_date]
        if not eligible:
            return _BenchmarkSeries(self.closes, None, False, "基准在模拟起始日前没有可用日K")
        return _BenchmarkSeries(self.closes, self.closes[max(eligible)], True, None)

    def values(self, trade_date: str, initial_cash: float) -> tuple[float | None, float | None]:
        if not self.available or self.base_close is None:
            return None, None
        eligible = [key for key in self.closes if key <= trade_date]
        if not eligible:
            return None, None
        close = self.closes[max(eligible)]
        value = initial_cash * close / self.base_close
        return _money(value), round((close / self.base_close - 1) * 100, 4)


def _prepare_benchmark(rows: list[Kline], as_of: datetime, error: str | None) -> _BenchmarkSeries:
    if error:
        return _BenchmarkSeries({}, None, False, error)
    cutoff = completed_daily_bar_cutoff(as_of)
    closes = {
        row.date: row.close
        for row in rows
        if valid_kline(row)
        and row.volume >= 0
        and (row_date := _date_or_none(row.date)) is not None
        and row_date <= cutoff
    }
    if not closes:
        return _BenchmarkSeries({}, None, False, "基准没有可用的完整日K")
    first = min(closes)
    return _BenchmarkSeries(dict(sorted(closes.items())), closes[first], True, None)


def _paper_equity_point(
    trade_date: str,
    cash: float,
    initial_cash: float,
    cost_profile: PaperCostProfile,
    states: Iterable[_PaperState],
    trades: list[PaperTradeDraft],
    benchmark: _BenchmarkSeries,
    peak_equity: float,
) -> tuple[PaperEquityPointDraft, float]:
    valuation = _portfolio_valuation(list(states), cost_profile)
    cumulative_cost = sum(item.friction_amount for item in trades)
    total = cash + valuation.market_value - valuation.exit_friction
    gross_total = total + cumulative_cost + valuation.exit_friction
    peak = max(peak_equity, total)
    drawdown = (total / peak - 1) * 100 if peak > 0 else 0
    benchmark_equity, benchmark_return = benchmark.values(trade_date, initial_cash)
    return_pct = (total / initial_cash - 1) * 100
    excess_return = _optional_difference(return_pct, benchmark_return)
    return (
        PaperEquityPointDraft(
            as_of_date=trade_date,
            cash_balance=_money(cash),
            market_value=_money(valuation.market_value),
            estimated_exit_friction=_money(valuation.exit_friction),
            total_equity=_money(total),
            gross_equity=_money(gross_total),
            cumulative_cost=_money(cumulative_cost + valuation.exit_friction),
            realized_pnl=_money(valuation.realized),
            unrealized_pnl=_money(valuation.unrealized),
            return_pct=round(return_pct, 4),
            gross_return_pct=round((gross_total / initial_cash - 1) * 100, 4),
            benchmark_equity=benchmark_equity,
            benchmark_return_pct=benchmark_return,
            excess_return_pct=excess_return,
            exposure_pct=round(valuation.market_value / total * 100, 4) if total > 0 else 0,
            drawdown_pct=round(drawdown, 4),
        ),
        peak,
    )


def _portfolio_valuation(
    states: list[_PaperState],
    cost_profile: PaperCostProfile,
) -> _PortfolioValuation:
    open_items = [item for item in states if item.status == "open" and item.last_price is not None]
    market_values = [(item.last_price or 0) * item.quantity for item in open_items]
    exit_friction = sum(
        trade_costs(cost_profile, side="sell", gross_amount=value).total
        for value in market_values
    )
    return _PortfolioValuation(
        market_value=sum(market_values),
        exit_friction=exit_friction,
        realized=sum(item.realized_pnl or 0 for item in states if item.status == "closed"),
        unrealized=sum(_unrealized_pnl(item, cost_profile) for item in open_items),
    )


def _optional_difference(value: float, baseline: float | None) -> float | None:
    return round(value - baseline, 4) if baseline is not None else None


def _unrealized_pnl(state: _PaperState, cost_profile: PaperCostProfile) -> float:
    market = (state.last_price or 0) * state.quantity
    cost_basis = (state.entry_price or 0) * state.quantity + state.buy_friction
    exit_cost = trade_costs(cost_profile, side="sell", gross_amount=market).total
    return market - exit_cost - cost_basis


def _trade(
    state: _PaperState,
    side: str,
    trade_date: str,
    price: float,
    quantity: int,
    gross: float,
    costs: PaperTradeCosts,
    reason: str,
) -> PaperTradeDraft:
    return PaperTradeDraft(
        strategy_id=state.source.id,
        symbol=state.source.symbol,
        side=side,
        trade_date=trade_date,
        price=round(price, 4),
        quantity=quantity,
        gross_amount=_money(gross),
        commission_amount=costs.commission,
        stamp_duty_amount=costs.stamp_duty,
        transfer_fee_amount=costs.transfer_fee,
        slippage_amount=costs.slippage,
        friction_amount=costs.total,
        reason=reason,
    )


def _simulation_from_state(state: _PaperState) -> PaperStrategySimulation:
    return PaperStrategySimulation(
        strategy_id=state.source.id,
        status=state.status,
        allocation_order=state.allocation_order,
        normalized_target_price=_price(state.target),
        normalized_stop_price=_price(state.stop),
        entry_wait_sessions=state.entry_wait_sessions,
        entry_date=state.entry_date,
        entry_price=_price(state.entry_price),
        quantity=state.quantity,
        buy_friction=state.buy_friction,
        held_sessions=state.held_sessions,
        last_price=_price(state.last_price),
        pending_exit_reason=state.pending_exit_reason,
        pending_exit_date=state.pending_exit_date,
        exit_date=state.exit_date,
        exit_price=_price(state.exit_price),
        exit_reason=state.exit_reason,
        sell_friction=state.sell_friction,
        gross_realized_pnl=state.gross_realized_pnl,
        realized_pnl=state.realized_pnl,
        return_pct=state.return_pct,
        rule_profile_id=state.rule_profile_id,
        rule_data_degraded=state.rule_data_degraded,
        error_message=state.error_message,
        last_processed_date=state.last_processed_date,
    )


def _board_lot_quantity(
    budget: float,
    price: float,
    cost_profile: PaperCostProfile,
    rule: PaperTradeRuleProfile,
) -> int:
    if budget <= 0 or price <= 0:
        return 0
    maximum = floor(budget / price)
    if maximum < rule.min_buy_quantity:
        return 0
    if rule.buy_quantity_step == 1:
        quantity = maximum
    else:
        quantity = rule.min_buy_quantity + (
            (maximum - rule.min_buy_quantity) // rule.buy_quantity_step
        ) * rule.buy_quantity_step
    while quantity >= rule.min_buy_quantity:
        gross = quantity * price
        if gross + trade_costs(cost_profile, side="buy", gross_amount=gross).total <= budget:
            return quantity
        quantity -= rule.buy_quantity_step
    return 0


def _allocation_ordered_states(states: dict[int, _PaperState]) -> list[_PaperState]:
    return sorted(states.values(), key=lambda item: item.allocation_order)


def _strategy_fingerprint_values(item: PaperStrategy) -> dict[str, object]:
    return {
        "id": item.id,
        "plan_id": item.plan_id,
        "plan_revision": item.plan_revision,
        "advice_id": item.advice_id,
        "symbol": item.symbol,
        "activation_market_time": item.activation_market_time,
        "allocation_pct": item.allocation_pct,
        "priority": item.priority,
        "entry_expiry_sessions": item.entry_expiry_sessions,
        "snapshot_market_time": item.snapshot_market_time,
        "snapshot_price": item.snapshot_price,
        "snapshot_adjustment_mode": item.snapshot_adjustment_mode,
        "snapshot_anchor_date": item.snapshot_anchor_date,
        "snapshot_anchor_close": item.snapshot_anchor_close,
        "snapshot_data_version": item.snapshot_data_version,
        "snapshot_contract_version": item.snapshot_contract_version,
        "target_price": item.target_price,
        "stop_price": item.stop_price,
        "horizon_days": item.horizon_days,
    }


def _market_fingerprint_values(
    rows_by_symbol: dict[str, list[Kline]],
    benchmark_rows: list[Kline],
    metadata: dict[str, PaperInstrumentMetadata],
    as_of: datetime,
) -> dict[str, object]:
    cutoff = completed_daily_bar_cutoff(as_of)

    def rows(values: list[Kline]) -> list[dict[str, object]]:
        return [
            {
                "date": item.date,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
                "adjustment_mode": item.adjustment_mode,
                "data_version": item.data_version,
                "contract_version": item.contract_version,
                "source": item.source,
            }
            for item in sorted(values, key=lambda value: value.date)
            if valid_kline(item)
            and (row_date := _date_or_none(item.date)) is not None
            and row_date <= cutoff
        ]

    return {
        "symbols": {symbol: rows(values) for symbol, values in sorted(rows_by_symbol.items())},
        "benchmark": rows(benchmark_rows),
        "metadata": {
            symbol: item.model_dump(mode="json")
            for symbol, item in sorted(metadata.items())
        },
    }


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _is_current_st_name(name: str | None) -> bool | None:
    if not name:
        return None
    normalized = name.upper().replace(" ", "")
    return normalized.startswith(("ST", "*ST", "S*ST"))


def _market_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("模拟策略缺少有效市场时间") from exc


def _date_or_none(value: object) -> date | None:
    try:
        parsed = date.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.isoformat() == str(value) else None


def _simulation_message(strategies: int, executions: int, closed: int, unavailable: int) -> str:
    return f"已重放 {strategies} 条策略，生成 {executions} 笔成交，平仓 {closed} 条，数据不可用 {unavailable} 条"


def _money(value: float) -> float:
    return round(value, 2)


def _price(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


__all__ = [
    "DEFAULT_BENCHMARK_SYMBOL",
    "available_cost_profiles",
    "create_paper_strategy",
    "delete_pending_paper_strategy",
    "get_paper_trading_dashboard",
    "run_paper_simulation",
    "simulate_paper_portfolio",
    "update_paper_trading_account",
]
