import { expect, test } from "@playwright/test";

test("IndexedDB fallback delivers a shared alert at most once across two pages", async ({ context, page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium", "one real Chromium project is sufficient for the cross-page race");

  const pendingAlertRoutes = [];
  await context.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/alerts/events") {
      pendingAlertRoutes.push(route);
      if (pendingAlertRoutes.length === 2) {
        await Promise.all(pendingAlertRoutes.splice(0).map((pending) => fulfillJson(pending, [{
          id: 2,
          created_at: "2026-07-19 10:01:00",
          event_type: "触发",
          stock_name: "并发测试",
          message: "只能投递一次",
        }])));
      }
      return;
    }
    await fulfillJson(route, []);
  });

  const secondPage = await context.newPage();
  await Promise.all([page.goto("/"), secondPage.goto("/")]);
  await page.evaluate(async () => {
    const { ALERT_NOTIFICATION_COORDINATION_DB_NAME, ALERT_NOTIFICATION_CURSOR_KEY } = await import(
      "/static/js/notifications.js?notification-race-setup=1"
    );
    localStorage.clear();
    localStorage.setItem(ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({ createdAt: "", id: 1 }));
    await new Promise((resolve, reject) => {
      const request = indexedDB.deleteDatabase(ALERT_NOTIFICATION_COORDINATION_DB_NAME);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("notification coordination database is blocked"));
    });
  });

  const poll = (target) => target.evaluate(async () => {
    const { pollAlertNotifications } = await import("/static/js/notifications.js?notification-race=1");
    const tags = [];
    class FakeNotification {
      static permission = "granted";
      constructor(_title, options) { tags.push(options.tag); }
    }
    const completed = await pollAlertNotifications(
      { alertNotificationsEnabled: true },
      { NotificationApi: FakeNotification, locks: false }
    );
    return { completed, tags };
  });

  const outcomes = await Promise.all([poll(page), poll(secondPage)]);
  expect(outcomes.flatMap((outcome) => outcome.tags)).toEqual(["ashare-radar-alert-2"]);
  expect(outcomes.every((outcome) => outcome.completed)).toBe(true);
  await expect.poll(() => pendingAlertRoutes.length).toBe(0);
  const cursor = await secondPage.evaluate(() => JSON.parse(
    localStorage.getItem("ashare-radar.alert-notification-cursor.v1")
  ));
  expect(cursor.id).toBe(2);
});

async function fulfillJson(route, payload, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(payload),
  });
}
