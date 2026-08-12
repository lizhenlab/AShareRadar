"""Persistence for immutable deterministic local paper-trading runs."""

from __future__ import annotations

import json
import sqlite3

from app.models.paper_trading import (
    PaperEquityPoint,
    PaperEquityPointDraft,
    PaperRunComparison,
    PaperRunExport,
    PaperSimulationDraft,
    PaperStrategy,
    PaperStrategyCreate,
    PaperStrategySimulation,
    PaperTrade,
    PaperTradeDraft,
    PaperTradingAccount,
    PaperTradingAccountUpdate,
    PaperTradingDashboard,
    PaperTradingEvent,
    PaperTradingEventDraft,
    PaperTradingRun,
)
from app.models.paper_trading_config import (
    DEFAULT_PAPER_ACCOUNT_NAME,
    DEFAULT_PAPER_INITIAL_CASH,
    MODELLED_ONE_WAY_FRICTION_PCT,
    PAPER_TRADING_RULE_VERSION,
)
from app.models.paper_trading_costs import available_cost_profiles
from app.models.reviews import AdviceReviewPlan
from app.repositories.base import SQLiteRepository
from app.repositories.paper_trading_metrics import (
    ClosedMetrics,
    LatestMetrics,
    build_positions,
    calculate_closed_metrics,
    calculate_performance,
    calculate_risk_metrics,
    cost_profile_from_run,
    drawdown_durations,
    exposure_metrics,
    latest_metrics,
    optional_metric_delta,
    payoff_ratio,
    sample_warning,
)
from app.utils.audit_time import audit_now_text
from app.utils.errors import NotFoundError


PAPER_TRADING_NOTES = (
    "模拟策略只使用激活日之后的完整日K，绝不回填激活前成交。",
    "股票执行 T+1；买入日触及止盈或止损只锁定信号，下一可卖交易日才执行。",
    "停牌、零成交量、一字涨停买入和一字跌停卖出均不撮合，并保留逐日事件。",
    "佣金、最低佣金、印花税、过户费和滑点分别建模；模型成本不等于真实券商费率。",
    "日K无法还原盘中先后顺序、订单簿深度和排队位置；同日双触发继续按止损优先。",
)


_ClosedMetrics = ClosedMetrics
_LatestMetrics = LatestMetrics
_performance = calculate_performance
_closed_metrics = calculate_closed_metrics
_payoff_ratio = payoff_ratio
_latest_metrics = latest_metrics
_exposure_metrics = exposure_metrics
_sample_warning = sample_warning
_risk_metrics = calculate_risk_metrics
_drawdown_durations = drawdown_durations
_positions = build_positions
_cost_profile_from_run = cost_profile_from_run
_optional_delta = optional_metric_delta


