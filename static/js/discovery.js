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
} from "./market-scan-contracts.js";
import { marketScanResultRow } from "./market-scan-view.js";
export {
  buildDiscoveryPresetDefinition,
  isDiscoveryPresetUiRepresentable,
  normalizeDiscoveryLeaderboard,
  rankChangeLabel,
} from "./market-scan-contracts.js";

const DISCOVERY_PAGE_SIZE = 100;
const RANK_CHANGE_PAGE_SIZE = 200;
const COMPLETED_RUN_STATUSES = new Set(["success", "degraded"]);
const SORT_TO_MARKET = Object.freeze({
  rank: "rank",
  symbol: "symbol",
  score: "score",
  trend: "trend_score",
  change: "change_pct",
  turnover: "turnover_rate",
  amount: "amount",
  quality: "data_quality_score",
});

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

  async function loadPresets() {
    return runOperation("正在读取筛选方案...", async () => {
      const payload = validateDiscoveryPresetPage(await request(
        "/api/discovery/presets?page=1&page_size=100",
        requestOptions()
      ));
      state.presets = payload.items;
      if (!presetById(state.selectedId)) state.selectedId = null;
      renderPresetOptions();
      setFeedback(payload.total ? `已读取 ${payload.total} 个筛选方案` : "暂无已保存筛选方案");
      return state.presets;
    }, "筛选方案读取");
  }

  async function savePreset() {
    if (selectedPreset() && !isDiscoveryPresetUiRepresentable(selectedPreset())) {
      setFeedback("复杂方案按后端定义只读，不能用当前界面覆盖保存", "error");
      return null;
    }
    let definition;
    try {
      definition = buildDiscoveryPresetDefinition(elements.name.value, elements);
    } catch (error) {
      setFeedback(error.message, "error");
      elements.name.focus?.();
      return null;
    }
    return runOperation("正在保存筛选方案...", async () => {
      const preset = validateDiscoveryPreset(await request("/api/discovery/presets", requestOptions({
        method: "POST",
        body: JSON.stringify(definition),
      })));
      const hadAppliedPreset = Boolean(state.applied);
      upsertPreset(preset);
      state.selectedId = preset.id;
      clearApplied();
      renderPresetOptions();
      elements.name.value = preset.name;
      setFeedback(`已保存筛选方案“${preset.name}”`, "success");
      if (hadAppliedPreset) void loadStandardResults();
      return preset;
    }, "筛选方案保存");
  }

  async function renamePreset() {
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    const name = normalizedName(elements.name.value);
    if (!name) {
      setFeedback("请输入新的方案名称", "error");
      elements.name.focus?.();
      return null;
    }
    return runOperation("正在重命名筛选方案...", async () => {
      const renamed = validateDiscoveryPreset(await request(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}`,
        requestOptions({
          method: "PATCH",
          body: JSON.stringify({ name, expected_revision: preset.revision }),
        })
      ));
      upsertPreset(renamed);
      state.selectedId = renamed.id;
      if (state.applied?.preset.id === renamed.id) state.applied.preset = renamed;
      renderPresetOptions();
      elements.name.value = renamed.name;
      setFeedback(`已重命名为“${renamed.name}”`, "success");
      return renamed;
    }, "筛选方案重命名");
  }

  async function deletePreset() {
    const preset = selectedPreset();
    if (!preset) return requirePreset();
    const confirmDelete = root.defaultView?.confirm || globalThis.confirm;
    if (typeof confirmDelete === "function" && !confirmDelete(`删除筛选方案“${preset.name}”？`)) return null;
    return runOperation("正在删除筛选方案...", async () => {
      await request(
        `/api/discovery/presets/${encodeURIComponent(preset.id)}?expected_revision=${encodeURIComponent(preset.revision)}`,
        requestOptions({ method: "DELETE" })
      );
      state.presets = state.presets.filter((item) => item.id !== preset.id);
      state.selectedId = null;
      const restoreResults = state.applied?.preset.id === preset.id;
      clearApplied();
      elements.name.value = "";
      renderPresetOptions();
      setFeedback(`已删除筛选方案“${preset.name}”`, "success");
      if (restoreResults) void loadStandardResults();
      return true;
    }, "筛选方案删除");
  }

  async function applyPreset(page = 1) {
    if (state.busy) return null;
    const context = preparePresetApplication();
    if (!context) return null;
    const { editable, preset, run } = context;
    const stage = editable ? "正在应用筛选方案..." : "正在按后端原定义只读应用复杂筛选方案...";
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
    if (editable) applyPresetFields(preset, elements);
    return { editable, preset, run };
  }

  async function executePresetApplication({ editable, page, preset, run }) {
    const identity = beginAppliedRequest(preset, run);
    try {
      const [pageResult, rankingResult] = await requestPresetApplication(identity, preset, run, page);
      if (pageResult.status === "rejected") throw pageResult.reason;
      const payload = normalizeDiscoveryLeaderboard(pageResult.value);
      const rankPayload = resolveRankChanges(rankingResult, run.id);
      if (!acceptPresetApplication(identity, payload)) return null;
      commitPresetApplication(payload, rankPayload);
      renderDiscoveryResults(payload, rankPayload);
      reportPresetApplication(payload, rankingResult, editable);
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
      body: JSON.stringify({ run_id: run.id, page, page_size: DISCOVERY_PAGE_SIZE }),
    });
    const ranking = request(
      `/api/discovery/runs/${encodeURIComponent(run.id)}/rank-changes?page=1&page_size=${RANK_CHANGE_PAGE_SIZE}`,
      options
    );
    return Promise.allSettled([operation, ranking]);
  }

  function resolveRankChanges(result, runId) {
    return result.status === "fulfilled" ? validateDiscoveryRankChanges(result.value, runId) : null;
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
      queued: previous?.runId === payload.run_id && previous?.preset.id === payload.preset.id
        ? previous.queued
        : new Set(),
    };
    upsertPreset(payload.preset);
    state.selectedId = payload.preset.id;
    renderPresetOptions();
  }

  function reportPresetApplication(payload, rankingResult, editable) {
    if (rankingResult.status === "rejected") {
      setFeedback(`方案已应用；全市场排名变化暂不可用：${compactErrorMessage(rankingResult.reason?.message)}`, "warn");
    } else if (editable) {
      setFeedback(`已应用筛选方案“${payload.preset.name}”`, "success");
    } else {
      setFeedback(`已按后端原定义只读应用复杂方案“${payload.preset.name}”`, "success");
    }
  }

  async function enqueueSymbol(symbol, button) {
    const applied = state.applied;
    if (!applied || !symbol || state.busy) return null;
    button.disabled = true;
    button.setAttribute?.("aria-busy", "true");
    setFeedback(`正在将 ${symbol} 加入研究队列...`);
    try {
      const payload = await request(
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
      applied.queued.add(symbol);
      button.textContent = "已在研究队列";
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

  async function runOperation(stage, operation, failureLabel) {
    if (state.busy) return null;
    const sequence = ++state.sequence;
    setBusy(true);
    setFeedback(stage);
    try {
      return await operation();
    } catch (error) {
      if (!isAbortError(error) && sequence === state.sequence) {
        setFeedback(`${failureLabel}失败：${compactErrorMessage(error?.message)}`, "error");
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
    renderRankSummary(rankPayload);
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
    elements.select.innerHTML = `<option value="">选择已保存方案</option>${state.presets.map((preset) => (
      `<option value="${preset.id}">${escapeHtml(preset.name)}${isDiscoveryPresetUiRepresentable(preset) ? "" : "（只读）"}</option>`
    )).join("")}`;
    elements.select.value = state.presets.some((preset) => String(preset.id) === selected) ? selected : "";
    renderControls();
  }

  function renderControls() {
    const preset = selectedPreset();
    const selected = Boolean(preset);
    elements.select.disabled = state.busy;
    elements.name.disabled = state.busy;
    elements.save.disabled = state.busy || Boolean(preset && !isDiscoveryPresetUiRepresentable(preset));
    elements.apply.disabled = state.busy || !selected;
    elements.rename.disabled = state.busy || !selected;
    elements.remove.disabled = state.busy || !selected;
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    controls.setAttribute("aria-busy", state.busy ? "true" : "false");
    renderControls();
  }

  function setFeedback(message, kind) {
    elements.feedback.className = kind || "";
    elements.feedback.textContent = String(message || "");
    if (message) elements.announcement.textContent = String(message);
  }

  function selectedPreset() {
    return presetById(state.selectedId);
  }

  function presetById(id) {
    return state.presets.find((preset) => preset.id === Number(id)) || null;
  }

  function upsertPreset(preset) {
    const index = state.presets.findIndex((item) => item.id === preset.id);
    if (index < 0) state.presets.push(preset);
    else state.presets.splice(index, 1, preset);
    state.presets.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
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
    elements.rankSummary.hidden = true;
    elements.rankSummary.textContent = "";
  }

  function handlePresetSelection() {
    const previousApplied = Boolean(state.applied);
    state.selectedId = elements.select.value ? Number(elements.select.value) : null;
    const preset = selectedPreset();
    elements.name.value = preset?.name || "";
    clearApplied();
    renderControls();
    setFeedback(preset
      ? isDiscoveryPresetUiRepresentable(preset)
        ? `已选择筛选方案“${preset.name}”`
        : `已选择复杂方案“${preset.name}”：将按后端原定义只读应用，不会用当前界面覆盖保存`
      : "");
    if (previousApplied) void loadStandardResults();
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
    elements.apply.addEventListener("click", () => void applyPreset(1));
    elements.rename.addEventListener("click", () => void renamePreset());
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
      && payload.preset.id === identity.presetId;
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
    loadPresets,
    renamePreset,
    savePreset,
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

function applyPresetFields(preset, elements) {
  const criteria = preset.criteria || {};
  elements.status.value = "success";
  elements.keyword.value = "";
  elements.market.value = Array.isArray(criteria.market) && criteria.market.length === 1 ? criteria.market[0] : "";
  elements.industry.value = Array.isArray(criteria.industry) && criteria.industry.length === 1 ? criteria.industry[0] : "";
  elements.isSt.value = typeof criteria.is_st === "boolean" ? String(criteria.is_st) : "";
  elements.isNew.value = typeof criteria.is_new === "boolean" ? String(criteria.is_new) : "";
  elements.quality.value = criteria.quality?.min ?? "";
  const sort = Array.isArray(preset.sort) ? preset.sort[0] : null;
  elements.sort.value = SORT_TO_MARKET[sort?.field] || "score";
  elements.order.value = sort?.order === "asc" ? "asc" : "desc";
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
    apply: byId("discoveryPresetApply"),
    rename: byId("discoveryPresetRename"),
    remove: byId("discoveryPresetDelete"),
    feedback: byId("discoveryPresetFeedback"),
    announcement: byId("marketScanAnnouncement"),
    rankSummary: byId("discoveryRankSummary"),
    filters: byId("marketScanFilters"),
    status: byId("marketScanStatus"),
    market: byId("marketScanMarket"),
    industry: byId("marketScanIndustry"),
    isSt: byId("marketScanSt"),
    isNew: byId("marketScanNew"),
    quality: byId("marketScanQuality"),
    keyword: byId("marketScanKeyword"),
    sort: byId("marketScanSort"),
    order: byId("marketScanOrder"),
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

function inertDiscoveryController() {
  const noOp = async () => null;
  return {
    activate: noOp,
    applyPreset: noOp,
    deletePreset: noOp,
    loadPresets: noOp,
    renamePreset: noOp,
    savePreset: noOp,
    state: { activated: false, applied: null, presets: [] },
  };
}
