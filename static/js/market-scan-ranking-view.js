import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export function productionRankCell(view) {
  if (view.productionScoreRuleVersion !== "full-market-score-v6") return escapeHtml(view.rank);
  return `<div class="market-scan-production-rank"><strong>${escapeHtml(view.rank)}</strong><small>v5 ${escapeHtml(view.baseProductionRank ?? "--")} → v6</small></div>`;
}

export function productionScoreCell(view) {
  const score = `<strong class="market-scan-score">${escapeHtml(marketScanScoreText(view.score))}</strong>`;
  if (view.productionScoreRuleVersion !== "full-market-score-v6") return score;
  const adjustment = Number(view.probabilityRankingAdjustment);
  const adjustmentText = Number.isFinite(adjustment)
    ? `${adjustment >= 0 ? "+" : ""}${formatNumber(adjustment, 2)}` : "--";
  return `<div class="market-scan-production-score">${score}<small>v5 ${escapeHtml(marketScanScoreText(view.baseProductionScore))} · 概率调整 ${escapeHtml(adjustmentText)}</small></div>`;
}

export function productionScoreTitle(view) {
  return view.productionScoreRuleVersion === "full-market-score-v6"
    ? "生产 v6：不可变 v5 原始分叠加有界 H5 联合执行概率调整；原 v5 排名保留"
    : "生产 v5 的序数趋势状态分，不代表上涨概率";
}

export function marketScanScoreText(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(Math.round(number)) : "--";
}
