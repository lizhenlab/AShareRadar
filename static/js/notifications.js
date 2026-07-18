import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";
import { $ } from "./dom.js";

export const ALERT_NOTIFICATION_POLL_MS = 30000;
export const ALERT_NOTIFICATION_CURSOR_KEY = "ashare-radar.alert-notification-cursor.v1";
export const ALERT_NOTIFICATION_ENABLED_KEY = "ashare-radar.alert-notifications-enabled.v1";
export const ALERT_NOTIFICATION_PAGE_SIZE = 50;
export const ALERT_NOTIFICATION_MAX_PAGES = 200;
const MAX_INDIVIDUAL_NOTIFICATIONS = 3;

export function initializeAlertNotifications(state, options = {}) {
  const permission = notificationPermission(options.NotificationApi);
  const preference = readEnabledPreference(options.storage);
  state.alertNotificationsEnabled = permission === "granted" && preference !== false;
  renderAlertNotificationState(state.alertNotificationsEnabled ? permission : notificationIdleState(permission));
  if (!state.alertNotificationsEnabled) return false;
  startAlertNotificationPolling(state, options);
  return true;
}

export async function enableAlertNotifications(state, options = {}) {
  const NotificationApi = notificationApi(options.NotificationApi);
  if (!NotificationApi) {
    renderAlertNotificationState("unsupported");
    return false;
  }
  let permission = NotificationApi.permission;
  if (permission === "default") {
    try {
      permission = await NotificationApi.requestPermission();
    } catch (error) {
      state.alertNotificationsEnabled = false;
      renderAlertNotificationState("permission-error");
      return false;
    }
  }
  if (permission !== "granted") {
    state.alertNotificationsEnabled = false;
    renderAlertNotificationState(permission);
    return false;
  }
  const wasDisabled = readEnabledPreference(options.storage) === false;
  state.alertNotificationsEnabled = true;
  writeEnabledPreference(true, options.storage);
  if (wasDisabled) clearCursor(state, options.storage);
  renderAlertNotificationState("granted");
  await pollAlertNotifications(state, { ...options, NotificationApi });
  startAlertNotificationPolling(state, { ...options, NotificationApi });
  return true;
}

export function disableAlertNotifications(state, options = {}) {
  state.alertNotificationsEnabled = false;
  state.alertNotificationEpoch = Number(state.alertNotificationEpoch || 0) + 1;
  state.alertNotificationPollToken = null;
  state.alertNotificationPolling = false;
  stopAlertNotificationPolling(state);
  clearCursor(state, options.storage);
  writeEnabledPreference(false, options.storage);
  renderAlertNotificationState(notificationIdleState(notificationPermission(options.NotificationApi)));
  return true;
}

export function startAlertNotificationPolling(state, options = {}) {
  if (state.alertNotificationsEnabled === false || state.alertNotificationTimer != null) return false;
  const intervalMs = options.intervalMs || ALERT_NOTIFICATION_POLL_MS;
  state.alertNotificationTimer = setInterval(
    () => void pollAlertNotifications(state, options),
    intervalMs
  );
  return true;
}

export function stopAlertNotificationPolling(state) {
  if (state.alertNotificationTimer == null) return false;
  clearInterval(state.alertNotificationTimer);
  state.alertNotificationTimer = null;
  return true;
}

export async function pollAlertNotifications(state, options = {}) {
  const NotificationApi = notificationApi(options.NotificationApi);
  if (!NotificationApi || NotificationApi.permission !== "granted" || state.alertNotificationsEnabled === false) return false;
  if (state.alertNotificationPolling) return false;
  const epoch = Number(state.alertNotificationEpoch || 0);
  const pollToken = {};
  state.alertNotificationPollToken = pollToken;
  state.alertNotificationPolling = true;
  try {
    const cursor = readCursor(state, options.storage);
    const events = await loadAlertEventBatch(cursor, options);
    if (!notificationPollIsCurrent(state, pollToken, epoch)) return false;
    deliverAlertNotifications(state, events, { ...options, NotificationApi });
    if (state.alertNotificationDeliveryFailed) {
      renderAlertNotificationState("delivery-error");
      return false;
    }
    renderAlertNotificationState("granted");
    return true;
  } catch (error) {
    if (notificationPollIsCurrent(state, pollToken, epoch)) renderAlertNotificationState("error");
    return false;
  } finally {
    if (state.alertNotificationPollToken === pollToken) {
      state.alertNotificationPollToken = null;
      state.alertNotificationPolling = false;
    }
  }
}

