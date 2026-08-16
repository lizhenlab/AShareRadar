from __future__ import annotations

from datetime import date, datetime

from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline, UNKNOWN_KLINE_DATA_VERSION
from app.services.trading_calendar import (
    DAILY_KLINE_PUBLISH_TIME,
    TradingCalendarCoverageError,
    trading_dates_between,
)
from app.utils.market_data import finite_float, valid_kline
from app.utils.market_time import market_local_naive


FACTOR_EXECUTION_METADATA_VERSION = "factor-execution-evidence.v1"
_INVALID_VERSIONS = {"", "unknown", "legacy"}


def factor_calibration_evidence_issue(rows: list[Kline]) -> str | None:
    if not rows:
        return None
    dates = [_strict_date(row.date) for row in rows]
    if any(row_date is None for row_date in dates):
        return "日K日期不是严格ISO交易日"
    observed_dates = [row_date for row_date in dates if row_date is not None]
    if observed_dates != sorted(observed_dates):
        return "日K未按交易日严格递增"
    if len(set(observed_dates)) != len(observed_dates):
        return "日K存在重复交易日"
    try:
        expected_dates = trading_dates_between(observed_dates[0], observed_dates[-1])
    except TradingCalendarCoverageError:
        return "可信交易日历不覆盖校准窗口"
    if tuple(observed_dates) != expected_dates:
        return "日K没有覆盖校准窗口内的每个固定交易会话"
    for row, row_date in zip(rows, observed_dates, strict=True):
        issue = _row_evidence_issue(row, row_date)
        if issue is not None:
            return f"{row_date.isoformat()} {issue}"
    return None


def calibration_row_is_observed(row: Kline, *, require_tradable_open: bool = False) -> bool:
    if not calibration_row_has_session_evidence(row) or row.session_status != "trading":
        return False
    if require_tradable_open and row.open_execution_status != "tradable":
        return False
    return True


def calibration_row_has_session_evidence(row: Kline) -> bool:
    row_date = _strict_date(row.date)
    return row_date is not None and _row_evidence_issue(row, row_date) is None


def _row_evidence_issue(row: Kline, row_date: date) -> str | None:
    if not valid_kline(row):
        return "日K价格或成交量无效"
    if row.adjustment_mode != "qfq":
        return "缺少统一前复权口径"
    if row.data_version.strip() in _INVALID_VERSIONS | {UNKNOWN_KLINE_DATA_VERSION}:
        return "缺少可审计数据版本"
    if row.contract_version != DAILY_KLINE_CONTRACT_VERSION:
        return "缺少受支持的日K数据合同"
    if row.fallback_used:
        return "日K来自降级数据源"
    as_of = _as_of_market_datetime(row.as_of)
    if (
        not row.point_in_time
        or as_of is None
        or as_of.date() != row_date
        or as_of.time() < DAILY_KLINE_PUBLISH_TIME
    ):
        return "缺少逐交易日PIT快照"
    if row.execution_metadata_version != FACTOR_EXECUTION_METADATA_VERSION:
        return "缺少受支持的执行元数据版本"
    return _execution_metadata_issue(row)


def _execution_metadata_issue(row: Kline) -> str | None:
    if row.session_status == "unknown":
        return "停牌状态未知"
    if row.open_execution_status == "unknown":
        return "开盘成交资格未知"
    if row.corporate_action_status == "unknown":
        return "公司行动状态未知"
    if row.session_status == "suspended" and (
        row.open_execution_status != "unavailable" or row.volume != 0
    ):
        return "停牌会话与成交资格或成交量矛盾"
    if row.session_status == "trading" and row.volume <= 0:
        return "交易会话缺少正成交量"
    if row.corporate_action_status == "effective_event":
        factor = finite_float(row.adjustment_factor)
        if factor is None or factor <= 0:
            return "公司行动缺少有效复权因子"
    return None


def _strict_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def _as_of_market_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return market_local_naive(parsed)


__all__ = [
    "FACTOR_EXECUTION_METADATA_VERSION",
    "calibration_row_has_session_evidence",
    "calibration_row_is_observed",
    "factor_calibration_evidence_issue",
]
