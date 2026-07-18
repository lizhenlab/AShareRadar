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

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
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
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
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
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const first = await pollAlertNotifications(state, {
          NotificationApi: FakeNotification,
          storage,
          maxPages: 2,
        });
        const firstCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
        const second = await pollAlertNotifications(state, {
          NotificationApi: FakeNotification,
          storage,
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
        deliverAlertNotifications(state, [event(5, "2026-07-16 10:00:00")], { NotificationApi: FakeNotification, storage });

        const completed = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
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
        deliverAlertNotifications(state, [event(1)], { NotificationApi: FakeNotification, storage });

        const firstCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
        const failedCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));

        assert(firstCompleted === false, "delivery failure was reported as a successful poll");
        assert(failedCursor.id === 2, "cursor advanced past the failed event");
        assert(sent.join(",") === "ashare-radar-alert-2", "events after a failure were delivered or the successful prefix was lost");
        assert(elements.get("alertNotificationState").textContent.includes("投递失败"), "delivery failure state was not visible");
        assert(elements.get("alertNotificationState").dataset.tone === "warn", "delivery failure state did not use warning tone");

        const retryCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });
        const retryCursor = JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
        const finalCompleted = await pollAlertNotifications(state, { NotificationApi: FakeNotification, storage });

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

        assert(await enableAlertNotifications(state, { NotificationApi: FakeNotification, storage }) === true, "initial enable failed");
        assert(JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY)).id === 5, "initial baseline was not saved");
        disableAlertNotifications(state, { NotificationApi: FakeNotification, storage });
        newestId = 6;
        const initialized = initializeAlertNotifications(state, { NotificationApi: FakeNotification, storage });

        assert(initialized === false && state.alertNotificationTimer == null, "disabled preference restarted polling");
        assert(storage.getItem(ALERT_NOTIFICATION_ENABLED_KEY) === "0", "disabled preference was not persisted");
        assert(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY) === null, "disable kept a cursor that would backfill muted events");
        assert(elements.get("enableAlertNotifications").textContent === "启用桌面提醒", "disabled action was not rendered");
        assert(elements.get("alertNotificationState").textContent === "应用内已停用", "disabled state was not explained");

        assert(await enableAlertNotifications(state, { NotificationApi: FakeNotification, storage }) === true, "reenable failed");
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


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
