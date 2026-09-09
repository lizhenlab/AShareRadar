import { escapeHtml } from "./dom.js";
import { formatAuditTimestamp } from "./audit-time.js";
import { formatNumber } from "./format.js";
import { validateUiSymbol } from "./symbols.js";

export function createNotificationNavigation(context) {
  const navigation = { context, sequence: 0 };
  return {
    open: (target) => openNotificationTarget(navigation, target),
    cancel: () => cancelNotificationNavigation(navigation),
  };
}

function cancelNotificationNavigation(navigation) {
  navigation.sequence += 1;
  const panel = notificationPanel(navigation.context);
  if (panel) {
    panel.hidden = true;
    panel.innerHTML = "";
  }
}

async function openNotificationTarget(navigation, target) {
  const context = navigation.context;
  const notice = validatedNotificationTarget(target);
  if (!notice) return false;
  if (notice.kind === "event") context.setActiveSymbol(notice.symbol);
  context.setWorkspaceView("tools");
  const sequence = ++navigation.sequence;
  renderNotificationDetails(context, notice, "loading");
  focusNotificationDetails(context);
  if (notice.kind === "summary") return true;
  let owner = null;
  try {
    const loading = context.loadAll({ reveal: false });
    owner = { symbol: notice.symbol, loadSeq: context.state.loadSeq };
    const loaded = await loading;
    if (!ownsNotificationNavigation(navigation, sequence, owner, !loaded)) return false;
    renderNotificationDetails(context, notice, loaded ? "ready" : "unavailable");
    return Boolean(loaded);
  } catch {
    if (owner && !ownsNotificationNavigation(navigation, sequence, owner, true)) return false;
    if (navigation.sequence !== sequence) return false;
    renderNotificationDetails(context, notice, "unavailable");
    return false;
  }
}

function ownsNotificationNavigation(navigation, sequence, owner, allowFailedFallback) {
  const { state } = navigation.context;
  return navigation.sequence === sequence && state.loadSeq === owner.loadSeq
    && state.primaryView === "research" && state.workspaceView === "tools"
    && (state.symbol === owner.symbol || (allowFailedFallback && state.failedLoadSymbol === owner.symbol));
}

function validatedNotificationTarget(target) {
  if (!target || !/^[0-9a-f]{32}$/.test(target.streamId)) return null;
  if (target.kind === "summary") {
    return Number.isSafeInteger(target.count) && target.count > 0 ? { ...target } : null;
  }
  if (target.kind !== "event" || !Number.isSafeInteger(target.event?.id) || target.event.id <= 0) return null;
  try {
    return { kind: "event", streamId: target.streamId, event: { ...target.event }, symbol: validateUiSymbol(target.event.symbol) };
  } catch {
    return null;
  }
}

function notificationPanel(context) {
  return (context.root || globalThis.document)?.getElementById?.("notificationDetail");
}

function renderNotificationDetails(context, notice, phase) {
  const panel = notificationPanel(context);
  if (!panel) return;
  panel.hidden = false;
  panel.dataset.streamId = notice.streamId;
  panel.innerHTML = notice.kind === "summary" ? summaryDetails(notice) : eventDetails(notice, phase);
}

function summaryDetails(notice) {
  return `<div class="alert-event">
    <strong>桌面提醒摘要 · ${escapeHtml(notice.count)} 条新预警</strong>
    <p>这是本次通知的批量摘要，未指向单条记录；下方仍是当前股票的规则与最近记录。</p>
    <p>通知点击不会标记已读，也不会重新检查预警。</p>
  </div>`;
}

function eventDetails(notice, phase) {
  const event = notice.event;
  const status = phase === "loading" ? "正在读取当前行情，触发记录保持原值。"
    : phase === "ready" ? "当前分析已独立加载；以下是触发时记录，不是当前行情。"
      : "当前行情未载入，页面可能保留上次成功分析；以下仅为通知触发时记录。";
  return `<div class="alert-event">
    <strong>通知触发时记录 · ${escapeHtml(event.stock_name || notice.symbol)} (${escapeHtml(notice.symbol)})</strong>
    <span>${escapeHtml(event.name || "预警")} · ${escapeHtml(event.event_type || "触发")} · ${escapeHtml(formatAuditTimestamp(event.created_at))}</span>
    <p>${escapeHtml(event.message || "预警条件已触发")}</p>
    <p>触发时价格 ${formatNumber(event.price)} · 涨跌幅 ${formatNumber(event.change_pct)}% · 阈值 ${formatNumber(event.threshold)}</p>
    <p class="notification-detail-status" role="status">${status}</p>
    <p>只读通知快照；规则或历史记录后来删除、数据库恢复，都不会改写此处原文。</p>
  </div>`;
}

function focusNotificationDetails(context) {
  const panel = notificationPanel(context);
  panel?.focus?.({ preventScroll: true });
  panel?.scrollIntoView?.({ block: "nearest", behavior: "auto" });
}
