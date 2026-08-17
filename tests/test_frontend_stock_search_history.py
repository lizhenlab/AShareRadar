from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stock_search_history_is_recent_deduplicated_bounded_and_persistent() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import {
        STOCK_SEARCH_HISTORY_STORAGE_KEY,
        loadStockSearchHistory,
        mergeStockSearchHistory,
        saveStockSearchHistory,
      } from "./static/js/stock-search-history.js";

      const storage = memoryStorage();
      let items = [];
      items = mergeStockSearchHistory(items, { symbol: "600519", name: "贵州茅台" }, 3);
      items = mergeStockSearchHistory(items, { symbol: "000001.SZ", name: "平安银行" }, 3);
      items = mergeStockSearchHistory(items, { symbol: "300750", name: "宁德时代" }, 3);
      items = mergeStockSearchHistory(items, { symbol: "600519.SH", name: "贵州茅台（更新）" }, 3);
      assert.deepEqual(items, [
        { symbol: "600519.SH", name: "贵州茅台（更新）" },
        { symbol: "300750.SZ", name: "宁德时代" },
        { symbol: "000001.SZ", name: "平安银行" },
      ]);

      assert.equal(saveStockSearchHistory(items, storage, 3), true);
      assert.deepEqual(loadStockSearchHistory(storage, 3), items);
      const payload = JSON.parse(storage.getItem(STOCK_SEARCH_HISTORY_STORAGE_KEY));
      payload.items.push({ symbol: "javascript:alert(1)", name: "非法记录" });
      payload.items.push({ symbol: "000001", name: "重复记录" });
      storage.setItem(STOCK_SEARCH_HISTORY_STORAGE_KEY, JSON.stringify(payload));
      assert.deepEqual(loadStockSearchHistory(storage, 3), items);
    '''
    _run_node_script(script)


def test_stock_search_history_ui_records_successes_and_selects_a_saved_stock() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchHistory } from "./static/js/stock-search-history.js";

      const elements = new Map();
      const rootListeners = {};
      const root = {
        getElementById(id) {
          if (!elements.has(id)) elements.set(id, fakeElement(id));
          return elements.get(id);
        },
        addEventListener(type, handler) { rootListeners[type] = handler; },
        removeEventListener(type) { delete rootListeners[type]; },
      };
      const selected = [];
      const storage = memoryStorage();
      let controller = createStockSearchHistory({
        root,
        storage,
        onSelect(symbol, item) { selected.push({ symbol, item }); },
      });

      assert.equal(root.getElementById("stockSearchHistory").hidden, false);
      assert.equal(controller.record({ symbol: "600519.SH", name: "<贵州茅台>" }), true);
      assert.equal(controller.record({ symbol: "bad", name: "无效" }), false);
      assert.equal(root.getElementById("stockSearchHistoryCount").textContent, "1");
      assert.doesNotMatch(root.getElementById("stockSearchHistoryList").innerHTML, /<贵州茅台>/);
      assert.match(root.getElementById("stockSearchHistoryList").innerHTML, /&lt;贵州茅台&gt;/);

      root.getElementById("stockSearchHistoryList").listeners.click({
        target: {
          closest() {
            return { dataset: { stockHistorySymbol: "600519.SH" } };
          },
        },
      });
      assert.deepEqual(selected, [{
        symbol: "600519.SH",
        item: { symbol: "600519.SH", name: "<贵州茅台>" },
      }]);
      assert.equal(root.getElementById("stockSearchHistory").hidden, false);
      assert.equal(controller.close(), false);
      assert.equal(root.getElementById("stockSearchHistory").hidden, false);

      controller.destroy();
      controller = createStockSearchHistory({ root, storage });
      assert.deepEqual(controller.items(), [{ symbol: "600519.SH", name: "<贵州茅台>" }]);
      assert.equal(root.getElementById("stockSearchHistoryCount").textContent, "1");
      assert.equal(root.getElementById("stockSearchHistory").hidden, false);

      controller.clear();
      assert.deepEqual(controller.items(), []);
      assert.equal(root.getElementById("stockSearchHistoryEmpty").hidden, false);
      assert.equal(root.getElementById("stockSearchHistoryClear").disabled, true);
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_history_markup_is_accessible_and_keeps_the_submit_button_unambiguous() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="stockSearchHistoryToggle"' not in html
    assert 'id="stockSearchHistoryDisclosure"' not in html
    assert html.index('id="stockSearchHistory"') < html.index('id="quickList"')
    history_opening = html.split('id="stockSearchHistory"', 1)[1].split(">", 1)[0]
    assert " hidden" not in history_opening
    assert "服务重启后仍会保留" in html
    assert 'id="stockSearchHistoryList"' in html
    assert 'id="stockSearchHistoryClear"' in html
    search_form = html.split('<form class="search-row" id="searchForm">', 1)[1].split("</form>", 1)[0]
    assert search_form.count("<button") == 1
    assert 'button type="submit"' in search_form


def _run_node_script(body: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", f"{body}\n{NODE_HELPERS}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


NODE_HELPERS = r'''
function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
  };
}

function fakeElement(id) {
  return {
    id,
    hidden: false,
    disabled: false,
    innerHTML: "",
    textContent: "",
    attributes: {},
    listeners: {},
    dataset: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
    removeEventListener(type) { delete this.listeners[type]; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    focus() {},
    contains() { return false; },
  };
}
'''
