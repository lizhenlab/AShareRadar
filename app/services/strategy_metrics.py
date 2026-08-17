"""Single whitelist for fields accepted by the strategy compiler."""

from __future__ import annotations

from app.models.strategy_lab import StrategyMetricDefinition


_NUMBER_OPERATORS = ["eq", "ne", "gt", "gte", "lt", "lte", "between"]
_CATEGORY_OPERATORS = ["eq", "ne", "in"]
_BOOLEAN_OPERATORS = ["eq", "ne"]


STRATEGY_METRICS: tuple[StrategyMetricDefinition, ...] = (
    StrategyMetricDefinition(
        name="amount",
        label="成交额",
        kind="number",
        unit="CNY",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.amount",
    ),
    StrategyMetricDefinition(
        name="turnover_rate",
        label="换手率",
        kind="number",
        unit="percent",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.turnover_rate",
    ),
    StrategyMetricDefinition(
        name="change_pct",
        label="当日涨跌幅",
        kind="number",
        unit="percent",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.change_pct",
    ),
    StrategyMetricDefinition(
        name="trend_score",
        label="生产趋势分",
        kind="number",
        unit="score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.trend_score",
    ),
    StrategyMetricDefinition(
        name="data_quality_score",
        label="数据质量分",
        kind="number",
        unit="score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.data_quality_score",
    ),
    StrategyMetricDefinition(
        name="alpha_1d",
        label="1日 Alpha 研究分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.alpha_1d",
    ),
    StrategyMetricDefinition(
        name="alpha_5d",
        label="5日 Alpha 研究分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.alpha_5d",
    ),
    StrategyMetricDefinition(
        name="alpha_20d",
        label="20日 Alpha 研究分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.alpha_20d",
    ),
    StrategyMetricDefinition(
        name="confidence",
        label="证据置信分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.confidence",
    ),
    StrategyMetricDefinition(
        name="risk",
        label="风险分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="minimize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.risk",
    ),
    StrategyMetricDefinition(
        name="tradability",
        label="可交易性分",
        kind="number",
        unit="ordinal_score_0_100",
        direction="maximize",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.scores.tradability",
    ),
    StrategyMetricDefinition(
        name="return_pct",
        label="区间收益率",
        kind="number",
        unit="percent",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[1, 5, 20, 60],
        source_field="score_details.components.score_dimensions.raw_features.return_{period}d_pct",
    ),
    StrategyMetricDefinition(
        name="volume_ratio",
        label="量比",
        kind="number",
        unit="ratio",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.volume_ratio",
    ),
    StrategyMetricDefinition(
        name="listing_days",
        label="上市天数",
        kind="number",
        unit="calendar_days",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="derived.listing_days_at_data_as_of",
    ),
    StrategyMetricDefinition(
        name="history_sessions",
        label="完整历史交易日数量",
        kind="number",
        unit="trading_sessions",
        allowed_operators=_NUMBER_OPERATORS,
        allowed_periods=[],
        source_field="score_details.components.score_dimensions.point_in_time_evidence.payload.bar_contract_61",
    ),
    StrategyMetricDefinition(
        name="is_st",
        label="ST 状态",
        kind="boolean",
        unit="boolean",
        allowed_operators=_BOOLEAN_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.is_st",
    ),
    StrategyMetricDefinition(
        name="is_new",
        label="新股状态",
        kind="boolean",
        unit="boolean",
        allowed_operators=_BOOLEAN_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.is_new",
    ),
    StrategyMetricDefinition(
        name="suspended",
        label="停牌状态",
        kind="boolean",
        unit="boolean",
        allowed_operators=_BOOLEAN_OPERATORS,
        allowed_periods=[],
        source_field="derived.suspended_at_data_as_of",
    ),
    StrategyMetricDefinition(
        name="board",
        label="上市板块",
        kind="category",
        unit="board_code",
        allowed_operators=_CATEGORY_OPERATORS,
        allowed_periods=[],
        source_field="derived.listing_board",
    ),
    StrategyMetricDefinition(
        name="industry",
        label="行业",
        kind="category",
        unit="industry_name",
        allowed_operators=_CATEGORY_OPERATORS,
        allowed_periods=[],
        source_field="market_scan_result.industry",
    ),
)

STRATEGY_METRIC_BY_NAME = {item.name: item for item in STRATEGY_METRICS}


def strategy_metric_registry() -> list[StrategyMetricDefinition]:
    return [item.model_copy(deep=True) for item in STRATEGY_METRICS]


__all__ = ["STRATEGY_METRIC_BY_NAME", "STRATEGY_METRICS", "strategy_metric_registry"]
