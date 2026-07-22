from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_alert_notifications_baseline_new_event_and_deduplication() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const { deliverAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        const storage = memoryStorage();
        const first = [event(1, "2026-07-16 10:00:00", "触发")];
        const duplicateTrigger = event(2, "2026-07-16 10:01:00", "触发");

        assert(deliverAlertNotifications(state, first, { NotificationApi: FakeNotification, storage }) === 0, "first load must establish a baseline");
        const second = [
          event(3, "2026-07-16 10:02:00", "恢复"),
          duplicateTrigger,
          duplicateTrigger,
          ...first,
        ];
        assert(deliverAlertNotifications(state, second, { NotificationApi: FakeNotification, storage }) === 1, "one new trigger should be delivered");
        assert(sent.length === 1 && sent[0].options.tag === "ashare-radar-alert-2", "trigger notification was malformed");
        assert(deliverAlertNotifications(state, second, { NotificationApi: FakeNotification, storage }) === 0, "same events were delivered twice");

        function event(id, created_at, event_type) {
          return { id, created_at, event_type, stock_name: "测试股票", message: `事件${id}` };
        }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notifications_collapse_bursts_into_one_summary() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const { deliverAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        deliverAlertNotifications(state, [{ id: 1, created_at: "2026-07-16 10:00:00", event_type: "触发" }], { NotificationApi: FakeNotification });
        const burst = [2, 3, 4, 5].map((id) => ({
          id,
          created_at: `2026-07-16 10:0${id}:00`,
          event_type: "触发",
          message: `事件${id}`,
        }));

        const delivered = deliverAlertNotifications(state, burst, { NotificationApi: FakeNotification });

        assert(delivered === 4, "burst count was not reported");
        assert(sent.length === 1, "burst should produce one summary notification");
        assert(sent[0].title.includes("4 条新预警"), "summary title did not include the trigger count");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_first_poll_establishes_newest_baseline() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const requests = [];
        globalThis.fetch = async (url) => {
          requests.push(String(url));
          return jsonResponse([
            event(3, "2026-07-16 10:02:00"),
            event(2, "2026-07-16 10:01:00"),
            event(1, "2026-07-16 10:00:00"),
          ]);
        };
        const { ALERT_NOTIFICATION_CURSOR_KEY, pollAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        const storage = memoryStorage();
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });
        const cursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(completed === true, "baseline poll failed");
        assert(requests.length === 1 && !requests[0].includes("after_id"), "first poll should use the newest-event list");
        assert(cursor.id === 3 && cursor.createdAt === "2026-07-16 10:02:00", "first poll did not persist the newest cursor");
        assert(sent.length === 0, "baseline poll emitted an old notification");
        function event(id, created_at) { return { id, created_at, event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_poll_drains_more_than_fifty_events_before_advancing_cursor() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const createdAt = "2026-07-16 10:00:00";
        const pending = Array.from({ length: 125 }, (_, index) => event(index + 2));
        const requests = [];
        globalThis.fetch = async (url) => {
          const parsed = new URL(String(url), "http://local.test");
          const afterId = Number(parsed.searchParams.get("after_id"));
          requests.push({ afterId, limit: Number(parsed.searchParams.get("limit")) });
          return jsonResponse(pending.filter((item) => item.id > afterId).slice(0, 50));
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          deliverAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        const storage = memoryStorage();
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });
        const cursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(completed === true, "paginated poll failed");
        assert(requests.length === 3, "poll did not drain all cursor pages");
        assert(requests.every((request) => request.limit === 50), "page size was not bounded at 50");
        assert(requests.every((request) => request.afterId > 0), "id cursor was not sent");
        assert(requests.every((request) => !Object.hasOwn(request, "createdAt")), "timestamp cursor should not control pagination");
        assert(requests.map((request) => request.afterId).join(",") === "1,51,101", "page cursors were unstable");
        assert(sent.length === 1 && sent[0].title.includes("125 条新预警"), "drained events were not delivered once");
        assert(cursor.id === 126 && cursor.createdAt === createdAt, "cursor did not advance to the final drained event");
        function event(id) { return { id, created_at: createdAt, event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_backlog_advances_in_bounded_batches() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const pending = Array.from({ length: 125 }, (_, index) => event(index + 2));
        let requestCount = 0;
        globalThis.fetch = async (url) => {
          requestCount += 1;
          const parsed = new URL(String(url), "http://local.test");
          const afterId = Number(parsed.searchParams.get("after_id"));
          return jsonResponse(pending.filter((item) => item.id > afterId).slice(0, 50));
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          deliverAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title) { sent.push(title); }
        }
        const state = {};
        const storage = memoryStorage();
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const first = await pollAlertNotifications(state, {
          NotificationApi: FakeNotification,
          storage,
          locks,
          maxPages: 2,
        });
        const firstCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
        const second = await pollAlertNotifications(state, {
          NotificationApi: FakeNotification,
          storage,
          locks,
          maxPages: 2,
        });
        const finalCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(first === true && second === true, "bounded backlog polls failed");
        assert(firstCursor.id === 101 && finalCursor.id === 126, "bounded batches did not advance incrementally");
        assert(requestCount === 3, "bounded backlog used an unexpected number of pages");
        assert(sent.length === 2 && sent[0].includes("100 条") && sent[1].includes("25 条"), "backlog batches were not summarized");
        function event(id) { return { id, created_at: "2026-07-16 10:00:00", event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_poll_advances_by_id_when_new_event_has_older_created_at() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const requests = [];
        globalThis.fetch = async (url) => {
          const parsed = new URL(String(url), "http://local.test");
          requests.push(parsed.searchParams);
          return jsonResponse([event(6, "2026-07-16 09:00:00")]);
        };
        const { ALERT_NOTIFICATION_CURSOR_KEY, deliverAlertNotifications, pollAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(_title, options) { sent.push(options.tag); }
        }
        const state = {};
        const storage = memoryStorage();
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        deliverAlertNotifications(state, [event(5, "2026-07-16 10:00:00")], { NotificationApi: FakeNotification, storage });

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });
        const cursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(completed === true, "backdated event poll failed");
        assert(requests.length === 1 && requests[0].get("after_id") === "5", "id cursor was not used");
        assert(!requests[0].has("after_created_at"), "timestamp cursor was sent");
        assert(sent.join(",") === "ashare-radar-alert-6", "backdated event was skipped");
        assert(cursor.id === 6, "cursor did not advance to the database id");
        function event(id, created_at) { return { id, created_at, event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_poll_keeps_cursor_when_page_does_not_advance() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const createdAt = "2026-07-16 10:00:00";
        const repeatedPage = Array.from({ length: 50 }, (_, index) => event(index + 2));
        let requestCount = 0;
        globalThis.fetch = async () => {
          requestCount += 1;
          return new Response(JSON.stringify(repeatedPage), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          deliverAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        const storage = memoryStorage();
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
        const cursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(completed === false && requestCount === 2, "non-advancing pagination was not stopped");
        assert(cursor.id === 1, "partial drain advanced the persisted cursor");
        assert(sent.length === 0, "partial drain emitted notifications");
        function event(id) { return { id, created_at: createdAt, event_type: "触发", message: `事件${id}` }; }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_alert_notification_delivery_failure_retries_without_skipping_later_events() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["enableAlertNotifications", { textContent: "", disabled: false }],
          ["alertNotificationState", { textContent: "", dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const createdAt = "2026-07-16 10:00:00";
        const pending = [event(2), event(3), event(4)];
        const requestedAfterIds = [];
        globalThis.fetch = async (url) => {
          const parsed = new URL(String(url), "http://local.test");
          const afterId = Number(parsed.searchParams.get("after_id"));
          requestedAfterIds.push(afterId);
          return jsonResponse(pending.filter((item) => item.id > afterId));
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          ALERT_NOTIFICATION_FALLBACK_LOCK_KEY,
          deliverAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const attempts = [];
        const sent = [];
        let failEventThreeOnce = true;
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) {
            attempts.push(options.tag);
            if (options.tag === "ashare-radar-alert-3" && failEventThreeOnce) {
              failEventThreeOnce = false;
              throw new Error("OS notification delivery failed");
            }
            sent.push(options.tag);
          }
        }
        const state = {};
        const storage = memoryStorage();
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const firstCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });
        const failedCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(firstCompleted === false, "delivery failure was reported as a successful poll");
        assert(failedCursor.id === 2, "cursor advanced past the failed event");
        assert(!storage.getItem(ALERT_NOTIFICATION_FALLBACK_LOCK_KEY), "fallback delivery lease was left permanent");
        assert(sent.join(",") === "ashare-radar-alert-2", "events after a failure were delivered or the successful prefix was lost");
        assert(elements.get("alertNotificationState").textContent.includes("投递失败"), "delivery failure state was not visible");
        assert(elements.get("alertNotificationState").dataset.tone === "warn", "delivery failure state did not use warning tone");

        const retryCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });
        const retryCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
        const finalCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });

        assert(retryCompleted === true && finalCompleted === true, "retry did not recover polling");
        assert(requestedAfterIds.join(",") === "1,2,4", "retry did not resume from the last delivered event");
        assert(attempts.join(",") === "ashare-radar-alert-2,ashare-radar-alert-3,ashare-radar-alert-3,ashare-radar-alert-4", "failed and later events were not retried in order");
        assert(sent.join(",") === "ashare-radar-alert-2,ashare-radar-alert-3,ashare-radar-alert-4", "successful events were duplicated or skipped");
        assert(retryCursor.id === 4, "successful retry did not advance the cursor");
        assert(elements.get("alertNotificationState").textContent === "等待新触发", "successful retry did not clear the failure state");
        function event(id) {
          return { id, created_at: createdAt, event_type: "触发", stock_name: `股票${id}`, message: `事件${id}` };
        }
        function jsonResponse(value) {
          return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function memoryStorage() {
          const values = new Map();
          return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_permission_is_requested_only_by_enable_action() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["enableAlertNotifications", { textContent: "", disabled: false }],
          ["alertNotificationState", { textContent: "", dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
        const { enableAlertNotifications, stopAlertNotificationPolling } = await import("./static/js/notifications.js");
        let permissionRequests = 0;
        class FakeNotification {
          static permission = "default";
          static async requestPermission() {
            permissionRequests += 1;
            FakeNotification.permission = "granted";
            return "granted";
          }
        }
        const state = {};

        const enabled = await enableAlertNotifications(state, { NotificationApi: FakeNotification });
        stopAlertNotificationPolling(state);

        assert(enabled === true && permissionRequests === 1, "enable action did not own the permission request");
        assert(elements.get("enableAlertNotifications").disabled === false, "enabled notifications could not be stopped");
        assert(elements.get("enableAlertNotifications").textContent === "停用桌面提醒", "enabled state did not expose the stop action");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_disable_persists_and_reenable_skips_muted_events() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["enableAlertNotifications", { textContent: "", disabled: false }],
          ["alertNotificationState", { textContent: "", dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const storage = memoryStorage();
        const sent = [];
        let newestId = 5;
        let requests = 0;
        globalThis.fetch = async () => {
          requests += 1;
          return jsonResponse([event(newestId)]);
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          ALERT_NOTIFICATION_ENABLED_KEY,
          disableAlertNotifications,
          enableAlertNotifications,
          initializeAlertNotifications,
          stopAlertNotificationPolling,
        } = await import("./static/js/notifications.js");
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = {};
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };

        assert(await enableAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks }) === true, "initial enable failed");
        assert(JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY)).id === 5, "initial baseline was not saved");
        disableAlertNotifications(state, { NotificationApi: FakeNotification, storage });
        newestId = 6;
        const initialized = initializeAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });

        assert(initialized === false && state.alertNotificationTimer == null, "disabled preference restarted polling");
        assert(storage.getItem(ALERT_NOTIFICATION_ENABLED_KEY) === "0", "disabled preference was not persisted");
        assert(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY) === null, "disable kept a cursor that would backfill muted events");
        assert(elements.get("enableAlertNotifications").textContent === "启用桌面提醒", "disabled action was not rendered");
        assert(elements.get("alertNotificationState").textContent === "应用内已停用", "disabled state was not explained");

        assert(await enableAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks }) === true, "reenable failed");
        stopAlertNotificationPolling(state);
        assert(requests === 2, "disabled initialization unexpectedly polled events");
        assert(sent.length === 0, "reenable replayed an event created while notifications were disabled");
        assert(JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY)).id === 6, "reenable did not establish a fresh baseline");

        function event(id) { return { id, created_at: `2026-07-16 10:0${id}:00`, event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return {
            getItem: (key) => values.get(key) ?? null,
            setItem: (key, value) => values.set(key, value),
            removeItem: (key) => values.delete(key),
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_disable_invalidates_an_inflight_poll() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        let resolveRequest;
        globalThis.fetch = () => new Promise((resolve) => { resolveRequest = resolve; });
        const { disableAlertNotifications, pollAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, options }); }
        }
        const state = { alertNotificationsEnabled: true };
        const polling = pollAlertNotifications(state, { NotificationApi: FakeNotification });
        disableAlertNotifications(state, { NotificationApi: FakeNotification });
        resolveRequest(new Response(JSON.stringify([{
          id: 9, created_at: "2026-07-16 10:09:00", event_type: "触发", message: "不应投递",
        }]), { status: 200, headers: { "Content-Type": "application/json" } }));

        assert(await polling === false, "disabled in-flight poll reported success");
        assert(sent.length === 0, "disabled in-flight poll still delivered a notification");
        assert(state.alertNotificationPolling === false, "disabled poll left the state locked");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_permission_request_failure_remains_retryable() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["enableAlertNotifications", { textContent: "", disabled: false }],
          ["alertNotificationState", { textContent: "", dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const { enableAlertNotifications } = await import("./static/js/notifications.js");
        class FakeNotification {
          static permission = "default";
          static async requestPermission() { throw new Error("permission service unavailable"); }
        }
        const state = {};

        assert(await enableAlertNotifications(state, { NotificationApi: FakeNotification }) === false, "permission failure was reported as enabled");
        assert(state.alertNotificationsEnabled === false, "permission failure left notifications active");
        assert(elements.get("enableAlertNotifications").disabled === false, "permission failure prevented a retry");
        assert(elements.get("alertNotificationState").textContent.includes("权限请求失败"), "permission failure was not explained");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_web_lock_deduplicates_two_pages_with_a_fresh_shared_cursor() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const requests = [];
        globalThis.fetch = async (url) => {
          requests.push(String(url));
          return jsonResponse([event(2)]);
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          deliverAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const storage = memoryStorage();
        const sent = [];
        const lockCalls = [];
        let lockTail = Promise.resolve();
        const locks = {
          request(name, options, callback) {
            lockCalls.push({ name, options });
            const current = lockTail.then(() => callback({ name }));
            lockTail = current.catch(() => {});
            return current;
          },
        };
        class FakeNotification {
          static permission = "granted";
          constructor(_title, options) { sent.push(options.tag); }
        }
        const firstPage = { alertNotificationsEnabled: true };
        const secondPage = { alertNotificationsEnabled: true };
        deliverAlertNotifications(firstPage, [event(1)], { NotificationApi: FakeNotification, storage });

        const results = await Promise.all([
          pollAlertNotifications(firstPage, { NotificationApi: FakeNotification, storage, locks }),
          pollAlertNotifications(secondPage, { NotificationApi: FakeNotification, storage, locks }),
        ]);
        const cursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(results.every(Boolean), "one page reported a failed coordinated poll");
        assert(requests.length === 2 && requests.every((url) => url.includes("after_id=1")), "pages did not fetch from their initial cursor");
        assert(sent.join(",") === "ashare-radar-alert-2", "the shared event was delivered more than once");
        assert(cursor.id === 2, "shared cursor did not advance");
        assert(lockCalls.length === 2, "delivery did not use the Web Locks API");
        assert(lockCalls.every((call) => call.options.mode === "exclusive" && call.options.ifAvailable === true), "lock was not short/non-waiting");

        function event(id) { return { id, created_at: "2026-07-16 10:00:00", event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function memoryStorage() {
          const values = new Map();
          return {
            getItem: (key) => values.get(key) ?? null,
            setItem: (key, value) => values.set(key, value),
            removeItem: (key) => values.delete(key),
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_storage_changes_merge_cursor_and_stop_other_page_polling() -> None:
    _run_node(
        r'''
        globalThis.document = { getElementById() { return null; } };
        const intervalIds = new Set();
        const cleared = [];
        let nextInterval = 0;
        globalThis.setInterval = () => { const id = ++nextInterval; intervalIds.add(id); return id; };
        globalThis.clearInterval = (id) => { intervalIds.delete(id); cleared.push(id); };
        const storageTarget = eventTarget();
        const storage = memoryStorage();
        storage.setItem("ashare-radar.alert-notifications-enabled.v1", "1");
        storage.setItem("ashare-radar.alert-notification-cursor.v1", JSON.stringify({ createdAt: "", id: 5 }));
        let resolveFetch;
        let requestedUrl = "";
        globalThis.fetch = (url) => {
          requestedUrl = String(url);
          return new Promise((resolve) => { resolveFetch = resolve; });
        };
        const {
          ALERT_NOTIFICATION_CURSOR_KEY,
          ALERT_NOTIFICATION_ENABLED_KEY,
          initializeAlertNotifications,
          pollAlertNotifications,
        } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(_title, options) { sent.push(options.tag); }
        }
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        const state = {};
        assert(initializeAlertNotifications(state, { NotificationApi: FakeNotification, storage, storageTarget, locks }) === true, "page did not initialize");
        assert(intervalIds.size === 1, "polling interval did not start");

        storage.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({ createdAt: "", id: 6 }));
        const polling = pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, storageTarget, locks });
        await Promise.resolve();
        assert(requestedUrl.includes("after_id=6"), "poll did not merge the latest shared cursor");
        storage.setItem(ALERT_NOTIFICATION_ENABLED_KEY, "0");
        storageTarget.dispatch({ key: ALERT_NOTIFICATION_ENABLED_KEY, newValue: "0" });
        resolveFetch(jsonResponse([event(7)]));

        assert(await polling === false, "storage-disabled in-flight poll reported success");
        assert(state.alertNotificationsEnabled === false, "storage disable did not update page state");
        assert(state.alertNotificationTimer == null && intervalIds.size === 0 && cleared.length === 1, "storage disable did not stop polling");
        assert(sent.length === 0, "storage-disabled page delivered an event");

        function event(id) { return { id, created_at: "2026-07-16 10:00:00", event_type: "触发", message: `事件${id}` }; }
        function jsonResponse(value) { return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } }); }
        function eventTarget() {
          let handler = null;
          return {
            addEventListener(name, callback) { if (name === "storage") handler = callback; },
            removeEventListener() {},
            dispatch(event) { handler(event); },
          };
        }
        function memoryStorage() {
          const values = new Map();
          return {
            getItem: (key) => values.get(key) ?? null,
            setItem: (key, value) => values.set(key, value),
            removeItem: (key) => values.delete(key),
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_notification_delivery_fails_closed_when_shared_storage_is_unavailable() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["enableAlertNotifications", { textContent: "", disabled: false }],
          ["alertNotificationState", { textContent: "", dataset: {} }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => new Response(JSON.stringify([{
          id: 2, created_at: "2026-07-16 10:01:00", event_type: "触发", message: "不应投递",
        }]), { status: 200, headers: { "Content-Type": "application/json" } });
        const { pollAlertNotifications } = await import("./static/js/notifications.js");
        const sent = [];
        class FakeNotification {
          static permission = "granted";
          constructor(_title, options) { sent.push(options.tag); }
        }
        const storage = {
          getItem() { throw new Error("storage denied"); },
          setItem() { throw new Error("storage denied"); },
        };
        const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
        const state = {
          alertNotificationsEnabled: true,
          alertNotificationCursor: { createdAt: "", id: 1 },
        };

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage, locks });

        assert(completed === false, "uncoordinated poll reported success");
        assert(sent.length === 0, "uncoordinated page emitted a duplicate-prone notification");
        assert(elements.get("alertNotificationState").textContent.includes("无法安全协调多标签页"), "unsafe storage state was hidden");
        assert(elements.get("alertNotificationState").dataset.tone === "warn", "unsafe storage state was not a warning");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def _run_node(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Node exited without output"
        raise AssertionError(f"Node test script failed with exit code {result.returncode}:\n{detail}")
