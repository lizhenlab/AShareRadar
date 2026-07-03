import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { changeClass, formatNumber } from "./format.js";

export async function loadWatchlist(state) {
  try {
    const items = await fetchJson("/api/watchlist");
    state.watchlist = items;
    renderWatchlist(items);
  } catch (error) {
    state.watchlist = [];
    $("watchList").innerHTML = `<div class="watch-row"><strong>自选股读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

export function renderWatchlist(items) {
  $("watchList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="watch-row" data-symbol="${escapeHtml(item.symbol)}">
            <button type="button" class="watch-main" data-action="open" data-symbol="${escapeHtml(item.symbol)}">
              <strong>${escapeHtml(item.name)} <span>${escapeHtml(item.code)}</span></strong>
              <small>${escapeHtml(item.note || item.group_name || "默认关注")}</small>
            </button>
            <div class="watch-side">
              <strong>${formatNumber(item.latest_price)}</strong>
              <span class="${changeClass(item.latest_change_pct)}">${formatNumber(item.latest_change_pct)}%</span>
              <button type="button" class="icon-button" title="移出自选" aria-label="移出自选" data-action="remove" data-symbol="${escapeHtml(item.symbol)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="watch-row"><strong>暂无自选</strong><span>输入代码后加入关注。</span></div>`;
}

export async function addWatchlistItem(state) {
  const symbol = $("watchSymbolInput").value.trim() || state.symbol;
  const note = $("watchNoteInput").value.trim();
  const button = $("watchForm").querySelector("button");
  try {
    button.disabled = true;
    button.textContent = "加入中";
    await fetchJson("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, note }),
    });
    $("watchNoteInput").value = "";
    await loadWatchlist(state);
  } finally {
    button.disabled = false;
    button.textContent = "加入";
  }
}

export async function removeWatchlistItem(state, symbol) {
  await fetchJson(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
  await loadWatchlist(state);
}
