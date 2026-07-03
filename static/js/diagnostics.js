import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { compactErrorMessage } from "./errors.js";
import { formatNumber } from "./format.js";

export async function loadMonitoring(state) {
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
    state.monitorTimer = setInterval(() => loadMonitoring(state), 15000);
  }
}

export async function runMonitorTask(state, task) {
  const buttons = document.querySelectorAll(".monitor-actions button");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    $("schedulerState").textContent = "执行中";
    await fetchJson(`/api/tasks/run-once?task=${encodeURIComponent(task)}`, { method: "POST" });
  } catch (error) {
    $("schedulerState").textContent = compactErrorMessage(error.message);
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
    });
    loadMonitoring(state);
    loadDataStatus();
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
  $("schedulerState").textContent = schedulerStateText(status);
  const runMap = schedulerRunMap(runs);
  $("taskCards").innerHTML = (status.tasks || []).map((task) => renderTaskCard(task, runMap.get(task.name))).join("");
}

function schedulerStateText(status) {
  if (status.running) return "运行中";
  return status.enabled ? "已启用" : "已关闭";
}

function schedulerRunMap(runs) {
  return new Map((runs || []).map((item) => [item.task_name, item]));
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
  if (task.last_status === "failed") return "bad";
  if (task.running) return "running";
  return "";
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
        .map((item) => {
          const repeat = item.repeat_count && item.repeat_count > 1 ? ` · 重复 ${item.repeat_count} 次` : "";
          const seenAt = item.last_seen_at || item.created_at;
          return `
          <div class="monitor-event ${item.level === "warning" ? "warn" : ""}">
            <strong>${escapeHtml(eventCategory(item.category))}${item.symbol ? ` · ${escapeHtml(item.symbol)}` : ""}</strong>
            <span>${escapeHtml(seenAt)}${escapeHtml(repeat)}</span>
            <p>${escapeHtml(item.message)}</p>
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
  if (!plan) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <div class="source-plan-head ${sourcePlanTone(plan)}">
      <strong>${escapeHtml(plan.health_level)}</strong>
      <span>${escapeHtml(plan.summary)}</span>
    </div>
    <div class="source-plan-sources">${renderSourcePlanSources(plan)}</div>
    ${renderSourcePlanWarnings(plan.warnings)}
    <div class="source-plan-actions">${renderSourcePlanSuggestions(plan.suggestions)}</div>
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
  const rows = (warnings || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  return rows ? `<div class="source-plan-warnings">${rows}</div>` : "";
}

function renderSourcePlanSuggestions(suggestions) {
  return (suggestions || [])
    .slice(0, 2)
    .map((item) => `<small>${escapeHtml(item)}</small>`)
    .join("");
}
