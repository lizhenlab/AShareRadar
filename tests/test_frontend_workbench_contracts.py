from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_raw_v2_workbench_contract_binds_identity_children_local_rows_and_times() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { validateStockWorkbenchResponse } from "./static/js/workbench-contracts.js";

      const symbolPaths = [
        ["feature_snapshot"], ["factor_lab"], ["market_regime"], ["signal_validation"],
        ["risk_reward"], ["timeframe_alignment"], ["alpha_evidence"], ["diagnosis"],
        ["evidence_chain"], ["qa_report"], ["event_digest"], ["peer_comparison"],
        ["t_strategy"], ["risk_radar"],
        ["chip_analysis"], ["leadership"], ["theme_context"], ["replay"], ["chart_marks"],
        ["insights", "overview"], ["insights", "fund_flow"], ["insights", "order_pressure"],
        ["insights", "events"], ["insights", "financial_health"], ["insights", "valuation"],
        ["insights", "lhb"], ["insights", "abnormal_events"], ["insights", "rule_matches"],
      ];

      const valid = rawWorkbench();
      const result = validateStockWorkbenchResponse(valid, "600519.SH");
      assert.equal(result.symbol, "600519.SH");
      assert.equal(result.research_cohort.signal_date, "2026-08-13");
      assert.equal(result.research_cohort.daily_bar_cutoff, "2026-08-12");

      expectRejected("wrong expected identity", (value) => value, "请求股票", "000001.SZ");
      expectRejected("wrong exchange suffix", (value) => { value.symbol = "600519.SZ"; }, "交易所");
      expectRejected("quote identity", (value) => { value.analysis.quote.code = "601318"; }, "行情与请求股票不一致");
      expectRejected("requested cohort identity", (value) => { value.research_cohort.requested_symbol = "601318.SH"; }, "cohort 股票身份冲突");
      expectRejected("observed cohort identity", (value) => { value.research_cohort.observed_symbol = "601318.SH"; }, "cohort 股票身份冲突");

      for (const path of symbolPaths) {
        expectRejected(`child ${path.join(".")}`, (value) => {
          let child = value;
          for (const part of path) child = child[part];
          child.symbol = "601318.SH";
        }, `${path.join(".")} 与工作台股票不一致`);
      }
      expectRejected("strategy card owner", (value) => {
        value.insights.strategy_cards[0].symbol = "601318.SH";
      }, "insights.strategy_cards[0] 与工作台股票不一致");
      expectRejected("strategy card time", (value) => {
        value.insights.strategy_cards[0].updated_at = "2026-08-12T16:31:30Z";
      }, "insights.strategy_cards[0] 更新时间晚于研究决策时点");
      for (const field of ["stock_profile", "review"]) {
        expectRejected(`analysis.${field}`, (value) => {
          value.analysis[field].symbol = "601318.SH";
        }, `analysis.${field} 与工作台股票不一致`);
      }
      for (const field of ["alert_rules", "alert_events", "notes"]) {
        expectRejected(`${field} row`, (value) => { value[field][0].symbol = "601318.SH"; }, `${field}[0]`);
        expectRejected(`${field} collection`, (value) => { value[field] = {}; }, `${field} 必须是数组`);
      }

      expectRejected("quote after decision", (value) => {
        value.analysis.quote.timestamp = "2026-08-12T16:32:00Z";
        value.research_cohort.quote_event_time = "2026-08-12T16:32:00Z";
      }, "行情时间晚于研究决策时点");
      expectRejected("quote timestamp not cohort-bound", (value) => {
        value.research_cohort.quote_event_time = "2026-08-12T16:29:00Z";
      }, "行情时间未绑定研究 cohort");
      expectRejected("signal date uses Shanghai timezone", (value) => {
        value.research_cohort.signal_date = "2026-08-12";
      }, "信号日与行情事件日不一致");
      expectRejected("cutoff after signal", (value) => {
        value.research_cohort.daily_bar_cutoff = "2026-08-14";
      }, "日K截止晚于信号日");
      expectRejected("cutoff not last daily bar", (value) => {
        value.research_cohort.daily_bar_cutoff = "2026-08-11";
      }, "日K晚于研究截止日");
      expectRejected("daily bars out of order", (value) => {
        value.analysis.klines.reverse();
      }, "日K必须严格递增");
      expectRejected("daily bar after cutoff", (value) => {
        value.analysis.klines.push({ date: "2026-08-13" });
      }, "日K晚于研究截止日");
      expectRejected("invalid signal calendar date", (value) => {
        value.research_cohort.signal_date = "2026-02-30";
      }, "必须是 ISO 日期");
      expectRejected("future timestamp", (value) => {
        value.generated_at = "2099-01-01T00:00:00Z";
      }, "不晚于当前时间");
      expectRejected("future decision timestamp", (value) => {
        value.generated_at = "2099-01-01T00:00:01Z";
        value.context_generated_at = "2099-01-01T00:00:00Z";
        value.research_cohort.decision_time = "2099-01-01T00:00:00Z";
      }, "不晚于当前时间");
      expectRejected("generated before decision context", (value) => {
        value.generated_at = "2026-08-12T16:30:30Z";
      }, "响应时间与研究上下文不一致");
      expectRejected("context must exactly bind decision text", (value) => {
        value.context_generated_at = "2026-08-13T00:31:00+08:00";
      }, "响应时间与研究上下文不一致");
      expectRejected("research child from old signal day", (value) => {
        value.event_digest.updated_at = "2026-08-12T15:30:00Z";
      }, "event_digest 更新时间与信号日不一致");
      expectRejected("research child after decision", (value) => {
        value.risk_radar.updated_at = "2026-08-12T16:31:30Z";
      }, "risk_radar 更新时间晚于研究决策时点");

      function rawWorkbench() {
        const symbol = "600519.SH";
        const owned = (extra = {}) => ({ symbol, ...extra });
        const researchOwned = (extra = {}) => ({ symbol, updated_at: "2026-08-12T16:30:00Z", ...extra });
        const insights = Object.fromEntries([
          "overview", "fund_flow", "order_pressure", "events", "financial_health",
          "valuation", "lhb", "abnormal_events", "rule_matches",
        ].map((key) => [key, researchOwned()]));
        insights.strategy_cards = [researchOwned({ name: "突破确认策略" })];
        const value = {
          schema_version: "stock-workbench-v2",
          symbol,
          generated_at: "2026-08-12T16:32:00Z",
          context_generated_at: "2026-08-12T16:31:00Z",
          research_mode: "interactive_shadow",
          production_effect: "none",
          diagnosis_production_effect: "none",
          research_cohort: {
            requested_symbol: symbol,
            observed_symbol: symbol,
            mode: "interactive_shadow",
            decision_time: "2026-08-12T16:31:00Z",
            quote_event_time: "2026-08-12T16:30:00Z",
            signal_date: "2026-08-13",
            daily_bar_cutoff: "2026-08-12",
            production_effect: "none",
            advice_persistence: "disabled",
          },
          analysis: {
            quote: { code: "600519", market: "SH", timestamp: "2026-08-12T16:30:00Z" },
            klines: [{ date: "2026-08-11" }, { date: "2026-08-12" }],
            stock_profile: owned(),
            review: owned(),
          },
          insights,
          alert_rules: [owned({ id: 1 })],
          alert_events: [owned({ id: 2 })],
          notes: [owned({ id: 3 })],
        };
        for (const path of symbolPaths.filter((path) => path.length === 1)) {
          value[path[0]] = path[0] === "chart_marks" ? owned() : researchOwned();
        }
        return value;
      }

      function expectRejected(label, mutate, message, expectedSymbol = "600519.SH") {
        const value = structuredClone(rawWorkbench());
        mutate(value);
        assert.throws(
          () => validateStockWorkbenchResponse(value, expectedSymbol),
          (error) => error?.name === "StockWorkbenchContractError" && error.message.includes(message),
          label,
        );
      }
    '''
    _run_node_script(script)


def test_generic_json_fixture_never_auto_upgrades_legacy_workbench_payloads() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { jsonResponse, legacyWorkbenchResponse } from "./tests/frontend_app_flow_helpers.mjs";

      const legacy = {
        analysis: {
          quote: { code: "600519", market: "SH", timestamp: "2026-08-12T10:00:00+08:00" },
          klines: [],
        },
      };
      const genericPayload = await jsonResponse(legacy).json();
      assert.strictEqual(genericPayload, legacy);
      assert.equal(genericPayload.schema_version, undefined);

      const explicitlyUpgraded = await legacyWorkbenchResponse(legacy).json();
      assert.equal(explicitlyUpgraded.schema_version, "stock-workbench-v2");
      assert.equal(explicitlyUpgraded.symbol, "600519.SH");
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)
