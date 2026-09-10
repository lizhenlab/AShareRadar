"""Bounded, strict validation of the vendor's published Market Dumps schema."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
import importlib
import math
import re
from typing import Any, Literal

from app.services.fuyao_sync_control import FuyaoSyncControl
from app.utils.clock import ASHARE_TIMEZONE, market_now
from app.utils.exchange_calendar_contract import bundled_exchange_sessions


DumpKind = Literal["daily", "actions"]
DAILY_FIELDS = ("thscode", "currency", "interval", "adjusted", "date_ms", "open_price", "high_price", "low_price", "close_price", "volume", "turnover")
ACTION_FIELDS = ("thscode", "ticker", "ex_date_ms", "dividend_per_share", "per_share_bonus", "allotment_ratio", "allotment_price", "currency")
DAILY_NUMBERS = DAILY_FIELDS[5:]
ACTION_NUMBERS = ACTION_FIELDS[3:7]
BATCH_SIZE = 8192


class FuyaoDumpError(ValueError):
    """A sanitized dump contract failure; no URL, token, or response body."""


def parquet_modules() -> tuple[Any, Any]:
    """Load the optional batch reader before spending a remote request."""
    try:
        return importlib.import_module("pyarrow"), importlib.import_module("pyarrow.parquet")
    except ImportError:
        raise FuyaoDumpError("批量历史数据需要 pyarrow，请先安装项目运行依赖") from None


def dump_fields(kind: DumpKind) -> tuple[str, ...]:
    return DAILY_FIELDS if kind == "daily" else ACTION_FIELDS


def require_schema(columns: Iterable[str], kind: DumpKind) -> None:
    names = tuple(columns)
    if len(set(names)) != len(names) or not set(dump_fields(kind)).issubset(names):
        raise FuyaoDumpError("Parquet 缺少必要字段或存在重复列")


def dump_date(value: object, *, latest: date | None = None) -> date:
    if type(value) is not int:
        raise FuyaoDumpError("日期必须为整数毫秒时间戳")
    try:
        instant = datetime.fromtimestamp(value / 1000, ASHARE_TIMEZONE)
    except (ValueError, OverflowError, OSError):
        raise FuyaoDumpError("日期时间戳超出支持范围") from None
    if instant.time() != time.min or not date(1990, 1, 1) <= instant.date() <= (latest or date(2100, 1, 1)):
        raise FuyaoDumpError("日期必须为上海时区零点且在支持范围内")
    return instant.date()


def _number(value: object, *, signed: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise FuyaoDumpError("行情和企业行动字段必须为有限数值")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise FuyaoDumpError("行情和企业行动字段必须为有限数值") from None
    if not math.isfinite(number) or number < 0 and not signed:
        raise FuyaoDumpError("行情和企业行动字段不接受负数或非有限数值")
    return 0.0 if number == 0 else number


def normalize_row(row: Mapping[str, object], kind: DumpKind, *, today: date | None = None) -> dict[str, Any]:
    require_schema(row, kind)
    symbol = row["thscode"]
    if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", symbol) is None:
        raise FuyaoDumpError("股票代码必须包含受支持的交易所后缀")
    if row["currency"] != "CNY":
        raise FuyaoDumpError("只接收人民币 A 股数据")
    result = {field: row[field] for field in dump_fields(kind)}
    fields = DAILY_NUMBERS if kind == "daily" else ACTION_NUMBERS
    result.update({field: _number(row[field], signed=kind == "actions" and field == "per_share_bonus") for field in fields})
    if kind == "daily":
        _validate_daily(result, today or market_now().date())
    else:
        _validate_action(result)
    return result


def _validate_action(row: Mapping[str, Any]) -> None:
    """Signed aggregate share changes may represent a capital reduction.

    Preserve the provider ratio: shareholder classes can have different reductions,
    so this is not an attestation of an individual holder's entitlement or return.
    """
    dump_date(row["ex_date_ms"])
    if row["ticker"] != row["thscode"][:6]:
        raise FuyaoDumpError("企业行动的 ticker 与证券代码不一致")
    bonus = row["per_share_bonus"]
    factor = 1 + bonus + row["allotment_ratio"]
    if bonus <= -1 or not math.isfinite(factor) or factor <= 0:
        raise FuyaoDumpError("企业行动股本变动比例必须大于 -1，且变动后比例必须为有限正数")


def _validate_daily(row: Mapping[str, Any], today: date) -> None:
    if row["adjusted"] != "none" or row["interval"] != "1d":
        raise FuyaoDumpError("只接收未复权日线，禁止混入前复权缓存")
    dump_date(row["date_ms"], latest=today)
    low, high = row["low_price"], row["high_price"]
    if low <= 0 or not low <= min(row["open_price"], row["close_price"]) <= max(row["open_price"], row["close_price"]) <= high:
        raise FuyaoDumpError("日线 OHLC 的价格范围无效")


def validate_trading_dates(observed: Iterable[str], trade_dates: tuple[str, ...] | None = None,
                           control: FuyaoSyncControl | None = None) -> str:
    control = control or FuyaoSyncControl()
    control.checkpoint("checking_calendar")
    actual = _calendar_dates(observed, control)
    if not actual:
        raise FuyaoDumpError("日线数据为空")
    if trade_dates is None:
        calendar = _calendar_dates(bundled_exchange_sessions(), control)
    else:
        calendar = _calendar_dates(trade_dates, control)
    first, last = min(actual), max(actual)
    if not calendar or min(calendar) > first or max(calendar) < last:
        raise FuyaoDumpError("交易日历覆盖不足，不能确认全市场日期连续性")
    expected = {value for value in calendar if first <= value <= last}
    control.checkpoint()
    if actual != expected:
        raise FuyaoDumpError("存在全市场交易日缺口或非交易日数据；请改用全量同步并检查来源")
    return "explicit_calendar" if trade_dates is not None else "bundled_exchange_calendar"


def _calendar_dates(values: Iterable[str | date], control: FuyaoSyncControl) -> set[date]:
    result = set()
    for count, value in enumerate(values):
        if count % BATCH_SIZE == 0:
            control.checkpoint()
        result.add(date.fromisoformat(value) if isinstance(value, str) else value)
    control.checkpoint()
    return result
