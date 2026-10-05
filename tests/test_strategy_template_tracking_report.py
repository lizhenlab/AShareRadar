from __future__ import annotations

from copy import deepcopy
from html import escape

import pytest

from tools.strategy_template_tracking_report import render_strategy_template_tracking_html


def _selection(template_id="bounded_medium_trend", *, outcome="available", result=0.04):
    return {
        "template_id": template_id, "status": "ready", "evaluated_count": 5000, "eligible_count": 40,
        "selected_count": 1, "available_outcome_count": 1 if outcome == "available" else 0,
        "outcome_status": outcome, "target_invested_weight": 0.4, "unallocated_weight": 0.6,
        "residual_cash_cny": 599_700, "estimated_round_trip_cost_cny": 300,
        "weighted_gross_return": result if outcome == "available" else None,
        "execution_fingerprint": "a" * 64, "result_digest": "b" * 64,
        "rejection_counts": {"score_below_minimum": 4800}, "missing_reason_counts": {}, "reasons": [],
        "positions": [{"symbol": "600000.SH", "name": "样本股票", "industry": "银行", "target_weight": 0.4,
                       "signal_price": 100, "forward_return": result / 0.4 if outcome == "available" else None,
                       "outcome_reason": None if outcome == "available" else "target_session_not_completed"}],
        "rejected": [{"symbol": "SHOULD_NOT_RENDER_REJECTED_SYMBOL", "reason": "individual-reject"}] * 5000,
    }


def _report():
    ids = ("bounded_medium_trend", "medium_momentum", "low_volatility_trend")
    selections = [_selection(), _selection(ids[1], result=0.09), _selection(ids[2], outcome="pending")]
    return {
        "schema_version": "strategy-template-tracking-v1", "generated_at": "2026-09-19T16:00:00+08:00",
        "as_of_completed_date": "2026-09-18", "status": "descriptive_only", "horizon_sessions": 10,
        "notional_cash_cny": 1_000_000, "source_session_count": 1,
        "source": {"database": "data/research.sqlite3", "selected_run_count": 1},
        "templates": [{"template_id": item, "commission_rate": 0.0003, "spec_version": "initial-v1"} for item in ids],
        "run_failures": [], "excluded_runs": [], "limitations": ["当前结果不是样本外收益优势证明。"],
        "cohorts": [{"rule_version": "rule-v5", "score_spec_hash": "c" * 64, "session_count": 1,
                     "template_summaries": [{"template_id": item, "session_count": 1, "mean_selected_count": 1,
                                             "mean_target_invested_weight": 0.4, "mean_estimated_round_trip_cost_cny": 300,
                                             "outcome_counts": {"pending" if item == ids[2] else "available": 1}}
                                            for item in ids],
                     "comparisons": [{"template_id": ids[1], "paired_session_count": 1, "paired_dates": ["2026-09-01"],
                                      "candidate_average_return": 0.09, "baseline_average_return": 0.04,
                                      "candidate_minus_baseline_return": 0.05, "mean_selected_overlap_count": 1,
                                      "excluded_pair_counts": {}},
                                     {"template_id": ids[2], "paired_session_count": 0, "paired_dates": [],
                                      "candidate_average_return": None, "baseline_average_return": None,
                                      "candidate_minus_baseline_return": None, "mean_selected_overlap_count": 1,
                                      "excluded_pair_counts": {"candidate:pending|baseline:available": 1}}],
                     "sessions": [{"signal_date": "2026-09-01", "target_date": "2026-09-15", "run_id": 17,
                                   "published_at": "2026-09-01T08:00:00Z", "snapshot_digest": "d" * 64,
                                   "selections": selections}]}],
    }


def test_report_shows_fixed_templates_paired_dates_cash_and_auditable_specs():
    html = render_strategy_template_tracking_html(_report())
    for text in ("基线 · 中期趋势", "中期动量", "低波趋势", "+9.00%", "+4.00%", "+5.00 个百分点",
                 "同日完整配对差", "¥599,700.00", "¥300.00", "待到期", "评分未达门槛 × 4800",
                 "commission_rate", "initial-v1", "执行规格指纹", "冻结持仓", "600000.SH"):
        assert text in html
    assert "SHOULD_NOT_RENDER_REJECTED_SYMBOL" not in html
    assert "individual-reject" not in html
    assert "a" * 64 in html and "c" * 64 in html
    assert "实际配对日期" in html and "2026-09-01" in html


