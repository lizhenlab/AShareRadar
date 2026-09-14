import { escapeHtml } from "./dom.js";
import { financialPeriodKey, sourceValue } from "./fuyao-contracts.js";
import { renderFuyaoValueResearch } from "./fuyao-value-view.js";

const e = escapeHtml;
const TABLE_NAMES = { income: "利润表", balance: "资产负债表", cashflow: "现金流量表", indicators: "财务指标" };

export function renderFuyaoStock(target, observation, options = {}) {
  if (!target) return;
  const bundle = observation?.financials;
  const periods = Array.isArray(bundle?.periods) ? bundle.periods : [];
  const selected = periods.find((period) => financialPeriodKey(period) === options.period) || periods[0];
  target.innerHTML = `<div class="fuyao-heading"><strong>财报与估值记录</strong>
    <button type="button" class="mini-button" data-fuyao-refresh="financials">刷新当前股财报</button></div>
    ${selected ? financialReport(bundle, selected) : emptyFinancial(options.message)}
    ${renderFuyaoValueResearch(observation?.value_research, selected ? financialPeriodKey(selected) : "")}
    ${valuationScore(observation?.valuation_score)}${valuationReport(observation)}`;
}

function emptyFinancial(message) {
  return `<p class="fuyao-note">${e(message || "当前股票尚无扶摇财报缓存。点击刷新可在后台准备数据，原有分析仍可使用。")}</p>`;
}

function financialReport(bundle, period) {
  const options = bundle.periods.map((item) => `<option value="${e(financialPeriodKey(item))}"${item === period ? " selected" : ""}>
    ${e(periodLabel(item))}</option>`).join("");
  const dates = (period.supplier_report_dates || []).join("、") || "未返回";
  return `<label class="fuyao-period">报告期 <select id="fuyaoPeriod" aria-label="财报报告期">${options}</select></label>
    <p class="fuyao-note">${e(bundle.symbol)} · ${e(period.source || bundle.source)} · 获取时间 ${e(period.fetched_at || bundle.fetched_at)}</p>
    <p class="fuyao-note">币种 ${e(period.currency || "未返回")} · ${period.alignment === "complete" ? "三张报表已按报告期对齐" : "本期报表未齐"} · 财务评分暂不生成</p>
    <div id="fuyaoFinancialFacts" class="fuyao-table-scroll"><table><thead><tr><th>项目</th><th>提供者原值</th><th>单位</th><th>来源表</th></tr></thead>
    <tbody>${period.metrics.map(financialMetric).join("")}</tbody></table></div>
    <details class="fuyao-details"><summary>报告日期与数据说明</summary><p>供应商报告日期：${e(dates)}</p>
    ${(bundle.warnings || []).map((text) => `<p>${e(text)}</p>`).join("")}</details>`;
}

function financialMetric(fact) {
  const raw = fact.raw_value === null ? "未返回" : fact.raw_value;
  return `<tr><th>${e(fact.label)}</th><td>${e(raw)}</td><td>${e(fact.unit || "待核实")}</td>
    <td>${e(TABLE_NAMES[fact.source_kind] || fact.source_kind)}</td></tr>`;
}

function periodLabel(period) {
  return `${period.period_end} ${period.period_type === "annual" ? "年报" : "季度报告（单季/累计待核实）"}`;
}

function valuationScore(assessment) {
  if (!assessment) return "";
  const available = assessment.score_available === true && typeof assessment.score === "number" && Number.isFinite(assessment.score)
    && assessment.score >= 0 && assessment.score <= 100;
  const title = available ? `估值辅助分 ${assessment.score}/100` : "估值辅助分暂不可用";
  const components = Array.isArray(assessment.components) ? assessment.components : [];
  return `<section class="fuyao-details" data-fuyao-valuation-score aria-label="扶摇估值辅助评分">
    <div class="fuyao-heading"><strong>${e(title)}</strong><span>${e(assessment.source)}</span></div>
    <p class="fuyao-note">${e(assessment.symbol)} · 获取 ${e(assessment.fetched_at || "尚无缓存")}
      · 计算 ${e(assessment.evaluated_at || "未返回")}</p>
    <p>按 PE TTM 与 PB MRQ 的固定规则计算，不代表财务健康或上涨概率。用于个股研究，不参与全市场排名。</p>
    ${!available ? `<p class="fuyao-note">${e(assessment.unavailable_reason || "当前记录尚不满足计算条件。")}</p>` : ""}
    ${components.length ? `<div class="fuyao-table-scroll"><table><thead><tr><th>计算项</th><th>输入值</th><th>分值贡献</th><th>依据</th></tr></thead>
      <tbody><tr><th>基础分</th><td>—</td><td>${e(sourceValue(assessment.base_score))}</td><td>固定规则起点</td></tr>
      ${components.map(valuationContribution).join("")}</tbody></table></div>` : ""}
    ${valuationNotes(assessment.evidence)}${valuationNotes(assessment.missing_data, "缺少：")}${valuationNotes(assessment.warnings)}
    <details><summary>计算来源</summary><p>规则版本：${e(assessment.rule_version || "未返回")}</p>
      <p>观察记录：${e(assessment.observation_id ?? "未返回")}</p>
      <p class="fuyao-version">观察摘要：${e(assessment.observation_digest || "未返回")}</p></details></section>`;
}

function valuationContribution(item) {
  const points = typeof item.points === "number" && Number.isFinite(item.points)
    ? `${item.points > 0 ? "+" : ""}${item.points}` : "未计入";
  return `<tr><th>${e(item.label)}</th><td>${e(sourceValue(item.value))}</td>
    <td>${e(points)}</td><td>${e(item.reason)}</td></tr>`;
}

function valuationNotes(values, prefix = "") {
  return (Array.isArray(values) ? values : []).map(value => `<p class="fuyao-note">${e(prefix)}${e(value)}</p>`).join("");
}

function valuationReport(observation) {
  const record = observation?.valuation;
  if (!record?.payload?.values) return "";
  const names = { pe_ttm: "PE TTM", pe_mrq: "PE MRQ", pb_mrq: "PB MRQ", ps_ttm: "PS TTM", pcf_ttm: "PCF TTM" };
  const fields = observation.valuation_history?.fields || {};
  return `<details class="fuyao-details"><summary>同口径估值快照</summary><p>${e(record.source)} · 获取 ${e(record.fetched_at)}</p>
    <div class="fuyao-table-scroll"><table><thead><tr><th>指标</th><th>原值</th><th>本地观察日</th><th>分位</th></tr></thead><tbody>
    ${Object.entries(names).map(([key, label]) => `<tr><th>${label}</th><td>${e(sourceValue(record.payload.values[key]))}</td>
    <td>${e(fields[key]?.sample_days ?? 0)}</td><td>${e(sourceValue(fields[key]?.percentile, "%"))}</td></tr>`).join("")}</tbody></table></div>
    <p class="fuyao-note">${e(observation.valuation_history?.basis || "最新快照，不代表完整历史估值；批次时间不代表逐指标更新时间。")}</p></details>`;
}
