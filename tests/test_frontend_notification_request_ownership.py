from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stop_mode", ["same-page", "storage"])
def test_disabling_notifications_aborts_pending_page_and_stops_backlog(stop_mode: str) -> None:
    _run_node(
        f'const stopMode = "{stop_mode}";'
        + r'''
        const requests = [];
        const first = deferred();
        globalThis.fetch = (url, options) => {
          requests.push({ url, signal: options.signal });
          return requests.length === 1 ? first.promise : Promise.resolve(page(50, 100));
        };
        notifications.initializeAlertNotifications(state, options);
        const polling = notifications.pollAlertNotifications(state, options);
        if (stopMode === "same-page") notifications.disableAlertNotifications(state, options);
        else {
          storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "0");
          storageTarget.dispatch({ key: notifications.ALERT_NOTIFICATION_ENABLED_KEY });
        }
        const abortedAtStop = requests[0].signal.aborted;
        first.resolve(page(0, 100));
        const completed = await polling;
        assert.equal(completed, false);
        assert.equal(sent.length, 0);
        assert.equal(state.alertNotificationPolling, false);
        assert.equal(intervals.size, 0);
        assert.equal(abortedAtStop, true, "disabling must abort the active HTTP request");
        assert.equal(requests.length, 1, "disabled poll must not fetch later backlog pages");
        '''
    )


def test_old_poll_cleanup_cannot_cancel_or_clear_immediately_reenabled_poll() -> None:
    _run_node(r'''
        const requests = [];
        const first = deferred();
        const second = deferred();
        globalThis.fetch = (url, options) => {
          requests.push({ url, signal: options.signal });
          return requests.length === 1 ? first.promise : second.promise;
        };
        const oldPolling = notifications.pollAlertNotifications(state, options);
        notifications.disableAlertNotifications(state, options);
        const enabling = notifications.enableAlertNotifications(state, options);
        const currentToken = state.alertNotificationPollToken;
        first.resolve(page(0, 1));
        assert.equal(await oldPolling, false);
        assert.equal(state.alertNotificationPollToken, currentToken);
        assert.equal(state.alertNotificationPolling, true);
        assert.equal(requests[0].signal.aborted, true);
        assert.equal(requests[1].signal.aborted, false);
        second.resolve(baseline(100));
        assert.equal(await enabling, true);
        assert.equal(state.alertNotificationPolling, false);
        assert.equal(state.alertNotificationPollToken, null);
        assert.deepEqual(readCursor(), { streamId: stream, id: 100 });
        assert.equal(sent.length, 0, "reenabling establishes a baseline without historical delivery");
        assert.equal(requests.length, 2);
        notifications.stopAlertNotificationPolling(state);
    ''')


def test_external_abort_releases_request_without_changing_notification_preference_or_cursor() -> None:
    _run_node(r'''
        const parent = new AbortController();
        const pending = deferred();
        const requests = [];
        globalThis.fetch = (url, options) => {
          requests.push({ url, signal: options.signal });
          return pending.promise;
        };
        const polling = notifications.pollAlertNotifications(state, { ...options, signal: parent.signal });
        parent.abort();
        assert.equal(await polling, false);
        assert.equal(requests[0].signal.aborted, true);
        assert.equal(state.alertNotificationPolling, false);
        assert.equal(state.alertNotificationsEnabled, true);
        assert.equal(storage.getItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY), "1");
        assert.deepEqual(readCursor(), { streamId: stream, id: 0 });
        pending.resolve(page(0, 100));
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(requests.length, 1);
        assert.equal(sent.length, 0);
    ''')


def test_external_abort_while_waiting_for_delivery_lock_prevents_notification_and_cursor_commit() -> None:
    _run_node(r'''
        const parent = new AbortController();
        const entered = deferred();
        const release = deferred();
        globalThis.fetch = async () => page(0, 1);
        const locks = { request: async (_name, _options, callback) => {
          entered.resolve();
          await release.promise;
          return callback({});
        } };
        const polling = notifications.pollAlertNotifications(state, { ...options, locks, signal: parent.signal });
        await entered.promise;
        parent.abort();
        release.resolve();
        assert.equal(await polling, false, "cancelled poll cannot commit after acquiring the delivery lock");
        assert.equal(sent.length, 0);
        assert.deepEqual(readCursor(), { streamId: stream, id: 0 });
        assert.equal(state.alertNotificationPolling, false);
    ''')


def test_scoped_poll_preserves_bounded_pages_deduplication_and_committed_cursor() -> None:
    _run_node(r'''
        const requested = [];
        globalThis.fetch = async (url, options) => {
          assert.equal(options.signal.aborted, false);
          const after = Number(new URL(url, "http://test").searchParams.get("after_id"));
          requested.push(after);
          return page(after, 125);
        };
        assert.equal(await notifications.pollAlertNotifications(state, { ...options, maxPages: 2 }), true);
        assert.deepEqual(requested, [0, 50]);
        assert.deepEqual(readCursor(), { streamId: stream, id: 100 });
        assert.equal(sent.length, 1);
        assert.equal(await notifications.pollAlertNotifications(state, options), true);
        assert.deepEqual(requested, [0, 50, 100]);
        assert.deepEqual(readCursor(), { streamId: stream, id: 125 });
        assert.equal(sent.length, 2);
        assert.equal(await notifications.pollAlertNotifications(state, options), true);
        assert.deepEqual(requested, [0, 50, 100, 125]);
        assert.equal(sent.length, 2, "already committed events cannot be delivered again");
    ''')


def _run_node(script: str) -> None:
    harness = r'''
        import assert from "node:assert/strict";
        const notifications = await import("./static/js/notifications.js");
        globalThis.document = { getElementById() { return null; } };
        const stream = "a".repeat(32);
        const sent = [];
        const intervals = new Set();
        globalThis.setInterval = () => { const id = {}; intervals.add(id); return id; };
        globalThis.clearInterval = (id) => intervals.delete(id);
        const values = new Map();
        const storage = {
          getItem: (key) => values.get(key) ?? null,
          setItem: (key, value) => values.set(key, value),
          removeItem: (key) => values.delete(key),
        };
        let storageHandler;
        const storageTarget = {
          addEventListener(_name, callback) { storageHandler = callback; },
          dispatch(event) { storageHandler(event); },
        };
        class FakeNotification {
          static permission = "granted";
          constructor(title, options) { sent.push({ title, ...options }); }
        }
        const options = { NotificationApi: FakeNotification, storage, storageTarget,
          locks: { request: (_name, _options, callback) => Promise.resolve(callback({})) } };
        const state = { alertNotificationsEnabled: true };
        storage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "1");
        storage.setItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({ streamId: stream, id: 0 }));
        function readCursor() { return JSON.parse(storage.getItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY)); }
        function deferred() {
          let resolve;
          const promise = new Promise((complete) => { resolve = complete; });
          return { promise, resolve };
        }
        function response(payload) { return new Response(JSON.stringify(payload), {
          status: 200, headers: { "Content-Type": "application/json" },
        }); }
        function baseline(id) { return response({ stream_id: stream, baseline_id: 0,
          cursor_id: id, reset: true, events: [], has_more: false }); }
        function page(after, end) {
          const last = Math.min(after + 50, end);
          const events = Array.from({ length: last - after }, (_, index) => ({
            id: after + index + 1, created_at: "2026-09-10 10:00:00", event_type: "触发", message: "测试事件",
          }));
          return response({ stream_id: stream, baseline_id: 0, cursor_id: last,
            reset: false, events, has_more: last < end });
        }
    '''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + script],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
