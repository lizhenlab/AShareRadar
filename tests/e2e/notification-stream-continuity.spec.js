import { expect, test } from "@playwright/test";

const OLD_STREAM = "a".repeat(32);
const RESTORED_STREAM = "b".repeat(32);
const LATER_STREAM = "c".repeat(32);

test("IndexedDB delivery rejects a late old stream after another page adopts restored history", async ({ context, page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium", "Chromium exercises the real cross-page IndexedDB race");
  let oldRoute;
  let requests = 0;
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/alerts/notification-events") return fulfillJson(route, []);
    expect(url.searchParams.get("stream_id")).toBe(OLD_STREAM);
    expect(url.searchParams.get("after_id")).toBe("100");
    if (++requests === 1) { oldRoute = route; return; }
    await fulfillJson(route, envelope(RESTORED_STREAM, 4, [event(5)], true));
  });
  const second = await context.newPage();
  await Promise.all([page.goto("/"), second.goto("/")]);
  await seedCursor(page, OLD_STREAM, 100);
  const oldPoll = poll(page);
  await expect.poll(() => Boolean(oldRoute)).toBe(true);
  const newResult = await poll(second);
  expect(newResult).toEqual({ completed: true, tags: [`ashare-radar-alert-${RESTORED_STREAM}-5`] });
  await fulfillJson(oldRoute, envelope(OLD_STREAM, 0, [event(101)], false));
  expect(await oldPoll).toEqual({ completed: false, tags: [] });
  expect(await readCursor(page)).toEqual({ streamId: RESTORED_STREAM, id: 5 });
});

test("a stream change inside pagination discards the batch then resumes from the new restore floor", async ({ context, page }) => {
  const requests = [];
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/alerts/notification-events") return fulfillJson(route, []);
    requests.push({ stream: url.searchParams.get("stream_id"), after: url.searchParams.get("after_id") });
    if (requests.length === 1) {
      return fulfillJson(route, envelope(RESTORED_STREAM, 4, Array.from({ length: 50 }, (_, i) => event(i + 5)), true, true));
    }
    return fulfillJson(route, envelope(LATER_STREAM, 8, [event(9), event(10)], true));
  });
  await page.goto("/");
  await seedCursor(page, OLD_STREAM, 100);
  expect(await poll(page)).toEqual({ completed: false, tags: [] });
  expect(await readCursor(page)).toEqual({ streamId: OLD_STREAM, id: 100 });
  expect(await poll(page)).toEqual({ completed: true, tags: [
    `ashare-radar-alert-${LATER_STREAM}-9`, `ashare-radar-alert-${LATER_STREAM}-10`,
  ] });
  expect(await readCursor(page)).toEqual({ streamId: LATER_STREAM, id: 10 });
  expect(requests).toEqual([
    { stream: OLD_STREAM, after: "100" },
    { stream: RESTORED_STREAM, after: "54" },
    { stream: OLD_STREAM, after: "100" },
  ]);
});

async function seedCursor(page, streamId, id) {
  await page.evaluate(({ streamId, id }) => {
    localStorage.clear();
    localStorage.setItem("ashare-radar.alert-notification-cursor.v2", JSON.stringify({ streamId, id }));
    localStorage.setItem("ashare-radar.alert-notifications-enabled.v1", "1");
  }, { streamId, id });
}

async function readCursor(page) {
  return page.evaluate(() => JSON.parse(localStorage.getItem("ashare-radar.alert-notification-cursor.v2")));
}

async function poll(page) {
  return page.evaluate(async () => {
    const { pollAlertNotifications } = await import("/static/js/notifications.js");
    const tags = [];
    class FakeNotification {
      static permission = "granted";
      constructor(_title, options) { tags.push(options.tag); }
    }
    const completed = await pollAlertNotifications(
      { alertNotificationsEnabled: true }, { NotificationApi: FakeNotification, locks: false }
    );
    return { completed, tags };
  });
}

function event(id) {
  return { id, created_at: "2026-09-09 10:00:00", event_type: "触发", message: `恢复后事件${id}` };
}

function envelope(stream_id, baseline_id, events, reset, has_more = false) {
  return { stream_id, baseline_id, cursor_id: events.at(-1)?.id ?? baseline_id, events, reset, has_more };
}

async function fulfillJson(route, payload) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
}
