import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";
import { $ } from "./dom.js";

export const ALERT_NOTIFICATION_POLL_MS = 30000;
export const ALERT_NOTIFICATION_CURSOR_KEY = "ashare-radar.alert-notification-cursor.v1";
export const ALERT_NOTIFICATION_ENABLED_KEY = "ashare-radar.alert-notifications-enabled.v1";
export const ALERT_NOTIFICATION_LOCK_NAME = "ashare-radar.alert-notification-delivery.v1";
export const ALERT_NOTIFICATION_FALLBACK_LOCK_KEY = "ashare-radar.alert-notification-lock.v1";
export const ALERT_NOTIFICATION_COORDINATION_DB_NAME = "ashare-radar-notification-coordination-v1";
export const ALERT_NOTIFICATION_PAGE_SIZE = 50;
export const ALERT_NOTIFICATION_MAX_PAGES = 200;
const MAX_INDIVIDUAL_NOTIFICATIONS = 3;
const ALERT_NOTIFICATION_COORDINATION_STORE = "delivery-locks";
const ALERT_NOTIFICATION_STORAGE_PROBE_KEY = "ashare-radar.alert-notification-storage-probe.v1";

export function initializeAlertNotifications(state, options = {}) {
  bindAlertNotificationStorage(state, options);
  const permission = notificationPermission(options.NotificationApi);
  const preference = readEnabledPreference(options.storage);
  state.alertNotificationsEnabled = permission === "granted" && preference !== false;
  renderAlertNotificationState(state.alertNotificationsEnabled ? permission : notificationIdleState(permission));
  if (!state.alertNotificationsEnabled) return false;
  startAlertNotificationPolling(state, options);
  return true;
}

export async function enableAlertNotifications(state, options = {}) {
  bindAlertNotificationStorage(state, options);
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
  if (wasDisabled) clearCursor(state, options.storage);
  writeEnabledPreference(true, options.storage);
  renderAlertNotificationState("granted");
  await pollAlertNotifications(state, { ...options, NotificationApi });
  startAlertNotificationPolling(state, { ...options, NotificationApi });
  return true;
}

export function disableAlertNotifications(state, options = {}) {
  deactivateAlertNotifications(state);
  writeEnabledPreference(false, options.storage);
  clearCursor(state, options.storage);
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

function deactivateAlertNotifications(state) {
  state.alertNotificationsEnabled = false;
  state.alertNotificationEpoch = Number(state.alertNotificationEpoch || 0) + 1;
  state.alertNotificationPollToken = null;
  state.alertNotificationPolling = false;
  stopAlertNotificationPolling(state);
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
    const delivered = await deliverAlertNotificationsOnce(
      state,
      events,
      { ...options, NotificationApi },
      pollToken,
      epoch
    );
    if (!delivered || !notificationPollIsCurrent(state, pollToken, epoch)) return false;
    if (state.alertNotificationDeliveryFailed) {
      renderAlertNotificationState("delivery-error");
      return false;
    }
    renderAlertNotificationState("granted");
    return true;
  } catch (error) {
    if (notificationPollIsCurrent(state, pollToken, epoch)) {
      renderAlertNotificationState(
        error?.name === "NotificationCoordinationError" ? "coordination-unavailable" : "error"
      );
    }
    return false;
  } finally {
    if (state.alertNotificationPollToken === pollToken) {
      state.alertNotificationPollToken = null;
      state.alertNotificationPolling = false;
    }
  }
}

async function deliverAlertNotificationsOnce(state, events, options, pollToken, epoch) {
  return withAlertNotificationLock(options, () => {
    if (!notificationPollIsCurrent(state, pollToken, epoch)) return false;
    deliverAlertNotifications(state, events, options);
    return true;
  });
}

