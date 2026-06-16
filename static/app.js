import { fetchJson } from "./js/api.js";
import { $, escapeHtml, setMetricTone } from "./js/dom.js";
import { compactErrorMessage } from "./js/errors.js";
import { changeClass, formatAmount, formatNumber, toneByScore, toneByText } from "./js/format.js";
import { normalizeUiSymbol } from "./js/symbols.js";

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

function providerState(item) {
  if (!item.enabled) return { text: "未启用", tone: "idle" };
  if (item.healthy) return { text: "当前正常", tone: "ok" };
  return { text: "最近失败", tone: "bad" };
}

function providerDetail(item) {
  if (!item.enabled) return item.last_error ? compactErrorMessage(item.last_error) : "未配置或主动关闭，不参与当前分析。";
  if (item.healthy) {
    const latency = item.latency_ms === null || item.latency_ms === undefined ? "" : ` · 延迟 ${formatNumber(item.latency_ms, 0)}ms`;
    return item.last_success ? `最近成功：${item.last_success}${latency}` : "等待首次成功请求";
  }
  const error = compactErrorMessage(item.last_error || "最近一次请求失败");
  const lastSuccess = item.last_success ? `；上次成功：${item.last_success}` : "";
  return `${error}${lastSuccess}`;
}

async function loadAll() {
  const requestId = ++state.loadSeq;
  const requestedSymbol = state.symbol;
  try {
    setStatus("数据刷新中", "warn");
    const workbench = await fetchJson(`/api/stock/workbench?symbol=${encodeURIComponent(requestedSymbol)}`);
    if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
    const analysis = workbench.analysis;
    document.body.classList.remove("is-stale");
    renderAnalysis(analysis);
    renderInsights(workbench.insights);
    renderResearch(workbench);
    state.chartMarks = workbench.chart_marks ? workbench.chart_marks.marks || [] : [];
    syncMarkCategories(workbench.chart_marks ? workbench.chart_marks.categories || [] : []);
    renderMarkFilters();
    drawKline(analysis.klines, analysis.ma5, analysis.ma20);
    renderAlerts(workbench.alert_rules || []);
    renderAlertEvents(workbench.alert_events || []);
    renderNotes(workbench.notes || []);
    loadMarketPanels(requestId, requestedSymbol);
    $("sourceLine").textContent = `数据源：${analysis.quote.source}，更新时间：${analysis.quote.timestamp}`;
    setStatus("实时连接正常", "ok");
    loadDataStatus();
    loadMonitoring();
    loadMinuteAnalysis();
    await loadWatchlist();
    loadAdviceTimeline();
    loadPlateRank();
    startStream();
  } catch (error) {
    if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
    markLoadFailure(error, requestedSymbol);
  }
}

async function loadMarketPanels(requestId, requestedSymbol) {
  const [marketResult, strongResult] = await Promise.allSettled([
    fetchJson("/api/market"),
    fetchJson("/api/strong-stocks"),
  ]);
  if (requestId !== state.loadSeq || requestedSymbol !== state.symbol) return;
  const market = marketResult.status === "fulfilled" ? marketResult.value : null;
  const strong = strongResult.status === "fulfilled" ? strongResult.value : null;
  renderMarket(market ? market.indices || [] : []);
  renderStrongStocks(strong ? strong.items || [] : market ? market.strong_stocks || [] : []);
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

async function loadWatchlist() {
  try {
    const items = await fetchJson("/api/watchlist");
    state.watchlist = items;
    renderWatchlist(items);
  } catch (error) {
    state.watchlist = [];
    $("watchList").innerHTML = `<div class="watch-row"><strong>自选股读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function renderWatchlist(items) {
  $("watchList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="watch-row" data-symbol="${escapeHtml(item.symbol)}">
            <button type="button" class="watch-main" data-action="open" data-symbol="${escapeHtml(item.symbol)}">
              <strong>${escapeHtml(item.name)} <span>${escapeHtml(item.code)}</span></strong>
              <small>${escapeHtml(item.note || item.group_name || "默认关注")}</small>
            </button>
            <div class="watch-side">
              <strong>${formatNumber(item.latest_price)}</strong>
              <span class="${changeClass(item.latest_change_pct)}">${formatNumber(item.latest_change_pct)}%</span>
              <button type="button" class="icon-button" title="移出自选" aria-label="移出自选" data-action="remove" data-symbol="${escapeHtml(item.symbol)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="watch-row"><strong>暂无自选</strong><span>输入代码后加入关注。</span></div>`;
}

async function addWatchlistItem() {
  const symbol = $("watchSymbolInput").value.trim() || state.symbol;
  const note = $("watchNoteInput").value.trim();
  const button = $("watchForm").querySelector("button");
  try {
    button.disabled = true;
    button.textContent = "加入中";
    await fetchJson("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, note }),
    });
    $("watchNoteInput").value = "";
    await loadWatchlist();
  } finally {
    button.disabled = false;
    button.textContent = "加入";
  }
}

async function removeWatchlistItem(symbol) {
  await fetchJson(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
  await loadWatchlist();
}

async function loadAdviceTimeline() {
  try {
    const items = await fetchJson(`/api/advice/history?symbol=${encodeURIComponent(state.symbol)}&limit=8`);
    renderAdviceTimeline(items);
  } catch (error) {
    $("adviceTimeline").innerHTML = `<div class="timeline-item"><strong>时间线暂不可用</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function loadAlerts() {
  const [rulesResult, eventsResult] = await Promise.allSettled([
    fetchJson(`/api/alerts?symbol=${encodeURIComponent(state.symbol)}`),
    fetchJson(`/api/alerts/events?symbol=${encodeURIComponent(state.symbol)}&limit=6`),
  ]);
  if (rulesResult.status === "fulfilled") {
    renderAlerts(rulesResult.value);
  } else {
    $("alertList").innerHTML = `<div class="alert-row"><strong>预警读取失败</strong><span>${escapeHtml(rulesResult.reason.message)}</span></div>`;
  }
  if (eventsResult.status === "fulfilled") {
    renderAlertEvents(eventsResult.value);
  } else {
    $("alertEvents").innerHTML = `<div class="alert-event"><strong>事件读取失败</strong><p>${escapeHtml(eventsResult.reason.message)}</p></div>`;
  }
}

async function loadMinuteAnalysis() {
  const el = $("minuteAnalysis");
  if (!el) return;
  const requestedSymbol = state.symbol;
  el.innerHTML = loadingState("分钟分析加载中", "正在读取5分钟K线，不影响主分析。");
  try {
    const report = await fetchJson(`/api/stock/minute-analysis?symbol=${encodeURIComponent(requestedSymbol)}&interval=5m&limit=120`);
    if (requestedSymbol !== state.symbol) return;
    renderMinuteAnalysis(report);
  } catch (error) {
    if (requestedSymbol !== state.symbol) return;
    el.innerHTML = `<div class="minute-empty"><strong>分钟分析暂不可用</strong><span>${escapeHtml(compactErrorMessage(error.message))}</span><span>当前不按分钟区间做T，主分析和日线策略仍可参考。</span></div>`;
  }
}

async function addAlertRule() {
  const conditionType = $("alertType").value;
  const rawThreshold = $("alertThreshold").value.trim();
  const allowsDynamicLevel = conditionType === "break_support" || conditionType === "break_resistance";
  if (!rawThreshold && !allowsDynamicLevel) throw new Error("请输入有效阈值");
  const threshold = rawThreshold ? Number(rawThreshold) : 0;
  if (!Number.isFinite(threshold)) throw new Error("请输入有效阈值");
  await fetchJson("/api/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: state.symbol,
      condition_type: conditionType,
      threshold,
      note: "本地个股研究提醒",
    }),
  });
  $("alertThreshold").value = "";
  await loadAlerts();
}

async function evaluateAlerts() {
  const button = $("evaluateAlerts");
  try {
    button.disabled = true;
    button.textContent = "检查中";
    const result = await fetchJson(`/api/alerts/evaluate?symbol=${encodeURIComponent(state.symbol)}`, { method: "POST" });
    renderAlertEvaluation(result);
    await loadAlerts();
  } finally {
    button.disabled = false;
    button.textContent = "检查";
  }
}

async function removeAlertRule(ruleId) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, { method: "DELETE" });
  await loadAlerts();
}

async function updateAlertRule(ruleId, payload) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadAlerts();
}

function renderAlerts(items) {
  $("alertList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="alert-row ${item.enabled ? "" : "is-muted"}">
            <div>
              <strong>${escapeHtml(item.name)}</strong>
              <span>${escapeHtml(item.condition_label)} ${formatNumber(item.threshold)} · ${escapeHtml(item.enabled ? item.last_state || "等待" : "已暂停")}</span>
              <small>触发 ${escapeHtml(item.trigger_count)} 次 · 冷却 ${escapeHtml(item.cooldown_seconds || 300)} 秒 · ${escapeHtml(item.last_checked_at || "尚未检查")}</small>
            </div>
            <div class="row-actions">
              <button type="button" class="mini-button" data-alert-toggle="${escapeHtml(item.id)}" data-alert-enabled="${item.enabled ? "false" : "true"}">${item.enabled ? "暂停" : "启用"}</button>
              <button type="button" class="icon-button" title="删除预警" aria-label="删除预警" data-alert-remove="${escapeHtml(item.id)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="alert-row"><strong>暂无预警</strong><span>添加价格、涨跌幅或趋势评分提醒。</span></div>`;
}

function renderAlertEvents(items) {
  $("alertEvents").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="alert-event ${item.event_type === "恢复" ? "recover" : ""}">
            <strong>${escapeHtml(item.name)} · ${escapeHtml(item.event_type || "触发")}</strong>
            <span>${escapeHtml(item.created_at)} · ${formatNumber(item.price)} / ${formatNumber(item.change_pct)}%</span>
            <p>${escapeHtml(item.message)}</p>
          </div>`
        )
        .join("")
    : `<div class="alert-event"><strong>暂无触发记录</strong><p>手动检查或后续调度触发后会记录在这里。</p></div>`;
}

function renderAlertEvaluation(result) {
  $("alertEvents").innerHTML = `
    <div class="alert-event">
      <strong>检查完成</strong>
      <span>${escapeHtml(result.checked_at)} · 触发 ${escapeHtml(result.triggered_count)} / ${escapeHtml(result.checked_count)}</span>
      <p>新增触发记录 ${escapeHtml(result.new_event_count)} 条。</p>
    </div>
  `;
}

async function loadNotes() {
  try {
    const notes = await fetchJson(`/api/stock/notes?symbol=${encodeURIComponent(state.symbol)}&limit=8`);
    renderNotes(notes);
  } catch (error) {
    $("noteList").innerHTML = `<div class="note-row"><strong>笔记读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

async function addStockNote() {
  const content = $("noteContent").value.trim();
  if (!content) throw new Error("请输入笔记内容");
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  await fetchJson("/api/stock/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: state.symbol,
      content,
      note_type: $("noteType").value,
      price: quote ? quote.price : undefined,
      trade_date: quote ? quote.timestamp : undefined,
    }),
  });
  $("noteContent").value = "";
  await loadNotes();
  await loadChartMarks();
}

async function removeStockNote(noteId) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, { method: "DELETE" });
  await loadNotes();
  await loadChartMarks();
}

