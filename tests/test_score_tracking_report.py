from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import pytest

from tools.score_tracking_report import render_score_tracking_html


def _report() -> dict[str, Any]:
    return {
        "schema_version": "score-tracking.v1",
        "generated_at": "2026-09-19T10:00:00+08:00",
        "as_of_completed_date": "2026-09-18",
        "score_semantics": "ordinal-not-probability",
        "promotion_eligible": False,
        "selection_policy": "earliest-published-per-contract-session",
        "cohorts": [{
            "mode": "official", "scope": "全市场", "rule_version": "v3", "score_spec_hash": "a" * 64,
            "horizon_trading_days": 5, "status": "insufficient_data", "expected_session_count": 4,
            "mature_session_count": 3, "pending_session_count": 1, "missing_session_count": 1,
            "paired_session_count": 1, "high_average_return": 0.0, "low_average_return": -0.012,
            "high_minus_low_return": 0.012, "confidence_interval_95": [-0.003, 0.027],
            "insufficient_reasons": ["有效日期不足"],
            "sessions": [{
                "run_id": "scan-20260911-01", "signal_date": "2026-09-11", "target_date": "2026-09-18",
                "status": "paired", "high_frozen_count": 10, "low_frozen_count": 10,
                "high_available_count": 10, "low_available_count": 8, "high_coverage": 1.0,
                "low_coverage": 0.8, "high_return": 0.0, "low_return": -0.012, "spread": 0.012, "reason": None,
            }],
        }],
    }


class _HtmlProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(attrs)


def test_report_renders_frozen_groups_zero_negative_returns_and_coverage() -> None:
    html = render_score_tracking_html(_report())
    for expected in (
        "高分，后来表现更好吗？", "全市场", "收盘扫描", "v3", "scan-20260911-01", "2026-09-18",
        "+0.00%", "-1.20%", "+1.20 个百分点", "-0.30 个百分点 至 +2.70 个百分点",
        "覆盖 100%", "覆盖 80%", "10 / 10", "8 / 10", "有效日期不足", "待到期", "缺失", "不可比", "已配对",
    ):
        assert expected in html
    assert "评分是排序指标，不是上涨概率" in html
    assert "不是可执行多空策略收益" in html
    assert "不自动批准模型上线" in html
    assert "不能当作独立样本数" in html
    assert "到期未配对记录" in html
    assert "到期未配对包含结果缺失、不可比及信号日缺口" in html


def test_report_escapes_all_dynamic_strings() -> None:
    report = _report()
    attack = '<script src="https://invalid.test/x">&\'"</script>'
    for key in ("schema_version", "generated_at", "as_of_completed_date", "selection_policy"):
        report[key] = attack
    cohort = report["cohorts"][0]
    for key in ("mode", "scope", "rule_version", "score_spec_hash", "status"):
        cohort[key] = attack
    cohort["insufficient_reasons"] = [attack]
    session = cohort["sessions"][0]
    for key in ("run_id", "signal_date", "target_date", "status", "reason"):
        session[key] = attack
    html = render_score_tracking_html(report)
    assert attack not in html
    assert "&lt;script src=&quot;https://invalid.test/x&quot;&gt;&amp;&#x27;&quot;&lt;/script&gt;" in html
    parser = _HtmlProbe()
    parser.feed(html)
    assert "script" not in parser.tags
    assert all(key not in {"src", "href"} and not key.startswith("on") for key, _ in parser.attributes)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf"), True, "1.2"])
def test_report_does_not_convert_unavailable_returns_to_zero(value: object) -> None:
    report = _report()
    cohort = report["cohorts"][0]
    for key in ("high_average_return", "low_average_return", "high_minus_low_return"):
        cohort[key] = value
    cohort["confidence_interval_95"] = [value, value]
    session = cohort["sessions"][0]
    for key in ("high_return", "low_return", "spread", "high_coverage", "low_coverage"):
        session[key] = value
    html = render_score_tracking_html(report)
    assert "+0.00%" not in html
    assert "nan%" not in html and "inf%" not in html
    assert '<td class="number">—</td>' in html
    assert "覆盖 —" in html


@pytest.mark.parametrize("report", [{}, {"cohorts": []}, {"cohorts": None}, {"cohorts": "bad-input"}])
def test_empty_report_explains_what_is_missing(report: dict[str, object]) -> None:
    html = render_score_tracking_html(report)
    assert "暂无可跟踪的评分记录" in html
    assert "暂无批次明细" in html
    assert "尚未提供，无法确认选组口径" in html
    assert "已配对记录</span><strong>0</strong>" in html


def test_empty_cohort_and_missing_interval_are_readable() -> None:
    report = _report()
    cohort = report["cohorts"][0]
    cohort.update(sessions=[], confidence_interval_95=None, insufficient_reasons=[])
    html = render_score_tracking_html(report)
    assert "此分组暂无逐日记录" in html
    assert "未提供额外原因" in html
    assert "收益差 95% 区间" in html


@pytest.mark.parametrize("interval", [{"lower": 0, "upper": 0}, {"low": 0, "high": 0}])
def test_mapping_interval_preserves_zero(interval: dict[str, int]) -> None:
    report = _report()
    report["cohorts"][0]["confidence_interval_95"] = interval
    assert "+0.00 个百分点 至 +0.00 个百分点" in render_score_tracking_html(report)


