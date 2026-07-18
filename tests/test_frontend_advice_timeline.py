from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_empty_and_single_snapshot_states_show_current_advice_semantics() -> None:
    script = r'''
      const target = timelineTarget();
      globalThis.document = { getElementById() { return target; } };
      const { formatAdviceTime, renderAdviceTimeline } = await import("./static/js/advice-timeline.js");

      if (!renderAdviceTimeline([]) || !target.innerHTML.includes("暂无核心分析建议变化")) {
        throw new Error(`empty timeline was not a valid empty state: ${target.innerHTML}`);
      }

      const item = {
        id: 1,
        action: "观察",
        confidence: 68,
        market_time: "2026-07-15 14:55:00",
        created_at: "2026-07-15 15:01:00",
        updated_at: "2026-07-15 15:01:00",
        trend_label: "偏强",
        trend_score: 72,
        risk_level: "中等",
        data_quality_level: "良好",
        data_quality_score: 86,
        data_quality_source: "日线收盘快照",
        conclusion_basis: "analysis_action_advice",
        snapshot_contract_version: "2",
        rule_version: "rules-7",
        model_version: "model-3",
        comparison_status: "no_previous",
        has_changes: false,
        changes: [],
      };
      if (!renderAdviceTimeline([item])) throw new Error("single snapshot was rejected");
      const html = target.innerHTML;
      for (const text of [
        "观察 · 建议强度 68/100",
        "市场时间 2026-07-15 14:55:00",
        "记录时间 2026-07-15 15:01:00",
        "偏强 · 72/100",
        "中等",
        "良好 · 86/100 · 来源 日线收盘快照",
        "无更早可比留痕",
      ]) {
        if (!html.includes(text)) throw new Error(`single snapshot omitted ${text}: ${html}`);
      }
      if (html.includes("68%") || html.includes("首次")) {
        throw new Error(`single snapshot used probability/first-record wording: ${html}`);
      }
      if (html.includes("<b>结论依据</b>analysis_action_advice")) {
        throw new Error(`metadata enum was rendered as explanatory body text: ${html}`);
      }
      if (!html.includes("结论口径 analysis_action_advice")) {
        throw new Error(`conclusion-basis metadata was missing from provenance: ${html}`);
      }
      if (formatAdviceTime({ created_at: "A", updated_at: "B" }) !== "A 至 B") {
        throw new Error("record-time range formatting regressed");
      }

      function timelineTarget() {
        return { innerHTML: "", attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
      }
    '''
    _run_node_script(script)


def test_multiple_changes_continuation_and_version_change_render_neutrally() -> None:
    script = r'''
      const target = { innerHTML: "", setAttribute() {} };
      globalThis.document = { getElementById() { return target; } };
      const { renderAdviceTimeline } = await import("./static/js/advice-timeline.js");

      const base = {
        action: "控制风险",
        confidence: 74,
        market_time: "2026-07-15 14:55:00",
        created_at: "2026-07-15 15:01:00",
        trend_label: "震荡",
        trend_score: 61,
        risk_level: "偏高",
        data_quality_level: "良好",
        data_quality_score: 82,
      };
      renderAdviceTimeline([
        {
          ...base,
          comparison_status: "comparable",
          has_changes: true,
          changes: [
            { category: "action", field: "action", before: "观察", after: "控制风险", comparable: true },
            { category: "trend", field: "trend_score", before: 67, after: 61, delta: -6, direction: "worsened", comparable: true },
            { category: "price_levels", field: "support", before: null, after: 98.25, comparable: true },
          ],
        },
        { ...base, comparison_status: "comparable", has_changes: false, changes: [] },
        {
          ...base,
          comparison_status: "version_changed",
          has_changes: true,
          changes: [
            { category: "data_quality", field: "data_quality_score", before: 79, after: 82, direction: "improved", comparable: false },
          ],
        },
      ]);

      const html = target.innerHTML;
      for (const text of [
        "自上次保留快照以来 3 项变化",
        "动作 · 建议动作",
        "趋势 · 趋势评分",
        "支撑 / 压力 · 支撑位",
        "结论延续",
        "分析版本已变化，记录差异仅作中性展示，不作方向判断。",
        "查看 1 项记录差异",
      ]) {
        if (!html.includes(text)) throw new Error(`comparison timeline omitted ${text}: ${html}`);
      }
      if ((html.match(/<details/g) || []).length !== 2 || !html.includes("<b>前</b>观察") || !html.includes("<b>后</b>控制风险")) {
        throw new Error(`before/after details were incomplete: ${html}`);
      }
      if (html.includes("worsened") || html.includes("improved") || html.includes("改善") || html.includes("恶化") || html.includes("is-good") || html.includes("is-risk")) {
        throw new Error(`direction metadata leaked into neutral comparison UI: ${html}`);
      }
    '''
    _run_node_script(script)


