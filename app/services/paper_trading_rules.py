"""Effective-dated A-share paper-trading rule profiles and daily-bar tradeability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import isclose

from app.models.market import Kline
from app.models.paper_trading_config import PAPER_TRADING_RULE_VERSION
from app.models.paper_trading import PaperInstrumentMetadata, PaperTradeRuleProfile
from app.services.trading_calendar import TradingCalendarCoverageError, trading_day_gap
from app.utils.symbols import normalize_symbol


SSE_RULE_SOURCE = "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml"
SZSE_RULE_SOURCE = "https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html"
BSE_RULE_SOURCE = "https://www.bse.cn/jygl_list/200028217.html"
_SSE_2023_RULE_SOURCE = "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20250612_10824490.shtml"
_SZSE_2023_RULE_SOURCE = "https://www.szse.cn/lawrules/index/rule/t20230217_598773.html"
_BSE_2021_RULE_SOURCE = "https://www.bse.cn/jygl_list/200010919.html"


@dataclass(frozen=True)
class DailyTradeability:
    can_buy: bool
    can_sell: bool
    code: str
    message: str
    model_limited: bool = False


@dataclass(frozen=True)
class _RuleTemplate:
    profile_id: str
    exchange: str
    board: str
    effective_from: str
    price_limit_pct: float
    min_buy_quantity: int
    buy_quantity_step: int
    first_listing_sessions_without_limit: int
    source_url: str


_MAIN_SH_2023 = _RuleTemplate(
    "sse-main-2023",
    "SH",
    "main",
    "2023-04-10",
    10,
    100,
    100,
    5,
    _SSE_2023_RULE_SOURCE,
)
_MAIN_SH_2026 = _RuleTemplate(
    "sse-main-2026",
    "SH",
    "main",
    "2026-07-06",
    10,
    100,
    100,
    5,
    SSE_RULE_SOURCE,
)
_STAR_2023 = _RuleTemplate(
    "sse-star-2023",
    "SH",
    "star",
    "2023-04-10",
    20,
    200,
    1,
    5,
    _SSE_2023_RULE_SOURCE,
)
_STAR_2026 = _RuleTemplate(
    "sse-star-2026",
    "SH",
    "star",
    "2026-07-06",
    20,
    200,
    1,
    5,
    SSE_RULE_SOURCE,
)
_MAIN_SZ_2023 = _RuleTemplate(
    "szse-main-2023",
    "SZ",
    "main",
    "2023-04-10",
    10,
    100,
    100,
    5,
    _SZSE_2023_RULE_SOURCE,
)
_MAIN_SZ_2026 = _RuleTemplate(
    "szse-main-2026",
    "SZ",
    "main",
    "2026-07-06",
    10,
    100,
    100,
    5,
    SZSE_RULE_SOURCE,
)
_CHINEXT_2023 = _RuleTemplate(
    "szse-chinext-2023",
    "SZ",
    "chinext",
    "2023-04-10",
    20,
    100,
    100,
    5,
    _SZSE_2023_RULE_SOURCE,
)
_CHINEXT_2026 = _RuleTemplate(
    "szse-chinext-2026",
    "SZ",
    "chinext",
    "2026-07-06",
    20,
    100,
    100,
    5,
    SZSE_RULE_SOURCE,
)
_BSE_2021 = _RuleTemplate(
    "bse-main-2021",
    "BJ",
    "bse",
    "2021-11-15",
    30,
    100,
    1,
    1,
    _BSE_2021_RULE_SOURCE,
)
_BSE_2026 = _RuleTemplate(
    "bse-main-2026",
    "BJ",
    "bse",
    "2026-07-06",
    30,
    100,
    1,
    1,
    BSE_RULE_SOURCE,
)

_RULE_BOOK: dict[tuple[str, str], tuple[_RuleTemplate, ...]] = {
    ("SH", "main"): (_MAIN_SH_2023, _MAIN_SH_2026),
    ("SH", "star"): (_STAR_2023, _STAR_2026),
    ("SZ", "main"): (_MAIN_SZ_2023, _MAIN_SZ_2026),
    ("SZ", "chinext"): (_CHINEXT_2023, _CHINEXT_2026),
    ("BJ", "bse"): (_BSE_2021, _BSE_2026),
}


def resolve_trade_rule_profile(
    symbol: str,
    trade_date: date,
    metadata: PaperInstrumentMetadata | None,
) -> PaperTradeRuleProfile:
    template = _rule_template(symbol, trade_date)
    limit_pct, reasons = _effective_price_limit(template, trade_date, metadata)
    limit_pct, status_reasons = _status_price_limit(limit_pct, metadata)
    reasons.extend(status_reasons)
    suffix = "-no-limit" if limit_pct is None else ""
    return PaperTradeRuleProfile(
        profile_id=f"{template.profile_id}{suffix}",
        exchange=template.exchange,
        board=template.board,
        effective_from=template.effective_from,
        price_limit_pct=limit_pct,
        min_buy_quantity=template.min_buy_quantity,
        buy_quantity_step=template.buy_quantity_step,
        first_listing_sessions_without_limit=template.first_listing_sessions_without_limit,
        source_url=template.source_url,
        quality="degraded" if reasons else "ok",
        degradation_reasons=reasons,
    )


def _effective_price_limit(
    template: _RuleTemplate,
    trade_date: date,
    metadata: PaperInstrumentMetadata | None,
) -> tuple[float | None, list[str]]:
    reasons: list[str] = []
    limit_pct: float | None = template.price_limit_pct
    effective_from = date.fromisoformat(template.effective_from)
    if trade_date < effective_from:
        reasons.append("rule_effective_date_out_of_range")
        limit_pct = None
    if metadata is None or not metadata.list_date:
        reasons.append("listing_date_missing")
        limit_pct = None
    else:
        listing_date = _date_or_none(metadata.list_date)
        if listing_date is None or listing_date > trade_date:
            reasons.append("listing_date_invalid")
            limit_pct = None
        else:
            try:
                listing_session = trading_day_gap(listing_date - timedelta(days=1), trade_date)
            except TradingCalendarCoverageError:
                reasons.append("trading_calendar_out_of_coverage")
                limit_pct = None
            else:
                if listing_session <= template.first_listing_sessions_without_limit:
                    limit_pct = None
    return limit_pct, reasons


def _status_price_limit(
    limit_pct: float | None,
    metadata: PaperInstrumentMetadata | None,
) -> tuple[float | None, list[str]]:
    if metadata is None or metadata.is_st is None or not metadata.status_effective_date:
        return limit_pct, ["historical_st_status_unknown"]
    if metadata.is_st:
        return None, ["st_rule_requires_historical_exchange_parameter"]
    return limit_pct, []


def assess_daily_tradeability(
    row: Kline,
    *,
    previous_close: float | None,
    profile: PaperTradeRuleProfile,
) -> DailyTradeability:
    if row.volume <= 0:
        return DailyTradeability(
            can_buy=False,
            can_sell=False,
            code="suspended_or_zero_volume",
            message="成交量为零或停牌，买卖均不撮合",
        )
    if previous_close is None or previous_close <= 0 or profile.price_limit_pct is None:
        return DailyTradeability(
            can_buy=True,
            can_sell=True,
            code="tradeable_rule_degraded",
            message="日K可交易，但缺少可验证的历史涨跌停参数",
            model_limited=True,
        )
    if not _one_price_bar(row):
        return DailyTradeability(
            can_buy=True,
            can_sell=True,
            code="tradeable_daily_bar",
            message="日K存在成交区间；订单簿排队和盘中顺序无法由日K精确判断",
            model_limited=True,
        )
    upper = previous_close * (1 + profile.price_limit_pct / 100)
    lower = previous_close * (1 - profile.price_limit_pct / 100)
    tolerance = max(0.003, profile.price_limit_pct / 100 * 0.015)
    if row.close >= upper * (1 - tolerance):
        return DailyTradeability(
            can_buy=False,
            can_sell=True,
            code="locked_limit_up",
            message="日K显示一字涨停，买入按无法排队成交处理",
            model_limited=True,
        )
    if row.close <= lower * (1 + tolerance):
        return DailyTradeability(
            can_buy=True,
            can_sell=False,
            code="locked_limit_down",
            message="日K显示一字跌停，卖出按无法排队成交处理",
            model_limited=True,
        )
    return DailyTradeability(
        can_buy=True,
        can_sell=True,
        code="single_price_non_limit",
        message="单一成交价但未达到可验证涨跌停阈值",
        model_limited=True,
    )


def _rule_template(symbol: str, trade_date: date) -> _RuleTemplate:
    templates = _RULE_BOOK[_rule_book_key(symbol)]
    eligible = [
        item
        for item in templates
        if date.fromisoformat(item.effective_from) <= trade_date
    ]
    return eligible[-1] if eligible else templates[0]


def _rule_book_key(symbol: str) -> tuple[str, str]:
    code, market = normalize_symbol(symbol)
    if market == "bj":
        return "BJ", "bse"
    if market == "sh":
        return ("SH", "star") if code.startswith(("688", "689")) else ("SH", "main")
    return ("SZ", "chinext") if code.startswith(("300", "301")) else ("SZ", "main")


def _one_price_bar(row: Kline) -> bool:
    return (
        isclose(row.open, row.high, rel_tol=0, abs_tol=0.0001)
        and isclose(row.open, row.low, rel_tol=0, abs_tol=0.0001)
        and isclose(row.open, row.close, rel_tol=0, abs_tol=0.0001)
    )


def _date_or_none(value: object) -> date | None:
    try:
        parsed = date.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.isoformat() == str(value) else None


__all__ = [
    "BSE_RULE_SOURCE",
    "DailyTradeability",
    "PAPER_TRADING_RULE_VERSION",
    "SSE_RULE_SOURCE",
    "SZSE_RULE_SOURCE",
    "assess_daily_tradeability",
    "resolve_trade_rule_profile",
]
