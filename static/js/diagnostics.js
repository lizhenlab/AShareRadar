import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { compactErrorMessage } from "./errors.js";
import { formatNumber } from "./format.js";

export async function loadMonitoring(state) {
  const requestId = Number(state.monitorSeq || 0) + 1;
  state.monitorSeq = requestId;
  try {
    const [statusResult, runsResult, eventsResult] = await Promise.allSettled([
      fetchJson("/api/tasks/status"),
      fetchJson("/api/tasks/runs?limit=8"),
      fetchJson("/api/monitor/events?limit=8"),
    ]);
    if (!isCurrentMonitoringRequest(state, requestId)) return false;
    renderSchedulerResult(statusResult, runsResult);
    renderMonitorEventsResult(eventsResult);
    return true;
  } finally {
    if (isCurrentMonitoringRequest(state, requestId)) {
      maintainMonitorTimer(state);
    }
  }
}

function isCurrentMonitoringRequest(state, requestId) {
  return state.monitorSeq === requestId;
}

function renderSchedulerResult(statusResult, runsResult) {
  if (statusResult.status === "fulfilled") {
    try {
      renderSchedulerStatus(statusResult.value, runsResult.status === "fulfilled" ? runsResult.value : []);
      return;
    } catch (error) {
      renderSchedulerError(error);
      return;
    }
  }
  renderSchedulerError(statusResult.reason);
}

function renderSchedulerError(error) {
  $("schedulerState").textContent = "读取失败";
  $("taskCards").innerHTML = `<div class="task-card"><strong>监控暂不可用</strong><span>${escapeHtml(errorMessage(error))}</span></div>`;
}

function renderMonitorEventsResult(eventsResult) {
  if (eventsResult.status === "fulfilled") {
    try {
      renderMonitorEvents(eventsResult.value);
      return;
    } catch (error) {
      renderMonitorEventsError(error);
      return;
    }
  }
  renderMonitorEventsError(eventsResult.reason);
}

function renderMonitorEventsError(error) {
  $("monitorEvents").innerHTML = `<div class="monitor-event warn"><strong>事件读取失败</strong><p>${escapeHtml(errorMessage(error))}</p></div>`;
}

function maintainMonitorTimer(state) {
  if (state.monitorTimer && !document.hidden) {
    clearInterval(state.monitorTimer);
    state.monitorTimer = null;
  }
  if (!state.monitorTimer && !document.hidden) {
    state.monitorTimer = setInterval(() => loadMonitoring(state), 15000);
  }
}

export async function runMonitorTask(state, task) {
  if (state.monitorTaskRunning) return false;
  state.monitorTaskRunning = true;
  const buttons = document.querySelectorAll(".monitor-actions button");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    $("schedulerState").textContent = "执行中";
    await fetchJson(`/api/tasks/run-once?task=${encodeURIComponent(task)}`, { method: "POST" });
    await loadMonitoring(state);
    return true;
  } catch (error) {
    $("schedulerState").textContent = compactErrorMessage(error.message);
    return false;
  } finally {
    state.monitorTaskRunning = false;
    buttons.forEach((button) => {
      button.disabled = false;
    });
    await loadDataStatus();
  }
}

export async function loadDataStatus() {
  try {
    const status = await fetchJson("/api/data/status");
    renderDataStatus(status);
  } catch (error) {
    $("providerStatus").innerHTML = `<div class="provider-item"><strong>状态读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
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
      <i class="task-badge ${taskBadgeClass(task)}">${escapeHtml(taskStatusText(task, recent))}</i>
    </div>`;
}

function taskStatusText(task, recent) {
  if (task.running) return "执行中";
  return statusLabel(task.last_status || (recent && recent.status));
}

function taskMessage(task, recent) {
  return task.last_message || (recent && recent.message) || "等待首次运行";
}

function taskBadgeClass(task) {
  if (task.last_status === "failed" || task.last_status === "cancelled") return "bad";
  if (task.running) return "running";
  return "";
}

function statusLabel(status) {
  if (status === "success") return "正常";
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
