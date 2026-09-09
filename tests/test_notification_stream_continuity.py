from __future__ import annotations

import pytest

from tests.test_frontend_notifications import _run_node


PRELUDE = r'''
const elements = new Map([
  ["enableAlertNotifications", { textContent: "", disabled: false }],
  ["alertNotificationState", { textContent: "", dataset: {} }],
]);
globalThis.document = { getElementById: (id) => elements.get(id) || null };
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
const notifications = await import("./static/js/notifications.js");
const { pollAlertNotifications, ALERT_NOTIFICATION_CURSOR_KEY } = notifications;
const A = "a".repeat(32), B = "b".repeat(32), C = "c".repeat(32);
const values = new Map();
const storage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};
const sent = [];
class NotificationApi {
  static permission = "granted";
  constructor(title, options) { sent.push({ title, ...options }); }
}
const locks = { request: (_name, _options, callback) => Promise.resolve(callback({})) };
const options = { storage, locks, NotificationApi };
const state = { alertNotificationsEnabled: true };
const cursor = () => JSON.parse(storage.getItem(ALERT_NOTIFICATION_CURSOR_KEY));
const seed = (streamId, id) => storage.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({ streamId, id }));
const event = (id) => ({ id, created_at: "2026-09-09 10:00:00", event_type: "触发", message: `事件${id}` });
const page = (stream_id, baseline_id, cursor_id, events = [], reset = false, has_more = false) => (
  { stream_id, baseline_id, cursor_id, reset, events, has_more }
);
const response = (value) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
function assert(value, message) { if (!value) throw new Error(message); }
'''


@pytest.mark.parametrize("old_id", [3, 5, 100])
def test_notification_restore_uses_persisted_baseline_not_old_id(old_id: int) -> None:
    _run_node(PRELUDE + f"seed(A, {old_id});" + r'''
        const requests = [];
        globalThis.fetch = async (url) => {
          requests.push(new URL(url, "http://localhost"));
          return response(page(B, 4, 6, [event(5), event(6)], true));
        };
        assert(await pollAlertNotifications(state, options), "restored stream did not synchronize");
        assert(requests[0].pathname === "/api/alerts/notification-events", "legacy endpoint used");
        assert(requests[0].searchParams.get("stream_id") === A, "request omitted stream identity");
        assert(cursor().streamId === B && cursor().id === 6, "restored cursor did not bind new history");
        assert(sent.length === 2, "new events before the first restored poll were skipped");
        assert(sent.every((item) => item.tag.includes(B)), "OS tags reused ids across histories");
    ''')


def test_notification_legacy_upgrade_creates_current_baseline_and_keeps_notice() -> None:
    _run_node(PRELUDE + r'''
        storage.setItem("ashare-radar.alert-notification-cursor.v1", JSON.stringify({ createdAt: "old", id: 100 }));
        storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "1");
        const urls = [];
        globalThis.fetch = async (url) => {
          urls.push(new URL(url, "http://localhost"));
          return response(urls.length === 1 ? page(B, 4, 9, [], true) : page(B, 4, 9));
        };
        assert(await pollAlertNotifications(state, options), "legacy upgrade handshake failed");
        assert(!urls[0].searchParams.has("after_id") && !urls[0].searchParams.has("stream_id"), "unbound legacy id was reused");
        assert(cursor().streamId === B && cursor().id === 9 && sent.length === 0, "upgrade replayed history");
        assert(storage.getItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY) === "1", "upgrade changed notification preference");
        assert(await pollAlertNotifications(state, options), "idle upgraded stream failed");
        assert(elements.get("alertNotificationState").textContent.includes("升级"), "next idle poll hid the migration notice");
        assert(elements.get("alertNotificationState").textContent.includes("不补发历史提醒"), "upgrade notice omitted the historical-delivery boundary");
        assert(storage.getItem("ashare-radar.alert-notification-cursor.v1") === null, "successful migration retained an active legacy cursor");
    ''')