async function withAlertNotificationLock(options, callback) {
  const store = writableSharedStorage(options.storage);
  if (!store) throw notificationCoordinationError("共享游标存储不可用");
  const locks = alertNotificationLocks(options.locks);
  if (locks) {
    return locks.request(
      ALERT_NOTIFICATION_LOCK_NAME,
      { mode: "exclusive", ifAvailable: true },
      (lock) => lock ? callback() : false
    );
  }
  const indexedDb = alertNotificationIndexedDb(options.indexedDB);
  if (!indexedDb) throw notificationCoordinationError("浏览器缺少可用的跨标签页锁");
  try {
    return await withIndexedDbAlertNotificationLock(indexedDb, callback);
  } catch (error) {
    if (error?.name === "NotificationCoordinationError") throw error;
    throw notificationCoordinationError("IndexedDB 协调失败", error);
  }
}

function alertNotificationLocks(candidate) {
  if (candidate === false) return null;
  const locks = candidate || globalThis.navigator?.locks;
  return locks && typeof locks.request === "function" ? locks : null;
}

function alertNotificationIndexedDb(candidate) {
  if (candidate === false) return null;
  const indexedDb = candidate || globalThis.indexedDB;
  return indexedDb && typeof indexedDb.open === "function" ? indexedDb : null;
}

async function withIndexedDbAlertNotificationLock(indexedDb, callback) {
  const database = await openAlertNotificationLockDatabase(indexedDb);
  try {
    return await runIndexedDbAlertNotificationTransaction(database, callback);
  } finally {
    database.close?.();
  }
}

function openAlertNotificationLockDatabase(indexedDb) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let request;
    try {
      request = indexedDb.open(ALERT_NOTIFICATION_COORDINATION_DB_NAME, 1);
    } catch (error) {
      reject(notificationCoordinationError("无法打开 IndexedDB", error));
      return;
    }
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(ALERT_NOTIFICATION_COORDINATION_STORE)) {
        database.createObjectStore(ALERT_NOTIFICATION_COORDINATION_STORE);
      }
    };
    request.onsuccess = () => {
      if (settled) {
        request.result.close?.();
        return;
      }
      settled = true;
      resolve(request.result);
    };
    request.onerror = () => {
      if (settled) return;
      settled = true;
      reject(notificationCoordinationError("无法打开 IndexedDB", request.error));
    };
    request.onblocked = () => {
      if (settled) return;
      settled = true;
      reject(notificationCoordinationError("IndexedDB 升级被其他页面阻塞"));
    };
  });
}

function runIndexedDbAlertNotificationTransaction(database, callback) {
  return new Promise((resolve, reject) => {
    let callbackError = null;
    let callbackResult = false;
    let transaction;
    try {
      transaction = database.transaction(ALERT_NOTIFICATION_COORDINATION_STORE, "readwrite");
      const store = transaction.objectStore(ALERT_NOTIFICATION_COORDINATION_STORE);
      const request = store.put(
        { acquiredAt: Date.now(), token: `${Date.now()}:${Math.random().toString(36).slice(2)}` },
        ALERT_NOTIFICATION_FALLBACK_LOCK_KEY
      );
      request.onsuccess = () => {
        try {
          callbackResult = callback();
          if (callbackResult && typeof callbackResult.then === "function") {
            throw new TypeError("通知协调回调必须同步完成");
          }
          store.delete(ALERT_NOTIFICATION_FALLBACK_LOCK_KEY);
        } catch (error) {
          callbackError = error;
          transaction.abort();
        }
      };
    } catch (error) {
      reject(notificationCoordinationError("无法创建 IndexedDB 原子事务", error));
      return;
    }
    transaction.oncomplete = () => resolve(callbackResult);
    transaction.onabort = () => reject(
      callbackError || notificationCoordinationError("IndexedDB 原子事务被中止", transaction.error)
    );
    transaction.onerror = () => {};
  });
}

function writableSharedStorage(storage) {
  const store = storageApi(storage);
  if (!store) return null;
  const probe = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
  const probeKey = `${ALERT_NOTIFICATION_STORAGE_PROBE_KEY}.${probe}`;
  try {
    store.setItem(probeKey, probe);
    const available = store.getItem(probeKey) === probe;
    clearStorageProbe(store, probeKey);
    return available ? store : null;
  } catch (error) {
    try {
      clearStorageProbe(store, probeKey);
    } catch (restoreError) {
      // The caller will fail closed because shared coordination is unavailable.
    }
    return null;
  }
}

