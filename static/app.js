import { fetchJson } from "./js/api.js";
import { addAlertRule, evaluateAlerts, removeAlertRule, renderAlertEvents, renderAlerts, updateAlertRule } from "./js/alerts.js";
import { drawKlineChart } from "./js/chart.js";
import { loadDataStatus, loadMonitoring, runMonitorTask } from "./js/diagnostics.js";
import { $, escapeHtml, setMetricTone } from "./js/dom.js";
import { compactErrorMessage } from "./js/errors.js";
import { changeClass, formatNumber } from "./js/format.js";
import { addStockNote, removeStockNote, renderNotes, updateStockNote } from "./js/notes.js";
import { renderResearch } from "./js/research-panels.js";
import { normalizeUiSymbol, validateUiSymbol } from "./js/symbols.js";
import { addWatchlistItem, loadWatchlist, removeWatchlistItem, renderWatchlist } from "./js/watchlist.js";
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
  streamSeq: 0,
  loadSeq: 0,
  watchlist: [],
};

const METRIC_IDS = ["trendScore", "trendLabel", "actionAdvice", "support", "resistance", "ma5", "ma20", "dataQuality"];
const WORKBENCH_PANEL_IDS = [
  "diagnosisPanel",
  "insightOverview",
  "featureSnapshot",
  "qualityPanel",
  "signalEvidence",
  "alphaEvidence",
  "factorList",
  "fundFlowPanel",
  "orderPressurePanel",
  "financialPanel",
  "valuationPanel",
  "abnormalPanel",
  "lhbPanel",
  "aiDashboard",
  "marketRegime",
  "signalValidation",
  "timeframeAlignment",
  "riskReward",
  "factorLab",
  "ruleMatches",
  "strategyCards",
  "buySignals",
  "sellSignals",
  "tSignals",
  "minuteAnalysis",
  "themePanel",
  "leadershipPanel",
  "chipPanel",
  "reviewSummary",
  "reviewPoints",
  "reviewEvents",
  "replayPanel",
  "stockEvents",
  "alertList",
  "alertEvents",
  "markFilters",
  "noteList",
  "adviceTimeline",
];

function setWorkspaceView(view) {
  const target = view || "overview";
  document.querySelectorAll(".workspace-tabs button[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === target);
  });
  document.querySelectorAll(".workspace-view[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.viewPanel === target);
  });
  const analysis = state.lastAnalysis;
  if (!analysis) return;
  requestAnimationFrame(() => {
    if (state.lastAnalysis !== analysis) return;
    drawKline(analysis.klines, analysis.ma5, analysis.ma20);
  });
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
  setStatus("数据刷新中", "warn");
  const workbench = await loadCurrentWorkbench(request);
  if (!workbench || isStaleLoad(request)) return;
  if (!renderCurrentWorkbench(workbench, request)) return;
  refreshCompanionPanels(request);
  await loadCurrentWatchlist(request);
  if (isStaleLoad(request)) return;
  refreshPostWatchlistPanels(request);
}

async function loadCurrentWorkbench(request) {
  try {
    return await loadWorkbench(request.symbol);
  } catch (error) {
    if (!isStaleLoad(request)) markLoadFailure(error, request.symbol, request.previousSymbol);
    return null;
  }
}

function renderCurrentWorkbench(workbench, request) {
  try {
    renderWorkbench(workbench);
    return true;
  } catch (error) {
    if (!isStaleLoad(request)) markRenderFailure(error, request.symbol);
    return false;
  }
}

async function loadCurrentWatchlist(request) {
  try {
    await loadWatchlist(state, { isCurrent: () => !isStaleLoad(request) });
  } catch (error) {
    if (!isStaleLoad(request)) markCompanionFailure(error);
  }
}

function beginLoadRequest() {
  stopStream();
  const request = {
    id: ++state.loadSeq,
    symbol: state.symbol,
    previousSymbol: displayedAnalysisSymbol(),
  };
  renderWorkbenchPending(request.symbol);
  return request;
}

function invalidateActiveLoad() {
  stopStream();
  state.loadSeq += 1;
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
  const analysis = plainObject(workbench.analysis);
  syncWorkbenchChartMarks(workbench.chart_marks);
  renderAnalysis(analysis);
  renderInsights(workbench.insights);
  renderResearch(workbench, state);
  renderAlerts(workbench.alert_rules || []);
  renderAlertEvents(workbench.alert_events || []);
  renderNotes(workbench.notes || []);
  state.lastAnalysis = analysis;
  state.lastInsights = workbench.insights;
  drawKline(analysis.klines, analysis.ma5, analysis.ma20);
  const localWarnings = asLocalDataWarnings(workbench.local_data_warnings);
  $("sourceLine").textContent = sourceLineText(analysis, localWarnings);
  document.body.classList.remove("is-stale");
  setStatus(localWarnings.length ? "核心行情正常，本地数据部分降级" : "实时连接正常", localWarnings.length ? "warn" : "ok");
}

