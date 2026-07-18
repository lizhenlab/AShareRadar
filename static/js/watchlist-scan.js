import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { validateUiSymbol } from "./symbols.js";

export const WATCHLIST_SCAN_LABELS = Object.freeze({
  close_above_ma20: "收盘高于20日均线",
  close_below_ma20: "收盘低于20日均线",
  breakout_20d_high: "突破前20日高点",
  volume_surge_5d: "成交量达到5日均量1.5倍",
});

export const MAX_WATCHLIST_SCAN_SYMBOLS = 50;

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const SHANGHAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export async function runWatchlistScan(state, options = {}) {
  setScanFeedback("");
  let payload;
  try {
    payload = watchlistScanPayload(options.root, options);
  } catch (error) {
    setScanFeedback(error?.message || "扫描参数无效", "error");
    throw error;
  }
  const sequence = Number(state.watchlistScanSeq || 0) + 1;
  state.watchlistScanSeq = sequence;
  renderScanLoading(payload.universe);
  try {
    const result = await fetchJson("/api/watchlist/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      signal: options.signal,
    });
    if (!scanIsCurrent(state, sequence, options)) return false;
    state.watchlistScanResult = result;
    renderWatchlistScan(result);
    return true;
  } catch (error) {
    if (isAbortError(error) || !scanIsCurrent(state, sequence, options)) return false;
    renderScanUnavailable(error);
    setScanFeedback(error?.message || "扫描失败，请稍后重试", "error");
    throw error;
  }
}

export function initializeWatchlistScanControls(root = $("watchlistScanForm"), options = {}) {
  const asOf = scanElement(root, "watchlistScanAsOf");
  if (asOf) asOf.max = shanghaiDateText(resolvedNow(options.now));
  return syncWatchlistScanUniverse(root);
}

export function syncWatchlistScanUniverse(root = $("watchlistScanForm")) {
  const custom = selectedScanUniverse(root) === "symbols";
  const field = scanElement(root, "watchlistScanCustomField");
  const input = scanElement(root, "watchlistScanSymbols");
  if (field) field.hidden = !custom;
  if (input) input.disabled = !custom;
  setScanFeedback("");
  return custom;
}

export function watchlistScanPayload(root = $("watchlistScanForm"), options = {}) {
  const conditions = selectedScanConditions(root);
  if (!conditions.length) throw new Error("请至少选择一个扫描条件");
  const universe = selectedScanUniverse(root);
  const payload = { universe, conditions };
  if (universe === "symbols") payload.symbols = customScanSymbols(root);
  const now = resolvedNow(options.now);
  const asOfDate = scanAsOfDate(root, now);
  const asOf = shanghaiAsOfTimestamp(asOfDate, now);
  if (asOf) payload.as_of = asOf;
  return payload;
}

export function selectedScanConditions(root = $("watchlistScanForm")) {
  if (!root || typeof root.querySelectorAll !== "function") return [];
  return Array.from(root.querySelectorAll("input[data-scan-condition]:checked"))
    .map((input) => input.value)
    .filter((value) => Object.prototype.hasOwnProperty.call(WATCHLIST_SCAN_LABELS, value));
}

export function selectedScanUniverse(root = $("watchlistScanForm")) {
  const selected = typeof root?.querySelector === "function"
    ? root.querySelector('input[name="scanUniverse"]:checked')
    : null;
  return selected?.value === "symbols" ? "symbols" : "watchlist";
}

export function customScanSymbols(root = $("watchlistScanForm")) {
  const input = scanElement(root, "watchlistScanSymbols");
  const tokens = String(input?.value || "")
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean);
  if (!tokens.length) throw new Error("请输入至少一个自定义股票代码");
  const symbols = [];
  const seen = new Set();
  for (const token of tokens) {
    let symbol;
    try {
      symbol = validateUiSymbol(token);
    } catch (_error) {
      throw new Error(`股票代码 ${token} 无效，请输入6位A股代码`);
    }
    if (seen.has(symbol)) continue;
    seen.add(symbol);
    symbols.push(symbol);
    if (symbols.length > MAX_WATCHLIST_SCAN_SYMBOLS) {
      throw new Error(`一次最多扫描 ${MAX_WATCHLIST_SCAN_SYMBOLS} 只股票`);
    }
  }
  return symbols;
}

