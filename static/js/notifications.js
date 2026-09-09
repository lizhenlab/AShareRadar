import { $ } from "./dom.js";
import { createRequestScope } from "./api.js";
import { loadAlertNotificationBatch, sameNotificationStream, validNotificationCursor } from "./notification-feed.js";
export { ALERT_NOTIFICATION_PAGE_SIZE, ALERT_NOTIFICATION_MAX_PAGES } from "./notification-feed.js";

export const ALERT_NOTIFICATION_POLL_MS = 30000;
export const ALERT_NOTIFICATION_CURSOR_KEY = "ashare-radar.alert-notification-cursor.v2";
export const ALERT_NOTIFICATION_ENABLED_KEY = "ashare-radar.alert-notifications-enabled.v1";
export const ALERT_NOTIFICATION_LOCK_NAME = "ashare-radar.alert-notification-delivery.v1";
export const ALERT_NOTIFICATION_FALLBACK_LOCK_KEY = "ashare-radar.alert-notification-lock.v1";
export const ALERT_NOTIFICATION_COORDINATION_DB_NAME = "ashare-radar-notification-coordination-v1";
const LEGACY_ALERT_NOTIFICATION_CURSOR_KEY = "ashare-radar.alert-notification-cursor.v1";
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
  state.alertNotificationUpgradeNotice = false;
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
  state.alertNotificationPollToken?.scope.abort();
  state.alertNotificationPollToken = null;
  state.alertNotificationPolling = false;
  stopAlertNotificationPolling(state);
}

export async function pollAlertNotifications(state, options = {}) {
  const NotificationApi = notificationApi(options.NotificationApi);
  if (!NotificationApi || NotificationApi.permission !== "granted" || state.alertNotificationsEnabled === false) return false;
  if (state.alertNotificationPolling) return false;
  const epoch = Number(state.alertNotificationEpoch || 0);
  const pollToken = { scope: createRequestScope(null, options.signal) };
  state.alertNotificationPollToken = pollToken;
  state.alertNotificationPolling = true;
  try {
    const upgrading = legacyCursorPresent(options.storage) && !readStoredCursor(options.storage);
    const cursor = readCursor(state, options.storage);
    const batch = await loadAlertNotificationBatch(cursor, { ...options, signal: pollToken.scope.signal });
    if (!notificationPollIsCurrent(state, pollToken, epoch)) return false;
    const delivered = await deliverAlertNotificationsOnce(
      state,
      batch,
      { ...options, NotificationApi, upgrading },
      pollToken,
      epoch
    );
    if (!delivered || !notificationPollIsCurrent(state, pollToken, epoch)) return false;
    if (state.alertNotificationDeliveryFailed) {
      renderAlertNotificationState("delivery-error");
      return false;
    }
    renderAlertNotificationState(state.alertNotificationUpgradeNotice ? "upgraded" : "granted");
    return true;
  } catch (error) {
    if (notificationPollIsCurrent(state, pollToken, epoch)) {
      renderAlertNotificationState(
        error?.name === "NotificationCoordinationError" ? "coordination-unavailable" : "error"
      );
    }
    return false;
  } finally {
    pollToken.scope.dispose();
    if (state.alertNotificationPollToken === pollToken) {
      state.alertNotificationPollToken = null;
      state.alertNotificationPolling = false;
    }
  }
}

