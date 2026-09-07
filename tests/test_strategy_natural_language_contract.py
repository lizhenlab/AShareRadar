from __future__ import annotations

import pytest

from app.models.strategy_lab import StrategyNaturalLanguageRequest
from app.services.strategy_natural_language import parse_chinese_strategy


def _parse(text: str):
    return parse_chinese_strategy(StrategyNaturalLanguageRequest(text=text))


@pytest.mark.parametrize(
    ("text", "boards"),
    [
        ("不要创业板，只选上海主板", ["sh_main"]),
        ("全市场排除科创板和北交所", ["sh_main", "sz_main", "chinext"]),
        ("排除科创板、北交所", ["sh_main", "sz_main", "chinext"]),
        ("全市场，只选上海主板和深圳主板", ["sh_main", "sz_main"]),
        ("沪深A股，排除创业板", ["sh_main", "star", "sz_main"]),
    ],
)
def test_explicit_board_scope_is_preserved(text, boards):
    parsed = _parse(text)
    assert parsed.draft.universe.boards == boards
    assert parsed.compile.execution_plan.executable is True
    assert parsed.requires_confirmation is True
    assert parsed.compile.execution_plan.will_start_scan is False


@pytest.mark.parametrize(
    "text",
    [
        "只选上海主板，排除上海主板",
        "不排除创业板",
        "除了科创板都选",
        "上海主板或者创业板",
        "沪深A股，只选北交所",
        "排除上海主板、科创板、深圳主板、创业板、北交所",
        "只选上海主板，只选创业板",
        "上海主板不要",
        "创业板除外",
        "排除科创板，北交所",
    ],
)
def test_conflicting_or_unsupported_board_scope_blocks_execution(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize(
    ("word", "operator"),
    [("超过", "gt"), ("大于", "gt"), ("至少", "gte"), ("不少于", "gte"),
     ("低于", "lt"), ("小于", "lt"), ("至多", "lte"), ("不超过", "lte")],
)
def test_amount_comparison_keeps_inclusive_boundary(word, operator):
    parsed = _parse(f"成交额{word}1亿")
    assert parsed.compile.execution_plan.executable is True
    assert len(parsed.draft.hard_filters) == 1
    item = parsed.draft.hard_filters[0]
    assert (item.operator, item.value) == (operator, 100_000_000.0)


@pytest.mark.parametrize("text", ["成交额至少2亿，成交额至多1亿", "成交额不低于1亿", "成交额超过-1亿",
                                  "不要求成交额超过1亿", "成交额至少1亿至多2亿", "成交额至少1e3元"])
def test_invalid_or_unsupported_amount_constraints_block_execution(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize(
    "text",
    ["选200只", "选0只", "选-1只", "选2.5只", "选10只，选20只", "行业最多0只",
     "持有100交易日", "持有0交易日", "持有-1交易日", "持有2.5交易日", "持有5天"],
)
def test_explicit_invalid_numbers_are_not_silently_executable(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize("text", ["选至少10只", "选1e3只", "持有一百个交易日", "选10只或20只",
                                  "质量至少1e3", "质量至少70.5", "质量至少七十分", "行业至少3只"])
def test_unrecognized_explicit_numbers_block_execution(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


def test_valid_zero_threshold_is_not_replaced_by_default():
    parsed = _parse("数据质量至少0，上市不足0天，选100只，持有60交易日")
    assert parsed.draft.exclusions.min_data_quality_score == 0
    assert parsed.draft.exclusions.min_listing_days == 0
    assert parsed.draft.portfolio_constraints.stock_count == 100
    assert parsed.draft.rebalance_policy.hold_sessions == 60
    assert parsed.compile.execution_plan.executable is True
    assert "默认最低数据质量分70" not in parsed.applied_defaults


def test_quality_score_units_and_duplicate_identical_amount_are_supported():
    parsed = _parse("数据质量至少80分，成交额至少1亿，成交额至少1亿")
    assert parsed.draft.exclusions.min_data_quality_score == 80
    assert len(parsed.draft.hard_filters) == 1
    assert parsed.compile.execution_plan.executable is True


@pytest.mark.parametrize("text", ["数据质量超过70", "质量至少101", "上市不足10001天", "至少1501交易日历史"])
def test_invalid_exclusion_bounds_do_not_become_executable_defaults(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize("text", [
    "成交额超过1亿不选", "成交额超过1亿除外", "创业板不买", "科创板别选",
    "持有5交易日或10交易日", "选10只以上", "不持有5交易日", "成交额大于1亿，低于2亿",
])
def test_complete_constraint_clauses_cannot_discard_prefixes_or_suffixes(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize("text", [
    "成交额超过1亿，除外", "成交额超过1亿，2亿元以内", "选成交额超过1亿10只",
    "不保守，选20只", "保守，激进，选20只", "选10只，持有5交易日之外的股票",
])
def test_clause_boundaries_cannot_hide_unconsumed_or_conflicting_intent(text):
    parsed = _parse(text)
    assert parsed.ambiguities
    assert parsed.compile.execution_plan.executable is False


@pytest.mark.parametrize("profile", ["稳健", "激进"])
def test_explicit_supported_clauses_remain_usable(profile):
    parsed = _parse(f"{profile}，排除ST，选20只，行业最多3只，持有5交易日，数据质量至少80分，成交额至少1亿")
    assert not parsed.ambiguities
    assert parsed.compile.execution_plan.executable is True
    assert parsed.draft.profile == ("conservative" if profile == "稳健" else "aggressive")