function clearStorageProbe(store, probeKey) {
  if (typeof store.removeItem === "function") {
    store.removeItem(probeKey);
    return;
  }
  store.setItem(probeKey, "");
}

function notificationCoordinationError(message, cause) {
  const error = new Error(message, cause === undefined ? undefined : { cause });
  error.name = "NotificationCoordinationError";
  return error;
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
  const memory = validCursor(state.alertNotificationCursor) ? state.alertNotificationCursor : null;
  const shared = readStoredCursor(storage);
  const cursor = newestCursor(memory, shared);
  state.alertNotificationCursor = cursor;
  return cursor;
}

function readStoredCursor(storage) {
  const store = storageApi(storage);
  if (!store) return null;
  try {
    const parsed = JSON.parse(store.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
    return validCursor(parsed) ? parsed : null;
  } catch (error) {
    return null;
  }
}

function writeCursor(state, cursor, storage) {
  const shared = readStoredCursor(storage);
  const nextCursor = newestCursor(cursor, newestCursor(state.alertNotificationCursor, shared));
  state.alertNotificationCursor = nextCursor;
  const store = storageApi(storage);
  if (!store) return;
  try {
    if (!shared || cursorAfter(nextCursor, shared)) {
      store.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify(nextCursor));
    }
  } catch (error) {
    // In-memory state is only page-local; coordinated delivery fails closed before this path.
  }
}

function newestCursor(left, right) {
  const validLeft = validCursor(left) ? left : null;
  const validRight = validCursor(right) ? right : null;
  if (!validLeft) return validRight;
  if (!validRight) return validLeft;
  return cursorAfter(validRight, validLeft) ? validRight : validLeft;
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

function bindAlertNotificationStorage(state, options) {
  const target = options.storageTarget || globalThis;
  if (!target || typeof target.addEventListener !== "function") return false;
  const current = state.alertNotificationStorageBinding;
  if (current?.target === target) {
    current.options = { ...options };
    return true;
  }
  if (current) current.target.removeEventListener?.("storage", current.handler);
  const binding = { target, options: { ...options }, handler: null };
  binding.handler = (event) => handleAlertNotificationStorageEvent(state, binding.options, event);
  state.alertNotificationStorageBinding = binding;
  target.addEventListener("storage", binding.handler);
  return true;
}

function handleAlertNotificationStorageEvent(state, options, event) {
  if (event?.key === ALERT_NOTIFICATION_CURSOR_KEY) {
    const cursor = cursorFromStorageValue(event.newValue);
    if (cursor) state.alertNotificationCursor = newestCursor(state.alertNotificationCursor, cursor);
    else if (event.newValue == null || event.newValue === "") state.alertNotificationCursor = null;
    return;
  }
  if (event?.key !== ALERT_NOTIFICATION_ENABLED_KEY) return;
  if (String(event.newValue) === "0") {
    deactivateAlertNotifications(state);
    state.alertNotificationCursor = null;
    renderAlertNotificationState("disabled");
    return;
  }
  if (String(event.newValue) !== "1" || notificationPermission(options.NotificationApi) !== "granted") return;
  if (state.alertNotificationsEnabled !== true) {
    state.alertNotificationsEnabled = true;
    state.alertNotificationEpoch = Number(state.alertNotificationEpoch || 0) + 1;
    renderAlertNotificationState("granted");
    void pollAlertNotifications(state, options);
  }
  startAlertNotificationPolling(state, options);
}

function cursorFromStorageValue(value) {
  try {
    const cursor = JSON.parse(value);
    return validCursor(cursor) ? cursor : null;
  } catch (error) {
    return null;
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
  if (permission === "coordination-unavailable") return { button: "停用桌面提醒", status: "无法安全协调多标签页提醒", tone: "warn", disabled: false };
  return { button: "启用桌面提醒", status: "仅通知后续新触发", tone: "", disabled: false };
}
