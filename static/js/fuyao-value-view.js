import { escapeHtml } from "./dom.js";
import { financialPeriodKey, sourceValue } from "./fuyao-contracts.js";

const e = escapeHtml;
const CHECK_STATUS = { observed: "已观察", attention: "需关注", unavailable: "待补充" };

export function renderFuyaoValueResearch(report, selectedPeriodKey) {
  if (!report) return "";
  const selected = report.periods.find(period => financialPeriodKey(period) === selectedPeriodKey);
  return `<section class="fuyao-details fuyao-version" data-fuyao-value-research aria-label="价值研究摘要">
    <div class="fuyao-heading"><strong>价值研究摘要</strong><span>${e(report.symbol)}</span></div>
    <p class="fuyao-note">整理时间 ${e(report.evaluated_at)} · 事实与核实清单，不另生成评分或目标价。</p>
    ${valuationEvidence(report.valuation)}${financialEvidence(selected)}
    <details><summary>研究边界</summary>${report.limitations.map(text => `<p class="fuyao-note">${e(text)}</p>`).join("")}</details>
    </section>`;
}

function valuationEvidence(valuation) {
  return `<div data-value-valuation><strong>已准入评分输入覆盖 ${e(valuation.available_inputs)}/${e(valuation.required_inputs)}</strong>
    <p class="fuyao-note">覆盖表示本次可采用的 PE TTM / PB MRQ 项数，负值也计入；不是置信度。</p>
    <p class="fuyao-note">${e(valuation.source)} · 估值获取 ${e(valuation.fetched_at || "尚无可用时间")}</p>
    <p>盈利收益率（PE TTM 倒数）：<strong>${ratioValue(valuation.earnings_yield_pct)}</strong></p>
    <p class="fuyao-note">${e(valuation.earnings_yield_reason)} 不代表分红现金或预期回报。</p>
    <p>账面权益 / 价格（PB MRQ 倒数）：<strong>${ratioValue(valuation.book_to_price_pct)}</strong></p>
    <p class="fuyao-note">${e(valuation.book_to_price_reason)} 不代表清算价值。</p>
    ${valuation.checks.map(researchCheck).join("")}</div>`;
}

function financialEvidence(period) {
  if (!period) return `<div data-value-financial><strong>所选报告期的财务观察不足</strong>
    <p class="fuyao-note">未找到与当前选择相同的报告期。下一步核实：检查该期财报记录是否齐备，再查看对应期的盈利与现金流。</p></div>`;
  const label = period.period_type === "annual" ? "年报" : "季度报告（单季 / 累计待核实）";
  return `<div data-value-financial><strong>${e(period.period_end)} ${label} · 财务符号观察</strong>
    <p class="fuyao-note">${e(period.source)} · 财报获取 ${e(period.fetched_at || "尚无可用时间")}</p>
    <p>${e(period.summary)}</p>${period.checks.map(researchCheck).join("")}</div>`;
}

function researchCheck(check) {
  return `<details data-value-check="${e(check.key)}"><summary>${e(CHECK_STATUS[check.status])} · ${e(check.label)}</summary>
    <p>${e(check.summary)}</p><p class="fuyao-note">观察值：${e(sourceValue(check.value))} · 单位：${e(check.unit || "待核实")}</p>
    <p>下一步核实：${e(check.action)}</p></details>`;
}

function ratioValue(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? e(`${Number(value.toPrecision(4))}%`) : "暂不可用";
}
