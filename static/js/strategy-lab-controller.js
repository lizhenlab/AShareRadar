import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import {
  strategySpecFromEditor,
  syncCustomObjectivesVisibility,
  syncCustomWeightsVisibility,
  syncStrategyEditor,
  validateCandidatePage,
  validateEvidence,
  validateParsedStrategy,
  validatePortfolioDraft,
  validateSchedule,
  validateSimulationPlan,
  validateStrategy,
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
import { createStrategyScheduleManager } from "./strategy-schedule-manager.js";
import {
  compiledEditorDraft, matchesSavedStrategyDraft, renderStrategyDraftStatus, strategyDraftKey,
  unknownStrategySaveResult, validateCompiledStrategy, validateSavedDraftConfirmation,
} from "./strategy-draft-state.js";
import {
  readBoundedStrategyPage, renderPageStatus, strategyPageQuery, syncStrategyPaging, validateExecutionHistoryPage,
  validateSavedCandidatePage, validateSavedExecution, validateSavedStrategyPage,
} from "./strategy-history.js";

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
    compileExecutable: false, scheduleWriting: false,
    editorEpoch: 0, compileSequence: 0, compilePending: false, compileDeferred: false,
    compiledSpec: null, compiledFingerprint: null, compiledEditorKey: null,
    saveOutcomePending: false,
    strategyPage: null, strategyListStale: false, strategyListSequence: 0,
    historyPage: null, historySequence: 0, executionReadOnly: false,
  };
  let compileTimer = null;
  let compileAbort = null;
  const templateCatalog = createStrategyTemplateCatalog({
    root,
    fetcher: request,
    onLoadDraft: (template) => runTask("正在载入策略模板草案", () => applyTemplateDraft(template)),
  });
  const scheduleManager = createStrategyScheduleManager({
    root, fetcher: request,
    onWritingChange: (busy) => { state.scheduleWriting = busy; syncActions(); resumeDeferredCompilation(); },
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
    invalidateCompilation();
    state.spec = structuredClone(template.strategy_spec);
    state.strategy = null;
    resetHistory();
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

  async function loadStrategies(selectedId = state.strategy?.strategy_id, pageNumber = state.strategyPage?.page || 1) {
    return runTask("正在读取策略列表", () => fetchStrategies(selectedId, pageNumber));
  }

  async function fetchStrategies(selectedId = state.strategy?.strategy_id, pageNumber = 1) {
    const sequence = ++state.strategyListSequence;
    const page = await readBoundedStrategyPage(async number => validateSavedStrategyPage(
      await request(`${API}/strategies?include_archived=true&${strategyPageQuery(number)}`, timeout()), number
    ), pageNumber);
    if (sequence !== state.strategyListSequence) return null;
    state.strategyPage = page;
    state.strategyListStale = false;
    state.strategies = page.items;
    renderStrategyList(elements, page, selectedId, state.strategy);
    renderPageStatus(elements.strategyListPage, page);
    announceStrategyLab(elements, page.total ? `已读取第 ${page.page} 页 ${page.items.length} 个策略，共 ${page.total} 个` : "尚无已保存策略");
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
      invalidateCompilation();
      state.spec = state.parsed.draft;
      state.strategy = null;
      resetHistory();
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
    clearTimeout(compileTimer);
    if (!state.spec) return null;
    if ((state.busy || state.scheduleWriting) && !force) { state.compileDeferred = true; return null; }
    invalidateCompilation();
    const owner = { sequence: state.compileSequence, epoch: state.editorEpoch };
    compileAbort = new AbortController();
    state.compilePending = true;
    syncActions();
    try {
      const spec = suppliedSpec || strategySpecFromEditor(root, state.spec);
      const compiled = validateCompiledStrategy(await request(`${API}/compile`, { ...jsonInit({ spec, dry_run: true }), signal: compileAbort.signal }), spec);
      if (!ownsCompilation(owner)) return null;
      state.spec = compiled.normalized_spec;
      rememberCompiledDraft(compiled);
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
      if (!ownsCompilation(owner)) return null;
      state.compilePending = false;
      state.compileExecutable = false;
      announceStrategyLab(elements, compactErrorMessage(error.message), "error");
      syncActions();
      return null;
    }
  }

  async function saveStrategy() {
    return runTask("正在保存不可变策略版本", async () => {
      if (state.saveOutcomePending) throw new Error("保存结果待核对，请刷新列表并显式载入目标策略后再保存");
      const submitted = compiledEditorDraft(root, state);
      if (!submitted || state.parsed?.unsupported_clauses?.length) throw new Error("当前输入尚未完成有效编译，请核对草案后再保存");
      const previous = state.strategy ? structuredClone(state.strategy) : null;
      const spec = submitted.spec;
      const updating = Boolean(previous);
      const url = updating ? `${API}/strategies/${previous.strategy_id}` : `${API}/strategies`;
      const body = updating
        ? { spec, expected_revision: previous.revision, confirmed: true }
        : { spec, confirmed: true };
      const strategy = await saveDraftWithConfirmation(url, body, submitted, previous);
      const editedDuringSave = state.editorEpoch !== submitted.epoch || compiledEditorDraft(root, state)?.key !== submitted.key;
      rememberConfirmedStrategy(strategy, editedDuringSave);
      templateCatalog.clearSource("模板来源已清除：策略已保存为独立版本。");
      if (!editedDuringSave) rememberCompiledDraft({ normalized_spec: strategy.spec, fingerprint: strategy.fingerprint });
      clearExecution();
      await refreshConfirmedViews(`策略 #${strategy.strategy_id} v${strategy.strategy_version} 已保存；旧版本保持不变`, [
        ["策略列表", () => fetchStrategies(strategy.strategy_id)], ["执行历史", loadHistory],
      ]);
      syncActions();
      return strategy;
    });
  }

  async function loadSelectedStrategy() {
    const id = Number(elements.strategySavedSelect.value);
    if (!id) return null;
    return runTask("正在载入策略版本", async () => {
      const strategy = validateStrategy(await request(`${API}/strategies/${id}`, timeout()));
      if (strategy.strategy_id !== id) throw new Error("载入策略身份与选择不一致");
      state.saveOutcomePending = false;
      invalidateCompilation();
      state.strategy = strategy;
      resetHistory();
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
    if (!state.strategy || state.saveOutcomePending) return null;
    return runTask("正在复制策略", async () => {
      const name = `${state.strategy.spec.name} 副本`;
      const payload = await request(`${API}/strategies/${state.strategy.strategy_id}/copy`, jsonInit({ name, revision: state.strategy.strategy_version, confirmed: true }));
      const copied = validateStrategy(payload);
      rememberConfirmedStrategy(copied);
      templateCatalog.clearSource("模板来源已清除：当前为独立复制的策略版本。");
      clearExecution();
      syncStrategyEditor(root, copied.spec);
      await compileEditor(true);
      await refreshConfirmedViews(`已复制为策略 #${copied.strategy_id}`, [
        ["策略列表", () => fetchStrategies(copied.strategy_id)],
      ]);
      syncActions();
      return copied;
    });
  }

  async function archiveStrategy() {
    if (!state.strategy) return null;
    return runTask("正在归档策略", async () => {
      const archived = validateStrategy(await request(`${API}/strategies/${state.strategy.strategy_id}/archive`, jsonInit({ expected_revision: state.strategy.revision, archived: true })));
      rememberConfirmedStrategy(archived);
      await refreshConfirmedViews("策略已归档；历史版本、执行和证据仍可读取", [
        ["策略列表", () => fetchStrategies(archived.strategy_id)],
      ], "warn");
      syncActions();
      return archived;
    });
  }

  async function execute(kind) {
    if (!requireSavedDraft()) return null;
    return runTask(kind === "latest_scan" ? "正在生成最近组合草案" : "正在进行历史时点回放", async () => {
      const body = executionRequest(kind);
      const draft = validatePortfolioDraft(await request(`${API}/executions`, jsonInit(body, "POST", 120000)));
      clearExecution();
      state.execution = draft;
      state.executionReadOnly = false;
      renderPortfolioDraft(elements, draft);
      const message = draft.summary.no_trade ? "执行完成：当前约束下 no_trade，请查看原因" : `执行完成：形成 ${draft.summary.selected_count} 只研究组合草案`;
      await refreshConfirmedViews(message, [
        ["候选分页", () => loadCandidates()], ["执行历史", loadHistory],
      ], draft.summary.no_trade ? "warn" : "ready");
      syncActions();
      return draft;
    });
  }

  async function loadCandidates(pageNumber = state.candidatePageNumber, sort = state.candidateSort) {
    if (!state.execution) return null;
    const execution = state.execution;
    const id = execution.context.execution_id;
    const descending = !["risk", "original_rank"].includes(sort);
    const query = new URLSearchParams({ page: pageNumber, page_size: 50, sort_by: sort, descending });
    const page = validateCandidatePage(await request(`${API}/executions/${id}/candidates?${query}`, timeout()));
    if (state.execution !== execution) return null;
    if (state.executionReadOnly) validateSavedCandidatePage(page, id, pageNumber);
    if (page.page !== pageNumber) throw new Error("候选分页与请求页码不一致，请重试");
    state.candidatePage = page;
    state.candidatePageNumber = pageNumber;
    state.candidateSort = sort;
    root.querySelectorAll("[data-strategy-sort]").forEach((button) => {
      const active = button.dataset.strategySort === sort;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderCandidatePage(elements, page);
    return page;
  }

  function rememberConfirmedStrategy(strategy, preserveEditor = false) {
    state.strategyListSequence += 1;
    state.strategyListStale = true;
    state.strategy = strategy;
    resetHistory();
    if (!preserveEditor) { invalidateCompilation(); state.spec = strategy.spec; }
    state.strategies = state.strategies.map(item => item.strategy_id === strategy.strategy_id ? strategy : item);
    renderStrategyList(elements, { items: state.strategies }, strategy.strategy_id, strategy);
  }

  async function refreshConfirmedViews(message, tasks, kind = "ready", isCurrent = () => true) {
    const results = await Promise.allSettled(tasks.map(([, load]) => load()));
    if (!isCurrent()) return;
    const failures = tasks.filter((_, index) => results[index].status === "rejected").map(([label]) => label);
    if (failures.length) {
      announceStrategyLab(elements, `${message}；${failures.join("、")}同步未完成，请刷新对应视图或重试排序，无需重复提交`, "warn");
    } else {
      announceStrategyLab(elements, message, kind);
    }
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

  async function loadHistory(pageNumber = 1) {
    if (!state.strategy) return null;
    const owner = state.strategy;
    const sequence = ++state.historySequence;
    const id = owner.strategy_id;
    const [executions, versions] = await Promise.all([
      readBoundedStrategyPage(async number => validateExecutionHistoryPage(
        await request(`${API}/strategies/${id}/executions?${strategyPageQuery(number)}`, timeout()), id, number
      ), pageNumber),
      request(`${API}/strategies/${id}/versions`, timeout()).then(validateVersionPage),
    ]);
    if (state.strategy !== owner || sequence !== state.historySequence) return null;
    state.historyPage = executions;
    renderHistory(elements, executions, versions);
    renderPageStatus(elements.strategyHistoryPage, executions);
    return executions;
  }

  async function loadSavedExecution() {
    const selected = state.historyPage?.items.find(item => item.execution_id === Number(elements.strategyExecutionSelect.value));
    if (!selected || !state.strategy) return null;
    return runTask("正在读取已保存执行", async () => {
      const owner = state.strategy;
      const draft = validateSavedExecution(await request(`${API}/executions/${selected.execution_id}`, timeout()), selected);
      if (state.strategy !== owner) return null;
      clearExecution();
      state.execution = draft;
      state.executionReadOnly = true;
      renderPortfolioDraft(elements, draft);
      await refreshConfirmedViews(`已读取执行 #${selected.execution_id}（策略 v${selected.strategy_version}，只读）；当前编辑草案保持不变`, [
        ["候选分页", () => loadCandidates()], ["已保存纸面委托", () => loadSavedSimulationPlan(draft)],
      ], "ready", () => state.execution === draft && state.strategy === owner);
      return draft;
    });
  }

  async function loadSavedSimulationPlan(draft) {
    try {
      const payload = await request(`${API}/executions/${draft.context.execution_id}/simulation-plan`, timeout());
      if (state.execution !== draft) return null;
      const plan = payload === null ? null : validateSimulationPlan(payload, draft.context);
      if (plan) renderSimulationPlan(elements, plan);
      else elements.strategyLifecycleContent.textContent = "该执行尚未保存纸面委托；查看历史不会自动生成。";
      return plan;
    } catch (error) {
      if (state.execution === draft) elements.strategyLifecycleContent.textContent = "已保存纸面委托读取失败；重新查看该执行可重试，不会生成新委托。";
      throw error;
    }
  }

  function resetHistory() {
    state.historySequence += 1;
    state.historyPage = null;
    renderHistory(elements, { items: [], total: 0 }, { items: [], total: 0 });
    renderPageStatus(elements.strategyHistoryPage, null);
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
    if (!requireSavedDraft()) return null;
    return runTask("正在保存盘后定时执行", async () => {
      scheduleManager.beginCreation();
      const schedule = validateSchedule(await request(`${API}/schedules`, jsonInit({ strategy_id: state.strategy.strategy_id, revision: state.strategy.strategy_version, cadence: "daily_after_close", mode: "official", notional_cash_cny: Number(elements.strategyNotional.value) })));
      renderSchedule(elements, schedule);
      announceStrategyLab(elements, `定时任务 #${schedule.schedule_id} 已固定绑定策略 v${schedule.strategy_version}`);
      await scheduleManager.confirmCreation(schedule);
      return schedule;
    });
  }

  async function createSimulationPlan() {
    if (!state.execution || state.executionReadOnly || !requireSavedDraft()) return null;
    return runTask("正在生成模拟交易研究计划", async () => {
      const id = state.execution.context.execution_id;
      const plan = validateSimulationPlan(await request(`${API}/executions/${id}/simulation-plan`, jsonInit({}, "POST")), state.execution.context);
      renderSimulationPlan(elements, plan);
      announceStrategyLab(elements, `纸面委托草案 #${plan.plan_id} 已生成：${plan.orders.length} 条；可核对明细，不会加入复盘模拟账户`);
      return plan;
    });
  }

  async function runTask(label, task) {
    if (state.busy || state.scheduleWriting) return null;
    state.busy = true;
    scheduleManager.setBusy(true);
    setStrategyLabBusy(elements, true, label);
    syncActions();
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
      resumeDeferredCompilation();
    }
  }

  function syncActions() {
    const busy = state.busy || state.scheduleWriting;
    const hasStrategy = Boolean(state.strategy && !state.strategy.archived);
    const savedMatches = !state.saveOutcomePending && matchesSavedStrategyDraft(root, state);
    const hasCurrentExecution = currentExecutionMatchesSaved(hasStrategy && savedMatches);
    const canSave = Boolean(!state.saveOutcomePending && compiledEditorDraft(root, state) && !state.parsed?.unsupported_clauses?.length);
    elements.strategySave.disabled = busy || !canSave;
    elements.strategyExecuteLatest.disabled = busy || !hasStrategy || !savedMatches;
    elements.strategyExecuteReplay.disabled = busy || !hasStrategy || !savedMatches;
    elements.strategyCreateSchedule.disabled = busy || !hasStrategy || !savedMatches;
    elements.strategyEvidenceRefresh.disabled = busy || !hasStrategy;
    elements.strategyHistoryRefresh.disabled = busy || !state.strategy;
    elements.strategyArchive.disabled = busy || !hasStrategy;
    elements.strategyCreateSimulation.disabled = busy || !hasCurrentExecution;
    elements.strategyParse.disabled = busy;
    templateCatalog.setBusy(busy);
    scheduleManager.setStrategy(state.strategy);
    scheduleManager.setBusy(state.busy);
    syncStrategyPaging(elements, state, busy);
    renderStrategyDraftStatus(root.getElementById("strategyDraftStatus"), state, savedMatches);
  }

  function currentExecutionMatchesSaved(ready) {
    const context = state.execution?.context;
    return Boolean(ready && !state.executionReadOnly && context && context.strategy_id === state.strategy.strategy_id
      && context.strategy_version === state.strategy.strategy_version
      && context.strategy_fingerprint === state.strategy.fingerprint);
  }

  function bindEvents() {
    elements.strategyParse.addEventListener("click", parseNaturalLanguage);
    elements.strategyListRefresh.addEventListener("click", () => loadStrategies());
    elements.strategyListPrev.addEventListener("click", () => changeStrategyPage(-1));
    elements.strategyListNext.addEventListener("click", () => changeStrategyPage(1));
    elements.strategyLoad.addEventListener("click", loadSelectedStrategy);
    elements.strategyCopy.addEventListener("click", copyStrategy);
    elements.strategySave.addEventListener("click", saveStrategy);
    elements.strategyArchive.addEventListener("click", archiveStrategy);
    elements.strategyExecuteLatest.addEventListener("click", () => execute("latest_scan"));
    elements.strategyExecuteReplay.addEventListener("click", () => execute("historical_replay"));
    elements.strategyEvidenceRefresh.addEventListener("click", () => runTask("正在刷新跨日期证据", () => loadEvidence(true)));
    elements.strategyHistoryRefresh.addEventListener("click", () => runTask("正在刷新历史", loadHistory));
    elements.strategyHistoryPrev.addEventListener("click", () => changeHistoryPage(-1));
    elements.strategyHistoryNext.addEventListener("click", () => changeHistoryPage(1));
    elements.strategyExecutionLoad.addEventListener("click", loadSavedExecution);
    elements.strategyCompare.addEventListener("click", compareExecutions);
    elements.strategyVersionCompare.addEventListener("click", compareVersions);
    elements.strategyCreateSchedule.addEventListener("click", createSchedule);
    elements.strategyCreateSimulation.addEventListener("click", createSimulationPlan);
    elements.strategyCandidatePrev.addEventListener("click", () => changeCandidatePage(-1));
    elements.strategyCandidateNext.addEventListener("click", () => changeCandidatePage(1));
    elements.strategyCandidateRows.addEventListener("click", openCandidateEvidence);
    elements.strategyCandidateDialogClose.addEventListener("click", () => elements.strategyCandidateDialog.close());
    root.querySelectorAll("[data-strategy-sort]").forEach((button) => button.addEventListener("click", selectSort));
    root.querySelectorAll("#strategyEditor input, #strategyEditor select, #strategyEditor textarea").forEach((input) => {
      input.addEventListener("input", handleDraftInput);
      input.addEventListener("change", () => {
        if (compiledEditorDraft(root, state)) return;
        handleDraftInput(); void compileEditor(false);
      });
    });
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
    const page = Math.max(1, state.candidatePageNumber + offset);
    void runTask("正在读取候选分页", () => loadCandidates(page));
  }

  function changeStrategyPage(offset) {
    const page = state.strategyPage;
    const next = (page?.page || 1) + offset;
    if (!page || state.strategyListStale || next < 1 || next > page.page_count) return null;
    return loadStrategies(state.strategy?.strategy_id, next);
  }

  function changeHistoryPage(offset) {
    const page = state.historyPage;
    const next = (page?.page || 1) + offset;
    if (!page || next < 1 || next > page.page_count) return null;
    return runTask("正在读取历史分页", () => loadHistory(next));
  }

  function selectSort(event) {
    const sort = event.currentTarget.dataset.strategySort;
    void runTask("正在切换独立排序", () => loadCandidates(1, sort));
  }

  function openCandidateEvidence(event) {
    const button = event.target.closest("[data-strategy-candidate]");
    if (!button) return;
    const item = state.candidatePage?.items?.find((candidate) => candidate.symbol === button.dataset.strategyCandidate);
    if (item) renderCandidateEvidence(elements, item);
  }

  function clearExecution() {
    state.execution = null;
    state.executionReadOnly = false;
    state.candidatePage = null;
    state.candidatePageNumber = 1;
    resetStrategyExecutionView(elements);
  }

  function invalidateCompilation() {
    clearTimeout(compileTimer);
    compileAbort?.abort();
    compileAbort = null;
    state.compileSequence += 1;
    state.compilePending = false;
    state.compileExecutable = false;
    state.compiledSpec = null;
    state.compiledFingerprint = null;
    state.compiledEditorKey = null;
  }

  function handleDraftInput() {
    state.editorEpoch += 1;
    invalidateCompilation();
    templateCatalog.markCustom();
    syncActions();
    compileTimer = setTimeout(() => void compileEditor(false), 200);
  }

  function ownsCompilation(owner) {
    return owner.sequence === state.compileSequence && owner.epoch === state.editorEpoch;
  }

  function resumeDeferredCompilation() {
    if (!state.compileDeferred || state.busy || state.scheduleWriting) return;
    state.compileDeferred = false;
    void compileEditor();
  }

  function rememberCompiledDraft(compiled) {
    state.compiledSpec = structuredClone(compiled.normalized_spec);
    state.compiledFingerprint = compiled.fingerprint;
    state.compiledEditorKey = strategyDraftKey(strategySpecFromEditor(root, state.spec));
    state.compilePending = false;
    state.compileExecutable = true;
  }

  function requireSavedDraft() {
    if (!state.saveOutcomePending && state.strategy && !state.strategy.archived && matchesSavedStrategyDraft(root, state)) return true;
    announceStrategyLab(elements, "当前草案未保存或仍在编译，请确认保存后再执行或创建计划", "warn");
    return false;
  }

  async function saveDraftWithConfirmation(url, body, submitted, previous) {
    try {
      const response = await request(url, jsonInit(body, previous ? "PUT" : "POST"));
      return validateSavedDraftConfirmation(validateStrategy(response), submitted, previous);
    } catch (error) {
      if (!unknownStrategySaveResult(error)) throw error;
      state.saveOutcomePending = true;
      throw new Error(`保存结果待核对：${compactErrorMessage(error.message)}；请刷新列表并显式载入目标策略，勿重复提交`);
    }
  }

  return { activate, state, loadStrategies, parseNaturalLanguage, execute, loadEvidence, templateCatalog, scheduleManager };
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
