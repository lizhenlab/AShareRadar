import { changeClass, formatNumber } from "./format.js";
import { asObject, safeText, thresholdClass } from "./research-render-utils.js";

export { formatNumber };

export function fallbackText(value, fallback = "--") {
  const text = safeText(value).trim();
  return text || fallback;
}

export function joinedMetric(left, right, separator = " · ") {
  return `${fallbackText(left)}${separator}${fallbackText(right)}`;
}

export function marketRegimeRiskClass(value) {
  return thresholdClass(value, { higherIsRisk: true, riskAt: 1.15, goodAt: 0.92 });
}

export function validationStatusClass(status) {
  status = safeText(status);
  if (status.includes("风险") || status.includes("压制")) return "risk";
  if (status.includes("确认")) return "good";
  return "";
}

export function timeframeConflictClass(report) {
  report = asObject(report);
  const conflictLevel = safeText(report.conflict_level);
  const alignmentLabel = safeText(report.alignment_label);
  if (conflictLevel.includes("冲突") || alignmentLabel.includes("偏弱")) return "risk";
  if (alignmentLabel.includes("共振")) return "good";
  return "";
}

export function timeframeMaText(item) {
  if (item.above_ma === true) return `高于均线 ${formatNumber(item.ma_value)}`;
  if (item.above_ma === false) return `低于均线 ${formatNumber(item.ma_value)}`;
  return "均线关系待确认";
}

export function timeframeItemClass(item) {
  return thresholdClass(item.score, { goodAt: 62, riskAt: 45 });
}

export function riskRewardRatingClass(rating) {
  rating = safeText(rating);
  if (rating.includes("风险") || rating.includes("不足")) return "risk";
  if (rating.includes("较好")) return "good";
  return "";
}

export function scenarioClass(item) {
  const name = safeText(asObject(item).name);
  if (name.includes("防守")) return "risk";
  if (name.includes("积极")) return "good";
  return "";
}

export function firstText(items, fallback) {
  return Array.isArray(items) ? items[0] || fallback : fallback;
}

export function factorDirectionClass(item) {
  item = asObject(item);
  if (item.direction === "负向") return "risk";
  if (item.direction === "正向") return "good";
  return "";
}

export function conceptChangeClass(value) {
  const className = changeClass(value);
  if (className === "up") return "up-bg";
  if (className === "down") return "down-bg";
  return "neutral";
}

export function replayHeadline(replay) {
  return Number(replay.sample_count || 0) >= 5 ? `样本有效率 ${formatNumber(replay.success_rate, 1)}%` : "样本偏少";
}

export function formatReplayReturn(value) {
  return value === null || value === undefined ? "--" : `${formatNumber(value)}%`;
}
