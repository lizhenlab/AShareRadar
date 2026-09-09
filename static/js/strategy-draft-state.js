import { strategySpecFromEditor } from "./strategy-lab-contracts.js";

export function strategyDraftKey(value) {
  if (Array.isArray(value)) return `[${value.map(strategyDraftKey).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${strategyDraftKey(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function validateCompiledStrategy(value, submitted = null) {
  if (!value?.normalized_spec || !/^[a-f0-9]{64}$/.test(value.fingerprint || "")
    || typeof value.execution_plan?.executable !== "boolean" || value.execution_plan.will_start_scan === true) {
    throw new Error("编译结果缺少有效草案指纹或执行边界，请重新编译");
  }
  if (submitted && !compilerPreservesDraft(value.normalized_spec, submitted)) {
    throw new Error("编译结果与发出的草案语义不一致，请重新编译");
  }
  return value;
}

function compilerPreservesDraft(normalized, submitted) {
  try {
    const left = unorderedCompilerFields(submitted);
    const right = unorderedCompilerFields(normalized);
    const weights = Object.entries(left.objectives);
    const total = weights.reduce((sum, [, value]) => sum + value, 0);
    if (!Number.isFinite(total) || total <= 0) return false;
    if (weights.length !== Object.keys(right.objectives).length) return false;
    if (weights.some(([field, value]) => !Number.isFinite(right.objectives[field])
      || Math.abs(right.objectives[field] - value / total) > 5.1e-11)) return false;
    left.objectives = right.objectives;
    return strategyDraftKey(left) === strategyDraftKey(right);
  } catch {
    return false;
  }
}

function unorderedCompilerFields(spec) {
  const value = structuredClone(spec);
  value.universe.boards.sort();
  value.hard_filters.sort((left, right) => strategyDraftKey(left).localeCompare(strategyDraftKey(right)));
  value.evidence_policy.allowed_sources.sort();
  value.evidence_policy.blocked_sources.sort();
  return value;
}

export function compiledEditorDraft(root, state) {
  if (!state.compileExecutable || state.compilePending || !state.compiledSpec || !state.compiledFingerprint) return null;
  try {
    const key = strategyDraftKey(strategySpecFromEditor(root, state.spec));
    if (key !== state.compiledEditorKey) return null;
    return { spec: structuredClone(state.compiledSpec), fingerprint: state.compiledFingerprint, key, epoch: state.editorEpoch };
  } catch {
    return null;
  }
}

export function matchesSavedStrategyDraft(root, state) {
  const draft = compiledEditorDraft(root, state);
  return Boolean(draft && state.strategy && draft.fingerprint === state.strategy.fingerprint
    && strategyDraftKey(draft.spec) === strategyDraftKey(state.strategy.spec));
}

export function validateSavedDraftConfirmation(strategy, submitted, previous) {
  if (strategy.fingerprint !== submitted.fingerprint || strategyDraftKey(strategy.spec) !== strategyDraftKey(submitted.spec)) {
    throw new Error("保存回执与提交草案的指纹或内容不一致，请刷新已保存策略核对");
  }
  if (previous && (strategy.strategy_id !== previous.strategy_id || strategy.revision <= previous.revision
    || strategy.strategy_version <= previous.strategy_version)) {
    throw new Error("保存回执与原策略身份或版本不一致，请刷新核对");
  }
  return strategy;
}

export function unknownStrategySaveResult(error) {
  const status = Number(error?.status);
  return !Number.isInteger(status) || status < 400 || status >= 500 || status === 408;
}

export function renderStrategyDraftStatus(element, state, savedMatches) {
  if (!element) return;
  if (state.saveOutcomePending) {
    element.textContent = "保存结果待核对。请刷新已保存策略列表并显式载入目标策略核对；不会自动重试或认领同名策略。";
    element.dataset.state = "unconfirmed";
    return;
  }
  const saved = state.strategy ? `已保存 #${state.strategy.strategy_id} v${state.strategy.strategy_version}` : "尚未保存";
  const phase = state.compilePending ? "正在编译当前输入" : savedMatches ? "编辑器与已保存版本一致" : state.compiledFingerprint ? "草案有未保存修改" : "当前输入未完成有效编译";
  element.textContent = `${saved} · ${phase}。${savedMatches ? "执行与新定时任务使用此版本。" : "请完成编译并确认保存后执行。"}`;
  element.dataset.state = state.compilePending ? "compiling" : savedMatches ? "saved" : "draft";
}