def test_rendering_does_not_invent_net_return_or_probability_and_is_self_contained():
    html = render_strategy_template_tracking_html(_report())
    assert "不是净值、实际成交收益或预测命中率" in html
    assert "不从后续毛收益直接扣减" in html
    assert "+8.97%" not in html
    assert "未计算净收益、净值、置信区间或自动晋级结论" in html
    assert "<script" not in html and "<link" not in html and " src=" not in html and " href=" not in html
    assert 'name="viewport"' in html and "@media(max-width:640px)" in html
    assert 'tabindex="0"' in html and '<th scope="col">' in html


def test_pending_or_missing_returns_are_never_displayed_as_zero_or_paired():
    report = _report()
    report["cohorts"][0]["sessions"][0]["selections"][1]["outcome_status"] = "missing"
    report["cohorts"][0]["sessions"][0]["selections"][1]["weighted_gross_return"] = 0.09
    html = render_strategy_template_tracking_html(report)
    assert "中期动量较基线：—" in html
    assert "低波趋势较基线：—" in html
    assert "候选待到期，基线结果可用 × 1" in html


def test_zero_difference_is_preserved_instead_of_missing():
    report = _report()
    report["cohorts"][0]["comparisons"][0]["candidate_minus_baseline_return"] = 0
    assert "+0.00 个百分点" in render_strategy_template_tracking_html(report)


def test_all_external_strings_and_raw_template_specs_are_escaped():
    attack = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    report = _report()
    report["generated_at"] = attack
    report["cohorts"][0]["rule_version"] = attack
    report["cohorts"][0]["sessions"][0]["selections"][0]["positions"][0]["name"] = attack
    report["cohorts"][0]["sessions"][0]["selections"][0]["rejection_counts"] = {attack: 1}
    report["templates"][0]["spec"] = attack
    report["run_failures"] = [{"run_id": attack, "reason": attack, "error_type": attack}]
    report["excluded_runs"] = [{"run_id": 1, "reason": attack}]
    html = render_strategy_template_tracking_html(report)
    assert attack not in html and "<script>" not in html and "<img" not in html
    assert escape(attack, quote=True) in html


@pytest.mark.parametrize("invalid", [None, True, float("inf"), float("nan"), 10**400, "unknown"])
def test_invalid_numbers_do_not_crash_or_become_zero(invalid):
    report = _report()
    report["notional_cash_cny"] = invalid
    report["horizon_sessions"] = invalid
    report["cohorts"][0]["comparisons"][0]["candidate_minus_baseline_return"] = invalid
    html = render_strategy_template_tracking_html(report)
    assert "名义资金 —" in html and "D+—" in html
    assert "nan" not in html.lower() and "inf" not in html.lower()


def test_no_selection_and_no_cohorts_have_clear_empty_states():
    report = _report()
    selection = report["cohorts"][0]["sessions"][0]["selections"][0]
    selection.update(positions=[], outcome_status="no_selection", status="no_trade", weighted_gross_return=None)
    html = render_strategy_template_tracking_html(report)
    assert "未选中股票，查看现金及筛选原因" in html and "不建立持仓" in html
    report["cohorts"] = []
    html = render_strategy_template_tracking_html(report)
    assert "没有可用的冻结扫描分组" in html


def test_failure_summary_is_visible_even_without_any_cohort():
    report = _report()
    report["cohorts"] = []
    report["run_failures"] = [{"run_id": 3, "reason": "frozen_snapshot_unavailable", "error_type": "ValueError"}]
    html = render_strategy_template_tracking_html(report)
    assert "读取失败 1 批次" in html and "存在读取或封存校验失败的批次" in html
    assert "冻结快照无法读取或未通过封存校验" in html


def test_rendering_preserves_input_and_handles_absent_optional_collections():
    report = _report()
    before = deepcopy(report)
    render_strategy_template_tracking_html(report)
    assert report == before
    assert "没有可用的冻结扫描分组" in render_strategy_template_tracking_html({"cohorts": "invalid", "source": None})
    assert "没有可用的冻结扫描分组" in render_strategy_template_tracking_html({"cohorts": [None], "limitations": "not a list"})


