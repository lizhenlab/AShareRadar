from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_watchlist_add_survives_stale_load_abort_and_blocks_duplicate_submit() -> None:
    script = r'''
      import { addWatchlistItem } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = { symbol: "600519.SH", watchlist: [] };
      const parent = new AbortController();
      const writeReply = deferredReply();
      const calls = [];
      let committedWrites = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url === "/api/watchlist" && options.method === "POST") {
          const response = await writeReply.promise;
          committedWrites += 1;
          return response;
        }
        if (call.url === "/api/watchlist") return jsonResponse([watchItem("600519.SH", "贵州茅台")]);
        throw new Error(`unexpected request: ${call.url}`);
      };
      dom.element("watchSymbolInput").value = "600519";
      dom.element("watchNoteInput").value = "旧股票关注";
      const options = {
        symbol: "600519.SH",
        signal: parent.signal,
        isCurrent: () => state.symbol === "600519.SH" && !parent.signal.aborted,
      };

      const addition = addWatchlistItem(state, options);
      await Promise.resolve();
      const duplicateResult = await addWatchlistItem(state, options);
      assert(duplicateResult === false, "pending watchlist add did not block a duplicate submit");
      assert(calls.length === 1, `duplicate submit sent another write: ${calls.length}`);
      assert(dom.element("watchForm").attributes["aria-busy"] === "true", "pending watchlist form did not expose loading state");

      const write = calls[0];
      state.symbol = "000001.SZ";
      dom.element("watchSymbolInput").value = "000001";
      dom.element("watchNoteInput").value = "新股票草稿";
      dom.element("watchList").innerHTML = "新股票自选面板";
      parent.abort();
      assert(!write.options.signal.aborted, "stock switch aborted the in-flight watchlist write");

      writeReply.resolve(jsonResponse({}));
      const result = await addition;

      assert(result === false, "stale watchlist write applied its old UI tail");
      assert(committedWrites === 1, "watchlist write did not finish after the old load was aborted");
      assert(calls.length === 2 && calls[1].url === "/api/watchlist" && !write.options.signal.aborted, "committed stale write was not read back globally");
      assert(dom.element("watchList").innerHTML.includes("贵州茅台"), "global readback did not update the watchlist");
      assert(dom.element("watchSymbolInput").value === "000001" && dom.element("watchNoteInput").value === "新股票草稿", "old watchlist completion cleared the new stock form");
      assert(!dom.button.disabled && dom.button.textContent === "加入", "watchlist button did not recover after the detached write");
      assert(dom.element("watchForm").attributes["aria-busy"] === "false", "watchlist form did not clear loading state");
    '''
    _run_node_script(script)


def test_independent_watchlist_add_and_delete_do_not_abort_each_other() -> None:
    script = r'''
      import { addWatchlistItem, removeWatchlistItem } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = { watchlist: [watchItem("600000.SH", "浦发银行")] };
      const addReply = deferredReply();
      const deleteReply = deferredReply();
      const calls = [];
      let backendItems = [];
      let successes = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url === "/api/watchlist" && options.method === "POST") return addReply.promise;
        if (call.url.startsWith("/api/watchlist/") && options.method === "DELETE") return deleteReply.promise;
        if (call.url === "/api/watchlist") return jsonResponse(backendItems);
        throw new Error(`unexpected request: ${call.url}`);
      };
      const options = { onMutationSuccess() { successes += 1; } };

      const addition = addWatchlistItem(state, options);
      const removal = removeWatchlistItem(state, "600000.SH", options);
      await Promise.resolve();
      const writes = calls.filter((call) => call.options.method === "POST" || call.options.method === "DELETE");
      assert(writes.length === 2, `expected two watchlist writes, got ${writes.length}`);
      assert(writes.every((call) => !call.options.signal.aborted), "one watchlist write aborted the other");

      backendItems = [];
      deleteReply.resolve(jsonResponse(null));
      const removeResult = await removal;
      assert(removeResult === true && !writes[0].options.signal.aborted, "delete completion cancelled the pending add");

      backendItems = [watchItem("000001.SZ", "平安银行")];
      addReply.resolve(jsonResponse({}));
      const addResult = await addition;

      assert(addResult === true, "independent watchlist add was silently lost");
      assert(writes.every((call) => !call.options.signal.aborted), "a watchlist write signal was aborted after completion");
      assert(successes === 2, `not all successful writes were confirmed: ${successes}`);
      assert(state.watchlist.length === 1 && state.watchlist[0].symbol === "000001.SZ", "final watchlist readback was not applied");
      assert(!dom.button.disabled && dom.button.textContent === "加入", "watchlist add button did not recover");
    '''
    _run_node_script(script)


