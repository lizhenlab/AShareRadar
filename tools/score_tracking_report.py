"""Render an offline, self-contained report of frozen score groups and outcomes."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from html import escape


_STATUS_LABELS = {
    "paired": "已配对",
    "pending": "待到期",
    "missing": "缺失",
    "not_comparable": "不可比",
    "calendar_unavailable": "交易日历缺失",
    "insufficient_data": "证据不足",
    "ok": "可查看描述结果",
}
_MODE_LABELS = {"official": "收盘扫描", "intraday": "盘中扫描", "preopen": "盘前扫描"}
_SELECTION_LABELS = {
    "earliest-published-per-contract-session": "同一评分口径、同一信号日，使用最早发布的扫描批次，避免事后挑选较好结果。",
    "provided-snapshots": "使用本次提供的历史快照；无法据此确认快照选择是否独立于后续表现。",
}
_REASON_LABELS = {
    "frozen_snapshot_unavailable": "冻结快照无法读取或未通过封存校验",
    "failed_frozen_snapshots": "存在无法读取或未通过封存校验的冻结批次",
    "insufficient_or_duplicate_frozen_members": "冻结成员不足或存在重复股票",
    "invalid_frozen_score": "冻结评分无效",
    "score_tails_overlap_or_no_score_difference": "高低分组重叠或没有分数差异",
    "trusted_calendar_target_unavailable": "可信交易日历无法确定目标日",
    "target_session_not_completed": "目标交易日尚未结束",
    "matured_group_outcome_coverage_below_minimum": "已到期分组的有效结果覆盖不足",
    "unobserved_signal_session_gaps": "跟踪期间有未记录信号的交易日",
    "minimum_paired_session_count": "有效配对日期不足",
    "incomplete_matured_target_dates": "存在结果不完整的已到期日期",
    "minimum_high_group_sample_size": "高分组有效样本不足",
    "minimum_low_group_sample_size": "低分组有效样本不足",
    "unverified_score_contract": "评分口径无法核验",
    "calendar_coverage_unavailable": "交易日历覆盖不足，部分目标日期尚无法确定",
}
_STYLE = """
:root{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:#20364a;background:#f3f6f8;line-height:1.6}*{box-sizing:border-box}body{margin:0}main{max-width:1200px;
margin:auto;padding:40px 24px 64px}h1{font-size:clamp(1.65rem,4vw,2.4rem);line-height:1.2;margin:12px 0}
h2{font-size:1.2rem;margin:0 0 16px}p{margin:8px 0}header{margin-bottom:28px}.eyebrow{color:#286659;
font-weight:700;letter-spacing:.08em;font-size:.8rem}.muted,small{color:#586d7c}.notice{border-left:4px solid
#307e70;background:#e8f3ef;padding:14px 18px;border-radius:4px}.cards{display:grid;grid-template-columns:
repeat(4,minmax(0,1fr));gap:14px;margin:24px 0}.card,section{background:white;border:1px solid #d9e2e8;
border-radius:12px;padding:22px}.card strong{display:block;font-size:1.9rem;font-variant-numeric:tabular-nums}
section{margin-top:20px;min-width:0}.table-wrap{max-width:100%;overflow-x:auto;border:1px solid #dce5eb;
border-radius:8px}table{border-collapse:collapse;width:100%;font-size:.875rem}caption{text-align:left;
font-weight:600;padding:12px;background:#f4f8fa}th,td{padding:12px;text-align:left;border-bottom:1px solid
#e6edf1;vertical-align:top}th{background:#f2f6f8;white-space:nowrap}tbody tr:last-child td{border-bottom:0}
td.number{white-space:nowrap;font-variant-numeric:tabular-nums}td.identity{min-width:170px;max-width:250px}
.break{overflow-wrap:anywhere;word-break:break-word}.tag{display:inline-block;padding:2px 9px;border-radius:20px;
background:#edf2f6;font-size:.8rem;white-space:nowrap}.paired{background:#e4f2ec;color:#185843}
.pending{background:#fff3d6;color:#715817}.missing,.not_comparable,.calendar_unavailable{background:#f9e9e6;color:#834a3c}
details{margin-top:14px;border:1px solid #dce5eb;border-radius:8px;padding:16px;min-width:0}
summary{cursor:pointer;font-weight:650;overflow-wrap:anywhere}details[open] summary{margin-bottom:16px}
.metadata{display:grid;grid-template-columns:130px minmax(0,1fr);gap:7px 16px;font-size:.88rem}
dt{color:#586d7c}dd{margin:0;overflow-wrap:anywhere}.session-note{font-size:.85rem;min-width:160px;max-width:300px}
.empty{padding:28px;background:#f4f8fa;border-radius:8px}.limits{padding-left:22px}.limits li{margin:9px 0}
.failure-notice{background:#fff3ed;border-left:4px solid #b86231;padding:14px 18px;border-radius:4px}
footer{margin-top:24px;font-size:.8rem;color:#586d7c}.counts{display:flex;gap:8px 20px;flex-wrap:wrap;
margin:16px 0;font-size:.85rem}.subline{display:block;font-size:.78rem;color:#586d7c;margin-top:4px}
@media(max-width:640px){main{padding:24px 12px 40px}.cards{grid-template-columns:repeat(2,minmax(0,1fr));
gap:10px}.card,section{padding:16px}.card strong{font-size:1.6rem}.metadata{grid-template-columns:1fr;gap:3px}
dd{margin-bottom:8px}details{padding:12px}th,td{padding:10px}.notice{padding:12px}}
@media print{body{background:white}main{max-width:none;padding:0}.table-wrap{overflow:visible}section,
.card,details{break-inside:avoid}.cards{gap:8px}th,td{padding:6px}}
"""


def _text(value: object, fallback: str = "待确认") -> str:
    if value is None or value == "":
        return escape(fallback)
    if isinstance(value, (Mapping, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return escape(str(value), quote=True)


def _rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _count(value: object) -> str:
    number = _number(value)
    return str(int(number)) if number is not None and number >= 0 and number.is_integer() else "—"


def _percent(value: object, *, difference: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "—"
    suffix = " 个百分点" if difference else "%"
    return f"{number * 100:+.2f}{suffix}"


def _coverage(value: object) -> str:
    number = _number(value)
    return f"{number * 100:.0f}%" if number is not None and 0 <= number <= 1 else "—"


def _status(value: object) -> str:
    raw = str(value) if value is not None else ""
    css = raw if raw in {"paired", "pending", "missing", "not_comparable", "calendar_unavailable"} else ""
    return f'<span class="tag {css}">{_text(_STATUS_LABELS.get(raw, raw))}</span>'


def _reason(value: object, fallback: str = "—") -> str:
    label = _REASON_LABELS.get(value, value) if isinstance(value, str) else value
    return _text(label, fallback)


def _interval(value: object) -> str:
    if isinstance(value, Mapping):
        low = value.get("lower", value.get("low"))
        high = value.get("upper", value.get("high"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = value
    else:
        return "—"
    if _number(low) is None or _number(high) is None:
        return "—"
    return f"{_percent(low, difference=True)} 至 {_percent(high, difference=True)}"


def _identity(cohort: Mapping[str, object]) -> str:
    mode = str(cohort.get("mode", ""))
    return (
        f"{_text(_MODE_LABELS.get(mode, mode))} · {_text(cohort.get('scope'))}"
        f'<span class="subline break">规则 {_text(cohort.get("rule_version"))}</span>'
        f'<span class="subline break">评分指纹 {_text(cohort.get("score_spec_hash"))}</span>'
    )


def _comparison_row(cohort: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="identity">{_identity(cohort)}</td>'
        f'<td class="number">{_count(cohort.get("horizon_trading_days"))} 个交易日</td>'
        f'<td>{_status(cohort.get("status"))}</td>'
        f'<td class="number">{_count(cohort.get("paired_session_count"))} / '
        f'{_count(cohort.get("expected_session_count"))}</td>'
        f'<td class="number">{_percent(cohort.get("high_average_return"))}</td>'
        f'<td class="number">{_percent(cohort.get("low_average_return"))}</td>'
        f'<td class="number">{_percent(cohort.get("high_minus_low_return"), difference=True)}</td>'
        f'<td class="number">{_interval(cohort.get("confidence_interval_95"))}</td></tr>'
    )


def _session_row(session: Mapping[str, object]) -> str:
    return (
        f'<tr><td class="identity break">{_text(session.get("run_id"))}</td>'
        f'<td class="number">{_text(session.get("signal_date"))}</td>'
        f'<td class="number">{_text(session.get("target_date"))}</td>'
        f'<td>{_status(session.get("status"))}</td>'
        f'<td class="number">{_count(session.get("high_available_count"))} / '
        f'{_count(session.get("high_frozen_count"))}<span class="subline">'
        f'覆盖 {_coverage(session.get("high_coverage"))}</span></td>'
        f'<td class="number">{_count(session.get("low_available_count"))} / '
        f'{_count(session.get("low_frozen_count"))}<span class="subline">'
        f'覆盖 {_coverage(session.get("low_coverage"))}</span></td>'
        f'<td class="number">{_percent(session.get("high_return"))}</td>'
        f'<td class="number">{_percent(session.get("low_return"))}</td>'
        f'<td class="number">{_percent(session.get("spread"), difference=True)}</td>'
        f'<td class="session-note break">{_reason(session.get("reason"))}</td></tr>'
    )


def _reasons(value: object) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = [_reason(item) for item in value if item is not None and item != ""]
        return "；".join(values) or "未提供额外原因；仍需遵守下方证据限制。"
    return _reason(value, "未提供额外原因；仍需遵守下方证据限制。")


def _cohort_detail(cohort: Mapping[str, object], index: int) -> str:
    sessions = _rows(cohort.get("sessions"))
    counts = "".join(
        f"<span>{label} <b>{_count(cohort.get(key))}</b></span>"
        for key, label in (
            ("expected_session_count", "应跟踪"), ("mature_session_count", "已到期"),
            ("pending_session_count", "待到期"), ("missing_session_count", "到期未配对"),
            ("paired_session_count", "已配对"),
            ("unobserved_session_count", "未记录信号的交易日"),
            ("prospective_recording_session_count", "提前封存且 PIT 核验日期"),
        )
    )
    incomparable = sum(session.get("status") == "not_comparable" for session in sessions)
    missing_calendar = _count(cohort.get(
        "calendar_unavailable_session_count", sum(session.get("status") == "calendar_unavailable" for session in sessions),
    ))
    table = (
        '<div class="table-wrap" tabindex="0" role="region" aria-label="逐日跟踪明细，可横向滚动">'
        '<table><caption>每日冻结组的到期结果；覆盖为有效结果数 / 冻结股票数</caption><thead><tr>'
        '<th scope="col">批次 ID</th><th scope="col">信号日</th><th scope="col">目标日</th>'
        '<th scope="col">状态</th><th scope="col">高分组</th><th scope="col">低分组</th>'
        '<th scope="col">高分组收益</th><th scope="col">低分组收益</th><th scope="col">收益差</th>'
        '<th scope="col">说明</th></tr></thead><tbody>'
        + "".join(_session_row(session) for session in sessions)
        + "</tbody></table></div>"
        if sessions else '<p class="empty">此分组暂无逐日记录，暂时无法评估评分与后续表现的关系。</p>'
    )
    return (
        f'<details><summary>分组 {index} · {_text(cohort.get("scope"))} · '
        f'{_text(cohort.get("rule_version"))} · D+{_count(cohort.get("horizon_trading_days"))}</summary>'
        f'<p class="break">{_identity(cohort)}</p><p>{_status(cohort.get("status"))}</p>'
        f'<div class="counts">{counts}<span>不可比 <b>{incomparable}</b></span>'
        f'<span>交易日历缺失 <b>{missing_calendar}</b></span></div>'
        '<p class="muted">到期未配对包含结果缺失、不可比及信号日缺口；分类计数不可直接相加。'
        '交易日历缺失单独展示，目标日未知时不计入已到期或待到期。</p>'
        '<p class="muted">提前封存与 PIT 核验只说明封存时点及价格证据检查通过，不代表预测有效。</p>'
        f'<p class="muted break">证据说明：{_reasons(cohort.get("insufficient_reasons"))}</p>{table}</details>'
    )


def _summary_cards(cohorts: list[Mapping[str, object]]) -> str:
    values = [("可比口径分组", str(len(cohorts)))]
    for label, key in (
        ("已配对记录", "paired_session_count"), ("待到期记录", "pending_session_count"),
        ("到期未配对记录", "missing_session_count"),
    ):
        numbers = [_number(cohort.get(key)) for cohort in cohorts]
        value = str(sum(int(number) for number in numbers if number is not None and number >= 0))
        values.append((label, value))
    return '<div class="cards">' + "".join(
        f'<div class="card"><span class="muted">{label}</span><strong>{value}</strong></div>' for label, value in values
    ) + "</div>"


def _failure_section(report: Mapping[str, object]) -> str:
    failures = _rows(report.get("run_failures"))
    source = report.get("source")
    declared = _number(source.get("failed_run_count")) if isinstance(source, Mapping) else None
    count = max(len(failures), int(declared) if declared is not None and declared >= 0 else 0)
    if count == 0:
        return ""
    rows = "".join(
        f'<tr><td class="break">{_text(row.get("run_id"))}</td>'
        f'<td class="number">{_text(row.get("signal_date"))}</td>'
        f'<td class="break">{_reason(row.get("reason"))}</td>'
        f'<td class="break">{_text(row.get("error_type"))}</td></tr>' for row in failures
    )
    table = (
        '<div class="table-wrap" tabindex="0" role="region" aria-label="失败批次明细，可横向滚动">'
        '<table><caption>未进入统计的批次</caption><thead><tr><th scope="col">批次 ID</th>'
        '<th scope="col">信号日</th><th scope="col">失败原因</th><th scope="col">错误类型</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>' if rows else ""
    )
    missing_detail = (
        f'<p class="muted">已提供 {len(failures)} 条失败明细，其余 {count - len(failures)} 条明细缺失。</p>'
        if count > len(failures) else ""
    )
    return (
        '<section><h2>失败批次</h2><div class="failure-notice">'
        f'<b>{count} 个批次未能完成快照读取或封存校验。</b>这些批次未进入统计，当前结果证据不足；'
        '不能把未纳入的批次当作没有发生，也不能把空结果解释为全部校验通过。</div>'
        f'{missing_detail}{table}</section>'
    )


def render_score_tracking_html(report: Mapping[str, object]) -> str:
    """Render report data without scripts, network dependencies, or raw dynamic HTML."""
    cohorts = _rows(report.get("cohorts"))
    policy = report.get("selection_policy")
    policy_label = _SELECTION_LABELS.get(policy, policy) if isinstance(policy, str) else policy
    comparison = (
        '<div class="table-wrap" tabindex="0" role="region" aria-label="同版高低分对比，可横向滚动">'
        '<table><caption>仅在相同模式、范围、规则版本、评分指纹与观察期限内比较</caption><thead><tr>'
        '<th scope="col">评分口径</th><th scope="col">观察期限</th><th scope="col">证据状态</th>'
        '<th scope="col">配对 / 应跟踪</th><th scope="col">高分组平均收益</th>'
        '<th scope="col">低分组平均收益</th><th scope="col">高分减低分</th>'
        '<th scope="col">收益差 95% 区间</th></tr></thead><tbody>'
        + "".join(_comparison_row(cohort) for cohort in cohorts)
        + "</tbody></table></div>"
        if cohorts else '<p class="empty">暂无可跟踪的评分记录。保留扫描时的评分与分组，待目标交易日到期并补齐行情后再查看。</p>'
    )
    details = "".join(_cohort_detail(cohort, index) for index, cohort in enumerate(cohorts, 1))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
        f'<title>评分后续表现跟踪</title><style>{_STYLE}</style></head><body><main><header>'
        '<span class="eyebrow">A 股研究 · 评分验证</span><h1>高分，后来表现更好吗？</h1>'
        '<p class="muted">跟踪已冻结的高低分组，在同一评分口径下核对到期表现。</p>'
        f'<p class="muted break">已完成交易日截至 {_text(report.get("as_of_completed_date"))} · '
        f'报告生成 {_text(report.get("generated_at"))}</p></header>'
        '<div class="notice"><b>评分是排序指标，不是上涨概率。</b><br>'
        '此报告用于诊断评分与后续表现的关系，不自动批准模型上线，也不证明预测有效。</div>'
        f'{_summary_cards(cohorts)}<p class="muted">记录数按评分口径与观察期限累计，同一天可能对应多条记录，不能当作独立样本数。</p>'
        f'{_failure_section(report)}'
        '<section><h2>同版高低分对比</h2><p class="muted">“—”表示缺少可用结果，不按零收益处理。'
        f'收益差以百分点表示。窄屏可左右滑动表格。</p>{comparison}</section>'
        f'<section><h2>逐日跟踪</h2><p class="muted">展开各分组查看批次、到期状态与两组覆盖。</p>'
        f'{details or "<p>暂无批次明细。待到期、缺失和不可比记录将分别展示。</p>"}</section>'
        '<section><h2>选组规则</h2><dl class="metadata"><dt>选组策略</dt>'
        f'<dd>{_text(policy_label, "尚未提供，无法确认选组口径。")}</dd>'
        f'<dt>报告格式版本</dt><dd>{_text(report.get("schema_version"))}</dd></dl></section>'
        '<section><h2>证据限制</h2><ul class="limits">'
        '<li>高分减低分是两个组的描述性收益差，不是可执行多空策略收益；未据此推导扣费后的组合净值。</li>'
        '<li>同日股票与重叠持有期存在相关性。配对记录数和区间不意味着独立样本充分，也不自动证明统计有效。</li>'
        '<li>待到期、行情缺失、覆盖不足或口径不可比的记录不能作为零收益纳入结论。请结合逐日状态与两组覆盖阅读。</li>'
        '<li>只能比较同一评分口径。历史回看、样本筛选和反复调参可能影响结果；新规则仍需未参与调参的数据验证。</li>'
        '<li>执行效果还需要成交约束、手续费、滑点、涨跌停、停牌与仓位评估。本报告不自动触发模型晋级或交易。</li>'
        '</ul></section><footer>此文件可离线打开，不含脚本或外部资源。数据与规则的有效性以审计输入为准。</footer>'
        '</main></body></html>'
    )
