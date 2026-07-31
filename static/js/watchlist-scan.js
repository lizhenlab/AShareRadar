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
export const WATCHLIST_SCAN_TIMEOUT_MS = 60000;

const WATCHLIST_SCAN_METRIC_LABELS = Object.freeze({
  close: "收盘价",
  ma20: "20日均线",
  previous_20d_high: "前20日高点",
  volume_ratio_5d: "5日量比",
});

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
  const previousResult = state.watchlistScanResult || null;
  const form = options.root || $("watchlistScanForm");
  setScanFormBusy(form, true);
  renderScanLoading(payload);
  try {
    const result = await fetchJson("/api/watchlist/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: WATCHLIST_SCAN_TIMEOUT_MS,
      signal: options.signal,
    });
    if (!scanIsCurrent(state, sequence, options)) return false;
    state.watchlistScanResult = result;
    state.watchlistScanComparison = null;
    renderWatchlistScan(result);
    renderWatchlistScanComparison(null);
    return true;
  } catch (error) {
    if (isAbortError(error) || !scanIsCurrent(state, sequence, options)) return false;
    if (previousResult) {
      renderWatchlistScan(previousResult);
    } else {
      renderScanUnavailable(error);
    }
    setScanFeedback(error?.message || "扫描失败，请稍后重试", "error");
    throw error;
  } finally {
    if (scanIsCurrent(state, sequence, options)) setScanFormBusy(form, false);
  }
}

