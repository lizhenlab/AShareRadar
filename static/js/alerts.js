import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export async function loadAlerts(state, options = {}) {
  const symbol = options.symbol || state.symbol;
  const isCurrent = options.isCurrent || (() => true);
  const [rulesResult, eventsResult] = await Promise.allSettled([
    fetchJson(`/api/alerts?symbol=${encodeURIComponent(symbol)}`),
    fetchJson(`/api/alerts/events?symbol=${encodeURIComponent(symbol)}&limit=6`),
  ]);
  if (!isCurrent()) return false;
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
  return true;
}

export async function addAlertRule(state, options = {}) {
  const symbol = options.symbol || state.symbol;
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
      symbol,
      condition_type: conditionType,
      threshold,
      note: "本地个股研究提醒",
    }),
  });
  if (options.isCurrent && !options.isCurrent()) return false;
  $("alertThreshold").value = "";
  await loadAlerts(state, { symbol, isCurrent: options.isCurrent });
  return true;
}

export async function evaluateAlerts(state, options = {}) {
  const symbol = options.symbol || state.symbol;
  const button = $("evaluateAlerts");
  if (button.disabled) return false;
  try {
    button.disabled = true;
    button.textContent = "检查中";
    const result = await fetchJson(`/api/alerts/evaluate?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
    if (options.isCurrent && !options.isCurrent()) return false;
    renderAlertEvaluation(result);
    await loadAlerts(state, { symbol, isCurrent: options.isCurrent });
    return true;
  } finally {
    button.disabled = false;
    button.textContent = "检查";
  }
}

export async function removeAlertRule(state, ruleId, options = {}) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, { method: "DELETE" });
  if (options.isCurrent && !options.isCurrent()) return false;
  await loadAlerts(state, { symbol: options.symbol || state.symbol, isCurrent: options.isCurrent });
  return true;
}

export async function updateAlertRule(state, ruleId, payload, options = {}) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (options.isCurrent && !options.isCurrent()) return false;
  await loadAlerts(state, { symbol: options.symbol || state.symbol, isCurrent: options.isCurrent });
  return true;
}

export function renderAlerts(items) {
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

export function renderAlertEvents(items) {
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

export function renderAlertEvaluation(result) {
  const failedCount = Number(result.failed_count || 0);
  const completedCount = Math.max(0, Number(result.checked_count || 0) - failedCount);
  $("alertEvents").innerHTML = `
    <div class="alert-event ${failedCount ? "is-warning" : ""}">
      <strong>${failedCount ? "检查部分完成" : "检查完成"}</strong>
      <span>${escapeHtml(result.checked_at)} · 成功 ${escapeHtml(completedCount)} / ${escapeHtml(result.checked_count)} · 触发 ${escapeHtml(result.triggered_count)}</span>
      <p>新增触发记录 ${escapeHtml(result.new_event_count)} 条${failedCount ? `，失败 ${escapeHtml(failedCount)} 条，请稍后重试。` : "。"}</p>
    </div>
  `;
}
