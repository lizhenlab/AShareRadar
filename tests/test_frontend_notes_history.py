"""Explicit note history reads preserve page, stock, and draft ownership."""

from __future__ import annotations

import pytest

from tests.test_frontend_notes_alerts_requests import _run_node_script


def test_notes_recent_load_uses_fifty_and_workbench_does_not_fetch_history() -> None:
    _run_node_script(SETUP + r'''
      let calls = 0;
      globalThis.fetch = async url => {
        calls += 1;
        assert(String(url).endsWith("&limit=50"), "recent refresh reverted to eight rows");
        return jsonResponse([historyNote(1)]);
      };
      assert(renderWorkbenchNotes(state, [historyNote(2)], state.symbol), "workbench notes were not rendered");
      assert(calls === 0 && !state.notesHistory.active, "initial workbench fetched all history");
      assert(await loadNotes(state), "ordinary note refresh failed");
      assert(calls === 1 && !state.notesHistory.active, "ordinary refresh unexpectedly enabled history");
      assert(dom.element("noteHistoryStatus").textContent.includes("最近 1 条"), "recent count was not displayed");
    ''')


def test_history_pagination_binds_snapshot_and_survives_workbench_refresh() -> None:
    _run_node_script(SETUP + r'''
      const calls = [];
      globalThis.fetch = async (url, options) => {
        const query = new URL(String(url), "http://local").searchParams;
        calls.push({ query, options });
        return jsonResponse(historyPage(Number(query.get("page"))));
      };
      assert(await loadNoteHistory(state), "first history page failed");
      assert(await loadNoteHistory(state, "next"), "next history page failed");
      assert(calls[0].query.get("expected_snapshot") === null, "first page reused an old snapshot");
      assert(calls[1].query.get("expected_snapshot") === "a".repeat(64), "turning page was not bound to the first snapshot");
      assert(calls.every(call => call.options.cache === "no-store" && call.query.get("page_size") === "50"), "history caching or size contract changed");
      const rendered = dom.element("noteList").innerHTML;
      const source = state.researchActivityNoteSource;
      assert(!renderWorkbenchNotes(state, [historyNote(999)], state.symbol), "background workbench overwrote an active history page");
      assert(dom.element("noteList").innerHTML === rendered && state.researchActivityNoteSource === source, "background refresh replaced history state");
      assert(state.notesHistory.page === 2 && state.researchActivityNotes.length === 50, "wrong history page was installed");
      assert(dom.element("noteHistoryStatus").textContent.includes("共 120 条 · 第 2 / 3 页"), "total/page status was absent");
    ''')


def test_history_conflict_keeps_old_page_until_explicit_refresh() -> None:
    _run_node_script(SETUP + r'''
      let conflict = false;
      let refreshFails = false;
      const queries = [];
      globalThis.fetch = async url => {
        const query = new URL(String(url), "http://local").searchParams;
        queries.push(query);
        if (conflict && query.has("expected_snapshot")) return errorResponse(409, "历史已变化");
        if (refreshFails) return errorResponse(503, "暂不可用");
        return jsonResponse(historyPage(Number(query.get("page")), conflict ? 121 : 120, conflict ? "b" : "a"));
      };
      await loadNoteHistory(state);
      const rendered = dom.element("noteList").innerHTML;
      conflict = true;
      assert(!await loadNoteHistory(state, "next"), "conflicting history page was accepted");
      assert(dom.element("noteList").innerHTML === rendered && state.notesHistory.page === 1, "conflict discarded or advanced the old page");
      assert(state.notesHistory.stale && dom.element("noteHistoryNext").disabled, "stale page remained pageable");
      assert(dom.element("noteHistoryStatus").textContent.includes("请点击刷新"), "conflict lacked an explicit recovery action");
      assert(!await loadNoteHistory(state, "next") && queries.length === 2, "stale navigation silently retried");
      refreshFails = true;
      assert(!await loadNoteHistory(state, "refresh"), "failed refresh was accepted");
      assert(state.notesHistory.stale && dom.element("noteHistoryNext").disabled, "a failed refresh restored a known stale snapshot");
      assert(!await loadNoteHistory(state, "next") && queries.length === 3, "failed refresh reopened stale navigation");
      refreshFails = false;
      assert(await loadNoteHistory(state, "refresh"), "explicit refresh did not recover");
      assert(!queries.at(-1).has("expected_snapshot") && !state.notesHistory.stale, "refresh kept the stale snapshot");
      assert(state.notesHistory.total === 121, "new total was not installed");
    ''')


def test_history_delete_last_row_moves_to_last_valid_page() -> None:
    _run_node_script(SETUP + r'''
      let deleted = false;
      const reads = [];
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "DELETE") { deleted = true; return jsonResponse({ ok: true, removed: true }); }
        const query = new URL(String(url), "http://local").searchParams;
        reads.push(query);
        return jsonResponse(historyPage(Number(query.get("page")), deleted ? 50 : 51, deleted ? "b" : "a"));
      };
      await loadNoteHistory(state);
      await loadNoteHistory(state, "next");
      assert(state.notesHistory.page === 2 && state.researchActivityNotes.length === 1, "last page fixture failed");
      assert(await removeStockNote(state, 1, async () => {}, { expectedRevision: "a".repeat(64) }), "delete was not acknowledged");
      assert(reads[2].get("page") === "2" && !reads[2].has("expected_snapshot"), "write readback reused a stale snapshot or reset page prematurely");
      assert(reads[3].get("page") === "1" && reads[3].get("expected_snapshot") === "b".repeat(64), "last-page fallback lost its new snapshot");
      assert(state.notesHistory.page === 1 && state.notesHistory.total === 50 && state.researchActivityNotes.length === 50, "delete left an empty page beyond the end");
    ''')


