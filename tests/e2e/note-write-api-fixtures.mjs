import { createHash } from "node:crypto";
import { expect } from "@playwright/test";
import { mockApi, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

export function noteApi() {
  const state = { notes: new Map([note(7), note(8), note(9, "000001.SZ")].map(item => [item.id, item])),
    writes: [], reads: [], completions: [], failNotes: false, hold: null, serial: 0 };
  state.pauseNextWrite = () => {
    let release;
    state.hold = new Promise(resolve => { release = resolve; });
    return release;
  };
  state.handle = async (url, request) => {
    if (url.pathname === "/api/stock/notes" && request.method() === "GET") {
      state.reads.push(url.pathname);
      return state.failNotes ? { status: 503, payload: { detail: "隔离测试列表暂不可用" } }
        : { payload: notesFor(state, url.searchParams.get("symbol")) };
    }
    if (url.pathname === "/api/stock/chart-marks") {
      return { payload: { symbol: canonical(url.searchParams.get("symbol")), marks: [], categories: [] } };
    }
    if (!/^\/api\/stock\/notes\/[0-9]+$/.test(url.pathname)) return null;
    const id = Number(url.pathname.split("/").at(-1));
    if (request.method() === "GET") {
      state.reads.push(url.pathname);
      return { payload: state.notes.get(id) };
    }
    return writeNote(state, id, url, request);
  };
  return state;
}

async function writeNote(state, id, url, request) {
  const method = request.method();
  const body = method === "PATCH" ? request.postDataJSON() : {};
  const expected = method === "DELETE" ? url.searchParams.get("expected_revision") : body.expected_revision;
  const attempt = { id, method, body, expected, status: null };
  state.writes.push(attempt);
  const hold = state.hold;
  state.hold = null;
  if (hold) await hold;
  const current = state.notes.get(id);
  if (!current || current.revision !== expected) {
    attempt.status = 409;
    return { status: 409, payload: { detail: "笔记版本冲突，原草稿已保留" } };
  }
  if (method === "DELETE") state.notes.delete(id);
  else {
    const { expected_revision, ...fields } = body;
    const saved = { ...current, ...fields, updated_at: `2026-09-09T02:00:${String(++state.serial).padStart(2, "0")}.000000Z` };
    saved.revision = revision(saved);
    state.notes.set(id, saved);
  }
  attempt.status = 200;
  state.completions.push(id);
  return { payload: method === "DELETE" ? { ok: true, removed: true } : state.notes.get(id) };
}

function note(id, symbol = "600519.SH") {
  const item = { id, symbol, code: symbol.slice(0, 6), market: symbol.slice(-2),
    name: symbol === "600519.SH" ? "贵州茅台" : "平安银行", note_type: "观察", content: `笔记${id}原内容`,
    price: 100, trade_date: "2026-09-08", color: null, visible: true,
    created_at: "2026-09-08T02:00:00.000000Z", updated_at: "2026-09-08T02:00:00.000000Z" };
  return { ...item, revision: revision(item) };
}

function revision(item) {
  // This fake models conditional-write identity; backend tests cover raw SQLite token encoding.
  const { revision: _revision, ...fields } = item;
  return createHash("sha256").update(JSON.stringify(fields)).digest("hex");
}

function canonical(symbol) {
  return String(symbol).startsWith("000001") ? "000001.SZ" : "600519.SH";
}

function notesFor(api, symbol) {
  return [...api.notes.values()].filter(item => item.symbol === canonical(symbol));
}

export async function openNotes(page, api) {
  await mockApi(page, { api: api.handle, workbench(symbol) {
    return { ...workbenchPayload(symbol), notes: notesFor(api, symbol) };
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-tools").click();
  await expect(page.locator("[data-note-edit]")).toHaveCount(2);
}

export function noteForm(page, id) {
  return page.locator(`[data-note-edit-form][data-note-id="${id}"]`);
}

export async function editNote(page, id, content) {
  await page.locator(`[data-note-edit="${id}"]`).click();
  await noteForm(page, id).locator('[name="content"]').fill(content);
}

export async function saveNote(page, id) {
  await noteForm(page, id).locator('button[type="submit"]').click();
}

export async function switchStock(page, symbol, name) {
  if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
  await page.locator("#symbolInput").fill(symbol);
  await page.locator("#searchForm button").click();
  await expect(page.locator("#stockName")).toHaveText(name);
}
