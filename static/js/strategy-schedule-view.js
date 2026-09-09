import { escapeHtml } from "./dom.js";

export function strategyScheduleElements(root) {
  const ids = ["Manager", "Context", "Status", "Rows", "Page", "Prev", "Next", "Refresh"];
  return Object.fromEntries(ids.map(name => [name.toLowerCase(), root.getElementById(`strategySchedule${name}`)]));
}

export function renderScheduleContext(elements, strategy) {
  elements.context.textContent = strategy
    ? `当前载入：${strategy.spec?.name || "策略"} #${strategy.strategy_id} · 编辑版本 v${strategy.strategy_version}${strategy.archived ? " · 已归档" : ""}`
    : "载入已保存策略后查看定时任务。";
}

export function renderManagedSchedules(elements, state) {
  const page = state.page;
  elements.page.textContent = page ? `第 ${page.page} / ${page.page_count || 1} 页 · 共 ${page.total} 个任务` : "尚未读取";
  elements.rows.innerHTML = page
    ? page.items.map(item => scheduleRow(item, state.strategy.archived)).join("") || "<p>此策略尚无定时任务。</p>"
    : "";
  syncScheduleControls(elements, state);
}

export function syncScheduleControls(elements, state) {
  const busy = state.reading || state.writing || state.externalBusy;
  elements.manager.setAttribute("aria-busy", String(Boolean(state.reading || state.writing)));
  elements.refresh.disabled = busy || !state.strategy;
  elements.prev.disabled = busy || state.orderStale || !state.page || state.page.page <= 1;
  elements.next.disabled = busy || state.orderStale || !state.page || state.page.page >= state.page.page_count;
  elements.rows.querySelectorAll("[data-strategy-schedule-toggle]").forEach(button => {
    button.disabled = busy || button.dataset.archivedDisabled === "true";
  });
}

export function announceScheduleStatus(elements, message, kind = "ready") {
  elements.status.textContent = message;
  elements.status.dataset.kind = kind;
}

function scheduleRow(item, archived) {
  const blocked = archived && !item.enabled;
  const source = item.mode === "official" ? "盘后正式榜单" : "盘中预览榜单";
  return `<article class="strategy-schedule-row" data-strategy-schedule-id="${item.schedule_id}">
    <div class="strategy-schedule-toolbar"><strong>任务 #${item.schedule_id} · 固定 v${item.strategy_version} · ${item.enabled ? "已启用" : "已停用"}</strong>
      <button type="button" class="mini-button" data-strategy-schedule-toggle="${item.schedule_id}" data-archived-disabled="${blocked}"${blocked ? " disabled" : ""} aria-label="${item.enabled ? "停用" : "恢复"}任务 #${item.schedule_id}">${item.enabled ? "停用" : "恢复"}</button></div>
    <p class="strategy-schedule-meta"><span>${source}</span><span>名义资金 ${escapeHtml(item.notional_cash_cny.toLocaleString("zh-CN"))} 元</span>
      <span>最近扫描 ${identity(item.last_market_scan_run_id)}</span><span>最近执行 ${identity(item.last_execution_id)}</span></p>
    <p class="strategy-schedule-meta">固定指纹 <code>${escapeHtml(item.strategy_fingerprint.slice(0, 16))}…</code>${blocked ? "<span>策略已归档，无法恢复任务。</span>" : ""}</p>
  </article>`;
}

function identity(value) {
  return value == null ? "尚无" : `#${value}`;
}
