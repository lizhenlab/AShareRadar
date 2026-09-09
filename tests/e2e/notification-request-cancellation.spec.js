import { expect, test } from "@playwright/test";

test("disabling notifications cancels a real pending browser request", async ({ context, page }) => {
  const notificationRoutes = [];
  await context.route("**/api/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/api/alerts/notification-events") {
      notificationRoutes.push(route);
      return;
    }
    await route.fulfill({ json: [] });
  });
  await page.goto("/");
  await page.evaluate(async () => {
    const notifications = await import("/static/js/notifications.js");
    const sent = [];
    class FakeNotification {
      static permission = "granted";
      constructor(title) { sent.push(title); }
    }
    localStorage.setItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY, "1");
    localStorage.setItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({
      streamId: "a".repeat(32), id: 0,
    }));
    const state = { alertNotificationsEnabled: true };
    const options = { NotificationApi: FakeNotification, storage: localStorage };
    window.notificationCancellation = {
      notifications, state, options, sent,
      polling: notifications.pollAlertNotifications(state, options),
    };
  });
  await expect.poll(() => notificationRoutes.length).toBe(1);
  const failedRequest = page.waitForEvent("requestfailed", {
    predicate: (request) => new URL(request.url()).pathname === "/api/alerts/notification-events",
  });
  await page.evaluate(() => {
    const { notifications, state, options } = window.notificationCancellation;
    notifications.disableAlertNotifications(state, options);
  });
  expect((await failedRequest).failure().errorText).toMatch(/abort|cancel/i);
  const result = await page.evaluate(async () => {
    const { notifications, state, sent, polling } = window.notificationCancellation;
    return {
      completed: await polling, polling: state.alertNotificationPolling, sent,
      enabled: localStorage.getItem(notifications.ALERT_NOTIFICATION_ENABLED_KEY),
      cursor: localStorage.getItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY),
    };
  });
  expect(result).toEqual({ completed: false, polling: false, sent: [], enabled: "0", cursor: null });
  expect(notificationRoutes).toHaveLength(1);
});