async function deliverAlertNotificationsOnce(state, batch, options, pollToken, epoch) {
  return withAlertNotificationLock(options, () => {
    if (!notificationPollIsCurrent(state, pollToken, epoch)) return false;
    if (readEnabledPreference(options.storage) === false) {
      deactivateAlertNotifications(state);
      renderAlertNotificationState("disabled");
      return false;
    }
    return deliverAlertNotifications(state, batch, options) !== null;
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

export function deliverAlertNotifications(state, batch, options = {}) {
  const cursor = readCursor(state, options.storage);
  state.alertNotificationDeliveryFailed = false;
  if (!batchMatchesSharedCursor(batch, cursor)) return null;
  if (!batch.requestedCursor) {
    writeCursor(state, { streamId: batch.streamId, id: batch.cursorId }, options.storage);
    if (options.upgrading) state.alertNotificationUpgradeNotice = true;
    clearLegacyCursor(options.storage);
    return 0;
  }
  const base = cursor?.streamId === batch.streamId
    ? cursor : { streamId: batch.streamId, id: batch.baselineId };
  if (!sameNotificationStream(cursor, base)) writeCursor(state, base, options.storage);
  const pendingEvents = uniqueEventsAfter(batch.events, base);
  const result = notifyPendingEvents(pendingEvents, notificationApi(options.NotificationApi), batch.streamId, options.onNotificationClick);
  state.alertNotificationDeliveryFailed = result.failed;
  if (result.cursor && cursorAfter(result.cursor, base)) {
    writeCursor(state, { streamId: batch.streamId, id: result.cursor.id }, options.storage);
  }
  if (result.delivered > 0) state.alertNotificationUpgradeNotice = false;
  return result.delivered;
}

function batchMatchesSharedCursor(batch, cursor) {
  if (!batch.requestedCursor) return !cursor;
  return Boolean(cursor && (cursor.streamId === batch.requestedCursor.streamId || cursor.streamId === batch.streamId));
}

function notifyPendingEvents(events, NotificationApi, streamId, onNotificationClick) {
  const triggers = events.filter((event) => event?.event_type === "触发");
  if (triggers.length > MAX_INDIVIDUAL_NOTIFICATIONS) {
    const delivered = createNotification(NotificationApi, `AShareRadar · ${triggers.length} 条新预警`, {
      body: "打开研究工作台查看最新触发记录。",
      tag: `ashare-radar-alert-${streamId}-summary`,
    }, { callback: onNotificationClick, target: Object.freeze({ kind: "summary", streamId, count: triggers.length }) });
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
      tag: `ashare-radar-alert-${streamId}-${event.id}`,
    }, { callback: onNotificationClick, target: capturedEventTarget(event, streamId) });
    if (!succeeded) return { cursor, delivered, failed: true };
    cursor = eventCursor(event);
    delivered += 1;
  }
  return { cursor, delivered, failed: false };
}

function capturedEventTarget(event, streamId) {
  const fields = ["id", "symbol", "stock_name", "name", "event_type", "message", "price", "change_pct", "threshold", "created_at"];
  const snapshot = Object.fromEntries(fields.map((field) => [field, event[field]]));
  return Object.freeze({ kind: "event", streamId, event: Object.freeze(snapshot) });
}

function createNotification(NotificationApi, title, options, activation) {
  if (!NotificationApi) return false;
  let notification;
  try {
    notification = new NotificationApi(title, options);
  } catch (error) {
    return false;
  }
  try {
    notification.onclick = () => activateNotification(notification, activation);
  } catch (error) {
    // The notification was created; a missing click handler must not duplicate it.
  }
  return true;
}

function activateNotification(notification, activation) {
  try { globalThis.focus?.(); } catch { /* Navigation remains available if window focus is denied. */ }
  try { notification.close?.(); } catch { /* Closing the OS card is independent of opening its details. */ }
  try {
    Promise.resolve(activation?.callback?.(activation.target)).catch(() => {});
  } catch {
    // A constructed notification is already delivered; navigation cannot turn it into a retry.
  }
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

function readCursor(state, storage) {
  const memory = validNotificationCursor(state.alertNotificationCursor) ? state.alertNotificationCursor : null;
  const shared = readStoredCursor(storage);
  const cursor = storageApi(storage) ? shared : memory;
  state.alertNotificationCursor = cursor;
  return cursor;
}

function readStoredCursor(storage) {
  const store = storageApi(storage);
  if (!store) return null;
  try {
    const parsed = JSON.parse(store.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
    return validNotificationCursor(parsed) ? parsed : null;
  } catch (error) {
    return null;
  }
}

function writeCursor(state, cursor, storage) {
  const shared = readStoredCursor(storage);
  const nextCursor = sameNotificationStream(cursor, shared) && shared.id > cursor.id ? shared : cursor;
  const store = storageApi(storage);
  if (!store) {
    state.alertNotificationCursor = nextCursor;
    return;
  }
  try {
    if (!sameNotificationStream(nextCursor, shared) || nextCursor.id !== shared.id) {
      store.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify(nextCursor));
    }
  } catch (error) {
    throw notificationCoordinationError("共享通知游标保存失败", error);
  }
  state.alertNotificationCursor = nextCursor;
}

function clearCursor(state, storage) {
  state.alertNotificationCursor = null;
  const store = storageApi(storage);
  if (!store) return;
  try {
    if (typeof store.removeItem === "function") store.removeItem(ALERT_NOTIFICATION_CURSOR_KEY);
    else store.setItem(ALERT_NOTIFICATION_CURSOR_KEY, "");
    clearLegacyCursor(storage);
  } catch (error) {
    // The in-memory cursor is still cleared when storage is unavailable.
  }
}

function legacyCursorPresent(storage) {
  try {
    return Boolean(storageApi(storage)?.getItem(LEGACY_ALERT_NOTIFICATION_CURSOR_KEY));
  } catch (error) {
    return false;
  }
}

function clearLegacyCursor(storage) {
  const store = storageApi(storage);
  if (!store) return;
  if (typeof store.removeItem === "function") store.removeItem(LEGACY_ALERT_NOTIFICATION_CURSOR_KEY);
  else store.setItem(LEGACY_ALERT_NOTIFICATION_CURSOR_KEY, "");
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
    readCursor(state, options.storage);
    return;
  }
  if (event?.key !== ALERT_NOTIFICATION_ENABLED_KEY) return;
  const enabled = readEnabledPreference(options.storage);
  if (enabled === false) {
    deactivateAlertNotifications(state);
    state.alertNotificationCursor = null;
    renderAlertNotificationState("disabled");
    return;
  }
  if (enabled !== true || notificationPermission(options.NotificationApi) !== "granted") return;
  if (state.alertNotificationsEnabled !== true) {
    state.alertNotificationsEnabled = true;
    state.alertNotificationEpoch = Number(state.alertNotificationEpoch || 0) + 1;
    renderAlertNotificationState("granted");
    void pollAlertNotifications(state, options);
  }
  startAlertNotificationPolling(state, options);
}

function notificationPollIsCurrent(state, token, epoch) {
  return (
    state.alertNotificationPollToken === token
    && !token.scope.signal.aborted
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
  if (permission === "upgraded") return { button: "停用桌面提醒", status: "提醒协议已升级，已建立当前基线，不补发历史提醒；请刷新其他页面", tone: "", disabled: false };
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
