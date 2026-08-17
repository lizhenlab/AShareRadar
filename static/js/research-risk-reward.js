import { $, escapeHtml } from "./dom.js";
import {
  fallbackText,
  formatNumber,
  joinedMetric,
  marketRegimeRiskClass,
  riskRewardRatingClass,
  scenarioClass,
  timeframeConflictClass,
  timeframeItemClass,
  timeframeMaText,
} from "./research-formatters.js";
import {
  asArray,
  asObject,
  renderInlineItems,
  renderMetricPairs,
  signedText,
} from "./research-render-utils.js";

export function renderMarketRegime(report) {
  const el = $("marketRegime");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="regime-head">
      <div>
        <span>市场环境</span>
        <strong>${escapeHtml(report.market_label)} · ${escapeHtml(report.stock_state)}</strong>
      </div>
      <i class="${marketRegimeRiskClass(report.risk_multiplier)}">风险倍率 ${formatNumber(report.risk_multiplier, 2)}</i>
    </div>
    <div class="regime-tags">${renderMarketRegimeTags(report)}</div>
    <div class="regime-grid">
      ${renderMarketRegimeColumn("操作提醒", report.suggestions, 3)}
      ${renderMarketRegimeColumn("判断依据", report.evidence, 4)}
    </div>
  `;
}

function renderMarketRegimeTags(report) {
  return renderInlineItems([
    report.industry_label,
    `${report.breadth_label || "市场宽度待确认"} · ${report.breadth_score ?? "--"}分`,
    `证据充分度修正 ${signedText(report.confidence_adjustment)}`,
  ], "span");
}

function renderMarketRegimeColumn(title, items, limit) {
  return `
    <div>
      <strong>${escapeHtml(title)}</strong>
      ${renderInlineItems(items, "span", limit)}
    </div>`;
}

export function renderTimeframeAlignment(report) {
  const el = $("timeframeAlignment");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = timeframeAlignmentView(report);
  el.innerHTML = `
    <div class="timeframe-head">
      <div>
        <span>多周期一致性</span>
        <strong>${escapeHtml(view.label)} · ${escapeHtml(view.score)}分</strong>
      </div>
      <i class="${view.conflictClass}">${escapeHtml(view.conflictLevel)}</i>
    </div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="timeframe-grid">
      ${view.timeframes.map(renderTimeframeItem).join("")}
    </div>
    ${renderTimeframeSuggestions(view.suggestions)}
  `;
}

function timeframeAlignmentView(report) {
  report = asObject(report);
  return {
    label: report.alignment_label,
    score: report.alignment_score,
    conflictLevel: report.conflict_level,
    conflictClass: timeframeConflictClass(report),
    summary: report.summary,
    timeframes: asArray(report.timeframes),
    suggestions: asArray(report.suggestions),
  };
}

function renderTimeframeItem(item) {
  item = asObject(item);
  return `
    <div class="timeframe-item ${timeframeItemClass(item)}">
      <div>
        <strong>${escapeHtml(fallbackText(item.name))}</strong>
        <span>${escapeHtml(joinedMetric(item.score, item.label))}</span>
      </div>
      <small>${escapeHtml(item.window_days)}日 · 涨跌 ${formatNumber(item.return_pct)}% · 回撤 ${formatNumber(item.max_drawdown_pct)}%</small>
      <small>${escapeHtml(timeframeMaText(item))}</small>
    </div>
  `;
}

function renderTimeframeSuggestions(items) {
  return `<div class="timeframe-suggestions">${renderInlineItems(items, "span", 3)}</div>`;
}

export function renderRiskReward(report) {
  const el = $("riskReward");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const view = riskRewardView(report);
  el.innerHTML = `
    <div class="risk-reward-head">
      <div>
        <span>风险收益与情景</span>
        <strong>${escapeHtml(view.rating)}</strong>
      </div>
      <i class="${view.ratingClass}">收益风险比 ${view.ratioAvailable ? formatNumber(view.rewardRiskRatio, 2) : "—"}</i>
    </div>
    <div class="risk-reward-metrics">${renderRiskRewardMetrics(view)}</div>
    <p>${escapeHtml(view.summary)}</p>
    <div class="scenario-grid">
      ${view.scenarios.map(renderScenarioPlan).join("")}
    </div>
    ${renderInlineItems(view.notes, "small", 1)}
  `;
}

function riskRewardView(report) {
  report = asObject(report);
  const upsideAvailable = report.upside_available === true && report.upside_target_basis !== "unavailable";
  const downsideAvailable = report.downside_available === true && report.downside_stop_basis !== "unavailable";
  const atrAvailable = [report.upside_target_basis, report.downside_stop_basis]
    .some((basis) => String(basis || "").includes("atr"));
  return {
    rating: report.rating,
    ratingClass: riskRewardRatingClass(report.rating),
    rewardRiskRatio: report.reward_risk_ratio,
    ratioAvailable: report.ratio_available === true && upsideAvailable && downsideAvailable,
    currentPrice: report.current_price,
    upsideTarget: report.upside_target,
    upsidePct: report.upside_pct,
    downsideStop: report.downside_stop,
    downsidePct: report.downside_pct,
    atrPct: report.atr_pct,
    volatilityPct: report.volatility_pct,
    upsideAvailable,
    downsideAvailable,
    atrAvailable,
    upsideBasis: report.upside_target_basis,
    downsideBasis: report.downside_stop_basis,
    summary: report.summary,
    scenarios: asArray(report.scenarios),
    notes: [report.availability_reason, ...asArray(report.notes)].filter(Boolean),
  };
}

function renderRiskRewardMetrics(view) {
  return renderMetricPairs([
    ["现价", positiveMetric(view.currentPrice)],
    ["上方目标", levelMetric(view.upsideAvailable, view.upsideTarget, view.upsidePct, view.upsideBasis)],
    ["下方防守", levelMetric(view.downsideAvailable, view.downsideStop, view.downsidePct, view.downsideBasis)],
    ["ATR / 波动", volatilityMetric(view)],
  ]);
}

function positiveMetric(value, digits = 2) {
  return Number.isFinite(Number(value)) && Number(value) > 0 ? formatNumber(value, digits) : "—";
}

function levelMetric(available, level, percent, basis) {
  if (!available) return "— / 证据不可用";
  return `${formatNumber(level)} / ${formatNumber(percent)}% · ${levelBasisLabel(basis)}`;
}

function volatilityMetric(view) {
  const atrValue = view.atrAvailable ? positiveMetric(view.atrPct) : "—";
  const volatilityValue = positiveMetric(view.volatilityPct);
  const atr = atrValue === "—" ? atrValue : `${atrValue}%`;
  const volatility = volatilityValue === "—" ? volatilityValue : `${volatilityValue}%`;
  return atr === "—" && volatility === "—" ? "— / 证据不可用" : `${atr} / ${volatility}`;
}

function levelBasisLabel(value) {
  return {
    resistance: "压力位证据",
    atr: "ATR 证据",
    resistance_and_atr: "压力位 + ATR 证据",
    structure: "结构位证据",
    structure_and_atr: "结构位 + ATR 证据",
  }[value] || "证据不可用";
}

function renderScenarioPlan(item) {
  item = asObject(item);
  const ruleWeight = item.rule_weight ?? item.probability ?? "--";
  return `
    <div class="scenario-item ${scenarioClass(item)}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>规则情景权重 ${escapeHtml(ruleWeight)}/100</span>
      </div>
      <small>触发：${escapeHtml(item.trigger)}</small>
      <small>预期：${escapeHtml(item.expected_move)}</small>
      <small>应对：${escapeHtml(item.response)}</small>
      <em>失效：${escapeHtml(item.invalidation)}</em>
    </div>
  `;
}
