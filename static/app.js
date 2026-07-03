import { fetchJson } from "./js/api.js";
import { addAlertRule, evaluateAlerts, removeAlertRule, renderAlertEvents, renderAlerts, updateAlertRule } from "./js/alerts.js";
import { drawKlineChart } from "./js/chart.js";
import { loadDataStatus, loadMonitoring, runMonitorTask } from "./js/diagnostics.js";
import { $, escapeHtml } from "./js/dom.js";
import { compactErrorMessage } from "./js/errors.js";
import { changeClass, formatNumber } from "./js/format.js";
import { addStockNote, removeStockNote, renderNotes, updateStockNote } from "./js/notes.js";
import { renderResearch } from "./js/research-panels.js";
import { normalizeUiSymbol, validateUiSymbol } from "./js/symbols.js";
import { addWatchlistItem, loadWatchlist, removeWatchlistItem } from "./js/watchlist.js";
import { renderAnalysis, renderInsights, renderMarket, renderMinuteAnalysis, renderQuotes, renderStrongStocks } from "./js/workbench.js";

const state = {
  symbol: "600519",
  stream: null,
  lastAnalysis: null,
  lastInsights: null,
  chartMarks: [],
  activeMarkCategories: new Set(),
  monitorTimer: null,
  streamRetryTimer: null,
  streamRetryCount: 0,
  loadSeq: 0,
  watchlist: [],
};

function setWorkspaceView(view) {
  const target = view || "overview";
  document.querySelectorAll(".workspace-tabs button[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === target);
  });
  document.querySelectorAll(".workspace-view[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.viewPanel === target);
  });
  if (state.lastAnalysis) {
    requestAnimationFrame(() => drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20));
  }
}

function setStatus(text, kind = "") {
  const el = $("dataStatus");
  el.textContent = text;
  el.className = `status-pill ${kind}`;
}