def test_legacy_and_malformed_payloads_degrade_without_non_finite_values() -> None:
    script = r'''
      const target = { innerHTML: "", setAttribute() {} };
      globalThis.document = { getElementById() { return target; } };
      const { renderAdviceTimeline } = await import("./static/js/advice-timeline.js");

      renderAdviceTimeline([{
        id: 9,
        action: "观察",
        confidence: 60,
        created_at: "2026-06-01 10:00:00",
        updated_at: "2026-06-01 11:00:00",
        trend_label: "震荡",
        trend_score: 55,
        risk_level: "中等",
        data_quality_level: "优秀",
        data_quality_score: 88,
        reason: "旧留痕",
      }]);
      if (!target.innerHTML.includes("快照版本未知，无法与上一条保留快照直接横比。")) {
        throw new Error(`old history shape was not rendered as legacy: ${target.innerHTML}`);
      }
      if (!target.innerHTML.includes("建议强度 60/100") || !target.innerHTML.includes("记录时间 2026-06-01 10:00:00 至 2026-06-01 11:00:00")) {
        throw new Error(`old history fields were not safely retained: ${target.innerHTML}`);
      }

      if (renderAdviceTimeline(null) || !target.innerHTML.includes("核心分析建议变化暂不可用")) {
        throw new Error(`null response did not degrade explicitly: ${target.innerHTML}`);
      }
      if (renderAdviceTimeline([null, [], {}]) || !target.innerHTML.includes("保留快照内容无法安全展示")) {
        throw new Error(`malformed array did not degrade explicitly: ${target.innerHTML}`);
      }

      renderAdviceTimeline([{
        action: null,
        confidence: Infinity,
        market_time: null,
        created_at: null,
        trend_label: ["bad"],
        trend_score: NaN,
        risk_level: { bad: true },
        data_quality_level: null,
        data_quality_score: -Infinity,
        comparison_status: "comparable",
        has_changes: true,
        changes: [{ category: "price_levels", field: "support", before: 1e308, after: Infinity }],
      }]);
      const malformedHtml = target.innerHTML;
      if (!malformedHtml.includes("建议待确认 · 建议强度 --/100") || !malformedHtml.includes("1e+308") || !malformedHtml.includes("不可用")) {
        throw new Error(`malformed fields did not use safe fallbacks: ${malformedHtml}`);
      }
      for (const unsafe of ["Infinity", "NaN", "[object Object]"]) {
        if (malformedHtml.includes(unsafe)) throw new Error(`malformed value leaked as ${unsafe}: ${malformedHtml}`);
      }

      renderAdviceTimeline([{
        action: "观察",
        confidence: 50,
        comparison_status: "comparable",
        has_changes: true,
        changes: "not-an-array",
      }]);
      if (!target.innerHTML.includes("变化明细暂不可用") || target.innerHTML.includes("not-an-array")) {
        throw new Error(`malformed changes array was not safely downgraded: ${target.innerHTML}`);
      }
    '''
    _run_node_script(script)


def test_every_rendered_server_string_is_html_escaped() -> None:
    script = r'''
      const target = { innerHTML: "", setAttribute() {} };
      globalThis.document = { getElementById() { return target; } };
      const { renderAdviceTimeline, renderAdviceTimelineUnavailable } = await import("./static/js/advice-timeline.js");
      const attack = '<img src=x onerror="globalThis.pwned=1">';
      renderAdviceTimeline([{
        action: attack,
        confidence: 60,
        market_time: attack,
        created_at: attack,
        trend_label: attack,
        trend_score: 55,
        risk_level: attack,
        data_quality_level: attack,
        data_quality_score: 80,
        data_quality_source: attack,
        conclusion_basis: attack,
        snapshot_contract_version: attack,
        rule_version: attack,
        model_version: attack,
        comparison_status: "comparable",
        has_changes: true,
        changes: [{ category: attack, field: attack, before: '<script>before()</script>', after: attack, direction: attack }],
      }]);
      if (target.innerHTML.includes("<img") || target.innerHTML.includes("<script>")) {
        throw new Error(`server HTML was rendered as markup: ${target.innerHTML}`);
      }
      if (!target.innerHTML.includes("&lt;img") || !target.innerHTML.includes("&lt;script&gt;before()&lt;/script&gt;")) {
        throw new Error(`escaped server text was lost: ${target.innerHTML}`);
      }

      renderAdviceTimelineUnavailable(new Error('<svg onload="globalThis.pwned=2">'));
      if (target.innerHTML.includes("<svg") || !target.innerHTML.includes("&lt;svg")) {
        throw new Error(`failure message was not escaped: ${target.innerHTML}`);
      }
      if (globalThis.pwned) throw new Error("server payload executed script");
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
