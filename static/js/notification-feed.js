import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";

export const ALERT_NOTIFICATION_PAGE_SIZE = 50;
export const ALERT_NOTIFICATION_MAX_PAGES = 200;

export function validNotificationCursor(value) {
  return Boolean(value && validStreamId(value.streamId) && validId(value.id));
}

export function sameNotificationStream(left, right) {
  return validNotificationCursor(left) && validNotificationCursor(right) && left.streamId === right.streamId;
}

export async function loadAlertNotificationBatch(cursor, options = {}) {
  const requestedCursor = cursor ? { ...cursor } : null;
  const first = await requestPage(requestedCursor, options);
  const batch = {
    requestedCursor,
    streamId: first.stream_id,
    baselineId: first.baseline_id,
    cursorId: first.cursor_id,
    events: [...first.events],
  };
  const maxPages = pageLimit(options.maxPages);
  let page = first;
  for (let count = 1; page.has_more && count < maxPages; count += 1) {
    const nextCursor = { streamId: batch.streamId, id: page.cursor_id };
    page = await requestPage(nextCursor, options);
    if (page.stream_id !== batch.streamId || page.baseline_id !== batch.baselineId) {
      throw new TypeError("分页期间预警历史已切换，请重试");
    }
    batch.events.push(...page.events);
    batch.cursorId = page.cursor_id;
  }
  return batch;
}

function pageLimit(value) {
  return Number.isSafeInteger(value) && value > 0
    ? Math.min(value, ALERT_NOTIFICATION_MAX_PAGES)
    : ALERT_NOTIFICATION_MAX_PAGES;
}

async function requestPage(cursor, options) {
  const params = new URLSearchParams({ limit: String(ALERT_NOTIFICATION_PAGE_SIZE) });
  if (cursor) {
    params.set("stream_id", cursor.streamId);
    params.set("after_id", String(cursor.id));
  }
  const page = await fetchJson(`/api/alerts/notification-events?${params}`, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    cache: "no-store",
    signal: options.signal,
  });
  validatePage(page, cursor);
  return page;
}

function validatePage(page, cursor) {
  if (
    !page || !validStreamId(page.stream_id)
    || !validId(page.baseline_id) || !validId(page.cursor_id)
    || page.cursor_id < page.baseline_id
    || typeof page.reset !== "boolean" || typeof page.has_more !== "boolean"
    || !Array.isArray(page.events) || page.events.length > ALERT_NOTIFICATION_PAGE_SIZE
  ) throw new TypeError("预警流响应格式异常");
  if (!cursor) {
    if (!page.reset || page.events.length || page.has_more) {
      throw new TypeError("初次预警同步必须建立当前基线");
    }
    return;
  }
  const changedStream = page.stream_id !== cursor.streamId;
  if (page.reset !== changedStream) throw new TypeError("预警流重置标记不一致");
  const startId = changedStream ? page.baseline_id : cursor.id;
  if (startId < page.baseline_id) throw new TypeError("预警游标低于流起点");
  validateEvents(page, startId);
}

function validateEvents(page, startId) {
  let previousId = startId;
  for (const event of page.events) {
    if (!validId(event?.id) || event.id <= previousId) {
      throw new TypeError("预警事件必须按唯一ID向前推进");
    }
    previousId = event.id;
  }
  if (page.cursor_id !== previousId) throw new TypeError("预警响应游标与事件不一致");
  if (page.has_more && page.events.length !== ALERT_NOTIFICATION_PAGE_SIZE) {
    throw new TypeError("预警分页标记与事件数量不一致");
  }
}

function validId(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validStreamId(value) {
  return typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
}