function loadingState(title, detail = "正在读取数据，请稍候。") {
  return `<div class="minute-state loading"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

async function loadAll() {
  const request = beginLoadRequest();
  try {
    setStatus("数据刷新中", "warn");
    const workbench = await loadWorkbench(request.symbol);
    if (isStaleLoad(request)) return;
    renderWorkbench(workbench);
    refreshCompanionPanels(request);
    await loadWatchlist(state);
    if (isStaleLoad(request)) return;
    refreshPostWatchlistPanels();
  } catch (error) {
    if (isStaleLoad(request)) return;
    markLoadFailure(error, request.symbol);
  }
}

function beginLoadRequest() {
  return {
    id: ++state.loadSeq,
    symbol: state.symbol,
  };
}

function isStaleLoad(request) {
  return request.id !== state.loadSeq || request.symbol !== state.symbol;
}

function setActiveSymbol(symbol) {
  state.symbol = normalizeUiSymbol(symbol);
  syncSymbolInputs(state.symbol);
}

function syncSymbolInputs(symbol) {
  const code = normalizeUiSymbol(symbol).slice(0, 6);
  const symbolInput = $("symbolInput");
  const watchSymbolInput = $("watchSymbolInput");
  if (symbolInput) symbolInput.value = code;
  if (watchSymbolInput) watchSymbolInput.value = code;
}

function loadWorkbench(symbol) {
  return fetchJson(`/api/stock/workbench?symbol=${encodeURIComponent(symbol)}`);
}

function renderWorkbench(workbench) {
  const analysis = workbench.analysis;
  document.body.classList.remove("is-stale");
  renderAnalysis(analysis, { state, drawKline });
  renderInsights(workbench.insights, state);
  renderResearch(workbench, state);
  syncWorkbenchChartMarks(workbench.chart_marks);
  drawKline(analysis.klines, analysis.ma5, analysis.ma20);
  renderAlerts(workbench.alert_rules || []);
  renderAlertEvents(workbench.alert_events || []);
  renderNotes(workbench.notes || []);
  $("sourceLine").textContent = `数据源：${analysis.quote.source}，更新时间：${analysis.quote.timestamp}`;
  setStatus("实时连接正常", "ok");
}

function syncWorkbenchChartMarks(chartMarks) {
  state.chartMarks = chartMarks ? chartMarks.marks || [] : [];
  syncMarkCategories(chartMarks ? chartMarks.categories || [] : []);
  renderMarkFilters();
}

function refreshCompanionPanels(request) {
  loadMarketPanels(request.id, request.symbol);
  loadDataStatus();
  loadMonitoring(state);
  loadMinuteAnalysis(loadContextFromRequest(request));
}

function refreshPostWatchlistPanels() {
  loadAdviceTimeline();
  loadPlateRank();
  startStream();
}

async function loadMarketPanels(requestId, requestedSymbol) {
  const [marketResult, strongResult] = await fetchMarketPanelResults();
  if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
  const panels = marketPanelData(marketResult, strongResult);
  renderMarket(panels.indices);
  renderStrongStocks(panels.strongStocks, panels.strongMeta);
}

function fetchMarketPanelResults() {
  return Promise.allSettled([fetchJson("/api/market"), fetchJson("/api/strong-stocks")]);
}

function marketPanelData(marketResult, strongResult) {
  const market = fulfilledValue(marketResult);
  const strong = fulfilledValue(strongResult);
  return {
    indices: marketIndices(market),
    strongStocks: strongStockItems(market, strong),
    strongMeta: strongStockMeta(market, strong),
  };
}

function fulfilledValue(result) {
  return result.status === "fulfilled" ? result.value : null;
}

function marketIndices(market) {
  return market && Array.isArray(market.indices) ? market.indices : [];
}

function strongStockItems(market, strong) {
  if (strong && Array.isArray(strong.items)) return strong.items;
  if (market && Array.isArray(market.strong_stocks)) return market.strong_stocks;
  return [];
}

function strongStockMeta(market, strong) {
  if (strong && Array.isArray(strong.items)) return withoutItems(strong);
  if (market && Array.isArray(market.strong_stocks)) return market.strong_stocks_meta || market.strong_meta || {};
  return {};
}

function withoutItems(payload) {
  const { items, ...meta } = payload || {};
  return meta;
}

function markLoadFailure(error, requestedSymbol = state.symbol) {
  const message = compactErrorMessage(error.message);
  const requested = normalizeUiSymbol(requestedSymbol);
  const displayedQuote = state.lastAnalysis && state.lastAnalysis.quote;
  const displayed = displayedQuote ? `${displayedQuote.code}.${displayedQuote.market}` : "";
  document.body.classList.add("is-stale");
  setStatus(`${requested} 加载失败`, "warn");
  const sourceLine = $("sourceLine");
  if (sourceLine) {
    sourceLine.textContent = state.lastAnalysis
      ? `本次请求 ${requested} 失败：${message}；当前仍显示 ${displayed} 的上次成功数据。`
      : `本次请求 ${requested} 失败：${message}`;
  }
}

async function loadAdviceTimeline() {
  const requestedSymbol = state.symbol;
  const requestedLoadSeq = state.loadSeq;
  try {
    const items = await fetchJson(`/api/advice/history?symbol=${encodeURIComponent(requestedSymbol)}&limit=8`);
    if (requestedSymbol !== state.symbol || requestedLoadSeq !== state.loadSeq) return;
    renderAdviceTimeline(items);
  } catch (error) {
    if (requestedSymbol !== state.symbol || requestedLoadSeq !== state.loadSeq) return;
    $("adviceTimeline").innerHTML = `<div class="timeline-item"><strong>时间线暂不可用</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function loadMinuteAnalysis(request = currentLoadContext()) {
  const el = $("minuteAnalysis");
  if (!el) return;
  const requestedSymbol = request.symbol;
  el.innerHTML = loadingState("分钟分析加载中", "正在读取5分钟K线，不影响主分析。");
  try {
    const report = await fetchJson(`/api/stock/minute-analysis?symbol=${encodeURIComponent(requestedSymbol)}&interval=5m&limit=120`);
    if (isStaleContext(request)) return;
    renderMinuteAnalysis(report);
  } catch (error) {
    if (isStaleContext(request)) return;
    el.innerHTML = `<div class="minute-empty"><strong>分钟分析暂不可用</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span><span>当前不按分钟区间做T，主分析和日线策略仍可参考。</span></div>`;
  }
}

async function loadChartMarks(request = currentLoadContext()) {
  const requestedSymbol = request.symbol;
  try {
    const summary = await fetchJson(`/api/stock/chart-marks?symbol=${encodeURIComponent(requestedSymbol)}&limit=40`);
    if (isStaleContext(request)) return;
    state.chartMarks = summary.marks || [];
    syncMarkCategories(summary.categories || []);
    renderMarkFilters();
    if (state.lastAnalysis) {
      drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
    }
  } catch (error) {
    if (isStaleContext(request)) return;
    state.chartMarks = [];
  }
}

function currentLoadContext() {
  return {
    symbol: state.symbol,
    loadSeq: state.loadSeq,
  };
}

function loadContextFromRequest(request) {
  return {
    symbol: request.symbol,
    loadSeq: request.loadSeq ?? request.id,
  };
}

function isStaleContext(request) {
  return request.symbol !== state.symbol || request.loadSeq !== state.loadSeq;
}

function syncMarkCategories(categories) {
  const current = state.activeMarkCategories;
  if (!current || current.size === 0) {
    state.activeMarkCategories = new Set(categories);
    return;
  }
  state.activeMarkCategories = new Set(categories.filter((item) => current.has(item)));
  if (state.activeMarkCategories.size === 0 && categories.length) {
    state.activeMarkCategories = new Set(categories);
  }
}

function renderMarkFilters() {
  const el = $("markFilters");
  if (!el) return;
  const categories = Array.from(new Set((state.chartMarks || []).map((item) => item.category))).sort();
  el.innerHTML = categories.length
    ? categories
        .map((category) => {
          const active = state.activeMarkCategories.has(category);
          return `<button type="button" class="${active ? "active" : ""}" data-mark-category="${escapeHtml(category)}">${escapeHtml(category)}</button>`;
        })
        .join("")
    : `<span>暂无图表标注</span>`;
}

function renderAdviceTimeline(items) {
  $("adviceTimeline").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="timeline-item">
            <div>
              <strong>${escapeHtml(item.action)} · ${escapeHtml(item.confidence)}%</strong>
              <span>${escapeHtml(formatAdviceTime(item))} · 趋势 ${escapeHtml(item.trend_score)} · ${escapeHtml(item.data_quality_level)} ${escapeHtml(item.data_quality_score)}分${item.repeat_count > 1 ? ` · 合并${escapeHtml(item.repeat_count)}次` : ""}</span>
            </div>
            <p>${escapeHtml(item.reason)}</p>
          </div>`
        )
        .join("")
    : `<div class="timeline-item"><strong>暂无建议留痕</strong><p>完成一次个股分析后，这里会记录趋势评分和建议变化。</p></div>`;
}

function formatAdviceTime(item) {
  if (!item.updated_at || item.updated_at === item.created_at) return item.created_at;
  return `${item.created_at} 至 ${item.updated_at}`;
}

async function runButtonTask(button, task) {
  const previousText = button.textContent;
  const canUseBusyText = button.classList.contains("mini-button");
  try {
    button.disabled = true;
    if (canUseBusyText) button.textContent = "处理中";
    await task();
  } catch (error) {
    setStatus(compactErrorMessage(error.message), "warn");
  } finally {
    button.disabled = false;
    if (canUseBusyText) button.textContent = previousText;
  }
}

async function runSubmitTask(form, busyText, task) {
  const button = submitButton(form);
  if (button && button.disabled) return;
  const previousText = button ? button.textContent : "";
  try {
    if (button) {
      button.disabled = true;
      button.textContent = busyText;
    }
    await task();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = previousText;
    }
  }
}

function submitButton(form) {
  if (!form || typeof form.querySelector !== "function") return null;
  return form.querySelector('button[type="submit"]') || form.querySelector("button");
}

async function loadPlateRank() {
  try {
    const plates = await fetchJson("/api/plates?limit=8");
    renderPlates(plates);
  } catch (error) {
    $("plateList").innerHTML = `<div class="leader-row"><strong>行业背景暂不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function renderPlates(items) {
  $("plateList").innerHTML = items.length
    ? items
        .map(
          (item) => `
      <div class="leader-row">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <small>${escapeHtml(item.leading_stock ? `领涨：${item.leading_stock}` : item.source)}</small>
        </div>
        <div>
          <div class="leader-rank">${escapeHtml(item.rank)}</div>
          <span class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</span>
        </div>
      </div>`
        )
        .join("")
    : `<div class="leader-row"><strong>暂无行业背景</strong><span>等待本地调度器刷新。</span></div>`;
}

function startStream() {
  if (state.streamRetryTimer) {
    clearTimeout(state.streamRetryTimer);
    state.streamRetryTimer = null;
  }
  if (state.stream) state.stream.close();
  const watchSymbols = state.watchlist.map((item) => item.symbol);
  const symbols = [state.symbol, ...watchSymbols, "600519", "000001", "300750", "002594", "600036"]
    .filter(Boolean)
    .map(normalizeUiSymbol)
    .filter((item, index, rows) => rows.indexOf(item) === index)
    .slice(0, 8)
    .join(",");
  state.stream = new EventSource(`/api/stream/quotes?symbols=${symbols}`);
  state.stream.onmessage = (event) => {
    let rows;
    try {
      rows = JSON.parse(event.data);
    } catch (error) {
      setStatus("实时行情数据异常，等待下一次刷新", "warn");
      return;
    }
    state.streamRetryCount = 0;
    renderQuotes(rows);
    setStatus("实时连接正常", "ok");
  };
  state.stream.addEventListener("quote-error", handleStreamQuoteError);
  state.stream.onerror = () => scheduleStreamReconnect();
}

function handleStreamQuoteError(event) {
  if (event && typeof event.data === "string") {
    try {
      const payload = JSON.parse(event.data);
      setStatus(compactErrorMessage(payload.message || "实时行情暂不可用"), "warn");
      return;
    } catch (error) {
      setStatus("实时行情暂不可用", "warn");
      return;
    }
  }
  setStatus("实时行情暂不可用", "warn");
}

function scheduleStreamReconnect() {
  setStatus("实时连接波动，准备重连", "warn");
  if (state.streamRetryTimer || document.hidden) return;
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  const delay = Math.min(30000, 2000 * 2 ** Math.min(state.streamRetryCount, 4));
  state.streamRetryCount += 1;
  state.streamRetryTimer = setTimeout(() => {
    state.streamRetryTimer = null;
    startStream();
  }, delay);
}

function drawKline(rows, ma5, ma20) {
  drawKlineChart({
    canvas: $("klineCanvas"),
    rows,
    ma5,
    ma20,
    marks: state.chartMarks,
    activeCategories: state.activeMarkCategories,
    formatNumber,
  });
}

$("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    setActiveSymbol(validateUiSymbol($("symbolInput").value || "600519"));
  } catch (error) {
    markLoadFailure(error, $("symbolInput").value);
    return;
  }
  loadAll();
});

