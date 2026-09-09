import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  SCREEN_ALERT_DETAIL_SIZE, SCREEN_ALERT_HISTORY_SIZE, acknowledgedDetailPage,
  screenAlertAcknowledgement, validateScreenAlertDetail, validateScreenAlertHistory,
} from "./discovery-screen-alerts-contracts.js";
import {
  discoveryScreenAlertElements, renderScreenAlertDetail, renderScreenAlertHistory,
  renderScreenAlertSelection, screenAlertRetainedDetail, screenAlertStatus, syncScreenAlertControls,
} from "./discovery-screen-alerts-view.js";

export function createDiscoveryScreenAlertsController(options = {}) {
  const root = options.root || globalThis.document;
  const context = { root, elements: discoveryScreenAlertElements(root), request: options.fetcher || fetchJson,
    bindings: [], state: { preset: null, history: null, detail: null, ack: null, detailTarget: null,
      historyTarget: 1, historyRequest: null, detailRequest: null, generation: 0,
      surfaceActive: Boolean(options.surfaceActive), disposed: false } };
  bindEvents(context);
  renderScreenAlertSelection(context.elements, context.state);
  return {
    state: context.state,
    selectionChanged: preset => selectionChanged(context, preset),
    recorded: payload => recorded(context, payload),
    open: () => openHistory(context),
    setSurfaceActive: active => setSurfaceActive(context, active),
    dispose: () => dispose(context),
  };
}

function selectionChanged(context, preset) {
  const { state, elements } = context;
  if (state.disposed) return;
  const selected = preset ? { id: preset.id, revision: preset.revision, name: String(preset.name || "筛选方案") } : null;
  if (selected && (!Number.isSafeInteger(selected.id) || selected.id < 1
    || !Number.isSafeInteger(selected.revision) || selected.revision < 1)) throw new TypeError("筛选方案身份无效");
  if (selected?.id === state.preset?.id && selected?.revision === state.preset?.revision) {
    state.preset = selected;
    renderScreenAlertSelection(elements, state);
    return;
  }
  cancelRead(context, "history"); cancelRead(context, "detail");
  Object.assign(state, { preset: selected, history: null, detail: null, ack: null, detailTarget: null,
    historyTarget: 1, generation: state.generation + 1 });
  elements.History.open = false;
  renderScreenAlertSelection(elements, state);
  screenAlertStatus(elements, "History", selected ? "展开后读取已记录变化；不会重新扫描或生成记录。" : "请先选择筛选方案。", "idle");
  screenAlertStatus(elements, "Detail", "", "idle");
}

function recorded(context, payload) {
  const { state, elements } = context;
  if (state.disposed || !state.preset || payload?.preset?.preset_id !== state.preset.id
    || payload?.preset?.preset_revision !== state.preset.revision) return false;
  try {
    const ack = screenAlertAcknowledgement(payload, state.preset);
    if (!ack) return false;
    cancelRead(context, "history"); cancelRead(context, "detail");
    state.ack = ack;
    state.detailTarget = null;
    state.historyTarget = 1;
    state.detail = acknowledgedDetailPage(ack);
    renderScreenAlertDetail(elements, state);
    screenAlertStatus(elements, "Detail", "本次变化记录已确认；以下明细来自保存回执，只读查看。", "ready");
    screenAlertStatus(elements, "History", "本次已确认记录已显示；展开或刷新历史可查看已保存记录。", "idle");
    return true;
  } catch (error) {
    screenAlertStatus(elements, "History", `记录回执无法展示：${compactErrorMessage(error.message)}；请读取历史核对，勿重复记录。`, "error");
    return false;
  }
}

function openHistory(context) {
  if (!canRead(context)) return Promise.resolve(null);
  context.elements.History.open = true;
  if (context.state.historyRequest) return Promise.resolve(null);
  return loadHistory(context, context.state.historyTarget);
}

