import {
  DEFAULT_REQUEST_TIMEOUT_MS,
  GLOBAL_DATA_TTL_MS,
  cancelCachedJsonRequest,
  createRequestScope,
  fetchCachedJson,
  fetchJson,
  getCachedJsonSnapshot,
  invalidateCachedJson,
  isAbortError,
} from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { compactErrorMessage } from "./errors.js";
import { formatNumber } from "./format.js";

const standaloneDataStatusState = {};
export const MONITORING_REFRESH_INTERVAL_MS = 15000;
export const DATA_STATUS_ENDPOINT = "/api/data/status";
export const SYSTEM_DIAGNOSTICS_ENDPOINT = "/api/system/diagnostics";
export const MONITORING_ENDPOINTS = Object.freeze([
  "/api/tasks/status",
  "/api/tasks/runs?limit=8",
  "/api/monitor/events?limit=8",
]);

export async function loadMonitoring(state, options = {}) {
  const requestId = Number(state.monitorSeq || 0) + 1;
  state.monitorSeq = requestId;
  const request = createRequestScope(state.monitorRequest, options.signal);
  state.monitorRequest = request;
  const isCurrent = () => isCurrentMonitoringRequest(state, requestId, request, options);
  try {
    const [statusResult, runsResult, eventsResult] = await Promise.allSettled([
      statusRequest(MONITORING_ENDPOINTS[0], request.signal, options),
      statusRequest(MONITORING_ENDPOINTS[1], request.signal, options),
      statusRequest(MONITORING_ENDPOINTS[2], request.signal, options),
    ]);
    if (!isCurrent() || hasAbortedResult(statusResult, runsResult, eventsResult)) return false;
    const statusLoaded = renderSchedulerStatusResult(statusResult, runsResult);
    const runsLoaded = renderSchedulerRunsResult(runsResult);
    const eventsLoaded = renderMonitorEventsResult(eventsResult);
    return statusLoaded && runsLoaded && eventsLoaded;
  } finally {
    if (isCurrent()) maintainMonitorTimer(state);
    finishRequest(state, "monitorRequest", request);
  }
}

function isCurrentMonitoringRequest(state, requestId, request, options) {
  return (
    state.monitorSeq === requestId &&
    state.monitorRequest === request &&
    !request.signal.aborted &&
    (!options.isCurrent || options.isCurrent())
  );
}

function renderSchedulerStatusResult(statusResult, runsResult) {
  const status = resultOrCachedValue(statusResult, MONITORING_ENDPOINTS[0]);
  const runs = resultOrCachedValue(runsResult, MONITORING_ENDPOINTS[1]);
  if (status.found) {
    try {
      renderSchedulerStatus(status.value, runs.found ? runs.value : null);
      return statusResult.status === "fulfilled";
    } catch (error) {
      renderSchedulerError(error);
      return false;
    }
  }
  renderSchedulerError(statusResult.reason);
  return false;
}

function renderSchedulerRunsResult(runsResult) {
  if (runsResult.status === "fulfilled") return true;
  $("taskCards").innerHTML += `<div class="task-card"><strong>运行记录读取失败</strong><span>${escapeHtml(errorMessage(runsResult.reason))}</span></div>`;
  return false;
}

function renderSchedulerError(error) {
  $("schedulerState").textContent = "读取失败";
  $("taskCards").innerHTML = `<div class="task-card"><strong>监控暂不可用</strong><span>${escapeHtml(errorMessage(error))}</span></div>`;
}

function renderMonitorEventsResult(eventsResult) {
  const events = resultOrCachedValue(eventsResult, MONITORING_ENDPOINTS[2]);
  if (events.found) {
    try {
      renderMonitorEvents(events.value);
      return eventsResult.status === "fulfilled";
    } catch (error) {
      renderMonitorEventsError(error);
      return false;
    }
  }
  renderMonitorEventsError(eventsResult.reason);
  return false;
}

