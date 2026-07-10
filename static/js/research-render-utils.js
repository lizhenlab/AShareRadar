import { escapeHtml } from "./dom.js";

export function renderInlineItems(items, tagName, limit, className = "") {
  return renderLimitedItems(items, limit, (item) => renderTag(tagName, item, className));
}

export function renderLimitedItems(items, limit, renderItem) {
  return asArray(items).slice(0, limit).map(renderItem).join("");
}

export function renderMissingData(items, options = {}) {
  const values = asArray(items);
  if (!values.length) return "";
  const tagName = options.tagName || "small";
  const prefix = options.prefix || "待补：";
  const separator = options.separator || "、";
  return renderTag(tagName, `${prefix}${values.join(separator)}`);
}

export function renderMetricPairs(items) {
  return asArray(items)
    .map((item) => {
      const [label, value] = Array.isArray(item) ? item : [item, ""];
      return `<span>${escapeHtml(label)} <b>${escapeHtml(value)}</b></span>`;
    })
    .join("");
}

export function renderTag(tagName, value, className = "") {
  const classAttr = className ? ` class="${escapeHtml(className)}"` : "";
  return `<${tagName}${classAttr}>${escapeHtml(value)}</${tagName}>`;
}

export function signedText(value) {
  const text = value ?? "";
  return `${Number(value) > 0 ? "+" : ""}${text}`;
}

export function asArray(items) {
  return Array.isArray(items) ? items : [];
}

export function asObject(item) {
  return item && typeof item === "object" && !Array.isArray(item) ? item : {};
}

export function safeText(value) {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

export function thresholdClass(value, options = {}) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  if (options.higherIsRisk) {
    if (number >= options.riskAt) return "risk";
    if (number <= options.goodAt) return "good";
    return "";
  }
  if (number >= options.goodAt) return "good";
  if (number <= options.riskAt) return "risk";
  return "";
}