@pytest.mark.parametrize("value, displayed", [(1e308, "+1.00E+310"), (-1e308, "-1.00E+310")])
def test_finite_extreme_returns_keep_a_percentage_unit_without_overflow_or_false_zero(value, displayed):
    report = _report()
    comparison = report["cohorts"][0]["comparisons"][0]
    comparison.update(candidate_average_return=value, candidate_minus_baseline_return=value)
    html = render_strategy_template_tracking_html(report)
    assert f"{displayed}%" in html
    assert f"{displayed} 个百分点" in html
    assert "inf%" not in html.lower() and "inf 个百分点" not in html.lower()


@pytest.mark.parametrize("reason, meaning", [
    ("snapshot_seal_invalid", "缺少原始发布封印"),
    ("frozen_snapshot_invalid", "冻结扫描内容不完整或不一致"),
    ("signal_availability_invalid", "决策、发布及封印时间"),
    ("decision_time_invalid", "决策时点无效"),
    ("missing_frozen_universe", "缺少冻结股票池"),
    ("score_contract_unregistered", "评分规则未经注册"),
    ("pit_feature_binding_invalid", "因子时点证据缺失或不一致"),
    ("canonical_run_not_requested", "未包含该合同当日最早发布的扫描"),
    ("calendar_unavailable", "交易日历无法确定目标日期"),
    ("holding_path_bar_missing_or_duplicate", "持有期日K有缺失或重复"),
    ("forward_bar_contract_invalid", "后续日K的价格、成交量或数据合同无效"),
    ("mixed_forward_price_vintages", "混用不同数据版本"),
    ("corporate_action_effective_event", "观察期间发生公司行动"),
    ("corporate_action_status_unknown", "公司行动状态未知"),
    ("forward_observation_after_as_of_or_unknown", "采集时间未知或晚于本报告截止时间"),
    ("forward_snapshot_time_invalid", "快照时间不在收盘至采集时间之间"),
    ("nonfinite_forward_return", "收益计算溢出或无效"),
    ("signal_price_basis_evidence_missing", "缺少信号日价格基准证据"),
    ("signal_price_basis_evidence_invalid", "价格基准证据无效或版本不受支持"),
    ("signal_price_basis_identity_conflict", "价格证据与股票、日期或扫描批次不一致"),
    ("signal_price_basis_bar_invalid", "信号日冻结K线无法证明有效的价格基准"),
    ("target_adjustment_overlap_missing", "缺少与冻结信号日重叠的K线"),
    ("target_adjustment_overlap_invalid", "与信号日重叠的后续K线无效或重复"),
    ("target_adjustment_rebase_conflict", "同一信号日的新旧价格不一致"),
])
def test_backend_failure_reasons_are_readable_in_empty_reports(reason, meaning):
    report = _report()
    report["cohorts"] = []
    report["run_failures"] = [{"run_id": index, "reason": reason, "error_type": "ValueError"} for index in range(15)]
    html = render_strategy_template_tracking_html(report)
    assert "读取失败 15 批次" in html and "失败原因汇总：" in html and "× 15" in html
    assert meaning in html and reason not in html


