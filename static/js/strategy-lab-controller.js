import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  strategySpecFromEditor,
  syncCustomObjectivesVisibility,
  syncCustomWeightsVisibility,
  syncStrategyEditor,
  validateCandidatePage,
  validateEvidence,
  validateHistory,
  validateParsedStrategy,
  validatePortfolioDraft,
  validateSchedule,
  validateSimulationPlan,
  validateStrategy,
  validateStrategyPage,
  validateVersionDiff,
  validateVersionPage,
} from "./strategy-lab-contracts.js";
import {
  announceStrategyLab,
  renderCandidateEvidence,
  renderCandidatePage,
  renderComparison,
  renderEvidence,
  renderExecutionPlan,
  renderHistory,
  renderParsedStrategy,
  renderPortfolioDraft,
  renderSchedule,
  renderSimulationPlan,
  renderStrategyList,
  renderVersionComparison,
  resetStrategyExecutionView,
  setStrategyLabBusy,
  strategyLabElements,
} from "./strategy-lab-view.js";
import { createStrategyTemplateCatalog } from "./strategy-template-catalog.js";

const API = "/api/strategy-lab";

export function createStrategyLabController(options = {}) {
  const root = options.root || globalThis.document;
  const shell = root?.getElementById?.("strategyLab");
  if (!shell) return inertController();
  const elements = strategyLabElements(root);
  const request = options.fetcher || fetchJson;
  const state = {
    activated: false, busy: false, strategies: [], parsed: null, spec: null, strategy: null,
    execution: null, candidatePage: null, candidatePageNumber: 1, candidateSort: "utility_score",
    compileExecutable: false,
  };
  const templateCatalog = createStrategyTemplateCatalog({
    root,
    fetcher: request,
    onLoadDraft: (template) => runTask("正在载入策略模板草案", () => applyTemplateDraft(template)),
  });
  bindEvents();
  syncActions();

  async function activate() {
    if (state.activated) return state.strategies;
    state.activated = true;
    await Promise.all([loadStrategies(), templateCatalog.load()]);
    return state.strategies;
  }

  async function applyTemplateDraft(template) {
    state.spec = structuredClone(template.strategy_spec);
    state.strategy = null;
    state.parsed = null;
    state.compileExecutable = false;
    clearExecution();
    syncStrategyEditor(root, state.spec);
    elements.strategyParseOutput.dataset.state = "ready";
    elements.strategyParseOutput.textContent = `已从“${template.name}”载入结构化研究草案；尚未保存或扫描。`;
    const compiled = await compileEditor(true, structuredClone(state.spec));
    syncActions();
    return compiled;
  }

  async function loadStrategies(selectedId = state.strategy?.strategy_id) {
    return runTask("正在读取策略列表", () => fetchStrategies(selectedId));
  }

  async function fetchStrategies(selectedId = state.strategy?.strategy_id) {
    const page = validateStrategyPage(await request(`${API}/strategies?include_archived=true&page_size=100`, timeout()));
    state.strategies = page.items;
    renderStrategyList(elements, page, selectedId);
    announceStrategyLab(elements, page.total ? `已读取 ${page.total} 个版本化策略` : "尚无已保存策略");
    syncActions();
    return page;
  }

  async function parseNaturalLanguage() {
    return runTask("正在解析策略意图", async () => {
      const payload = await request(`${API}/parse`, jsonInit({
        text: elements.strategyNaturalText.value,
        name: elements.strategyName.value,
      }));
      state.parsed = validateParsedStrategy(payload);
      state.spec = state.parsed.draft;
      state.strategy = null;
      templateCatalog.clearSource("模板来源已清除：当前草案来自自然语言解析。");
      clearExecution();
      syncStrategyEditor(root, state.spec);
      renderParsedStrategy(elements, state.parsed);
      await compileEditor(true);
      syncActions();
      const unsupported = state.parsed.unsupported_clauses.length;
      announceStrategyLab(elements, unsupported ? `发现 ${unsupported} 个未支持条件，保存已阻断` : "策略草案已生成，请核对后确认保存", unsupported ? "warn" : "ready");
      return state.parsed;
    });
  }

  async function compileEditor(force = false, suppliedSpec = null) {
    if (!state.spec || (state.busy && !force)) return null;
    try {
      const spec = suppliedSpec || strategySpecFromEditor(root, state.spec);
      const compiled = await request(`${API}/compile`, jsonInit({ spec, dry_run: true }));
      state.spec = compiled.normalized_spec;
      state.compileExecutable = compiled.execution_plan?.executable === true;
      renderExecutionPlan(elements, compiled);
      if (!force) {
        announceStrategyLab(
          elements,
          state.compileExecutable ? "执行计划已更新，可以确认保存" : "执行计划仍有阻断条件",
          state.compileExecutable ? "ready" : "warn",
        );
      }
      syncActions();
      return compiled;
    } catch (error) {
      state.compileExecutable = false;
      announceStrategyLab(elements, compactErrorMessage(error.message), "error");
      syncActions();
      return null;
    }
  }

  async function saveStrategy() {
    return runTask("正在保存不可变策略版本", async () => {
      const spec = strategySpecFromEditor(root, state.spec);
      const updating = state.strategy && state.strategy.strategy_id === Number(elements.strategySavedSelect.value);
      const url = updating ? `${API}/strategies/${state.strategy.strategy_id}` : `${API}/strategies`;
      const body = updating
        ? { spec, expected_revision: state.strategy.revision, confirmed: true }
        : { spec, confirmed: true };
      const strategy = validateStrategy(await request(url, jsonInit(body, updating ? "PUT" : "POST")));
      state.strategy = strategy;
      state.spec = strategy.spec;
      templateCatalog.clearSource("模板来源已清除：策略已保存为独立版本。");
      state.compileExecutable = true;
      clearExecution();
      await fetchStrategies(strategy.strategy_id);
      await loadHistory();
      announceStrategyLab(elements, `策略 #${strategy.strategy_id} v${strategy.strategy_version} 已保存；旧版本保持不变`);
      syncActions();
      return strategy;
    });
  }

  async function loadSelectedStrategy() {
    const id = Number(elements.strategySavedSelect.value);
    if (!id) return null;
    return runTask("正在载入策略版本", async () => {
      const strategy = validateStrategy(await request(`${API}/strategies/${id}`, timeout()));
      state.strategy = strategy;
      state.spec = strategy.spec;
      state.parsed = null;
      templateCatalog.clearSource("模板来源已清除：当前为已保存策略版本。");
      clearExecution();
      syncStrategyEditor(root, strategy.spec);
      await compileEditor(true);
      await Promise.all([loadHistory(), loadEvidence(false)]);
      announceStrategyLab(elements, `已载入策略 #${id} v${strategy.strategy_version}`);
      syncActions();
      return strategy;
    });
  }

  async function copyStrategy() {
    if (!state.strategy) return null;
    return runTask("正在复制策略", async () => {
      const name = `${state.strategy.spec.name} 副本`;
      const payload = await request(`${API}/strategies/${state.strategy.strategy_id}/copy`, jsonInit({ name, revision: state.strategy.strategy_version, confirmed: true }));
      const copied = validateStrategy(payload);
      state.strategy = copied;
      state.spec = copied.spec;
      templateCatalog.clearSource("模板来源已清除：当前为独立复制的策略版本。");
      state.compileExecutable = true;
      clearExecution();
      syncStrategyEditor(root, copied.spec);
      await fetchStrategies(copied.strategy_id);
      announceStrategyLab(elements, `已复制为策略 #${copied.strategy_id}`);
      syncActions();
      return copied;
    });
  }

  async function archiveStrategy() {
    if (!state.strategy) return null;
    return runTask("正在归档策略", async () => {
      const archived = validateStrategy(await request(`${API}/strategies/${state.strategy.strategy_id}/archive`, jsonInit({ expected_revision: state.strategy.revision, archived: true })));
      state.strategy = archived;
      await fetchStrategies(archived.strategy_id);
      announceStrategyLab(elements, "策略已归档；历史版本、执行和证据仍可读取", "warn");
      syncActions();
      return archived;
    });
  }

  async function execute(kind) {
    if (!state.strategy) return null;
    return runTask(kind === "latest_scan" ? "正在生成最近组合草案" : "正在进行历史时点回放", async () => {
      const body = executionRequest(kind);
      const draft = validatePortfolioDraft(await request(`${API}/executions`, jsonInit(body, "POST", 120000)));
      state.execution = draft;
      state.candidatePageNumber = 1;
      renderPortfolioDraft(elements, draft);
      await Promise.all([loadCandidates(), loadHistory()]);
      announceStrategyLab(elements, draft.summary.no_trade ? "执行完成：当前约束下 no_trade，请查看原因" : `执行完成：形成 ${draft.summary.selected_count} 只研究组合草案`, draft.summary.no_trade ? "warn" : "ready");
      syncActions();
      return draft;
    });
  }

  async function loadCandidates() {
    if (!state.execution) return null;
    const id = state.execution.context.execution_id;
    const descending = !["risk", "original_rank"].includes(state.candidateSort);
    const query = new URLSearchParams({ page: state.candidatePageNumber, page_size: 50, sort_by: state.candidateSort, descending });
    const page = validateCandidatePage(await request(`${API}/executions/${id}/candidates?${query}`, timeout()));
    state.candidatePage = page;
    renderCandidatePage(elements, page);
    return page;
  }

  async function loadEvidence(refresh) {
    if (!state.strategy) return null;
    const id = state.strategy.strategy_id;
    const url = refresh ? `${API}/strategies/${id}/evidence/refresh` : `${API}/strategies/${id}/evidence?revision=${state.strategy.strategy_version}&mode=official`;
    const init = refresh ? jsonInit({ revision: state.strategy.strategy_version, mode: "official" }) : timeout();
    const evidence = validateEvidence(await request(url, init));
    renderEvidence(elements, evidence);
    if (refresh) announceStrategyLab(elements, `证据快照已生成：${evidence.status}`, evidence.status === "eligible_for_manual_review" ? "ready" : "warn");
    return evidence;
  }

  async function loadHistory() {
    if (!state.strategy) return null;
    const id = state.strategy.strategy_id;
    const [executions, versions] = await Promise.all([
      request(`${API}/strategies/${id}/executions?page_size=100`, timeout()).then(validateHistory),
      request(`${API}/strategies/${id}/versions`, timeout()).then(validateVersionPage),
    ]);
    renderHistory(elements, executions, versions);
    return executions;
  }

  async function compareExecutions() {
    const left = Number(elements.strategyCompareLeft.value);
    const right = Number(elements.strategyCompareRight.value);
    if (!left || !right || left === right) throw new Error("请选择两个不同的历史执行");
    return runTask("正在比较历史执行", async () => {
      const query = new URLSearchParams({ left_execution_id: left, right_execution_id: right });
      const comparison = await request(`${API}/executions/compare?${query}`, timeout());
      renderComparison(elements, comparison);
      announceStrategyLab(elements, "历史执行比较完成");
      return comparison;
    });
  }

  async function compareVersions() {
    if (!state.strategy) return null;
    const left = Number(elements.strategyVersionLeft.value);
    const right = Number(elements.strategyVersionRight.value);
    if (!left || !right || left === right) throw new Error("请选择两个不同的 StrategySpec 版本");
    return runTask("正在比较 StrategySpec 版本", async () => {
      const query = new URLSearchParams({ left_revision: left, right_revision: right });
      const comparison = validateVersionDiff(await request(`${API}/strategies/${state.strategy.strategy_id}/diff?${query}`, timeout()));
      renderVersionComparison(elements, comparison);
      announceStrategyLab(elements, `StrategySpec v${left} 与 v${right} 比较完成`);
      return comparison;
    });
  }

  async function createSchedule() {
    if (!state.strategy) return null;
    return runTask("正在保存盘后定时执行", async () => {
      const schedule = validateSchedule(await request(`${API}/schedules`, jsonInit({ strategy_id: state.strategy.strategy_id, revision: state.strategy.strategy_version, cadence: "daily_after_close", mode: "official", notional_cash_cny: Number(elements.strategyNotional.value) })));
      renderSchedule(elements, schedule);
      announceStrategyLab(elements, `定时任务 #${schedule.schedule_id} 已固定绑定策略 v${schedule.strategy_version}`);
      return schedule;
    });
  }

  async function createSimulationPlan() {
    if (!state.execution) return null;
    return runTask("正在生成模拟交易研究计划", async () => {
      const id = state.execution.context.execution_id;
      const plan = validateSimulationPlan(await request(`${API}/executions/${id}/simulation-plan`, jsonInit({}, "POST")));
      renderSimulationPlan(elements, plan);
      announceStrategyLab(elements, `模拟计划 #${plan.plan_id} 已生成：${plan.orders.length} 条纸面委托；不会提交券商`);
      return plan;
    });
  }

  async function runTask(label, task) {
    if (state.busy) return null;
    state.busy = true;
    setStrategyLabBusy(elements, true, label);
    try {
      return await task();
    } catch (error) {
      announceStrategyLab(elements, compactErrorMessage(error.message), "error");
      return null;
    } finally {
      state.busy = false;
      elements.strategyLab.setAttribute("aria-busy", "false");
      if (elements.strategyLabStatus.dataset.kind === "busy") {
        announceStrategyLab(elements, "操作完成");
      }
      syncActions();
    }
  }

  function syncActions() {
    const hasStrategy = Boolean(state.strategy && !state.strategy.archived);
    const hasCurrentExecution = Boolean(
      hasStrategy
      && state.execution
      && state.execution.context.strategy_id === state.strategy.strategy_id
      && state.execution.context.strategy_version === state.strategy.strategy_version
    );
    const canSave = Boolean(state.spec && state.compileExecutable && !state.parsed?.unsupported_clauses?.length);
    elements.strategySave.disabled = state.busy || !canSave;
    elements.strategyExecuteLatest.disabled = state.busy || !hasStrategy;
    elements.strategyExecuteReplay.disabled = state.busy || !hasStrategy;
    elements.strategyCreateSchedule.disabled = state.busy || !hasStrategy;
    elements.strategyEvidenceRefresh.disabled = state.busy || !hasStrategy;
    elements.strategyHistoryRefresh.disabled = state.busy || !state.strategy;
    elements.strategyArchive.disabled = state.busy || !hasStrategy;
    elements.strategyCreateSimulation.disabled = state.busy || !hasCurrentExecution;
    elements.strategyParse.disabled = state.busy;
    templateCatalog.setBusy(state.busy);
  }

  function bindEvents() {
    elements.strategyParse.addEventListener("click", parseNaturalLanguage);
    elements.strategyListRefresh.addEventListener("click", () => loadStrategies());
    elements.strategyLoad.addEventListener("click", loadSelectedStrategy);
    elements.strategyCopy.addEventListener("click", copyStrategy);
    elements.strategySave.addEventListener("click", saveStrategy);
    elements.strategyArchive.addEventListener("click", archiveStrategy);
    elements.strategyExecuteLatest.addEventListener("click", () => execute("latest_scan"));
    elements.strategyExecuteReplay.addEventListener("click", () => execute("historical_replay"));
    elements.strategyEvidenceRefresh.addEventListener("click", () => runTask("正在刷新跨日期证据", () => loadEvidence(true)));
    elements.strategyHistoryRefresh.addEventListener("click", () => runTask("正在刷新历史", loadHistory));
    elements.strategyCompare.addEventListener("click", compareExecutions);
    elements.strategyVersionCompare.addEventListener("click", compareVersions);
    elements.strategyCreateSchedule.addEventListener("click", createSchedule);
    elements.strategyCreateSimulation.addEventListener("click", createSimulationPlan);
    elements.strategyCandidatePrev.addEventListener("click", () => changeCandidatePage(-1));
    elements.strategyCandidateNext.addEventListener("click", () => changeCandidatePage(1));
    elements.strategyCandidateRows.addEventListener("click", openCandidateEvidence);
    elements.strategyCandidateDialogClose.addEventListener("click", () => elements.strategyCandidateDialog.close());
    root.querySelectorAll("[data-strategy-sort]").forEach((button) => button.addEventListener("click", selectSort));
    root.querySelectorAll("#strategyEditor input, #strategyEditor select, #strategyEditor textarea").forEach((input) => input.addEventListener("change", () => {
      templateCatalog.markCustom();
      void compileEditor(false);
    }));
    elements.strategyWeightingMethod.addEventListener("change", () => syncCustomWeightsVisibility(root));
    elements.strategyProfile.addEventListener("change", () => syncCustomObjectivesVisibility(root));
  }

  function executionRequest(kind) {
    const body = { strategy_id: state.strategy.strategy_id, revision: state.strategy.strategy_version, kind, mode: "official", notional_cash_cny: Number(elements.strategyNotional.value), current_weights: {} };
    if (kind === "historical_replay") {
      if (!elements.strategyReplayDate.value) throw new Error("请选择历史数据日");
      body.data_date = elements.strategyReplayDate.value;
    }
    return body;
  }

  function changeCandidatePage(offset) {
    state.candidatePageNumber = Math.max(1, state.candidatePageNumber + offset);
    void runTask("正在读取候选分页", loadCandidates);
  }

  function selectSort(event) {
    state.candidateSort = event.currentTarget.dataset.strategySort;
    state.candidatePageNumber = 1;
    root.querySelectorAll("[data-strategy-sort]").forEach((button) => {
      const active = button === event.currentTarget;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    void runTask("正在切换独立排序", loadCandidates);
  }

  function openCandidateEvidence(event) {
    const button = event.target.closest("[data-strategy-candidate]");
    if (!button) return;
    const item = state.candidatePage?.items?.find((candidate) => candidate.symbol === button.dataset.strategyCandidate);
    if (item) renderCandidateEvidence(elements, item);
  }

  function clearExecution() {
    state.execution = null;
    state.candidatePage = null;
    state.candidatePageNumber = 1;
    resetStrategyExecutionView(elements);
  }

  return { activate, state, loadStrategies, parseNaturalLanguage, execute, loadEvidence, templateCatalog };
}

function jsonInit(body, method = "POST", timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs };
}

function timeout(timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  return { timeoutMs };
}

function inertController() {
  return { activate: async () => [], state: { activated: false }, loadStrategies: async () => null, parseNaturalLanguage: async () => null, execute: async () => null, loadEvidence: async () => null };
}
