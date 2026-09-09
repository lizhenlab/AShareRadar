import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { escapeHtml } from "./dom.js";
import { compactErrorMessage } from "./errors.js";
import {
  buildDiscoveryPresetDefinition,
  isDiscoveryPresetUiRepresentable,
  normalizeDiscoveryLeaderboard,
  rankChangeLabel,
  validateDiscoveryPreset,
  validateDiscoveryPresetPage,
  validateDiscoveryRankChanges,
  validateDiscoveryResearchQueueResponse,
} from "./market-scan-contracts.js";
import { applyDiscoveryPresetFields, marketScanFilterElements } from "./market-scan-filters.js";
import { marketScanPageSize } from "./layout-optimizations.js";
import { marketScanResultRow } from "./market-scan-view.js";
import { validateMarketScanScreenAlert } from "./market-scan-screening-contracts.js";
import {
  discoveryDefinitionKey, discoveryEditorKey, discoveryPresetPageText,
  discoveryPresetSelectionText, unknownDiscoveryWriteResult, validateDiscoveryDeleteReceipt, validateDiscoveryWriteReceipt,
} from "./discovery-preset-state.js";
export {
  buildDiscoveryPresetDefinition,
  isDiscoveryPresetUiRepresentable,
  normalizeDiscoveryLeaderboard,
  rankChangeLabel,
} from "./market-scan-contracts.js";