export async function loadWatchlistScanHistory(state, options = {}) {
  try {
    const items = await fetchJson("/api/watchlist/scans?limit=20", {
      signal: options.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!Array.isArray(items)) throw new TypeError("扫描历史格式异常");
    state.watchlistScanHistory = items;
    renderWatchlistScanHistory(items, state.watchlistScanResult?.id, null, Boolean(state.watchlistScanResult));
    return true;
  } catch (error) {
    if (isAbortError(error)) return false;
    renderWatchlistScanHistory([], null, error, Boolean(state.watchlistScanResult));
    return false;
  }
}

export async function loadWatchlistScanRecord(state, scanId, options = {}) {
  const id = positiveScanId(scanId);
  const result = await fetchJson(`/api/watchlist/scans/${id}`, {
    signal: options.signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  state.watchlistScanResult = result;
  state.watchlistScanComparison = null;
  renderWatchlistScan(result);
  renderWatchlistScanComparison(null);
  renderWatchlistScanHistory(state.watchlistScanHistory || [], result.id, null, true);
  return result;
}

export async function compareWatchlistScanHistory(state, scanId, options = {}) {
  if (!state.watchlistScanResult) throw new Error("请先运行或载入一个扫描结果");
  const id = positiveScanId(scanId);
  const baseline = await fetchJson(`/api/watchlist/scans/${id}`, {
    signal: options.signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  const comparison = buildWatchlistScanComparison(state.watchlistScanResult, baseline);
  state.watchlistScanComparison = comparison;
  renderWatchlistScanComparison(comparison);
  return comparison;
}

export function exportWatchlistScanResult(state, options = {}) {
  const result = state.watchlistScanResult;
  if (!result) throw new Error("当前没有可导出的扫描结果");
  const content = JSON.stringify(result, null, 2);
  if (typeof options.save === "function") {
    options.save(content, watchlistScanExportName(result));
    return true;
  }
  const blob = new Blob([content], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = watchlistScanExportName(result);
  anchor.click?.();
  URL.revokeObjectURL(url);
  return true;
}

export function buildWatchlistScanComparison(current, baseline) {
  const currentMatched = matchedSymbolSet(current);
  const baselineMatched = matchedSymbolSet(baseline);
  return {
    current_id: current?.id || null,
    baseline_id: baseline?.id || null,
    current_as_of: current?.as_of || "--",
    baseline_as_of: baseline?.as_of || "--",
    current_matched_count: currentMatched.size,
    baseline_matched_count: baselineMatched.size,
    newly_matched: [...currentMatched].filter((symbol) => !baselineMatched.has(symbol)).sort(),
    no_longer_matched: [...baselineMatched].filter((symbol) => !currentMatched.has(symbol)).sort(),
  };
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

export function syncWatchlistScanConditions(root = $("watchlistScanForm"), changed = null) {
  const value = changed?.value;
  if (!changed?.checked || !["close_above_ma20", "close_below_ma20"].includes(value)) return false;
  const opposite = value === "close_above_ma20" ? "close_below_ma20" : "close_above_ma20";
  const input = typeof root?.querySelector === "function"
    ? root.querySelector(`input[data-scan-condition][value="${opposite}"]`)
    : null;
  if (input?.checked) input.checked = false;
  return Boolean(input);
}

export function watchlistScanPayload(root = $("watchlistScanForm"), options = {}) {
  const conditions = selectedScanConditions(root);
  if (!conditions.length) throw new Error("请至少选择一个扫描条件");
  if (conditions.includes("close_above_ma20") && conditions.includes("close_below_ma20")) {
    throw new Error("高于20日均线与低于20日均线不能同时选择");
  }
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
    syncScanHistoryButtons(Boolean(result));
    return;
  }
  const ordered = [...success].sort((left, right) => Number(right.matched) - Number(left.matched));
  target.innerHTML = `
    <div class="scan-evidence-banner">
      <strong>只读历史证据</strong>
      <span>截至 ${escapeHtml(result.as_of || "--")} · 规则版本 ${escapeHtml(result.rule_version || "--")}</span>
    </div>
    <div class="scan-summary">
      <strong>${escapeHtml(ordered.filter((item) => item.matched).length)} / ${escapeHtml(universe.length)} 只满足全部条件</strong>
      <span>条件结果和指标均来自该时点，不代表当前状态</span>
    </div>
    <div class="scan-result-list">${ordered.map((item) => scanItemHtml(item)).join("")}${missing.map(scanMissingHtml).join("")}</div>`;
  setScanBusy(target, false);
  syncScanHistoryButtons(Boolean(result));
}

function renderWatchlistScanHistory(items, selectedId = null, error = null, hasResult = false) {
  const select = $("watchlistScanHistory");
  if (!select) return;
  const rows = Array.isArray(items) ? items : [];
  select.innerHTML = rows.length
    ? rows.map((item) => `<option value="${escapeHtml(item.id)}"${Number(item.id) === Number(selectedId) ? " selected" : ""}>#${escapeHtml(item.id)} · ${escapeHtml(item.as_of || "--")} · ${escapeHtml(item.matched_count || 0)}/${escapeHtml(item.universe_count || 0)} 命中</option>`).join("")
    : `<option value="">${escapeHtml(error ? "历史读取失败" : "暂无历史记录")}</option>`;
  select.disabled = !rows.length;
  syncScanHistoryButtons(hasResult);
}

function renderWatchlistScanComparison(comparison) {
  const target = $("watchlistScanComparison");
  if (!target) return;
  if (!comparison) {
    target.hidden = true;
    target.innerHTML = "";
    return;
  }
  const gained = comparison.newly_matched || [];
  const lost = comparison.no_longer_matched || [];
  target.hidden = false;
  target.innerHTML = `
    <strong>扫描对比：${escapeHtml(comparison.baseline_as_of)} → ${escapeHtml(comparison.current_as_of)}</strong>
    <span>命中数 ${escapeHtml(comparison.baseline_matched_count)} → ${escapeHtml(comparison.current_matched_count)}</span>
    <span>新增命中：${escapeHtml(gained.slice(0, 12).join("、") || "无")}${gained.length > 12 ? ` 等 ${gained.length} 只` : ""}</span>
    <span>不再命中：${escapeHtml(lost.slice(0, 12).join("、") || "无")}${lost.length > 12 ? ` 等 ${lost.length} 只` : ""}</span>`;
}

function syncScanHistoryButtons(hasResult = false) {
  const selected = Number($("watchlistScanHistory")?.value);
  const hasSelected = Number.isInteger(selected) && selected > 0;
  const load = $("loadWatchlistScanHistoryRecord");
  const compare = $("compareWatchlistScanHistory");
  const exportButton = $("exportWatchlistScanResult");
  if (load) load.disabled = !hasSelected;
  if (compare) compare.disabled = !hasSelected || !hasResult;
  if (exportButton) exportButton.disabled = !hasResult;
}

function matchedSymbolSet(result) {
  return new Set((Array.isArray(result?.success) ? result.success : []).filter((item) => item?.matched).map((item) => item.symbol));
}

function positiveScanId(value) {
  const id = Number(value);
  if (!Number.isInteger(id) || id <= 0) throw new Error("请选择有效扫描历史");
  return id;
}

function watchlistScanExportName(result) {
  const date = String(result?.as_of || "scan").slice(0, 10).replaceAll("-", "");
  return `watchlist-scan-${date}-${result?.id || "current"}.json`;
}

function scanItemHtml(item) {
  const matchedConditions = Array.isArray(item.matched_conditions) ? item.matched_conditions : [];
  const conditions = Object.entries(item.condition_results || {});
  const metrics = Object.entries(item.metrics || {});
  return `
    <article class="scan-result ${item.matched ? "is-matched" : ""}" data-scan-evidence-symbol="${escapeHtml(item.symbol)}">
      <header>
        <span><strong>${escapeHtml(item.symbol)}</strong><small>数据日期 ${escapeHtml(item.data_date || "--")}</small></span>
        <b>${item.matched ? "满足全部条件" : "未全部满足"}</b>
      </header>
      <div class="scan-condition-evidence" aria-label="当时条件结果">
        ${conditions.length
          ? conditions.map(([condition, matched]) => `<span>${escapeHtml(WATCHLIST_SCAN_LABELS[condition] || condition)}：${matched ? "是" : "否"}</span>`).join("")
          : `<span>无条件结果</span>`}
      </div>
      <div class="scan-metric-evidence" aria-label="当时指标">
        ${metrics.length
          ? metrics.map(([key, value]) => `<span><small>${escapeHtml(WATCHLIST_SCAN_METRIC_LABELS[key] || key)}</small><strong> ${escapeHtml(formatNumber(value))}</strong></span>`).join("")
          : `<span><small>当时指标</small><strong>--</strong></span>`}
      </div>
      <footer>
        <small>${matchedConditions.length ? `命中 ${escapeHtml(matchedConditions.length)} 项` : "未命中条件"}</small>
        <button type="button" class="mini-button" data-scan-current-symbol="${escapeHtml(item.symbol)}">查看当前分析</button>
      </footer>
    </article>`;
}

function scanMissingHtml(item) {
  return `
    <article class="scan-result is-missing">
      <span><strong>${escapeHtml(item.symbol || "--")}</strong><small>数据缺失</small></span>
      <span><small>${escapeHtml(item.reason || "日K数据不可用")}</small></span>
    </article>`;
}

function scanIsCurrent(state, sequence, options) {
  return state.watchlistScanSeq === sequence && (!options.isCurrent || options.isCurrent());
}

function renderScanLoading(payload) {
  const target = $("watchlistScanResults");
  if (!target) return;
  setScanBusy(target, true);
  const scope = payload.universe === "symbols" ? "自定义代码" : "当前观察池";
  const stage = payload.as_of ? "正在读取历史日K并计算条件" : "正在读取完整日K并计算条件";
  target.innerHTML = `<div class="scan-state loading"><strong>${escapeHtml(stage)}</strong><span>阶段 1/2 · 准备${escapeHtml(scope)}证据快照</span></div>`;
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

function setScanFormBusy(form, busy) {
  if (!form) return;
  if (typeof form.setAttribute === "function") form.setAttribute("aria-busy", String(Boolean(busy)));
  else form.ariaBusy = String(Boolean(busy));
}

function setScanFeedback(message, tone = "") {
  const target = $("watchlistScanFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
}
