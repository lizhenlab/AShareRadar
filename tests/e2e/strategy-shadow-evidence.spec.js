import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";


test("Shadow evidence is honest and usable on desktop and mobile Chromium", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("chromium"), "desktop/mobile Chromium are the product gates");
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      if (url.pathname === "/api/strategy-lab/strategies") {
        return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
      }
      return null;
    },
  });

  await page.goto("/");
  await page.locator('[data-primary-view="market"]').click();
  await page.locator("#strategyLab").evaluate((element) => {
    element.hidden = false;
    element.open = true;
  });
  await page.evaluate(async (evidence) => {
    const contracts = await import("/static/js/strategy-lab-contracts.js");
    const view = await import("/static/js/strategy-lab-view.js");
    contracts.validateEvidence(evidence);
    view.renderEvidence(
      { strategyEvidenceContent: document.getElementById("strategyEvidenceContent") },
      evidence,
    );
  }, evidencePayload());

  const center = page.locator("#strategyEvidenceContent");
  await center.scrollIntoViewIfNeeded();
  await expect(center.locator(".strategy-shadow-boundary")).toContainText("影子研究，不改变生产排名");
  await expect(center.locator(".strategy-shadow-boundary")).toContainText("生产排名写入：否");
  await center.locator('.strategy-shadow-candidate[data-candidate-id="v5_5_frontier"] > summary').click();
  await expect(center).toContainText("Candidate spec hash");
  await expect(center).toContainText("证据不可用");
  await expect(center).toContainText("independent_sessions");
  await expect(center.locator(".strategy-shadow-topn-wrap tbody tr")).toHaveCount(3);
  await expect(center.locator(".strategy-shadow-topn-wrap tbody tr").nth(0)).toContainText("--");

  const layout = await center.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const table = element.querySelector(".strategy-shadow-topn-wrap");
    return {
      left: box.left,
      right: box.right,
      viewport: window.innerWidth,
      tableScrollable: table.scrollWidth > table.clientWidth,
    };
  });
  expect(layout.left).toBeGreaterThanOrEqual(0);
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.tableScrollable).toBeTruthy();
});


function evidencePayload() {
  const topN = (top_n) => ({
    top_n, horizon_trading_days: 5, status: "insufficient_data",
    sample_size: top_n * 3, independent_session_count: 3,
    gross_return: 0.01, net_return: null, cost_drag: null, turnover_rate: null,
    insufficient_reasons: ["minimum_session_count"],
  });
  return {
    strategy_fingerprint: "b".repeat(64), status: "insufficient_data",
    research_boundary: {
      status: "shadow_only", baseline_kind: "offline_evaluation_baseline",
      baseline_production_score_rule_version: "full-market-score-v4",
      baseline_production_score_spec_hash: "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a",
      execution_contract_compatibility: "not_available",
      production_ranking_mutated: false, statement: "影子研究，不改变生产排名",
    },
    promotion: {
      observed_independent_session_count: 3, required_independent_session_count: 20,
      multiple_testing_ready: false, pbo_ready: false,
      pbo_status: "not_computed", deflated_sharpe_status: "not_computed",
      blockers: ["独立交易日样本不足"], conclusion: "保持 Shadow。",
    },
    coverage: [], top_n: [], rank_evidence: [], execution: {
      execution_id: null, production_score_rule_version: null,
      production_score_spec_hash: null, evidence_digest_verified: false,
    },
    baseline_generated_at: null, baseline_report_digest: null,
    data_sources: [], freshness_notes: [], limitations: [],
    shadow_candidates: [{
      candidate_id: "v5_5_frontier", status: "insufficient_data",
      evidence_status: "insufficient_data", spec_hash: "a".repeat(64),
      point_in_time_integrity_verified: true, independent_session_count: 3,
      coverage: {
        status: "unavailable", independent_session_count: 3, scored_run_count: 3,
        scored_item_count: 16470, item_coverage_ratio: null,
        reasons: ["评分行覆盖率未持久化，未用 0 代替"],
      },
      top_n: [20, 50, 100].map(topN),
      rank_delta_vs_production: {
        status: "unavailable", compared_run_count: null, compared_item_count: null,
        candidate_ranking_count: null, production_ranking_count: null,
        common_symbol_count: null, missing_candidate_count: null,
        missing_production_count: null,
        mean_rank_delta: null, median_rank_delta: null, mean_absolute_rank_delta: null,
        maximum_absolute_rank_delta: null, top20_overlap_ratio: null,
        top50_overlap_ratio: null, top100_overlap_ratio: null,
        reasons: ["v4-v5.x 逐股排名差尚未持久化"],
      },
      constraints: {
        status: "available", passed: true, hysteresis_turnover_rate: 0.24,
        failed_constraints: [], reasons: [],
      },
      exposure: {
        status: "available", passed: false, record_count: 3,
        maximum_absolute_share_difference: 0.4071, threshold: 0.2, reasons: [],
      },
      promotion_gate: {
        status: "available", gate_version: "shadow-promotion-gates-v2",
        decision: "remain-shadow", passed: false,
        failed_criteria: ["independent_sessions", "board_industry_liquidity_exposure"], reasons: [],
      },
    }],
  };
}
