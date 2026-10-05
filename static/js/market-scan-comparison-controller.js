import { fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { requestMarketScanRead } from "./market-scan-read-client.js";
import { comparisonRunKey, comparisonSymbol, validateMarketScanComparison } from "./market-scan-comparison-contracts.js";
import { createMarketScanComparisonView } from "./market-scan-comparison-view.js";

export function createMarketScanComparisonController(options = {}) {
  const view = options.view || createMarketScanComparisonView(options.root || globalThis.document);
  if (!view) return { sync() {}, abort() {}, clear() {} };
  const context = { view, request: options.request || fetchJson, getRun: options.getRun, isActive: options.isActive || (() => true), disposed: false,
    state: { run: null, runKey: null, symbols: [], payload: null, busy: false, active: true, differencesOnly: false, message: "从榜单选择 2–4 只股票，比较同一批次的冻结数据。" }, sequence: 0, controller: null };
  const api = {
    sync: () => syncComparison(context), abort: () => invalidateComparison(context, "对比读取已暂停，请重新核验。"),
    clear: () => clearComparison(context), toggle: (symbol, runId) => toggleComparison(context, symbol, runId),
    compare: () => loadComparison(context), export: () => exportComparison(context),
    differences: (value) => { context.state.differencesOnly = Boolean(value); renderComparison(context); },
    remove: (symbol) => toggleComparison(context, symbol, context.state.run?.id),
    rowsChanged: () => { syncComparison(context); view.renderRows(context.state); },
    dispose: () => { context.disposed = true; invalidateComparison(context, "对比已关闭。"); disconnect?.(); },
  };
  const disconnect = view.bind(api);
  syncComparison(context);
  return api;
}

function renderComparison(context) {
  context.state.active = !context.disposed && context.isActive();
  context.view.render(context.state);
}

function syncComparison(context) {
  const run = context.getRun?.() || null;
  const key = comparisonRunKey(run);
  if (context.state.runKey !== key) {
    invalidateComparison(context, "冻结批次已切换，请重新选择候选。", false);
    context.state.symbols = [];
  }
  context.state.run = key ? { ...run } : null;
  context.state.runKey = key;
  renderComparison(context);
}

function invalidateComparison(context, message, render = true) {
  context.controller?.abort();
  context.controller = null;
  context.sequence += 1;
  context.state.busy = false;
  context.state.payload = null;
  context.state.message = message;
  if (render) renderComparison(context);
}

function clearComparison(context) {
  invalidateComparison(context, "已清空候选对比，可重新选择 2–4 只股票。", false);
  context.state.symbols = [];
  renderComparison(context);
}

function toggleComparison(context, symbol, runId) {
  syncComparison(context);
  const state = context.state;
  if (!state.active || !state.run || state.run.id !== runId || !comparisonSymbol(symbol)) return false;
  const selected = state.symbols.includes(symbol);
  if (!selected && state.symbols.length >= 4) return false;
  invalidateComparison(context, "候选已变更，请点击“开始对比”核验冻结数据。", false);
  state.symbols = selected ? state.symbols.filter((value) => value !== symbol) : [...state.symbols, symbol];
  renderComparison(context);
  return true;
}

async function loadComparison(context) {
  syncComparison(context);
  const state = context.state;
  if (!state.active || !state.run || state.symbols.length < 2 || state.busy) return null;
  invalidateComparison(context, "正在核验同批次冻结数据…", false);
  const owner = { sequence: context.sequence, key: state.runKey, run: { ...state.run }, symbols: [...state.symbols], controller: new AbortController() };
  context.controller = owner.controller;
  state.busy = true;
  renderComparison(context);
  try {
    const payload = await requestMarketScanRead(context.request, `/api/market-scans/${owner.run.id}/compare`, {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: owner.controller.signal,
      body: JSON.stringify({ symbols: owner.symbols, expected_snapshot_digest: owner.run.snapshot_digest }),
    });
    if (!ownsComparison(context, owner)) return null;
    state.payload = structuredClone(validateMarketScanComparison(payload, owner.run, owner.symbols));
    state.message = `已核验 ${state.symbols.length} 只候选的冻结数据。原始冻结排名可能与概率调整后的生产榜单不同。`;
    return state.payload;
  } catch (error) {
    if (ownsComparison(context, owner) && !isAbortError(error)) {
      state.payload = null;
      state.message = `对比读取失败：${compactErrorMessage(error?.message)}。请重新核验。`;
    }
    return null;
  } finally {
    if (ownsComparison(context, owner)) { context.controller = null; state.busy = false; renderComparison(context); }
  }
}

function ownsComparison(context, owner) {
  return !context.disposed && context.isActive() && context.sequence === owner.sequence && context.controller === owner.controller
    && !owner.controller.signal.aborted && comparisonRunKey(context.getRun?.()) === owner.key
    && JSON.stringify(context.state.symbols) === JSON.stringify(owner.symbols);
}

function exportComparison(context) {
  syncComparison(context);
  const state = context.state;
  if (!state.active || !state.payload || state.busy) return false;
  try {
    validateMarketScanComparison(state.payload, state.run, state.symbols);
    context.view.save(structuredClone(state.payload));
    return true;
  } catch (error) {
    invalidateComparison(context, `导出失败：${compactErrorMessage(error?.message)}`);
    return false;
  }
}