export function deliverAlertNotifications(state, events, options = {}) {
  const cursor = readCursor(state, options.storage);
  const nextCursor = latestCursor(events);
  state.alertNotificationDeliveryFailed = false;
  if (!cursor) {
    writeCursor(state, nextCursor || emptyCursor(), options.storage);
    return 0;
  }
  const pendingEvents = uniqueEventsAfter(events, cursor);
  const result = notifyPendingEvents(pendingEvents, notificationApi(options.NotificationApi));
  state.alertNotificationDeliveryFailed = result.failed;
  if (result.cursor && cursorAfter(result.cursor, cursor)) {
    writeCursor(state, result.cursor, options.storage);
  }
  return result.delivered;
}

async function loadAlertEventBatch(cursor, options) {
  if (!cursor) return requestAlertEventPage(null, options);
  const events = [];
  let pageCursor = cursor;
  const requestedMaxPages = positiveInteger(options.maxPages);
  const maxPages = Math.min(requestedMaxPages || ALERT_NOTIFICATION_MAX_PAGES, ALERT_NOTIFICATION_MAX_PAGES);
  for (let pageNumber = 0; pageNumber < maxPages; pageNumber += 1) {
    const page = await requestAlertEventPage(pageCursor, options);
    if (!page.length) return events;
    const nextCursor = latestCursor(page);
    if (!nextCursor || !cursorAfter(nextCursor, pageCursor)) {
      throw new TypeError("预警事件游标未向前推进");
    }
    events.push(...page);
    if (page.length < ALERT_NOTIFICATION_PAGE_SIZE) return events;
    pageCursor = nextCursor;
  }
  // Commit the bounded batch so the next poll can continue from its last id.
  // Throwing here would keep the old cursor forever when the backlog is large.
  return events;
}

async function requestAlertEventPage(cursor, options) {
  const events = await fetchJson(alertEventPageUrl(cursor), {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    signal: options.signal,
  });
  if (!Array.isArray(events) || events.length > ALERT_NOTIFICATION_PAGE_SIZE) {
    throw new TypeError("预警事件格式异常");
  }
  return events;
}

function alertEventPageUrl(cursor) {
  const params = new URLSearchParams({ limit: String(ALERT_NOTIFICATION_PAGE_SIZE) });
  if (cursor) {
    params.set("after_id", String(cursor.id));
  }
  return `/api/alerts/events?${params}`;
}

function notifyPendingEvents(events, NotificationApi) {
  const triggers = events.filter((event) => event?.event_type === "触发");
  if (triggers.length > MAX_INDIVIDUAL_NOTIFICATIONS) {
    const delivered = createNotification(NotificationApi, `AShareRadar · ${triggers.length} 条新预警`, {
      body: "打开研究工作台查看最新触发记录。",
      tag: "ashare-radar-alert-summary",
    });
    return {
      cursor: delivered ? latestCursor(events) : null,
      delivered: delivered ? triggers.length : 0,
      failed: !delivered,
    };
  }

  let cursor = null;
  let delivered = 0;
  for (const event of events) {
    if (event?.event_type !== "触发") {
      cursor = eventCursor(event);
      continue;
    }
    const succeeded = createNotification(NotificationApi, `AShareRadar · ${event.stock_name || event.name || event.symbol || "预警"}`, {
      body: String(event.message || "预警条件已触发").slice(0, 180),
      tag: `ashare-radar-alert-${event.id}`,
    });
    if (!succeeded) return { cursor, delivered, failed: true };
    cursor = eventCursor(event);
    delivered += 1;
  }
  return { cursor, delivered, failed: false };
}

function createNotification(NotificationApi, title, options) {
  if (!NotificationApi) return false;
  let notification;
  try {
    notification = new NotificationApi(title, options);
  } catch (error) {
    return false;
  }
  try {
    notification.onclick = () => {
      globalThis.focus?.();
      notification.close?.();
    };
  } catch (error) {
    // The notification was created; a missing click handler must not duplicate it.
  }
  return true;
}

function latestCursor(events) {
  return events.reduce((latest, event) => {
    const candidate = eventCursor(event);
    if (!validEventCursor(candidate)) return latest;
    return !latest || cursorAfter(candidate, latest) ? candidate : latest;
  }, null);
}