def _net_report():
    report = _report()
    report["schema_version"] = "strategy-template-tracking-v2"
    ids = ("bounded_medium_trend", "medium_momentum", "low_volatility_trend")
    results = [_net_selection(item, result) for item, result in zip(ids, (.03, .08, .04), strict=True)]
    report["net_comparison"] = {
        "schema_version": "strategy-template-net-returns-v1", "as_of": report["generated_at"],
        "horizon_sessions": 10, "notional_cash_cny": 1_000_000,
        "entry_policy": "D+1-open", "exit_policy": "D+H+1-close", "continuous_portfolio_simulation": False,
        "promotion_eligible": False, "execution_evidence": {"provenance_status": "official_raw_file_verified",
            "artifact_digests": ["e" * 64], "manifest_digest": "f" * 64},
        "cost_specs": [{"template_id": item, "base": {"buy_slippage_bps": 5}, "stress_rule": "fixed-stress"} for item in ids],
        "cohorts": [{"rule_version": "rule-v5", "score_spec_hash": "c" * 64,
                     "sessions": [{"run_id": 17, "signal_date": "2026-09-01", "entry_date": "2026-09-02",
                                   "exit_date": "2026-09-16", "snapshot_digest": "d" * 64, "selections": results}]}],
    }
    report["strategy_selection"] = {
        "schema_version": "strategy-template-selection-v1", "evaluation_kind": "retrospective-net-scenario-selection",
        "status": "insufficient_data", "adoptable_template_id": None, "promotion_eligible": False,
        "inference_contract": {"method": "two-sided-null-centered-circular-moving-block-bootstrap",
                               "minimum_complete_anchors": 20, "multiplicity_adjustment": "Holm-FWER"},
        "adoption_blockers": ["当前为回溯研究，前瞻证据不足。"],
        "cohorts": [{"rule_version": "rule-v5", "score_spec_hash": "c" * 64, "status": "insufficient_data",
                     "planned_anchor_dates": ["2026-09-01"], "matured_anchor_dates": ["2026-09-01"],
                     "pending_anchor_dates": [], "missing_anchor_dates": [], "complete_anchor_count": 1,
                     "diagnostic_leader_id": "medium_momentum", "statistical_winner_id": None,
                     "risk_qualified_winner_id": None, "adoptable_template_id": None,
                     "template_summaries": [{"template_id": item, "mean_net_return": result["net_return"],
                                             "mean_stress_net_return": result["stress_net_return"],
                                             "worst_independent_batch_drawdown": -.08} for item, result in zip(ids, results, strict=True)],
                     "comparisons": [{"left_template_id": "medium_momentum", "right_template_id": "bounded_medium_trend",
                                      "mean_net_return_difference": .05, "mean_stress_net_return_difference": .05,
                                      "two_sided_p_value": None, "holm_adjusted_p_value": None, "reject_equal_means": None}],
                     "blockers": ["独立完整锚点日期不足。"]}],
    }
    return report


def _net_selection(template, value):
    base = {"status": "available", "net_return": value, "total_fees_cny": 1250,
            "filled_count": 1, "unfilled_count": 0, "unavailable_count": 0,
            "residual_cash_cny": 1_000_000 * (1 + value), "independent_batch_max_drawdown": -.07, "reason_counts": {},
            "positions": [{"symbol": "600000.SH", "target_weight": .4, "budget_cny": 400_000,
                           "status": "available", "reason": None, "quantity": 100,
                           "buy_amount_cny": 400_000, "sell_amount_cny": 410_000,
                           "buy_fees_cny": 400, "sell_fees_cny": 850, "pnl_cny": 8750}]}
    stress = deepcopy(base)
    stress.update(net_return=value-.01, total_fees_cny=1500, independent_batch_max_drawdown=-.08)
    return {"template_id": template, "status": "available", "reason_codes": [],
            "net_return": value, "stress_net_return": value-.01, "base": base, "stress": stress}


def test_net_report_separates_diagnostic_statistics_adoption_and_return_timings():
    report = _net_report()
    html = render_strategy_template_tracking_html(report)
    for text in ("哪套策略更好", "诊断领先", "统计优势", "可采用模板", "暂无合格证据", "保留当前策略",
                 "净收益比较 · D+1 → D+H+1", "历史毛收益回看 · D → D+H", "计划入场 2026-09-02 开盘",
                 "计划退出 2026-09-16 收盘", "基础成本净收益均值", "压力成本净收益均值", "+8.00%", "+7.00%",
                 "¥1,250.00", "¥1,500.00", "最差独立批次回撤", "Holm 校正 p 值", "独立完整锚点日期不足",
                 "尚不可检验", "前瞻证据不足", "相同完整锚点日期", "固定统计比较规则", "fixed-stress"):
        assert text in html
    assert html.index('id="strategy-selection"') < html.index('id="net-comparison"') < html.index("历史毛收益回看")
    assert "单批回撤不能当作连续策略回撤" in html and "-8.00%" in html