def test_notification_pagination_discards_batch_if_stream_changes_midway() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        let calls = 0;
        globalThis.fetch = async () => response(++calls === 1
          ? page(B, 4, 54, Array.from({ length: 50 }, (_, i) => event(i + 5)), true, true)
          : page(C, 8, 9, [event(9)], true));
        assert(await pollAlertNotifications(state, options) === false, "mixed stream batch was accepted");
        assert(cursor().streamId === A && cursor().id === 100, "partial old stream changed shared cursor");
        assert(sent.length === 0, "partial old stream was delivered");
    ''')


def test_notification_late_old_response_cannot_replace_known_new_stream() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        const first = { alertNotificationsEnabled: true }, second = { alertNotificationsEnabled: true };
        let release;
        let requests = 0;
        globalThis.fetch = async () => ++requests === 1
          ? new Promise((resolve) => { release = resolve; })
          : response(page(B, 4, 5, [event(5)], true));
        const oldPoll = pollAlertNotifications(first, options);
        await Promise.resolve();
        assert(await pollAlertNotifications(second, options), "new stream adoption failed");
        release(response(page(A, 0, 101, [event(101)])));
        assert(await oldPoll === false, "late old stream was accepted after new shared identity");
        assert(cursor().streamId === B && cursor().id === 5 && sent.length === 1, "late old response changed delivery state");
    ''')


def test_notification_late_initial_response_cannot_skip_another_page_backlog() -> None:
    _run_node(PRELUDE + r'''
        let release;
        let requests = 0;
        globalThis.fetch = async () => ++requests === 1
          ? new Promise((resolve) => { release = resolve; })
          : response(page(B, 0, 5, [], true));
        const oldPoll = pollAlertNotifications(state, options);
        await Promise.resolve();
        assert(await pollAlertNotifications({ alertNotificationsEnabled: true }, options), "other page bootstrap failed");
        release(response(page(B, 0, 9, [], true)));
        assert(await oldPoll === false, "late bootstrap overwrote active stream baseline");
        assert(cursor().id === 5 && sent.length === 0, "late bootstrap silently skipped events6-9");
    ''')


def test_notification_reset_failure_retains_new_stream_and_successful_prefix() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        let fail = true;
        class FailingNotification extends NotificationApi {
          constructor(title, details) {
            if (fail && details.tag.endsWith("-6")) throw new Error("OS failure");
            super(title, details);
          }
        }
        globalThis.fetch = async (url) => {
          const params = new URL(url, "http://localhost").searchParams;
          return response(params.get("stream_id") === A
            ? page(B, 4, 7, [event(5), event(6), event(7)], true)
            : page(B, 4, 7, [event(6), event(7)]));
        };
        assert(await pollAlertNotifications(state, { ...options, NotificationApi: FailingNotification }) === false, "failed delivery reported success");
        assert(cursor().streamId === B && cursor().id === 5, "failure did not preserve new stream and successful prefix");
        fail = false;
        assert(await pollAlertNotifications(state, { ...options, NotificationApi: FailingNotification }), "failed suffix was not retried");
        assert(cursor().id === 7 && sent.length === 3, "retry duplicated prefix or skipped suffix");
    ''')


@pytest.mark.parametrize("broken", [
    "null",
    "[]",
    "({...valid, stream_id: 'bad'})",
    "({...valid, baseline_id: -1})",
    "({...valid, baseline_id: '0'})",
    "({...valid, cursor_id: Number.MAX_SAFE_INTEGER + 1})",
    "({...valid, reset: 1})",
    "({...valid, reset: true})",
    "({...valid, has_more: 'false'})",
    "({...valid, has_more: true})",
    "({...valid, events: [event(2), event(2)]})",
    "({...valid, events: [event(3), event(2)], cursor_id: 2})",
    "({...valid, events: [event(1)], cursor_id: 1})",
    "({...valid, events: [event('2')]})",
    "({...valid, cursor_id: 3})",
    "({...valid, baseline_id: 2})",
])
def test_notification_invalid_feed_preserves_cursor_without_delivery(broken: str) -> None:
    _run_node(PRELUDE + "seed(A, 1); const valid = page(A, 0, 2, [event(2)]);"
              + f"globalThis.fetch = async () => response({broken});" + r'''
        assert(await pollAlertNotifications(state, options) === false, "invalid feed was accepted");
        assert(cursor().streamId === A && cursor().id === 1 && sent.length === 0, "invalid feed advanced delivery");
    ''')


@pytest.mark.parametrize("status", [400, 422, 503])
def test_notification_http_failure_keeps_cursor_retryable(status: int) -> None:
    _run_node(PRELUDE + f"seed(A, 100); const status = {status};" + r'''
        globalThis.fetch = async () => new Response(JSON.stringify({ detail: "unavailable" }), { status });
        assert(await pollAlertNotifications(state, options) === false, "failed response accepted");
        assert(cursor().id === 100 && cursor().streamId === A && sent.length === 0, "HTTP failure changed cursor");
        globalThis.fetch = async () => response(page(B, 4, 5, [event(5)], true));
        assert(await pollAlertNotifications(state, options), "HTTP failure prevented later stream retry");
        assert(cursor().streamId === B && cursor().id === 5 && sent.length === 1, "recovery skipped new event");
    ''')


def test_notification_reset_first_delivery_failure_commits_only_stream_floor() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        class UnavailableNotification {
          static permission = "granted";
          constructor() { throw new Error("OS unavailable"); }
        }
        globalThis.fetch = async () => response(page(B, 4, 5, [event(5)], true));
        assert(await pollAlertNotifications(state, { ...options, NotificationApi: UnavailableNotification }) === false, "OS error accepted");
        assert(cursor().streamId === B && cursor().id === 4, "new stream floor was not retained before OS delivery");
        let params;
        globalThis.fetch = async (url) => {
          params = new URL(url, "http://localhost").searchParams;
          return response(page(B, 4, 5, [event(5)]));
        };
        assert(await pollAlertNotifications(state, options), "first failed notification could not retry");
        assert(params.get("stream_id") === B && params.get("after_id") === "4", "retry reused old stream number");
        assert(cursor().id === 5 && sent.length === 1, "first event skipped after retry");
    ''')


