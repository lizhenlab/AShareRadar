import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { marketScanProbabilitySnapshot } from "./market-scan-probability-view.js";

const SCORE_TERM_LABELS = Object.freeze({
  trend_delta: "趋势项",
  change: "涨跌幅项",
  volume_ratio: "量比项",
  amount: "成交额项",
  turnover_rate: "换手率项",
  quality: "数据质量项",
  ma_alignment: "均线结构",
  range_position_20d: "20 日区间位置",
  return_20d_pct: "20 日收益",
  return_5d_pct: "5 日收益",
});

const DEGRADATION_LABELS = Object.freeze({
  quote_fallback: "行情使用兜底源",
  kline_fallback: "日 K 使用兜底源",
  industry_missing: "行业元数据缺失",
  list_date_missing: "上市日期缺失",
  metadata_incomplete: "股票元数据不完整",
});

export function marketScanSnapshotTargetId(item) {
  const runId = positiveInteger(item?.run_id) || "unknown";
  const symbol = String(item?.symbol || "unknown").replace(/[^A-Za-z0-9_-]/g, "-");
  return `market-scan-snapshot-${runId}-${symbol}`;
}

export function toggleMarketScanSnapshot(root, button) {
  const targetId = String(button?.dataset?.marketScanSnapshotTarget || "");
  const target = targetId ? root.getElementById(targetId) : null;
  if (!target) return false;
  const expanded = Boolean(target.hidden);
  target.hidden = !expanded;
  button.setAttribute?.("aria-expanded", expanded ? "true" : "false");
  if (typeof button?.setAttribute !== "function") button["aria-expanded"] = expanded ? "true" : "false";
  return expanded;
}

export function marketScanSnapshotRow(item, probabilityResearch) {
  const id = marketScanSnapshotTargetId(item);
  return `<tr class="market-scan-snapshot-row" id="${escapeHtml(id)}" hidden>
    <td colspan="10">${marketScanSnapshotContent(item, probabilityResearch)}</td>
  </tr>`;
}

export function marketScanResearchDimensionCell(value) {
  const item = objectValue(value);
  const details = objectValue(item.score_details);
  const components = objectValue(details.components);
  const dimensions = objectValue(components.score_dimensions);
  const scores = objectValue(dimensions.scores);
  return `<div class="market-scan-dimensions" title="独立序数研究维度；风险越低越好，置信度和可交易性越高越好"><span>信 ${escapeHtml(numberText(scores.confidence))}</span><span class="risk">险 ${escapeHtml(numberText(scores.risk))}</span><span>易 ${escapeHtml(numberText(scores.tradability))}</span></div>`;
}

export function marketScanSnapshotContent(value, probabilityResearch) {
  const item = objectValue(value);
  const details = objectValue(item.score_details);
  const components = objectValue(details.components);
  const leader = objectValue(components.leader_score);
  const finalScore = objectValue(components.final_score);
  const refinement = objectValue(components.rank_refinement);
  const dimensions = objectValue(components.score_dimensions);
  const ranking = objectValue(details.ranking);
  const ruleDeltas = objectValue(leader.rule_deltas);
  const weightedTerms = objectValue(refinement.weighted_terms);
  const hasDetails = Object.keys(details).length > 0;
  return `<section class="market-scan-snapshot" aria-label="${escapeHtml(item.symbol || "股票")} 的冻结扫描快照">
    <div class="market-scan-snapshot-heading">
      <div><strong>冻结扫描快照</strong><span>批次 #${escapeHtml(item.run_id ?? "--")} · 只读持久化证据</span></div>
      <p>以下字段来自扫描完成时保存的结果，不请求当前行情，也不会重新计算评分。</p>
    </div>
    <div class="market-scan-snapshot-grid">
      ${snapshotMetric("趋势强度（生产）", item.score)}
      ${snapshotMetric("原始排名分", item.raw_score, 6)}
      ${snapshotMetric("基础趋势分", item.trend_score)}
      ${snapshotMetric("龙头分", item.leader_score)}
      ${snapshotMetric("质量扣分", finalScore.quality_penalty, 4)}
      ${snapshotMetric("精排扣分", finalScore.rank_discount, 6)}
    </div>
    ${hasDetails ? scoreBreakdown(leader, finalScore, refinement, ruleDeltas, weightedTerms) : missingScoreDetails()}
    ${scoreDimensions(dimensions)}
    ${marketScanProbabilitySnapshot(item, probabilityResearch)}
    <div class="market-scan-snapshot-columns">
      ${rankingEvidence(ranking, item)}
      ${provenanceEvidence(item)}
    </div>
    <p class="market-scan-snapshot-rule"><strong>规则版本：</strong>${escapeHtml(details.run_rule_version || "旧批次未保存")}${details.score_spec_hash ? ` · 规则哈希 ${escapeHtml(details.score_spec_hash)}` : ""}</p>
  </section>`;
}