export function renderWatchlistScan(result) {
  const target = $("watchlistScanResults");
  if (!target) return;
  const success = Array.isArray(result?.success) ? result.success : [];
  const missing = Array.isArray(result?.missing) ? result.missing : [];
  const universe = Array.isArray(result?.universe) ? result.universe : [];
  if (!universe.length) {
    setScanBusy(target, false);
    target.innerHTML = `<div class="scan-state"><strong>当前范围暂无可扫描股票</strong></div>`;
    return;
  }
  const ordered = [...success].sort((left, right) => Number(right.matched) - Number(left.matched));
  target.innerHTML = `
    <div class="scan-summary">
      <strong>${escapeHtml(ordered.filter((item) => item.matched).length)} / ${escapeHtml(universe.length)} 只满足全部条件</strong>
      <span>数据截至 ${escapeHtml(result.as_of || "--")} · 规则 ${escapeHtml(result.rule_version || "--")}</span>
    </div>
    <div class="scan-result-list">${ordered.map(scanItemHtml).join("")}${missing.map(scanMissingHtml).join("")}</div>`;
  setScanBusy(target, false);
}

function scanItemHtml(item) {
  const matchedConditions = Array.isArray(item.matched_conditions) ? item.matched_conditions : [];
  const details = Object.entries(item.condition_results || {})
    .map(([condition, matched]) => `${WATCHLIST_SCAN_LABELS[condition] || condition}：${matched ? "是" : "否"}`)
    .join(" · ");
  return `
    <button type="button" class="scan-result ${item.matched ? "is-matched" : ""}" data-scan-symbol="${escapeHtml(item.symbol)}">
      <span><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.data_date || "--")}</small></span>
      <span><b>${item.matched ? "满足" : "未全部满足"}</b><small>${escapeHtml(details || "无条件结果")}</small></span>
      <span><small>收盘 ${escapeHtml(formatNumber(item.metrics?.close))}${matchedConditions.length ? ` · 命中 ${escapeHtml(matchedConditions.length)} 项` : ""}</small></span>
    </button>`;
}

function scanMissingHtml(item) {
  return `
    <div class="scan-result is-missing">
      <span><strong>${escapeHtml(item.symbol || "--")}</strong><small>数据缺失</small></span>
      <span><small>${escapeHtml(item.reason || "日K数据不可用")}</small></span>
    </div>`;
}

function scanIsCurrent(state, sequence, options) {
  return state.watchlistScanSeq === sequence && (!options.isCurrent || options.isCurrent());
}

function renderScanLoading(universe) {
  const target = $("watchlistScanResults");
  if (!target) return;
  setScanBusy(target, true);
  target.innerHTML = `<div class="scan-state"><strong>正在扫描${universe === "symbols" ? "自定义代码" : "当前观察池"}</strong></div>`;
}

function renderScanUnavailable(error) {
  const target = $("watchlistScanResults");
  if (!target) return;
  setScanBusy(target, false);
  target.innerHTML = `<div class="scan-state is-unavailable"><strong>观察池扫描失败</strong><span>${escapeHtml(error?.message || "请稍后重试")}</span></div>`;
}

function scanAsOfDate(root, now = new Date()) {
  const value = String(scanElement(root, "watchlistScanAsOf")?.value || "").trim();
  if (!value) return null;
  if (!strictIsoDate(value)) throw new Error("历史截至日格式无效");
  if (value > shanghaiDateText(now)) throw new Error("历史截至日不能晚于今天");
  return value;
}

function strictIsoDate(value) {
  if (!ISO_DATE_PATTERN.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function shanghaiAsOfTimestamp(value, now) {
  if (!value || value === shanghaiDateText(now)) return null;
  // Offset-free datetimes are interpreted as Shanghai market time by the backend.
  return `${value}T23:59:59`;
}

function resolvedNow(value) {
  const current = value === undefined
    ? new Date()
    : new Date(value instanceof Date ? value.getTime() : value);
  if (Number.isNaN(current.getTime())) throw new Error("当前时间格式无效");
  return current;
}

function shanghaiDateText(value = new Date()) {
  const parts = Object.fromEntries(
    SHANGHAI_DATE_FORMATTER.formatToParts(value).map((part) => [part.type, part.value])
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function scanElement(root, id) {
  if (root && typeof root.querySelector === "function") {
    const found = root.querySelector(`#${id}`);
    if (found) return found;
  }
  return $(id);
}

function setScanBusy(target, busy) {
  if (typeof target.setAttribute === "function") target.setAttribute("aria-busy", String(Boolean(busy)));
  else target.ariaBusy = String(Boolean(busy));
}

function setScanFeedback(message, tone = "") {
  const target = $("watchlistScanFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
}