async function loadHistory(context, page) {
  const { state, elements } = context;
  if (!canRead(context) || !elements.History.open) return null;
  const owner = beginRead(context, "history");
  const presetId = state.preset.id;
  state.historyTarget = page;
  screenAlertStatus(elements, "History", `正在读取第 ${page} 页记录…`, "busy");
  try {
    const query = new URLSearchParams({ page, page_size: SCREEN_ALERT_HISTORY_SIZE });
    const value = await context.request(`${eventUrl(presetId)}?${query}`, readOptions(owner));
    if (!ownsRead(context, "history", owner)) return null;
    state.history = validateScreenAlertHistory(value, presetId, page);
    renderScreenAlertHistory(elements, state);
    screenAlertStatus(elements, "History", `已读取方案 #${presetId} 的第 ${page} 页，共 ${state.history.total} 条记录。`);
    return state.history;
  } catch (error) {
    if (ownsRead(context, "history", owner) && !isAbortError(error)) {
      const retained = state.history ? `保留此前第 ${state.history.page} 页记录` : "尚未读取历史记录";
      screenAlertStatus(elements, "History", `第 ${page} 页记录读取失败：${compactErrorMessage(error.message)}；${retained}，可重试。`, "error");
    }
    return null;
  } finally {
    finishRead(context, "history", owner);
  }
}

async function loadDetail(context, target) {
  const { state, elements } = context;
  if (!canRead(context) || target.event.preset_id !== state.preset.id) return null;
  const owner = beginRead(context, "detail");
  state.detailTarget = target;
  elements.Detail.hidden = false;
  screenAlertStatus(elements, "Detail", `正在读取记录 #${target.event.id} 第 ${target.page} 页明细…`, "busy");
  try {
    const query = new URLSearchParams({ page: target.page, page_size: SCREEN_ALERT_DETAIL_SIZE, kind: target.kind });
    const value = await context.request(`${eventUrl(state.preset.id)}/${target.event.id}?${query}`, readOptions(owner));
    if (!ownsRead(context, "detail", owner)) return null;
    state.detail = validateScreenAlertDetail(value, target.event, target.page, target.kind);
    state.detailTarget = null;
    renderScreenAlertDetail(elements, state);
    renderScreenAlertHistory(elements, state);
    screenAlertStatus(elements, "Detail", `已读取历史记录 #${target.event.id}；按记录时的方案修订和前后批次显示。`);
    return state.detail;
  } catch (error) {
    if (ownsRead(context, "detail", owner) && !isAbortError(error)) {
      screenAlertStatus(elements, "Detail", `记录 #${target.event.id} 第 ${target.page} 页明细读取失败：${compactErrorMessage(error.message)}；${screenAlertRetainedDetail(state)}。`, "error");
    }
    return null;
  } finally {
    finishRead(context, "detail", owner);
  }
}

function selectHistoryEvent(context, event) {
  const id = Number(event.target.closest?.("[data-screen-alert-event]")?.dataset.screenAlertEvent);
  const summary = context.state.history?.items.find(item => item.id === id);
  return summary ? loadDetail(context, { event: summary, page: 1, kind: "all" }) : null;
}

function changeDetailPage(context, page, kind) {
  const { state, elements } = context;
  if (!state.detail || !canRead(context) || state.detailRequest) return null;
  if (state.detail.source === "history") return loadDetail(context, { event: state.detail.event, page, kind });
  state.detailTarget = null;
  state.detail = acknowledgedDetailPage(state.ack, page, kind);
  renderScreenAlertDetail(elements, state);
  screenAlertStatus(elements, "Detail", "本次变化记录已确认；明细分页在本地完成。", "ready");
  return state.detail;
}

function refreshDetail(context) {
  const { state } = context;
  const target = state.detailTarget || (state.detail?.source === "history"
    ? { event: state.detail.event, page: state.detail.page, kind: state.detail.kind } : null);
  return target ? loadDetail(context, target) : null;
}

