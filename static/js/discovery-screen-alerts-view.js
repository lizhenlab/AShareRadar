import { escapeHtml } from "./dom.js";
import { formatAuditTimestamp } from "./audit-time.js";

const LABELS = Object.freeze({ all: "全部", entered: "新进入", exited: "退出", unrankable: "当前不可排名" });

export function discoveryScreenAlertElements(root) {
  const names = ["Context", "Detail", "DetailContext", "Kind", "DetailStatus", "DetailRows", "DetailPage",
    "DetailPrev", "DetailNext", "DetailRefresh", "History", "HistoryStatus", "HistoryRows", "HistoryPage",
    "HistoryPrev", "HistoryNext", "HistoryRefresh"];
  const elements = Object.fromEntries(names.map(name => [name, root.getElementById(`discoveryScreenAlerts${name}`)]));
  elements.shell = root.getElementById("discoveryScreenAlerts");
  if (Object.values(elements).some(element => !element)) throw new Error("筛选变化记录界面不完整");
  return elements;
}

export function renderScreenAlertSelection(elements, state) {
  elements.Context.textContent = state.preset
    ? `当前方案：${state.preset.name} #${state.preset.id} · 当前修订 v${state.preset.revision}。历史记录保留记录时的修订与批次。`
    : "选择方案后查看变化记录。";
  renderScreenAlertHistory(elements, state);
  renderScreenAlertDetail(elements, state);
}

export function renderScreenAlertHistory(elements, state) {
  const value = state.history;
  elements.HistoryRows.innerHTML = value
    ? value.items.map(event => eventRow(event, state.detail?.event.id)).join("") || "<p>此页没有已记录的变化。</p>"
    : "";
  elements.HistoryPage.textContent = value ? pageLabel(value, "条记录") : "尚未读取历史";
  syncScreenAlertControls(elements, state);
}

export function renderScreenAlertDetail(elements, state) {
  const value = state.detail;
  elements.Detail.hidden = !value && !state.detailTarget;
  elements.DetailContext.textContent = value ? eventContext(value) : "尚未取得所选记录明细。";
  elements.DetailRows.innerHTML = value ? detailRows(value) : "";
  elements.DetailPage.textContent = value ? pageLabel(value, "只股票") : "尚未读取明细";
  elements.Kind.value = value?.kind || "all";
  syncScreenAlertControls(elements, state);
}

export function syncScreenAlertControls(elements, state) {
  const detail = state.detail;
  const readingHistory = Boolean(state.historyRequest);
  const readingDetail = Boolean(state.detailRequest);
  const inactive = !state.surfaceActive || !state.preset || state.disposed;
  elements.History.setAttribute("aria-busy", String(readingHistory));
  elements.Detail.setAttribute("aria-busy", String(readingDetail));
  elements.HistoryRefresh.disabled = inactive || readingHistory;
  syncPageControls(elements, "History", state.history, inactive || readingHistory);
  syncPageControls(elements, "Detail", detail, inactive || readingDetail);
  elements.DetailRefresh.disabled = inactive || readingDetail || (!state.detailTarget && detail?.source !== "history");
  elements.Kind.disabled = inactive || readingDetail || !detail;
  elements.HistoryRows.querySelectorAll("[data-screen-alert-event]").forEach(button => { button.disabled = inactive; });
}

function syncPageControls(elements, section, page, inactive) {
  elements[`${section}Prev`].disabled = inactive || !page || page.page <= 1;
  elements[`${section}Next`].disabled = inactive || !page || page.page >= page.page_count;
}

export function screenAlertStatus(elements, section, message, kind = "ready") {
  elements[`${section}Status`].textContent = message;
  elements[`${section}Status`].dataset.kind = kind;
}

export function screenAlertRetainedDetail(state) {
  const value = state.detail;
  if (!value) return "尚无已加载明细";
  return value.source === "ack" ? `保留本次已确认记录的${LABELS[value.kind]}第 ${value.page} 页`
    : `保留记录 #${value.event.id} 的${LABELS[value.kind]}第 ${value.page} 页`;
}

function eventRow(event, selectedId) {
  return `<article class="discovery-screen-alert-row" data-screen-alert-id="${event.id}">
    <button type="button" class="mini-button" data-screen-alert-event="${event.id}"${selectedId === event.id ? ' aria-current="true"' : ""}>查看记录 #${event.id}</button>
    <p>修订 v${event.preset_revision} · 批次 #${event.previous_run_id} → #${event.current_run_id} · ${escapeHtml(formatAuditTimestamp(event.created_at))}（上海）</p>
    <p>新进入 ${event.entered_count} · 退出 ${event.exited_count} · 当前不可排名 ${event.suppressed_unrankable_count}</p>
  </article>`;
}

function eventContext(value) {
  const event = value.event;
  const source = value.source === "ack" ? "本次已确认记录" : `历史记录 #${event.id}`;
  const time = event.created_at ? ` · ${formatAuditTimestamp(event.created_at)}（上海）` : "";
  return `${source} · 方案 #${event.preset_id} 修订 v${event.preset_revision} · 批次 #${event.previous_run_id} → #${event.current_run_id}${time}。`
    + `新进入 ${event.entered_count}、退出 ${event.exited_count}、当前不可排名 ${event.suppressed_unrankable_count}。记录摘要 ${event.event_digest}。`;
}

function detailRows(value) {
  if (!value.items.length) return `<p>${value.total === 0 ? "此分类没有股票变化。" : "此页没有股票变化。"}</p>`;
  return value.items.map(item => `<div class="discovery-screen-alert-change" data-change="${item.change}">
    <strong>${LABELS[item.change]}</strong><code>${escapeHtml(item.symbol)}</code>
    <span>${changeSource(item.change, value.event)}</span>
  </div>`).join("");
}

function changeSource(kind, event) {
  if (kind === "entered") return `当前批次 #${event.current_run_id} 入选`;
  if (kind === "exited") return `前批次 #${event.previous_run_id} 入选，当前退出`;
  return `当前批次 #${event.current_run_id} 不可排名，未计为退出`;
}

function pageLabel(value, unit) {
  return `第 ${value.page} / ${value.page_count || 1} 页 · 共 ${value.total} ${unit}`;
}
