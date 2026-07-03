import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export async function loadAlerts(state) {
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

export async function addAlertRule(state) {
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
  await loadAlerts(state);
}

export async function evaluateAlerts(state) {
  const button = $("evaluateAlerts");
  try {
    button.disabled = true;
    button.textContent = "检查中";
    const result = await fetchJson(`/api/alerts/evaluate?symbol=${encodeURIComponent(state.symbol)}`, { method: "POST" });
    renderAlertEvaluation(result);
    await loadAlerts(state);
  } finally {
    button.disabled = false;
    button.textContent = "检查";
  }
}

export async function removeAlertRule(state, ruleId) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, { method: "DELETE" });
  await loadAlerts(state);
}

export async function updateAlertRule(state, ruleId, payload) {
  await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadAlerts(state);
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
  $("alertEvents").innerHTML = `
    <div class="alert-event">
      <strong>检查完成</strong>
      <span>${escapeHtml(result.checked_at)} · 触发 ${escapeHtml(result.triggered_count)} / ${escapeHtml(result.checked_count)}</span>
      <p>新增触发记录 ${escapeHtml(result.new_event_count)} 条。</p>
    </div>
  `;
}