function scoreDimensions(dimensions) {
  const scores = objectValue(dimensions.scores);
  if (!Object.keys(scores).length) return "";
  const utility = objectValue(scores.decision_utility);
  const evidence = objectValue(dimensions.point_in_time_evidence);
  const volumeContext = objectValue(dimensions.volume_context);
  const evidenceLabel = evidence.status === "verified-persisted-at-scan-time"
    ? "已冻结并通过摘要校验"
    : "不可验证";
  return `<section><h4>独立评分维度</h4>
    <p class="market-scan-snapshot-rule">这些是序数研究分，不代表上涨概率；风险分越高风险越大，其余分数越高越好。</p>
    <div class="market-scan-snapshot-grid">
      ${snapshotMetric("1 日 Alpha", scores.alpha_1d, 2)}
      ${snapshotMetric("5 日 Alpha", scores.alpha_5d, 2)}
      ${snapshotMetric("20 日 Alpha", scores.alpha_20d, 2)}
      ${snapshotMetric("置信度", scores.confidence, 2)}
      ${snapshotMetric("风险", scores.risk, 2)}
      ${snapshotMetric("可交易性", scores.tradability, 2)}
      ${snapshotMetric("稳健效用", utility.conservative, 2)}
      ${snapshotMetric("均衡效用", utility.balanced, 2)}
      ${snapshotMetric("进取效用", utility.aggressive, 2)}
    </div>
    ${volumeContextText(volumeContext)}
    <p class="market-scan-snapshot-rule"><strong>时点证据：</strong>${escapeHtml(evidenceLabel)}${evidence.payload_digest ? ` · ${escapeHtml(String(evidence.payload_digest).slice(0, 12))}` : ""}</p>
  </section>`;
}

function volumeContextText(context) {
  if (!Object.keys(context).length) return "";
  const label = context.price_volume_alignment === "same-completed-session"
    ? "同一完整交易日量价对齐，量能生命周期已参与研究维度"
    : context.price_volume_alignment === "intraday-time-aligned-volume-unavailable-neutralized"
      ? "盘中缺少同一时刻量能证据，量能生命周期已置零，不与当前价格方向混算"
      : "量能口径未识别";
  return `<p class="market-scan-snapshot-rule"><strong>量能口径：</strong>${escapeHtml(label)} · 日K截止 ${escapeHtml(context.volume_data_date || "--")}</p>`;
}

function scoreBreakdown(leader, finalScore, refinement, ruleDeltas, weightedTerms) {
  return `<div class="market-scan-score-breakdown">
    <section><h4>评分组成</h4><dl>
      ${definition("龙头基础分", leader.base)}
      ${definition("趋势增减分", leader.trend_delta, 4, true)}
      ${termDefinitions(ruleDeltas, true)}
      ${definition("质量扣分", finalScore.quality_penalty, 4, true, -1)}
      ${definition("扣分前基础分", finalScore.base, 4)}
      ${definition("精排扣分", finalScore.rank_discount, 6, true, -1)}
      ${definition("原始排名分", finalScore.raw, 6)}
      ${definition("趋势强度取整分", finalScore.score)}
    </dl></section>
    <section><h4>排名细化值</h4><dl>
      ${definition("精排综合值", refinement.score, 6)}
      ${termDefinitions(weightedTerms, false)}
    </dl></section>
  </div>`;
}

