import {
  DEFAULT_REQUEST_TIMEOUT_MS,
  GLOBAL_DATA_TTL_MS,
  createRequestScope,
  fetchCachedJson,
  fetchJson,
  getCachedJsonSnapshot,
  isAbortError,
} from "./js/api.js";
import {
  addAlertRule,
  alertRuleUpdatesFromForm,
  evaluateAlerts,
  removeAlertRule,
  renderAlertEvents,
  renderAlerts,
  toggleAlertRuleEditor,
  updateAlertRule,
} from "./js/alerts.js";
import {
  renderAdviceTimeline,
  renderAdviceTimelineLoading,
  renderAdviceTimelineUnavailable,
} from "./js/advice-timeline.js";
import {
  beginAdviceReviewEdit,
  cancelAdviceReviewEdit,
  deleteAdviceReviewPlan,
  evaluateAdviceReviewPlan,
  loadAdviceReviews,
  retryAdviceReviewHistory,
  selectAdviceReviewSnapshot,
  setAdviceReviewEvaluationAsOf,
  submitAdviceReviewPlan,
  syncAdviceReviewSnapshots,
  toggleAdviceReviewHistory,
} from "./js/advice-reviews.js";
import { drawKlineChart } from "./js/chart.js";
import { createChartInspector } from "./js/chart-inspector.js";
import {
  cancelDataStatusRefresh,
  cancelMonitoringRefresh,
  loadDataStatus,
  loadMonitoring,
  loadSystemDiagnostics,
  runMonitorTask,
} from "./js/diagnostics.js";
import { $, escapeHtml, setMetricTone } from "./js/dom.js";
import { compactErrorMessage } from "./js/errors.js";
import { changeClass, formatNumber } from "./js/format.js";
import {
  addStockNote,
  removeStockNote,
  renderNotes,
  stockNoteUpdatesFromForm,
  toggleStockNoteEditor,
  updateStockNote,
} from "./js/notes.js";
import { disableAlertNotifications, enableAlertNotifications, initializeAlertNotifications } from "./js/notifications.js";
import {
  commitLocalDataImport,
  exportLocalUserData,
  invalidateLocalDataImportPreview,
  loadRuntimeCleanupPreview,
  previewLocalDataImport,
  readLocalDataFile,
  runRuntimeCleanup,
} from "./js/local-data.js";
import { renderResearch } from "./js/research-panels.js";
import { ACTIVITY_FILTERS, mergeResearchActivity, renderResearchActivity } from "./js/research-activity.js";
import { createStockSearchController } from "./js/stock-search.js";
import { normalizeUiSymbol, validateUiSymbol } from "./js/symbols.js";
import {
  addWatchlistItem,
  appendWatchlistMessage,
  invalidateWatchlistCache,
  isExcludedWatchlistItem,
  loadWatchlist,
  markWatchlistItemViewed,
  removeWatchlistItem,
  renderWatchlist,
  toggleWatchlistEditor,
  updateWatchlistItem,
  watchlistUpdatesFromForm,
} from "./js/watchlist.js";
import {
  initializeWatchlistScanControls,
  runWatchlistScan,
  syncWatchlistScanUniverse,
} from "./js/watchlist-scan.js";
import {
  minuteAvailabilityState,
  renderAnalysis,
  renderInsights,
  renderMarket,
  renderMinuteAnalysis,
  renderQuotes,
  renderStrongStocks,
} from "./js/workbench.js";
import {
  DEFAULT_WORKSPACE_PREFERENCES,
  WORKSPACE_PREFERENCE_OPTIONS,
  loadWorkspacePreferences,
  saveWorkspacePreferences,
} from "./js/workspace-preferences.js";

const DAILY_CHART_RANGES = WORKSPACE_PREFERENCE_OPTIONS.dailyChartRange;
const MINUTE_CHART_INTERVALS = WORKSPACE_PREFERENCE_OPTIONS.minuteChartInterval;
const MINUTE_CHART_ROW_LIMIT = 500;

const state = {
  symbol: "600519",
  stream: null,
  lastAnalysis: null,
  lastInsights: null,
  lastMinuteReport: null,
  lastMinuteSymbol: "",
  dailyChartInspection: null,
  minuteChartInspection: null,
  workspaceView: DEFAULT_WORKSPACE_PREFERENCES.workspaceView,
  dailyChartRange: DEFAULT_WORKSPACE_PREFERENCES.dailyChartRange,
  dailyChartMa5: DEFAULT_WORKSPACE_PREFERENCES.dailyChartMa5,
  dailyChartMa20: DEFAULT_WORKSPACE_PREFERENCES.dailyChartMa20,
  minuteChartInterval: DEFAULT_WORKSPACE_PREFERENCES.minuteChartInterval,
  minuteChartPhase: "idle",
  mobileChartView: DEFAULT_WORKSPACE_PREFERENCES.mobileChartView,
  minuteRequest: null,
  minuteRequestSeq: 0,
  chartMarks: [],
  activeMarkCategories: new Set(),
  monitorTimer: null,
  streamRetryTimer: null,
  streamRetryCount: 0,
  streamRetryGeneration: 0,
  streamSeq: 0,
  streamContext: null,
  streamSubscriptionKey: "",
  loadSeq: 0,
  loadRequest: null,
  pendingLoad: null,
  watchlist: [],
  researchActivitySymbol: "",
  researchActivityFilter: "all",
  researchActivityAdvice: [],
  researchActivityAlerts: [],
  researchActivityNotes: [],
  researchActivityAdviceSource: { phase: "loading", message: "等待建议记录" },
  researchActivityAlertSource: { phase: "loading", message: "等待提醒记录" },
  researchActivityNoteSource: { phase: "loading", message: "等待笔记记录" },
  coreStatus: { phase: "idle", text: "数据连接中", kind: "" },
  dataQualityStatus: { phase: "unknown", text: "", kind: "" },
  auxiliaryStatus: { failures: {} },
  mutationStatus: { phase: "idle", text: "", kind: "" },
  sseStatus: { phase: "idle", text: "", kind: "", hasValidFrame: false },
  visibilityRefreshSources: new Set(),
  adviceReviewDetails: [],
  adviceReviewSnapshots: [],
  adviceReviewEditingPlanId: null,
  adviceReviewHistories: {},
  adviceReviewAsOfByPlan: {},
  adviceReviewEvaluationSeqByPlan: {},
  adviceReviewHistoryEpoch: 0,
  adviceReviewHistorySymbol: "",
  adviceTimelineWatermark: null,
};

const stockSearchBindings = [];
let restoringWorkspacePreferences = false;

export const GLOBAL_REFRESH_TTL_MS = GLOBAL_DATA_TTL_MS;
export const GLOBAL_ENDPOINTS = Object.freeze([
  "/api/market",
  "/api/strong-stocks",
  "/api/data/status",
  "/api/tasks/status",
  "/api/tasks/runs?limit=8",
  "/api/monitor/events?limit=8",
  "/api/watchlist",
  "/api/plates?limit=8",
  "/api/system/diagnostics",
]);

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
  "researchActivity",
  "adviceTimeline",
  "reviewPlanList",
  "watchlistScanResults",
];