const RANK_CHANGE_PAGE_SIZE = 200;
const COMPLETED_RUN_STATUSES = new Set(["success", "degraded"]);
export function createDiscoveryController(options = {}) {
  const root = options.root || globalThis.document;
  const controls = root?.getElementById?.("discoveryPresetControls");
  if (!controls) return inertDiscoveryController();
  const request = options.fetcher || fetchJson;
  const getRun = typeof options.getRun === "function" ? options.getRun : () => null;
  const loadStandardResults = typeof options.loadStandardResults === "function"
    ? options.loadStandardResults
    : () => null;
  const elements = discoveryElements(root);
  const state = {
    activated: false,
    busy: false,
    presets: [],
    selectedId: null,
    selected: null,
    selectionEpoch: 0,
    page: 1,
    pageCount: 0,
    total: 0,
    orderStale: false,
    writeUnconfirmed: false,
    applied: null,
    sequence: 0,
    appliedRequest: null,
    appliedSequence: 0,
  };

  bindEvents();
  renderPresetOptions();

  async function activate() {
    if (state.activated) return state.presets;
    state.activated = true;
    return loadPresets();
  }

  async function loadPresets(page = state.page) {
    if (!Number.isInteger(page) || page < 1) return null;
    if (page !== state.page && (state.orderStale || page > state.pageCount)) return null;
    return runOperation("正在读取筛选方案...", async () => {
      await readPresetPage(page);
      setFeedback(state.total ? discoveryPresetPageText(state) : "暂无已保存筛选方案");
      return state.presets;
    }, "筛选方案读取");
  }

  async function readPresetPage(page, clamp = true) {
    const payload = validateDiscoveryPresetPage(await request(
      `/api/discovery/presets?page=${page}&page_size=100`, requestOptions()
    ));
    if (payload.page !== page || payload.page_size !== 100) throw new Error("方案分页响应与请求页不一致");
    if (page > Math.max(1, payload.page_count)) {
      if (!clamp) throw new Error("方案页数再次变化，请刷新列表");
      return readPresetPage(Math.max(1, payload.page_count), false);
    }
    state.presets = payload.items;
    state.page = page;
    state.pageCount = payload.page_count;
    state.total = payload.total;
    state.orderStale = false;
    renderPresetOptions();
  }

  function savePreset() {
    if (selectedPreset() && !isDiscoveryPresetUiRepresentable(selectedPreset())) {
      setFeedback("兼容方案不能通过当前表单完整复制；请先取消方案选择后自建条件，或导出保留原定义。", "warn");
      return null;
    }
    return writePreset(null);
  }

  function updatePreset() {
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    if (!isDiscoveryPresetUiRepresentable(preset)) {
      setFeedback("兼容方案无法在当前表单完整表达，不能覆盖原定义；可导出原方案。", "error");
      return null;
    }
    return writePreset(preset);
  }

  async function writePreset(existing) {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    let definition;
    try {
      definition = buildDiscoveryPresetDefinition(elements.name.value, elements);
    } catch (error) {
      setFeedback(error.message, "error");
      elements.name.focus?.();
      return null;
    }
    return runOperation("正在保存筛选方案...", async () => {
      const owner = { epoch: state.selectionEpoch, key: discoveryDefinitionKey(definition) };
      const preset = await submitPresetWrite(definition, existing);
      const hadAppliedPreset = Boolean(state.applied);
      const changed = discoveryEditorKey(elements) !== owner.key;
      if (state.selectionEpoch === owner.epoch) {
        selectSavedPreset(preset);
        clearApplied();
        if (!changed) elements.name.value = preset.name;
      }
      const message = `${existing ? "已更新" : "已保存新"}筛选方案“${preset.name}”${changed ? "；保存期间的新输入已保留，尚未保存" : ""}`;
      await synchronizePresetWrite(message);
      if (hadAppliedPreset && state.selectionEpoch === owner.epoch) void loadStandardResults();
      return preset;
    }, "筛选方案保存");
  }

  function submitPresetWrite(definition, existing) {
    return requestPresetReceipt(
      existing ? `/api/discovery/presets/${encodeURIComponent(existing.id)}` : "/api/discovery/presets",
      requestOptions({
        method: existing ? "PUT" : "POST",
        body: JSON.stringify(existing ? { ...definition, expected_revision: existing.revision } : definition),
      }), definition, existing
    );
  }

  function requestPresetReceipt(url, requestConfig, definition, existing) {
    const knownIds = [...state.presets.map(item => item.id), state.selectedId];
    return requestConfirmedPresetMutation(url, requestConfig, payload => (
      validateDiscoveryWriteReceipt(payload, definition, existing, knownIds)
    ));
  }

  async function requestConfirmedPresetMutation(url, requestConfig, validate) {
    try {
      const payload = await requestOrderedPresetMutation(url, requestConfig);
      return validate(payload);
    } catch (error) {
      if (unknownDiscoveryWriteResult(error)) {
        state.writeUnconfirmed = true;
        throw new Error(`保存结果待核对：${compactErrorMessage(error?.message)}。请刷新方案列表并重新选择具体方案核对。`);
      }
      throw error;
    }
  }

  async function requestOrderedPresetMutation(url, requestConfig) {
    const wasStale = state.orderStale;
    state.orderStale = true;
    try {
      return await request(url, requestConfig);
    } catch (error) {
      if (!unknownDiscoveryWriteResult(error)) state.orderStale = wasStale;
      throw error;
    }
  }

  async function synchronizePresetWrite(message) {
    try {
      await readPresetPage(state.page);
      setFeedback(message, "success");
    } catch (error) {
      state.orderStale = true;
      renderPresetOptions();
      setFeedback(`${message}；列表同步未完成：${compactErrorMessage(error?.message)}，请刷新后翻页。`, "warn");
    }
  }

  function selectSavedPreset(preset) {
    state.selected = preset;
    state.selectedId = preset?.id || null;
    options.onPresetChange?.(preset);
    renderPresetOptions();
  }

  function requireWriteConfirmation() {
    setFeedback("保存结果待核对，请刷新方案列表并重新选择具体方案核对后再保存。", "warn");
    return null;
  }

  async function renamePreset() {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    const name = normalizedName(elements.name.value);
    if (!name) {
      setFeedback("请输入新的方案名称", "error");
      elements.name.focus?.();
      return null;
    }
    return runOperation("正在重命名筛选方案...", async () => {
      const editorKey = discoveryEditorKey(elements);
      const renamed = await requestPresetReceipt(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}`,
        requestOptions({
          method: "PATCH",
          body: JSON.stringify({ name, expected_revision: preset.revision }),
        }), { ...preset, name }, preset
      );
      selectSavedPreset(renamed);
      if (state.applied?.preset.id === renamed.id) state.applied.preset = renamed;
      if (discoveryEditorKey(elements) === editorKey) elements.name.value = renamed.name;
      await synchronizePresetWrite(`已重命名为“${renamed.name}”`);
      return renamed;
    }, "筛选方案重命名");
  }

  async function exportPreset() {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    return runOperation("正在导出筛选方案...", async () => {
      const archive = await request(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}/export`,
        requestOptions()
      );
      savePresetArchive(root, archive, preset.name);
      setFeedback(`已导出筛选方案“${preset.name}”`, "success");
      return archive;
    }, "筛选方案导出");
  }

  async function recordScreenAlert() {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    const run = leaderboardRun();
    if (!run) {
      setFeedback("请先选择一个已发布全市场榜单", "error");
      return null;
    }
    return runOperation("正在记录筛选变化提醒...", async () => {
      const payload = validateMarketScanScreenAlert(await request(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}/screen-alerts`,
        requestOptions({
          method: "POST",
          body: JSON.stringify({
            current_run_id: run.id,
            expected_preset_revision: preset.revision,
          }),
        })
      ), preset.id, run.id);
      if (payload.preset.preset_revision !== preset.revision) throw new Error("筛选变化回执的方案修订与请求不一致");
      setFeedback(screenAlertFeedback(payload), payload.status === "ready" ? "success" : "warn");
      options.onScreenAlertRecorded?.(payload);
      return payload;
    }, "筛选变化提醒");
  }

  async function importPreset(file = elements.importFile.files?.[0]) {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    if (!file) {
      setFeedback("请选择筛选方案 JSON 文件", "error");
      return null;
    }
    return runOperation("正在导入筛选方案...", async () => {
      const archive = JSON.parse(await file.text());
      const preset = await requestPresetReceipt(
        "/api/discovery/presets/import",
        requestOptions({ method: "POST", body: JSON.stringify(archive) }), archive.preset, null
      );
      selectSavedPreset(preset);
      elements.name.value = preset.name;
      if (isDiscoveryPresetUiRepresentable(preset)) applyDiscoveryPresetFields(preset, elements);
      await synchronizePresetWrite(`已导入筛选方案“${preset.name}”`);
      return preset;
    }, "筛选方案导入");
  }

  async function deletePreset() {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    const confirmDelete = root.defaultView?.confirm || globalThis.confirm;
    if (typeof confirmDelete === "function" && !confirmDelete(`删除筛选方案“${preset.name}”？`)) return null;
    return runOperation("正在删除筛选方案...", async () => {
      await requestConfirmedPresetMutation(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}?expected_revision=${encodeURIComponent(preset.revision)}`,
        requestOptions({ method: "DELETE" }), payload => validateDiscoveryDeleteReceipt(payload, preset.id)
      );
      state.presets = state.presets.filter((item) => item.id !== preset.id);
      selectSavedPreset(null);
      const restoreResults = state.applied?.preset.id === preset.id;
      clearApplied();
      elements.name.value = "";
      renderPresetOptions();
      await synchronizePresetWrite(`已删除筛选方案“${preset.name}”`);
      if (restoreResults) void loadStandardResults();
      return true;
    }, "筛选方案删除");
  }

  async function applyPreset(page = 1) {
    if (state.writeUnconfirmed) return requireWriteConfirmation();
    if (state.busy) return null;
    const context = preparePresetApplication();
    if (!context) return null;
    const { editable, preset, run } = context;
    const stage = editable ? "正在应用筛选方案..." : "正在按原定义应用筛选方案...";
    return runOperation(
      stage,
      () => executePresetApplication({ editable, page, preset, run }),
      "筛选方案应用"
    );
  }

  function preparePresetApplication() {
    const preset = selectedPreset();
    if (!preset) {
      requirePreset();
      return null;
    }
    const run = leaderboardRun();
    if (!run) {
      setFeedback("请先等待全市场扫描完成，再应用筛选方案", "error");
      return null;
    }
    const editable = isDiscoveryPresetUiRepresentable(preset);
    if (editable) applyDiscoveryPresetFields(preset, elements);
    return { editable, preset, run };
  }

  async function executePresetApplication({ editable, page, preset, run }) {
    const identity = beginAppliedRequest(preset, run);
    try {
      const [pageResult, rankingResult] = await requestPresetApplication(identity, preset, run, page);
      if (pageResult.status === "rejected") throw pageResult.reason;
      const payload = normalizeDiscoveryLeaderboard(pageResult.value);
      const rankOutcome = resolveRankChanges(rankingResult, run.id);
      if (!acceptPresetApplication(identity, payload)) return null;
      commitPresetApplication(payload, rankOutcome.payload);
      renderDiscoveryResults(payload, rankOutcome.payload);
      reportPresetApplication(payload, rankOutcome, editable);
      return payload;
    } finally {
      finishAppliedRequest(identity);
    }
  }

  function requestPresetApplication(identity, preset, run, page) {
    const options = requestOptions({ signal: identity.controller.signal });
    const operation = request(`/api/discovery/presets/${encodeURIComponent(preset.id)}/apply`, {
      ...options,
      method: "POST",
      body: JSON.stringify({ run_id: run.id, page, page_size: marketScanPageSize(elements) }),
    });
    const ranking = request(
      `/api/discovery/runs/${encodeURIComponent(run.id)}/rank-changes?page=1&page_size=${RANK_CHANGE_PAGE_SIZE}`,
      options
    );
    return Promise.allSettled([operation, ranking]);
  }

  function resolveRankChanges(result, runId) {
    if (result.status === "rejected") return { payload: null, error: result.reason };
    try {
      return { payload: validateDiscoveryRankChanges(result.value, runId), error: null };
    } catch (error) {
      return { payload: null, error };
    }
  }

  function acceptPresetApplication(identity, payload) {
    if (isAppliedRequestCurrent(identity, payload)) return true;
    if (identity.sequence === state.appliedSequence) {
      setFeedback("榜单批次或筛选方案已变化，旧请求结果已忽略", "warn");
    }
    return false;
  }

  function commitPresetApplication(payload, rankPayload) {
    const previous = state.applied;
    state.applied = {
      page: payload.page,
      pageCount: payload.page_count,
      preset: payload.preset,
      rankPayload,
      runId: payload.run_id,
      payload,
      queued: previous?.runId === payload.run_id && previous?.preset.id === payload.preset.id
        ? previous.queued
        : new Set(),
      selected: previous?.runId === payload.run_id && previous?.preset.id === payload.preset.id
        ? previous.selected
        : new Set(),
    };
    selectSavedPreset(payload.preset);
  }

  function reportPresetApplication(payload, rankOutcome, editable) {
    if (rankOutcome.error) {
      setFeedback(`方案已应用；全市场排名变化暂不可用：${compactErrorMessage(rankOutcome.error?.message)}`, "warn");
    } else if (editable) {
      setFeedback(`已应用筛选方案“${payload.preset.name}”`, "success");
    } else {
      setFeedback(`已按原定义应用筛选方案“${payload.preset.name}”`, "success");
    }
  }

  async function enqueueSymbol(symbol, button) {
    const applied = state.applied;
    if (!applied || !symbol || state.busy) return null;
    button.disabled = true;
    button.setAttribute?.("aria-busy", "true");
    setFeedback(`正在将 ${symbol} 加入研究队列...`);
    try {
      const rawPayload = await request(
        `/api/discovery/presets/${encodeURIComponent(applied.preset.id)}/research-queue`,
        requestOptions({
          method: "POST",
          body: JSON.stringify({
            run_id: applied.runId,
            expected_preset_revision: applied.preset.revision,
            symbols: [symbol],
          }),
        })
      );
      requireCurrentApplied(applied);
      const payload = validateDiscoveryResearchQueueResponse(rawPayload, {
        runId: applied.runId,
        presetId: applied.preset.id,
        presetRevision: applied.preset.revision,
        symbols: [symbol],
      });
      applied.queued.add(symbol);
      applied.selected.delete(symbol);
      button.textContent = "已在研究队列";
      renderBulkControls(applied.payload);
      setFeedback(
        payload?.added_count ? `${symbol} 已加入研究队列` : `${symbol} 已在研究队列中`,
        "success"
      );
      return payload;
    } catch (error) {
      if (!isAbortError(error)) setFeedback(`加入研究队列失败：${compactErrorMessage(error?.message)}`, "error");
      button.disabled = false;
      return null;
    } finally {
      button.setAttribute?.("aria-busy", "false");
    }
  }

  async function enqueueSelected() {
    const symbols = [...(state.applied?.selected || [])];
    if (!symbols.length) {
      setFeedback("请先选择要加入研究队列的股票", "error");
      return null;
    }
    return enqueueMany(symbols, "已选股票");
  }

  async function enqueueAllFiltered() {
    const applied = state.applied;
    if (!applied) return null;
    return runOperation("正在读取当前筛选结果...", async () => {
      const symbols = await allFilteredSymbols(applied);
      return enqueueSymbolBatches(applied, symbols, "当前筛选结果");
    }, "批量加入研究队列");
  }

  async function enqueueMany(symbols, label) {
    const applied = state.applied;
    if (!applied || state.busy) return null;
    return runOperation(`正在批量加入${label}...`, () => (
      enqueueSymbolBatches(applied, symbols, label)
    ), "批量加入研究队列");
  }

  async function enqueueSymbolBatches(applied, symbols, label) {
    let added = 0;
    let existing = 0;
    const unique = [...new Set(symbols)].filter(Boolean);
    for (let index = 0; index < unique.length; index += 100) {
      requireCurrentApplied(applied);
      const chunk = unique.slice(index, index + 100);
      const rawPayload = await request(
        `/api/discovery/presets/${encodeURIComponent(applied.preset.id)}/research-queue`,
        requestOptions({
          method: "POST",
          body: JSON.stringify({
            run_id: applied.runId,
            expected_preset_revision: applied.preset.revision,
            symbols: chunk,
          }),
        })
      );
      requireCurrentApplied(applied);
      const payload = validateDiscoveryResearchQueueResponse(rawPayload, {
        runId: applied.runId,
        presetId: applied.preset.id,
        presetRevision: applied.preset.revision,
        symbols: chunk,
      });
      chunk.forEach((symbol) => applied.queued.add(symbol));
      added += Number(payload?.added_count) || 0;
      existing += Number(payload?.existing_count) || 0;
      setFeedback(`正在批量加入${label}：${Math.min(index + chunk.length, unique.length)}/${unique.length}`);
    }
    unique.forEach((symbol) => applied.selected.delete(symbol));
    renderDiscoveryResults(applied.payload, applied.rankPayload);
    setFeedback(`${label}处理完成：新增 ${added}，已存在 ${existing}`, "success");
    return { added_count: added, existing_count: existing, total: unique.length };
  }

  async function allFilteredSymbols(applied) {
    const symbols = [];
    const totalPages = Math.max(1, applied.pageCount);
    for (let page = 1; page <= totalPages; page += 1) {
      requireCurrentApplied(applied);
      const payload = normalizeDiscoveryLeaderboard(await request(
        `/api/discovery/presets/${encodeURIComponent(applied.preset.id)}/apply`,
        requestOptions({
          method: "POST",
          body: JSON.stringify({ run_id: applied.runId, page, page_size: marketScanPageSize(elements) }),
        })
      ));
      if (
        payload.run_id !== applied.runId
        || payload.preset.id !== applied.preset.id
        || payload.preset.revision !== applied.preset.revision
      ) {
        throw new Error("筛选方案或榜单批次已变化，请重新应用方案");
      }
      symbols.push(...payload.items.map((item) => item.symbol));
      setFeedback(`正在读取当前筛选结果：${Math.min(symbols.length, payload.total)}/${payload.total}`);
    }
    return symbols;
  }

  function requireCurrentApplied(applied) {
    if (state.applied !== applied || leaderboardRun()?.id !== applied.runId) {
      throw new Error("筛选方案或榜单批次已变化，请重新应用方案");
    }
  }

  async function runOperation(stage, operation, failureLabel) {
    if (state.busy) return null;
    const sequence = ++state.sequence;
    setBusy(true);
    setFeedback(stage);
    try {
      return await operation();
    } catch (error) {
      if (!isAbortError(error) && sequence === state.sequence) {
        setFeedback(state.writeUnconfirmed ? compactErrorMessage(error?.message)
          : `${failureLabel}失败：${compactErrorMessage(error?.message)}`, state.writeUnconfirmed ? "warn" : "error");
      }
      return null;
    } finally {
      if (sequence === state.sequence) setBusy(false);
    }
  }

  function renderDiscoveryResults(payload, rankPayload) {
    const rankBySymbol = new Map((rankPayload?.items || []).map((item) => [item.symbol, item]));
    setDisplayedRunId(payload.run_id);
    elements.tableWrap.setAttribute("aria-busy", "false");
    elements.pagination.setAttribute("aria-busy", "false");
    if (!payload.items.length) {
      elements.rows.innerHTML = "";
      elements.tableWrap.hidden = true;
      elements.resultState.hidden = false;
      elements.resultState.className = "market-scan-result-state";
      elements.resultState.textContent = "当前筛选方案没有匹配结果";
    } else {
      elements.rows.innerHTML = payload.items.map((item) => {
        const change = rankBySymbol.get(item.symbol);
        return marketScanResultRow(item, {
          discovery: true,
          queued: state.applied.queued.has(item.symbol),
          selected: state.applied.selected.has(item.symbol),
          run: leaderboardRun(),
          rankLabel: change ? rankChangeLabel(change) : unavailableRankChangeLabel(item, rankPayload),
          rankMovement: change?.movement || "unavailable",
        });
      }).join("");
      elements.tableWrap.hidden = false;
      elements.resultState.hidden = true;
    }
    elements.pagination.hidden = payload.total === 0;
    elements.pageText.textContent = `第 ${payload.page}/${Math.max(payload.page_count, 1)} 页 · 共 ${payload.total} 条`;
    elements.prev.disabled = payload.page <= 1;
    elements.next.disabled = payload.page_count === 0 || payload.page >= payload.page_count;
    renderBulkControls(payload);
    renderRankSummary(rankPayload);
  }

  function renderBulkControls(payload) {
    const applied = state.applied;
    elements.bulkControls.hidden = !applied;
    const visible = payload.items.map((item) => item.symbol).filter((symbol) => !applied.queued.has(symbol));
    const selectedVisible = visible.filter((symbol) => applied.selected.has(symbol));
    elements.selectPage.checked = visible.length > 0 && selectedVisible.length === visible.length;
    elements.selectPage.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visible.length;
    elements.selectedCount.textContent = `已选 ${applied.selected.size} 项`;
    elements.enqueueSelected.disabled = state.busy || applied.selected.size === 0;
    elements.enqueueAll.disabled = state.busy || payload.total === 0;
  }

  function renderRankSummary(payload) {
    elements.rankSummary.hidden = false;
    if (!payload) {
      elements.rankSummary.textContent = "全市场排名变化暂不可用（非方案内名次）";
    } else if (payload.comparable) {
      elements.rankSummary.textContent = `全市场排名变化（非方案内名次）· 相邻同规则批次 ${payload.previous_run_id} → ${payload.current_run_id} · 规则 ${payload.current_rule_version} · ${payload.total} 项变化`;
    } else if (payload.reason === "rule_version_mismatch") {
      elements.rankSummary.textContent = `全市场相邻批次规则不同（${payload.previous_rule_version || "--"} / ${payload.current_rule_version}），不比较排名`;
    } else {
      elements.rankSummary.textContent = "暂无可比较的上一批次同规则全市场榜单";
    }
  }

  function renderPresetOptions() {
    const selected = String(state.selectedId || "");
    const pageItems = state.presets.map(preset => preset.id === state.selectedId ? state.selected : preset);
    const pinned = state.selected && !pageItems.some(preset => preset.id === state.selectedId) ? [state.selected] : [];
    elements.select.innerHTML = `<option value="">${state.writeUnconfirmed ? "请选择具体方案核对保存结果" : "选择已保存方案"}</option>${[...pinned, ...pageItems].map((preset) => (
      `<option value="${preset.id}">${escapeHtml(preset.name)}${isDiscoveryPresetUiRepresentable(preset) ? "" : "（兼容模式）"}</option>`
    )).join("")}`;
    elements.select.value = state.writeUnconfirmed ? "" : selected;
    elements.presetPageInfo.textContent = discoveryPresetPageText(state);
    elements.selectionInfo.textContent = discoveryPresetSelectionText(state);
    renderControls();
  }

  function renderControls() {
    const preset = selectedPreset();
    const selected = Boolean(preset);
    const blocked = state.busy || state.writeUnconfirmed;
    elements.select.disabled = state.busy;
    elements.name.disabled = state.busy;
    elements.apply.disabled = blocked || !selected;
    elements.screenAlert.disabled = blocked || !selected;
    elements.exportButton.disabled = blocked || !selected;
    renderPresetWriteControls(preset);
    renderPresetPagination();
  }

  function renderPresetWriteControls(preset) {
    const blocked = state.busy || state.writeUnconfirmed;
    const incompatible = Boolean(preset && !isDiscoveryPresetUiRepresentable(preset));
    elements.save.disabled = blocked || incompatible;
    elements.update.disabled = blocked || !preset || incompatible;
    elements.rename.disabled = blocked || !preset;
    elements.importButton.disabled = blocked;
    elements.remove.disabled = blocked || !preset;
  }

  function renderPresetPagination() {
    elements.presetPrev.disabled = state.busy || state.orderStale || state.page <= 1;
    elements.presetNext.disabled = state.busy || state.orderStale || state.page >= state.pageCount;
    elements.presetRefresh.disabled = state.busy;
    elements.selectionInfo.textContent = discoveryPresetSelectionText(state);
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    controls.setAttribute("aria-busy", state.busy ? "true" : "false");
    renderControls();
    if (state.applied) renderBulkControls(state.applied.payload);
  }

  function setFeedback(message, kind) {
    elements.feedback.className = kind || "";
    elements.feedback.textContent = String(message || "");
    if (message) elements.announcement.textContent = String(message);
  }

  function selectedPreset() {
    return state.selected;
  }

  function presetById(id) {
    return state.presets.find((preset) => preset.id === Number(id)) || null;
  }

  function requirePreset() {
    setFeedback("请先选择一个筛选方案", "error");
    elements.select.focus?.();
    return null;
  }

  function clearApplied() {
    const hadPendingRequest = Boolean(state.appliedRequest);
    invalidateAppliedRequest();
    if (hadPendingRequest) {
      state.sequence += 1;
      if (state.busy) setBusy(false);
    }
    state.applied = null;
    elements.bulkControls.hidden = true;
    elements.rankSummary.hidden = true;
    elements.rankSummary.textContent = "";
  }

  function handlePresetSelection() {
    const id = elements.select.value ? Number(elements.select.value) : null;
    if (state.writeUnconfirmed && id) return void confirmPresetSelection(id);
    state.selectionEpoch += 1;
    const previousApplied = Boolean(state.applied);
    const preset = presetById(id) || (state.selected?.id === id ? state.selected : null);
    selectSavedPreset(preset);
    elements.name.value = preset?.name || "";
    if (preset && isDiscoveryPresetUiRepresentable(preset)) applyDiscoveryPresetFields(preset, elements);
    clearApplied();
    renderControls();
    setFeedback(preset
      ? isDiscoveryPresetUiRepresentable(preset)
        ? `已选择筛选方案“${preset.name}”`
        : `已选择兼容方案“${preset.name}”：将按保存时的原定义应用`
      : "");
    if (previousApplied) void loadStandardResults();
  }

  async function confirmPresetSelection(id) {
    return runOperation("正在核对已保存筛选方案...", async () => {
      const preset = validateDiscoveryPreset(await request(`/api/discovery/presets/${encodeURIComponent(id)}`, requestOptions()));
      if (preset.id !== id) throw new Error("方案核对响应身份不一致");
      state.writeUnconfirmed = false;
      state.selectionEpoch += 1;
      const hadApplied = Boolean(state.applied);
      clearApplied();
      selectSavedPreset(preset);
      elements.name.value = preset.name;
      if (isDiscoveryPresetUiRepresentable(preset)) applyDiscoveryPresetFields(preset, elements);
      setFeedback(`已核对并选择方案“${preset.name}”修订 ${preset.revision}`, "success");
      if (hadApplied) void loadStandardResults();
    }, "筛选方案核对");
  }

  function handlePresetPagination(event, direction) {
    if (!state.applied) return;
    if (leaderboardRun()?.id !== state.applied.runId) {
      clearApplied();
      return;
    }
    event.preventDefault?.();
    event.stopImmediatePropagation?.();
    const page = state.applied.page + direction;
    if (page < 1 || page > state.applied.pageCount) return;
    void applyPreset(page);
  }

  function bindEvents() {
    elements.select.addEventListener("change", handlePresetSelection);
    elements.save.addEventListener("click", () => void savePreset());
    elements.update.addEventListener("click", () => void updatePreset());
    elements.presetPrev.addEventListener("click", () => void loadPresets(state.page - 1));
    elements.presetNext.addEventListener("click", () => void loadPresets(state.page + 1));
    elements.presetRefresh.addEventListener("click", () => void loadPresets());
    elements.apply.addEventListener("click", () => void applyPreset(1));
    elements.rename.addEventListener("click", () => void renamePreset());
    elements.screenAlert.addEventListener("click", () => void recordScreenAlert());
    elements.exportButton.addEventListener("click", () => void exportPreset());
    elements.importButton.addEventListener("click", () => elements.importFile.click?.());
    elements.importFile.addEventListener("change", () => void importPreset());
    elements.remove.addEventListener("click", () => void deletePreset());
    elements.prev.addEventListener("click", (event) => handlePresetPagination(event, -1), true);
    elements.next.addEventListener("click", (event) => handlePresetPagination(event, 1), true);
    elements.filters.addEventListener("submit", clearApplied, true);
    elements.filters.addEventListener("reset", clearApplied, true);
    elements.start.addEventListener("click", clearApplied, true);
    elements.retry.addEventListener("click", clearApplied, true);
    elements.rows.addEventListener("click", (event) => {
      const button = event.target.closest?.("button[data-discovery-enqueue-symbol]");
      if (button) void enqueueSymbol(button.dataset.discoveryEnqueueSymbol, button);
    });
    elements.rows.addEventListener("change", (event) => {
      const checkbox = event.target.closest?.("input[data-discovery-select-symbol]");
      const applied = state.applied;
      if (!checkbox || !applied) return;
      if (checkbox.checked) applied.selected.add(checkbox.dataset.discoverySelectSymbol);
      else applied.selected.delete(checkbox.dataset.discoverySelectSymbol);
      renderBulkControls(applied.payload);
    });
    elements.selectPage.addEventListener("change", () => {
      const applied = state.applied;
      if (!applied) return;
      applied.payload.items.forEach((item) => {
        if (applied.queued.has(item.symbol)) return;
        if (elements.selectPage.checked) applied.selected.add(item.symbol);
        else applied.selected.delete(item.symbol);
      });
      renderDiscoveryResults(applied.payload, applied.rankPayload);
    });
    elements.enqueueSelected.addEventListener("click", () => void enqueueSelected());
    elements.enqueueAll.addEventListener("click", () => void enqueueAllFiltered());
  }

  function leaderboardRun() {
    const latest = getRun();
    const displayedId = displayedRunId();
    if (displayedId && (!latest || !COMPLETED_RUN_STATUSES.has(latest.status) || latest.id === displayedId)) {
      return latest?.id === displayedId ? latest : { id: displayedId, status: "success" };
    }
    return latest && COMPLETED_RUN_STATUSES.has(latest.status) ? latest : null;
  }

  function beginAppliedRequest(preset, run) {
    invalidateAppliedRequest();
    const controller = new AbortController();
    const identity = {
      controller,
      presetId: preset.id,
      presetRevision: preset.revision,
      runId: run.id,
      sequence: state.appliedSequence,
    };
    state.appliedRequest = controller;
    return identity;
  }

  function isAppliedRequestCurrent(identity, payload) {
    const currentPreset = selectedPreset();
    return state.appliedRequest === identity.controller
      && state.appliedSequence === identity.sequence
      && currentPreset?.id === identity.presetId
      && currentPreset?.revision === identity.presetRevision
      && leaderboardRun()?.id === identity.runId
      && payload.run_id === identity.runId
      && payload.preset.id === identity.presetId
      && payload.preset.revision === identity.presetRevision;
  }

  function finishAppliedRequest(identity) {
    if (state.appliedRequest === identity.controller) state.appliedRequest = null;
  }

  function invalidateAppliedRequest() {
    state.appliedRequest?.abort();
    state.appliedRequest = null;
    state.appliedSequence += 1;
  }

  function displayedRunId() {
    const value = elements.tableWrap.dataset?.marketScanRunId
      || elements.tableWrap["data-market-scan-run-id"];
    const runId = Number(value);
    return Number.isInteger(runId) && runId > 0 ? runId : null;
  }

  function setDisplayedRunId(runId) {
    if (elements.tableWrap.dataset) elements.tableWrap.dataset.marketScanRunId = String(runId);
    elements.tableWrap["data-market-scan-run-id"] = String(runId);
  }

  return {
    activate,
    applyPreset,
    deletePreset,
    exportPreset,
    importPreset,
    enqueueAllFiltered,
    enqueueSelected,
    loadPresets,
    renamePreset,
    recordScreenAlert,
    savePreset,
    updatePreset,
    selectedPreset,
    state,
  };
}