function rankingEvidence(ranking, item) {
  const values = objectValue(ranking.tie_break_values);
  const rules = Array.isArray(ranking.tie_break) ? ranking.tie_break : [];
  const ruleText = rules.length
    ? rules.map((entry) => `${entry?.[0] || "--"} ${entry?.[1] === "desc" ? "降序" : "升序"}`).join(" → ")
    : "旧批次未保存";
  return `<section><h4>最终排序规则</h4><dl>
    ${definition("榜单名次", item.rank)}
    ${definitionText("排序链", ruleText)}
    ${definition("原始分细化值", values.raw_score ?? item.raw_score, 6)}
    ${definitionText("最终代码细化值", values.symbol || item.symbol || "--")}
  </dl></section>`;
}

function provenanceEvidence(item) {
  const degradation = degradationText(item);
  return `<section><h4>数据与降级证据</h4><dl>
    ${definitionText("行情日期", quoteDate(item.quote_timestamp) || "--")}
    ${definitionText("报价时间", item.quote_timestamp || "--")}
    ${definitionText("日 K 截止日", item.data_date || "--")}
    ${definitionText("行情源", item.quote_source || "--")}
    ${definitionText("日 K 源", item.kline_source || "--")}
    ${definitionText("元数据源", item.metadata_source || "--")}
    ${definitionText("复权方式", adjustmentLabel(item.adjustment_mode))}
    ${definitionText("Fallback", fallbackText(item))}
    ${definitionText("降级原因", degradation)}
  </dl></section>`;
}

function missingScoreDetails() {
  return '<p class="market-scan-snapshot-missing">该旧批次没有持久化评分组成；系统不会用当前规则补算历史证据。</p>';
}

function snapshotMetric(label, value, decimals = 0) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(numberText(value, decimals))}</strong></div>`;
}

function termDefinitions(terms, signed) {
  const entries = Object.entries(terms);
  if (!entries.length) return definitionText("规则增减分", "无额外规则项");
  return entries.map(([name, value]) => definition(SCORE_TERM_LABELS[name] || name, value, 6, signed)).join("");
}

function definition(label, value, decimals = 0, signed = false, multiplier = 1) {
  const number = finiteNumber(value);
  const rendered = number === null ? "--" : numberText(number * multiplier, decimals, signed);
  return definitionText(label, rendered);
}

function definitionText(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

function degradationText(item) {
  const reasons = Array.isArray(item.degradation_reasons) ? item.degradation_reasons : [];
  const labels = reasons.map((reason) => DEGRADATION_LABELS[reason] || reason).filter(Boolean);
  if (item.metadata_degraded && !labels.some((label) => label.includes("元数据") || label.includes("缺失"))) {
    labels.push("元数据降级");
  }
  return labels.length ? [...new Set(labels)].join("；") : "无";
}

function fallbackText(item) {
  const parts = [];
  if (item.quote_fallback_used) parts.push("行情");
  if (item.kline_fallback_used) parts.push("日 K");
  return parts.length ? `${parts.join("、")}使用兜底源` : "未使用";
}

function adjustmentLabel(value) {
  if (value === "qfq") return "前复权（qfq）";
  return value || "--";
}

function quoteDate(timestamp) {
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(String(timestamp || ""));
  return match?.[1] || "";
}

function numberText(value, decimals = 0, signed = false) {
  const number = finiteNumber(value);
  if (number === null) return "--";
  const rendered = formatNumber(number, decimals);
  return signed && number > 0 ? `+${rendered}` : rendered;
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function positiveInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}