def test_notification_empty_restored_stream_uses_persisted_floor_and_no_cache() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        globalThis.fetch = async (_url, request) => {
          assert(request.cache === "no-store", "feed could use cached stream identity");
          return response(page(B, 4, 4, [], true));
        };
        assert(await pollAlertNotifications(state, options), "empty restored stream rejected");
        assert(cursor().streamId === B && cursor().id === 4 && sent.length === 0, "empty reset lost its floor");
    ''')


def test_notification_failed_second_page_does_not_commit_first_page() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        let calls = 0;
        globalThis.fetch = async () => ++calls === 1
          ? response(page(B, 4, 54, Array.from({ length: 50 }, (_, i) => event(i + 5)), true, true))
          : new Response("unavailable", { status: 503 });
        assert(await pollAlertNotifications(state, options) === false, "partial batch delivered");
        assert(cursor().streamId === A && cursor().id === 100 && sent.length === 0, "partial fetch committed reset or events");
    ''')


def test_notification_same_stream_baseline_change_during_pagination_is_rejected() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 1);
        let calls = 0;
        globalThis.fetch = async () => response(++calls === 1
          ? page(A, 0, 51, Array.from({ length: 50 }, (_, i) => event(i + 2)), false, true)
          : page(A, 1, 52, [event(52)]));
        assert(await pollAlertNotifications(state, options) === false, "changed stream floor accepted");
        assert(cursor().id === 1 && sent.length === 0, "changed floor committed partial page");
    ''')


def test_notification_cancel_and_reenable_keeps_new_poll_owner() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        let release, calls = 0;
        globalThis.fetch = async () => ++calls === 1
          ? new Promise((resolve) => { release = resolve; })
          : response(page(B, 4, 7, [], true));
        const oldPoll = pollAlertNotifications(state, options);
        await Promise.resolve();
        notifications.disableAlertNotifications(state, options);
        assert(await notifications.enableAlertNotifications(state, options), "explicit reenable failed");
        release(response(page(A, 0, 101, [event(101)])));
        assert(await oldPoll === false, "cancelled old poll was accepted after restart");
        assert(state.alertNotificationPolling === false && cursor().streamId === B && cursor().id === 7, "cancelled poll overwrote restart");
        assert(sent.length === 0, "reenable replayed muted history");
    ''')