$("quickList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-symbol]");
  if (!button) return;
  setActiveSymbol(button.dataset.symbol);
  loadAll();
});

document.querySelector(".workspace-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button) return;
  setWorkspaceView(button.dataset.view);
});

$("watchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await addWatchlistItem(state);
    startStream();
  } catch (error) {
    $("watchList").innerHTML = `<div class="watch-row"><strong>加入失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("watchList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const symbol = button.dataset.symbol;
  if (button.dataset.action === "open") {
    setActiveSymbol(symbol);
    loadAll();
    return;
  }
  if (button.dataset.action === "remove") {
    await runButtonTask(button, async () => {
      await removeWatchlistItem(state, symbol);
      startStream();
    });
  }
});

$("alertForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await runSubmitTask(event.currentTarget, "添加中", () => addAlertRule(state));
  } catch (error) {
    $("alertList").innerHTML = `<div class="alert-row"><strong>添加失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("evaluateAlerts").addEventListener("click", async () => {
  try {
    await evaluateAlerts(state);
  } catch (error) {
    $("alertEvents").innerHTML = `<div class="alert-event"><strong>检查失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("alertList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-alert-toggle]");
  if (toggleButton) {
    await runButtonTask(toggleButton, () => updateAlertRule(state, toggleButton.dataset.alertToggle, { enabled: toggleButton.dataset.alertEnabled === "true" }));
    return;
  }
  const button = event.target.closest("button[data-alert-remove]");
  if (!button) return;
  await runButtonTask(button, () => removeAlertRule(state, button.dataset.alertRemove));
});

$("noteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await runSubmitTask(event.currentTarget, "保存中", () => addStockNote(state, loadChartMarks));
  } catch (error) {
    $("noteList").innerHTML = `<div class="note-row"><strong>保存失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("noteList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-note-toggle]");
  if (toggleButton) {
    await runButtonTask(toggleButton, () => updateStockNote(state, toggleButton.dataset.noteToggle, { visible: toggleButton.dataset.noteVisible === "true" }, loadChartMarks));
    return;
  }
  const button = event.target.closest("button[data-note-remove]");
  if (!button) return;
  await runButtonTask(button, () => removeStockNote(state, button.dataset.noteRemove, loadChartMarks));
});

$("markFilters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mark-category]");
  if (!button) return;
  const category = button.dataset.markCategory;
  if (state.activeMarkCategories.has(category)) {
    state.activeMarkCategories.delete(category);
  } else {
    state.activeMarkCategories.add(category);
  }
  renderMarkFilters();
  if (state.lastAnalysis) {
    drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
  }
});

document.querySelector(".monitor-actions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-task]");
  if (!button) return;
  runMonitorTask(state, button.dataset.task);
});

window.addEventListener("resize", () => {
  clearTimeout(window.__chartTimer);
  window.__chartTimer = setTimeout(() => {
    if (state.lastAnalysis) {
      drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
    }
  }, 200);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (state.monitorTimer) {
      clearInterval(state.monitorTimer);
      state.monitorTimer = null;
    }
    if (state.streamRetryTimer) {
      clearTimeout(state.streamRetryTimer);
      state.streamRetryTimer = null;
    }
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
    return;
  }
  loadMonitoring(state);
  if (state.lastAnalysis) startStream();
});

export const __appTest = {
  state,
  loadChartMarks,
  loadAdviceTimeline,
  loadMarketPanels,
  loadMinuteAnalysis,
  setActiveSymbol,
  startStream,
};

if (!globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__) {
  loadAll();
}
