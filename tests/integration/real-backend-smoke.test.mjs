import assert from "node:assert/strict";
import test from "node:test";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "@playwright/test";
import { createBackend } from "./backend-launcher.mjs";

async function createRecords(page) {
  return page.evaluate(async () => {
    const { fetchJson } = await import("/static/js/api.js");
    const post = body => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const alert = await fetchJson("/api/alerts", post({
      symbol: "600519", condition_type: "price_below", threshold: 1200,
      name: "集成测试持久预警", note: "真实HTTP与SQLite", cooldown_seconds: 300,
    }));
    const note = await fetchJson("/api/stock/notes", post({
      symbol: "600519", content: "集成测试持久笔记", price: 1288, trade_date: "2026-05-13",
      note_type: "note", color: "blue", visible: true,
    }));
    return { alert, note };
  });
}

async function readRecords(page, noteId) {
  return page.evaluate(async (id) => {
    const { fetchJson } = await import("/static/js/api.js");
    return {
      alerts: await fetchJson("/api/alerts?symbol=600519"),
      notes: await fetchJson("/api/stock/notes?symbol=600519"),
      history: await fetchJson("/api/stock/notes/page?symbol=600519&page=1&page_size=50"),
      note: await fetchJson(`/api/stock/notes/${id}`),
    };
  }, noteId);
}

function assertPersisted(records, created) {
  assert.equal(records.alerts.length, 1);
  assert.equal(records.notes.length, 1);
  assert.deepEqual(records.alerts[0], created.alert);
  assert.deepEqual(records.notes[0], created.note);
  assert.deepEqual(records.note, created.note);
  assert.deepEqual(records.history.items, [created.note]);
  assert.equal(records.history.total, 1);
  assert.equal(records.history.page_count, 1);
  assert.equal(records.history.symbol, "600519.SH");
  assert.match(records.history.snapshot, /^[0-9a-f]{64}$/);
  assert.match(records.note.revision, /^[0-9a-f]{64}$/);
  assert.equal(records.alerts[0].symbol, "600519.SH");
  assert.equal(records.alerts[0].stock_name, "贵州茅台");
  assert.equal(records.alerts[0].threshold, 1200);
  assert.equal(records.note.content, "集成测试持久笔记");
  assert.equal(records.note.price, 1288);
}

async function confirmTaskReceipt(page, requestId) {
  return page.evaluate(async (id) => {
    const { fetchJson } = await import("/static/js/api.js");
    const requestId = id || crypto.randomUUID();
    const submitted = await fetchJson(`/api/tasks/run-once?task=check_data_health&request_id=${requestId}`, { method: "POST" });
    if (submitted.receipt.request_id !== requestId) throw new Error("Wrong request receipt identity");
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const receipt = await fetchJson(`/api/tasks/requests/${requestId}`, { cache: "no-store" });
      if (["completed", "failed", "interrupted"].includes(receipt.status)) return receipt;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error("Manual request did not settle within the bounded integration wait");
  }, requestId);
}

async function inspectMaintenance(page) {
  return page.evaluate(async () => {
    const { fetchJson } = await import("/static/js/api.js");
    const status = await fetchJson("/api/system/workspace-maintenance");
    const storage = await fetchJson(`/api/system/workspace-maintenance/storage?scope_digest=${status.scope_digest}`, { method: "POST" });
    return { status, storage };
  });
}

test("browser transport preserves user records and manual receipts across backend restart and reads configured maintenance", { timeout: 60000 }, async (t) => {
  const backend = await createBackend();
  let browserLaunch = Promise.resolve(null);
  t.after(async () => {
    try {
      const browser = await browserLaunch.catch(() => null);
      await browser?.close();
    } finally { await backend.close(); }
  });
  browserLaunch = chromium.launch({ timeout: 15000 });
  const browser = await browserLaunch;
  if (t.signal.aborted) return;
  const context = await browser.newContext({ serviceWorkers: "block" });
  let origin = backend.baseURL;
  const externalRequests = [];
  await context.route("**/*", async (route) => {
    const requestOrigin = new URL(route.request().url()).origin;
    if (requestOrigin === origin) return route.continue();
    externalRequests.push(route.request().url());
    return route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.goto(`${origin}/_integration/smoke`);
  await writeFile(join(backend.directory, "runtime.maintenance.json"), JSON.stringify({
    schema_version: "workspace-maintenance-scope-v1",
    storage_roots: [{ name: "集成主库", category: "database", path: join(backend.directory, "runtime.sqlite3") }],
  }));
  const created = await createRecords(page);
  assertPersisted(await readRecords(page, created.note.id), created);
  const receipt = await confirmTaskReceipt(page);
  assert.equal(receipt.status, "completed");
  assert.equal(receipt.runs.length, 1);
  const maintenance = await inspectMaintenance(page);
  assert.equal(maintenance.status.status, "configured");
  assert.equal(maintenance.storage.status, "complete");
  assert.equal(maintenance.storage.totals.unique_file_count, 1);
  assert.ok(maintenance.storage.totals.unique_bytes > 0);
  await page.close();
  origin = await backend.restart();
  const reloaded = await context.newPage();
  reloaded.on("pageerror", error => pageErrors.push(error.message));
  await reloaded.goto(`${origin}/_integration/smoke`);
  assertPersisted(await readRecords(reloaded, created.note.id), created);
  assert.deepEqual(await confirmTaskReceipt(reloaded, receipt.request_id), receipt);
  assert.deepEqual(externalRequests, []);
  assert.deepEqual(pageErrors, []);
});