def test_history_update_refreshes_current_page_without_old_snapshot() -> None:
    _run_node_script(SETUP + r'''
      let saved = false;
      const queries = [];
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "PATCH") { saved = true; return jsonResponse({ ...historyNote(70), content: "修改成功" }); }
        const query = new URL(String(url), "http://local").searchParams;
        queries.push(query);
        const page = historyPage(Number(query.get("page")), 120, saved ? "b" : "a");
        if (saved) page.items[0].content = "修改成功";
        return jsonResponse(page);
      };
      await loadNoteHistory(state);
      await loadNoteHistory(state, "next");
      assert(await updateStockNote(state, 70, { content: "修改成功", expected_revision: "a".repeat(64) }, async () => {}), "update failed");
      assert(queries.at(-1).get("page") === "2" && !queries.at(-1).has("expected_snapshot"), "write refresh reset page or retained old snapshot");
      assert(state.notesHistory.page === 2 && dom.element("noteList").innerHTML.includes("修改成功"), "current page did not reflect the write");
    ''')


@pytest.mark.parametrize("mutation", [
    'page.symbol = "000001.SZ";', 'page.page = 3;', 'page.page_size = 25;',
    'page.total = 120.5;', 'page.page_count = 4;', 'page.snapshot = "b".repeat(64);',
    'page.items.pop();', 'page.items[1].id = page.items[0].id;',
    'page.items[0].id = 120;', 'page.items[0].code = "000001";',
    'page.items[0].symbol = "000001.SZ";', 'page.items[0].revision = "bad";',
    'page.items[0].price = "10";', 'page.items[0].visible = "true";',
])
def test_invalid_history_page_never_replaces_current_page(mutation: str) -> None:
    _run_node_script(SETUP + r'''
      globalThis.fetch = async () => jsonResponse(historyPage(1));
      await loadNoteHistory(state);
      const rendered = dom.element("noteList").innerHTML;
      const page = historyPage(2);
    ''' + mutation + r'''
      globalThis.fetch = async () => jsonResponse(page);
      assert(!await loadNoteHistory(state, "next"), "malformed page was accepted");
      assert(dom.element("noteList").innerHTML === rendered && state.notesHistory.page === 1, "malformed page replaced current notes");
      assert(state.notesHistory.stale && dom.element("noteHistoryStatus").textContent.includes("不匹配"), "malformed page did not require refresh");
    ''')


def test_history_switch_cancels_pending_read_and_resets_pagination() -> None:
    _run_node_script(SETUP + r'''
      const pending = deferredReply();
      let oldSignal;
      globalThis.fetch = async (_url, options) => { oldSignal = options.signal; return pending.promise; };
      const oldRead = loadNoteHistory(state);
      await Promise.resolve();
      state.symbol = "000001.SZ";
      resetNoteHistory(state);
      assert(oldSignal.aborted && !state.notesHistory.active && state.notesHistory.snapshot === null, "stock switch retained old read/page ownership");
      globalThis.fetch = async () => jsonResponse(historyPage(1, 2, "c", state.symbol));
      assert(await loadNoteHistory(state), "new stock history failed");
      const rendered = dom.element("noteList").innerHTML;
      pending.resolve(jsonResponse(historyPage(1)));
      assert(!await oldRead, "old stock response claimed current ownership");
      assert(dom.element("noteList").innerHTML === rendered && state.notesHistory.symbol === "000001.SZ", "late response polluted the new stock");
    ''')


def test_history_navigation_preserves_open_same_stock_editor() -> None:
    _run_node_script(SETUP + r'''
      globalThis.fetch = async url => jsonResponse(historyPage(Number(new URL(String(url), "http://local").searchParams.get("page"))));
      await loadNoteHistory(state);
      const list = dom.element("noteList");
      const row = { querySelector() { return { setAttribute() {} }; } };
      const form = { hidden: false, dataset: { noteId: "120" }, closest() { return row; }, contains() { return false; } };
      list.querySelectorAll = () => [form];
      list.querySelector = () => null;
      list.append = item => { list.retained = item; };
      assert(await loadNoteHistory(state, "next"), "page navigation failed");
      assert(list.retained === row && !form.hidden, "paging silently discarded an editor absent from the new page");
      assert(state.notesHistory.page === 2, "preserved draft prevented valid page navigation");
    ''')


SETUP = r'''
  import { loadNotes, loadNoteHistory, renderWorkbenchNotes, removeStockNote, updateStockNote } from "./static/js/notes.js";
  import { resetNoteHistory } from "./static/js/notes-history.js";
  const dom = installNotesAlertsDom();
  const state = { symbol: "600519.SH", lastAnalysis: null };
  function historyNote(id, symbol = "600519.SH") {
    return { id, symbol, code: symbol.slice(0, 6), market: symbol.slice(7), name: "股票",
      revision: "a".repeat(64), note_type: "观察", content: `历史笔记 ${id}`, price: 10,
      trade_date: "2026-10-14", color: null, visible: true,
      created_at: "2026-10-14T10:00:00.000000Z", updated_at: "2026-10-14T10:00:00.000000Z" };
  }
  function historyPage(page, total = 120, snapshot = "a", symbol = "600519.SH") {
    const offset = (page - 1) * 50;
    const count = Math.min(50, Math.max(0, total - offset));
    return { symbol, page, page_size: 50, total, page_count: Math.ceil(total / 50), snapshot: snapshot.repeat(64),
      items: Array.from({ length: count }, (_, index) => historyNote(total - offset - index, symbol)) };
  }
'''