function bindEvents(context) {
  const { elements, state, root } = context;
  const bind = (target, event, action) => { target.addEventListener(event, action); context.bindings.push([target, event, action]); };
  bind(elements.History, "toggle", () => historyToggled(context));
  bind(elements.HistoryRefresh, "click", () => loadHistory(context, state.historyTarget));
  bind(elements.HistoryPrev, "click", () => historyStep(context, -1));
  bind(elements.HistoryNext, "click", () => historyStep(context, 1));
  bind(elements.HistoryRows, "click", event => selectHistoryEvent(context, event));
  bind(elements.DetailPrev, "click", () => detailStep(context, -1));
  bind(elements.DetailNext, "click", () => detailStep(context, 1));
  bind(elements.DetailRefresh, "click", () => refreshDetail(context));
  bind(elements.Kind, "change", () => changeDetailPage(context, 1, elements.Kind.value));
  bind(root, "visibilitychange", () => { if (root.hidden) suspendReads(context); });
}

function historyToggled(context) {
  if (context.elements.History.open) return context.state.historyRequest ? null : openHistory(context);
  if (context.state.historyRequest) {
    cancelRead(context, "history");
    screenAlertStatus(context.elements, "History", "历史读取已取消；已加载记录保持不变。", "idle");
    syncScreenAlertControls(context.elements, context.state);
  }
  return null;
}

function historyStep(context, direction) {
  const { state } = context;
  const page = state.history?.page + direction;
  if (state.historyRequest || !state.history || page < 1 || page > state.history.page_count) return null;
  return loadHistory(context, page);
}

function detailStep(context, direction) {
  const value = context.state.detail;
  const page = value?.page + direction;
  if (!value || page < 1 || page > value.page_count) return null;
  return changeDetailPage(context, page, value.kind);
}

function setSurfaceActive(context, active) {
  context.state.surfaceActive = Boolean(active);
  if (!active) suspendReads(context);
  syncScreenAlertControls(context.elements, context.state);
}

function suspendReads(context) {
  for (const section of ["history", "detail"]) {
    if (!context.state[`${section}Request`]) continue;
    cancelRead(context, section);
    const retained = section === "detail" ? screenAlertRetainedDetail(context.state) : "已加载记录保持不变";
    screenAlertStatus(context.elements, section === "detail" ? "Detail" : "History", `读取已取消；${retained}，返回后可刷新。`, "idle");
  }
  syncScreenAlertControls(context.elements, context.state);
}

function beginRead(context, section) {
  cancelRead(context, section);
  const owner = { generation: context.state.generation, abort: new AbortController() };
  context.state[`${section}Request`] = owner;
  syncScreenAlertControls(context.elements, context.state);
  return owner;
}

function cancelRead(context, section) {
  context.state[`${section}Request`]?.abort.abort();
  context.state[`${section}Request`] = null;
  if (section === "detail" && context.state.detail) context.elements.Kind.value = context.state.detail.kind;
}

function ownsRead(context, section, owner) {
  return context.state[`${section}Request`] === owner && owner.generation === context.state.generation
    && !owner.abort.signal.aborted && canRead(context) && (section !== "history" || context.elements.History.open);
}

function finishRead(context, section, owner) {
  if (context.state[`${section}Request`] !== owner) return;
  context.state[`${section}Request`] = null;
  syncScreenAlertControls(context.elements, context.state);
}

function canRead(context) {
  return !context.state.disposed && context.state.surfaceActive && !context.root.hidden
    && Boolean(context.state.preset) && Boolean(context.elements.shell.getClientRects().length);
}

function dispose(context) {
  context.state.disposed = true;
  cancelRead(context, "history"); cancelRead(context, "detail");
  context.bindings.forEach(([target, event, handler]) => target.removeEventListener(event, handler));
  context.bindings = [];
}

function eventUrl(presetId) {
  return `/api/discovery/presets/${presetId}/screen-alerts`;
}

function readOptions(owner) {
  return { signal: owner.abort.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS };
}