def test_delete_success_is_locally_confirmed_when_readback_fails() -> None:
    script = r'''
      import { removeWatchlistItem } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = {
        watchlist: [
          watchItem("600000.SH", "浦发银行"),
          watchItem("000001.SZ", "平安银行"),
        ],
      };
      let successCount = 0;
      let readbackError = "";
      globalThis.fetch = async (url, options = {}) => {
        if (String(url).startsWith("/api/watchlist/") && options.method === "DELETE") return jsonResponse(null);
        if (String(url) === "/api/watchlist") {
          return { ok: false, status: 503, async json() { return { detail: "回读服务暂不可用" }; } };
        }
        throw new Error(`unexpected request: ${url}`);
      };

      const result = await removeWatchlistItem(state, "600000.SH", {
        onMutationSuccess() { successCount += 1; },
        onReadbackError(error) { readbackError = error.message; },
      });

      assert(result === true, "successful DELETE was reported as a failed deletion");
      assert(successCount === 1, "successful DELETE was not confirmed before readback");
      assert(readbackError === "回读服务暂不可用", `readback degradation was not surfaced: ${readbackError}`);
      assert(state.watchlist.length === 1 && state.watchlist[0].symbol === "000001.SZ", "deleted symbol remained in local state");
      assert(dom.element("watchList").innerHTML.includes("平安银行"), "remaining local watchlist was not rendered");
      assert(!dom.element("watchList").innerHTML.includes("浦发银行"), "deleted row returned after failed readback");
      assert(dom.element("watchList").innerHTML.includes("已删除，列表同步降级"), "failed readback did not render a degradation hint");
      assert(!dom.element("watchList").innerHTML.includes("删除失败"), "failed GET was misreported as DELETE failure");
    '''
    _run_node_script(script)


def test_delete_commit_stays_global_after_stock_switch_and_failed_reads() -> None:
    script = r'''
      import { loadWatchlist, removeWatchlistItem } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = {
        symbol: "600519.SH",
        watchlist: [
          watchItem("600000.SH", "浦发银行"),
          watchItem("000001.SZ", "平安银行"),
        ],
      };
      const oldLoad = new AbortController();
      const newLoad = new AbortController();
      const deleteReply = deferredReply();
      const calls = [];
      let getCalls = 0;
      let contextualEffects = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url.startsWith("/api/watchlist/") && options.method === "DELETE") {
          return deleteReply.promise;
        }
        if (call.url === "/api/watchlist") {
          getCalls += 1;
          const detail = getCalls === 1 ? "新股票主查询失败" : "删除后回读失败";
          return { ok: false, status: 503, async json() { return { detail }; } };
        }
        throw new Error(`unexpected request: ${call.url}`);
      };
      const oldOptions = {
        signal: oldLoad.signal,
        isCurrent: () => state.symbol === "600519.SH" && !oldLoad.signal.aborted,
        onMutationSuccess() { contextualEffects += 1; },
        onReadbackError() { contextualEffects += 1; },
      };

      const removal = removeWatchlistItem(state, "600000.SH", oldOptions);
      await Promise.resolve();
      const write = calls[0];
      assert(write && write.options.method === "DELETE", "delete write did not start");

      state.symbol = "000001.SZ";
      oldLoad.abort();
      assert(!write.options.signal.aborted, "stock switch aborted the in-flight delete write");

      const duplicateResult = await removeWatchlistItem(state, "600000.sh", oldOptions);
      assert(duplicateResult === false, "duplicate delete was not rejected while the first write was pending");
      assert(calls.filter((call) => call.options.method === "DELETE").length === 1, "duplicate delete sent a second write");

      let mainReadError = "";
      try {
        await loadWatchlist(state, {
          signal: newLoad.signal,
          isCurrent: () => state.symbol === "000001.SZ" && !newLoad.signal.aborted,
        });
      } catch (error) {
        mainReadError = error.message;
      }
      assert(mainReadError === "新股票主查询失败", `new stock read did not fail as arranged: ${mainReadError}`);
      assert(state.watchlist.some((item) => item.symbol === "600000.SH"), "pending delete changed global state before commit");

      deleteReply.resolve(jsonResponse(null));
      const result = await removal;
      const reads = calls.filter((call) => call.url === "/api/watchlist");

      assert(result === false, "stale delete completion enabled old stock UI effects");
      assert(reads.length === 2, `delete completion did not perform an independent readback: ${reads.length}`);
      assert(!reads[1].options.signal.aborted, "old stock signal cancelled the global delete readback");
      assert(contextualEffects === 0, "stale delete completion ran caller-context callbacks");
      assert(state.watchlist.length === 1 && state.watchlist[0].symbol === "000001.SZ", "committed delete left the old row in global state");
      assert(dom.element("watchList").innerHTML.includes("平安银行"), "global removal did not render the remaining row");
      assert(!dom.element("watchList").innerHTML.includes("浦发银行"), "failed reads restored the committed deletion");
      assert(dom.element("watchList").innerHTML.includes("已删除，列表同步降级"), "failed global readback was not marked as degraded");
    '''
    _run_node_script(script)


