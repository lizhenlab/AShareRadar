from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_paper_dashboard_renders_escaped_strategy_equity_and_model_notes() -> None:
    _run_node(
        r'''
        const elements = paperElements();
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const { renderPaperTradingDashboard } = await import("./static/js/paper-trading.js");
        const state = {
          adviceReviewDashboardDetails: [{ plan: { id: 10, revision: 1, symbol: "600519", hypothesis: "趋势延续" } }],
          paperTradingDashboard: dashboard(),
        };

        renderPaperTradingDashboard(state);

        const strategy = elements.get("paperStrategyList").innerHTML;
        const equity = elements.get("paperEquityChart").innerHTML;
        assert(strategy.includes("持仓中") && strategy.includes("目标 110.00"), "strategy state was not rendered");
        assert(!strategy.includes("<script>"), "strategy content was not escaped");
        assert(equity.includes("<polyline") && equity.includes("2026-07-03"), "equity curve was not rendered");
        assert(elements.get("paperTradingNotes").innerHTML.includes("不连接券商"), "model boundary note was omitted");
        assert(elements.get("paperInitialCash").disabled === true, "funding remained editable after strategy creation");

        function dashboard() {
          return {
            account: { initial_cash: 1000000 },
            performance: {
              total_equity: 1005000, total_return_pct: 0.5, max_drawdown_pct: -0.2,
              cash_balance: 900000, market_value: 105000, realized_pnl: 0,
              open_count: 1, closed_count: 0, win_rate_pct: null,
            },
            strategies: [{
              id: 7, plan_id: 10, plan_revision: 1, symbol: "<script>600519</script>",
              allocation_pct: 10, status: "open", activation_market_time: "2026-07-01 10:00:00",
              entry_date: "2026-07-02", held_sessions: 2, target_price: 110, stop_price: 90,
            }],
            positions: [], trades: [],
            equity_curve: [
              { as_of_date: "2026-07-02", total_equity: 999500, return_pct: -0.05 },
              { as_of_date: "2026-07-03", total_equity: 1005000, return_pct: 0.5 },
            ],
            notes: ["不连接券商，不发送真实委托"],
          };
        }
        function paperElements() {
          const ids = [
            "paperInitialCash", "savePaperAccount", "paperTradingSummary", "paperStrategyList",
            "paperPositionList", "paperTradeList", "paperEquityChart", "paperTradingNotes",
            "paperReviewPlan", "paperTradingFeedback",
          ];
          return new Map(ids.map((id) => [id, { innerHTML: "", value: "", disabled: false, hidden: false, dataset: {}, focus() {} }]));
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_paper_strategy_creation_uses_frozen_plan_endpoint_and_refreshes_dashboard() -> None:
    _run_node(
        r'''
        const elements = paperElements();
        elements.get("paperReviewPlan").value = "10";
        elements.get("paperAllocationPct").value = "25";
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url: String(url), options });
          if (options.method === "POST") return json({ id: 7, symbol: "600519" }, 201);
          return json(dashboard());
        };
        const { createPaperStrategy } = await import("./static/js/paper-trading.js");
        const state = { adviceReviewDashboardDetails: [], paperTradingSeq: 0 };

        await createPaperStrategy(state);

        const request = calls[0];
        const body = JSON.parse(request.options.body);
        assert(request.url === "/api/paper-trading/strategies" && request.options.method === "POST", "wrong strategy endpoint");
        assert(body.plan_id === 10 && body.allocation_pct === 25, "strategy payload lost plan allocation");
        assert(calls[1].url === "/api/paper-trading", "dashboard was not refreshed");
        assert(elements.get("paperTradingFeedback").textContent.includes("加入模拟"), "success feedback was omitted");

        function dashboard() {
          return {
            account: { initial_cash: 1000000 },
            performance: { total_equity: 1000000 },
            strategies: [], positions: [], trades: [], equity_curve: [], notes: [],
          };
        }
        function paperElements() {
          const ids = [
            "paperInitialCash", "savePaperAccount", "paperTradingSummary", "paperStrategyList",
            "paperPositionList", "paperTradeList", "paperEquityChart", "paperTradingNotes",
            "paperReviewPlan", "paperAllocationPct", "paperTradingFeedback",
          ];
          return new Map(ids.map((id) => [id, { innerHTML: "", value: "", disabled: false, hidden: false, dataset: {}, focus() {} }]));
        }
        function json(value, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_paper_dashboard_renders_run_history_events_benchmark_and_exports() -> None:
    _run_node(
        r'''
        const elements = paperElements();
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const { renderPaperTradingDashboard } = await import("./static/js/paper-trading.js");
        const state = {
          adviceReviewDashboardDetails: [],
          paperTradingDashboard: dashboard(),
          paperTradingComparison: comparison(),
        };

        renderPaperTradingDashboard(state);

        assert(elements.get("paperRunHistory").value === "2", "selected historical run was not restored");
        assert(elements.get("paperCompareLeft").value === "1", "left comparison default was not selected");
        assert(elements.get("paperCompareRight").value === "2", "right comparison default was not selected");
        assert(elements.get("paperExportJson").href.endsWith("/runs/2/export.json"), "JSON export did not target selected run");
        assert(elements.get("paperExportEvents").href.includes("dataset=events"), "event export link was omitted");
        assert(elements.get("paperRunMetadata").innerHTML.includes("fingerprint-2"), "provenance fingerprint was omitted");
        assert(elements.get("paperEventList").innerHTML.includes("locked_limit_up"), "execution event ledger was omitted");
        assert(elements.get("paperEquityChart").innerHTML.includes("paper-benchmark-line"), "benchmark curve was omitted");
        assert(elements.get("paperEquityChart").innerHTML.includes("paper-trade-marker"), "trade marker was omitted");
        assert(elements.get("paperRunComparison").innerHTML.includes("总成本"), "run comparison was omitted");
        assert(elements.get("paperTradingSummary").innerHTML.includes("成本侵蚀"), "enhanced cost metric was omitted");
        assert(elements.get("paperStrategyList").innerHTML.includes("规则元数据降级"), "degraded rule quality was hidden");

        function dashboard() {
          const run = (id, profile) => ({
            id, as_of: "2026-07-03 16:00:00", rule_version: "paper-review-plan-v2",
            cost_profile_id: profile, cost_profile_name: profile, cost_profile_version: "2026.07",
            strategy_count: 1, execution_count: 1, closed_count: 0, data_unavailable_count: 0,
            input_fingerprint: `fingerprint-${id}`, data_start_date: "2026-07-01",
            data_end_date: "2026-07-03", data_sources: ["paper-test"],
            benchmark_symbol: "000300.SH", benchmark_status: "available",
            message: "ok", created_at: "2026-07-03T08:00:00.000000Z",
          });
          return {
            account: { initial_cash: 1000000, default_cost_profile: "base" },
            performance: {
              total_equity: 1001000, total_return_pct: 0.1, gross_return_pct: 0.12,
              excess_return_pct: -0.2, max_drawdown_pct: -0.05, cash_balance: 900000,
              market_value: 101000, realized_pnl: 0, total_cost: 20, cost_drag_pct: 0.02,
              open_count: 1, closed_count: 0, win_rate_pct: null, payoff_ratio: null,
              expectancy: null, profit_factor: null, turnover_pct: 10, average_exposure_pct: 10,
              sample_warning: "样本较少", risk_metric_message: "风险指标不可用",
            },
            selected_run_id: 2,
            runs: [run(2, "stress"), run(1, "base")],
            strategies: [{
              id: 7, plan_id: 10, plan_revision: 1, symbol: "600519",
              allocation_pct: 10, priority: 0, status: "data_unavailable",
              activation_market_time: "2026-07-01 10:00:00",
              target_price: 110, stop_price: 90, rule_data_degraded: true,
              error_message: "历史 ST 状态缺失",
            }],
            positions: [],
            trades: [{
              run_id: 2, strategy_id: 7, symbol: "600519", side: "buy",
              trade_date: "2026-07-02", price: 100, quantity: 1000,
              gross_amount: 100000, commission_amount: 5, stamp_duty_amount: 0,
              transfer_fee_amount: 1, slippage_amount: 20, friction_amount: 26,
              reason: "strategy_entry",
            }],
            events: [{
              run_id: 2, sequence: 1, strategy_id: 7, symbol: "600519",
              event_date: "2026-07-02", event_code: "locked_limit_up",
              category: "execution", severity: "warning", message: "一字涨停未成交",
            }],
            equity_curve: [
              { as_of_date: "2026-07-02", total_equity: 999974, benchmark_equity: 1000000, return_pct: -0.0026, drawdown_pct: -0.0026 },
              { as_of_date: "2026-07-03", total_equity: 1001000, benchmark_equity: 1003000, return_pct: 0.1, drawdown_pct: 0 },
            ],
            notes: ["不会发送真实委托"],
          };
        }
        function comparison() {
          const performance = {
            total_return_pct: 0.1, gross_return_pct: 0.12, excess_return_pct: -0.2,
            max_drawdown_pct: -0.05, total_cost: 20, win_rate_pct: null, profit_factor: null,
          };
          return {
            left_run: { id: 1 }, right_run: { id: 2 },
            left_performance: performance, right_performance: performance,
            deltas: { total_return_pct: 0, gross_return_pct: 0, excess_return_pct: 0,
              max_drawdown_pct: 0, total_cost: 0, win_rate_pct: null, profit_factor: null },
          };
        }
        function paperElements() {
          const ids = [
            "paperInitialCash", "paperDefaultCostProfile", "savePaperAccount",
            "paperTradingSummary", "paperStrategyList", "paperPositionList",
            "paperTradeList", "paperEventList", "paperEquityChart", "paperTradingNotes",
            "paperReviewPlan", "paperTradingFeedback", "paperRunHistory",
            "paperCompareLeft", "paperCompareRight", "paperExportJson",
            "paperExportTrades", "paperExportEvents", "paperRunMetadata",
            "paperRunComparison",
          ];
          return new Map(ids.map((id) => [id, {
            innerHTML: "", value: "", href: "", disabled: false, hidden: false,
            dataset: {}, focus() {},
          }]));
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_paper_dashboard_load_failure_renders_actionable_error_state() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["paperTradingSummary", { innerHTML: "" }],
          ["paperTradingFeedback", { textContent: "", hidden: true, dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => new Response(
          JSON.stringify({ detail: "行情服务暂不可用" }),
          { status: 503, headers: { "Content-Type": "application/json" } }
        );
        const { loadPaperTradingDashboard } = await import("./static/js/paper-trading.js");
        const loaded = await loadPaperTradingDashboard({ paperTradingSeq: 0 });

        assert(loaded === false, "failed request was reported as loaded");
        assert(elements.get("paperTradingSummary").innerHTML.includes("模拟交易暂不可用"), "error state title was omitted");
        assert(elements.get("paperTradingFeedback").hidden === false, "error feedback stayed hidden");
        assert(elements.get("paperTradingFeedback").dataset.tone === "error", "error tone was omitted");
        assert(elements.get("paperTradingFeedback").textContent.includes("行情服务暂不可用"), "server detail was omitted");

        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_paper_trading_workspace_is_present_in_static_markup() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="workspace-tab-paper"' in html
    assert 'id="workspace-panel-paper"' in html
    assert 'id="paperTradingSummary"' in html
    assert 'id="paperEquityChart"' in html and 'aria-label="模拟账户净值曲线"' in html
    assert 'id="paperRunHistory"' in html
    assert 'id="paperEventList"' in html
    assert 'id="paperCostProfile"' in html
    assert 'id="paperBenchmarkSymbol"' in html
    assert "不会发送真实委托" in html


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