function setWorkspaceView(view) {
  const buttons = Array.from(document.querySelectorAll(".workspace-tabs button[data-view]"));
  const supportedViews = WORKSPACE_PREFERENCE_OPTIONS.workspaceView;
  const requested = supportedViews.includes(view) ? view : DEFAULT_WORKSPACE_PREFERENCES.workspaceView;
  const fallback = buttons.find((button) => supportedViews.includes(button.dataset.view));
  const target = buttons.some((button) => button.dataset.view === requested)
    ? requested
    : fallback?.dataset.view || DEFAULT_WORKSPACE_PREFERENCES.workspaceView;
  state.workspaceView = target;
  buttons.forEach((button) => {
    const active = button.dataset.view === target;
    button.classList.toggle("active", active);
    setElementAttribute(button, "aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".workspace-view[data-view-panel]").forEach((panel) => {
    const active = panel.dataset.viewPanel === target;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  persistWorkspacePreferences();
  if (target === "tools") void loadRuntimeCleanupPreview().catch(() => {});
  const analysis = state.lastAnalysis;
  if (!analysis) return;
  requestAnimationFrame(() => {
    if (state.lastAnalysis !== analysis) return;
    redrawResearchCharts();
  });
}

function setStatus(text, kind = "") {
  const el = $("dataStatus");
  el.textContent = text;
  el.className = `status-pill ${kind}`;
}

function setCoreStatus(phase, text, kind = "") {
  state.coreStatus = { phase, text, kind };
  renderCompositeStatus();
}

function setDataQualityStatus(phase, text = "", kind = "") {
  state.dataQualityStatus = { phase, text, kind };
  renderCompositeStatus();
}

function setMutationStatus(phase, text = "", kind = "") {
  state.mutationStatus = { phase, text, kind };
  renderCompositeStatus();
}

function setSseStatus(phase, text = "", kind = "", hasValidFrame = false) {
  state.sseStatus = { phase, text, kind, hasValidFrame };
  renderCompositeStatus();
}

function renderCompositeStatus() {
  const status = compositeStatus();
  setStatus(status.text, status.kind);
}

function compositeStatus() {
  const core = state.coreStatus || {};
  const quality = state.dataQualityStatus || {};
  const auxiliary = auxiliaryStatusValue();
  const mutation = state.mutationStatus || {};
  const stream = state.sseStatus || {};
  if (core.phase === "loading" || core.phase === "error") return statusValue(core, "数据连接中");
  const degradations = [
    quality.phase === "degraded" ? quality.text : "",
    auxiliary.text,
    ["error", "degraded"].includes(mutation.phase) ? mutation.text : "",
    ["error", "invalid", "reconnecting"].includes(stream.phase) ? stream.text : "",
  ].filter(Boolean);
  if (degradations.length) return { text: degradations.join("；"), kind: "warn" };
  if (stream.phase === "ready" && stream.hasValidFrame) {
    return statusValue(stream, "核心分析快照已加载；观察报价流已收到有效帧");
  }
  if (core.phase === "ready") {
    return {
      text: stream.text || "核心分析快照已加载；观察报价流待连接",
      kind: stream.kind || "",
    };
  }
  return statusValue(stream, core.text || "数据连接中");
}

function statusValue(status, fallback) {
  return { text: status.text || fallback, kind: status.kind || "" };
}

function auxiliaryStatusValue() {
  const failures = Object.values((state.auxiliaryStatus && state.auxiliaryStatus.failures) || {});
  return {
    text: failures.length ? `部分辅助数据降级：${failures.map((item) => item.text).join("；")}` : "",
    kind: failures.length ? "warn" : "",
  };
}

function setAuxiliaryFailure(source, label, error) {
  if (isAbortError(error)) return;
  const detail = compactErrorMessage(error && error.message ? error.message : String(error || ""));
  const text = !detail || detail === label || detail.startsWith(label) ? detail || label : `${label}：${detail}`;
  const failures = { ...((state.auxiliaryStatus && state.auxiliaryStatus.failures) || {}) };
  failures[source] = { label, text };
  state.auxiliaryStatus = { failures };
  renderCompositeStatus();
}

function clearAuxiliaryFailure(source) {
  const current = (state.auxiliaryStatus && state.auxiliaryStatus.failures) || {};
  if (!Object.prototype.hasOwnProperty.call(current, source)) return;
  const failures = { ...current };
  delete failures[source];
  state.auxiliaryStatus = { failures };
  renderCompositeStatus();
}

function loadingState(title, detail = "正在读取数据，请稍候。") {
  return `<div class="minute-state loading"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

async function loadAll(options = {}) {
  const request = beginLoadRequest(options);
  const workbenchLoad = loadCurrentWorkbench(request);
  const globalLoads = refreshGlobalPanels();
  const workbench = await workbenchLoad;
  if (!workbench || isStaleLoad(request)) return false;
  if (!renderCurrentWorkbench(workbench, request)) return false;
  const stockPanels = refreshStockPanels(request);
  await globalLoads.watchlist;
  if (options.waitForAdviceTimeline) await stockPanels.adviceTimeline;
  if (isStaleLoad(request)) return false;
  reconcileStreamSubscription({ context: loadContextFromRequest(request) });
  return true;
}

async function loadCurrentWorkbench(request) {
  try {
    return await loadWorkbench(request.symbol, request.signal);
  } catch (error) {
    if (isAbortError(error)) return null;
    if (!isStaleLoad(request)) markLoadFailure(error, request.symbol, request.previousSymbol, request.reveal);
    return null;
  }
}

function renderCurrentWorkbench(workbench, request) {
  try {
    renderWorkbench(workbench);
    if (state.pendingLoad === request) state.pendingLoad = null;
    if (request.reveal) revealMobileFeedback($("stockCode"));
    return true;
  } catch (error) {
    if (state.pendingLoad === request) state.pendingLoad = null;
    if (!isStaleLoad(request)) markRenderFailure(error, request.symbol, request.reveal);
    return false;
  }
}

async function refreshWatchlist(options = {}) {
  try {
    const loaded = await loadWatchlist(state, {
      force: Boolean(options.force),
      onItemsChanged: handleWatchlistItemsChanged,
      ttlMs: GLOBAL_REFRESH_TTL_MS,
    });
    if (loaded) {
      clearAuxiliaryFailure("watchlist");
      state.visibilityRefreshSources.delete("watchlist");
    }
    return loaded;
  } catch (error) {
    if (isAbortError(error)) return false;
    markCompanionFailure(error, "watchlist", "自选股读取暂不可用");
    state.visibilityRefreshSources.add("watchlist");
    return false;
  }
}

function refreshGlobalPanels(options = {}) {
  const refreshOptions = { force: Boolean(options.force) };
  return {
    dataStatus: refreshDataStatus(refreshOptions),
    market: loadMarketPanels(refreshOptions),
    monitoring: refreshMonitoring(refreshOptions),
    plates: loadPlateRank(refreshOptions),
    watchlist: refreshWatchlist(refreshOptions),
    diagnostics: loadSystemDiagnostics(state, refreshOptions),
  };
}

function beginLoadRequest(options = {}) {
  cancelMinuteRequest();
  if (state.loadRequest) state.loadRequest.abort();
  const loadRequest = createRequestScope();
  state.loadRequest = loadRequest;
  stopStream();
  const request = {
    id: ++state.loadSeq,
    symbol: state.symbol,
    previousSymbol: displayedAnalysisSymbol(),
    previousAnalysis: state.lastAnalysis,
    previousCoreStatus: { ...(state.coreStatus || {}) },
    previousDataQualityStatus: { ...(state.dataQualityStatus || {}) },
    previousMutationStatus: { ...(state.mutationStatus || {}) },
    signal: loadRequest.signal,
    reveal: Boolean(options.reveal),
  };
  state.pendingLoad = request;
  state.coreStatus = { phase: "loading", text: "数据刷新中", kind: "warn" };
  state.dataQualityStatus = { phase: "unknown", text: "", kind: "" };
  state.mutationStatus = { phase: "idle", text: "", kind: "" };
  state.sseStatus = { phase: "idle", text: "", kind: "", hasValidFrame: false };
  renderCompositeStatus();
  if (!request.previousAnalysis) renderWorkbenchPending(request.symbol);
  return request;
}

function invalidateActiveLoad() {
  cancelMinuteRequest();
  if (state.loadRequest) {
    state.loadRequest.abort();
    state.loadRequest = null;
  }
  stopStream();
  state.pendingLoad = null;
  state.loadSeq += 1;
}

function isStaleLoad(request) {
  return request.id !== state.loadSeq || request.symbol !== state.symbol || Boolean(request.signal && request.signal.aborted);
}

function setActiveSymbol(symbol) {
  state.symbol = normalizeUiSymbol(symbol);
  stockSearchBindings.forEach((binding) => binding.close());
  syncSymbolInputs(state.symbol);
}

function syncSymbolInputs(symbol) {
  const code = normalizeUiSymbol(symbol).slice(0, 6);
  const symbolInput = $("symbolInput");
  const watchSymbolInput = $("watchSymbolInput");
  if (symbolInput) symbolInput.value = code;
  if (watchSymbolInput) watchSymbolInput.value = code;
}

function loadWorkbench(symbol, signal) {
  return fetchJson(`/api/stock/workbench?symbol=${encodeURIComponent(symbol)}`, {
    signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
}

function renderWorkbench(workbench) {
  const analysis = plainObject(workbench.analysis);
  syncResearchActivityWorkbench(workbench, analysis);
  syncWorkbenchChartMarks(workbench.chart_marks);
  clearAuxiliaryFailure("chart-marks");
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
  state.coreStatus = { phase: "ready", text: "核心数据已加载", kind: "" };
  state.dataQualityStatus = workbenchDataQualityStatus(analysis, localWarnings);
  renderCompositeStatus();
}

function workbenchDataQualityStatus(analysis, localWarnings) {
  if (localWarnings.length) {
    return { phase: "degraded", text: "核心数据已加载，本地数据部分降级", kind: "warn" };
  }
  const quality = plainObject(analysis.data_quality);
  const score = Number(quality.score);
  const level = typeof quality.level === "string" ? quality.level.trim() : "";
  if (["一般", "较弱"].includes(level) || (Number.isFinite(score) && score < 70)) {
    return { phase: "degraded", text: `核心数据已加载，数据质量${level || "偏弱"}`, kind: "warn" };
  }
  return { phase: "ready", text: "", kind: "" };
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

function renderWorkbenchCancelled(symbol) {
  const requested = normalizeUiSymbol(symbol);
  document.body.classList.remove("is-stale");
  resetWorkbenchState();
  resetWorkbenchHeader(requested, "未加载", `${requested} 的数据加载已取消。`);
  renderWorkbenchPlaceholder("数据未加载", "本次加载已取消。", { loading: false });
  const sourceLine = $("sourceLine");
  if (sourceLine) sourceLine.textContent = `本次 ${requested} 数据加载已取消。`;
}

function renderWorkbenchFailure(symbol, message) {
  const requested = normalizeUiSymbol(symbol);
  resetWorkbenchState();
  resetWorkbenchHeader(requested, "加载失败", `${requested} 未能加载：${message}`);
  renderWorkbenchPlaceholder(`${requested} 未加载成功`, "当前工作台没有切换到这只股票，请稍后重试或检查数据源状态。");
}

function resetWorkbenchState() {
  cancelMinuteRequest();
  state.lastAnalysis = null;
  state.lastInsights = null;
  state.lastMinuteReport = null;
  state.lastMinuteSymbol = "";
  state.dailyChartInspection = null;
  state.minuteChartInspection = null;
  state.chartMarks = [];
  state.activeMarkCategories.clear();
  state.adviceReviewDetails = [];
  state.adviceReviewSnapshots = [];
  state.adviceReviewEditingPlanId = null;
  resetResearchActivityState();
  clearKlineCanvas();
  clearMinuteKlineCanvas();
  setMinuteChartStatus(`${minuteIntervalLabel(state.minuteChartInterval)} · 等待数据`, "idle");
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

function renderWorkbenchPlaceholder(title, detail, { loading = true } = {}) {
  const html = loading
    ? loadingState(title, detail)
    : `<div class="minute-state"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
  WORKBENCH_PANEL_IDS.forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = html;
  });
}

function clearKlineCanvas() {
  setChartInspectionSnapshot("daily", null);
  clearChartCanvas("klineCanvas");
}

function clearMinuteKlineCanvas() {
  setChartInspectionSnapshot("minute", null);
  clearChartCanvas("minuteKlineCanvas");
}

function clearChartCanvas(id) {
  const canvas = $(id);
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

function syncResearchActivityWorkbench(workbench, analysis) {
  const quote = plainObject(analysis.quote);
  const symbol = normalizeUiSymbol(`${quote.code || ""}.${String(quote.market || "").toUpperCase()}`);
  const warnings = asLocalDataWarnings(workbench.local_data_warnings);
  const alertWarning = warnings.find((item) => item.component === "alert_events");
  const noteWarning = warnings.find((item) => item.component === "notes");
  state.researchActivitySymbol = symbol;
  state.researchActivityAdvice = [];
  state.researchActivityAlerts = Array.isArray(workbench.alert_events) ? [...workbench.alert_events] : [];
  state.researchActivityNotes = Array.isArray(workbench.notes) ? [...workbench.notes] : [];
  state.researchActivityAdviceSource = { symbol, phase: "loading", message: "正在读取建议记录" };
  state.researchActivityAlertSource = alertWarning
    ? { symbol, phase: "unavailable", message: alertWarning.message }
    : { symbol, phase: "ready", message: "" };
  state.researchActivityNoteSource = noteWarning
    ? { symbol, phase: "unavailable", message: noteWarning.message }
    : { symbol, phase: "ready", message: "" };
  renderResearchActivityPanel();
}

function resetResearchActivityState() {
  state.researchActivitySymbol = "";
  state.researchActivityAdvice = [];
  state.researchActivityAlerts = [];
  state.researchActivityNotes = [];
  state.researchActivityAdviceSource = { phase: "loading", message: "等待建议记录" };
  state.researchActivityAlertSource = { phase: "loading", message: "等待提醒记录" };
  state.researchActivityNoteSource = { phase: "loading", message: "等待笔记记录" };
}

function setResearchActivityAdvice(symbol, phase, items = [], message = "") {
  const normalized = normalizeUiSymbol(symbol);
  if (!normalized || normalized !== state.symbol || normalized !== state.researchActivitySymbol) return false;
  state.researchActivityAdvice = phase === "ready" && Array.isArray(items) ? [...items] : [];
  state.researchActivityAdviceSource = { symbol: normalized, phase, message };
  renderResearchActivityPanel();
  return true;
}

function renderResearchActivityPanel() {
  const target = $("researchActivity");
  if (!target) return false;
  const merged = mergeResearchActivity({
    adviceItems: Array.isArray(state.researchActivityAdvice) ? state.researchActivityAdvice : [],
    alertEvents: Array.isArray(state.researchActivityAlerts) ? state.researchActivityAlerts : [],
    notes: Array.isArray(state.researchActivityNotes) ? state.researchActivityNotes : [],
    limit: 100,
  });
  const activityItems = state.researchActivityFilter === "all"
    ? merged.items.slice(0, 12)
    : merged.items.filter((item) => item.kind === state.researchActivityFilter).slice(0, 20);
  renderResearchActivity({
    ...merged,
    items: activityItems,
    activeKind: state.researchActivityFilter,
    sourceStates: {
      advice: activitySourceState(state.researchActivityAdviceSource),
      alert: activitySourceState(state.researchActivityAlertSource),
      note: activitySourceState(state.researchActivityNoteSource),
    },
  }, target);
  syncResearchActivityFilters();
  return true;
}

function activitySourceState(source) {
  const value = source && typeof source === "object" ? source : {};
  const owned = !value.symbol || normalizeUiSymbol(value.symbol) === state.researchActivitySymbol;
  if (!owned) return { phase: "loading", message: "等待当前股票记录" };
  return { phase: value.phase || "loading", message: value.message || "" };
}

function syncResearchActivityFilters() {
  document.querySelectorAll("button[data-activity-filter]").forEach((button) => {
    const active = button.dataset.activityFilter === state.researchActivityFilter;
    button.classList.toggle("active", active);
    setElementAttribute(button, "aria-pressed", String(active));
  });
}

function syncWorkbenchChartMarks(chartMarks) {
  state.chartMarks = chartMarks ? chartMarks.marks || [] : [];
  syncMarkCategories(chartMarks ? chartMarks.categories || [] : []);
  renderMarkFilters();
}

function refreshStockPanels(request) {
  const context = loadContextFromRequest(request);
  return {
    minute: loadMinuteAnalysis(context),
    adviceTimeline: loadAdviceTimeline(context),
    adviceReviews: loadAdviceReviews(state, context),
  };
}

async function refreshDataStatus(options = {}) {
  const pending = loadDataStatus(state, {
    force: Boolean(options.force),
    ttlMs: GLOBAL_REFRESH_TTL_MS,
  });
  const requestId = state.dataStatusSeq;
  const loaded = await pending;
  if (requestId !== state.dataStatusSeq) return false;
  if (document.hidden) {
    state.visibilityRefreshSources.add("data-status");
    return false;
  }
  if (loaded) {
    clearAuxiliaryFailure("data-status");
    state.visibilityRefreshSources.delete("data-status");
    return true;
  }
  markCompanionFailure(new Error("数据源状态读取失败"), "data-status", "数据源状态暂不可用");
  state.visibilityRefreshSources.add("data-status");
  return false;
}

async function refreshMonitoring(options = {}) {
  const pending = loadMonitoring(state, {
    force: Boolean(options.force),
    ttlMs: GLOBAL_REFRESH_TTL_MS,
  });
  const requestId = state.monitorSeq;
  const loaded = await pending;
  if (requestId !== state.monitorSeq) return false;
  if (document.hidden) {
    state.visibilityRefreshSources.add("monitoring");
    return false;
  }
  if (loaded && !monitoringPanelsFailed()) {
    clearAuxiliaryFailure("monitoring");
    state.visibilityRefreshSources.delete("monitoring");
    return true;
  }
  markCompanionFailure(new Error("本地监控状态读取失败"), "monitoring", "本地监控暂不可用");
  state.visibilityRefreshSources.add("monitoring");
  return false;
}

function monitoringPanelsFailed() {
  return (
    $("schedulerState").textContent === "读取失败" ||
    $("taskCards").innerHTML.includes("监控暂不可用") ||
    $("monitorEvents").innerHTML.includes("事件读取失败")
  );
}

async function loadMarketPanels(options = {}) {
  const requestId = Number(state.marketPanelsSeq || 0) + 1;
  state.marketPanelsSeq = requestId;
  const [marketResult, strongResult] = await fetchMarketPanelResults(options);
  if (requestId !== state.marketPanelsSeq) return false;
  syncAuxiliaryResult("market", "市场概览暂不可用", marketResult);
  syncAuxiliaryResult("strong-stocks", "强股排行暂不可用", strongResult);
  if (marketResult.status === "fulfilled" && strongResult.status === "fulfilled") {
    state.visibilityRefreshSources.delete("market-panels");
  } else {
    state.visibilityRefreshSources.add("market-panels");
  }
  const panels = marketPanelData(marketResult, strongResult);
  try {
    renderMarket(panels.indices, panels.marketMeta);
    renderStrongStocks(panels.strongStocks, panels.strongMeta);
    clearAuxiliaryFailure("market-panels");
  } catch (error) {
    if (requestId !== state.marketPanelsSeq) return false;
    renderMarket([], { degraded: true, warnings: [compactErrorMessage(error.message)] });
    $("leaderList").innerHTML = `<div class="leader-row"><strong>观察池排序暂不可用</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span></div>`;
    markCompanionFailure(error, "market-panels", "市场辅助面板显示异常");
    return false;
  }
  return marketResult.status === "fulfilled" && strongResult.status === "fulfilled";
}

function fetchMarketPanelResults(options = {}) {
  const requestOptions = {
    force: Boolean(options && options.force),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ttlMs: GLOBAL_REFRESH_TTL_MS,
  };
  return Promise.allSettled([
    fetchCachedJson(GLOBAL_ENDPOINTS[0], requestOptions),
    fetchCachedJson(GLOBAL_ENDPOINTS[1], requestOptions),
  ]);
}

function marketPanelData(marketResult, strongResult) {
  const market = fulfilledValue(marketResult, GLOBAL_ENDPOINTS[0]);
  const strong = fulfilledValue(strongResult, GLOBAL_ENDPOINTS[1]);
  const marketError = rejectedReason(marketResult, "市场概览接口暂不可用");
  const strongError = rejectedReason(strongResult, "强股接口暂不可用");
  return {
    indices: marketIndices(market),
    marketMeta: marketSampleMeta(market, marketError),
    strongStocks: strongStockItems(market, strong),
    strongMeta: strongStockMeta(market, strong, strongError),
  };
}

function fulfilledValue(result, url) {
  if (result.status === "fulfilled") return result.value;
  const cached = getCachedJsonSnapshot(url);
  return cached.found ? cached.value : null;
}

function rejectedReason(result, fallbackMessage = "接口暂不可用") {
  if (result.status !== "rejected") return "";
  const reason = result.reason;
  return compactErrorMessage(reason && reason.message ? reason.message : String(reason || fallbackMessage));
}

function syncAuxiliaryResult(source, label, result) {
  if (result.status === "fulfilled") {
    clearAuxiliaryFailure(source);
    return;
  }
  if (isAbortError(result.reason)) {
    return;
  }
  setAuxiliaryFailure(source, label, result.reason || new Error(label));
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

function markLoadFailure(error, requestedSymbol = state.symbol, previousSymbol = "", reveal = false) {
  const message = compactErrorMessage(error.message);
  const requested = normalizeUiSymbol(requestedSymbol);
  const displayed = previousSymbol || displayedAnalysisSymbol();
  stopStream();
  state.pendingLoad = null;
  renderWorkbenchFailure(requested, message);
  document.body.classList.add("is-stale");
  state.dataQualityStatus = { phase: "unknown", text: "", kind: "" };
  setCoreStatus("error", `${requested} 加载失败`, "warn");
  const sourceLine = $("sourceLine");
  if (sourceLine) {
    sourceLine.textContent = displayed
      ? `本次请求 ${requested} 失败：${message}；已隔离 ${displayed} 的上次成功数据。`
      : `本次请求 ${requested} 失败：${message}`;
  }
  if (reveal) revealMobileFeedback($("stockCode"));
}

function markRenderFailure(error, requestedSymbol = state.symbol, reveal = false) {
  const message = compactErrorMessage(error.message);
  const requested = normalizeUiSymbol(requestedSymbol);
  stopStream();
  state.pendingLoad = null;
  document.body.classList.add("is-stale");
  setCoreStatus("error", `${requested} 页面显示异常`, "warn");
  const sourceLine = $("sourceLine");
  if (sourceLine) {
    sourceLine.textContent = `本次请求 ${requested} 数据已返回，但页面渲染异常：${message}`;
  }
  if (reveal) revealMobileFeedback($("stockCode"));
}

function markCompanionFailure(error, source = "companion", label = "辅助数据暂不可用") {
  setAuxiliaryFailure(source, label, error);
}

async function loadAdviceTimeline(request = currentLoadContext()) {
  const requestedSymbol = request.symbol;
  const requestedLoadSeq = request.loadSeq;
  if (request.signal?.aborted || requestedSymbol !== state.symbol || requestedLoadSeq !== state.loadSeq) return false;
  state.adviceTimelineWatermark = null;
  renderAdviceTimelineLoading(requestedSymbol);
  setResearchActivityAdvice(requestedSymbol, "loading", [], "正在读取建议记录");
  try {
    const items = await fetchJson(`/api/advice/timeline?symbol=${encodeURIComponent(requestedSymbol)}&limit=8`, {
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (requestedSymbol !== state.symbol || requestedLoadSeq !== state.loadSeq) return false;
    if (renderAdviceTimeline(items)) {
      const adviceId = latestAdviceTimelineId(items);
      state.adviceTimelineWatermark = adviceId === null
        ? null
        : { symbol: requestedSymbol, loadSeq: requestedLoadSeq, adviceId };
      syncAdviceReviewSnapshots(state, items, state.lastAnalysis);
      setResearchActivityAdvice(requestedSymbol, "ready", items);
      clearAuxiliaryFailure("advice-timeline");
      return true;
    } else {
      setResearchActivityAdvice(requestedSymbol, "unavailable", [], "建议记录格式异常");
      markCompanionFailure(new Error("建议变化响应格式异常"), "advice-timeline", "建议变化时间线暂不可用");
      return false;
    }
  } catch (error) {
    if (isAbortError(error)) return false;
    if (requestedSymbol !== state.symbol || requestedLoadSeq !== state.loadSeq) return false;
    renderAdviceTimelineUnavailable(error);
    setResearchActivityAdvice(requestedSymbol, "unavailable", [], compactErrorMessage(error.message));
    markCompanionFailure(error, "advice-timeline", "建议变化时间线暂不可用");
    return false;
  }
}

function latestAdviceTimelineId(items) {
  const ids = (Array.isArray(items) ? items : [])
    .map((item) => item && item.id)
    .filter((id) => Number.isInteger(id) && id > 0);
  return ids.length ? Math.max(...ids) : null;
}

async function loadMinuteAnalysis(request = currentLoadContext(), interval = state.minuteChartInterval) {
  const el = $("minuteAnalysis");
  if (!el) return false;
  const requestedInterval = normalizeMinuteInterval(interval);
  const requestedSymbol = request.symbol;
  const minuteRequest = beginMinuteAnalysisRequest(request, requestedInterval);
  state.lastMinuteReport = null;
  state.lastMinuteSymbol = "";
  el.innerHTML = loadingState("分钟分析加载中", `正在读取${minuteIntervalLabel(requestedInterval)}K线，不影响主分析。`);
  clearMinuteKlineCanvas();
  setMinuteChartStatus(`${minuteIntervalLabel(requestedInterval)} · 加载中`, "loading");
  setElementAttribute($("minuteChartPane"), "aria-busy", "true");
  try {
    const report = await fetchJson(`/api/stock/minute-analysis?symbol=${encodeURIComponent(requestedSymbol)}&interval=${encodeURIComponent(requestedInterval)}&limit=120`, {
      signal: minuteRequest.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (isStaleMinuteRequest(minuteRequest)) return false;
    validateMinuteReport(report, requestedSymbol, requestedInterval);
    state.lastMinuteReport = report;
    state.lastMinuteSymbol = normalizeUiSymbol(requestedSymbol);
    renderMinuteAnalysis(report);
    drawMinuteKline(report);
    clearAuxiliaryFailure("minute-analysis");
    return true;
  } catch (error) {
    if (isAbortError(error) || isStaleMinuteRequest(minuteRequest)) return false;
    state.lastMinuteReport = null;
    state.lastMinuteSymbol = "";
    clearMinuteKlineCanvas();
    setMinuteChartStatus(`${minuteIntervalLabel(requestedInterval)} · 不可用`, "unavailable");
    el.innerHTML = `<div class="minute-empty"><strong>分钟分析暂不可用</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span><span>当前不按分钟区间做T，主分析和日线策略仍可参考。</span></div>`;
    markCompanionFailure(error, "minute-analysis", "分钟分析暂不可用");
    return false;
  } finally {
    minuteRequest.scope.dispose();
    if (state.minuteRequest === minuteRequest.scope) state.minuteRequest = null;
    if (!isStaleMinuteRequest(minuteRequest)) setElementAttribute($("minuteChartPane"), "aria-busy", "false");
  }
}

function beginMinuteAnalysisRequest(request, interval) {
  cancelMinuteRequest();
  const scope = createRequestScope(null, request.signal);
  state.minuteRequest = scope;
  return {
    id: ++state.minuteRequestSeq,
    symbol: request.symbol,
    loadSeq: request.loadSeq ?? request.id,
    interval,
    scope,
    signal: scope.signal,
  };
}

function cancelMinuteRequest() {
  state.minuteRequestSeq += 1;
  if (!state.minuteRequest) return;
  state.minuteRequest.abort();
  state.minuteRequest = null;
}

function isStaleMinuteRequest(request) {
  return (
    request.id !== state.minuteRequestSeq
    || request.symbol !== state.symbol
    || request.loadSeq !== state.loadSeq
    || request.interval !== state.minuteChartInterval
    || Boolean(request.signal && request.signal.aborted)
  );
}

async function loadChartMarks(request = currentLoadContext()) {
  const requestedSymbol = request.symbol;
  try {
    const summary = await fetchJson(`/api/stock/chart-marks?symbol=${encodeURIComponent(requestedSymbol)}&limit=40`, {
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (isStaleContext(request)) return;
    state.chartMarks = summary.marks || [];
    syncMarkCategories(summary.categories || []);
    renderMarkFilters();
    if (state.lastAnalysis) {
      drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
    }
    clearAuxiliaryFailure("chart-marks");
  } catch (error) {
    if (isAbortError(error)) return;
    if (isStaleContext(request)) return;
    markCompanionFailure(error, "chart-marks", "图表标注暂不可用");
  }
}

function currentLoadContext() {
  return {
    symbol: state.symbol,
    loadSeq: state.loadSeq,
    signal: state.loadRequest ? state.loadRequest.signal : undefined,
  };
}

function loadContextFromRequest(request) {
  return {
    symbol: request.symbol,
    loadSeq: request.loadSeq ?? request.id,
    signal: request.signal,
  };
}

function isStaleContext(request) {
  const loadSeq = request.loadSeq ?? request.id;
  return request.symbol !== state.symbol || loadSeq !== state.loadSeq || Boolean(request.signal && request.signal.aborted);
}

function currentWorkbenchMutationOptions() {
  const displayed = displayedWorkbenchContext();
  if (!displayed) return null;
  const loadContext = currentLoadContext();
  const context = { ...loadContext, symbol: displayed.symbol };
  return {
    symbol: displayed.symbol,
    context,
    signal: loadContext.signal,
    isCurrent: () => {
      const current = displayedWorkbenchContext();
      return (
        Boolean(current) &&
        current.symbol === displayed.symbol &&
        current.analysis === displayed.analysis &&
        !(loadContext.signal && loadContext.signal.aborted)
      );
    },
  };
}

function displayedWorkbenchContext() {
  const request = state.pendingLoad;
  if (request) {
    if (!request.previousAnalysis || !request.previousSymbol) return null;
    return { symbol: request.previousSymbol, analysis: request.previousAnalysis };
  }
  return {
    symbol: displayedAnalysisSymbol() || state.symbol,
    analysis: state.lastAnalysis,
  };
}

function currentWatchlistMutationOptions(actionLabel) {
  const context = { symbol: state.symbol, loadSeq: state.loadSeq };
  const isCurrent = () => context.symbol === state.symbol && context.loadSeq === state.loadSeq;
  return {
    context,
    isCurrent,
    symbol: context.symbol,
    onItemsChanged: handleWatchlistItemsChanged,
    onMutationSuccess() {
      if (!isCurrent()) return;
      clearWatchlistFeedback();
      setMutationStatus("idle");
    },
    onReadbackError(error) {
      if (!isCurrent()) return;
      setWatchlistFeedback(`已${actionLabel}，列表同步降级：${compactErrorMessage(error.message)}`, "warn");
      setMutationStatus(
        "degraded",
        `自选股已${actionLabel}，列表同步降级：${compactErrorMessage(error.message)}`,
        "warn"
      );
    },
  };
}

function setWatchlistFeedback(message, kind = "") {
  const feedback = $("watchlistFeedback");
  if (!feedback) return;
  feedback.textContent = message || "";
  feedback.className = `watchlist-feedback${kind ? ` ${kind}` : ""}`;
  feedback.hidden = !message;
}

function clearWatchlistFeedback() {
  setWatchlistFeedback("");
}

function setWatchlistEditError(form, error) {
  const feedback = form && typeof form.querySelector === "function" ? form.querySelector(".watch-edit-feedback") : null;
  if (!feedback) return;
  feedback.textContent = `保存失败：${compactErrorMessage(error.message)}`;
  feedback.hidden = false;
}

function setInlineEditError(form, error) {
  const feedback = form?.querySelector?.(".inline-edit-feedback");
  if (!feedback) return;
  feedback.textContent = `保存失败：${compactErrorMessage(error.message)}`;
  feedback.hidden = false;
}

function setInlineFeedback(id, error) {
  const feedback = $(id);
  if (!feedback) return;
  feedback.textContent = compactErrorMessage(error.message);
  feedback.dataset.tone = "error";
  feedback.hidden = false;
}

function setLocalDataRefreshWarning() {
  const feedback = $("localDataFeedback");
  if (!feedback) return;
  feedback.textContent = "用户数据已导入，但当前页面同步失败，请重新加载当前股票。";
  feedback.dataset.tone = "warn";
  feedback.hidden = false;
}

async function commitLocalDataAndRefresh() {
  const result = await commitLocalDataImport(state);
  if (!result) return false;
  invalidateWatchlistCache();
  const refresh = loadAll({ reveal: true });
  const refreshLoadSeq = state.loadSeq;
  const loaded = await refresh;
  if (!loaded && state.loadSeq === refreshLoadSeq) setLocalDataRefreshWarning();
  return result;
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
          return `<button type="button" class="${active ? "active" : ""}" data-mark-category="${escapeHtml(category)}" aria-pressed="${active}">${escapeHtml(category)}</button>`;
        })
        .join("")
    : `<span>暂无图表标注</span>`;
}

async function runButtonTask(button, task, options = {}) {
  if (button.disabled) return false;
  const previousText = button.textContent;
  const canUseBusyText = button.classList.contains("mini-button");
  try {
    button.disabled = true;
    if (canUseBusyText) button.textContent = "处理中";
    const result = await task();
    return result !== false;
  } catch (error) {
    if (isAbortError(error) || (options.isCurrent && !options.isCurrent())) return false;
    if (options.onError) options.onError(error);
    else markCompanionFailure(error);
    return false;
  } finally {
    button.disabled = false;
    if (canUseBusyText) button.textContent = previousText;
  }
}

function clearRowActionError(button) {
  const row = button?.closest?.(".alert-row") || button?.closest?.(".note-row");
  const feedback = row?.querySelector?.(".row-action-feedback");
  if (!feedback) return;
  feedback.textContent = "";
  feedback.hidden = true;
}

function showRowActionError(button, error) {
  const row = button?.closest?.(".alert-row") || button?.closest?.(".note-row");
  const feedback = row?.querySelector?.(".row-action-feedback");
  if (!feedback) {
    markCompanionFailure(error);
    return;
  }
  feedback.textContent = `操作失败：${compactErrorMessage(error.message)}`;
  feedback.hidden = false;
  revealMobileFeedback(row);
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

async function loadPlateRank(options = {}) {
  const requestId = Number(state.plateSeq || 0) + 1;
  state.plateSeq = requestId;
  try {
    const plates = await fetchCachedJson(GLOBAL_ENDPOINTS[7], {
      force: Boolean(options && options.force),
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      ttlMs: GLOBAL_REFRESH_TTL_MS,
    });
    if (requestId !== state.plateSeq) return false;
    renderPlates(plates);
    clearAuxiliaryFailure("plates");
    state.visibilityRefreshSources.delete("plates");
    return true;
  } catch (error) {
    if (isAbortError(error) || requestId !== state.plateSeq) return false;
    const cached = getCachedJsonSnapshot(GLOBAL_ENDPOINTS[7]);
    if (cached.found) renderPlates(cached.value);
    else {
      $("plateList").innerHTML = `<div class="leader-row"><strong>行业背景暂不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
    markCompanionFailure(error, "plates", "行业背景暂不可用");
    state.visibilityRefreshSources.add("plates");
    return false;
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

function handleWatchlistItemsChanged() {
  reconcileStreamSubscription();
}

function reconcileStreamSubscription({ context = currentLoadContext() } = {}) {
  if (document.hidden || state.pendingLoad || !state.lastAnalysis || isStaleContext(context)) return false;
  const subscriptionKey = streamSymbols().join(",");
  const streamContext = state.streamContext;
  if (
    state.stream &&
    state.streamSubscriptionKey === subscriptionKey &&
    streamContext &&
    streamContext.symbol === context.symbol &&
    streamContext.loadSeq === context.loadSeq
  ) {
    return false;
  }
  return startStream({ context });
}

function startStream({ retry = false, context = currentLoadContext() } = {}) {
  if (document.hidden || isStaleContext(context)) return false;
  clearStreamRetryTimer();
  if (!retry) state.streamRetryCount = 0;
  const streamId = ++state.streamSeq;
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  state.streamSubscriptionKey = "";
  state.streamContext = { symbol: context.symbol, loadSeq: context.loadSeq, signal: context.signal };
  setSseStatus("connecting", "观察报价流连接中", "", false);
  const symbols = streamSymbols();
  if (!symbols.length) {
    state.streamContext = null;
    setSseStatus("idle", "核心分析快照已加载；观察报价流未启动", "warn", false);
    return false;
  }
  let stream;
  try {
    stream = new EventSource(`/api/stream/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
  } catch (error) {
    state.streamContext = null;
    state.streamSubscriptionKey = "";
    const detail = compactErrorMessage(error.message || "创建失败");
    setSseStatus("error", `观察报价流创建失败：${detail}`, "warn", false);
    return false;
  }
  state.stream = stream;
  state.streamSubscriptionKey = symbols.join(",");
  stream.onmessage = (event) => {
    if (!isCurrentStream(stream, streamId, context)) return;
    const rows = quoteRowsFromStreamEvent(event);
    if (!rows) return;
    if (!rows.length) {
      setSseStatus("connecting", "观察报价流暂无有效数据，等待下一帧", "warn", false);
      return;
    }
    try {
      renderQuotes(rows);
    } catch (error) {
      setSseStatus("invalid", "观察报价流显示异常，已保留上一帧", "warn", false);
      return;
    }
    state.streamRetryCount = 0;
    setSseStatus("ready", "核心分析快照已加载；观察报价流已收到有效帧", "ok", true);
  };
  stream.addEventListener("quote-error", (event) => {
    if (isCurrentStream(stream, streamId, context)) handleStreamQuoteError(event);
  });
  stream.onerror = () => scheduleStreamReconnect(stream, streamId, context);
  return true;
}

function streamSymbols() {
  const watchlist = Array.isArray(state.watchlist) ? state.watchlist : [];
  const excludedSymbols = new Set(
    watchlist
      .filter(isExcludedWatchlistItem)
      .map((item) => canonicalStreamSymbol(item && item.symbol))
      .filter(Boolean)
  );
  const watchSymbols = watchlist.filter((item) => !isExcludedWatchlistItem(item)).map((item) => item && item.symbol);
  const activeSymbol = canonicalStreamSymbol(state.symbol);
  const observedSymbols = [...watchSymbols, "600519", "000001", "300750", "002594", "600036"]
    .map(canonicalStreamSymbol)
    .filter(Boolean)
    .filter((item) => !excludedSymbols.has(item))
    .filter((item) => item !== activeSymbol);
  return [activeSymbol, ...observedSymbols]
    .filter(Boolean)
    .filter((item, index, rows) => rows.indexOf(item) === index)
    .slice(0, 8);
}

function canonicalStreamSymbol(symbol) {
  const normalized = normalizeUiSymbol(symbol);
  return /^\d{6}\.(SH|SZ)$/.test(normalized) ? normalized : "";
}

function isCurrentStream(stream, streamId, context) {
  return streamId === state.streamSeq && stream === state.stream && !isStaleContext(context);
}

function quoteRowsFromStreamEvent(event) {
  let rows;
  try {
    rows = JSON.parse(event.data);
  } catch (error) {
    setSseStatus("invalid", "观察报价流数据异常，等待下一次刷新", "warn", false);
    return null;
  }
  if (!Array.isArray(rows)) {
    setSseStatus("invalid", "观察报价流数据格式异常，等待下一次刷新", "warn", false);
    return null;
  }
  if (!rows.every(isValidQuoteRow)) {
    setSseStatus("invalid", "观察报价流帧含无效数据，已保留上一帧", "warn", false);
    return null;
  }
  return rows;
}

function isValidQuoteRow(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) return false;
  return (
    typeof item.name === "string" &&
    Boolean(item.name.trim()) &&
    typeof item.code === "string" &&
    /^\d{6}$/.test(item.code) &&
    typeof item.market === "string" &&
    /^(SH|SZ)$/.test(item.market) &&
    isFiniteQuoteNumber(item.price) &&
    isFiniteQuoteNumber(item.change_pct) &&
    isFiniteQuoteNumber(item.amount)
  );
}

function isFiniteQuoteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function handleStreamQuoteError(event) {
  if (event && typeof event.data === "string") {
    try {
      const payload = JSON.parse(event.data);
      const detail = compactErrorMessage(payload.message || "暂不可用");
      setSseStatus("error", `观察报价流暂不可用：${detail}`, "warn", false);
      return;
    } catch (error) {
      setSseStatus("error", "观察报价流暂不可用", "warn", false);
      return;
    }
  }
  setSseStatus("error", "观察报价流暂不可用", "warn", false);
}

function scheduleStreamReconnect(stream, streamId, context) {
  if (!isCurrentStream(stream, streamId, context)) return;
  setSseStatus("reconnecting", "观察报价流连接波动，准备重连", "warn", false);
  if (state.streamRetryTimer || document.hidden) return;
  stopStream({ clearRetryTimer: false, preserveStatus: true });
  const delay = Math.min(30000, 2000 * 2 ** Math.min(state.streamRetryCount, 4));
  state.streamRetryCount += 1;
  const generation = ++state.streamRetryGeneration;
  state.streamRetryTimer = setTimeout(() => {
    if (generation !== state.streamRetryGeneration || isStaleContext(context)) return;
    state.streamRetryTimer = null;
    startStream({ retry: true, context });
  }, delay);
}

function stopStream({ clearRetryTimer = true, preserveStatus = false } = {}) {
  if (clearRetryTimer) clearStreamRetryTimer();
  if (state.stream || state.streamContext) state.streamSeq += 1;
  if (state.stream) state.stream.close();
  state.stream = null;
  state.streamContext = null;
  state.streamSubscriptionKey = "";
  if (!preserveStatus) setSseStatus("idle", "", "", false);
}

function clearStreamRetryTimer() {
  state.streamRetryGeneration += 1;
  if (!state.streamRetryTimer) return;
  clearTimeout(state.streamRetryTimer);
  state.streamRetryTimer = null;
}

function drawKline(rows, ma5, ma20) {
  const pane = $("dailyChartPane");
  if (pane && pane.hidden) {
    setChartInspectionSnapshot("daily", null);
    return { drawn: false, reason: "pane-hidden" };
  }
  const result = drawKlineChart({
    canvas: $("klineCanvas"),
    rows,
    ma5,
    ma20,
    marks: state.chartMarks,
    activeCategories: state.activeMarkCategories,
    formatNumber,
    rowLimit: state.dailyChartRange,
    showMa5: state.dailyChartMa5,
    showMa20: state.dailyChartMa20,
  });
  const suffix = result.drawn ? `${result.rowCount}根` : "暂无数据";
  setDailyChartStatus(`${state.dailyChartRange}日 · ${suffix}`, result.drawn ? "ready" : "empty");
  setChartInspectionSnapshot("daily", result.inspection);
  return result;
}

function drawMinuteKline(report = state.lastMinuteReport) {
  const interval = state.minuteChartInterval;
  const intervalLabel = minuteIntervalLabel(interval);
  if (!currentMinuteReportMatches(report, interval)) {
    const result = drawEmptyMinuteKline();
    setChartInspectionSnapshot("minute", result.inspection);
    if (state.minuteChartPhase !== "loading") setMinuteChartStatus(`${intervalLabel} · 等待数据`, "idle");
    return result;
  }

  const availability = minuteAvailabilityState(report);
  if (availability.status === "unavailable") {
    const result = drawEmptyMinuteKline();
    setChartInspectionSnapshot("minute", result.inspection);
    setMinuteChartStatus(`${intervalLabel} · 不可用`, "unavailable");
    return result;
  }

  const rows = Array.isArray(report.klines) ? report.klines : [];
  const pane = $("minuteChartPane");
  if (pane && pane.hidden) {
    const status = availability.status === "degraded" ? "degraded" : "ready";
    const qualifier = availability.status === "degraded" ? "降级 · " : "";
    setMinuteChartStatus(`${intervalLabel} · ${qualifier}${rows.length}根`, status);
    setChartInspectionSnapshot("minute", null);
    return { drawn: false, rowCount: rows.length, reason: "pane-hidden" };
  }
  const result = drawKlineChart({
    canvas: $("minuteKlineCanvas"),
    rows,
    rowLimit: MINUTE_CHART_ROW_LIMIT,
    showMarks: false,
    showMa5: false,
    showMa20: false,
    formatNumber,
  });
  if (!result.drawn) {
    setChartInspectionSnapshot("minute", result.inspection);
    setMinuteChartStatus(`${intervalLabel} · 数据不足`, "unavailable");
    return result;
  }
  const status = availability.status === "degraded" ? "degraded" : "ready";
  const qualifier = availability.status === "degraded" ? "降级 · " : "";
  setMinuteChartStatus(`${intervalLabel} · ${qualifier}${result.rowCount}根`, status);
  setChartInspectionSnapshot("minute", result.inspection);
  return result;
}

function currentMinuteReportMatches(report, interval) {
  const displayedSymbol = normalizeUiSymbol(displayedAnalysisSymbol() || state.symbol);
  return Boolean(
    report
    && state.lastMinuteSymbol
    && state.lastMinuteSymbol === displayedSymbol
    && minuteReportInterval(report) === interval
  );
}

function drawEmptyMinuteKline() {
  return drawKlineChart({
    canvas: $("minuteKlineCanvas"),
    rows: [],
    rowLimit: MINUTE_CHART_ROW_LIMIT,
    showMarks: false,
    showMa5: false,
    showMa20: false,
  });
}

function redrawResearchCharts() {
  if (state.lastAnalysis) {
    drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
  }
  drawMinuteKline();
}

function setChartInspectionSnapshot(kind, snapshot) {
  if (kind === "daily") state.dailyChartInspection = snapshot || null;
  else if (kind === "minute") state.minuteChartInspection = snapshot || null;
  else return false;
  renderChartInspection(kind, { phase: "idle" });
  return true;
}

function chartInspectionSnapshot(kind) {
  return kind === "daily" ? state.dailyChartInspection : state.minuteChartInspection;
}

function initializeChartInspectors() {
  initializeChartInspector("daily", "klineCanvas");
  initializeChartInspector("minute", "minuteKlineCanvas");
}

function initializeChartInspector(kind, canvasId) {
  const canvas = $(canvasId);
  if (
    !canvas
    || typeof canvas.addEventListener !== "function"
    || typeof canvas.removeEventListener !== "function"
    || typeof canvas.getBoundingClientRect !== "function"
  ) return null;
  return createChartInspector({
    canvas,
    getSnapshot: () => chartInspectionSnapshot(kind),
    onState: (inspection) => renderChartInspection(kind, inspection),
  });
}

function renderChartInspection(kind, inspection) {
  const prefix = kind === "daily" ? "daily" : "minute";
  const overlay = $(`${prefix}ChartInspector`);
  const values = $(`${prefix}ChartInspectorValues`);
  const snapshot = chartInspectionSnapshot(kind);
  const active = inspection && inspection.phase === "active" && snapshot && Number.isFinite(inspection.x) && Number.isFinite(inspection.y);
  if (!overlay || !values) return false;
  overlay.hidden = !active;
  setElementAttribute(overlay, "aria-hidden", String(!active));
  if (!active) {
    values.innerHTML = "";
    return false;
  }
  const vertical = overlay.querySelector(".chart-crosshair-x");
  const horizontal = overlay.querySelector(".chart-crosshair-y");
  if (vertical && vertical.style) {
    vertical.style.left = `${inspection.x}px`;
    vertical.style.top = `${snapshot.bounds.top}px`;
    vertical.style.height = `${snapshot.bounds.bottom - snapshot.bounds.top}px`;
  }
  if (horizontal && horizontal.style) {
    horizontal.style.left = `${snapshot.bounds.left}px`;
    horizontal.style.top = `${inspection.y}px`;
    horizontal.style.width = `${snapshot.bounds.right - snapshot.bounds.left}px`;
  }
  values.innerHTML = chartInspectionHtml(kind, inspection.item);
  return true;
}

function chartInspectionHtml(kind, item) {
  const open = Number(item.open);
  const close = Number(item.close);
  const changePct = Number.isFinite(open) && open !== 0 && Number.isFinite(close) ? (close - open) / open * 100 : null;
  const fields = [
    ["开", item.open],
    ["高", item.high],
    ["低", item.low],
    ["收", item.close],
    ["MA5", item.ma5],
    ["MA20", item.ma20],
  ].filter((entry) => entry[1] !== null && entry[1] !== undefined && Number.isFinite(Number(entry[1])));
  const source = [
    item.source,
    item.fromCache ? "缓存" : "",
    item.fallbackUsed ? "回退" : "",
    item.fetchedAt,
  ].filter(Boolean).join(" · ");
  const period = kind === "daily" ? "日线" : minuteIntervalLabel(state.minuteChartInterval);
  return `
    <div class="chart-inspector-heading">
      <strong>${escapeHtml(item.eventTime || "--")}</strong>
      <span>${escapeHtml(period)}${changePct === null ? "" : ` · ${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`}</span>
    </div>
    <div class="chart-inspector-grid">
      ${fields.map(([label, value]) => `<span><b>${escapeHtml(label)}</b> ${escapeHtml(formatNumber(value))}</span>`).join("")}
      <span><b>量</b> ${escapeHtml(formatChartVolume(item.volume))}</span>
    </div>
    ${source ? `<div class="chart-inspector-meta">${escapeHtml(source)}</div>` : ""}`;
}

function formatChartVolume(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(2)}万`;
  return formatNumber(number);
}

function setDailyChartStatus(text, tone = "") {
  const status = $("dailyChartStatus");
  if (!status) return;
  status.textContent = text;
  status.className = tone;
}

function setMinuteChartStatus(text, phase = "idle") {
  state.minuteChartPhase = phase;
  const status = $("minuteChartStatus");
  if (status) {
    status.textContent = text;
    status.className = phase;
  }
  const pane = $("minuteChartPane");
  if (pane) pane.dataset.availability = phase;
}

function minuteIntervalLabel(interval) {
  return {
    "5m": "5分钟",
    "15m": "15分钟",
    "30m": "30分钟",
    "60m": "60分钟",
  }[interval] || "5分钟";
}

function normalizeMinuteInterval(value) {
  const interval = String(value || "5m").trim().toLowerCase();
  return MINUTE_CHART_INTERVALS.includes(interval) ? interval : "5m";
}

function minuteReportInterval(report) {
  const interval = String(report && report.interval || "").trim().toLowerCase();
  return interval;
}

function validateMinuteReport(report, requestedSymbol, requestedInterval) {
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    throw new Error("分钟分析返回内容为空或格式异常");
  }
  if (minuteReportInterval(report) !== requestedInterval) {
    throw new Error("分钟分析返回的周期与当前选择不一致");
  }
  if (normalizeUiSymbol(report.symbol) !== normalizeUiSymbol(requestedSymbol)) {
    throw new Error("分钟分析返回的股票与当前选择不一致");
  }
}

function selectDailyChartRange(value) {
  const range = Number(value);
  if (!DAILY_CHART_RANGES.includes(range)) return false;
  if (state.dailyChartRange === range) {
    syncChartControls();
    persistWorkspacePreferences();
    return false;
  }
  state.dailyChartRange = range;
  syncChartControls();
  persistWorkspacePreferences();
  if (state.lastAnalysis) drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
  return true;
}

function setDailyChartOverlay(name, enabled) {
  if (name === "ma5") state.dailyChartMa5 = Boolean(enabled);
  else if (name === "ma20") state.dailyChartMa20 = Boolean(enabled);
  else return false;
  syncChartControls();
  persistWorkspacePreferences();
  if (state.lastAnalysis) drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
  return true;
}

async function selectMinuteChartInterval(value) {
  const interval = String(value || "").trim().toLowerCase();
  if (!MINUTE_CHART_INTERVALS.includes(interval)) return false;
  if (state.minuteChartInterval === interval) {
    syncChartControls();
    persistWorkspacePreferences();
    return false;
  }
  state.minuteChartInterval = interval;
  state.lastMinuteReport = null;
  state.lastMinuteSymbol = "";
  syncChartControls();
  persistWorkspacePreferences();
  clearMinuteKlineCanvas();
  setMinuteChartStatus(`${minuteIntervalLabel(interval)} · 等待数据`, "idle");
  if (!state.lastAnalysis || state.pendingLoad) return false;
  return loadMinuteAnalysis(currentLoadContext(), interval);
}

function setMobileChartView(value) {
  if (!WORKSPACE_PREFERENCE_OPTIONS.mobileChartView.includes(value)) return false;
  state.mobileChartView = value;
  syncChartWorkspaceLayout();
  persistWorkspacePreferences();
  if (!restoringWorkspacePreferences) requestAnimationFrame(redrawResearchCharts);
  return true;
}

function currentWorkspacePreferences() {
  return {
    workspaceView: state.workspaceView,
    dailyChartRange: state.dailyChartRange,
    dailyChartMa5: state.dailyChartMa5,
    dailyChartMa20: state.dailyChartMa20,
    minuteChartInterval: state.minuteChartInterval,
    mobileChartView: state.mobileChartView,
  };
}

function persistWorkspacePreferences() {
  if (restoringWorkspacePreferences) return false;
  return saveWorkspacePreferences(currentWorkspacePreferences());
}

function restoreWorkspacePreferences() {
  const preferences = loadWorkspacePreferences();
  restoringWorkspacePreferences = true;
  try {
    setWorkspaceView(preferences.workspaceView);
    selectDailyChartRange(preferences.dailyChartRange);
    setDailyChartOverlay("ma5", preferences.dailyChartMa5);
    setDailyChartOverlay("ma20", preferences.dailyChartMa20);
    void selectMinuteChartInterval(preferences.minuteChartInterval);
    setMobileChartView(preferences.mobileChartView);
  } finally {
    restoringWorkspacePreferences = false;
  }
  return currentWorkspacePreferences();
}

function syncChartControls() {
  document.querySelectorAll("button[data-daily-range]").forEach((button) => {
    const active = Number(button.dataset.dailyRange) === state.dailyChartRange;
    button.classList.toggle("active", active);
    setElementAttribute(button, "aria-pressed", String(active));
  });
  document.querySelectorAll("button[data-minute-interval]").forEach((button) => {
    const active = button.dataset.minuteInterval === state.minuteChartInterval;
    button.classList.toggle("active", active);
    setElementAttribute(button, "aria-pressed", String(active));
  });
  document.querySelectorAll("button[data-chart-view]").forEach((button) => {
    const active = button.dataset.chartView === state.mobileChartView;
    button.classList.toggle("active", active);
    setElementAttribute(button, "aria-pressed", String(active));
  });
  const ma5 = $("dailyMa5Toggle");
  const ma20 = $("dailyMa20Toggle");
  const minuteCanvas = $("minuteKlineCanvas");
  const minuteAnalysisPeriod = $("minuteAnalysisPeriod");
  if (ma5) ma5.checked = state.dailyChartMa5;
  if (ma20) ma20.checked = state.dailyChartMa20;
  if (minuteCanvas) {
    setElementAttribute(minuteCanvas, "aria-label", `${minuteIntervalLabel(state.minuteChartInterval)}分时K线走势图，可用左右方向键逐根查看`);
  }
  if (minuteAnalysisPeriod) {
    minuteAnalysisPeriod.textContent = `${minuteIntervalLabel(state.minuteChartInterval)}区间 / 盘中强弱`;
  }
}

function syncChartWorkspaceLayout() {
  const mobile = typeof globalThis.matchMedia === "function" && globalThis.matchMedia("(max-width: 820px)").matches;
  const workspace = $("chartWorkspace");
  const dailyPane = $("dailyChartPane");
  const minutePane = $("minuteChartPane");
  if (workspace) workspace.dataset.mobileChart = state.mobileChartView;
  if (dailyPane) dailyPane.hidden = mobile && state.mobileChartView !== "daily";
  if (minutePane) minutePane.hidden = mobile && state.mobileChartView !== "minute";
  if (dailyPane && dailyPane.hidden) setChartInspectionSnapshot("daily", null);
  if (minutePane && minutePane.hidden) setChartInspectionSnapshot("minute", null);
  syncChartControls();
}

function revealMobileFeedback(element) {
  if (!element || typeof globalThis.matchMedia !== "function") return;
  if (!globalThis.matchMedia("(max-width: 820px)").matches) return;
  const target =
    typeof element.closest === "function"
      ? element.closest(".watchlist-box, .main-card, .data-health, .data-monitor") || element.closest(".panel") || element
      : element;
  if (typeof target.scrollIntoView !== "function") return;
  requestAnimationFrame(() => target.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function setElementAttribute(element, name, value) {
  if (!element) return;
  if (typeof element.setAttribute === "function") {
    element.setAttribute(name, value);
    return;
  }
  if (name === "aria-invalid") element.ariaInvalid = value;
  if (name === "aria-selected") element.ariaSelected = value;
  if (name === "aria-expanded") element.ariaExpanded = value;
  if (name === "aria-activedescendant") element.ariaActiveDescendant = value;
}

function createStockSearchBinding({ inputId, listId, onSelect }) {
  const input = $(inputId);
  const list = $(listId);
  let view = { phase: "idle", query: "", items: [], activeIndex: -1, message: "" };
  const controller = createStockSearchController({
    onState(nextView) {
      view = nextView;
      renderStockSearchView(input, list, nextView);
    },
    onSelect(symbol, item) {
      if (input) input.value = item.code;
      onSelect(symbol, item);
    },
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      controller.move(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter" && view.phase === "ready" && view.items.length) {
      event.preventDefault();
      controller.selectIndex(view.activeIndex >= 0 ? view.activeIndex : 0);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      controller.close();
    }
  });
  input.addEventListener("blur", () => {
    setTimeout(() => controller.close(), 120);
  });
  list.addEventListener("pointerdown", (event) => event.preventDefault());
  list.addEventListener("click", (event) => {
    const option = event.target && typeof event.target.closest === "function"
      ? event.target.closest("button[data-stock-index]")
      : null;
    if (!option) return;
    controller.selectIndex(Number(option.dataset.stockIndex));
  });

  const binding = {
    input(value) {
      return controller.input(value);
    },
    close() {
      return controller.close();
    },
    selectDefault() {
      if (view.phase !== "ready" || !view.items.length) return null;
      return controller.selectIndex(view.activeIndex >= 0 ? view.activeIndex : 0);
    },
    validationMessage(fallback) {
      if (view.phase === "loading") return "正在搜索股票，请稍候。";
      if (view.phase === "empty") return "未找到匹配股票，请检查名称或输入6位代码。";
      if (view.phase === "unavailable") return "股票搜索暂不可用，请输入6位代码。";
      return fallback;
    },
  };
  stockSearchBindings.push(binding);
  return binding;
}

function renderStockSearchView(input, list, view) {
  if (!input || !list) return;
  const open = ["loading", "ready", "empty", "unavailable"].includes(view.phase);
  list.hidden = !open;
  setElementAttribute(input, "aria-expanded", String(open));
  if (!open) {
    list.innerHTML = "";
    setElementAttribute(input, "aria-activedescendant", "");
    return;
  }
  if (view.phase === "ready") {
    list.innerHTML = view.items
      .map((item, index) => stockSuggestionHtml(item, index, list.id, index === view.activeIndex))
      .join("");
    const activeId = view.activeIndex >= 0 ? `${list.id}-option-${view.activeIndex}` : "";
    setElementAttribute(input, "aria-activedescendant", activeId);
    return;
  }
  setElementAttribute(input, "aria-activedescendant", "");
  const states = {
    loading: ["正在搜索股票...", ""],
    empty: ["未找到匹配股票", ""],
    unavailable: ["股票搜索暂不可用，请输入6位代码。", "is-unavailable"],
  };
  const [message, className] = states[view.phase] || ["", ""];
  list.innerHTML = `<div class="stock-suggestion-state ${className}" role="option" aria-disabled="true">${escapeHtml(message)}</div>`;
}

function stockSuggestionHtml(item, index, listId, active) {
  const detail = [item.industry, item.source]
    .filter((value) => typeof value === "string" && value.trim())
    .join(" · ");
  return `
    <button type="button" class="stock-suggestion${active ? " is-active" : ""}" id="${escapeHtml(listId)}-option-${index}" role="option" aria-selected="${active ? "true" : "false"}" data-stock-index="${index}">
      <strong>${escapeHtml(item.name)}</strong>
      <span>${escapeHtml(item.code)}.${escapeHtml(item.market)}</span>
      <small>${escapeHtml(detail || "行业信息暂缺")}</small>
    </button>`;
}

function clearSymbolError() {
  const input = $("symbolInput");
  const error = $("symbolError");
  setElementAttribute(input, "aria-invalid", "false");
  if (!error) return;
  error.textContent = "";
  error.hidden = true;
}

function showSymbolError(error) {
  const input = $("symbolInput");
  const errorElement = $("symbolError");
  setElementAttribute(input, "aria-invalid", "true");
  if (errorElement) {
    errorElement.textContent = compactErrorMessage(error.message);
    errorElement.hidden = false;
  }
  if (input && typeof input.focus === "function") input.focus({ preventScroll: true });
}

function cancelPendingLoadForValidation() {
  const request = state.pendingLoad;
  if (!request) return;
  cancelMinuteRequest();
  if (state.loadRequest) state.loadRequest.abort();
  state.loadRequest = null;
  state.pendingLoad = null;
  state.loadSeq += 1;
  stopStream();
  if (!request.previousAnalysis || !request.previousSymbol) {
    renderWorkbenchCancelled(request.symbol);
    state.coreStatus = { phase: "idle", text: "尚未加载", kind: "" };
    renderCompositeStatus();
    return;
  }
  state.symbol = request.previousSymbol;
  state.lastAnalysis = request.previousAnalysis;
  state.coreStatus = request.previousCoreStatus;
  state.dataQualityStatus = request.previousDataQualityStatus;
  state.mutationStatus = request.previousMutationStatus;
  const watchSymbolInput = $("watchSymbolInput");
  if (watchSymbolInput) watchSymbolInput.value = request.previousSymbol.slice(0, 6);
  renderCompositeStatus();
  startStream({ context: currentLoadContext() });
}

function handleWorkspaceTabKeydown(event) {
  const current = event.target.closest("button[data-view]");
  if (!current) return;
  const tabs = Array.from(document.querySelectorAll(".workspace-tabs button[data-view]"));
  const index = tabs.indexOf(current);
  if (index < 0) return;
  let targetIndex;
  if (event.key === "ArrowRight") targetIndex = (index + 1) % tabs.length;
  else if (event.key === "ArrowLeft") targetIndex = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") targetIndex = 0;
  else if (event.key === "End") targetIndex = tabs.length - 1;
  else return;
  event.preventDefault();
  const target = tabs[targetIndex];
  setWorkspaceView(target.dataset.view);
  if (typeof target.focus === "function") target.focus();
}

const mainStockSearch = createStockSearchBinding({
  inputId: "symbolInput",
  listId: "symbolSuggestions",
  onSelect(symbol) {
    clearSymbolError();
    setActiveSymbol(symbol);
    void loadAll({ reveal: true });
  },
});

const watchStockSearch = createStockSearchBinding({
  inputId: "watchSymbolInput",
  listId: "watchSymbolSuggestions",
  onSelect() {},
});

$("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("symbolInput");
  let symbol;
  try {
    symbol = validateUiSymbol(input.value);
  } catch (error) {
    if (mainStockSearch.selectDefault()) {
      clearSymbolError();
      return;
    }
    cancelPendingLoadForValidation();
    showSymbolError(new Error(mainStockSearch.validationMessage(error.message)));
    return;
  }
  clearSymbolError();
  setActiveSymbol(symbol);
  loadAll({ reveal: true });
});

$("symbolInput").addEventListener("input", (event) => {
  try {
    validateUiSymbol(event.currentTarget.value);
    mainStockSearch.close();
    clearSymbolError();
  } catch (error) {
    mainStockSearch.input(event.currentTarget.value);
    // Keep the current validation message until the input becomes valid.
  }
});

$("watchSymbolInput").addEventListener("input", (event) => {
  try {
    validateUiSymbol(event.currentTarget.value);
    setElementAttribute(event.currentTarget, "aria-invalid", "false");
    watchStockSearch.close();
  } catch (error) {
    watchStockSearch.input(event.currentTarget.value);
  }
});

$("quickList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-symbol]");
  if (!button) return;
  clearSymbolError();
  setActiveSymbol(button.dataset.symbol);
  loadAll({ reveal: true });
});

const workspaceTabs = document.querySelector(".workspace-tabs");
workspaceTabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button) return;
  setWorkspaceView(button.dataset.view);
});
workspaceTabs.addEventListener("keydown", handleWorkspaceTabKeydown);

$("researchActivityFilters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-activity-filter]");
  if (!button) return;
  const filter = button.dataset.activityFilter;
  if (!ACTIVITY_FILTERS.some((item) => item.value === filter)) return;
  state.researchActivityFilter = filter;
  renderResearchActivityPanel();
});

$("reviewAdviceId").addEventListener("change", () => {
  selectAdviceReviewSnapshot(state);
});

$("reviewPlanCancel").addEventListener("click", () => {
  cancelAdviceReviewEdit(state);
});

$("reviewPlanForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = $("reviewPlanFeedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(event.currentTarget, state.adviceReviewEditingPlanId ? "更新中" : "建立中", () =>
      submitAdviceReviewPlan(state, options)
    );
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineFeedback("reviewPlanFeedback", error);
  }
});

$("reviewPlanList").addEventListener("click", async (event) => {
  const editButton = event.target.closest("button[data-review-edit]");
  if (editButton) {
    beginAdviceReviewEdit(state, editButton.dataset.reviewEdit);
    return;
  }
  const deleteButton = event.target.closest("button[data-review-delete]");
  if (deleteButton) {
    const options = currentWorkbenchMutationOptions();
    if (!options) return;
    await runButtonTask(
      deleteButton,
      () => deleteAdviceReviewPlan(state, deleteButton.dataset.reviewDelete, {
        ...options,
        confirm: (message) => window.confirm(message),
      }),
      { isCurrent: options.isCurrent, onError: (error) => setInlineFeedback("reviewPlanFeedback", error) }
    );
    return;
  }
  const historyRetryButton = event.target.closest("button[data-review-history-retry]");
  if (historyRetryButton) {
    const options = currentWorkbenchMutationOptions();
    if (options) await retryAdviceReviewHistory(state, historyRetryButton.dataset.reviewHistoryRetry, options);
    return;
  }
  const historyButton = event.target.closest("button[data-review-history]");
  if (historyButton) {
    const options = currentWorkbenchMutationOptions();
    if (options) await toggleAdviceReviewHistory(state, historyButton.dataset.reviewHistory, options);
    return;
  }
  const evaluateButton = event.target.closest("button[data-review-evaluate]");
  if (!evaluateButton) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  await runButtonTask(
    evaluateButton,
    () => evaluateAdviceReviewPlan(state, evaluateButton.dataset.reviewEvaluate, options),
    { isCurrent: options.isCurrent, onError: (error) => setInlineFeedback("reviewPlanFeedback", error) }
  );
});

$("reviewPlanList").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-review-as-of]");
  if (!input) return;
  setAdviceReviewEvaluationAsOf(state, input.dataset.reviewAsOf, input.value);
});

$("watchlistScanForm").addEventListener("change", (event) => {
  if (event.target.matches?.('input[name="scanUniverse"]')) {
    syncWatchlistScanUniverse(event.currentTarget);
  }
});

initializeWatchlistScanControls();

$("watchlistScanForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await runSubmitTask(event.currentTarget, "扫描中", () => runWatchlistScan(state));
  } catch (error) {
    if (!isAbortError(error)) {
      setInlineFeedback("watchlistScanFeedback", error);
      setMutationStatus("error", `观察池扫描失败：${compactErrorMessage(error.message)}`, "warn");
    }
  }
});

$("watchlistScanResults").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-scan-symbol]");
  if (!button) return;
  setActiveSymbol(button.dataset.scanSymbol);
  setWorkspaceView("overview");
  loadAll({ reveal: true });
});

$("chartWorkspace").addEventListener("click", (event) => {
  const rangeButton = event.target.closest("button[data-daily-range]");
  if (rangeButton) {
    selectDailyChartRange(rangeButton.dataset.dailyRange);
    return;
  }
  const intervalButton = event.target.closest("button[data-minute-interval]");
  if (intervalButton) {
    void selectMinuteChartInterval(intervalButton.dataset.minuteInterval);
    return;
  }
  const viewButton = event.target.closest("button[data-chart-view]");
  if (viewButton) setMobileChartView(viewButton.dataset.chartView);
});

$("dailyMa5Toggle").addEventListener("change", (event) => {
  setDailyChartOverlay("ma5", event.currentTarget.checked);
});

$("dailyMa20Toggle").addEventListener("change", (event) => {
  setDailyChartOverlay("ma20", event.currentTarget.checked);
});

$("watchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("watchSymbolInput");
  try {
    validateUiSymbol(input.value);
  } catch (error) {
    if (!watchStockSearch.selectDefault()) {
      const message = watchStockSearch.validationMessage(error.message);
      setElementAttribute(input, "aria-invalid", "true");
      setWatchlistFeedback(message, "error");
      input.focus?.();
      return;
    }
  }
  setElementAttribute(input, "aria-invalid", "false");
  clearWatchlistFeedback();
  const options = currentWatchlistMutationOptions("加入");
  try {
    if (await addWatchlistItem(state, options)) {
      reconcileStreamSubscription();
      revealMobileFeedback($("watchList"));
    }
  } catch (error) {
    if (isAbortError(error) || !options.isCurrent()) return;
    renderWatchlist(Array.isArray(state.watchlist) ? state.watchlist : []);
    $("watchList").innerHTML += `<div class="watch-row watch-row-error"><strong>加入失败</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span></div>`;
    setWatchlistFeedback(`加入失败：${compactErrorMessage(error.message)}`, "error");
    setMutationStatus("error", "自选股加入失败", "warn");
    revealMobileFeedback($("watchList"));
  }
});

$("watchList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const symbol = button.dataset.symbol;
  if (button.dataset.action === "open") {
    clearWatchlistFeedback();
    setActiveSymbol(symbol);
    const workbenchLoad = loadAll({ reveal: true, waitForAdviceTimeline: true });
    const loadContext = { symbol: state.symbol, loadSeq: state.loadSeq };
    void Promise.resolve(workbenchLoad)
      .then(async (loaded) => {
        if (!loaded || loadContext.symbol !== state.symbol || loadContext.loadSeq !== state.loadSeq) return;
        const options = currentWatchlistMutationOptions("标记已读");
        const watermark = state.adviceTimelineWatermark;
        if (
          !watermark ||
          watermark.symbol !== loadContext.symbol ||
          watermark.loadSeq !== loadContext.loadSeq
        ) {
          appendWatchlistMessage("主工作台已打开，未读状态保持", "建议变化尚未完整展示，请稍后重试。");
          setWatchlistFeedback("主工作台已打开；建议变化尚未完整展示，未读状态保持。", "warn");
          setMutationStatus("degraded", "建议变化未完整展示，未读状态保持", "warn");
          return;
        }
        try {
          await markWatchlistItemViewed(state, symbol, {
            ...options,
            viewedThroughAdviceId: watermark.adviceId,
          });
        } catch (error) {
          if (isAbortError(error) || !options.isCurrent()) return;
          const detail = compactErrorMessage(error.message);
          appendWatchlistMessage("主工作台已打开，未读状态同步失败", detail);
          setWatchlistFeedback(`主工作台已打开；未读状态未清除：${detail}`, "warn");
          setMutationStatus("degraded", "自选股未读状态同步失败", "warn");
        }
      })
      .catch(() => {});
    return;
  }
  if (button.dataset.action === "edit") {
    toggleWatchlistEditor(button, true);
    return;
  }
  if (button.dataset.action === "cancel-edit") {
    const row = typeof button.closest === "function" ? button.closest(".watch-row") : null;
    const editButton = row && typeof row.querySelector === "function" ? row.querySelector('[data-action="edit"]') : null;
    toggleWatchlistEditor(editButton || button, false);
    return;
  }
  if (button.dataset.action === "remove") {
    clearWatchlistFeedback();
    const options = currentWatchlistMutationOptions("删除");
    await runButtonTask(
      button,
      async () => {
        const removed = await removeWatchlistItem(state, symbol, options);
        if (!removed || !options.isCurrent()) return;
        reconcileStreamSubscription();
        revealMobileFeedback($("watchList"));
      },
      {
        isCurrent: options.isCurrent,
        onError(error) {
          renderWatchlist(Array.isArray(state.watchlist) ? state.watchlist : []);
          $("watchList").innerHTML += `<div class="watch-row watch-row-error"><strong>删除失败</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span></div>`;
          setWatchlistFeedback(`删除失败：${compactErrorMessage(error.message)}`, "error");
          setMutationStatus("error", "自选股删除失败", "warn");
          revealMobileFeedback($("watchList"));
        },
      }
    );
  }
});

$("watchList").addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-watch-edit]");
  if (!form) return;
  event.preventDefault();
  clearWatchlistFeedback();
  const feedback = form.querySelector(".watch-edit-feedback");
  if (feedback) {
    feedback.textContent = "";
    feedback.hidden = true;
  }
  const symbol = form.dataset.symbol;
  const options = currentWatchlistMutationOptions("更新");
  let saved = false;
  if (typeof form.setAttribute === "function") form.setAttribute("aria-busy", "true");
  try {
    await runSubmitTask(form, "保存中", async () => {
      saved = await updateWatchlistItem(state, symbol, watchlistUpdatesFromForm(form), options);
    });
    if (saved && options.isCurrent()) revealMobileFeedback($("watchList"));
  } catch (error) {
    if (isAbortError(error) || !options.isCurrent()) return;
    setWatchlistEditError(form, error);
    setWatchlistFeedback(`保存失败：${compactErrorMessage(error.message)}`, "error");
    setMutationStatus("error", "自选股研究队列保存失败", "warn");
  } finally {
    if (typeof form.setAttribute === "function") form.setAttribute("aria-busy", "false");
  }
});

$("alertForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  try {
    await runSubmitTask(event.currentTarget, "添加中", () => addAlertRule(state, options));
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      $("alertList").innerHTML = `<div class="alert-row"><strong>添加失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      revealMobileFeedback($("alertList"));
    }
  }
});

$("evaluateAlerts").addEventListener("click", async () => {
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  try {
    await evaluateAlerts(state, options);
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      $("alertEvents").innerHTML = `<div class="alert-event"><strong>检查失败</strong><p>${escapeHtml(error.message)}</p></div>`;
      revealMobileFeedback($("alertEvents"));
    }
  }
});

$("enableAlertNotifications").addEventListener("click", async () => {
  if (state.alertNotificationsEnabled) {
    disableAlertNotifications(state);
    return;
  }
  await enableAlertNotifications(state);
});

$("exportLocalData").addEventListener("click", async () => {
  await runButtonTask($("exportLocalData"), () => exportLocalUserData());
});

$("localDataImportFile").addEventListener("change", async (event) => {
  try {
    await readLocalDataFile(state, event.currentTarget.files?.[0]);
  } catch (error) {
    setInlineFeedback("localDataFeedback", error);
  }
});

$("localDataImportMode").addEventListener("change", () => {
  invalidateLocalDataImportPreview(state);
});

$("previewLocalDataImport").addEventListener("click", async () => {
  await runButtonTask($("previewLocalDataImport"), () => previewLocalDataImport(state), {
    onError: (error) => setInlineFeedback("localDataFeedback", error),
  });
});

$("commitLocalDataImport").addEventListener("click", async () => {
  await runButtonTask($("commitLocalDataImport"), () => commitLocalDataAndRefresh(), {
    onError: (error) => setInlineFeedback("localDataFeedback", error),
  });
});

$("runRuntimeCleanup").addEventListener("click", async () => {
  let preview;
  try {
    preview = await loadRuntimeCleanupPreview();
  } catch (error) {
    setInlineFeedback("localDataFeedback", error);
    return;
  }
  await runButtonTask(
    $("runRuntimeCleanup"),
    () => runRuntimeCleanup(preview, { confirm: (message) => window.confirm(message) }),
    { onError: (error) => setInlineFeedback("localDataFeedback", error) }
  );
});

$("alertList").addEventListener("click", async (event) => {
  const editButton = event.target.closest("button[data-alert-edit]");
  if (editButton) {
    toggleAlertRuleEditor(editButton, true);
    return;
  }
  const cancelButton = event.target.closest("button[data-alert-cancel]");
  if (cancelButton) {
    const row = cancelButton.closest?.(".alert-row");
    toggleAlertRuleEditor(row?.querySelector?.("[data-alert-edit]") || cancelButton, false);
    return;
  }
  const toggleButton = event.target.closest("button[data-alert-toggle]");
  if (toggleButton) {
    const options = currentWorkbenchMutationOptions();
    if (!options) return;
    clearRowActionError(toggleButton);
    const completed = await runButtonTask(
      toggleButton,
      () => updateAlertRule(state, toggleButton.dataset.alertToggle, { enabled: toggleButton.dataset.alertEnabled === "true" }, options),
      { isCurrent: options.isCurrent, onError: (error) => showRowActionError(toggleButton, error) }
    );
    if (completed) renderResearchActivityPanel();
    return;
  }
  const button = event.target.closest("button[data-alert-remove]");
  if (!button) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearRowActionError(button);
  const completed = await runButtonTask(
    button,
    () => removeAlertRule(state, button.dataset.alertRemove, options),
    { isCurrent: options.isCurrent, onError: (error) => showRowActionError(button, error) }
  );
  if (completed) renderResearchActivityPanel();
});

$("alertList").addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-alert-edit-form]");
  if (!form) return;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = form.querySelector(".inline-edit-feedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(form, "保存中", () =>
      updateAlertRule(state, form.dataset.alertId, alertRuleUpdatesFromForm(form), options)
    );
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineEditError(form, error);
  }
});