def test_duplicate_delete_is_released_after_failure_without_losing_the_row() -> None:
    script = r'''
      import { removeWatchlistItem, renderWatchlist } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = {
        watchlist: [
          watchItem("600000.SH", "浦发银行"),
          watchItem("000001.SZ", "平安银行"),
        ],
      };
      renderWatchlist(state.watchlist);
      const firstDeleteReply = deferredReply();
      const calls = [];
      let deleteCalls = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url.startsWith("/api/watchlist/") && options.method === "DELETE") {
          deleteCalls += 1;
          if (deleteCalls === 1) return firstDeleteReply.promise;
          return jsonResponse(null);
        }
        if (call.url === "/api/watchlist") return jsonResponse([watchItem("000001.SZ", "平安银行")]);
        throw new Error(`unexpected request: ${call.url}`);
      };

      const firstRemoval = removeWatchlistItem(state, "600000.SH");
      await Promise.resolve();
      const duplicateResult = await removeWatchlistItem(state, "600000.sh");
      assert(duplicateResult === false && deleteCalls === 1, "same-symbol delete was not deduplicated");

      firstDeleteReply.resolve({
        ok: false,
        status: 503,
        async json() { return { detail: "删除写入失败" }; },
      });
      let failure = "";
      try {
        await firstRemoval;
      } catch (error) {
        failure = error.message;
      }

      assert(failure === "删除写入失败", `delete failure was not propagated: ${failure}`);
      assert(calls.filter((call) => call.url === "/api/watchlist").length === 0, "failed delete started a readback");
      assert(state.watchlist.length === 2, "failed delete did not preserve the previous global state");
      assert(dom.element("watchList").innerHTML.includes("浦发银行"), "failed delete did not roll the row back");

      const retryResult = await removeWatchlistItem(state, "600000.sh");
      assert(retryResult === true && deleteCalls === 2, "failed delete kept the duplicate guard locked");
      assert(state.watchlist.length === 1 && state.watchlist[0].symbol === "000001.SZ", "successful retry did not remove the row");
    '''
    _run_node_script(script)


def test_watchlist_refresh_failure_keeps_cached_rows_with_non_destructive_warning() -> None:
    script = r'''
      import { loadWatchlist } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = { watchlist: [] };
      let failing = false;
      globalThis.fetch = async (url) => {
        if (String(url) !== "/api/watchlist") throw new Error(`unexpected URL ${url}`);
        if (failing) return { ok: false, status: 503, async json() { return { detail: "自选服务忙" }; } };
        return jsonResponse([watchItem("600519.SH", "贵州茅台")]);
      };

      await loadWatchlist(state);
      failing = true;
      let failure = "";
      try {
        await loadWatchlist(state, { force: true });
      } catch (error) {
        failure = error.message;
      }

      assert(failure === "自选服务忙", `refresh failure was not reported: ${failure}`);
      assert(state.watchlist.length === 1 && state.watchlist[0].symbol === "600519.SH", "cached rows were discarded");
      assert(dom.element("watchList").innerHTML.includes("贵州茅台"), "cached row was replaced by the failure state");
      assert(dom.element("watchList").innerHTML.includes("同步降级，显示上次结果"), "cached fallback lacked a scoped warning");
      assert(!dom.element("watchList").innerHTML.includes("<strong>自选股读取失败</strong>"), "cached fallback rendered a destructive error placeholder");
    '''
    _run_node_script(script)