def test_report_uses_semantic_tables_native_details_and_no_external_resources() -> None:
    html = render_score_tracking_html(_report())
    parser = _HtmlProbe()
    parser.feed(html)
    assert parser.tags.count("table") == 2
    assert "details" in parser.tags and "summary" in parser.tags
    assert "caption" in parser.tags
    assert ("lang", "zh-CN") in parser.attributes
    assert ("scope", "col") in parser.attributes
    assert "@media(max-width:640px)" in html
    assert "overflow-x:auto" in html
    assert not {"script", "link", "img", "iframe"} & set(parser.tags)
    assert "http://" not in html and "https://" not in html


def test_pending_missing_and_incomparable_sessions_keep_their_reasons() -> None:
    report = _report()
    cohort = report["cohorts"][0]
    cohort["sessions"] = [
        {"run_id": "run-1", "status": "pending", "reason": "目标交易日尚未结束"},
        {"run_id": "run-2", "status": "missing", "reason": "到期行情缺失"},
        {"run_id": "run-3", "status": "not_comparable", "reason": "评分指纹无法匹配"},
        {"run_id": "run-4", "status": "calendar_unavailable", "reason": "目标日无法确认"},
    ]
    html = render_score_tracking_html(report)
    for text in ("目标交易日尚未结束", "到期行情缺失", "评分指纹无法匹配", "不可比 <b>1</b>", "交易日历缺失 <b>1</b>"):
        assert text in html


@pytest.mark.parametrize("policy", ["earliest-published-per-contract-session", "provided-snapshots"])
def test_selection_policy_is_explained_in_chinese(policy: str) -> None:
    report = _report()
    report["selection_policy"] = policy
    html = render_score_tracking_html(report)
    assert policy not in html
    expected = "使用最早发布的扫描批次" if policy.startswith("earliest") else "使用本次提供的历史快照"
    assert expected in html


def test_empty_cohorts_do_not_hide_failed_frozen_snapshots() -> None:
    report = {
        "cohorts": [], "source": {"failed_run_count": 2},
        "run_failures": [
            {"run_id": 14, "signal_date": "2026-09-15", "reason": "frozen_snapshot_unavailable",
             "error_type": "MarketScanSnapshotSealError"},
        ],
    }
    html = render_score_tracking_html(report)
    assert "2 个批次未能完成快照读取或封存校验" in html
    assert "当前结果证据不足" in html
    assert "已提供 1 条失败明细，其余 1 条明细缺失" in html
    assert "冻结快照无法读取或未通过封存校验" in html
    assert "MarketScanSnapshotSealError" in html
    assert "<caption>未进入统计的批次</caption>" in html
    assert "不能把空结果解释为全部校验通过" in html


def test_failed_count_alone_still_displays_failure_warning() -> None:
    html = render_score_tracking_html({"source": {"failed_run_count": 3}, "cohorts": []})
    assert "3 个批次未能完成快照读取或封存校验" in html
    assert "其余 3 条明细缺失" in html


def test_failures_are_shown_even_when_source_count_is_wrong_and_are_escaped() -> None:
    attack = '<img src=x onerror="alert(1)">'
    report = {
        "source": {"failed_run_count": 0}, "cohorts": [],
        "run_failures": [{key: attack for key in ("run_id", "signal_date", "reason", "error_type")}],
    }
    html = render_score_tracking_html(report)
    assert "1 个批次未能完成快照读取或封存校验" in html
    assert attack not in html
    assert html.count("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;") == 4


def test_no_failed_batches_does_not_show_failure_warning() -> None:
    html = render_score_tracking_html({"source": {"failed_run_count": 0}, "run_failures": [], "cohorts": []})
    assert "<h2>失败批次</h2>" not in html


def test_audit_reason_codes_are_explained_in_chinese() -> None:
    report = _report()
    cohort = report["cohorts"][0]
    cohort["insufficient_reasons"] = ["minimum_paired_session_count", "failed_frozen_snapshots"]
    cohort["sessions"][0]["reason"] = "matured_group_outcome_coverage_below_minimum"
    html = render_score_tracking_html(report)
    assert "有效配对日期不足" in html
    assert "存在无法读取或未通过封存校验的冻结批次" in html
    assert "已到期分组的有效结果覆盖不足" in html
    assert "minimum_paired_session_count" not in html


def test_prospective_and_unobserved_date_counts_keep_their_limits() -> None:
    report = _report()
    cohort = report["cohorts"][0]
    cohort["prospective_recording_session_count"] = 0
    cohort["unobserved_session_count"] = 3
    html = render_score_tracking_html(report)
    assert "提前封存且 PIT 核验日期 <b>0</b>" in html
    assert "未记录信号的交易日 <b>3</b>" in html
    assert "提前封存与 PIT 核验只说明封存时点及价格证据检查通过，不代表预测有效" in html


def test_unavailable_prospective_and_gap_counts_are_not_assumed_zero() -> None:
    html = render_score_tracking_html(_report())
    assert "提前封存且 PIT 核验日期 <b>—</b>" in html
    assert "未记录信号的交易日 <b>—</b>" in html


def test_unknown_calendar_targets_are_shown_separately_from_mature_dates() -> None:
    report = _report()
    cohort = report["cohorts"][0]
    cohort["calendar_unavailable_session_count"] = 2
    cohort["insufficient_reasons"] = ["calendar_coverage_unavailable"]
    html = render_score_tracking_html(report)
    assert "交易日历缺失 <b>2</b>" in html
    assert "目标日未知时不计入已到期或待到期" in html
    assert "交易日历覆盖不足，部分目标日期尚无法确定" in html
