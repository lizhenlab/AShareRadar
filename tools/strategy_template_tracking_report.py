"""Self-contained descriptive comparisons of fixed strategy-template selections."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from html import escape
import json
import math


_TEMPLATE_LABELS = {
    "bounded_medium_trend": "基线 · 中期趋势",
    "medium_momentum": "中期动量",
    "low_volatility_trend": "低波趋势",
}
_STATUS_LABELS = {
    "ok": "可查看描述结果", "insufficient_data": "证据不足", "paired": "同日已配对",
    "descriptive_only": "仅供描述性比较", "no_selection": "未选中股票", "no_trade": "不建立持仓", "blocked": "准入受阻",
    "available": "结果可用", "complete": "结果完整", "pending": "待到期", "missing": "结果缺失",
    "calendar_unavailable": "交易日历缺失", "not_comparable": "不可比", "failed": "失败",
    "empty": "未选中股票", "all_cash": "全部保留现金", "ready": "方案已生成", "unavailable": "证据不可用",
    "unfilled": "未成交，保留现金", "historical_evidence_available": "回溯检验可用",
}
_REASON_LABELS = {
    "score_below_minimum": "评分未达门槛", "target_session_not_completed": "目标交易日尚未结束",
    "published_after_as_of": "发布晚于观察截止时间", "same_contract_session_rescan": "同日同合同重复扫描",
    "non_overlapping_signal_schedule": "预先固定的非重叠周期之外",
    "frozen_snapshot_unavailable": "冻结快照无法读取或未通过封存校验",
    "snapshot_seal_invalid": "缺少原始发布封印，或冻结快照的封印校验未通过",
    "frozen_snapshot_invalid": "冻结扫描内容不完整或不一致，无法还原当时的股票池",
    "signal_availability_invalid": "信号尚未完成，或决策、发布及封印时间不满足交易时点要求",
    "decision_time_invalid": "决策时点无效，无法证明当时已具备这些数据",
    "missing_frozen_universe": "缺少冻结股票池，无法重建当时的候选范围",
    "score_contract_unregistered": "评分规则未经注册，或冻结规则与登记的评分合同不一致",
    "pit_feature_binding_invalid": "因子时点证据缺失或不一致，无法证明属于这次扫描",
    "canonical_run_not_requested": "指定批次未包含该合同当日最早发布的扫描，不用较晚重扫替代",
    "calendar_unavailable": "可信交易日历无法确定目标日期",
    "holding_path_bar_missing_or_duplicate": "持有期日K有缺失或重复，无法计算固定期限结果",
    "forward_bar_contract_invalid": "后续日K的价格、成交量或数据合同无效",
    "mixed_forward_price_vintages": "后续日K混用不同数据版本，价格基准无法确认一致",
    "corporate_action_effective_event": "观察期间发生公司行动，跨日价格基准不可直接比较",
    "corporate_action_status_unknown": "公司行动状态未知，无法确认跨日价格可比",
    "forward_observation_after_as_of_or_unknown": "后续行情采集时间未知或晚于本报告截止时间",
    "forward_snapshot_time_invalid": "后续行情快照时间不在收盘至采集时间之间",
    "nonfinite_forward_return": "后续收益计算溢出或无效，未将其计作零收益",
    "signal_price_basis_evidence_missing": "缺少信号日价格基准证据",
    "signal_price_basis_evidence_invalid": "信号日价格基准证据无效或版本不受支持",
    "signal_price_basis_identity_conflict": "价格证据与股票、日期或扫描批次不一致",
    "signal_price_basis_bar_invalid": "信号日冻结K线无法证明有效的价格基准",
    "target_adjustment_overlap_missing": "后续行情缺少与冻结信号日重叠的K线，无法核对复权基准",
    "target_adjustment_overlap_invalid": "与信号日重叠的后续K线无效或重复",
    "target_adjustment_rebase_conflict": "同一信号日的新旧价格不一致，可能发生复权基准变化",
    "official_execution_evidence_unavailable": "缺少通过严格校验的正式执行证据，不能据日K价格假定成交",
    "failed_source_contract_unresolved": "失败批次的评分合同无法确定，不能排除样本选择偏差",
    "trusted_calendar_unavailable": "可信交易日历不足，无法固定完整比较日期",
    "mature_calendar_anchors_missing": "计划中的已到期锚点存在缺失，不压缩日期轴做统计检验",
    "minimum_complete_anchors_not_met": "独立完整锚点数量不足，尚不能判断统计优势",
    "no_familywise_statistical_winner": "没有模板在多重比较校正后同时优于其余两套模板",
    "cost_stress_advantage_not_confirmed": "压力成本下尚未保持对其余模板的正收益差",
    "independent_batch_drawdown_limit_exceeded": "独立批次回撤恶化超过预定风险上限",
    "retrospective_template_selection": "当前比较使用已经发生的历史，不是事前固定的前瞻验证",
    "independent_prospective_validation_missing": "缺少独立前瞻验证，不能证明未来收益优势",
    "continuous_capital_and_position_validation_missing": "尚未验证连续资金、持仓延续及实际调仓",
    "selection_blocked": "冻结选择未通过准入，不能建立净收益样本",
    "no_selection": "未选中股票，本独立批次保留现金",
    "empty_ready_selection": "草案状态与空篮不一致，无法确认现金结果",
    "outcome_not_mature": "计划退出交易日尚未完成",
    "cost_profile_not_effective": "成本规则在计划入场日尚未生效",
    "execution_evidence_after_as_of": "执行证据可得时间晚于本报告截止",
    "entry_session_evidence_missing": "缺少计划入场日的正式执行证据",
    "cash_or_prior_capacity_below_minimum_lot": "资金或前一日成交容量不足以买入最小整手，保留现金",
    "holding_path_evidence_missing": "持有路径缺少正式执行证据，整篮结果不可比",
    "corporate_action_ledger_required": "发生公司行动，需要额外持仓调整账本才能比较",
    "nonfinite_position_valuation": "持仓估值溢出或无效，未将其计作零收益",
    "position_quantity_not_representable": "模拟股数超出可精确计算范围，该批次净收益不可用",
    "nonfinite_exit_amount": "计划退出金额溢出或无效",
    "exit_quantity_rule_conflict": "计划卖出数量不符合交易规则",
    "exit_prior_session_capacity_exceeded": "计划卖出超过前一日成交容量约束",
    "exit_fee_cash_shortfall": "退出费用超出可用现金",
    "session_evidence_missing": "缺少交易日执行证据",
    "previous_session_missing": "缺少前一交易日价格与成交证据",
    "previous_close_reference_conflict": "前收盘与公司行动参考价不一致",
    "session_valuation_missing": "交易日估值所需价格或成交量缺失",
    "listing_rule_ineligible": "上市状态不符合交易规则",
}
_STYLE = """
:root{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:#203748;background:#f2f6f7;line-height:1.6}*{box-sizing:border-box}body{margin:0}main{max-width:1200px;
margin:auto;padding:36px 24px 64px}h1{font-size:clamp(1.8rem,4vw,2.5rem);line-height:1.2;margin:10px 0}
h2{font-size:1.2rem;margin:0 0 12px}h3{font-size:1rem;margin:0 0 12px}p{margin:8px 0}header{margin-bottom:24px}
p,h1,h2,h3,.card strong,.tag{overflow-wrap:anywhere}
.eyebrow{font-size:.8rem;font-weight:700;letter-spacing:.12em;color:#236958}.muted,small{color:#566c7b}
.notice{background:#e8f3ed;border-left:4px solid #2d8068;padding:15px 18px;border-radius:5px}
.warning{background:#fff3e7;border-left:4px solid #b77327;padding:15px 18px;border-radius:5px}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:22px 0}.card,section{
padding:22px;background:white;border:1px solid #d8e3e7;border-radius:12px;min-width:0}.card strong{
display:block;font-size:1.7rem;font-variant-numeric:tabular-nums}.card .muted{font-size:.85rem}
section{margin-top:22px}details{border:1px solid #d8e3e7;padding:16px;border-radius:9px;margin-top:15px;
min-width:0}summary{cursor:pointer;font-weight:650;overflow-wrap:anywhere}details[open]>summary{margin-bottom:16px}
.table-wrap{overflow-x:auto;max-width:100%;border:1px solid #dce6ea;border-radius:8px}table{
border-collapse:collapse;width:100%;font-size:.85rem}caption{text-align:left;padding:12px;background:#f3f7f8;
font-weight:650}th,td{padding:11px;text-align:left;border-bottom:1px solid #e5ecef;vertical-align:top}
th{white-space:nowrap;background:#f3f7f8}tbody tr:last-child td{border-bottom:0}.number{white-space:nowrap;
font-variant-numeric:tabular-nums}.break{overflow-wrap:anywhere;word-break:break-word}.identity{max-width:280px;
min-width:160px}.counts{display:flex;gap:8px 20px;flex-wrap:wrap;margin:12px 0;font-size:.88rem}
.tag{display:inline-block;background:#edf2f5;padding:2px 9px;border-radius:20px;font-size:.8rem}
.metadata{display:grid;grid-template-columns:150px minmax(0,1fr);gap:5px 16px;font-size:.86rem}
dt{color:#566c7b}dd{margin:0;overflow-wrap:anywhere}.empty{padding:20px;background:#f3f7f8;border-radius:8px}
pre{font-size:.78rem;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#f3f7f8;
padding:14px;border-radius:7px;max-width:100%}.subline{display:block;color:#566c7b;font-size:.78rem;margin-top:3px}
footer{margin-top:24px;font-size:.8rem;color:#566c7b}.limits{padding-left:22px}.limits li{margin:8px 0}
@media(max-width:640px){main{padding:24px 12px 40px}.cards{grid-template-columns:1fr;gap:10px}.card,section{
padding:16px}details{padding:12px}.metadata{grid-template-columns:1fr;gap:3px}dd{margin-bottom:8px}th,td{
padding:9px}.notice,.warning{padding:12px}}
@media print{body{background:white}main{max-width:none;padding:0}section,.card{break-inside:avoid}.table-wrap{
overflow:visible}th,td{padding:6px}}
"""


def _text(value: object, fallback: str = "—") -> str:
    if value is None or value == "":
        return escape(fallback)
    if isinstance(value, (Mapping, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return escape(str(value), quote=True)


def _rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _count(value: object) -> str:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return "—"
    return f"{number:.2E}" if number >= 1e12 else str(int(number))


def _percent(value: object, *, difference: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "—"
    scaled = number * 100
    displayed = f"{scaled:+.2f}" if math.isfinite(scaled) and abs(scaled) < 1e8 else f"{Decimal(str(number)) * 100:+.2E}"
    return displayed + (" 个百分点" if difference else "%")


def _money(value: object) -> str:
    number = _number(value)
    if number is None or number < 0:
        return "—"
    return f"¥{number:.2E}" if number >= 1e12 else f"¥{number:,.2f}"


def _decimal(value: object) -> str:
    number = _number(value)
    if number is None or number < 0:
        return "—"
    return f"{number:.2E}" if number >= 1e12 else f"{number:.1f}"


def _status(value: object) -> str:
    raw = str(value) if value is not None else ""
    return '<span class="tag">' + _text(_STATUS_LABELS.get(raw, raw)) + "</span>"


def _template_name(value: object) -> str:
    raw = str(value) if value is not None else ""
    return _text(_TEMPLATE_LABELS.get(raw, raw))


def _reason_counts(value: object) -> str:
    counts = _mapping(value)
    if not counts:
        return "未记录额外原因"
    return "；".join(f"{_reason(reason)} × {_count(count)}" for reason, count in counts.items())


def _reason(value: object) -> str:
    if not isinstance(value, str):
        return _text(value)
    if value.startswith("candidate:") and "|baseline:" in value:
        candidate, baseline = value.removeprefix("candidate:").split("|baseline:", 1)
        return f'候选{_text(_STATUS_LABELS.get(candidate, candidate))}，基线{_text(_STATUS_LABELS.get(baseline, baseline))}'
    return _text(_REASON_LABELS.get(value, value))


def _provenance(value: object) -> str:
    return _text({
        "official_raw_file_verified": "正式执行原始文件已校验",
        "synthetic": "合成测试数据，不具备正式证据资格", "unavailable": "正式执行证据不可用",
    }.get(str(value), value))


def _position_row(position: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="break">{_text(position.get("symbol"))}<span class="subline">{_text(position.get("name"))}</span></td>'
        f'<td class="break">{_text(position.get("industry"))}</td><td class="number">{_percent(position.get("target_weight"))}</td>'
        f'<td class="number">{_money(position.get("signal_price"))}</td><td class="number">{_percent(position.get("forward_return"))}</td>'
        f'<td class="break">{_reason(position.get("outcome_reason"))}</td></tr>'
    )


def _selection_detail(selection: Mapping[str, object]) -> str:
    positions = _rows(selection.get("positions"))
    gross_return = selection.get("weighted_gross_return") if selection.get("outcome_status") == "available" else None
    counts = "".join(f'<span>{label} <b>{_count(value)}</b></span>' for label, value in (
        ("已评估", selection.get("evaluated_count")), ("通过准入", selection.get("eligible_count")),
        ("选中股票", len(positions)), ("结果可用", selection.get("available_outcome_count")),
    ))
    table = _table("冻结持仓与信号价格后续表现", ("股票", "行业", "目标权重", "信号价", "后续毛收益", "缺失说明"),
                   "".join(_position_row(row) for row in positions)) if positions else '<p class="empty">未选中股票，查看现金及筛选原因。</p>'
    return (
        f'<details><summary>{_template_name(selection.get("template_id"))} · {_status(selection.get("outcome_status"))}</summary>'
        f'<div class="counts">{counts}<span>选股状态 {_status(selection.get("status"))}</span></div>'
        f'<p>目标投入权重 {_percent(selection.get("target_invested_weight"))} · 剩余现金 {_money(selection.get("residual_cash_cny"))}</p>'
        f'<p class="muted">未配置权重 {_percent(selection.get("unallocated_weight"))}；其现金收益按零作诊断，不将缺失股票权重分配给其余股票。</p>'
        f'<p>加权后续毛收益 <b>{_percent(gross_return)}</b></p>'
        f'<p class="muted">往返成本估算 {_money(selection.get("estimated_round_trip_cost_cny"))}；仅展示规划估算，不从后续毛收益直接扣减。</p>'
        f'<p class="break">筛选原因汇总：{_reason_counts(selection.get("rejection_counts"))}</p>'
        f'<p class="break">结果缺失汇总：{_reason_counts(selection.get("missing_reason_counts"))}</p>'
        f'<p class="break muted">说明：{_text(selection.get("reasons"))}</p>{table}'
        '<details><summary>执行规格与结果身份</summary><dl class="metadata">'
        f'<dt>执行规格指纹</dt><dd>{_text(selection.get("execution_fingerprint"))}</dd>'
        f'<dt>结果摘要</dt><dd>{_text(selection.get("result_digest"))}</dd></dl></details></details>'
    )


def _table(caption: str, headers: Sequence[str], body: str) -> str:
    return (
        '<div class="table-wrap" tabindex="0" role="region" aria-label="对照明细，可横向滚动"><table>'
        f'<caption>{_text(caption)}</caption><thead><tr>'
        + "".join(f'<th scope="col">{_text(header)}</th>' for header in headers)
        + f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _session_detail(session: Mapping[str, object]) -> str:
    selections = _rows(session.get("selections"))
    return (
        f'<details><summary>{_text(session.get("signal_date"))} → {_text(session.get("target_date"), "目标日待确认")} · '
        f'批次 {_text(session.get("run_id"))}</summary><dl class="metadata">'
        f'<dt>快照摘要</dt><dd>{_text(session.get("snapshot_digest"))}</dd>'
        f'<dt>发布时间</dt><dd>{_text(session.get("published_at"))}</dd></dl>{_session_comparisons(selections)}'
        + "".join(_selection_detail(selection) for selection in selections) + "</details>"
    )


def _session_comparisons(selections: Sequence[Mapping[str, object]]) -> str:
    by_id = {str(item.get("template_id")): item for item in selections}
    baseline = by_id.get("bounded_medium_trend", {})
    base_return = _number(baseline.get("weighted_gross_return"))
    comparisons = []
    for template_id in ("medium_momentum", "low_volatility_trend"):
        candidate = by_id.get(template_id, {})
        value = _number(candidate.get("weighted_gross_return"))
        comparable = candidate.get("outcome_status") == baseline.get("outcome_status") == "available"
        spread = value - base_return if comparable and value is not None and base_return is not None else None
        comparisons.append(f"{_template_name(template_id)}较基线：{_percent(spread, difference=True)}")
    return '<p class="muted">同日完整配对差：' + "；".join(comparisons) + "</p>"


def _template_summary(summary: Mapping[str, object]) -> str:
    counts = _mapping(summary.get("outcome_counts"))
    statuses = " · ".join(f"{_text(_STATUS_LABELS[key])} {_count(counts.get(key, 0))}" for key in (
        "available", "pending", "missing", "no_selection", "calendar_unavailable", "blocked",
    ))
    return (
        f'<article class="card"><h3>{_template_name(summary.get("template_id"))}</h3>'
        f'<strong>{_decimal(summary.get("mean_selected_count"))} <small>只</small></strong>'
        '<p class="muted">每个冻结信号日的平均选股数</p>'
        f'<p>平均投入 {_percent(summary.get("mean_target_invested_weight"))}</p>'
        f'<p class="muted">平均往返成本估算 {_money(summary.get("mean_estimated_round_trip_cost_cny"))}</p>'
        f'<p class="muted">{statuses}</p></article>'
    )


def _comparison_row(comparison: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="identity">{_template_name(comparison.get("template_id"))}</td>'
        f'<td class="number">{_count(comparison.get("paired_session_count"))}</td>'
        f'<td class="number">{_percent(comparison.get("candidate_average_return"))}</td>'
        f'<td class="number">{_percent(comparison.get("baseline_average_return"))}</td>'
        f'<td class="number">{_percent(comparison.get("candidate_minus_baseline_return"), difference=True)}</td>'
        f'<td class="number">{_decimal(comparison.get("mean_selected_overlap_count"))}</td></tr>'
    )


def _cohort(cohort: Mapping[str, object], index: int) -> str:
    comparisons = _rows(cohort.get("comparisons"))
    table = _table("双方结果完整的同一信号日；每个配对日期等权", (
        "候选模板", "配对日期数", "候选毛收益均值", "基线毛收益均值", "候选较基线差", "各日期平均重叠股票数",
    ), "".join(_comparison_row(item) for item in comparisons))
    explanations = "".join(
        f'<p class="break"><b>{_template_name(item.get("template_id"))}</b> · {_count(item.get("paired_session_count"))} 个同日配对 · '
        f'较基线差 <b>{_percent(item.get("candidate_minus_baseline_return"), difference=True)}</b></p>'
        f'<p class="break muted">未配对原因 {_reason_counts(item.get("excluded_pair_counts"))}</p>'
        f'<details><summary>{_template_name(item.get("template_id"))} · 实际配对日期</summary>'
        f'<p class="break">{_text(item.get("paired_dates"))}</p></details>' for item in comparisons
    )
    summaries = "".join(_template_summary(item) for item in _rows(cohort.get("template_summaries")))
    sessions = "".join(_session_detail(item) for item in _rows(cohort.get("sessions")))
    return (
        f'<section><h2>评分合同分组 {index}</h2><dl class="metadata">'
        f'<dt>规则版本</dt><dd>{_text(cohort.get("rule_version"))}</dd>'
        f'<dt>评分指纹</dt><dd>{_text(cohort.get("score_spec_hash"))}</dd>'
        f'<dt>冻结信号日数</dt><dd>{_count(cohort.get("session_count"))}</dd></dl>'
        f'<div class="cards">{summaries}</div>{table}'
        '<p class="muted">每项候选的基线均值仅使用该项候选的配对日期，两个候选对应的基线均值可能不同。'
        '平均重叠股票数覆盖本组全部冻结日期，不代表独立样本数量。</p>'
        f'{explanations}<details><summary>逐日选择与持仓详情</summary>{sessions}</details></section>'
    )


def _audit_section(report: Mapping[str, object]) -> str:
    source = _mapping(report.get("source"))
    failures = _rows(report.get("run_failures"))
    excluded = _rows(report.get("excluded_runs"))
    entries = "".join(
        f'<li class="break">批次 {_text(item.get("run_id"))} · {_reason(item.get("reason"))}'
        '</li>' for item in failures
    )
    exclusions = "".join(
        f'<li class="break">批次 {_text(item.get("run_id"))} · {_reason(item.get("reason"))}</li>' for item in excluded
    )
    notice = '<p class="warning">存在读取或封存校验失败的批次，当前报告存在证据缺口。</p>' if failures else ""
    failure_counts = Counter(str(item.get("reason") or "frozen_snapshot_unavailable") for item in failures)
    return (
        f'<section><h2>数据与读取结果</h2>{notice}<p>读取失败 {_count(len(failures))} 批次 · '
        f'排除 {_count(len(excluded))} 批次</p>'
        + (f'<p class="break">失败原因汇总：{_reason_counts(failure_counts)}</p>' if failures else "")
        + '<dl class="metadata">'
        f'<dt>本地数据库</dt><dd>{_text(source.get("database"))}</dd>'
        f'<dt>已选批次数</dt><dd>{_count(source.get("selected_run_count"))}</dd></dl>'
        f'<details><summary>失败与排除原因</summary><ul>{entries}{exclusions}</ul>'
        + ('<p>没有记录失败或排除批次。</p>' if not entries and not exclusions else "")
        + "</details></section>"
    )


def _specifications(report: Mapping[str, object]) -> str:
    templates = _rows(report.get("templates"))
    details = "".join(
        f'<details><summary>{_template_name(item.get("template_id"))} · 初始规格、固定费率与身份</summary>'
        f'<pre>{escape(json.dumps(dict(item), ensure_ascii=False, indent=2, default=str), quote=True)}</pre></details>'
        for item in templates
    )
    return (
        '<section><h2>固定模板规格</h2><p class="muted">三个模板使用各自初始规格。'
        '这里保留完整费率、约束及指纹供审阅，报告不会修改正式排名或执行交易。</p>'
        + details + "</section>"
    )


def _notes(value: object, empty: str) -> str:
    notes = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []
    return '<ul class="limits">' + "".join(f'<li>{_reason(note)}</li>' for note in notes) + "</ul>" if notes else f'<p class="muted">{_text(empty)}</p>'


def _template_or_none(value: object) -> str:
    return _template_name(value) if isinstance(value, str) and value else "尚无明确结果"


def _probability(value: object) -> str:
    number = _number(value)
    if number is None or not 0 <= number <= 1:
        return "—"
    return f"{number:.2E}" if 0 < number < .0001 else f"{number:.4f}"


def _selection_comparison(row: Mapping[str, object]) -> str:
    rejected = row.get("reject_equal_means")
    conclusion = "已通过差异检验" if rejected is True else "未通过差异检验" if rejected is False else "尚不可检验"
    return (
        f'<tr><td class="identity">{_template_name(row.get("left_template_id"))} − {_template_name(row.get("right_template_id"))}</td>'
        f'<td class="number">{_percent(row.get("mean_net_return_difference"), difference=True)}</td>'
        f'<td class="number">{_percent(row.get("mean_stress_net_return_difference"), difference=True)}</td>'
        f'<td class="number">{_probability(row.get("two_sided_p_value"))}</td>'
        f'<td class="number">{_probability(row.get("holm_adjusted_p_value"))}</td><td>{conclusion}</td></tr>'
    )


def _selection_summary(row: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="identity">{_template_name(row.get("template_id"))}</td>'
        f'<td class="number">{_percent(row.get("mean_net_return"))}</td>'
        f'<td class="number">{_percent(row.get("mean_stress_net_return"))}</td>'
        f'<td class="number">{_percent(row.get("worst_independent_batch_drawdown"))}</td></tr>'
    )


def _selection_cohort(cohort: Mapping[str, object], index: int) -> str:
    summary = _table("相同完整锚点日期的净收益比较", ("模板", "基础成本净收益均值", "压力成本净收益均值", "最差独立批次回撤（含压力）"),
                     "".join(_selection_summary(row) for row in _rows(cohort.get("template_summaries"))))
    comparisons = _table("成对差异与多重比较校正；显著差异不等于可采用", (
        "比较方向", "基础净收益差", "压力净收益差", "双侧 p 值", "Holm 校正 p 值", "差异检验",
    ), "".join(_selection_comparison(row) for row in _rows(cohort.get("comparisons"))))
    dates = "".join(f'<dt>{label}</dt><dd>{_text(cohort.get(key))}</dd>' for key, label in (
        ("planned_anchor_dates", "计划锚点"), ("matured_anchor_dates", "已到期锚点"),
        ("pending_anchor_dates", "待到期锚点"), ("missing_anchor_dates", "缺失锚点"),
    ))
    return (
        f'<details open><summary>净收益选择 · 评分合同分组 {index}</summary><dl class="metadata">'
        f'<dt>规则版本</dt><dd>{_text(cohort.get("rule_version"))}</dd><dt>评分指纹</dt><dd>{_text(cohort.get("score_spec_hash"))}</dd>'
        f'<dt>完整锚点数</dt><dd>{_count(cohort.get("complete_anchor_count"))}</dd></dl>'
        '<div class="cards"><article class="card"><h3>诊断领先</h3>'
        f'<strong>{_template_or_none(cohort.get("diagnostic_leader_id"))}</strong><p class="muted">仅描述已观察样本的相对表现。</p></article>'
        '<article class="card"><h3>统计优势</h3>'
        f'<strong>{_template_or_none(cohort.get("statistical_winner_id"))}</strong><p class="muted">须满足预定比较与样本规则，仍属回溯研究。</p></article>'
        '<article class="card"><h3>可采用模板</h3><strong>暂无合格证据</strong>'
        '<p class="muted">保留当前策略；统计优势不直接授予采用资格。</p></article></div>'
        f'<p>压力与风险条件通过者：{_template_or_none(cohort.get("risk_qualified_winner_id"))}；通过仍不等于可采用。</p>'
        + _notes(cohort.get("blockers"), "本分组未记录额外阻断原因，仍须满足独立采用资格。")
        + summary + comparisons + f'<details><summary>计划与实际锚点日期</summary><dl class="metadata">{dates}</dl></details></details>'
    )


def _strategy_selection_section(report: Mapping[str, object]) -> str:
    selection = _mapping(report.get("strategy_selection"))
    if selection.get("schema_version") != "strategy-template-selection-v1":
        return '<section id="strategy-selection"><h2>哪套策略更好</h2><p class="empty">未生成净收益选择结论，保留当前策略。</p><p>历史毛收益领先不能代替净收益与采用资格证明。</p></section>'
    cohorts = _rows(selection.get("cohorts"))
    body = "".join(_selection_cohort(row, index) for index, row in enumerate(cohorts, 1))
    return (
        '<section id="strategy-selection"><h2>哪套策略更好</h2>'
        '<p class="warning">当前是回溯净收益比较，没有合格的采用证据，保留当前策略。'
        '诊断领先、统计优势与可采用资格分别展示，不跨评分合同合并赢家。</p>'
        + _notes(selection.get("adoption_blockers"), "尚未形成独立的前瞻采用证据。")
        + (body or '<p class="empty">没有可评估的净收益选择分组。</p>')
        + '<details><summary>固定统计比较规则</summary><pre>'
        + _text(selection.get("inference_contract")) + "</pre></details></section>"
    )


def _available_metric(result: Mapping[str, object], field: str) -> object:
    return result.get(field) if result.get("status") == "available" else None


def _net_selection_row(selection: Mapping[str, object]) -> str:
    base, stress = _mapping(selection.get("base")), _mapping(selection.get("stress"))
    return (
        f'<tr><td class="identity">{_template_name(selection.get("template_id"))}</td><td>{_status(selection.get("status"))}</td>'
        f'<td class="number">{_percent(_available_metric(base, "net_return"))}</td>'
        f'<td class="number">{_percent(_available_metric(stress, "net_return"))}</td>'
        f'<td class="number">{_money(_available_metric(base, "total_fees_cny"))}</td>'
        f'<td class="number">{_money(_available_metric(stress, "total_fees_cny"))}</td>'
        f'<td class="number">{_percent(_available_metric(base, "independent_batch_max_drawdown"))}</td>'
        f'<td class="number">{_percent(_available_metric(stress, "independent_batch_max_drawdown"))}</td></tr>'
    )


def _net_position_row(position: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="identity">{_text(position.get("symbol"))}</td><td>{_status(position.get("status"))}</td>'
        f'<td class="number">{_percent(position.get("target_weight"))}</td><td class="number">{_count(position.get("quantity"))}</td>'
        f'<td class="number">{_money(position.get("buy_amount_cny"))}</td><td class="number">{_money(position.get("sell_amount_cny"))}</td>'
        f'<td class="number">{_money(position.get("buy_fees_cny"))}</td><td class="number">{_money(position.get("sell_fees_cny"))}</td>'
        f'<td class="break">{_reason(position.get("reason"))}</td></tr>'
    )


def _net_execution_detail(selection: Mapping[str, object], scenario: str, label: str) -> str:
    result = _mapping(selection.get(scenario))
    counts = " · ".join(f'{name} {_count(result.get(key))}' for key, name in (
        ("filled_count", "模拟成交"), ("unfilled_count", "未成交"), ("unavailable_count", "证据不可用"),
    ))
    rows = _rows(result.get("positions"))
    positions = _table("独立批次成交诊断明细", ("股票", "状态", "原目标权重", "数量", "买入金额", "卖出金额", "买入费用", "卖出费用", "原因"),
                       "".join(_net_position_row(row) for row in rows)) if rows else '<p>没有持仓成交明细。</p>'
    return (
        f'<details><summary>{_template_name(selection.get("template_id"))} · {label}成交明细</summary>'
        f'<p>{_status(result.get("status"))} · {counts}</p>'
        f'<p>批次期末现金（退出后） {_money(_available_metric(result, "residual_cash_cny"))}</p>'
        f'<p class="break">原因汇总：{_reason_counts(result.get("reason_counts"))}</p>{positions}</details>'
    )


def _net_session(session: Mapping[str, object]) -> str:
    selections = _rows(session.get("selections"))
    table = _table("同一冻结选择的独立批次净收益；未到期或证据不足不计零", (
        "模板", "状态", "基础净收益", "压力净收益", "基础费用", "压力费用", "基础单批回撤", "压力单批回撤",
    ), "".join(_net_selection_row(item) for item in selections))
    reasons = "".join(f'<p class="break">{_template_name(item.get("template_id"))}</p>'
                      + _notes(item.get("reason_codes"), "未记录额外原因。") for item in selections)
    details = "".join(_net_execution_detail(item, scenario, label) for item in selections
                      for scenario, label in (("base", "基础成本"), ("stress", "压力成本")))
    return (
        f'<details><summary>信号 {_text(session.get("signal_date"))} · 批次 {_text(session.get("run_id"))}</summary>'
        f'<p>计划入场 {_text(session.get("entry_date"))} 开盘 → 计划退出 {_text(session.get("exit_date"))} 收盘</p>'
        f'<p class="break muted">冻结摘要 {_text(session.get("snapshot_digest"))}</p>{table}{reasons}{details}</details>'
    )


def _net_comparison_section(report: Mapping[str, object]) -> str:
    net = _mapping(report.get("net_comparison"))
    if net.get("schema_version") != "strategy-template-net-returns-v1":
        return '<section id="net-comparison"><h2>净收益比较</h2><p class="empty">尚未生成可成交时点下的净收益比较；历史毛收益不自动转换为净收益。</p></section>'
    cohorts = _rows(net.get("cohorts"))
    body = "".join(
        f'<details open><summary>净执行 · 评分合同分组 {index}</summary><dl class="metadata">'
        f'<dt>规则版本</dt><dd>{_text(row.get("rule_version"))}</dd><dt>评分指纹</dt><dd>{_text(row.get("score_spec_hash"))}</dd></dl>'
        + "".join(_net_session(session) for session in _rows(row.get("sessions"))) + "</details>"
        for index, row in enumerate(cohorts, 1)
    )
    evidence = _mapping(net.get("execution_evidence"))
    return (
        '<section id="net-comparison"><h2>净收益比较 · D+1 → D+H+1</h2>'
        '<p class="notice">信号后下一交易日开盘入场，固定 D+H+1 收盘退出，分别计算基础与压力成本。'
        '这是有执行证据约束的独立批次模拟，不是实际成交记录或连续组合净值；单批回撤不能当作连续策略回撤。</p>'
        '<p>入场未成交保留对应现金，不补位；退出受阻或路径证据未知则整篮不可比。'
        '压力成本可能改变同一目标预算下的成交股数；回撤按日收盘估值，不代表盘中最大损失。</p>'
        f'<p>观察截止 {_text(net.get("as_of"))} · 持有 {_count(net.get("horizon_sessions"))} 个交易日 · '
        f'名义资金 {_money(net.get("notional_cash_cny"))}</p>'
        f'<p class="break">执行证据来源状态 {_provenance(evidence.get("provenance_status"))}</p>'
        + (body or '<p class="empty">没有可供净执行比较的批次；不会据此选出获胜模板。</p>')
        + '<details><summary>固定成本情景与执行证据身份</summary><pre>'
        + _text({"cost_specs": net.get("cost_specs"), "execution_evidence": evidence})
        + "</pre></details></section>"
    )


def render_strategy_template_tracking_html(report: Mapping[str, object]) -> str:
    cohorts = _rows(report.get("cohorts"))
    body = "".join(_cohort(item, index) for index, item in enumerate(cohorts, 1))
    limits = report.get("limitations")
    limit_items = list(limits) if isinstance(limits, Sequence) and not isinstance(limits, (str, bytes)) else []
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>策略模板对照</title><style>{_STYLE}</style></head><body><main><header>'
        '<p class="eyebrow">A SHARE RADAR · 策略研究</p><h1>策略模板对照</h1>'
        '<p class="muted">固定基线、中期动量与低波趋势，检查相同历史日期的选择差异。</p>'
        f'<p>{_status(report.get("status"))} · 截至 {_text(report.get("as_of_completed_date"))}</p>'
        '<p class="notice">先查看净收益选择证据，再核对独立批次与历史毛收益。'
        '信号价格至固定 D+H 收盘的毛收益诊断不是净值、实际成交收益或预测命中率。'
        '各评分合同分别统计；毛收益回看只比较双方结果完整的同一信号日，日期等权。</p>'
        f'<div class="cards"><article class="card">可用毛收益分组<strong>{len(cohorts)}</strong></article>'
        f'<article class="card">已准入信号日<strong>{_count(report.get("source_session_count"))}</strong></article>'
        f'<article class="card">毛收益观察期<strong>D+{_count(report.get("horizon_sessions"))}</strong></article></div>'
        f'<p>每个方案的名义资金 {_money(report.get("notional_cash_cny"))}；基线为“基线 · 中期趋势”。</p></header>'
        + _strategy_selection_section(report) + _net_comparison_section(report)
        + '<section><h2>历史毛收益回看 · D → D+H</h2><p>以下保留冻结信号价格起算的历史诊断，不作为净收益选择或采用结论。</p></section>'
        + (body or '<section><p class="empty">没有可用的冻结扫描分组，尚不能比较策略模板。请查看读取结果。</p></section>')
        + _audit_section(report) + _specifications(report)
        + '<section><h2>结果边界</h2><p>历史毛收益部分的成本估算独立展示，未计算净收益、净值、置信区间或自动晋级结论。'
        '新增净收益与差异检验只在对应区域展示，回溯统计优势不等于前瞻采用资格；没有合格证据时保留当前策略。</p>'
        + '<ul class="limits">' + "".join(f"<li>{_text(item)}</li>" for item in limit_items)
        + f'</ul></section><footer class="break">生成时间 {_text(report.get("generated_at"))} · '
        f'报告规格 {_text(report.get("schema_version"))} · 本报告无需联网资源。</footer></main></body></html>'
    )


__all__ = ["render_strategy_template_tracking_html"]