function uniqueEventsAfter(events, cursor) {
  const seen = new Set();
  return events
    .filter((event) => {
      const candidate = eventCursor(event);
      if (!validEventCursor(candidate) || !cursorAfter(candidate, cursor)) return false;
      const key = String(candidate.id);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((left, right) => compareCursor(eventCursor(left), eventCursor(right)));
}

function eventCursor(event) {
  return {
    createdAt: typeof event?.created_at === "string" ? event.created_at.trim() : "",
    id: positiveInteger(event?.id),
  };
}

function compareCursor(left, right) {
  return left.id - right.id;
}

function cursorAfter(candidate, cursor) {
  return compareCursor(candidate, cursor) > 0;
}

function positiveInteger(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 0;
}

function validEventCursor(cursor) {
  return cursor.id > 0;
}

function emptyCursor() {
  return { createdAt: "", id: 0 };
}

function readCursor(state, storage) {
  if (state.alertNotificationCursor) return state.alertNotificationCursor;
  const store = storageApi(storage);
  if (!store) return null;
  try {
    const parsed = JSON.parse(store.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
    if (!validCursor(parsed)) return null;
    state.alertNotificationCursor = parsed;
    return parsed;
  } catch (error) {
    return null;
  }
}

function writeCursor(state, cursor, storage) {
  state.alertNotificationCursor = cursor;
  const store = storageApi(storage);
  if (!store) return;
  try {
    store.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify(cursor));
  } catch (error) {
    // Private browsing may deny localStorage; the in-memory cursor still prevents repeats.
  }
}

function clearCursor(state, storage) {
  state.alertNotificationCursor = null;
  const store = storageApi(storage);
  if (!store) return;
  try {
    if (typeof store.removeItem === "function") store.removeItem(ALERT_NOTIFICATION_CURSOR_KEY);
    else store.setItem(ALERT_NOTIFICATION_CURSOR_KEY, "");
  } catch (error) {
    // The in-memory cursor is still cleared when storage is unavailable.
  }
}

function validCursor(value) {
  return Boolean(
    value &&
    typeof value.createdAt === "string" &&
    Number.isSafeInteger(value.id) &&
    value.id >= 0
  );
}

function notificationApi(candidate) {
  return candidate || globalThis.Notification || null;
}

function storageApi(candidate) {
  if (candidate) return candidate;
  try {
    return globalThis.localStorage || null;
  } catch (error) {
    return null;
  }
}

function readEnabledPreference(storage) {
  const store = storageApi(storage);
  if (!store) return null;
  try {
    const value = String(store.getItem(ALERT_NOTIFICATION_ENABLED_KEY) || "").trim();
    if (value === "1") return true;
    if (value === "0") return false;
  } catch (error) {
    return null;
  }
  return null;
}

function writeEnabledPreference(enabled, storage) {
  const store = storageApi(storage);
  if (!store) return;
  try {
    store.setItem(ALERT_NOTIFICATION_ENABLED_KEY, enabled ? "1" : "0");
  } catch (error) {
    // The current page still honors the in-memory preference.
  }
}

function notificationPollIsCurrent(state, token, epoch) {
  return (
    state.alertNotificationPollToken === token
    && Number(state.alertNotificationEpoch || 0) === epoch
    && state.alertNotificationsEnabled !== false
  );
}

function notificationPermission(candidate) {
  const api = notificationApi(candidate);
  return api ? api.permission : "unsupported";
}

function notificationIdleState(permission) {
  return permission === "granted" ? "disabled" : permission;
}

function renderAlertNotificationState(permission) {
  const button = $("enableAlertNotifications");
  const status = $("alertNotificationState");
  if (!button || !status) return;
  const view = notificationView(permission);
  button.textContent = view.button;
  button.disabled = view.disabled;
  status.textContent = view.status;
  status.dataset.tone = view.tone;
}

function notificationView(permission) {
  if (permission === "granted") return { button: "停用桌面提醒", status: "等待新触发", tone: "ok", disabled: false };
  if (permission === "disabled") return { button: "启用桌面提醒", status: "应用内已停用", tone: "", disabled: false };
  if (permission === "permission-error") return { button: "启用桌面提醒", status: "权限请求失败，请重试", tone: "warn", disabled: false };
  if (permission === "denied") return { button: "桌面提醒已拒绝", status: "请在浏览器设置中调整", tone: "warn", disabled: true };
  if (permission === "unsupported") return { button: "桌面提醒不可用", status: "当前浏览器不支持", tone: "warn", disabled: true };
  if (permission === "error") return { button: "停用桌面提醒", status: "事件同步暂不可用", tone: "warn", disabled: false };
  if (permission === "delivery-error") return { button: "停用桌面提醒", status: "预警投递失败，将自动重试", tone: "warn", disabled: false };
  return { button: "启用桌面提醒", status: "仅通知后续新触发", tone: "", disabled: false };
}