function renderWorkbenchPending(symbol) {
  const requested = normalizeUiSymbol(symbol);
  document.body.classList.remove("is-stale");
  resetWorkbenchState();
  resetWorkbenchHeader(requested, "加载中", "正在读取核心行情、日K和研究面板...");
  renderWorkbenchPlaceholder("数据加载中", `正在切换到 ${requested}，请稍候。`);
  const sourceLine = $("sourceLine");
  if (sourceLine) sourceLine.textContent = `正在加载 ${requested} 的数据...`;
}

function renderWorkbenchFailure(symbol, message) {
  const requested = normalizeUiSymbol(symbol);
  resetWorkbenchState();
  resetWorkbenchHeader(requested, "加载失败", `${requested} 未能加载：${message}`);
  renderWorkbenchPlaceholder(`${requested} 未加载成功`, "当前工作台没有切换到这只股票，请稍后重试或检查数据源状态。");
}

function resetWorkbenchState() {
  state.lastAnalysis = null;
  state.lastInsights = null;
  state.chartMarks = [];
  state.activeMarkCategories.clear();
  clearKlineCanvas();
}

function resetWorkbenchHeader(symbol, name, summary) {
  $("stockCode").textContent = symbol;
  $("stockName").textContent = name;
  $("stockPrice").textContent = "--";
  $("stockChange").textContent = "--";
  $("stockChange").className = "";
  METRIC_IDS.forEach((id) => {
    $(id).textContent = "--";
    setMetricTone(id, "");
  });
  $("summary").textContent = summary;
}

function renderWorkbenchPlaceholder(title, detail) {
  const html = loadingState(title, detail);
  WORKBENCH_PANEL_IDS.forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = html;
  });
}

function clearKlineCanvas() {
  const canvas = $("klineCanvas");
  if (!canvas || typeof canvas.getContext !== "function") return;
  const ctx = canvas.getContext("2d");
  if (!ctx || typeof ctx.clearRect !== "function") return;
  ctx.clearRect(0, 0, canvas.width || canvas.clientWidth || 0, canvas.height || canvas.clientHeight || 0);
}

function displayedAnalysisSymbol() {
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  if (!quote || !quote.code || !quote.market) return "";
  return `${quote.code}.${String(quote.market).toUpperCase()}`;
}

function sourceLineText(analysis, localWarnings = []) {
  const quote = plainObject(analysis.quote);
  const source = `数据源：${quote.source || "--"}，更新时间：${quote.timestamp || "--"}`;
  if (!localWarnings.length) return source;
  const shown = localWarnings.slice(0, 2).map((item) => item.message).join("；");
  const suffix = localWarnings.length > 2 ? `；另有 ${localWarnings.length - 2} 项本地数据降级` : "";
  return `${source}；本地数据提示：${shown}${suffix}`;
}

