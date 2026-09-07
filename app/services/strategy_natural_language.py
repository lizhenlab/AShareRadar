"""Conservative Chinese parser that drafts, but never executes, a StrategySpec."""

from __future__ import annotations

import re
import math

from app.models.strategy_lab import (
    StrategyExclusions,
    StrategyBoard,
    StrategyFilterOperator,
    StrategyHardFilter,
    StrategyNaturalLanguageRequest,
    StrategyNaturalLanguageResponse,
    StrategyPortfolioConstraints,
    StrategyProfile,
    StrategyRebalancePolicy,
    StrategySpecInput,
    StrategyUniverse,
)
from app.services.strategy_compiler import compile_strategy_spec


_BOARD_KEYWORDS: tuple[tuple[str, StrategyBoard], ...] = (
    ("上海主板", "sh_main"),
    ("沪市主板", "sh_main"),
    ("科创板", "star"),
    ("深圳主板", "sz_main"),
    ("深市主板", "sz_main"),
    ("创业板", "chinext"),
    ("北交所", "beijing"),
)
_ALL_BOARDS: tuple[StrategyBoard, ...] = ("sh_main", "star", "sz_main", "chinext", "beijing")
_BOARD_RE = re.compile(r"沪深\s*A\s*股|全市场|全部\s*A\s*股|" + "|".join(dict(_BOARD_KEYWORDS)), re.I)
_BOARD_ACTIONS = r"排除|不要|剔除|不选|只选|仅选|选择|选|包含|包括"
_BOARD_ACTION_RE = re.compile("(" + _BOARD_ACTIONS + r")\s*$")
_COMPARATORS: dict[str, StrategyFilterOperator] = {
    "超过": "gt", "大于": "gt", "不少于": "gte", "至少": "gte",
    "低于": "lt", "小于": "lt", "不超过": "lte", "至多": "lte",
}
_NUMBER = r"(?P<value>[-+]?\d+(?:\.\d+)?)"
_INTEGER_RULES: dict[str, tuple[str, tuple[int, int, int]]] = {
    "股票数量": (r"(?:选择?|持有|组合)(?:不超过|最多|至多)?\s*" + _NUMBER + r"\s*只", (20, 1, 100)),
    "行业股票数量": (r"行业(?:最多|不超过|至多)\s*" + _NUMBER + r"\s*只", (3, 1, 100)),
    "持有交易日": (r"持有\s*" + _NUMBER + r"\s*(?:个)?(?:交易日|天|日)", (5, 1, 60)),
    "上市天数": (r"上市不足\s*" + _NUMBER + r"\s*(?:个)?(?:自然)?天", (120, 0, 10_000)),
    "历史交易日": (r"至少\s*" + _NUMBER + r"\s*(?:个)?交易日(?:历史|数据)", (61, 1, 1_500)),
    "最低数据质量分": (r"(?:数据)?质量(?:分)?(?:不少于|至少)\s*" + _NUMBER + r"\s*分?", (70, 0, 100)),
}
_AMOUNT_PATTERN = r"成交额\s*(" + "|".join(_COMPARATORS) + r")\s*" + _NUMBER + r"\s*(亿|万|元)?"
_BOARD_GROUP_PATTERN = r"(?:(?:" + _BOARD_ACTIONS + r")\s*)?(?:" + _BOARD_RE.pattern + ")"
_CONSTRAINT_JOINERS_RE = re.compile(r"(?:\s|和|及|以及|与|并且|且|中|的|股票|、)*")
_STANDALONE_PHRASE_RE = re.compile(r"(?:保守|稳健|激进|进取)(?:策略|型)?|选股|策略草案")
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
    defaults = _default_messages(text)
    ambiguities: list[str] = []
    unsupported = _unsupported_clauses(text)
    _validate_constraint_clauses(text, ambiguities)
    spec = _draft_spec(
        request,
        text,
        boards=_parsed_boards(text, defaults, ambiguities),
        exclusions=_parsed_exclusions(text, defaults, ambiguities),
        hard_filters=_parsed_hard_filters(text, ambiguities),
        ambiguities=ambiguities,
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


def _default_messages(text: str) -> list[str]:
    defaults = [
        "默认排除停牌股票",
        "默认执行股票T+1、板块涨跌停和基础成本模型",
        "默认只生成研究计划，保存或执行前需要用户确认",
    ]
    for pattern, message in (
        (r"交易日(?:历史|数据)", "默认至少需要61个完整交易日历史"),
        (r"质量", "默认最低数据质量分70"),
        (r"保守|稳健|激进|进取", "默认使用balanced多目标画像"),
    ):
        if not re.search(pattern, text):
            defaults.append(message)
    return defaults


def _parsed_boards(text: str, defaults: list[str], ambiguities: list[str]) -> list[StrategyBoard]:
    groups = _board_groups(text, ambiguities)
    if not groups:
        defaults.append("未指定股票板块，默认使用上海主板、科创板、深圳主板、创业板和北交所")
        return list(_ALL_BOARDS)
    positive = set().union(*(boards for action, boards in groups if action == "include"))
    excluded = set().union(*(boards for action, boards in groups if action == "exclude"))
    limited = [boards for action, boards in groups if action == "only"]
    selected = positive or set(_ALL_BOARDS)
    for boards in limited:
        selected &= boards
    explicit = set().union(*(boards for action, boards in groups if action != "exclude" and len(boards) < 4))
    if explicit & excluded:
        ambiguities.append("同一板块同时被选择和排除，请明确股票范围")
    selected -= excluded
    if not selected:
        ambiguities.append("板块约束冲突或排除了全部板块；草案占位范围不可执行")
    return [board for board in _ALL_BOARDS if board in selected] or list(_ALL_BOARDS)


def _parsed_exclusions(text: str, defaults: list[str], ambiguities: list[str]) -> StrategyExclusions:
    exclude_st = bool(re.search(r"排除\s*ST|非\s*ST|不要\s*ST", text, re.IGNORECASE))
    if not exclude_st:
        defaults.append("未明确ST规则，默认排除ST")
    return StrategyExclusions(
        exclude_st=True,
        min_listing_days=_bounded_integer(text, "上市天数", ambiguities),
        min_history_sessions=_bounded_integer(text, "历史交易日", ambiguities),
        min_data_quality_score=_quality_threshold(text, ambiguities),
    )


def _parsed_hard_filters(
    text: str,
    ambiguities: list[str],
) -> list[StrategyHardFilter]:
    filters: list[StrategyHardFilter] = []
    matches = list(re.finditer(_AMOUNT_PATTERN, text))
    if len(matches) != text.count("成交额"):
        ambiguities.append("成交额仅支持明确的超过/大于/至少/不少于/低于/小于/至多/不超过数值条件")
    for match in matches:
        amount = float(match.group("value")) * {"亿": 100_000_000, "万": 10_000, "元": 1}[match.group(3) or "元"]
        if not math.isfinite(amount) or not 0 <= amount <= 1_000_000_000_000_000:
            ambiguities.append("成交额必须是0至1000000000000000元之间的有限数值")
            continue
        item = StrategyHardFilter(field="amount", operator=_COMPARATORS[match.group(1)], value=amount)
        if item not in filters:
            filters.append(item)
    if len(filters) > 1:
        ambiguities.append("多条成交额条件需确认组合关系；当前不自动解释交集、并集或覆盖")
    if re.search(r"趋势较强|趋势强|强趋势", text):
        ambiguities.append("“趋势较强”没有明确阈值；未生成硬过滤，当前仅保留多周期Alpha最大化目标")
    if re.search(r"风险较低|低风险|风险低", text):
        ambiguities.append("“风险较低”没有明确阈值；未生成硬过滤，当前仅保留风险最小化目标")
    return filters


def _draft_spec(
    request: StrategyNaturalLanguageRequest,
    text: str,
    *,
    boards: list[StrategyBoard],
    exclusions: StrategyExclusions,
    hard_filters: list[StrategyHardFilter],
    ambiguities: list[str],
) -> StrategySpecInput:
    stock_count = _bounded_integer(text, "股票数量", ambiguities)
    industry_count = _bounded_integer(text, "行业股票数量", ambiguities)
    hold_sessions = _bounded_integer(text, "持有交易日", ambiguities)
    if re.search(r"持有\s*[-+]?\d+(?:\.\d+)?\s*(?:个)?(?:天|日)", text):
        ambiguities.append("持有期的天/日未明确自然日或交易日；请使用明确的交易日数量")
    return StrategySpecInput(
        name=request.name or "自然语言策略草案",
        description=request.text,
        universe=StrategyUniverse(boards=boards),
        exclusions=exclusions,
        hard_filters=hard_filters,
        profile=_profile_from_text(text, ambiguities),
        portfolio_constraints=StrategyPortfolioConstraints(
            stock_count=stock_count,
            max_stock_weight=1 / stock_count,
            max_industry_positions=industry_count,
        ),
        rebalance_policy=StrategyRebalancePolicy(
            hold_sessions=hold_sessions,
            rebalance_every_sessions=hold_sessions,
        ),
    )


def _board_groups(text: str, ambiguities: list[str]) -> list[tuple[str, set[StrategyBoard]]]:
    groups: list[tuple[str, set[StrategyBoard]]] = []
    for clause in re.split(r"[，,；;。\n]", text):
        matches = list(_BOARD_RE.finditer(clause))
        if not matches:
            continue
        if re.search(r"不排除|不要排除|除了|除外|之外|以外|或者|或是|不是|并非|不只|不仅|除非", clause):
            ambiguities.append("板块条件含未支持的否定、例外或选择关系；请分别写明确的选择/排除板块列表")
        end = 0
        for match in matches:
            prefix = clause[end:match.start()].strip()
            action_match = _BOARD_ACTION_RE.search(prefix)
            if action_match is not None or end == 0:
                if not prefix and groups and groups[-1][0] == "exclude":
                    ambiguities.append("逗号后的裸板块可能延续排除范围；请明确选择或排除")
                action = _board_action(prefix, action_match, ambiguities)
                groups.append((action, set()))
            elif prefix not in {"", "和", "及", "、", "与", "以及"}:
                ambiguities.append("板块之间的关系无法可靠解释；请用和/、连接同组板块")
            groups[-1][1].update(_board_token(match.group()))
            end = match.end()
        if re.search(r"不要|不选|排除|剔除|只选|仅选|不包含|不包括", clause[end:]):
            ambiguities.append("不支持后置板块指令；请将选择/排除放在板块列表之前")
    return groups


def _board_action(prefix: str, match: re.Match[str] | None, ambiguities: list[str]) -> str:
    if match is None:
        if prefix:
            ambiguities.append("板块条件的前置限定无法可靠解释；仅支持选择/只选/排除等明确指令")
        return "include"
    if prefix[:match.start()].strip() not in {"", "中", "中仅", "并", "并且"}:
        ambiguities.append("板块条件含未支持的前置作用域，请明确选择和排除范围")
    action = match.group(1)
    if action in {"排除", "不要", "剔除", "不选"}:
        return "exclude"
    return "only" if action in {"只选", "仅选"} else "include"


def _board_token(value: str) -> set[StrategyBoard]:
    if value in dict(_BOARD_KEYWORDS):
        return {dict(_BOARD_KEYWORDS)[value]}
    return set(_ALL_BOARDS[:-1] if value.startswith("沪深") else _ALL_BOARDS)


def _profile_from_text(text: str, ambiguities: list[str]) -> StrategyProfile:
    if re.search(r"保守|稳健", text) and re.search(r"激进|进取", text):
        ambiguities.append("同一策略包含冲突画像；请明确选择保守或激进画像")
        return "balanced"
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


def _bounded_integer(
    text: str, label: str, ambiguities: list[str],
) -> int:
    pattern, bounds = _INTEGER_RULES[label]
    default, lower, upper = bounds
    values = {match.group("value") for match in re.finditer(pattern, text)}
    if not values:
        return default
    if len(values) != 1 or not next(iter(values)).isdigit():
        ambiguities.append(f"{label}必须提供唯一明确整数；草案默认值仅作占位，不可执行")
        return default
    value = int(next(iter(values)))
    if not lower <= value <= upper:
        ambiguities.append(f"{label}必须位于{lower}至{upper}；显式越界值不能自动截断，草案不可执行")
        return default
    return value


def _quality_threshold(text: str, ambiguities: list[str]) -> int:
    if re.search(r"(?:数据)?质量(?:分)?(?:超过|大于|低于|小于|至多|不超过)", text):
        ambiguities.append("数据质量最低分仅支持至少/不少于整数；其它比较关系不能改写为最低分")
    return _bounded_integer(text, "最低数据质量分", ambiguities)


def _validate_constraint_clauses(text: str, ambiguities: list[str]) -> None:
    """Require complete consumption of supported-domain clauses, not keyword matches."""
    patterns = (
        _AMOUNT_PATTERN + r"元?", _BOARD_GROUP_PATTERN, r"(?:排除|非|不要)\s*ST",
        *(pattern for pattern, _bounds in _INTEGER_RULES.values()),
    )
    for clause in re.split(r"[，,；;。]", text):
        if not clause.strip() or _STANDALONE_PHRASE_RE.fullmatch(clause.strip()):
            continue
        remainder = _unconsumed_constraint_text(clause, patterns)
        if _CONSTRAINT_JOINERS_RE.fullmatch(remainder) is None:
            ambiguities.append(f"条件分句未完整解析：{clause.strip()}；请分别写明确字段、比较符与数值，不能忽略前后限定")


def _unconsumed_constraint_text(clause: str, patterns: tuple[str, ...]) -> str:
    # Match the original bytes of the clause: deleting earlier matches must not
    # manufacture a new valid phrase from originally separated fragments.
    covered = [False] * len(clause)
    for pattern in patterns:
        for match in re.finditer(pattern, clause, flags=re.IGNORECASE):
            covered[match.start():match.end()] = [True] * (match.end() - match.start())
    return "".join(" " if marked else character for character, marked in zip(clause, covered, strict=True))


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


__all__ = ["parse_chinese_strategy"]