$("noteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  try {
    await runSubmitTask(event.currentTarget, "保存中", () => addStockNote(state, loadChartMarks, options));
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      $("noteList").innerHTML = `<div class="note-row"><strong>保存失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      revealMobileFeedback($("noteList"));
    }
  }
});

$("noteList").addEventListener("click", async (event) => {
  const editButton = event.target.closest("button[data-note-edit]");
  if (editButton) {
    toggleStockNoteEditor(editButton, true);
    return;
  }
  const cancelButton = event.target.closest("button[data-note-cancel]");
  if (cancelButton) {
    const row = cancelButton.closest?.(".note-row");
    toggleStockNoteEditor(row?.querySelector?.("[data-note-edit]") || cancelButton, false);
    return;
  }
  const toggleButton = event.target.closest("button[data-note-toggle]");
  if (toggleButton) {
    const options = currentWorkbenchMutationOptions();
    if (!options) return;
    clearRowActionError(toggleButton);
    const completed = await runButtonTask(
      toggleButton,
      () => updateStockNote(state, toggleButton.dataset.noteToggle, { visible: toggleButton.dataset.noteVisible === "true" }, loadChartMarks, options),
      { isCurrent: options.isCurrent, onError: (error) => showRowActionError(toggleButton, error) }
    );
    if (completed) renderResearchActivityPanel();
    return;
  }
  const button = event.target.closest("button[data-note-remove]");
  if (!button) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearRowActionError(button);
  const completed = await runButtonTask(
    button,
    () => removeStockNote(state, button.dataset.noteRemove, loadChartMarks, options),
    { isCurrent: options.isCurrent, onError: (error) => showRowActionError(button, error) }
  );
  if (completed) renderResearchActivityPanel();
});

$("noteList").addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-note-edit-form]");
  if (!form) return;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = form.querySelector(".inline-edit-feedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(form, "保存中", () =>
      updateStockNote(
        state,
        form.dataset.noteId,
        stockNoteUpdatesFromForm(form),
        loadChartMarks,
        options
      )
    );
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineEditError(form, error);
  }
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

document.querySelector(".monitor-actions").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-task]");
  if (!button) return;
  await runMonitorTask(state, button.dataset.task);
  revealMobileFeedback($("schedulerState"));
});

window.addEventListener("resize", () => {
  clearTimeout(window.__chartTimer);
  window.__chartTimer = setTimeout(() => {
    syncChartWorkspaceLayout();
    redrawResearchCharts();
  }, 200);
});

function handleVisibilityChange() {
  if (document.hidden) {
    if (state.monitorTimer) {
      clearInterval(state.monitorTimer);
      state.monitorTimer = null;
    }
    if (state.monitorRequest) {
      state.visibilityRefreshSources.add("monitoring");
      cancelMonitoringRefresh(state);
    }
    if (state.dataStatusRequest) {
      state.visibilityRefreshSources.add("data-status");
      cancelDataStatusRefresh(state);
    }
    stopStream();
    return;
  }
  refreshGlobalPanels({ force: true });
  if (state.lastAnalysis) reconcileStreamSubscription();
}

document.addEventListener("visibilitychange", handleVisibilityChange);

initializeChartInspectors();
initializeAlertNotifications(state);
restoreWorkspacePreferences();

export const __appTest = {
  state,
  loadAll,
  loadChartMarks,
  loadAdviceTimeline,
  loadPlateRank,
  loadMarketPanels,
  loadMinuteAnalysis,
  redrawResearchCharts,
  selectDailyChartRange,
  selectMinuteChartInterval,
  setDailyChartOverlay,
  setMobileChartView,
  syncChartWorkspaceLayout,
  refreshDataStatus,
  refreshGlobalPanels,
  refreshMonitoring,
  refreshWatchlist,
  handleVisibilityChange,
  reconcileStreamSubscription,
  setActiveSymbol,
  setWorkspaceView,
  currentWorkspacePreferences,
  restoreWorkspacePreferences,
  startStream,
  compositeStatus,
  GLOBAL_ENDPOINTS,
  GLOBAL_REFRESH_TTL_MS,
  DAILY_CHART_RANGES,
  MINUTE_CHART_INTERVALS,
};

if (!globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__) {
  loadAll();
}