def test_notification_cross_tab_disable_is_checked_inside_delivery_lock() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 1);
        globalThis.fetch = async () => response(page(A, 0, 2, [event(2)]));
        const lateLock = { request(_name, _options, callback) {
          storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "0");
          return Promise.resolve(callback({}));
        }};
        assert(await pollAlertNotifications(state, { ...options, locks: lateLock }) === false, "shared disable ignored until storage event");
        assert(cursor().id === 1 && sent.length === 0 && !state.alertNotificationsEnabled, "disabled event delivered");
    ''')


def test_notification_delayed_storage_events_reread_current_stream_and_preference() -> None:
    _run_node(PRELUDE + r'''
        seed(B, 5);
        storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "1");
        let handler;
        const storageTarget = { addEventListener(_type, callback) { handler = callback; } };
        assert(notifications.initializeAlertNotifications(state, { ...options, storageTarget }), "restart preference lost");
        handler({ key: ALERT_NOTIFICATION_CURSOR_KEY, newValue: JSON.stringify({ streamId: A, id: 100 }) });
        assert(state.alertNotificationCursor.streamId === B && state.alertNotificationCursor.id === 5, "delayed storage event restored old stream");
        handler({ key: notifications.ALERT_NOTIFICATION_ENABLED_KEY, newValue: "0" });
        assert(state.alertNotificationsEnabled, "stale disable event stopped newer enabled preference");
        storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "0");
        handler({ key: notifications.ALERT_NOTIFICATION_ENABLED_KEY, newValue: "1" });
        assert(!state.alertNotificationsEnabled, "stale enable event overrode current disabled preference");
    ''')


def test_notification_upgrade_notice_clears_after_actual_new_trigger() -> None:
    _run_node(PRELUDE + r'''
        storage.setItem("ashare-radar.alert-notification-cursor.v1", "100");
        globalThis.fetch = async () => response(page(A, 0, 2, [], true));
        assert(await pollAlertNotifications(state, options), "upgrade failed");
        globalThis.fetch = async () => response(page(A, 0, 3, [event(3)]));
        assert(await pollAlertNotifications(state, options), "new event after upgrade failed");
        assert(sent.length === 1 && elements.get("alertNotificationState").textContent === "等待新触发", "migration notice outlived first new trigger");
    ''')


@pytest.mark.parametrize("broken_initial", [
    "page(B, 4, 4, [], false)",
    "page(B, 4, 5, [event(5)], true)",
    "page(B, 4, 4, [], true, true)",
    "page(B, 4, 3, [], true)",
])
def test_notification_initial_sync_must_be_an_empty_valid_baseline(broken_initial: str) -> None:
    _run_node(PRELUDE + f"globalThis.fetch = async () => response({broken_initial});" + r'''
        assert(await pollAlertNotifications(state, options) === false, "invalid initial baseline accepted");
        assert(cursor() === null && sent.length === 0, "invalid bootstrap persisted or delivered history");
    ''')


def test_notification_restored_stream_paginates_from_its_floor_and_only_once() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 100);
        const requests = [];
        globalThis.fetch = async (url) => {
          requests.push(new URL(url, "http://localhost").searchParams);
          return response(requests.length === 1
            ? page(B, 4, 54, Array.from({ length: 50 }, (_, i) => event(i + 5)), true, true)
            : page(B, 4, 55, [event(55)]));
        };
        assert(await pollAlertNotifications(state, options), "restored stream pagination failed");
        assert(requests.length === 2 && requests[1].get("stream_id") === B && requests[1].get("after_id") === "54", "reset pagination reused old stream/id");
        assert(cursor().streamId === B && cursor().id === 55, "reset pagination did not advance to its own last event");
        assert(sent.length === 1 && sent[0].title.includes("51 条新预警"), "restored pages were duplicated or skipped");
    ''')


def test_notification_aborted_poll_preserves_shared_cursor_for_retry() -> None:
    _run_node(PRELUDE + r'''
        seed(A, 1);
        const controller = new AbortController();
        let release;
        globalThis.fetch = () => new Promise((resolve) => { release = resolve; });
        const cancelled = pollAlertNotifications(state, { ...options, signal: controller.signal });
        controller.abort();
        assert(await cancelled === false, "aborted poll reported success");
        release(response(page(B, 4, 5, [event(5)], true)));
        await Promise.resolve();
        assert(cursor().streamId === A && cursor().id === 1 && sent.length === 0, "aborted response altered shared state");
        globalThis.fetch = async () => response(page(B, 4, 5, [event(5)], true));
        assert(await pollAlertNotifications(state, options), "abort left polling locked");
        assert(cursor().streamId === B && cursor().id === 5 && sent.length === 1, "retry after abort lost event");
    ''')


def test_notification_upgrade_failure_keeps_legacy_until_a_successful_handshake() -> None:
    _run_node(PRELUDE + r'''
        const key = "ashare-radar.alert-notification-cursor.v1";
        storage.setItem(key, "100");
        storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "1");
        globalThis.fetch = async () => new Response("unavailable", { status: 503 });
        assert(await pollAlertNotifications(state, options) === false, "failed upgrade accepted");
        assert(storage.getItem(key) === "100" && cursor() === null, "failed upgrade discarded its retry state");
        globalThis.fetch = async () => response(page(B, 4, 9, [], true));
        assert(await pollAlertNotifications(state, options), "upgrade retry failed");
        assert(storage.getItem(key) === null && cursor().id === 9 && sent.length === 0, "upgrade retry backfilled legacy history");
    ''')
