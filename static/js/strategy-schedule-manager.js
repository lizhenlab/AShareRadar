import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { validateManagedSchedule, validateScheduleConfirmation, validateSchedulePage } from "./strategy-schedule-contracts.js";
import {
  announceScheduleStatus, renderManagedSchedules, renderScheduleContext,
  strategyScheduleElements, syncScheduleControls,
} from "./strategy-schedule-view.js";

const API = "/api/strategy-lab/schedules";
const PAGE_SIZE = 20;

export function createStrategyScheduleManager(options = {}) {
  const elements = strategyScheduleElements(options.root || globalThis.document);
  const request = options.fetcher || fetchJson;
  const state = { strategy: null, page: null, reading: false, writing: false, externalBusy: false, creationPending: false, orderStale: false, sequence: 0, abort: null };
  let contextKey = "";

  function setStrategy(strategy) {
    const key = strategy ? `${strategy.strategy_id}:${strategy.strategy_version}:${strategy.archived}:${strategy.fingerprint}` : "";
    if (key === contextKey) return;
    cancelRead();
    contextKey = key;
    state.strategy = strategy ? structuredClone(strategy) : null;
    state.page = null;
    state.orderStale = false;
    renderScheduleContext(elements, state.strategy);
    renderManagedSchedules(elements, state);
    announceScheduleStatus(elements, strategy ? "展开后读取；不会自动执行策略。" : "请先载入或保存策略。", "idle");
    if (strategy && elements.manager.open) void refresh();
  }

  async function refresh(pageNumber = state.page?.page || 1, confirmedWriteKey = null) {
    if (!state.strategy || !elements.manager.open || state.creationPending || (state.writing && confirmedWriteKey !== contextKey)) return null;
    cancelRead();
    const owner = { key: contextKey, sequence: state.sequence, strategyId: state.strategy.strategy_id, abort: new AbortController() };
    state.abort = owner.abort;
    state.reading = true;
    syncScheduleControls(elements, state);
    announceScheduleStatus(elements, "正在读取已保存任务…", "busy");
    try {
      const query = new URLSearchParams({ strategy_id: owner.strategyId, include_disabled: true, page: pageNumber, page_size: PAGE_SIZE });
      const payload = await request(`${API}?${query}`, { signal: owner.abort.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      if (!ownsRead(owner)) return null;
      state.page = validateSchedulePage(payload, owner.strategyId, pageNumber, PAGE_SIZE);
      state.orderStale = false;
      renderManagedSchedules(elements, state);
      announceScheduleStatus(elements, `已读取 ${state.page.total} 个任务；任务固定使用创建时的策略版本。`);
      return state.page;
    } catch (error) {
      if (ownsRead(owner) && !isAbortError(error)) {
        announceScheduleStatus(elements, `任务读取失败：${compactErrorMessage(error.message)}${state.page ? "；显示上次成功读取的任务，请刷新确认。" : "；请重试。"}`, "error");
      }
      return null;
    } finally {
      if (ownsRead(owner)) {
        state.reading = false;
        state.abort = null;
        syncScheduleControls(elements, state);
      }
    }
  }

  async function toggleSchedule(event) {
    if (state.reading || state.writing || state.externalBusy) return null;
    const id = Number(event.target.closest?.("[data-strategy-schedule-toggle]")?.dataset.strategyScheduleToggle);
    const original = state.page?.items.find(item => item.schedule_id === id);
    if (!original || (state.strategy.archived && !original.enabled)) return null;
    const key = contextKey;
    const enabled = !original.enabled;
    state.writing = true;
    options.onWritingChange?.(true);
    syncScheduleControls(elements, state);
    announceScheduleStatus(elements, `正在${enabled ? "恢复" : "停用"}任务 #${id}…`, "busy");
    try {
      const payload = await request(`${API}/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }), timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      const confirmed = validateScheduleConfirmation(payload, original, enabled);
      await synchronizeConfirmedSchedule(confirmed, key);
      return confirmed;
    } catch (error) {
      if (key === contextKey) {
        state.orderStale = true;
        announceScheduleStatus(elements, `任务 #${id} 启停结果未确认：${compactErrorMessage(error.message)}；请先刷新任务核对状态，再决定是否重试。`, "error");
      }
      return null;
    } finally {
      state.writing = false;
      options.onWritingChange?.(false);
      syncScheduleControls(elements, state);
      if (key !== contextKey && state.strategy && elements.manager.open) await refresh();
    }
  }

  async function synchronizeConfirmedSchedule(confirmed, key) {
    if (key !== contextKey) return;
    state.page = { ...state.page, items: state.page.items.map(item => item.schedule_id === confirmed.schedule_id ? confirmed : item) };
    state.orderStale = true;
    renderManagedSchedules(elements, state);
    const message = `任务 #${confirmed.schedule_id} 已${confirmed.enabled ? "恢复" : "停用"}；固定版本 v${confirmed.strategy_version} 保持不变。`;
    announceScheduleStatus(elements, message);
    const refreshed = elements.manager.open ? await refresh(state.page.page, key) : state.page;
    if (key === contextKey) announceScheduleStatus(elements, refreshed ? message : `${message}列表同步未完成，请刷新任务，无需重复提交。`, refreshed ? "ready" : "warn");
  }

  async function confirmCreation(schedule) {
    if (!state.strategy || schedule.strategy_id !== state.strategy.strategy_id) return;
    validateManagedSchedule(schedule, state.strategy.strategy_id);
    state.creationPending = false;
    state.orderStale = true;
    syncScheduleControls(elements, state);
    const key = contextKey;
    if (!elements.manager.open) return;
    const page = await refresh(1);
    if (!page && key === contextKey) announceScheduleStatus(elements, `任务 #${schedule.schedule_id} 已创建；任务列表同步未完成，请刷新任务，无需重复创建。`, "warn");
  }

  function cancelRead() {
    state.sequence += 1;
    state.abort?.abort();
    state.abort = null;
    state.reading = false;
  }

  function ownsRead(owner) {
    return owner.sequence === state.sequence && owner.key === contextKey && !owner.abort.signal.aborted;
  }

  function setBusy(busy) {
    state.externalBusy = busy;
    if (!busy) state.creationPending = false;
    syncScheduleControls(elements, state);
  }

  function beginCreation() {
    cancelRead();
    state.creationPending = true;
    state.orderStale = true;
    syncScheduleControls(elements, state);
    if (state.strategy) announceScheduleStatus(elements, "已发起创建任务；请在提交结束后刷新列表确认，再继续翻页。", "warn");
  }

  elements.manager.addEventListener("toggle", () => {
    if (elements.manager.open) return refresh();
    if (state.reading) {
      cancelRead();
      announceScheduleStatus(elements, "读取已取消；重新展开可继续查看任务。", "idle");
      syncScheduleControls(elements, state);
    }
    return null;
  });
  elements.refresh.addEventListener("click", () => refresh());
  elements.prev.addEventListener("click", () => !state.reading && !state.writing && !state.externalBusy && !state.orderStale && state.page?.page > 1 ? refresh(state.page.page - 1) : null);
  elements.next.addEventListener("click", () => !state.reading && !state.writing && !state.externalBusy && !state.orderStale && state.page?.page < state.page?.page_count ? refresh(state.page.page + 1) : null);
  elements.rows.addEventListener("click", toggleSchedule);
  syncScheduleControls(elements, state);
  return { state, setStrategy, setBusy, refresh, confirmCreation, beginCreation };
}