function renderMonitorEventsError(error) {
  $("monitorEvents").innerHTML = `<div class="monitor-event warn"><strong>事件读取失败</strong><p>${escapeHtml(errorMessage(error))}</p></div>`;
}

function maintainMonitorTimer(state) {
  if (state.monitorTimer && document.hidden) {
    clearInterval(state.monitorTimer);
    state.monitorTimer = null;
    return;
  }
  if (!state.monitorTimer && !document.hidden) {
    state.monitorTimer = setInterval(
      () => loadMonitoring(state, { force: true }),
      MONITORING_REFRESH_INTERVAL_MS
    );
  }
}

export async function runMonitorTask(state, task, options = {}) {
  if (state.monitorTaskRunning) return false;
  state.monitorTaskRunning = true;
  const requestId = Number(state.monitorTaskSeq || 0) + 1;
  state.monitorTaskSeq = requestId;
  const request = createRequestScope(state.monitorTaskRequest, options.signal);
  state.monitorTaskRequest = request;
  const ownsRequest = () => state.monitorTaskSeq === requestId && state.monitorTaskRequest === request;
  const isCurrent = () =>
    ownsRequest() &&
    !request.signal.aborted &&
    (!options.isCurrent || options.isCurrent());
  const buttons = document.querySelectorAll(".monitor-actions button");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  let completed = false;
  try {
    $("schedulerState").textContent = "执行中";
    await fetchJson(`/api/tasks/run-once?task=${encodeURIComponent(task)}`, {
      method: "POST",
      signal: request.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!isCurrent()) return false;
    invalidateMonitoringCache();
    await loadMonitoring(state, { force: true, signal: request.signal, isCurrent });
    if (!isCurrent()) return false;
    completed = true;
    return true;
  } catch (error) {
    if (isAbortError(error) || !isCurrent()) return false;
    $("schedulerState").textContent = compactErrorMessage(error.message);
    return false;
  } finally {
    state.monitorTaskRunning = false;
    buttons.forEach((button) => {
      button.disabled = false;
    });
    if (ownsRequest() && !completed && $("schedulerState").textContent === "执行中") {
      $("schedulerState").textContent = "已取消";
    }
    if (isCurrent()) {
      invalidateDataStatusCache();
      await loadDataStatus(state, { force: true, signal: request.signal, isCurrent });
    }
    finishRequest(state, "monitorTaskRequest", request);
  }
}

export async function loadDataStatus(state = standaloneDataStatusState, options = {}) {
  const requestId = Number(state.dataStatusSeq || 0) + 1;
  state.dataStatusSeq = requestId;
  const request = createRequestScope(state.dataStatusRequest, options.signal);
  state.dataStatusRequest = request;
  const isCurrent = () =>
    state.dataStatusSeq === requestId &&
    state.dataStatusRequest === request &&
    !request.signal.aborted &&
    (!options.isCurrent || options.isCurrent());
  try {
    const status = await statusRequest(DATA_STATUS_ENDPOINT, request.signal, options);
    if (!isCurrent()) return false;
    renderDataStatus(status);
    return true;
  } catch (error) {
    if (isAbortError(error) || !isCurrent()) return false;
    const cached = getCachedJsonSnapshot(DATA_STATUS_ENDPOINT);
    if (cached.found) renderDataStatus(cached.value);
    else {
      $("providerStatus").innerHTML = `<div class="provider-item"><strong>状态读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    }
    return false;
  } finally {
    finishRequest(state, "dataStatusRequest", request);
  }
}

export async function loadSystemDiagnostics(state = standaloneDataStatusState, options = {}) {
  const requestId = Number(state.systemDiagnosticsSeq || 0) + 1;
  state.systemDiagnosticsSeq = requestId;
  const request = createRequestScope(state.systemDiagnosticsRequest, options.signal);
  state.systemDiagnosticsRequest = request;
  const isCurrent = () =>
    state.systemDiagnosticsSeq === requestId &&
    state.systemDiagnosticsRequest === request &&
    !request.signal.aborted &&
    (!options.isCurrent || options.isCurrent());
  try {
    const diagnostics = await statusRequest(SYSTEM_DIAGNOSTICS_ENDPOINT, request.signal, options);
    if (!isCurrent()) return false;
    renderSystemDiagnostics(diagnostics);
    return true;
  } catch (error) {
    if (isAbortError(error) || !isCurrent()) return false;
    renderSystemDiagnosticsUnavailable(error);
    return false;
  } finally {
    finishRequest(state, "systemDiagnosticsRequest", request);
  }
}

function statusRequest(url, signal, options = {}) {
  return fetchCachedJson(url, {
    force: Boolean(options.force),
    signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ttlMs: options.ttlMs ?? GLOBAL_DATA_TTL_MS,
  });
}

function resultOrCachedValue(result, url) {
  if (result.status === "fulfilled") return { found: true, value: result.value };
  return getCachedJsonSnapshot(url);
}

export function invalidateMonitoringCache() {
  MONITORING_ENDPOINTS.forEach((url) => invalidateCachedJson(url));
}

export function invalidateDataStatusCache() {
  invalidateCachedJson(DATA_STATUS_ENDPOINT);
  invalidateCachedJson(SYSTEM_DIAGNOSTICS_ENDPOINT);
}

export function cancelMonitoringRefresh(state) {
  if (state.monitorRequest) state.monitorRequest.abort();
  MONITORING_ENDPOINTS.forEach((url) => cancelCachedJsonRequest(url));
}

export function cancelDataStatusRefresh(state = standaloneDataStatusState) {
  if (state.dataStatusRequest) state.dataStatusRequest.abort();
  cancelCachedJsonRequest(DATA_STATUS_ENDPOINT);
}

function hasAbortedResult(...results) {
  return results.some((result) => result.status === "rejected" && isAbortError(result.reason));
}

function finishRequest(state, key, request) {
  if (state[key] === request) state[key] = null;
  request.dispose();
}

function renderSchedulerStatus(status, runs) {
  const safeStatus = asObject(status);
  $("schedulerState").textContent = schedulerStateText(safeStatus);
  const runMap = schedulerRunMap(runs);
  const tasks = asArray(safeStatus.tasks);
  $("taskCards").innerHTML = tasks.length
    ? tasks.map((task) => renderTaskCard(asObject(task), runMap.get(asObject(task).name))).join("")
    : `<div class="task-card"><strong>暂无调度任务</strong><span>等待任务注册。</span></div>`;
}

function schedulerStateText(status) {
  if (status.running) return "运行中";
  if (status.standby) return "其他实例运行中";
  return status.enabled ? "已启用" : "已关闭";
}

function schedulerRunMap(runs) {
  return new Map(asArray(runs).map((item) => [asObject(item).task_name, asObject(item)]));
}

function renderTaskCard(task, recent) {
  return `
    <div class="task-card">
      <div>
        <strong>${escapeHtml(task.display_name)}</strong>
        <span>${escapeHtml(taskMessage(task, recent))}</span>
        <small>下次：${escapeHtml(task.next_run_at || "--")}</small>
      </div>
      <i class="task-badge ${taskBadgeClass(task, recent)}">${escapeHtml(taskStatusText(task, recent))}</i>
    </div>`;
}

function taskStatusText(task, recent) {
  if (task.running) return "执行中";
  return statusLabel(task.last_status || (recent && recent.status));
}

function taskMessage(task, recent) {
  return task.last_message || (recent && recent.message) || "等待首次运行";
}

function taskBadgeClass(task, recent) {
  const status = task.last_status || (recent && recent.status);
  if (status === "failed" || status === "cancelled") return "bad";
  if (status === "degraded") return "warn";
  if (task.running) return "running";
  return "";
}

function statusLabel(status) {
  if (status === "success") return "正常";
  if (status === "degraded") return "降级";
  if (status === "failed") return "异常";
  if (status === "cancelled") return "已取消";
  if (status === "running") return "执行中";
  return "等待";
}

function renderMonitorEvents(items) {
  const rows = asArray(items);
  $("monitorEvents").innerHTML = rows.length
    ? rows
        .map((item) => {
          const event = asObject(item);
          const repeat = event.repeat_count && event.repeat_count > 1 ? ` · 重复 ${event.repeat_count} 次` : "";
          const seenAt = event.last_seen_at || event.created_at;
          return `
          <div class="monitor-event ${event.level === "warning" ? "warn" : ""}">
            <strong>${escapeHtml(eventCategory(event.category))}${event.symbol ? ` · ${escapeHtml(event.symbol)}` : ""}</strong>
            <span>${escapeHtml(seenAt)}${escapeHtml(repeat)}</span>
            <p>${escapeHtml(event.message)}</p>
          </div>`;
        })
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

function renderDataStatus(status) {
  const safeStatus = asObject(status);
  $("cachePath").textContent = "本地缓存";
  renderSourcePlan(safeStatus.source_plan);
  const capabilityStatuses = asArray(safeStatus.capability_statuses).map(asObject);
  const providers = asArray(safeStatus.providers).map(asObject);
  const cache = asObject(safeStatus.cache);
  const capabilities = asArray(safeStatus.capabilities).map(asObject);
  const capabilityStatusText = capabilityStatuses
    .filter((item) => item.enabled)
    .map((item) => `${escapeHtml(item.name)}·${escapeHtml(capabilityKindLabel(item.kind))}·${escapeHtml(capabilityHealthLabel(item))}`)
    .join(" / ");
  $("providerStatus").innerHTML = providers.length
    ? providers
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
    .join("")
    : `<div class="provider-item"><strong>暂无数据源状态</strong><span>等待数据源完成首次探测。</span></div>`;
  $("cacheStats").innerHTML = `
    <strong>缓存：报价 ${cache.quote_count ?? 0} 条，日K ${cache.daily_kline_count ?? cache.kline_count ?? 0} 条，分钟K ${cache.minute_kline_count ?? 0} 条</strong>
    <span>股票池 ${cache.stock_count ?? 0} 条 · 板块 ${cache.plate_count ?? 0} 条 · 快照历史 ${cache.quote_history_count ?? 0} 条</span>
    <span>说明：成功/失败是本地累计调用次数；状态看最近一次请求，未启用源不会参与当前分析。</span>
    <span>能力：${capabilities.length ? capabilities.map((item) => `${escapeHtml(item.name)}·${escapeHtml(item.reliability_level || "公开源")}·${item.enabled ? "可用" : "待启用"}`).join(" / ") : "等待探测"}</span>
    ${capabilityStatusText ? `<span>能力状态：${capabilityStatusText}</span>` : ""}
  `;
}

export function renderSystemDiagnostics(diagnostics) {
  const payload = asObject(diagnostics);
  const storage = asObject(payload.storage);
  const storageTarget = $("storageDiagnostics");
  const messagesTarget = $("diagnosticMessages");
  if (storageTarget) {
    const budgetMb = Number(storage.budget_bytes || 0) / 1024 / 1024;
    const usage = nonNegativePercent(storage.usage_pct);
    const meterValue = Math.min(100, usage);
    storageTarget.innerHTML = `
      <div class="storage-diagnostic-head">
        <strong>${escapeHtml(formatNumber(storage.db_size_mb || 0))} MB / ${escapeHtml(formatNumber(budgetMb))} MB</strong>
        <span>${escapeHtml(formatNumber(usage))}%</span>
      </div>
      <meter min="0" max="100" low="0" high="80" optimum="0" value="${escapeHtml(meterValue)}">${escapeHtml(usage)}%</meter>
      <div class="storage-row-counts">
        <span><b>${escapeHtml(storage.cache_rows || 0)}</b>可再生缓存</span>
        <span><b>${escapeHtml(storage.runtime_rows || 0)}</b>运行日志</span>
        <span><b>${escapeHtml(storage.user_rows || 0)}</b>用户数据</span>
      </div>`;
  }
  if (messagesTarget) {
    const warnings = asArray(payload.warnings);
    const suggestions = asArray(payload.suggestions);
    messagesTarget.innerHTML = warnings.length || suggestions.length
      ? `${warnings.map((item) => `<p class="diagnostic-warning">${escapeHtml(item)}</p>`).join("")}${suggestions.slice(0, 4).map((item) => `<p>${escapeHtml(item)}</p>`).join("")}`
      : `<p>当前未发现需要处理的系统诊断项。</p>`;
  }
}

function renderSystemDiagnosticsUnavailable(error) {
  const target = $("diagnosticMessages");
  if (target) target.innerHTML = `<p class="diagnostic-warning">诊断读取失败：${escapeHtml(errorMessage(error))}</p>`;
}

function nonNegativePercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return 0;
  return Math.round(number * 100) / 100;
}

function providerState(item) {
  if (!item.enabled) return { text: "未启用", tone: "idle" };
  if (item.healthy) return { text: "当前正常", tone: "ok" };
  return { text: "最近失败", tone: "bad" };
}

function providerDetail(item) {
  if (!item.enabled) return disabledProviderDetail(item);
  if (item.healthy) return healthyProviderDetail(item);
  return failedProviderDetail(item);
}

function disabledProviderDetail(item) {
  return item.last_error ? compactErrorMessage(item.last_error) : "未配置或主动关闭，不参与当前分析。";
}

function healthyProviderDetail(item) {
  return item.last_success ? `最近成功：${item.last_success}${providerLatencyText(item)}` : "等待首次成功请求";
}

function providerLatencyText(item) {
  return item.latency_ms === null || item.latency_ms === undefined ? "" : ` · 延迟 ${formatNumber(item.latency_ms, 0)}ms`;
}

function failedProviderDetail(item) {
  const error = compactErrorMessage(item.last_error ?? "最近一次请求失败");
  return `${error}${providerLastSuccessText(item)}`;
}

function providerLastSuccessText(item) {
  return item.last_success ? `；上次成功：${item.last_success}` : "";
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
  const safePlan = asObject(plan);
  if (!Object.keys(safePlan).length) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="source-plan-head ${sourcePlanTone(safePlan)}">
      <strong>${escapeHtml(safePlan.health_level || "待确认")}</strong>
      <span>${escapeHtml(safePlan.summary || "数据源状态等待探测。")}</span>
    </div>
    <div class="source-plan-sources">${renderSourcePlanSources(safePlan)}</div>
    ${renderSourcePlanWarnings(safePlan.warnings)}
    <div class="source-plan-actions">${renderSourcePlanSuggestions(safePlan.suggestions)}</div>
  `;
}

function sourcePlanTone(plan) {
  if (plan.health_level === "健康") return "good";
  if (plan.health_level === "高风险") return "risk";
  return "warn";
}

function renderSourcePlanSources(plan) {
  return sourcePlanSources(plan)
    .map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`)
    .join("");
}

function sourcePlanSources(plan) {
  return [
    ["报价", plan.primary_quote_source || "缺失"],
    ["日K", plan.primary_kline_source || "缺失"],
    ["分钟", plan.primary_minute_source || "缺失"],
  ];
}

function renderSourcePlanWarnings(warnings) {
  const rows = asArray(warnings).map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  return rows ? `<div class="source-plan-warnings">${rows}</div>` : "";
}

function renderSourcePlanSuggestions(suggestions) {
  return asArray(suggestions)
    .slice(0, 2)
    .map((item) => `<small>${escapeHtml(item)}</small>`)
    .join("");
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function errorMessage(error) {
  return compactErrorMessage(error && error.message ? error.message : String(error || "数据格式异常"));
}