def test_retrospective_statistical_and_risk_winners_do_not_become_adopted_templates():
    report = _net_report()
    cohort = report["strategy_selection"]["cohorts"][0]
    cohort.update(statistical_winner_id="medium_momentum", risk_qualified_winner_id="medium_momentum")
    cohort["comparisons"][0].update(two_sided_p_value=.002, holm_adjusted_p_value=.006, reject_equal_means=True)
    html = render_strategy_template_tracking_html(report)
    assert '<h3>统计优势</h3><strong>中期动量</strong>' in html
    assert "压力与风险条件通过者：中期动量" in html
    assert "0.0020" in html and "0.0060" in html and "已通过差异检验" in html
    assert '<h3>可采用模板</h3><strong>暂无合格证据</strong>' in html
    assert "保留当前策略" in html
    report["strategy_selection"].update(adoptable_template_id="medium_momentum", promotion_eligible=True)
    cohort["adoptable_template_id"] = "medium_momentum"
    assert '<h3>可采用模板</h3><strong>暂无合格证据</strong>' in render_strategy_template_tracking_html(report)


@pytest.mark.parametrize("status", ["pending", "unavailable", "blocked"])
def test_unavailable_net_results_do_not_show_stale_returns_fees_or_false_zero(status):
    report = _net_report()
    selected = report["net_comparison"]["cohorts"][0]["sessions"][0]["selections"][1]
    selected["status"] = status
    for result in (selected["base"], selected["stress"]):
        result.update(status=status, net_return=.12345, total_fees_cny=9876.54,
                      independent_batch_max_drawdown=.12345, residual_cash_cny=123456.78)
    html = render_strategy_template_tracking_html(report)
    assert "+12.35%" not in html and "¥9,876.54" not in html and "¥123,456.78" not in html
    assert "未到期或证据不足不计零" in html


def test_available_cash_only_net_result_is_zero_without_invented_fills():
    report = _net_report()
    selected = report["net_comparison"]["cohorts"][0]["sessions"][0]["selections"][0]
    for result in (selected["base"], selected["stress"]):
        result.update(net_return=0, total_fees_cny=0, filled_count=0, residual_cash_cny=1_000_000,
                      independent_batch_max_drawdown=0, positions=[], reason_counts={"no_selection": 1})
    html = render_strategy_template_tracking_html(report)
    assert "+0.00%" in html and "¥0.00" in html and "模拟成交 0" in html
    assert "没有持仓成交明细" in html


def test_legacy_missing_or_unknown_new_contracts_have_no_selection_claim():
    report = _report()
    for net, selection in ((None, None), ({}, {}), ({"schema_version": "future"}, {"schema_version": "future"})):
        report.update(net_comparison=net, strategy_selection=selection)
        html = render_strategy_template_tracking_html(report)
        assert "未生成净收益选择结论，保留当前策略" in html
        assert "历史毛收益不自动转换为净收益" in html
        assert '<h3>统计优势</h3>' not in html


def test_empty_new_contracts_show_explicit_missing_results():
    report = _net_report()
    report["strategy_selection"].update(cohorts=[], adoption_blockers=[])
    report["net_comparison"]["cohorts"] = []
    html = render_strategy_template_tracking_html(report)
    assert "没有可评估的净收益选择分组" in html
    assert "没有可供净执行比较的批次" in html
    assert "尚未形成独立的前瞻采用证据" in html


def test_new_dynamic_fields_are_escaped_and_private_exception_types_are_omitted():
    attack = '<img src=x onerror="alert(1)">'
    report = _net_report()
    report["strategy_selection"]["adoption_blockers"] = [attack]
    cohort = report["strategy_selection"]["cohorts"][0]
    cohort.update(diagnostic_leader_id=attack, statistical_winner_id=attack, blockers=[attack], planned_anchor_dates=[attack])
    cohort["comparisons"][0]["left_template_id"] = attack
    net = report["net_comparison"]
    net["execution_evidence"]["provenance_status"] = attack
    net["cost_specs"][0]["stress_rule"] = attack
    session = net["cohorts"][0]["sessions"][0]
    session.update(entry_date=attack, snapshot_digest=attack)
    session["selections"][0]["reason_codes"] = [attack]
    session["selections"][0]["base"]["positions"][0].update(symbol=attack, reason=attack)
    report["run_failures"] = [{"run_id": 1, "reason": "snapshot_seal_invalid", "error_type": "_TrackingAdmissionError"}]
    html = render_strategy_template_tracking_html(report)
    assert "<img" not in html and escape(attack, quote=True) in html
    assert "_TrackingAdmissionError" not in html
    assert "缺少原始发布封印" in html


