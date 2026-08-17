"""Conservative Chinese parser that drafts, but never executes, a StrategySpec."""

from __future__ import annotations

import re

from app.models.strategy_lab import (
    StrategyExclusions,
    StrategyHardFilter,
    StrategyNaturalLanguageRequest,
    StrategyNaturalLanguageResponse,
    StrategyPortfolioConstraints,
    StrategyRebalancePolicy,
    StrategySpecInput,
    StrategyUniverse,
)
from app.services.strategy_compiler import compile_strategy_spec


_BOARD_KEYWORDS = (
    ("上海主板", "sh_main"),
    ("沪市主板", "sh_main"),
    ("科创板", "star"),
    ("深圳主板", "sz_main"),
    ("深市主板", "sz_main"),
    ("创业板", "chinext"),
    ("北交所", "beijing"),
)
_UNSUPPORTED_PATTERNS = (
    r"市盈率|\bPE\b",
    r"市净率|\bPB\b",
    r"\bROE\b|净资产收益率",
    r"净利润|营收|现金流",
    r"\bMACD\b|\bRSI\b|布林",
    r"公告|解禁|分红|监管",
)


def parse_chinese_strategy(request: StrategyNaturalLanguageRequest) -> StrategyNaturalLanguageResponse:
    text = _normalized_text(request.text)
    defaults = _default_messages()
    ambiguities: list[str] = []
    unsupported = _unsupported_clauses(text)
    spec = _draft_spec(
        request,
        text,
        boards=_parsed_boards(text, defaults),
        exclusions=_parsed_exclusions(text, defaults),
        hard_filters=_parsed_hard_filters(text, ambiguities),
    )
    compiled = compile_strategy_spec(
        spec,
        ambiguities=ambiguities,
        unsupported_clauses=unsupported,
    )
    return StrategyNaturalLanguageResponse(
        original_text=request.text,
        draft=compiled.normalized_spec,
        applied_defaults=list(dict.fromkeys(defaults)),
        ambiguities=compiled.ambiguities,
        unsupported_clauses=compiled.unsupported_clauses,
        compile=compiled,
    )


def _default_messages() -> list[str]:
    return [
        "默认排除停牌股票",
        "默认至少需要61个完整交易日历史",
        "默认最低数据质量分70",
        "默认使用balanced多目标画像",
        "默认执行股票T+1、板块涨跌停和基础成本模型",
        "默认只生成研究计划，保存或执行前需要用户确认",
    ]


def _parsed_boards(text: str, defaults: list[str]) -> list[str]:
    boards = _boards_from_text(text)
    if boards is not None:
        return boards
    defaults.append("未指定股票板块，默认使用上海主板、科创板、深圳主板、创业板和北交所")
    return ["sh_main", "star", "sz_main", "chinext", "beijing"]


def _parsed_exclusions(text: str, defaults: list[str]) -> StrategyExclusions:
    exclude_st = bool(re.search(r"排除\s*ST|非\s*ST|不要\s*ST", text, re.IGNORECASE))
    if not exclude_st:
        defaults.append("未明确ST规则，默认排除ST")
    return StrategyExclusions(
        exclude_st=True,
        min_listing_days=_integer_match(text, r"上市不足\s*(\d+)\s*(?:个)?(?:自然)?天") or 120,
        min_history_sessions=_integer_match(text, r"至少\s*(\d+)\s*(?:个)?交易日(?:历史|数据)") or 61,
        min_data_quality_score=(
            _integer_match(text, r"(?:数据)?质量(?:分)?(?:超过|大于|不少于|至少)\s*(\d+)") or 70
        ),
    )


def _parsed_hard_filters(
    text: str,
    ambiguities: list[str],
) -> list[StrategyHardFilter]:
    filters: list[StrategyHardFilter] = []
    amount = _money_match(
        text,
        r"成交额(?:超过|大于|不少于|至少)\s*(\d+(?:\.\d+)?)\s*(亿|万|元)?",
    )
    if amount is not None:
        filters.append(StrategyHardFilter(field="amount", operator="gt", value=amount))
    elif "成交额" in text:
        ambiguities.append("提到了成交额但没有可识别的明确数值和单位，未生成成交额硬过滤")
    if re.search(r"趋势较强|趋势强|强趋势", text):
        ambiguities.append("“趋势较强”没有明确阈值；未生成硬过滤，当前仅保留多周期Alpha最大化目标")
    if re.search(r"风险较低|低风险|风险低", text):
        ambiguities.append("“风险较低”没有明确阈值；未生成硬过滤，当前仅保留风险最小化目标")
    return filters


def _draft_spec(
    request: StrategyNaturalLanguageRequest,
    text: str,
    *,
    boards: list[str],
    exclusions: StrategyExclusions,
    hard_filters: list[StrategyHardFilter],
) -> StrategySpecInput:
    stock_count = _integer_match(text, r"(?:选|持有|组合(?:不超过|最多)?)\s*(\d+)\s*只") or 20
    industry_count = _integer_match(text, r"行业(?:最多|不超过)\s*(\d+)\s*只") or 3
    hold_sessions = _integer_match(text, r"持有\s*(\d+)\s*(?:个)?(?:交易)?日") or 5
    return StrategySpecInput(
        name=request.name or "自然语言策略草案",
        description=request.text,
        universe=StrategyUniverse(boards=boards),
        exclusions=exclusions,
        hard_filters=hard_filters,
        profile=_profile_from_text(text),
        portfolio_constraints=StrategyPortfolioConstraints(
            stock_count=min(stock_count, 100),
            max_stock_weight=max(0.01, min(1.0, 1 / max(1, stock_count))),
            max_industry_positions=min(industry_count, 100),
        ),
        rebalance_policy=StrategyRebalancePolicy(
            hold_sessions=min(hold_sessions, 60),
            rebalance_every_sessions=min(hold_sessions, 60),
        ),
    )


def _boards_from_text(text: str) -> list[str] | None:
    if re.search(r"沪深\s*A\s*股", text, re.IGNORECASE):
        return ["sh_main", "star", "sz_main", "chinext"]
    if re.search(r"全市场|全部\s*A\s*股", text, re.IGNORECASE):
        return ["sh_main", "star", "sz_main", "chinext", "beijing"]
    found = []
    for keyword, board in _BOARD_KEYWORDS:
        if keyword in text and board not in found:
            found.append(board)
    return found or None


def _profile_from_text(text: str) -> str:
    if re.search(r"保守|稳健", text):
        return "conservative"
    if re.search(r"激进|进取", text):
        return "aggressive"
    return "balanced"


def _unsupported_clauses(text: str) -> list[str]:
    return [
        f"当前StrategySpec v1尚未接入该条件：{match.group(0)}"
        for pattern in _UNSUPPORTED_PATTERNS
        if (match := re.search(pattern, text, re.IGNORECASE)) is not None
    ]


def _integer_match(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return None if match is None else int(match.group(1))


def _money_match(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "元"
    multiplier = {"亿": 100_000_000, "万": 10_000, "元": 1}[unit]
    return value * multiplier


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


__all__ = ["parse_chinese_strategy"]
