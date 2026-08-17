import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";

test("individual D+2/D+3/D+4 probability is gated, auditable, and mobile safe", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/stock/upside-probability") {
        return { payload: probabilityReport(url.searchParams.get("symbol") || "600519.SH") };
      }
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#actionAdvice")).toHaveText("观察 · 证据强度 60/100");
  await expect(page.locator("#actionAdvice").locator("xpath=..")).toContainText("互动研究观点（Shadow）");
  await expect(page.locator("#actionAdvice").locator("xpath=..")).toContainText("不写建议历史、不影响正式建议");
  const shell = page.locator("#individualProbabilityResearch");
  await expect(shell).toHaveAttribute("data-status", "calibrated_shadow");
  await expect(page.locator("#individualProbabilitySemantics")).toContainText("趋势分是序数状态分，不是上涨概率");
  await expect(page.locator("#individualProbabilitySemantics")).toContainText("不参与生产评分、排名或操作建议");
  await expect(page.locator("#individualProbabilityTarget")).toContainText("D+1 官方日K开盘价代理（不保证成交）");
  await expect(page.locator("#individualProbabilityTarget")).toContainText("持有 1 / 2 / 3 个交易日");
  await expect(page.locator("#individualProbabilityTarget")).toContainText("扣除声明成本后的日K代理净收益 > 0");

  const cards = page.locator("#individualProbabilityCards .individual-probability-card");
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(0)).toContainText("D+2");
  await expect(cards.nth(0)).toContainText("61.2%");
  await expect(cards.nth(0)).toContainText("56.0%–66.0%");
  await expect(cards.nth(0)).toContainText("284 / 180000");
  await expect(cards.nth(1)).toContainText("D+3");
  await expect(cards.nth(1)).toContainText("样本不足");
  await expect(cards.nth(1)).toContainText("—");
  await expect(cards.nth(1)).not.toContainText(/0\.0%|50\.0%/);
  await expect(cards.nth(2)).toContainText("尚未生成");
  await expect(page.locator("#individualProbabilityEvidence")).toContainText("b".repeat(64));
  await expect(page.locator("#individualProbabilityEvidence")).toContainText("288 / 288");
  await expect(page.locator("#individualProbabilityEvidence")).toContainText("288 日（正式）");
  await expect(page.locator("#individualProbabilityLimitations")).toContainText("不改变生产评分、排名或操作建议");

  const diagnostic = cards.nth(0).locator(".individual-probability-diagnostics");
  await diagnostic.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(diagnostic).toHaveAttribute("open", "");
  await expect(diagnostic).toContainText("Brier skill");
  await expect(diagnostic).toContainText("AUC");
  await expect(diagnostic).toContainText("ECE");

  const layout = await shell.evaluate((element) => ({
    right: element.getBoundingClientRect().right,
    viewport: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
    summaryHeights: Array.from(element.querySelectorAll("details summary"), (node) => node.getBoundingClientRect().height),
  }));
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
  expect(Math.min(...layout.summaryHeights)).toBeGreaterThanOrEqual(44);
});

test("individual probability endpoint failure degrades only its panel", async ({ page }) => {
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/stock/upside-probability") {
        return { payload: { detail: "概率证据暂时离线" }, status: 503 };
      }
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      return null;
    },
  });

  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#sourceLine")).toContainText("E2E行情");
  const shell = page.locator("#individualProbabilityResearch");
  await expect(shell).toHaveAttribute("data-status", "unavailable");
  await expect(page.locator("#individualProbabilityRetry")).toBeVisible();
  await expect(page.locator("#individualProbabilityCards")).toContainText("概率证据暂时离线");
  await expect(page.locator("#individualProbabilityCards")).not.toContainText(/0\.0%|50\.0%/);
  await expect(page.locator("#dataStatus")).not.toContainText("概率证据");
});

function probabilityReport(symbol) {
  const code = String(symbol).slice(0, 6);
  const reportSymbol = String(symbol).includes(".")
    ? String(symbol).toUpperCase()
    : `${code}.${code.startsWith("6") ? "SH" : "SZ"}`;
  const counts = {
    observation_count: 180000, eligible_observation_count: 170000,
    independent_session_count: 284, out_of_sample_observation_count: 60000,
    out_of_sample_session_count: 120, evaluated_fold_count: 2,
  };
  const metrics = {
    brier_score: 0.196, reference_brier_score: 0.223, brier_skill_score: 0.12107623318385652,
    ece: 0.034, auc: 0.681, actual_positive_rate: 0.514,
    actual_positive_rate_ci_95: { lower: 0.49, upper: 0.54, level: 0.95 },
    bin_monotonic: true, highest_bin_above_base_rate: true,
    selection_gate_version: "market-scan-probability-selection-gates-v1",
    calibration_bin_count: 5, minimum_calibration_bin_session_count: 20,
    all_folds_positive_brier_skill: true,
  };
  const horizon = (displayDay, status) => ({
    display_day: displayDay, holding_sessions: displayDay - 1, status,
    probability: status === "calibrated_shadow" ? 0.612 : null,
    confidence_interval: status === "calibrated_shadow" ? { lower: 0.56, upper: 0.66, level: 0.95 } : null,
    base_rate: 0.514, counts: { ...counts }, calibration_metrics: { ...metrics },
    training_cutoff: "2026-07-13", model_version: "shadow-up-probability-logit-l2-v2-convergence-required",
    feature_version: "historical-replay-common-ohlcv-v1", evidence_digest: "d".repeat(64),
    gate_reasons: status === "calibrated_shadow" ? [] : [`D+${displayDay} 证据门禁未通过`],
  });
  return {
    schema_version: "individual-upside-probability-v1", symbol: reportSymbol, signal_date: "2026-07-14",
    generated_at: "2026-07-14T18:00:00+08:00", status: "calibrated_shadow",
    target_contract: {
      version: "individual-upside-net-return-label-v1", signal_cutoff: "completed_session_D_close",
      entry: "D_plus_1_official_daily_open_proxy_no_shift",
      exits: { "D+2": "D_plus_2_close_holding_session_1", "D+3": "D_plus_3_close_holding_session_2", "D+4": "D_plus_4_close_holding_session_3" },
      target: "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy", cost_profile: "base-a0441d84df44", execution_notional: 100000,
      feature_version: "historical-replay-common-ohlcv-v1", point_in_time_required: true,
    },
    horizons: [horizon(2, "calibrated_shadow"), horizon(3, "insufficient_data"), horizon(4, "not_generated")],
    evidence: {
      assessment_digest: "b".repeat(64), history_manifest_digest: "c".repeat(64),
      history_database_sha256: "a".repeat(64), official_pit_session_count: 288,
      required_official_pit_session_count: 288, historical_replay_session_count: 288,
      historical_replay_official: true, selection_qualified: true,
    },
    limitations: ["非官方历史 OOS 诊断不等于当前个股概率"], production_effect: "none",
  };
}