@pytest.mark.parametrize("invalid", [None, True, float("inf"), float("nan"), 10**400, "unknown"])
def test_new_statistical_and_net_numeric_fields_reject_invalid_values(invalid):
    report = _net_report()
    comparison = report["strategy_selection"]["cohorts"][0]["comparisons"][0]
    comparison.update(two_sided_p_value=invalid, holm_adjusted_p_value=invalid, reject_equal_means=False)
    session = report["net_comparison"]["cohorts"][0]["sessions"][0]
    for selected in session["selections"]:
        for scenario in ("base", "stress"):
            selected[scenario].update(net_return=invalid, total_fees_cny=invalid, independent_batch_max_drawdown=invalid)
    html = render_strategy_template_tracking_html(report)
    assert "未通过差异检验" in html
    assert "nan%" not in html.lower() and "¥nan" not in html.lower() and ">nan<" not in html.lower()
    assert "inf%" not in html.lower() and "¥inf" not in html.lower()
    assert '<td class="number">—</td>' in html


def test_extreme_money_counts_and_small_p_values_have_bounded_display_length():
    report = _net_report()
    report["notional_cash_cny"] = 1e308
    report["source_session_count"] = 1e308
    report["cohorts"][0]["template_summaries"][0]["mean_selected_count"] = 1e308
    comparison = report["strategy_selection"]["cohorts"][0]["comparisons"][0]
    comparison.update(two_sided_p_value=1e-8, holm_adjusted_p_value=2)
    html = render_strategy_template_tracking_html(report)
    assert "¥1.00E+308" in html and "1.00E-08" in html
    assert len(html) < 60_000


def test_multiple_contracts_keep_their_own_leaders_and_do_not_mutate_input():
    report = _net_report()
    other = deepcopy(report["strategy_selection"]["cohorts"][0])
    other.update(rule_version="other-rule", score_spec_hash="0" * 64, diagnostic_leader_id="low_volatility_trend", blockers=[])
    report["strategy_selection"]["cohorts"].append(other)
    before = deepcopy(report)
    html = render_strategy_template_tracking_html(report)
    assert '净收益选择 · 评分合同分组 1' in html and '净收益选择 · 评分合同分组 2' in html
    assert '<h3>诊断领先</h3><strong>中期动量</strong>' in html
    assert '<h3>诊断领先</h3><strong>低波趋势</strong>' in html
    assert "不跨评分合同合并赢家" in html and report == before


@pytest.mark.parametrize("reason, meaning", [
    ("official_execution_evidence_unavailable", "缺少通过严格校验的正式执行证据"),
    ("failed_source_contract_unresolved", "失败批次的评分合同无法确定"),
    ("trusted_calendar_unavailable", "无法固定完整比较日期"),
    ("mature_calendar_anchors_missing", "已到期锚点存在缺失"),
    ("minimum_complete_anchors_not_met", "独立完整锚点数量不足"),
    ("no_familywise_statistical_winner", "校正后同时优于其余两套模板"),
    ("cost_stress_advantage_not_confirmed", "压力成本下尚未保持"),
    ("independent_batch_drawdown_limit_exceeded", "回撤恶化超过预定风险上限"),
    ("retrospective_template_selection", "不是事前固定的前瞻验证"),
    ("independent_prospective_validation_missing", "缺少独立前瞻验证"),
    ("continuous_capital_and_position_validation_missing", "尚未验证连续资金"),
    ("non_overlapping_signal_schedule", "预先固定的非重叠周期之外"),
    ("position_quantity_not_representable", "模拟股数超出可精确计算范围"),
])
def test_selection_blockers_and_scan_exclusions_have_readable_explanations(reason, meaning):
    report = _net_report()
    report["strategy_selection"]["adoption_blockers"] = [reason]
    report["strategy_selection"]["cohorts"][0]["blockers"] = [reason]
    report["excluded_runs"] = [{"run_id": 19, "reason": reason}]
    html = render_strategy_template_tracking_html(report)
    assert meaning in html and reason not in html
    assert "可用毛收益分组" in html and "已准入信号日" in html