async function updateStockNote(noteId, payload) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadNotes();
  await loadChartMarks();
}

function renderNotes(items) {
  $("noteList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="note-row ${item.visible ? "" : "is-muted"}">
            <div>
              <strong>${escapeHtml(item.note_type)} · ${formatNumber(item.price)}</strong>
              <span>${escapeHtml(item.content)}</span>
              <small>${escapeHtml(item.trade_date || item.created_at)}${item.visible ? "" : " · 已隐藏"}</small>
            </div>
            <div class="row-actions">
              <button type="button" class="mini-button" data-note-toggle="${escapeHtml(item.id)}" data-note-visible="${item.visible ? "false" : "true"}">${item.visible ? "隐藏" : "显示"}</button>
              <button type="button" class="icon-button" title="删除笔记" aria-label="删除笔记" data-note-remove="${escapeHtml(item.id)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="note-row"><strong>暂无笔记</strong><span>记录你的个股观察，会同步为图表标注。</span></div>`;
}

async function loadChartMarks() {
  try {
    const summary = await fetchJson(`/api/stock/chart-marks?symbol=${encodeURIComponent(state.symbol)}&limit=40`);
    state.chartMarks = summary.marks || [];
    syncMarkCategories(summary.categories || []);
    renderMarkFilters();
    if (state.lastAnalysis) {
      drawKline(state.lastAnalysis.klines, state.lastAnalysis.ma5, state.lastAnalysis.ma20);
    }
  } catch (error) {
    state.chartMarks = [];
  }
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

async function loadMonitoring() {
  const [statusResult, runsResult, eventsResult] = await Promise.allSettled([
    fetchJson("/api/tasks/status"),
    fetchJson("/api/tasks/runs?limit=8"),
    fetchJson("/api/monitor/events?limit=8"),
  ]);
  if (statusResult.status === "fulfilled") {
    renderSchedulerStatus(statusResult.value, runsResult.status === "fulfilled" ? runsResult.value : []);
  } else {
    const error = statusResult.reason;
    $("schedulerState").textContent = "读取失败";
    $("taskCards").innerHTML = `<div class="task-card"><strong>监控暂不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
  if (eventsResult.status === "fulfilled") {
    renderMonitorEvents(eventsResult.value);
  } else {
    $("monitorEvents").innerHTML = `<div class="monitor-event warn"><strong>事件读取失败</strong><p>${escapeHtml(eventsResult.reason.message)}</p></div>`;
  }
  if (state.monitorTimer && !document.hidden) {
    clearInterval(state.monitorTimer);
    state.monitorTimer = null;
  }
  if (!state.monitorTimer && !document.hidden) {
    state.monitorTimer = setInterval(loadMonitoring, 15000);
  }
}

function renderSchedulerStatus(status, runs) {
  $("schedulerState").textContent = status.running ? "运行中" : status.enabled ? "已启用" : "已关闭";
  const runMap = new Map((runs || []).map((item) => [item.task_name, item]));
  $("taskCards").innerHTML = status.tasks
    .map((task) => {
      const recent = runMap.get(task.name);
      const statusText = task.running ? "执行中" : statusLabel(task.last_status || (recent && recent.status));
      const message = task.last_message || (recent && recent.message) || "等待首次运行";
      return `
        <div class="task-card">
          <div>
            <strong>${escapeHtml(task.display_name)}</strong>
            <span>${escapeHtml(message)}</span>
            <small>下次：${escapeHtml(task.next_run_at || "--")}</small>
          </div>
          <i class="task-badge ${task.last_status === "failed" ? "bad" : task.running ? "running" : ""}">${escapeHtml(statusText)}</i>
        </div>`;
    })
    .join("");
}

function statusLabel(status) {
  if (status === "success") return "正常";
  if (status === "failed") return "异常";
  if (status === "running") return "执行中";
  return "等待";
}

function renderMonitorEvents(items) {
  $("monitorEvents").innerHTML = items.length
    ? items
        .map(
          (item) => {
            const repeat = item.repeat_count && item.repeat_count > 1 ? ` · 重复 ${item.repeat_count} 次` : "";
            const seenAt = item.last_seen_at || item.created_at;
            return `
          <div class="monitor-event ${item.level === "warning" ? "warn" : ""}">
            <strong>${escapeHtml(eventCategory(item.category))}${item.symbol ? ` · ${escapeHtml(item.symbol)}` : ""}</strong>
            <span>${escapeHtml(seenAt)}${escapeHtml(repeat)}</span>
            <p>${escapeHtml(item.message)}</p>
          </div>`;
          }
        )
        .join("")
    : `<div class="monitor-event"><strong>暂无事件</strong><p>本地监控启动后会记录刷新和健康检查结果。</p></div>`;
}

function eventCategory(category) {
  const names = {
    scheduler: "调度器",
    task: "任务",
    provider: "数据源",
    quote: "报价",
    kline: "K线",
    plate: "行业",
    health: "健康",
  };
  return names[category] || category;
}

async function runMonitorTask(task) {
  const buttons = document.querySelectorAll(".monitor-actions button");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    $("schedulerState").textContent = "执行中";
    await fetchJson(`/api/tasks/run-once?task=${encodeURIComponent(task)}`, { method: "POST" });
  } catch (error) {
    $("schedulerState").textContent = error.message;
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
    });
    loadMonitoring();
    loadDataStatus();
  }
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

async function loadDataStatus() {
  try {
    const status = await fetchJson("/api/data/status");
    renderDataStatus(status);
  } catch (error) {
    $("providerStatus").innerHTML = `<div class="provider-item"><strong>状态读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function renderDataStatus(status) {
  $("cachePath").textContent = "本地缓存";
  renderSourcePlan(status.source_plan);
  const capabilityStatusText = (status.capability_statuses || [])
    .filter((item) => item.enabled)
    .map((item) => `${escapeHtml(item.name)}·${escapeHtml(capabilityKindLabel(item.kind))}·${escapeHtml(capabilityHealthLabel(item))}`)
    .join(" / ");
  $("providerStatus").innerHTML = status.providers
    .map((item) => {
      const stateInfo = providerState(item);
      return `
      <div class="provider-item ${stateInfo.tone}">
        <div>
          <strong>${escapeHtml(item.name)} · ${escapeHtml(stateInfo.text)}</strong>
          <span>累计成功 ${escapeHtml(item.success_count)} 次 · 累计失败 ${escapeHtml(item.failure_count)} 次</span>
          <small>${escapeHtml(providerDetail(item))}</small>
        </div>
        <i class="health-dot ${stateInfo.tone}"></i>
      </div>`;
    })
    .join("");
  $("cacheStats").innerHTML = `
    <strong>缓存：报价 ${status.cache.quote_count} 条，K线 ${status.cache.kline_count} 条</strong>
    <span>股票池 ${status.cache.stock_count} 条 · 板块 ${status.cache.plate_count} 条 · 快照历史 ${status.cache.quote_history_count} 条</span>
    <span>说明：成功/失败是本地累计调用次数；状态看最近一次请求，未启用源不会参与当前分析。</span>
    <span>能力：${status.capabilities.map((item) => `${escapeHtml(item.name)}·${escapeHtml(item.reliability_level || "公开源")}·${item.enabled ? "可用" : "待启用"}`).join(" / ")}</span>
    ${capabilityStatusText ? `<span>能力状态：${capabilityStatusText}</span>` : ""}
  `;
}

function capabilityKindLabel(kind) {
  const labels = {
    quote: "报价",
    kline: "日K",
    minute: "分钟",
    stock: "股票池",
    plate: "板块",
    concept: "概念",
    order_book: "盘口",
  };
  return labels[kind] || kind;
}

function capabilityHealthLabel(item) {
  if (!item.last_success && !item.last_error && !item.success_count && !item.failure_count) return "未探测";
  return item.healthy ? "正常" : "失败";
}

function renderSourcePlan(plan) {
  const el = $("sourcePlan");
  if (!el) return;
  if (!plan) {
    el.innerHTML = "";
    return;
  }
  const tone = plan.health_level === "健康" ? "good" : plan.health_level === "高风险" ? "risk" : "warn";
  const keySources = [
    ["报价", plan.primary_quote_source || "缺失"],
    ["日K", plan.primary_kline_source || "缺失"],
    ["分钟", plan.primary_minute_source || "缺失"],
  ];
  el.innerHTML = `
    <div class="source-plan-head ${tone}">
      <strong>${escapeHtml(plan.health_level)}</strong>
      <span>${escapeHtml(plan.summary)}</span>
    </div>
    <div class="source-plan-sources">
      ${keySources.map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join("")}
    </div>
    ${
      (plan.warnings || []).length
        ? `<div class="source-plan-warnings">${plan.warnings.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`
        : ""
    }
    <div class="source-plan-actions">
      ${(plan.suggestions || []).slice(0, 2).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
    </div>
  `;
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

function renderAnalysis(data) {
  state.lastAnalysis = data;
  const quote = data.quote;
  $("stockCode").textContent = `${quote.market}${quote.code}`;
  $("stockName").textContent = quote.name;
  $("stockPrice").textContent = formatNumber(quote.price);
  $("stockChange").textContent = `${formatNumber(quote.change)} / ${formatNumber(quote.change_pct)}%`;
  $("stockChange").className = changeClass(quote.change_pct);
  $("trendScore").textContent = `${data.trend_score}`;
  $("trendLabel").textContent = data.trend_label;
  $("actionAdvice").textContent = data.action_advice ? `${data.action_advice.action} ${data.action_advice.confidence}%` : "--";
  $("support").textContent = formatNumber(data.support);
  $("resistance").textContent = formatNumber(data.resistance);
  $("ma5").textContent = formatNumber(data.ma5);
  $("ma20").textContent = formatNumber(data.ma20);
  $("dataQuality").textContent = data.data_quality ? `${data.data_quality.level} ${data.data_quality.score ?? "--"}分` : "--";
  $("summary").textContent = data.beginner_summary;
  setMetricTone("trendScore", toneByScore(data.trend_score, 68, 45));
  setMetricTone("trendLabel", toneByText(data.trend_label));
  setMetricTone("actionAdvice", toneByText(data.action_advice ? data.action_advice.action : ""));
  setMetricTone("support", "");
  setMetricTone("resistance", "");
  setMetricTone("ma5", Number(quote.price) >= Number(data.ma5) ? "good" : "warn");
  setMetricTone("ma20", Number(quote.price) >= Number(data.ma20) ? "good" : "risk");
  setMetricTone("dataQuality", data.data_quality ? toneByScore(data.data_quality.score, 85, 70) : "warn");
  renderQuality(data.data_quality);
  renderSignalEvidence(data.signal_snapshot);
  renderSignals("buySignals", data.buy_points);
  renderSignals("sellSignals", data.sell_points);
  renderSignals("tSignals", data.t_plan);
  renderReview(data.review);
  drawKline(data.klines, data.ma5, data.ma20);
}

function renderInsights(data) {
  state.lastInsights = data;
  renderInsightOverview(data.overview);
  renderFactors(data.overview.factors || []);
  renderFundFlow(data.fund_flow);
  renderOrderPressure(data.order_pressure);
  renderStrategyCards(data.strategy_cards || []);
  renderStockEvents(data.events);
  renderFinancialHealth(data.financial_health);
  renderValuation(data.valuation);
  renderAbnormalEvents(data.abnormal_events);
  renderLhb(data.lhb);
  renderRuleMatches(data.rule_matches);
}

function renderResearch(workbench) {
  renderAiDashboard(workbench);
  renderFeatureSnapshot(workbench.feature_snapshot);
  renderDiagnosis(workbench.diagnosis);
  renderAlphaEvidence(workbench.alpha_evidence);
  renderMarketRegime(workbench.market_regime);
  renderSignalValidation(workbench.signal_validation);
  renderTimeframeAlignment(workbench.timeframe_alignment);
  renderRiskReward(workbench.risk_reward);
  renderFactorLab(workbench.factor_lab);
  renderThemeContext(workbench.theme_context);
  renderChipAnalysis(workbench.chip_analysis);
  renderLeadership(workbench.leadership);
  renderReplay(workbench.replay);
}

function handleAiDashboardClick(event) {
  const button = event.target.closest("button[data-ai-question]");
  if (!button) return;
  const input = $("aiQuestionInput");
  const form = $("aiQuestionForm");
  if (!input || !form) return;
  input.value = button.dataset.aiQuestion || "";
  form.requestSubmit();
}

async function handleAiQuestionSubmit(event) {
  event.preventDefault();
  const input = $("aiQuestionInput");
  const form = $("aiQuestionForm");
  const button = form ? form.querySelector("button") : null;
  const question = input ? input.value.trim() : "";
  if (!question) return;
  try {
    if (button) {
      button.disabled = true;
      button.textContent = "分析中";
    }
    const answer = await fetchJson("/api/stock/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: state.symbol, question }),
    });
    const card = document.createElement("div");
    card.innerHTML = renderQuestionAnswerCard(answer);
    const old = document.querySelector(".ai-card-wide");
    if (old) old.remove();
    if (form && card.firstElementChild) {
      form.insertAdjacentElement("afterend", card.firstElementChild);
    }
  } catch (error) {
    const old = document.querySelector(".ai-card-wide");
    if (old) old.remove();
    if (form) {
      form.insertAdjacentHTML("afterend", `<section class="ai-card ai-card-wide"><strong>本次问诊</strong><span class="risk">${escapeHtml(error.message)}</span></section>`);
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "问一下";
    }
  }
}

function renderAiDashboard(workbench) {
  const el = $("aiDashboard");
  if (!el || !workbench) return;
  const qa = workbench.qa_report || {};
  const questionAnswer = workbench.question_answer || null;
  const evidence = workbench.evidence_chain || {};
  const risk = workbench.risk_radar || {};
  const eventDigest = workbench.event_digest || {};
  const peer = workbench.peer_comparison || {};
  const tStrategy = workbench.t_strategy || {};
  el.innerHTML = `
    <div class="ai-dashboard-head">
      <div>
        <span>AI单股驾驶舱</span>
        <strong>${escapeHtml(qa.summary || "围绕当前个股生成可执行问诊。")}</strong>
      </div>
      <i>${escapeHtml(risk.overall_level || "风险待确认")}</i>
    </div>
    <form class="ai-question-bar" id="aiQuestionForm">
      <input id="aiQuestionInput" type="text" maxlength="120" placeholder="输入你想问这只个股的问题，例如：现在能不能买？" />
      <button type="submit">问一下</button>
    </form>
    <div class="ai-question-presets">
      ${["现在能不能买？", "风险在哪里？", "适不适合做T？", "明天重点看什么？"].map((item) => `<button type="button" data-ai-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join("")}
    </div>
    ${questionAnswer ? renderQuestionAnswerCard(questionAnswer) : ""}
    <div class="ai-dashboard-grid">
      ${renderQaCard(qa)}
      ${renderEvidenceChainCard(evidence)}
      ${renderRiskRadarCard(risk)}
      ${renderEventDigestCard(eventDigest)}
      ${renderPeerCard(peer)}
      ${renderTStrategyCard(tStrategy)}
    </div>
  `;
  const questionForm = $("aiQuestionForm");
  if (questionForm) {
    questionForm.addEventListener("submit", handleAiQuestionSubmit);
  }
  el.onclick = handleAiDashboardClick;
}

function renderQuestionAnswerCard(report) {
  const answerSource = report.answer_source || (report.llm_used ? "大模型解释增强" : "规则问诊");
  const llmStatus = report.llm_status || "";
  return `
    <section class="ai-card ai-card-wide">
      <div class="ai-answer-head">
        <strong>本次问诊</strong>
        <i class="${report.llm_used ? "good" : ""}">${escapeHtml(answerSource)}</i>
      </div>
      <div>
        <b>${escapeHtml(report.question || "未输入问题")}</b>
        <span class="ai-answer-text">${escapeHtml(report.answer || report.conclusion || "暂无回答")}</span>
      </div>
      <span>主题：${escapeHtml(report.topic || "--")} · 置信度 ${escapeHtml(report.confidence ?? "--")}%</span>
      ${llmStatus ? `<em class="${report.llm_used ? "good" : ""}">${escapeHtml(llmStatus)}</em>` : ""}
      ${(report.evidence || []).slice(0, 3).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}
      ${(report.actions || []).length ? `<div class="ai-answer-columns"><b>行动建议</b>${(report.actions || []).slice(0, 3).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      ${(report.invalidations || []).length ? `<div class="ai-answer-columns"><b>失效条件</b>${(report.invalidations || []).slice(0, 3).map((item) => `<span class="risk">${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      ${(report.related_questions || []).length ? `<div class="ai-related-questions">${(report.related_questions || []).slice(0, 3).map((item) => `<button type="button" data-ai-question="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join("")}</div>` : ""}
    </section>
  `;
}

function renderQaCard(report) {
  const items = report.items || [];
  return `
    <section class="ai-card">
      <strong>个股问诊</strong>
      ${items.length ? items
        .slice(0, 4)
        .map((item) => `<div><b>${escapeHtml(item.question)}</b><span>${escapeHtml(item.answer)}</span></div>`)
        .join("") : `<span>问诊结果待生成。</span>`}
    </section>
  `;
}

function renderEvidenceChainCard(report) {
  return `
    <section class="ai-card">
      <strong>证据链</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      ${(report.support || []).slice(0, 2).map((item) => `<span class="good">${escapeHtml(item)}</span>`).join("")}
      ${(report.opposition || []).slice(0, 2).map((item) => `<span class="risk">${escapeHtml(item)}</span>`).join("")}
      ${(report.invalidations || []).slice(0, 2).map((item) => `<em>${escapeHtml(item)}</em>`).join("") || `<span>失效条件待确认。</span>`}
    </section>
  `;
}

function renderRiskRadarCard(report) {
  return `
    <section class="ai-card">
      <strong>风险雷达</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      <div class="radar-list">
        ${(report.items || []).length ? (report.items || [])
          .slice(0, 6)
          .map((item) => `<span class="${Number(item.score) >= 68 ? "risk" : Number(item.score) <= 35 ? "good" : ""}">${escapeHtml(item.name)} ${escapeHtml(item.level)} · ${escapeHtml(item.score)}</span>`)
          .join("") : `<span>风险项待确认</span>`}
      </div>
    </section>
  `;
}

function renderEventDigestCard(report) {
  return `
    <section class="ai-card">
      <strong>事件摘要</strong>
      <p>${escapeHtml(report.summary || "")}</p>
      ${(report.negative_events || []).slice(0, 2).map((item) => `<span class="risk">${escapeHtml(item)}</span>`).join("")}
      ${(report.positive_events || []).slice(0, 2).map((item) => `<span class="good">${escapeHtml(item)}</span>`).join("")}
      ${(report.watch_events || []).slice(0, 2).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
    </section>
  `;
}

function renderPeerCard(report) {
  return `
    <section class="ai-card">
      <strong>同行对比</strong>
      <p>${escapeHtml(report.summary || "同行样本待确认。")}</p>
      <span>${escapeHtml(report.industry || "行业待确认")} · 样本 ${escapeHtml(report.sample_count || 0)}</span>
      <span>${escapeHtml(report.valuation_position || "")} / ${escapeHtml(report.strength_position || "")}</span>
      ${(report.metrics || []).slice(0, 3).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}
    </section>
  `;
}

function renderTStrategyCard(report) {
  return `
    <section class="ai-card">
      <strong>做T助手</strong>
      <p>${escapeHtml(report.summary || "做T建议待生成。")}</p>
      <span>低吸区：${escapeHtml(report.low_zone || "--")}</span>
      <span>高抛区：${escapeHtml(report.high_zone || "--")}</span>
      ${(report.stop_conditions || []).slice(0, 2).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}
    </section>
  `;
}

function renderFeatureSnapshot(feature) {
  const el = $("featureSnapshot");
  if (!el || !feature) {
    if (el) el.innerHTML = "";
    return;
  }
  const chips = [
    ["趋势", `${feature.trend_score} · ${feature.trend_label}`],
    ["资金", `${feature.fund_flow_score}`],
    ["龙头", `${feature.leader_score} · ${feature.leader_level}`],
    ["量能", `${formatNumber(feature.volume_ratio)}倍`],
    ["估值", `${feature.valuation_score}`],
    ["质量", `${feature.data_quality_level} ${feature.data_quality_score}`],
  ];
  el.innerHTML = `
    ${chips
      .map(
        ([label, value]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>`
      )
      .join("")}
    <div class="feature-tags">${(feature.tags || []).map((item) => `<i>${escapeHtml(item)}</i>`).join("")}</div>
  `;
}

function renderDiagnosis(diagnosis) {
  const el = $("diagnosisPanel");
  if (!el || !diagnosis) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="diagnosis-head">
      <div>
        <span>个股诊断</span>
        <strong>${escapeHtml(diagnosis.headline)}</strong>
      </div>
      <i>${escapeHtml(diagnosis.action)} · ${escapeHtml(diagnosis.confidence)}%</i>
    </div>
    <p>${escapeHtml(diagnosis.beginner_summary)}</p>
    <small>${escapeHtml(diagnosis.professional_summary)}</small>
    <div class="diagnosis-grid">
      <div>
        <strong>确认信号</strong>
        ${(diagnosis.confirmation_signals || []).slice(0, 4).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
      <div>
        <strong>硬风险</strong>
        ${(diagnosis.hard_risks || []).slice(0, 4).map((item) => `<span class="risk">${escapeHtml(item)}</span>`).join("")}
      </div>
    </div>
  `;
}

function renderAlphaEvidence(report) {
  const el = $("alphaEvidence");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="alpha-head">
      <strong>Alpha证据链</strong>
      <span>${escapeHtml(report.verdict)} · 置信度 ${escapeHtml(report.confidence)}%</span>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="alpha-grid">
      <div>
        <strong>支持证据</strong>
        ${(report.positives || [])
          .slice(0, 4)
          .map((item) => `<span class="good"><b>${escapeHtml(item.title)} +${escapeHtml(item.impact)}</b><small>${escapeHtml(item.reason)}</small></span>`)
          .join("") || `<span><b>暂无</b><small>等待更多积极证据。</small></span>`}
      </div>
      <div>
        <strong>风险证据</strong>
        ${(report.negatives || [])
          .slice(0, 4)
          .map((item) => `<span class="risk"><b>${escapeHtml(item.title)} ${escapeHtml(item.impact)}</b><small>${escapeHtml(item.reason)}</small></span>`)
          .join("") || `<span><b>暂无</b><small>当前未识别核心风险证据。</small></span>`}
      </div>
    </div>
    ${(report.missing_data || []).length ? `<em>待补数据：${escapeHtml(report.missing_data.slice(0, 6).join("、"))}</em>` : ""}
  `;
}

function renderMarketRegime(report) {
  const el = $("marketRegime");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const riskClass = Number(report.risk_multiplier) >= 1.15 ? "risk" : Number(report.risk_multiplier) <= 0.92 ? "good" : "";
  el.innerHTML = `
    <div class="regime-head">
      <div>
        <span>市场环境</span>
        <strong>${escapeHtml(report.market_label)} · ${escapeHtml(report.stock_state)}</strong>
      </div>
      <i class="${riskClass}">风险倍率 ${formatNumber(report.risk_multiplier, 2)}</i>
    </div>
    <div class="regime-tags">
      <span>${escapeHtml(report.industry_label)}</span>
      <span>${escapeHtml(report.breadth_label || "市场宽度待确认")} · ${escapeHtml(report.breadth_score ?? "--")}分</span>
      <span>置信修正 ${Number(report.confidence_adjustment) > 0 ? "+" : ""}${escapeHtml(report.confidence_adjustment)}</span>
    </div>
    <div class="regime-grid">
      <div>
        <strong>操作提醒</strong>
        ${(report.suggestions || []).slice(0, 3).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
      <div>
        <strong>判断依据</strong>
        ${(report.evidence || []).slice(0, 4).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
    </div>
  `;
}

function renderSignalValidation(report) {
  const el = $("signalValidation");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="validation-head">
      <div>
        <span>信号验证闭环</span>
        <strong>${escapeHtml(report.overall_status)}</strong>
      </div>
      <i>${escapeHtml((report.items || []).length)}项</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="validation-grid">
      ${(report.items || []).slice(0, 4).map(renderValidationItem).join("")}
    </div>
    ${(report.notes || []).slice(0, 1).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderValidationItem(item) {
  const statusClass = item.status.includes("风险") || item.status.includes("压制") ? "risk" : item.status.includes("确认") ? "good" : "";
  return `
    <div class="validation-item ${statusClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.status)} · ${escapeHtml(item.confidence)}%</span>
      </div>
      <small>触发：${escapeHtml(item.trigger_condition)}</small>
      <small>确认：${escapeHtml(item.confirmation_condition)}</small>
      <small>失效：${escapeHtml(item.invalidation_condition)}</small>
      <em>${escapeHtml(item.historical_reference)}</em>
    </div>
  `;
}

function renderTimeframeAlignment(report) {
  const el = $("timeframeAlignment");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const conflictClass = report.conflict_level.includes("冲突") || report.alignment_label.includes("偏弱") ? "risk" : report.alignment_label.includes("共振") ? "good" : "";
  el.innerHTML = `
    <div class="timeframe-head">
      <div>
        <span>多周期一致性</span>
        <strong>${escapeHtml(report.alignment_label)} · ${escapeHtml(report.alignment_score)}分</strong>
      </div>
      <i class="${conflictClass}">${escapeHtml(report.conflict_level)}</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="timeframe-grid">
      ${(report.timeframes || []).map(renderTimeframeItem).join("")}
    </div>
    <div class="timeframe-suggestions">
      ${(report.suggestions || []).slice(0, 3).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
    </div>
  `;
}

function renderTimeframeItem(item) {
  const itemClass = Number(item.score) >= 62 ? "good" : Number(item.score) <= 45 ? "risk" : "";
  return `
    <div class="timeframe-item ${itemClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.score)} · ${escapeHtml(item.label)}</span>
      </div>
      <small>${escapeHtml(item.window_days)}日 · 涨跌 ${formatNumber(item.return_pct)}% · 回撤 ${formatNumber(item.max_drawdown_pct)}%</small>
      <small>${item.above_ma ? "高于" : "低于"}均线 ${formatNumber(item.ma_value)}</small>
    </div>
  `;
}

function renderRiskReward(report) {
  const el = $("riskReward");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const ratingClass = report.rating.includes("风险") || report.rating.includes("不足") ? "risk" : report.rating.includes("较好") ? "good" : "";
  el.innerHTML = `
    <div class="risk-reward-head">
      <div>
        <span>风险收益与情景</span>
        <strong>${escapeHtml(report.rating)}</strong>
      </div>
      <i class="${ratingClass}">收益风险比 ${formatNumber(report.reward_risk_ratio, 2)}</i>
    </div>
    <div class="risk-reward-metrics">
      <span>现价 <b>${formatNumber(report.current_price)}</b></span>
      <span>上方目标 <b>${formatNumber(report.upside_target)} / ${formatNumber(report.upside_pct)}%</b></span>
      <span>下方防守 <b>${formatNumber(report.downside_stop)} / ${formatNumber(report.downside_pct)}%</b></span>
      <span>ATR / 波动 <b>${formatNumber(report.atr_pct, 2)}% / ${formatNumber(report.volatility_pct, 2)}%</b></span>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="scenario-grid">
      ${(report.scenarios || []).map(renderScenarioPlan).join("")}
    </div>
    ${(report.notes || []).slice(0, 1).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderScenarioPlan(item) {
  const scenarioClass = item.name.includes("防守") ? "risk" : item.name.includes("积极") ? "good" : "";
  return `
    <div class="scenario-item ${scenarioClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.probability)}%</span>
      </div>
      <small>触发：${escapeHtml(item.trigger)}</small>
      <small>预期：${escapeHtml(item.expected_move)}</small>
      <small>应对：${escapeHtml(item.response)}</small>
      <em>失效：${escapeHtml(item.invalidation)}</em>
    </div>
  `;
}

function renderFactorLab(report) {
  const el = $("factorLab");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="factor-lab-head">
      <div>
        <span>因子实验室</span>
        <strong>${escapeHtml(report.total_score)}分 · 校准置信 ${escapeHtml(report.calibrated_confidence)}%</strong>
      </div>
      <i>${escapeHtml((report.top_positive || [])[0] || "等待确认")}</i>
    </div>
    <div class="factor-lab-metrics">
      <span>个股画像 <b>${escapeHtml(report.profile_label || "常规个股")}</b></span>
      <span>历史样本 <b>${escapeHtml(report.calibration_sample_count || 0)}</b></span>
      <span>正向因子 <b>${escapeHtml(report.positive_factor_count || 0)}</b></span>
      <span>拖累因子 <b>${escapeHtml(report.negative_factor_count || 0)}</b></span>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="factor-lab-grid">
      ${(report.factors || []).slice(0, 6).map(renderStandardFactor).join("")}
    </div>
    ${(report.weight_policy || []).slice(0, 2).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}
    ${(report.notes || []).slice(0, 2).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderStandardFactor(item) {
  const calibration = item.calibration || {};
  const sampleText = calibration.sample_count
    ? `样本 ${escapeHtml(calibration.sample_count)} · ${escapeHtml(calibration.confidence_level || "观察")} / ${escapeHtml(calibration.expected_level || "观察")}`
    : escapeHtml(calibration.confidence_level || "待补数据");
  const returnText = calibration.sample_count
    ? `胜率 ${formatNumber(calibration.win_rate, 1)}% · 5日 ${formatNumber(calibration.avg_forward_5d_return)}% · 最大不利 ${formatNumber(calibration.max_adverse_return)}%`
    : "";
  const percentileText = item.percentile === null || item.percentile === undefined ? "" : `历史分位 ${formatNumber(item.percentile, 1)}%`;
  const bucket = (item.calibration_buckets || [])[0];
  const directionClass = item.direction === "负向" ? "risk" : item.direction === "正向" ? "good" : "";
  return `
    <div class="standard-factor ${directionClass}">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.score)} · 权重 ${formatNumber(item.weight, 2)}</span>
      </div>
      <div class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></i></div>
      <p>${escapeHtml(item.value)}</p>
      <small>${sampleText}</small>
      ${returnText ? `<small>${escapeHtml(returnText)}</small>` : ""}
      ${percentileText ? `<em>${escapeHtml(percentileText)}</em>` : ""}
      ${bucket ? `<em>${escapeHtml(bucket.name)}：${escapeHtml(bucket.sample_count)}样本 / 5日 ${formatNumber(bucket.avg_forward_5d_return)}%</em>` : ""}
      ${(item.evidence || []).slice(0, 1).map((text) => `<small>${escapeHtml(text)}</small>`).join("")}
    </div>
  `;
}

function renderChipAnalysis(chip) {
  const el = $("chipPanel");
  if (!el || !chip) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="chip-head">
      <strong>${escapeHtml(chip.distribution_label)} · ${escapeHtml(chip.concentration)}</strong>
      <span>成本中枢 ${formatNumber(chip.center_price)}</span>
    </div>
    <p>${escapeHtml(chip.summary)}</p>
    <div class="band-grid">
      <div>
        <strong>支撑区</strong>
        ${renderChipBands(chip.support_bands)}
      </div>
      <div>
        <strong>压力区</strong>
        ${renderChipBands(chip.pressure_bands)}
      </div>
    </div>
    ${(chip.notes || []).slice(0, 2).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderChipBands(items) {
  return (items || []).length
    ? items
        .slice(0, 3)
        .map((item) => `<span><b>${formatNumber(item.low)} - ${formatNumber(item.high)}</b><small>${formatNumber(item.share, 1)}% · ${escapeHtml(item.note)}</small></span>`)
        .join("")
    : `<span><b>暂无</b><small>当前价格附近缺少明显成交密集区。</small></span>`;
}

function renderLeadership(report) {
  const el = $("leadershipPanel");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="leader-head">
      <strong>${escapeHtml(report.score)} · ${escapeHtml(report.level)}</strong>
      <span>${escapeHtml(report.summary)}</span>
    </div>
    <div class="feature-tags">${(report.tags || []).map((item) => `<i>${escapeHtml(item)}</i>`).join("")}</div>
    ${(report.evidence || []).slice(0, 4).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    ${(report.missing_data || []).length ? `<small>待补：${escapeHtml(report.missing_data.join("、"))}</small>` : ""}
  `;
}

function renderThemeContext(report) {
  const el = $("themePanel");
  if (!el || !report) {
    if (el) el.innerHTML = "";
    return;
  }
  const concepts = report.concepts || [];
  el.innerHTML = `
    <div class="theme-head">
      <div>
        <strong>${escapeHtml(report.level)} · ${escapeHtml(report.score)}分</strong>
        <span>${escapeHtml(report.style)} · ${escapeHtml(report.relative_strength || "强弱待确认")}</span>
      </div>
      <i>${escapeHtml(report.industry)}${report.industry_change_pct === null || report.industry_change_pct === undefined ? "" : ` ${formatNumber(report.industry_change_pct)}%`}</i>
    </div>
    <p>${escapeHtml(report.summary)}</p>
    <div class="concept-strip">
      ${
        concepts.length
          ? concepts
              .slice(0, 6)
              .map(
                (item) => `
                <span class="${Number(item.change_pct) >= 0 ? "up-bg" : "down-bg"}">
                  <b>${escapeHtml(item.name)}</b>
                  <small>${formatNumber(item.change_pct)}%${item.leading_stock ? ` · 领涨 ${escapeHtml(item.leading_stock)}` : ""}</small>
                  <em>${escapeHtml(item.match_reason || item.source || "概念成分匹配")}</em>
                </span>`
              )
              .join("")
          : `<span><b>概念待确认</b><small>等待公开源或本地缓存补齐。</small></span>`
      }
    </div>
    <div class="theme-grid">
      <div>
        <strong>机会</strong>
        ${(report.opportunities || []).slice(0, 3).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
      </div>
      <div>
        <strong>风险</strong>
        ${(report.risks || []).slice(0, 3).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
      </div>
    </div>
    ${(report.evidence || []).slice(0, 2).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}
    ${(report.missing_data || []).length ? `<small>待补：${escapeHtml(report.missing_data.join("、"))}</small>` : ""}
  `;
}

function renderReplay(replay) {
  const el = $("replayPanel");
  if (!el || !replay) {
    if (el) el.innerHTML = "";
    return;
  }
  const replayHeadline = Number(replay.sample_count || 0) >= 5 ? `样本有效率 ${formatNumber(replay.success_rate, 1)}%` : "样本偏少";
  el.innerHTML = `
    <div class="replay-head">
      <strong>${escapeHtml(replayHeadline)}</strong>
      <span>样本 ${escapeHtml(replay.sample_count)} / ${escapeHtml(replay.window_days)} 日</span>
    </div>
    <p>${escapeHtml(replay.summary)}</p>
    <div class="replay-stats">
      ${(replay.pattern_stats || [])
        .slice(0, 4)
        .map(
          (item) => `
          <div>
            <strong>${escapeHtml(item.pattern)}</strong>
            <span>${escapeHtml(item.sample_count)}次 · 胜率 ${formatNumber(item.win_rate, 1)}% · 5日 ${formatNumber(item.avg_forward_5d_return)}%</span>
            <small>${escapeHtml(item.note)}</small>
          </div>`
        )
        .join("") || `<div><strong>暂无样本</strong><span>等待更多历史信号。</span></div>`}
    </div>
    <div class="replay-cases">
      ${(replay.cases || [])
        .slice(-5)
        .map(
          (item) => `
          <span>
            <b>${escapeHtml(item.date)} · ${escapeHtml(item.pattern)} · ${escapeHtml(item.outcome)}</b>
            <small>3日 ${item.forward_3d_return === null || item.forward_3d_return === undefined ? "--" : formatNumber(item.forward_3d_return)}% / 5日 ${item.forward_5d_return === null || item.forward_5d_return === undefined ? "--" : formatNumber(item.forward_5d_return)}%</small>
          </span>`
        )
        .join("")}
    </div>
  `;
}

function renderInsightOverview(overview) {
  $("insightOverview").innerHTML = `
    <div class="overview-score">
      <div>
        <span>全景评分</span>
        <strong>${escapeHtml(overview.total_score)}<small>/100</small></strong>
      </div>
      <i>${escapeHtml(overview.total_level)}</i>
    </div>
    <div class="overview-content">
      <strong>主要矛盾</strong>
      <p>${escapeHtml(overview.main_conflict)}</p>
      <div class="takeaways">
        ${(overview.beginner_takeaways || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
      <div class="key-price-list">
        ${(overview.key_prices || [])
          .map(
            (item) => `
            <div>
              <span>${escapeHtml(item.label)}</span>
              <strong>${formatNumber(item.price)}</strong>
              <small>${escapeHtml(item.note)}</small>
            </div>`
          )
          .join("")}
      </div>
    </div>
  `;
}

function renderFactors(items) {
  $("factorList").innerHTML = items
    .map(
      (item) => `
        <div class="factor-item">
          <div class="factor-head">
            <strong>${escapeHtml(item.name)}</strong>
            <span>${escapeHtml(item.score)} · ${escapeHtml(item.level)}</span>
          </div>
          <div class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></i></div>
          <p>${escapeHtml(item.summary)}</p>
          ${(item.evidence || []).map((text) => `<small>${escapeHtml(text)}</small>`).join("")}
          ${(item.missing_data || []).length ? `<em>待补充：${escapeHtml(item.missing_data.join("、"))}</em>` : ""}
        </div>`
    )
    .join("");
}

function renderFundFlow(flow) {
  $("fundFlowPanel").innerHTML = `
    <div class="flow-head">
      <strong>量价热度 ${escapeHtml(flow.overall_score)} · ${escapeHtml(flow.level)}</strong>
      <span>${escapeHtml(flow.source)}</span>
    </div>
    <p>${escapeHtml(flow.price_volume_relation)}</p>
    <div class="flow-windows">
      ${(flow.windows || [])
        .map(
          (item) => `
          <div>
            <span>${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(item.score)}</strong>
            <small>${escapeHtml(item.summary)}</small>
          </div>`
        )
        .join("")}
    </div>
    ${(flow.notes || []).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderOrderPressure(order) {
  $("orderPressurePanel").innerHTML = `
    <div class="flow-head">
      <strong>${escapeHtml(order.pressure_level)}</strong>
      <span>${escapeHtml(order.source)}</span>
    </div>
    <p>${escapeHtml(order.summary)}</p>
    <div class="mini-metrics">
      <span>买卖比：${order.bid_ask_ratio === null || order.bid_ask_ratio === undefined ? "--" : escapeHtml(order.bid_ask_ratio)}</span>
      <span>价差：${order.spread_pct === null || order.spread_pct === undefined ? "--" : `${formatNumber(order.spread_pct, 4)}%`}</span>
    </div>
    ${(order.notes || []).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderFinancialHealth(health) {
  $("financialPanel").innerHTML = `
    <div class="finance-head">
      <strong>财务体检 ${escapeHtml(health.score)} · ${escapeHtml(health.level)}</strong>
      <span>${escapeHtml(health.source)}</span>
    </div>
    <p>${escapeHtml(health.summary)}</p>
    <div class="metric-stack">
      ${(health.metrics || [])
        .slice(0, 5)
        .map(
          (item) => `
          <div>
            <strong>${escapeHtml(item.name)} <span>${escapeHtml(item.value)}</span></strong>
            <small>${escapeHtml(item.summary)}</small>
          </div>`
        )
        .join("")}
    </div>
  `;
}

function renderValuation(valuation) {
  $("valuationPanel").innerHTML = `
    <div class="finance-head">
      <strong>估值 ${escapeHtml(valuation.score)} · ${escapeHtml(valuation.level)}</strong>
      <span>${escapeHtml(valuation.market_cap_text || valuation.source)}</span>
    </div>
    <p>${escapeHtml(valuation.summary)}</p>
    <div class="mini-metrics">
      <span>PE：${valuation.pe === null || valuation.pe === undefined ? "--" : formatNumber(valuation.pe)}</span>
      <span>PB：${valuation.pb === null || valuation.pb === undefined ? "--" : formatNumber(valuation.pb)}</span>
      <span>${escapeHtml(valuation.valuation_anchor_label || "历史锚待确认")}：PE ${valuation.pe_percentile === null || valuation.pe_percentile === undefined ? "--" : `${formatNumber(valuation.pe_percentile, 1)}%`} / PB ${valuation.pb_percentile === null || valuation.pb_percentile === undefined ? "--" : `${formatNumber(valuation.pb_percentile, 1)}%`}</span>
      <span>同行分位：PE ${valuation.peer_pe_percentile === null || valuation.peer_pe_percentile === undefined ? "--" : `${formatNumber(valuation.peer_pe_percentile, 1)}%`} / PB ${valuation.peer_pb_percentile === null || valuation.peer_pb_percentile === undefined ? "--" : `${formatNumber(valuation.peer_pb_percentile, 1)}%`} · 样本 ${escapeHtml(valuation.peer_sample_count || 0)}</span>
      <span>价格位置：${valuation.price_percentile === null || valuation.price_percentile === undefined ? "--" : `${formatNumber(valuation.price_percentile, 1)}%`}</span>
    </div>
    ${(valuation.evidence || []).slice(0, 2).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
    ${(valuation.watch_points || []).slice(0, 3).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
  `;
}

function renderAbnormalEvents(summary) {
  $("abnormalPanel").innerHTML = `
    <div class="finance-head">
      <strong>${escapeHtml(summary.main_signal)} · ${escapeHtml(summary.level)}</strong>
      <span>评分 ${escapeHtml(summary.score)}</span>
    </div>
    <div class="event-list compact-list">
      ${(summary.events || []).length
        ? summary.events
            .slice(0, 4)
            .map(
              (item) => `
              <div class="stock-event">
                <strong>${escapeHtml(item.title)}<span>${escapeHtml(item.level)}</span></strong>
                <small>${escapeHtml(item.direction)} · ${escapeHtml(item.date)}</small>
                <p>${escapeHtml(item.description)}</p>
              </div>`
            )
            .join("")
        : `<div class="stock-event"><strong>暂无明显异动</strong><p>当前未触发放量、跳空、长影线或涨跌停附近信号。</p></div>`}
    </div>
  `;
}

function renderLhb(lhb) {
  $("lhbPanel").innerHTML = `
    <div class="finance-head">
      <strong>龙虎榜 ${escapeHtml(lhb.available ? "已接入" : "待接入")} · ${escapeHtml(lhb.level)}</strong>
      <span>${escapeHtml(lhb.source)}</span>
    </div>
    <p>${escapeHtml(lhb.summary)}</p>
    ${(lhb.reasons || []).slice(0, 4).map((item) => `<small>${escapeHtml(item)}</small>`).join("")}
    ${(lhb.action_items || []).length ? `<div class="event-actions">${lhb.action_items.slice(0, 3).map((item) => `<em>${escapeHtml(item)}</em>`).join("")}</div>` : ""}
    ${lhb.reliability ? `<small>可靠性：${escapeHtml(lhb.reliability)}</small>` : ""}
  `;
}

function renderRuleMatches(summary) {
  const matches = summary.matches || [];
  $("ruleMatches").innerHTML = matches.length
    ? matches
        .slice(0, 8)
        .map(
          (item) => `
          <article class="rule-item">
            <div>
              <strong>${escapeHtml(item.name)}</strong>
              <span class="tag ${item.level === "风险" ? "risk" : item.level === "积极" ? "good" : ""}">${escapeHtml(item.status)} · ${escapeHtml(item.level)}</span>
            </div>
            <p>${escapeHtml(item.reason)}</p>
            <small>${escapeHtml((item.actions || [])[0] || item.invalidation)}</small>
          </article>`
        )
        .join("")
    : `<article class="rule-item"><strong>暂无规则</strong><p>内置规则正在等待数据。</p></article>`;
}

function renderStrategyCards(items) {
  $("strategyCards").innerHTML = items
    .map(
      (item) => `
      <article class="strategy-card">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <span class="tag ${item.level === "风险" ? "risk" : item.level === "积极" ? "good" : ""}">${escapeHtml(item.status)} · ${escapeHtml(item.level)}</span>
        </div>
        <p>${escapeHtml((item.current_evidence || [])[0] || "")}</p>
        <dl>
          <dt>参考价</dt><dd>${escapeHtml(item.reference_price)}</dd>
          <dt>失效</dt><dd>${escapeHtml(item.invalidation)}</dd>
          <dt>适合</dt><dd>${escapeHtml(item.suitable_for)}</dd>
        </dl>
      </article>`
    )
    .join("");
}

function renderMinuteAnalysis(report) {
  const el = $("minuteAnalysis");
  if (!el || !report) return;
  const tPlan = report.t_plan || {};
  const supports = report.supports || [];
  const resistances = report.resistances || [];
  const warnings = report.warnings || [];
  const missing = report.missing_data || [];
  const statusTone = tPlan.suitability === "仅底仓可做T" ? "good" : tPlan.suitability === "不适合主动做T" ? "risk" : missing.length ? "warn" : "";
  if (missing.length && Number(report.sample_count) === 0) {
    el.innerHTML = `
      <div class="minute-status-card ${statusTone}">
        <div>
          <strong>分钟做T已暂停</strong>
          <span>${escapeHtml(report.summary || "分钟K线暂不可用，当前不按盘中区间做T。")}</span>
        </div>
        <i class="tag risk">${escapeHtml(tPlan.suitability || "不适合主动做T")}</i>
      </div>
      <div class="minute-empty">
        <strong>缺失数据：${escapeHtml(missing.join("、"))}</strong>
        ${(tPlan.execution_steps || []).slice(0, 2).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
        ${(tPlan.stop_conditions || []).slice(0, 2).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
    `;
    return;
  }
  el.innerHTML = `
    <div class="minute-head">
      <div>
        <strong>${escapeHtml(report.trend_label)} · ${escapeHtml(report.momentum_label)}</strong>
        <span>${escapeHtml(report.interval)} · 样本 ${escapeHtml(report.sample_count)} · ${escapeHtml(report.source)}</span>
      </div>
      <i class="${statusTone}">${escapeHtml(tPlan.suitability || "待确认")}</i>
    </div>
    <p>${escapeHtml(report.summary || "")}</p>
    <div class="minute-metrics">
      <span>最新价 <b>${formatNumber(report.latest_price)}</b></span>
      <span>区间涨跌 <b class="${changeClass(report.intraday_change_pct)}">${formatNumber(report.intraday_change_pct)}%</b></span>
      <span>盘中振幅 <b>${formatNumber(report.intraday_range_pct)}%</b></span>
      <span>量能 <b>${escapeHtml(report.volume_pulse || "--")}</b></span>
    </div>
    <div class="minute-zones">
      <div>
        <strong>低吸参考</strong>
        <b>${escapeHtml(tPlan.low_zone || "--")}</b>
        ${supports.slice(0, 2).map((item) => `<span>${escapeHtml(item.label)} ${formatNumber(item.price)} · 强度 ${escapeHtml(item.strength)}</span>`).join("")}
      </div>
      <div>
        <strong>高抛参考</strong>
        <b>${escapeHtml(tPlan.high_zone || "--")}</b>
        ${resistances.slice(0, 2).map((item) => `<span>${escapeHtml(item.label)} ${formatNumber(item.price)} · 强度 ${escapeHtml(item.strength)}</span>`).join("")}
      </div>
    </div>
    <div class="minute-steps">
      ${(tPlan.execution_steps || []).slice(0, 3).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      ${(tPlan.stop_conditions || []).slice(0, 2).map((item) => `<span class="risk">${escapeHtml(item)}</span>`).join("")}
      ${warnings.slice(0, 3).map((item) => `<span class="${statusTone === "risk" ? "risk" : "warn"}">${escapeHtml(item)}</span>`).join("")}
    </div>
  `;
}

function renderStockEvents(summary) {
  $("stockEvents").innerHTML = summary.events.length
    ? summary.events
        .map(
          (item) => `
          <div class="stock-event">
            <strong>${escapeHtml(item.title)}<span>${escapeHtml(item.level)}</span></strong>
            <small>${escapeHtml(item.date)} · ${escapeHtml(item.category)} · ${escapeHtml(item.source)}</small>
            <p>${escapeHtml(item.description)}</p>
            ${item.reliability ? `<small>可靠性：${escapeHtml(item.reliability)}</small>` : ""}
            ${item.action_hint ? `<em>${escapeHtml(item.action_hint)}</em>` : ""}
          </div>`
        )
        .join("")
    : `<div class="stock-event"><strong>暂无事件</strong><p>等待更多行情、公告或研报数据。</p></div>`;
  if ((summary.next_steps || []).length || (summary.missing_sources || []).length) {
    $("stockEvents").innerHTML += `
      <div class="stock-event event-followup">
        <strong>下一步核查<span>清单</span></strong>
        ${(summary.next_steps || []).slice(0, 4).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
        ${(summary.missing_sources || []).length ? `<small>待补数据：${summary.missing_sources.map(escapeHtml).join(" / ")}</small>` : ""}
      </div>`;
  }
}

function renderQuality(quality) {
  if (!quality) {
    $("qualityPanel").innerHTML = "";
    return;
  }
  const notes = quality.notes || [];
  const anomalies = quality.anomalies || [];
  const el = $("qualityPanel");
  el.className = `quality-panel ${toneByScore(quality.score, 85, 70)}`;
  el.innerHTML = `
    <div class="quality-head">
      <strong>${escapeHtml(quality.level)} · ${escapeHtml(quality.score)}分</strong>
      <span>${escapeHtml(quality.consistency_level)} · ${escapeHtml(quality.source)}</span>
    </div>
    <div class="quality-notes">
      ${notes.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      ${anomalies.length ? `<span class="warn">异常：${escapeHtml(anomalies.join("；"))}</span>` : ""}
    </div>
  `;
}

function renderSignalEvidence(snapshot) {
  if (!snapshot) {
    $("signalEvidence").innerHTML = "";
    return;
  }
  const groups = [
    ["加分依据", snapshot.positive || [], "good"],
    ["风险扣分", snapshot.negative || [], "risk"],
    ["中性观察", snapshot.neutral || [], ""],
  ];
  $("signalEvidence").innerHTML = `
    <div class="evidence-head">
      <strong>本次结论依据</strong>
      <span>${escapeHtml(snapshot.label)} · 可信度 ${escapeHtml(snapshot.confidence)}%</span>
    </div>
    <p>${escapeHtml(snapshot.summary)}</p>
    <div class="evidence-grid">
      ${groups
        .map(
          ([title, items, tone]) => `
          <div class="evidence-group">
            <strong>${escapeHtml(title)}</strong>
            ${
              items.length
                ? items
                    .map(
                      (item) => `
                      <span class="${tone}">
                        <b>${escapeHtml(item.name)} ${Number(item.impact) > 0 ? "+" : ""}${escapeHtml(item.impact)}</b>
                        <small>${escapeHtml(item.reason)}</small>
                      </span>`
                    )
                    .join("")
                : `<span><b>暂无</b><small>当前没有明显${escapeHtml(title)}。</small></span>`
            }
          </div>`
        )
        .join("")}
    </div>
    ${
      (snapshot.risk_notes || []).length
        ? `<div class="evidence-risks">${snapshot.risk_notes.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`
        : ""
    }
  `;
}

function renderReview(review) {
  if (!review) {
    $("reviewSummary").textContent = "历史复盘暂不可用。";
    $("reviewPoints").innerHTML = "";
    $("reviewEvents").innerHTML = "";
    return;
  }
  $("reviewSummary").textContent = review.review_summary;
  $("reviewPoints").innerHTML = review.key_points
    .map(
      (item) => `
      <div class="review-point">
        <span>${escapeHtml(item.label)}</span>
        <strong class="${item.level === "风险" ? "down" : item.level === "积极" ? "up" : ""}">${item.value}</strong>
      </div>`
    )
    .join("");
  $("reviewEvents").innerHTML = review.events.length
    ? review.events
        .map(
          (item) => `
          <div class="review-event">
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.date)} · ${escapeHtml(item.level)}</span>
            <p>${escapeHtml(item.description)}</p>
          </div>`
        )
        .join("")
    : `<div class="review-event"><strong>暂无异常事件</strong><p>近阶段没有触发明显大涨、大跌或高波动事件。</p></div>`;
}

function renderSignals(id, items) {
  $(id).innerHTML = items
    .map((item) => {
      const tagClass = item.level === "风险" ? "risk" : item.level === "积极" ? "good" : "";
      return `
        <article class="signal-item">
          <strong>${escapeHtml(item.title)}<span class="tag ${tagClass}">${escapeHtml(item.level)}</span></strong>
          <p>${escapeHtml(item.reason)}</p>
        </article>`;
    })
    .join("");
}

function renderMarket(indices) {
  $("marketStrip").innerHTML = indices.length
    ? indices
        .map(
          (item) => `
      <div class="index-card">
        <span>${escapeHtml(item.name)}</span>
        <strong>${formatNumber(item.price)}</strong>
        <em class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</em>
      </div>`
        )
        .join("")
    : `<div class="index-card"><span>市场概览</span><strong>暂无数据</strong><em>等待刷新</em></div>`;
}

function renderStrongStocks(items, meta = {}) {
  const scope = meta && meta.scope ? `${meta.scope} · 样本 ${meta.sample_count || items.length}` : `样本 ${items.length}`;
  $("leaderList").innerHTML = items.length
    ? `<div class="leader-row leader-scope"><strong>${escapeHtml(scope)}</strong><span>仅代表当前样本内排序，不是全市场涨幅榜。</span></div>` +
      items
        .slice(0, 8)
        .map(
          (item) => `
      <div class="leader-row">
        <div>
          <strong>${escapeHtml(item.name)} <span>${escapeHtml(item.code)}</span></strong>
          <small>${escapeHtml(item.reason)}</small>
          <small>${(item.tags || []).map((tag) => escapeHtml(tag)).join(" / ")}</small>
        </div>
        <div>
          <div class="leader-rank">${escapeHtml(item.rank)}</div>
          <span>龙头 ${escapeHtml(item.leader_score || 0)}</span>
          <span class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</span>
        </div>
      </div>`
        )
        .join("")
    : `<div class="leader-row"><strong>暂无观察池排序</strong><span>等待行情刷新后重新计算。</span></div>`;
}

function renderQuotes(items) {
  $("quoteList").innerHTML = items.length
    ? items
        .map(
          (item) => `
      <div class="quote-row">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <span>${escapeHtml(item.market)}${escapeHtml(item.code)} · 成交额 ${formatAmount(item.amount)}</span>
        </div>
        <div>
          <strong>${formatNumber(item.price)}</strong>
          <span class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</span>
        </div>
      </div>`
        )
        .join("")
    : `<div class="quote-row"><strong>实时观察等待中</strong><span>行情连接成功后自动更新。</span></div>`;
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
    const rows = JSON.parse(event.data);
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
  const canvas = $("klineCanvas");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  if (!rows || rows.length === 0) return;
  const data = rows.slice(-60);
  const padding = { left: 46, right: 16, top: 18, bottom: 28 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const maxPrice = Math.max(...data.map((item) => item.high), ma5 || 0, ma20 || 0);
  const minPrice = Math.min(...data.map((item) => item.low), ma5 || Infinity, ma20 || Infinity);
  const range = Math.max(0.01, maxPrice - minPrice);
  const xStep = chartWidth / data.length;
  const candleWidth = Math.max(4, Math.min(12, xStep * 0.58));
  const y = (price) => padding.top + (maxPrice - price) / range * chartHeight;

  ctx.strokeStyle = "#e6ebf2";
  ctx.lineWidth = 1;
  ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.fillStyle = "#667085";
  for (let i = 0; i <= 4; i++) {
    const py = padding.top + (chartHeight / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, py);
    ctx.lineTo(width - padding.right, py);
    ctx.stroke();
    const label = maxPrice - (range / 4) * i;
    ctx.fillText(formatNumber(label), 6, py + 4);
  }

  data.forEach((item, index) => {
    const x = padding.left + xStep * index + xStep / 2;
    const up = item.close >= item.open;
    ctx.strokeStyle = up ? "#d92d20" : "#0f9f6e";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(x, y(item.high));
    ctx.lineTo(x, y(item.low));
    ctx.stroke();
    const top = y(Math.max(item.open, item.close));
    const bottom = y(Math.min(item.open, item.close));
    ctx.fillRect(x - candleWidth / 2, top, candleWidth, Math.max(2, bottom - top));
  });

  drawPriceLine(ctx, data, 5, "#2563eb", y, padding.left, xStep);
  drawPriceLine(ctx, data, 20, "#b7791f", y, padding.left, xStep);
  drawChartMarks(ctx, data, y, padding.left, xStep, height);

  ctx.fillStyle = "#667085";
  ctx.fillText(data[0].date.slice(5), padding.left, height - 8);
  ctx.fillText(data[data.length - 1].date.slice(5), width - padding.right - 38, height - 8);
}

function drawChartMarks(ctx, data, y, left, xStep, height) {
  const marks = (state.chartMarks || []).filter((mark) => mark.visible !== false && state.activeMarkCategories.has(mark.category));
  if (!marks.length) return;
  const byDate = new Map();
  data.forEach((item, index) => {
    byDate.set(String(item.date).slice(0, 10), { item, index });
  });
  marks.slice(0, 18).forEach((mark) => {
    const key = String(mark.kline_date || mark.date || "").slice(0, 10);
    const target = byDate.get(key) || byDate.get(key.replaceAll("/", "-"));
    if (!target) return;
    const x = left + xStep * target.index + xStep / 2;
    const price = mark.price || target.item.close;
    const py = Math.max(18, Math.min(height - 38, y(price)));
    ctx.fillStyle = mark.color || (mark.level === "风险" ? "#0f9f6e" : mark.level === "积极" ? "#d92d20" : "#b7791f");
    ctx.beginPath();
    ctx.arc(x, py, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#344054";
    ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.fillText(String(mark.label || mark.category).slice(0, 6), x + 6, py - 6);
  });
}

function drawPriceLine(ctx, data, windowSize, color, y, left, xStep) {
  if (data.length < windowSize) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  data.forEach((item, index) => {
    if (index < windowSize - 1) return;
    const slice = data.slice(index - windowSize + 1, index + 1);
    const avg = slice.reduce((sum, row) => sum + row.close, 0) / windowSize;
    const x = left + xStep * index + xStep / 2;
    const py = y(avg);
    if (index === windowSize - 1) ctx.moveTo(x, py);
    else ctx.lineTo(x, py);
  });
  ctx.stroke();
}

$("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  state.symbol = $("symbolInput").value.trim() || "600519";
  loadAll();
});

$("quickList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-symbol]");
  if (!button) return;
  state.symbol = button.dataset.symbol;
  $("symbolInput").value = state.symbol;
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
    await addWatchlistItem();
  } catch (error) {
    $("watchList").innerHTML = `<div class="watch-row"><strong>加入失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("watchList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const symbol = button.dataset.symbol;
  if (button.dataset.action === "open") {
    state.symbol = symbol;
    $("symbolInput").value = symbol.slice(0, 6);
    $("watchSymbolInput").value = symbol.slice(0, 6);
    loadAll();
    return;
  }
  if (button.dataset.action === "remove") {
    await runButtonTask(button, () => removeWatchlistItem(symbol));
  }
});

$("alertForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await addAlertRule();
  } catch (error) {
    $("alertList").innerHTML = `<div class="alert-row"><strong>添加失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("evaluateAlerts").addEventListener("click", async () => {
  try {
    await evaluateAlerts();
  } catch (error) {
    $("alertEvents").innerHTML = `<div class="alert-event"><strong>检查失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("alertList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-alert-toggle]");
  if (toggleButton) {
    await runButtonTask(toggleButton, () => updateAlertRule(toggleButton.dataset.alertToggle, { enabled: toggleButton.dataset.alertEnabled === "true" }));
    return;
  }
  const button = event.target.closest("button[data-alert-remove]");
  if (!button) return;
  await runButtonTask(button, () => removeAlertRule(button.dataset.alertRemove));
});

$("noteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await addStockNote();
  } catch (error) {
    $("noteList").innerHTML = `<div class="note-row"><strong>保存失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
});

$("noteList").addEventListener("click", async (event) => {
  const toggleButton = event.target.closest("button[data-note-toggle]");
  if (toggleButton) {
    await runButtonTask(toggleButton, () => updateStockNote(toggleButton.dataset.noteToggle, { visible: toggleButton.dataset.noteVisible === "true" }));
    return;
  }
  const button = event.target.closest("button[data-note-remove]");
  if (!button) return;
  await runButtonTask(button, () => removeStockNote(button.dataset.noteRemove));
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
  runMonitorTask(button.dataset.task);
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
  loadMonitoring();
  if (state.lastAnalysis) startStream();
});

loadAll();
