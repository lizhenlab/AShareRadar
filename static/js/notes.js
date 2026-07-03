import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export async function loadNotes(state) {
  try {
    const notes = await fetchJson(`/api/stock/notes?symbol=${encodeURIComponent(state.symbol)}&limit=8`);
    renderNotes(notes);
  } catch (error) {
    $("noteList").innerHTML = `<div class="note-row"><strong>笔记读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

export async function addStockNote(state, refreshChartMarks) {
  const content = $("noteContent").value.trim();
  if (!content) throw new Error("请输入笔记内容");
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  await fetchJson("/api/stock/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: state.symbol,
      content,
      note_type: $("noteType").value,
      price: quote ? quote.price : undefined,
      trade_date: quote ? quote.timestamp : undefined,
    }),
  });
  $("noteContent").value = "";
  await loadNotes(state);
  await refreshChartMarks();
}

export async function removeStockNote(state, noteId, refreshChartMarks) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, { method: "DELETE" });
  await loadNotes(state);
  await refreshChartMarks();
}

export async function updateStockNote(state, noteId, payload, refreshChartMarks) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadNotes(state);
  await refreshChartMarks();
}

export function renderNotes(items) {
  $("noteList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="note-row ${item.visible ? "" : "is-muted"}">
            <div>
              <strong>${escapeHtml(item.note_type)} · ${formatNumber(item.price)}</strong>
              <span>${escapeHtml(item.content)}</span>
              <small>${escapeHtml(item.trade_date || item.created_at)}${item.visible ? "" : " · 已隐藏"}</small>
            </div>
            <div class="row-actions">
              <button type="button" class="mini-button" data-note-toggle="${escapeHtml(item.id)}" data-note-visible="${item.visible ? "false" : "true"}">${item.visible ? "隐藏" : "显示"}</button>
              <button type="button" class="icon-button" title="删除笔记" aria-label="删除笔记" data-note-remove="${escapeHtml(item.id)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="note-row"><strong>暂无笔记</strong><span>记录你的个股观察，会同步为图表标注。</span></div>`;
}
