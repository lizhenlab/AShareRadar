import { createHash } from "node:crypto";
import { expect } from "@playwright/test";
import { mockApi, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

export function noteHistoryApi() {
  const state = {
    notes: new Map([...Array.from({ length: 123 }, (_, index) => note(index + 1)), note(124, "000001.SZ")]
      .map(item => [item.id, item])),
    historyReads: [], recentReads: [], writes: [], returnedHistory: [], failNextHistory: false, hold: null,
  };
  state.notesFor = symbol => [...state.notes.values()].filter(item => item.symbol === canonical(symbol))
    .sort((left, right) => right.id - left.id);
  state.update = (id, content) => {
    const item = { ...state.notes.get(id), content, updated_at: "2026-09-09T02:00:00.000000Z" };
    item.revision = digest(item);
    state.notes.set(id, item);
    return item;
  };
  state.pauseNextHistory = () => {
    let release;
    state.hold = new Promise(resolve => { release = resolve; });
    return release;
  };
  state.handle = async (url, request) => {
    const symbol = canonical(url.searchParams.get("symbol"));
    if (url.pathname === "/api/stock/notes/page") return historyPage(state, url, symbol);
    if (url.pathname === "/api/stock/notes" && request.method() === "GET") {
      const limit = Number(url.searchParams.get("limit") || 50);
      state.recentReads.push({ symbol, limit });
      return { payload: state.notesFor(symbol).slice(0, limit) };
    }
    if (url.pathname === "/api/stock/chart-marks") return { payload: { symbol, marks: [], categories: [] } };
    if (!/^\/api\/stock\/notes\/[0-9]+$/.test(url.pathname) || request.method() !== "PATCH") return null;
    const id = Number(url.pathname.split("/").at(-1)), body = request.postDataJSON(), current = state.notes.get(id);
    state.writes.push({ id, body });
    if (current?.revision !== body.expected_revision) return { status: 409, payload: { detail: "笔记版本冲突" } };
    const { expected_revision: _expected, ...fields } = body;
    const item = { ...current, ...fields, updated_at: "2026-09-09T03:00:00.000000Z" };
    item.revision = digest(item);
    state.notes.set(id, item);
    return { payload: item };
  };
  return state;
}

async function historyPage(state, url, symbol) {
  const page = Number(url.searchParams.get("page")), pageSize = Number(url.searchParams.get("page_size"));
  const expected = url.searchParams.get("expected_snapshot"), items = state.notesFor(symbol);
  // This fake binds a page to one ordered history; repository tests cover SQLite snapshot encoding.
  const snapshot = digest({ symbol, pageSize, revisions: items.map(item => [item.id, item.revision]) });
  const read = { symbol, page, pageSize, expected, snapshot, status: 200 };
  state.historyReads.push(read);
  let result;
  if (state.failNextHistory) {
    state.failNextHistory = false;
    read.status = 503;
    result = { status: 503, payload: { detail: "隔离测试历史暂不可用" } };
  } else if (expected && expected !== snapshot) {
    read.status = 409;
    result = { status: 409, payload: { detail: "笔记历史已变化，请刷新后继续翻页" } };
  } else {
    result = { payload: { symbol, items: items.slice((page - 1) * pageSize, page * pageSize),
      total: items.length, page, page_size: pageSize, page_count: Math.ceil(items.length / pageSize), snapshot } };
  }
  const hold = state.hold;
  state.hold = null;
  if (hold) await hold;
  state.returnedHistory.push(read);
  return result;
}

function note(id, symbol = "600519.SH") {
  const item = { id, symbol, code: symbol.slice(0, 6), market: symbol.slice(-2),
    name: symbol === "600519.SH" ? "贵州茅台" : "平安银行", note_type: "观察", content: `历史笔记 ${id} 原内容`,
    price: 100, trade_date: "2026-09-08", color: null, visible: true,
    created_at: "2026-09-08T02:00:00.000000Z", updated_at: "2026-09-08T02:00:00.000000Z" };
  return { ...item, revision: digest(item) };
}

function digest(value) {
  const { revision: _revision, ...fields } = value;
  return createHash("sha256").update(JSON.stringify(fields)).digest("hex");
}

function canonical(symbol) {
  return String(symbol).startsWith("000001") ? "000001.SZ" : "600519.SH";
}

export async function openRecentNotes(page, api) {
  await mockApi(page, { api: api.handle, workbench(symbol) {
    return { ...workbenchPayload(symbol), notes: api.notesFor(symbol).slice(0, 50) };
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-tools").click();
  await expect(page.locator("#noteList [data-note-row]")).toHaveCount(50);
  await expect(page.locator("#noteHistoryStatus")).toContainText("当前显示最近 50 条");
}

export async function noteIds(page) {
  return page.locator("#noteList [data-note-row]").evaluateAll(rows => rows.map(row => Number(row.dataset.noteRow)));
}

export async function expectHistoryPage(page, number, count = 50) {
  await expect(page.locator("#noteHistoryControls")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#noteHistoryStatus")).toContainText(`共 123 条 · 第 ${number} / 3 页 · 每页 50 条`);
  await expect(page.locator("#noteList [data-note-row]")).toHaveCount(count);
}