function unavailableRankChangeLabel(item, payload) {
  if (!payload?.comparable) return "全市场排名变化不可用";
  const sourceRank = Number(item?.source_rank);
  if (Number.isInteger(sourceRank) && sourceRank > RANK_CHANGE_PAGE_SIZE) {
    return `全市场排名变化未查询（当前第 ${sourceRank} 名）`;
  }
  return "全市场排名变化未返回";
}

function discoveryElements(root) {
  const byId = (id) => {
    const element = root.getElementById(id);
    if (!element) throw new Error(`缺少 Discovery 界面元素：${id}`);
    return element;
  };
  return {
    controls: byId("discoveryPresetControls"),
    select: byId("discoveryPresetSelect"),
    name: byId("discoveryPresetName"),
    save: byId("discoveryPresetSave"),
    update: byId("discoveryPresetUpdate"),
    presetPrev: byId("discoveryPresetPrev"),
    presetNext: byId("discoveryPresetNext"),
    presetRefresh: byId("discoveryPresetRefresh"),
    presetPageInfo: byId("discoveryPresetPageInfo"),
    selectionInfo: byId("discoveryPresetSelectionInfo"),
    apply: byId("discoveryPresetApply"),
    rename: byId("discoveryPresetRename"),
    screenAlert: byId("discoveryPresetScreenAlert"),
    exportButton: byId("discoveryPresetExport"),
    importButton: byId("discoveryPresetImport"),
    importFile: byId("discoveryPresetImportFile"),
    remove: byId("discoveryPresetDelete"),
    feedback: byId("discoveryPresetFeedback"),
    announcement: byId("marketScanAnnouncement"),
    rankSummary: byId("discoveryRankSummary"),
    bulkControls: byId("discoveryBulkControls"),
    selectPage: byId("discoverySelectPage"),
    selectedCount: byId("discoverySelectedCount"),
    enqueueSelected: byId("discoveryEnqueueSelected"),
    enqueueAll: byId("discoveryEnqueueAll"),
    ...marketScanFilterElements(root, (_root, id) => byId(id)),
    start: byId("marketScanStart"),
    retry: byId("marketScanRetry"),
    resultState: byId("marketScanResultState"),
    tableWrap: byId("marketScanTableWrap"),
    rows: byId("marketScanRows"),
    pagination: byId("marketScanPagination"),
    pageText: byId("marketScanPageText"),
    prev: byId("marketScanPrev"),
    next: byId("marketScanNext"),
  };
}