def test_watchlist_queue_rendering_is_order_preserving_semantic_and_xss_safe() -> None:
    script = r'''
      import { renderWatchlist, watchlistSubscriptionKey } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const items = [
        {
          ...watchItem("600000.SH", "浦发银行"),
          group_name: "银行研究",
          note: '<img src=x onerror="globalThis.pwned=true">',
          pinned: true,
          research_status: "to_research",
          priority: "high",
          next_review_date: "2026-07-14",
          unread_change_count: 3,
        },
        {
          ...watchItem("600036.SH", "招商银行"),
          research_status: "excluded",
          priority: "low",
          next_review_date: "2026-07-15",
        },
      ];

      renderWatchlist(items, { today: "2026-07-15" });
      const html = dom.element("watchList").innerHTML;

      assert(html.indexOf("浦发银行") < html.indexOf("招商银行"), "frontend changed the backend queue order");
      assert(html.includes("待研究") && html.includes("高优先级"), "research status or priority was not rendered");
      assert(html.includes("逾期复核 · 2026-07-14") && html.includes("今日复核 · 2026-07-15"), "review due states were not rendered");
      assert(html.includes("3 条新变化") && html.includes("置顶"), "unread or pinned state was not rendered");
      assert(html.includes("分组 · 银行研究") && html.includes("关注原因 ·"), "group and reason were not rendered");
      assert(html.includes("is-excluded") && html.includes("已排除"), "excluded row was not explicitly weakened");
      assert(!html.includes("<img src=x") && html.includes("&lt;img"), "watchlist content was not HTML escaped");
      assert(watchlistSubscriptionKey(items) === "600000.SH", `excluded symbol leaked into subscription key: ${watchlistSubscriptionKey(items)}`);
    '''
    _run_node_script(script)


def test_watchlist_add_posts_research_queue_fields() -> None:
    script = r'''
      import { addWatchlistItem } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const state = { symbol: "600519.SH", watchlist: [] };
      let posted;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist" && options.method === "POST") {
          posted = JSON.parse(options.body);
          return jsonResponse({});
        }
        if (String(url) === "/api/watchlist") return jsonResponse([]);
        throw new Error(`unexpected request: ${url}`);
      };
      dom.element("watchSymbolInput").value = "600519";
      dom.element("watchStatusInput").value = "holding_research";
      dom.element("watchPriorityInput").value = "high";
      dom.element("watchReviewDateInput").value = "2026-07-20";
      dom.element("watchGroupInput").value = "核心仓";
      dom.element("watchNoteInput").value = "财报后复核";

      const result = await addWatchlistItem(state);

      assert(result === true, "queue addition did not complete");
      assert(posted.symbol === "600519", `wrong symbol payload: ${JSON.stringify(posted)}`);
      assert(posted.research_status === "holding_research" && posted.priority === "high", "research fields were not posted");
      assert(posted.next_review_date === "2026-07-20", "review date was not posted");
      assert(posted.group_name === "核心仓" && posted.note === "财报后复核", "group or note was not posted");
      assert(dom.element("watchStatusInput").value === "to_research" && dom.element("watchPriorityInput").value === "medium", "successful add did not restore queue defaults");
      assert(dom.element("watchReviewDateInput").value === "" && dom.element("watchNoteInput").value === "", "successful add did not clear transient fields");
    '''
    _run_node_script(script)


def test_watchlist_patch_sends_only_explicit_fields_and_supports_null_clear() -> None:
    script = r'''
      import { updateWatchlistItem } from "./static/js/watchlist.js";

      installWatchlistDom();
      const original = {
        ...watchItem("600000.SH", "浦发银行"),
        research_status: "watching",
        priority: "high",
        next_review_date: "2026-07-20",
        note: "等待财报",
      };
      const updated = { ...original, next_review_date: null, note: null };
      const state = { watchlist: [original] };
      const patchReply = deferredReply();
      let patchCalls = 0;
      let payload;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist/600000.SH" && options.method === "PATCH") {
          patchCalls += 1;
          payload = JSON.parse(options.body);
          return patchReply.promise;
        }
        if (String(url) === "/api/watchlist") return jsonResponse([updated]);
        throw new Error(`unexpected request: ${url}`);
      };

      const update = updateWatchlistItem(state, "600000.SH", { next_review_date: null, note: null, ignored: "x" });
      await Promise.resolve();
      const duplicate = await updateWatchlistItem(state, "600000.sh", { priority: "low" });
      assert(duplicate === false && patchCalls === 1, "same-symbol PATCH was not deduplicated");
      patchReply.resolve(jsonResponse(updated));
      const result = await update;

      assert(result === true, "watchlist PATCH did not complete");
      assert(JSON.stringify(payload) === JSON.stringify({ next_review_date: null, note: null }), `PATCH leaked implicit fields: ${JSON.stringify(payload)}`);
      assert(state.watchlist[0].next_review_date === null && state.watchlist[0].note === null, "explicit null clear was not applied locally");
      assert(state.watchlist[0].research_status === "watching" && state.watchlist[0].priority === "high", "PATCH changed omitted fields");
    '''
    _run_node_script(script)


