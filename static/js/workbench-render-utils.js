import { escapeHtml } from "./dom.js";

export function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function asArray(value) {
  return Array.isArray(value) ? value : [];
}

export function renderList(items, renderer, { limit, empty = "" } = {}) {
  const rows = limitedArray(items, limit);
  return rows.length ? rows.map(renderer).join("") : empty;
}

export function renderEscapedItems(items, tagName, { limit, className = "" } = {}) {
  const classAttr = className ? ` class="${escapeHtml(className)}"` : "";
  return renderList(items, (item) => `<${tagName}${classAttr}>${escapeHtml(item)}</${tagName}>`, { limit });
}

export function escapedJoin(items, separator, { limit } = {}) {
  return limitedArray(items, limit).map(escapeHtml).join(separator);
}

export function renderOptionalTag(tagName, value, { prefix = "", className = "" } = {}) {
  if (value === null || value === undefined || value === "") return "";
  const classAttr = className ? ` class="${escapeHtml(className)}"` : "";
  return `<${tagName}${classAttr}>${escapeHtml(prefix)}${escapeHtml(value)}</${tagName}>`;
}

export function levelToneClass(level) {
  if (level === "风险") return "risk";
  if (level === "积极") return "good";
  return "";
}

function limitedArray(items, limit) {
  const rows = asArray(items);
  return limit === undefined ? rows : rows.slice(0, limit);
}