function asLocalDataWarnings(value) {
  return (Array.isArray(value) ? value : [])
    .map(plainObject)
    .filter((item) => typeof item.message === "string" && item.message.trim())
    .map((item) => ({ component: String(item.component || "local_data"), message: item.message.trim().slice(0, 100) }))
    .slice(0, 5);
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

function refreshPostWatchlistPanels(request = currentLoadContext()) {
  loadAdviceTimeline();
  loadPlateRank(request);
  try {
    startStream();
  } catch (error) {
    if (!isStaleContext(request)) markCompanionFailure(error);
  }
}

async function loadMarketPanels(requestId, requestedSymbol) {
  const [marketResult, strongResult] = await fetchMarketPanelResults();
  if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
  const panels = marketPanelData(marketResult, strongResult);
  try {
    renderMarket(panels.indices, panels.marketMeta);
    renderStrongStocks(panels.strongStocks, panels.strongMeta);
  } catch (error) {
    if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
    renderMarket([], { degraded: true, warnings: [compactErrorMessage(error.message)] });
    $("leaderList").innerHTML = `<div class="leader-row"><strong>观察池排序暂不可用</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span></div>`;
    markCompanionFailure(error);
  }
}

function fetchMarketPanelResults() {
  return Promise.allSettled([fetchJson("/api/market"), fetchJson("/api/strong-stocks")]);
}

function marketPanelData(marketResult, strongResult) {
  const market = fulfilledValue(marketResult);
  const strong = fulfilledValue(strongResult);
  const marketError = rejectedReason(marketResult, "市场概览接口暂不可用");
  const strongError = rejectedReason(strongResult, "强股接口暂不可用");
  return {
    indices: marketIndices(market),
    marketMeta: marketSampleMeta(market, marketError),
    strongStocks: strongStockItems(market, strong),
    strongMeta: strongStockMeta(market, strong, strongError),
  };
}

function fulfilledValue(result) {
  return result.status === "fulfilled" ? result.value : null;
}

function rejectedReason(result, fallbackMessage = "接口暂不可用") {
  if (result.status !== "rejected") return "";
  const reason = result.reason;
  return compactErrorMessage(reason && reason.message ? reason.message : String(reason || fallbackMessage));
}

function marketIndices(market) {
  return market && Array.isArray(market.indices) ? market.indices : [];
}

function marketSampleMeta(market, marketError = "") {
  const meta = market ? plainObject(market.index_meta) : {};
  if (marketError) return { ...meta, degraded: true, fallback_reason: marketError };
  return meta;
}

function strongStockItems(market, strong) {
  if (strong && Array.isArray(strong.items)) return strong.items;
  if (market && Array.isArray(market.strong_stocks)) return market.strong_stocks;
  return [];
}

function strongStockMeta(market, strong, strongError = "") {
  if (strong && Array.isArray(strong.items)) return withoutItems(strong);
  if (market && Array.isArray(market.strong_stocks)) {
    const meta = market.strong_stocks_meta || market.strong_meta || {};
    return strongError ? { scope: "市场概览样本", ...meta, fallback_reason: strongError } : meta;
  }
  return strongError ? { fallback_reason: strongError } : {};
}

function withoutItems(payload) {
  const { items, ...meta } = payload || {};
  return meta;
}

function plainObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function markLoadFailure(error, requestedSymbol = state.symbol, previousSymbol = "") {
  const message = compactErrorMessage(error.message);
  const requested = normalizeUiSymbol(requestedSymbol);
  const displayed = previousSymbol || displayedAnalysisSymbol();
  renderWorkbenchFailure(requested, message);
  document.body.classList.add("is-stale");
  setStatus(`${requested} 加载失败`, "warn");
  const sourceLine = $("sourceLine");
  if (sourceLine) {
    sourceLine.textContent = displayed
      ? `本次请求 ${requested} 失败：${message}；已隔离 ${displayed} 的上次成功数据。`
      : `本次请求 ${requested} 失败：${message}`;
  }
}

function markRenderFailure(error, requestedSymbol = state.symbol) {
  const message = compactErrorMessage(error.message);
  const requested = normalizeUiSymbol(requestedSymbol);
  document.body.classList.add("is-stale");
  setStatus(`${requested} 页面显示异常`, "warn");
  const sourceLine = $("sourceLine");
  if (sourceLine) {
    sourceLine.textContent = `本次请求 ${requested} 数据已返回，但页面渲染异常：${message}`;
  }
}

function markCompanionFailure(error) {
  setStatus(compactErrorMessage(error.message), "warn");
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
    setStatus("图表标注暂不可用", "warn");
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

function currentMutationOptions() {
  const context = currentLoadContext();
  return {
    symbol: context.symbol,
    context,
    isCurrent: () => !isStaleContext(context),
  };
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
  if (button.disabled) return false;
  const previousText = button.textContent;
  const canUseBusyText = button.classList.contains("mini-button");
  try {
    button.disabled = true;
    if (canUseBusyText) button.textContent = "处理中";
    await task();
    return true;
  } catch (error) {
    setStatus(compactErrorMessage(error.message), "warn");
    return false;
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

async function loadPlateRank(request = currentLoadContext()) {
  try {
    const plates = await fetchJson("/api/plates?limit=8");
    if (isStaleContext(request)) return;
    renderPlates(plates);
  } catch (error) {
    if (isStaleContext(request)) return;
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

function startStream({ retry = false } = {}) {
  clearStreamRetryTimer();
  if (!retry) state.streamRetryCount = 0;
  const streamId = ++state.streamSeq;
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  const symbols = streamSymbols();
  if (!symbols.length) return;
  let stream;
  try {
    stream = new EventSource(`/api/stream/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
  } catch (error) {
    setStatus(compactErrorMessage(error.message || "实时连接创建失败"), "warn");
    return;
  }
  state.stream = stream;
  stream.onmessage = (event) => {
    if (!isCurrentStream(stream, streamId)) return;
    const rows = quoteRowsFromStreamEvent(event);
    if (!rows) return;
    state.streamRetryCount = 0;
    renderQuotes(rows);
    setStatus("实时连接正常", "ok");
  };
  stream.addEventListener("quote-error", (event) => {
    if (isCurrentStream(stream, streamId)) handleStreamQuoteError(event);
  });
  stream.onerror = () => scheduleStreamReconnect(stream, streamId);
}

function streamSymbols() {
  const watchSymbols = Array.isArray(state.watchlist) ? state.watchlist.map((item) => item && item.symbol) : [];
  return [state.symbol, ...watchSymbols, "600519", "000001", "300750", "002594", "600036"]
    .map(canonicalStreamSymbol)
    .filter(Boolean)
    .filter((item, index, rows) => rows.indexOf(item) === index)
    .slice(0, 8);
}

function canonicalStreamSymbol(symbol) {
  const normalized = normalizeUiSymbol(symbol);
  return /^\d{6}\.(SH|SZ)$/.test(normalized) ? normalized : "";
}

function isCurrentStream(stream, streamId) {
  return streamId === state.streamSeq && stream === state.stream;
}

function quoteRowsFromStreamEvent(event) {
  let rows;
  try {
    rows = JSON.parse(event.data);
  } catch (error) {
    setStatus("实时行情数据异常，等待下一次刷新", "warn");
    return null;
  }
  if (!Array.isArray(rows)) {
    setStatus("实时行情数据格式异常，等待下一次刷新", "warn");
    return null;
  }
  return rows;
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

function scheduleStreamReconnect(stream, streamId) {
  if (!isCurrentStream(stream, streamId)) return;
  setStatus("实时连接波动，准备重连", "warn");
  if (state.streamRetryTimer || document.hidden) return;
  stopStream({ clearRetryTimer: false });
  const delay = Math.min(30000, 2000 * 2 ** Math.min(state.streamRetryCount, 4));
  state.streamRetryCount += 1;
  state.streamRetryTimer = setTimeout(() => {
    state.streamRetryTimer = null;
    startStream({ retry: true });
  }, delay);
}

function stopStream({ clearRetryTimer = true } = {}) {
  if (clearRetryTimer) clearStreamRetryTimer();
  if (!state.stream) return;
  state.streamSeq += 1;
  state.stream.close();
  state.stream = null;
}

function clearStreamRetryTimer() {
  if (!state.streamRetryTimer) return;
  clearTimeout(state.streamRetryTimer);
  state.streamRetryTimer = null;
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
    invalidateActiveLoad();
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
  const options = currentMutationOptions();
  try {
    if (await addWatchlistItem(state, options)) startStream();
  } catch (error) {
    if (!options.isCurrent()) return;
    renderWatchlist(Array.isArray(state.watchlist) ? state.watchlist : []);
    $("watchList").innerHTML += `<div class="watch-row watch-row-error"><strong>加入失败</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span></div>`;
    setStatus("自选股加入失败", "warn");
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
  const options = currentMutationOptions();
  try {
    await runSubmitTask(event.currentTarget, "添加中", () => addAlertRule(state, options));
  } catch (error) {
    if (options.isCurrent()) {
      $("alertList").innerHTML = `<div class="alert-row"><strong>添加失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
  }
});

$("evaluateAlerts").addEventListener("click", async () => {
  const options = currentMutationOptions();
  try {
    await evaluateAlerts(state, options);
  } catch (error) {
    if (options.isCurrent()) {
      $("alertEvents").innerHTML = `<div class="alert-event"><strong>检查失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    }
  }
});

$("alertList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-alert-toggle]");
  if (toggleButton) {
    const options = currentMutationOptions();
    await runButtonTask(toggleButton, () => updateAlertRule(state, toggleButton.dataset.alertToggle, { enabled: toggleButton.dataset.alertEnabled === "true" }, options));
    return;
  }
  const button = event.target.closest("button[data-alert-remove]");
  if (!button) return;
  const options = currentMutationOptions();
  await runButtonTask(button, () => removeAlertRule(state, button.dataset.alertRemove, options));
});

$("noteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const options = currentMutationOptions();
  try {
    await runSubmitTask(event.currentTarget, "保存中", () => addStockNote(state, loadChartMarks, options));
  } catch (error) {
    if (options.isCurrent()) {
      $("noteList").innerHTML = `<div class="note-row"><strong>保存失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
  }
});

$("noteList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-note-toggle]");
  if (toggleButton) {
    const options = currentMutationOptions();
    await runButtonTask(toggleButton, () => updateStockNote(state, toggleButton.dataset.noteToggle, { visible: toggleButton.dataset.noteVisible === "true" }, loadChartMarks, options));
    return;
  }
  const button = event.target.closest("button[data-note-remove]");
  if (!button) return;
  const options = currentMutationOptions();
  await runButtonTask(button, () => removeStockNote(state, button.dataset.noteRemove, loadChartMarks, options));
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
    stopStream();
    return;
  }
  loadMonitoring(state);
  if (state.lastAnalysis) startStream();
});

export const __appTest = {
  state,
  loadAll,
  loadChartMarks,
  loadAdviceTimeline,
  loadPlateRank,
  loadMarketPanels,
  loadMinuteAnalysis,
  setActiveSymbol,
  setWorkspaceView,
  startStream,
};

if (!globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__) {
  loadAll();
}
