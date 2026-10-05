import { escapeHtml } from "./dom.js";

export function validateConditionImpacts(value, spec, funnel, population, matchedCount, matchedSymbols, runId, exclusionReasons) {
  requireImpact(Array.isArray(value) && value.length === funnel.length, "条件影响列表不完整");
  const seen = new Set(matchedSymbols);
  let additionalTotal = 0;
  value.forEach((impact, index) => {
    requireImpact(impact && impact.condition_code === funnel[index].condition_code && impact.label === funnel[index].label, "条件影响与筛选漏斗不一致");
    for (const key of ["additional_count", "missing_additional_count", "matched_without_condition"]) requireImpact(Number.isInteger(impact[key]) && impact[key] >= 0, "条件影响计数无效");
    requireImpact(impact.matched_without_condition === matchedCount + impact.additional_count && impact.matched_without_condition <= population, "移除后命中数量不守恒");
    requireImpact(impact.missing_additional_count <= impact.additional_count, "缺失新增数量越界");
    requireImpact(Array.isArray(impact.examples) && impact.examples.length === Math.min(3, impact.additional_count), "条件影响示例数量不完整");
    const exclusion = exclusionReasons.find((reason) => reason.code === impact.condition_code);
    requireImpact(impact.additional_count - impact.missing_additional_count <= (exclusion?.count || 0) - (exclusion?.missing_count || 0) && impact.missing_additional_count <= (exclusion?.missing_count || 0), "条件影响超过原始淘汰统计");
    impact.examples.forEach((example) => validateImpactExample(example, impact, spec, runId, seen));
    requireImpact(impact.examples.filter((example) => example.missing).length <= impact.missing_additional_count, "示例缺失数量超过统计");
    requireImpact(impact.examples.filter((example) => !example.missing).length <= impact.additional_count - impact.missing_additional_count, "示例非缺失数量超过统计");
    additionalTotal += impact.additional_count;
  });
  requireImpact(additionalTotal <= population - matchedCount, "新增数量包含重复淘汰候选");
}

function validateImpactExample(example, impact, spec, runId, seen) {
  requireImpact(example && example.run_id === runId && /^[0-9]{6}\.(SH|SZ|BJ)$/.test(example.symbol), "示例股票身份无效");
  requireImpact(example.symbol === `${example.code}.${example.market}` && typeof example.name === "string", "示例股票信息无效");
  requireImpact(["success", "missing", "skipped", "pending"].includes(example.status), "示例状态无效");
  requireImpact(!seen.has(example.symbol), "示例包含重复或已经命中的股票");
  seen.add(example.symbol);
  const observed = example.observed_value;
  const scalar = observed === null || typeof observed === "string" || typeof observed === "boolean" || (typeof observed === "number" && Number.isFinite(observed));
  requireImpact(scalar && typeof example.missing === "boolean" && example.missing === (observed === null), "示例原始值或缺失标记无效");
  const code = impact.condition_code;
  if (["status", "market", "industry", "keyword"].includes(code)) requireImpact(observed === null || typeof observed === "string", "文本条件原值必须保留文本或缺失");
  const originals = { status: example.status, market: example.market, keyword: example.name };
  if (Object.hasOwn(originals, code)) requireImpact(observed === originals[code], "条件原值与示例冻结字段不一致");
  requireObservedFailure(example, code, spec);
  if (impact.condition_code.startsWith("range.")) requireImpact(observed === null || typeof observed === "number", "区间示例必须保留数值或缺失");
  if (["is_st", "is_new"].includes(impact.condition_code)) requireImpact(observed === null || typeof observed === "boolean", "标记示例必须保留布尔值或缺失");
}

function requireObservedFailure(example, code, spec) {
  const value = example.observed_value;
  if (value === null) {
    requireImpact(code === "industry" || code.startsWith("range."), "固有字段不能伪造缺失");
    return;
  }
  let passes = true;
  if (code.startsWith("range.")) {
    const bounds = spec.ranges[code.slice(6)];
    passes = (bounds.min == null || value >= bounds.min) && (bounds.max == null || value <= bounds.max);
  } else if (code === "market") passes = spec.markets.includes(value);
  else if (code === "industry") passes = spec.industries.some((industry) => asciiLower(value).includes(asciiLower(industry)));
  else if (code === "keyword") passes = [example.symbol, example.code, value].some((text) => asciiLower(text).includes(asciiLower(spec.keyword)));
  else passes = spec[code] === value;
  requireImpact(!passes, "新增示例其实通过了被移除的条件");
}

function asciiLower(value) { return String(value).replace(/[A-Z]/g, (letter) => letter.toLowerCase()); }

function requireImpact(condition, message) {
  if (!condition) throw new Error(`条件影响校验失败：${message}`);
}

export function conditionImpactContent(impacts) {
  if (!impacts.length) return '<p class="market-scan-screening-region-state">没有额外筛选条件可移除。</p>';
  const rows = impacts.map((impact) => `<tr data-condition-impact="${escapeHtml(impact.condition_code)}"><th scope="row">${escapeHtml(impact.label)}</th><td>${impact.matched_without_condition}</td><td>+${impact.additional_count}</td><td>${impact.missing_additional_count}</td><td><ul class="screening-impact-examples">${impact.examples.map((example) => impactExample(example, impact.condition_code)).join("") || "<li>没有仅因该条件而落选的候选。</li>"}</ul></td></tr>`).join("");
  return `<div class="market-scan-screening-table-wrap" tabindex="0"><table class="market-scan-impact-table"><caption>移除单个完整条件，保留其余全部条件</caption><thead><tr><th>移除的条件</th><th>移除后总命中</th><th>新增命中</th><th>新增中证据缺失</th><th>新增示例（最多 3 只）</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function impactExample(example, code) {
  const value = example.observed_value === null ? "不可用" : typeof example.observed_value === "boolean" ? (example.observed_value ? "是" : "否") : String(example.observed_value);
  const unit = ({ "range.amount": " 元", "range.change_pct": " %", "range.turnover_rate": " %" })[code] || "";
  const status = ({ success: "有效排名", missing: "数据缺失", skipped: "已跳过", pending: "待处理" })[example.status];
  return `<li><strong>${escapeHtml(example.name)}</strong> ${escapeHtml(example.symbol)} · ${escapeHtml(status)}<br>原值：${escapeHtml(value)}${example.missing ? "" : unit}${example.missing ? "（证据缺失）" : ""}</li>`;
}