function requestOptions(overrides = {}) {
  return {
    ...overrides,
    headers: { "Content-Type": "application/json", ...(overrides.headers || {}) },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  };
}

function normalizedName(value) {
  return String(value || "").trim().slice(0, 80);
}

function screenAlertFeedback(payload) {
  if (payload.status === "unavailable") {
    return ({
      current_not_published: "当前批次尚未发布，未记录变化提醒",
      current_not_full_market: "当前批次不是完整全市场榜单，未记录变化提醒",
      previous_same_cohort_not_found: "暂无同模式、同股票池、同规则前批次，未记录变化提醒",
    })[payload.unavailable_reason] || "当前没有可记录的同 cohort 变化提醒";
  }
  const counts = `新进入 ${payload.entered_symbols.length}、退出 ${payload.exited_symbols.length}`;
  const suppressed = payload.suppressed_unrankable_symbols.length
    ? `；另有 ${payload.suppressed_unrankable_symbols.length} 只当前不可排名，未误报退出`
    : "";
  return `${payload.created ? "已记录" : "该变化已记录"}：${counts}${suppressed}`;
}

function savePresetArchive(root, archive, name) {
  const createUrl = root?.defaultView?.URL?.createObjectURL || globalThis.URL?.createObjectURL;
  const revokeUrl = root?.defaultView?.URL?.revokeObjectURL || globalThis.URL?.revokeObjectURL;
  if (typeof root?.createElement !== "function" || typeof createUrl !== "function") return;
  const blob = new Blob([JSON.stringify(archive, null, 2)], { type: "application/json;charset=utf-8" });
  const url = createUrl(blob);
  const anchor = root.createElement("a");
  anchor.href = url;
  anchor.download = `${String(name || "discovery-preset").replace(/[\\/:*?"<>|]+/g, "-")}.json`;
  anchor.click();
  if (typeof revokeUrl === "function") setTimeout(() => revokeUrl(url), 0);
}

function inertDiscoveryController() {
  const noOp = async () => null;
  return {
    activate: noOp,
    applyPreset: noOp,
    deletePreset: noOp,
    exportPreset: noOp,
    importPreset: noOp,
    enqueueAllFiltered: noOp,
    enqueueSelected: noOp,
    loadPresets: noOp,
    renamePreset: noOp,
    recordScreenAlert: noOp,
    savePreset: noOp,
    updatePreset: noOp,
    selectedPreset: () => null,
    state: { activated: false, applied: null, presets: [] },
  };
}