def test_mark_viewed_clears_local_unread_even_when_safe_readback_fails() -> None:
    script = r'''
      import { markWatchlistItemViewed } from "./static/js/watchlist.js";

      const dom = installWatchlistDom();
      const original = { ...watchItem("000001.SZ", "平安银行"), unread_change_count: 4, last_viewed_at: null };
      const marked = { ...original, unread_change_count: 0, last_viewed_at: "2026-07-15 10:00:00" };
      const state = { watchlist: [original] };
      let body;
      let readbackError = "";
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/watchlist/000001.SZ/mark-viewed" && options.method === "POST") {
          body = JSON.parse(options.body);
          return jsonResponse(marked);
        }
        if (String(url) === "/api/watchlist") {
          return { ok: false, status: 503, async json() { return { detail: "列表回读繁忙" }; } };
        }
        throw new Error(`unexpected request: ${url}`);
      };

      const result = await markWatchlistItemViewed(state, "000001.SZ", {
        viewedThroughAdviceId: 37,
        onReadbackError(error) { readbackError = error.message; },
      });

      assert(result === true, "successful mark-viewed was reported as failed after readback degradation");
      assert(body.clear_unread === true, `mark-viewed body was wrong: ${JSON.stringify(body)}`);
      assert(body.viewed_through_advice_id === 37, `mark-viewed watermark was wrong: ${JSON.stringify(body)}`);
      assert(state.watchlist[0].unread_change_count === 0, "successful mark-viewed did not clear local unread state");
      assert(readbackError === "列表回读繁忙", `readback degradation was not reported: ${readbackError}`);
      assert(dom.element("watchList").innerHTML.includes("已标记已读，列表同步降级"), "mark-viewed readback warning was missing");
      assert(!dom.element("watchList").innerHTML.includes("标记失败"), "readback degradation was mislabeled as a write failure");
    '''
    _run_node_script(script)


def test_mark_viewed_requires_a_rendered_advice_watermark_before_writing() -> None:
    script = r'''
      import { markWatchlistItemViewed } from "./static/js/watchlist.js";

      installWatchlistDom();
      const state = { watchlist: [watchItem("000001.SZ", "平安银行")] };
      let calls = 0;
      globalThis.fetch = async () => {
        calls += 1;
        return jsonResponse({});
      };

      let message = "";
      try {
        await markWatchlistItemViewed(state, "000001.SZ");
      } catch (error) {
        message = error.message;
      }

      assert(message.includes("未读状态保持"), `missing watermark error was unclear: ${message}`);
      assert(calls === 0, `missing watermark still wrote to the server: ${calls}`);
    '''
    _run_node_script(script)


def _run_node_script(test_body: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", f"{test_body}\n{FAKE_DOM_SCRIPT}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


FAKE_DOM_SCRIPT = r'''
function installWatchlistDom() {
  const elements = new Map();
  const button = { disabled: false, textContent: "加入" };
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        attributes: {},
        value: "",
        innerHTML: "",
        querySelector() { return button; },
        setAttribute(name, value) { this.attributes[name] = String(value); },
      });
    }
    return elements.get(id);
  }
  element("watchSymbolInput").value = "000001";
  element("watchNoteInput").value = "并发关注";
  globalThis.document = { getElementById: element };
  return { element, button };
}

function watchItem(symbol, name) {
  return {
    symbol,
    code: symbol.slice(0, 6),
    name,
    latest_price: 10,
    latest_change_pct: 1,
    note: "测试关注",
  };
}

function deferredReply() {
  let resolve;
  const promise = new Promise((settle) => { resolve = settle; });
  return { promise, resolve };
}

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    async json() { return payload; },
  };
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
'''