class PaperTradingRepository(SQLiteRepository):
    def account(self) -> PaperTradingAccount:
        with self._lock, self._connect() as conn:
            row = _account_row(conn, create=True)
        return _account_from_row(_required_row(row, "模拟账户初始化失败"))

    def update_account(self, payload: PaperTradingAccountUpdate) -> PaperTradingAccount:
        timestamp = audit_now_text()
        with self._lock, self._connect() as conn:
            _account_row(conn, create=True)
            if payload.initial_cash is not None:
                strategy_count = int(conn.execute("SELECT COUNT(*) FROM paper_strategy").fetchone()[0])
                if strategy_count:
                    raise ValueError("已有模拟策略时不能修改初始资金")
                conn.execute(
                    "UPDATE paper_trading_account SET initial_cash = ?, updated_at = ? WHERE id = 1",
                    (payload.initial_cash, timestamp),
                )
            if payload.default_cost_profile is not None:
                conn.execute(
                    "UPDATE paper_trading_account SET default_cost_profile = ?, updated_at = ? WHERE id = 1",
                    (payload.default_cost_profile, timestamp),
                )
            row = conn.execute("SELECT * FROM paper_trading_account WHERE id = 1").fetchone()
        return _account_from_row(_required_row(row, "模拟账户更新失败"))

    def create_strategy(
        self,
        plan: AdviceReviewPlan,
        payload: PaperStrategyCreate,
        *,
        activation_market_time: str,
    ) -> PaperStrategy:
        timestamp = audit_now_text()
        with self._lock, self._connect() as conn:
            _account_row(conn, create=True)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO paper_strategy (
                        plan_id, plan_revision, advice_id, symbol,
                        activation_market_time, allocation_pct, priority,
                        entry_expiry_sessions, snapshot_market_time, snapshot_price,
                        snapshot_adjustment_mode, snapshot_anchor_date,
                        snapshot_anchor_close, snapshot_data_version,
                        snapshot_contract_version, target_price, stop_price,
                        horizon_days, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        plan.id,
                        plan.revision,
                        plan.advice_id,
                        plan.symbol,
                        activation_market_time,
                        payload.allocation_pct,
                        payload.priority,
                        payload.entry_expiry_sessions,
                        plan.snapshot_market_time,
                        plan.snapshot_price,
                        plan.snapshot_adjustment_mode,
                        plan.snapshot_anchor_date,
                        plan.snapshot_anchor_close,
                        plan.snapshot_data_version,
                        plan.snapshot_contract_version,
                        plan.target_price,
                        plan.stop_price,
                        plan.horizon_days,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "paper_strategy.plan_id, paper_strategy.plan_revision" in str(exc):
                    raise ValueError("该复盘计划版本已加入模拟交易") from exc
                raise
            row = conn.execute("SELECT * FROM paper_strategy WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _strategy_from_row(_required_row(row, "模拟策略创建失败"))

    def delete_pending_strategy(self, strategy_id: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM paper_strategy WHERE id = ?", (strategy_id,)).fetchone()
            if row is None:
                raise NotFoundError("模拟策略不存在")
            result_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM paper_strategy_result WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()[0]
            )
            if result_count:
                raise ValueError("策略已进入不可变历史运行，不能删除")
            cursor = conn.execute("DELETE FROM paper_strategy WHERE id = ?", (strategy_id,))
        return cursor.rowcount > 0

    def strategies(self) -> list[PaperStrategy]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM paper_strategy
                ORDER BY activation_market_time ASC, priority DESC, plan_id ASC, id ASC
                """
            ).fetchall()
        return [_strategy_from_row(row) for row in rows]

    def save_simulation(self, draft: PaperSimulationDraft) -> PaperTradingDashboard:
        timestamp = audit_now_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _account_row(conn, create=True)
            _validate_simulation_strategy_ids(conn, draft.strategy_ids)
            run_id = _insert_paper_run(conn, draft, timestamp)
            _insert_strategy_results(conn, run_id, draft.strategies, timestamp)
            _insert_paper_trades(conn, run_id, draft.trades, timestamp)
            _insert_paper_equity(conn, run_id, draft.equity_curve, timestamp)
            _insert_paper_events(conn, run_id, draft.events, timestamp)
        return self.dashboard(run_id=run_id)

    def dashboard(self, *, run_id: int | None = None) -> PaperTradingDashboard:
        with self._lock, self._read_snapshot() as conn:
            account = _account_from_row(_required_row(_account_row(conn, create=False), "模拟账户不存在"))
            runs = [_run_from_row(row) for row in conn.execute("SELECT * FROM paper_trading_run ORDER BY id DESC LIMIT 100").fetchall()]
            selected_id = run_id if run_id is not None else (runs[0].id if runs else None)
            if run_id is not None and not any(item.id == run_id for item in runs):
                row = conn.execute("SELECT * FROM paper_trading_run WHERE id = ?", (run_id,)).fetchone()
                if row is None:
                    raise NotFoundError("模拟运行不存在")
                runs.append(_run_from_row(row))
            sources = [_strategy_from_row(row) for row in conn.execute("SELECT * FROM paper_strategy ORDER BY id ASC").fetchall()]
            strategies = _strategies_for_run(conn, sources, selected_id)
            trades = _trades_for_run(conn, selected_id)
            events = _events_for_run(conn, selected_id)
            equity = _equity_for_run(conn, selected_id)
            selected_run = next((item for item in runs if item.id == selected_id), None)
        profile = _cost_profile_from_run(selected_run)
        return PaperTradingDashboard(
            account=account,
            performance=_performance(account, strategies, equity, trades),
            strategies=sorted(strategies, key=lambda item: (item.allocation_order or 1_000_000, item.id)),
            positions=_positions(strategies, profile),
            trades=trades,
            events=events,
            equity_curve=equity,
            latest_run=runs[0] if runs else None,
            selected_run_id=selected_id,
            runs=sorted(runs, key=lambda item: item.id, reverse=True),
            cost_profiles=available_cost_profiles(),
            notes=list(PAPER_TRADING_NOTES),
        )

    def runs(self, *, limit: int = 100) -> list[PaperTradingRun]:
        with self._lock, self._read_snapshot() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trading_run ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def run_export(self, run_id: int) -> PaperRunExport:
        dashboard = self.dashboard(run_id=run_id)
        selected = next((item for item in dashboard.runs if item.id == run_id), None)
        if selected is None:
            raise NotFoundError("模拟运行不存在")
        return PaperRunExport(
            run=selected,
            performance=dashboard.performance,
            strategies=dashboard.strategies,
            trades=dashboard.trades,
            events=dashboard.events,
            equity_curve=dashboard.equity_curve,
        )

    def compare_runs(self, left_run_id: int, right_run_id: int) -> PaperRunComparison:
        left = self.run_export(left_run_id)
        right = self.run_export(right_run_id)
        fields = (
            "total_return_pct",
            "gross_return_pct",
            "benchmark_return_pct",
            "excess_return_pct",
            "max_drawdown_pct",
            "total_cost",
            "cost_to_gross_profit_pct",
            "win_rate_pct",
            "payoff_ratio",
            "expectancy",
            "profit_factor",
            "turnover_pct",
            "average_exposure_pct",
        )
        deltas = {
            name: _optional_delta(
                getattr(right.performance, name),
                getattr(left.performance, name),
            )
            for name in fields
        }
        return PaperRunComparison(
            left_run=left.run,
            right_run=right.run,
            left_performance=left.performance,
            right_performance=right.performance,
            deltas=deltas,
        )


def _validate_simulation_strategy_ids(conn: sqlite3.Connection, expected_ids: list[int]) -> None:
    current_ids = [int(row[0]) for row in conn.execute("SELECT id FROM paper_strategy ORDER BY id").fetchall()]
    if current_ids != sorted(expected_ids):
        raise RuntimeError("模拟策略在计算期间已变化，请重新运行")


def _insert_paper_run(conn: sqlite3.Connection, draft: PaperSimulationDraft, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO paper_trading_run (
            as_of, rule_version, modelled_one_way_friction_pct,
            cost_profile_id, cost_profile_name, cost_profile_version,
            benchmark_symbol, benchmark_status, benchmark_message,
            strategy_count, execution_count, closed_count,
            data_unavailable_count, input_fingerprint,
            strategy_snapshot_hash, market_data_hash, data_start_date,
            data_end_date, configuration_json, rule_profiles_json,
            data_sources_json, message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft.as_of,
            PAPER_TRADING_RULE_VERSION,
            0,
            draft.cost_profile.profile_id,
            draft.cost_profile.name,
            draft.cost_profile.version,
            draft.benchmark_symbol,
            draft.benchmark_status,
            draft.benchmark_message,
            len(draft.strategy_ids),
            draft.execution_count,
            draft.closed_count,
            draft.data_unavailable_count,
            draft.input_fingerprint,
            draft.strategy_snapshot_hash,
            draft.market_data_hash,
            draft.data_start_date,
            draft.data_end_date,
            _json_dump(draft.configuration),
            _json_dump([item.model_dump(mode="json") for item in draft.rule_profiles]),
            _json_dump(draft.data_sources),
            draft.message,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def _insert_strategy_results(
    conn: sqlite3.Connection,
    run_id: int,
    strategies: list[PaperStrategySimulation],
    timestamp: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO paper_strategy_result (
            run_id, strategy_id, status, allocation_order,
            normalized_target_price, normalized_stop_price, entry_wait_sessions,
            entry_date, entry_price, quantity, buy_friction, held_sessions,
            last_price, pending_exit_reason, pending_exit_date, exit_date,
            exit_price, exit_reason, sell_friction, gross_realized_pnl,
            realized_pnl, return_pct, rule_profile_id, rule_data_degraded,
            error_message, last_processed_date, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.strategy_id,
                item.status,
                item.allocation_order,
                item.normalized_target_price,
                item.normalized_stop_price,
                item.entry_wait_sessions,
                item.entry_date,
                item.entry_price,
                item.quantity,
                item.buy_friction,
                item.held_sessions,
                item.last_price,
                item.pending_exit_reason,
                item.pending_exit_date,
                item.exit_date,
                item.exit_price,
                item.exit_reason,
                item.sell_friction,
                item.gross_realized_pnl,
                item.realized_pnl,
                item.return_pct,
                item.rule_profile_id,
                int(item.rule_data_degraded),
                item.error_message,
                item.last_processed_date,
                timestamp,
            )
            for item in strategies
        ],
    )


def _insert_paper_trades(
    conn: sqlite3.Connection,
    run_id: int,
    trades: list[PaperTradeDraft],
    timestamp: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO paper_trade (
            run_id, strategy_id, symbol, side, trade_date, price, quantity,
            gross_amount, commission_amount, stamp_duty_amount,
            transfer_fee_amount, slippage_amount, friction_amount, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.strategy_id,
                item.symbol,
                item.side,
                item.trade_date,
                item.price,
                item.quantity,
                item.gross_amount,
                item.commission_amount,
                item.stamp_duty_amount,
                item.transfer_fee_amount,
                item.slippage_amount,
                item.friction_amount,
                item.reason,
                timestamp,
            )
            for item in trades
        ],
    )


def _insert_paper_equity(
    conn: sqlite3.Connection,
    run_id: int,
    points: list[PaperEquityPointDraft],
    timestamp: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO paper_equity_snapshot (
            run_id, as_of_date, cash_balance, market_value,
            estimated_exit_friction, total_equity, gross_equity,
            cumulative_cost, realized_pnl, unrealized_pnl, return_pct,
            gross_return_pct, benchmark_equity, benchmark_return_pct,
            excess_return_pct, exposure_pct, drawdown_pct, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.as_of_date,
                item.cash_balance,
                item.market_value,
                item.estimated_exit_friction,
                item.total_equity,
                item.gross_equity,
                item.cumulative_cost,
                item.realized_pnl,
                item.unrealized_pnl,
                item.return_pct,
                item.gross_return_pct,
                item.benchmark_equity,
                item.benchmark_return_pct,
                item.excess_return_pct,
                item.exposure_pct,
                item.drawdown_pct,
                timestamp,
            )
            for item in points
        ],
    )


def _insert_paper_events(
    conn: sqlite3.Connection,
    run_id: int,
    events: list[PaperTradingEventDraft],
    timestamp: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO paper_trading_event (
            run_id, sequence, strategy_id, symbol, event_date, event_code,
            category, severity, message, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.sequence,
                item.strategy_id,
                item.symbol,
                item.event_date,
                item.event_code,
                item.category,
                item.severity,
                item.message,
                _json_dump(item.details),
                timestamp,
            )
            for item in events
        ],
    )


def _account_row(conn: sqlite3.Connection, *, create: bool) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM paper_trading_account WHERE id = 1").fetchone()
    if row is not None or not create:
        return row
    timestamp = audit_now_text()
    conn.execute(
        """
        INSERT INTO paper_trading_account (
            id, name, initial_cash, modelled_one_way_friction_pct,
            default_cost_profile, created_at, updated_at
        ) VALUES (1, ?, ?, ?, 'base', ?, ?)
        """,
        (
            DEFAULT_PAPER_ACCOUNT_NAME,
            DEFAULT_PAPER_INITIAL_CASH,
            MODELLED_ONE_WAY_FRICTION_PCT,
            timestamp,
            timestamp,
        ),
    )
    return conn.execute("SELECT * FROM paper_trading_account WHERE id = 1").fetchone()


def _account_from_row(row: sqlite3.Row) -> PaperTradingAccount:
    return PaperTradingAccount(
        id=int(row["id"]),
        name=str(row["name"]),
        initial_cash=float(row["initial_cash"]),
        modelled_one_way_friction_pct=float(row["modelled_one_way_friction_pct"]),
        default_cost_profile=str(row["default_cost_profile"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _strategy_from_row(row: sqlite3.Row) -> PaperStrategy:
    return PaperStrategy(
        id=int(row["id"]),
        plan_id=int(row["plan_id"]),
        plan_revision=int(row["plan_revision"]),
        advice_id=int(row["advice_id"]),
        symbol=str(row["symbol"]),
        activation_market_time=str(row["activation_market_time"]),
        allocation_pct=float(row["allocation_pct"]),
        priority=int(row["priority"]),
        entry_expiry_sessions=int(row["entry_expiry_sessions"]),
        snapshot_market_time=str(row["snapshot_market_time"]),
        snapshot_price=float(row["snapshot_price"]),
        snapshot_adjustment_mode=str(row["snapshot_adjustment_mode"]),
        snapshot_anchor_date=str(row["snapshot_anchor_date"]) if row["snapshot_anchor_date"] is not None else None,
        snapshot_anchor_close=_optional_float(row, "snapshot_anchor_close"),
        snapshot_data_version=str(row["snapshot_data_version"]),
        snapshot_contract_version=str(row["snapshot_contract_version"]),
        target_price=float(row["target_price"]),
        stop_price=float(row["stop_price"]),
        horizon_days=int(row["horizon_days"]),
        status=str(row["status"]),
        normalized_target_price=_optional_float(row, "normalized_target_price"),
        normalized_stop_price=_optional_float(row, "normalized_stop_price"),
        entry_date=str(row["entry_date"]) if row["entry_date"] is not None else None,
        entry_price=_optional_float(row, "entry_price"),
        quantity=int(row["quantity"]),
        buy_friction=float(row["buy_friction"]),
        held_sessions=int(row["held_sessions"]),
        last_price=_optional_float(row, "last_price"),
        exit_date=str(row["exit_date"]) if row["exit_date"] is not None else None,
        exit_price=_optional_float(row, "exit_price"),
        exit_reason=str(row["exit_reason"]) if row["exit_reason"] is not None else None,
        sell_friction=float(row["sell_friction"]),
        realized_pnl=_optional_float(row, "realized_pnl"),
        return_pct=_optional_float(row, "return_pct"),
        error_message=str(row["error_message"]) if row["error_message"] is not None else None,
        last_processed_date=str(row["last_processed_date"]) if row["last_processed_date"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _strategies_for_run(
    conn: sqlite3.Connection,
    sources: list[PaperStrategy],
    run_id: int | None,
) -> list[PaperStrategy]:
    if run_id is None:
        return sources
    rows = conn.execute(
        "SELECT * FROM paper_strategy_result WHERE run_id = ? ORDER BY allocation_order, strategy_id",
        (run_id,),
    ).fetchall()
    by_strategy = {int(row["strategy_id"]): row for row in rows}
    return [
        _strategy_with_result(item, by_strategy.get(item.id))
        for item in sources
        if item.id in by_strategy
    ]


def _strategy_with_result(source: PaperStrategy, row: sqlite3.Row | None) -> PaperStrategy:
    if row is None:
        return source
    return source.model_copy(
        update={
            "status": str(row["status"]),
            "allocation_order": int(row["allocation_order"]) if row["allocation_order"] is not None else None,
            "normalized_target_price": _optional_float(row, "normalized_target_price"),
            "normalized_stop_price": _optional_float(row, "normalized_stop_price"),
            "entry_wait_sessions": int(row["entry_wait_sessions"]),
            "entry_date": _optional_text(row, "entry_date"),
            "entry_price": _optional_float(row, "entry_price"),
            "quantity": int(row["quantity"]),
            "buy_friction": float(row["buy_friction"]),
            "held_sessions": int(row["held_sessions"]),
            "last_price": _optional_float(row, "last_price"),
            "pending_exit_reason": _optional_text(row, "pending_exit_reason"),
            "pending_exit_date": _optional_text(row, "pending_exit_date"),
            "exit_date": _optional_text(row, "exit_date"),
            "exit_price": _optional_float(row, "exit_price"),
            "exit_reason": _optional_text(row, "exit_reason"),
            "sell_friction": float(row["sell_friction"]),
            "gross_realized_pnl": _optional_float(row, "gross_realized_pnl"),
            "realized_pnl": _optional_float(row, "realized_pnl"),
            "return_pct": _optional_float(row, "return_pct"),
            "rule_profile_id": _optional_text(row, "rule_profile_id"),
            "rule_data_degraded": bool(row["rule_data_degraded"]),
            "error_message": _optional_text(row, "error_message"),
            "last_processed_date": _optional_text(row, "last_processed_date"),
        }
    )


def _trades_for_run(conn: sqlite3.Connection, run_id: int | None) -> list[PaperTrade]:
    if run_id is None:
        return []
    rows = conn.execute(
        "SELECT * FROM paper_trade WHERE run_id = ? ORDER BY trade_date DESC, id DESC",
        (run_id,),
    ).fetchall()
    return [_trade_from_row(row) for row in rows]


def _events_for_run(conn: sqlite3.Connection, run_id: int | None) -> list[PaperTradingEvent]:
    if run_id is None:
        return []
    rows = conn.execute(
        "SELECT * FROM paper_trading_event WHERE run_id = ? ORDER BY sequence ASC",
        (run_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def _equity_for_run(conn: sqlite3.Connection, run_id: int | None) -> list[PaperEquityPoint]:
    if run_id is None:
        return []
    rows = conn.execute(
        "SELECT * FROM paper_equity_snapshot WHERE run_id = ? ORDER BY as_of_date ASC",
        (run_id,),
    ).fetchall()
    return [_equity_from_row(row) for row in rows]


def _trade_from_row(row: sqlite3.Row) -> PaperTrade:
    return PaperTrade(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        strategy_id=int(row["strategy_id"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        trade_date=str(row["trade_date"]),
        price=float(row["price"]),
        quantity=int(row["quantity"]),
        gross_amount=float(row["gross_amount"]),
        commission_amount=float(row["commission_amount"]),
        stamp_duty_amount=float(row["stamp_duty_amount"]),
        transfer_fee_amount=float(row["transfer_fee_amount"]),
        slippage_amount=float(row["slippage_amount"]),
        friction_amount=float(row["friction_amount"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> PaperTradingEvent:
    details = _json_load(str(row["details_json"]), {})
    return PaperTradingEvent(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        sequence=int(row["sequence"]),
        strategy_id=int(row["strategy_id"]) if row["strategy_id"] is not None else None,
        symbol=str(row["symbol"]) if row["symbol"] is not None else None,
        event_date=str(row["event_date"]),
        event_code=str(row["event_code"]),
        category=str(row["category"]),
        severity=str(row["severity"]),
        message=str(row["message"]),
        details=details if isinstance(details, dict) else {},
        created_at=str(row["created_at"]),
    )


def _equity_from_row(row: sqlite3.Row) -> PaperEquityPoint:
    return PaperEquityPoint(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        as_of_date=str(row["as_of_date"]),
        cash_balance=float(row["cash_balance"]),
        market_value=float(row["market_value"]),
        estimated_exit_friction=float(row["estimated_exit_friction"]),
        total_equity=float(row["total_equity"]),
        gross_equity=float(row["gross_equity"]),
        cumulative_cost=float(row["cumulative_cost"]),
        realized_pnl=float(row["realized_pnl"]),
        unrealized_pnl=float(row["unrealized_pnl"]),
        return_pct=float(row["return_pct"]),
        gross_return_pct=float(row["gross_return_pct"]),
        benchmark_equity=_optional_float(row, "benchmark_equity"),
        benchmark_return_pct=_optional_float(row, "benchmark_return_pct"),
        excess_return_pct=_optional_float(row, "excess_return_pct"),
        exposure_pct=float(row["exposure_pct"]),
        drawdown_pct=float(row["drawdown_pct"]),
        created_at=str(row["created_at"]),
    )


def _optional_float(row: sqlite3.Row, name: str) -> float | None:
    return float(row[name]) if row[name] is not None else None


def _optional_text(row: sqlite3.Row, name: str) -> str | None:
    return str(row[name]) if row[name] is not None else None


def _run_from_row(row: sqlite3.Row) -> PaperTradingRun:
    configuration = _json_load(str(row["configuration_json"]), {})
    profiles = _json_load(str(row["rule_profiles_json"]), [])
    sources = _json_load(str(row["data_sources_json"]), [])
    return PaperTradingRun(
        id=int(row["id"]),
        as_of=str(row["as_of"]),
        rule_version=str(row["rule_version"]),
        modelled_one_way_friction_pct=float(row["modelled_one_way_friction_pct"]),
        cost_profile_id=str(row["cost_profile_id"]),
        cost_profile_name=str(row["cost_profile_name"]),
        cost_profile_version=str(row["cost_profile_version"]),
        benchmark_symbol=str(row["benchmark_symbol"]) if row["benchmark_symbol"] is not None else None,
        benchmark_status=str(row["benchmark_status"]),
        benchmark_message=str(row["benchmark_message"]) if row["benchmark_message"] is not None else None,
        strategy_count=int(row["strategy_count"]),
        execution_count=int(row["execution_count"]),
        closed_count=int(row["closed_count"]),
        data_unavailable_count=int(row["data_unavailable_count"]),
        input_fingerprint=str(row["input_fingerprint"]),
        strategy_snapshot_hash=str(row["strategy_snapshot_hash"]),
        market_data_hash=str(row["market_data_hash"]),
        data_start_date=str(row["data_start_date"]) if row["data_start_date"] is not None else None,
        data_end_date=str(row["data_end_date"]) if row["data_end_date"] is not None else None,
        configuration=configuration if isinstance(configuration, dict) else {},
        rule_profiles=profiles if isinstance(profiles, list) else [],
        data_sources=[str(item) for item in sources] if isinstance(sources, list) else [],
        message=str(row["message"]),
        created_at=str(row["created_at"]),
    )


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str, fallback: object) -> object:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _required_row(row: sqlite3.Row | None, message: str) -> sqlite3.Row:
    if row is None:
        raise RuntimeError(message)
    return row


__all__ = [
    "DEFAULT_PAPER_INITIAL_CASH",
    "MODELLED_ONE_WAY_FRICTION_PCT",
    "PAPER_TRADING_NOTES",
    "PAPER_TRADING_RULE_VERSION",
    "PaperTradingRepository",
]
