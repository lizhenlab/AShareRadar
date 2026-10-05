import { comparisonRunKey } from "./market-scan-comparison-contracts.js";
import { normalizedScreenSpecKey, screenSpecFromPreset, screenSpecFromResultsQuery } from "./market-scan-screen-spec-source.js";

const records = new WeakMap();
const EVIDENCE_FIELDS = ["status", "mode", "scope", "data_date", "quote_date", "rule_version", "finished_at", "snapshot_digest", "snapshot_seal_origin", "snapshot_sealed_at"];

function recordFor(element) {
  if (!records.has(element)) records.set(element, { value: null, run: null, generation: 0, listeners: new Set() });
  return records.get(element);
}

export function readAppliedScreenContext(element) {
  return element ? structuredClone(recordFor(element).value) : null;
}

export function subscribeAppliedScreenContext(element, callback) {
  const record = recordFor(element);
  record.listeners.add(callback);
  return () => record.listeners.delete(callback);
}

export function clearAppliedScreenContext(element) {
  if (!element) return;
  const record = recordFor(element);
  record.value = null;
  record.generation += 1;
  publishContext(element, record);
}

export function resolveAppliedScreenRun(element, run) {
  if (comparisonRunKey(run)) return run;
  const retained = element && recordFor(element).run;
  return retained?.id === run?.id ? structuredClone(retained) : null;
}

export function commitStandardScreenContext(element, payload, query) {
  commitContext(element, payload.run, payload.total, { kind: "standard", label: "已应用的普通榜单筛选" }, () => {
    if (["active", "ready"].includes(payload.production_ranking?.status) || payload.production_ranking?.score_rule_version === "full-market-score-v6" || payload.ranking_policy === "v6" || payload.items.some((item) => item.production_score_rule_version === "full-market-score-v6")) {
      throw new Error("当前生产榜单使用概率调整排名，暂不支持原始冻结条件解释");
    }
    return screenSpecFromResultsQuery(query, payload.run.id);
  });
}

export function commitDiscoveryScreenContext(element, run, payload, expectedPreset = payload.preset) {
  const preset = payload.preset;
  const source = { kind: "preset", id: preset.id, revision: preset.revision, label: `已应用方案“${preset.name}” · 修订 ${preset.revision}` };
  commitContext(element, resolveAppliedScreenRun(element, run), payload.total, source, () => {
    if (payload.run_id !== run?.id || payload.rule_version !== resolveAppliedScreenRun(element, run)?.rule_version) throw new Error("方案响应与冻结批次不一致");
    const spec = screenSpecFromPreset(preset);
    if (preset.id !== expectedPreset.id || preset.revision !== expectedPreset.revision || normalizedScreenSpecKey(spec) !== normalizedScreenSpecKey(screenSpecFromPreset(expectedPreset))) throw new Error("方案响应与本次提交的方案条件不一致");
    return spec;
  });
}

function commitContext(element, run, total, source, createSpec) {
  if (!element) return;
  const record = recordFor(element);
  if (!comparisonRunKey(run)) return clearAppliedScreenContext(element);
  record.run = structuredClone(run);
  let spec = null, error = null;
  try { spec = createSpec(); } catch (failure) { error = String(failure?.message || "无法解释已应用条件"); }
  const identity = JSON.stringify([comparisonRunKey(run), source.kind, source.id, source.revision, spec && normalizedScreenSpecKey(spec), error]);
  if (record.value?.identity === identity && record.value.total === total) return;
  record.generation += 1;
  record.value = { run: structuredClone(run), spec, source, total, error, identity, generation: record.generation };
  publishContext(element, record);
}

function publishContext(element, record) {
  const button = element.ownerDocument?.getElementById?.("marketScanExplainEmpty");
  if (button) {
    button.hidden = record.value?.total !== 0;
    button.disabled = !record.value?.spec;
  }
  const unavailable = element.ownerDocument?.getElementById?.("marketScanExplainUnavailable");
  if (unavailable) {
    unavailable.hidden = !(record.value?.total === 0 && record.value?.error);
    unavailable.textContent = record.value?.error || "";
  }
  for (const listener of record.listeners) listener();
}

export function screenContextMatches(element, captured) {
  const current = element && recordFor(element).value;
  return Boolean(current && captured && current.identity === captured.identity && current.generation === captured.generation);
}

export function requireScreenEvidenceBinding(evidence, captured, spec = undefined, delta = false) {
  const fields = delta ? EVIDENCE_FIELDS.filter((key) => key !== "quote_date") : EVIDENCE_FIELDS;
  if (!evidence || evidence.run_id !== captured.run.id || !fields.every((key) => evidence[key] === captured.run[key])) {
    throw new Error("筛选证据与已应用榜单的完整冻结身份不一致");
  }
  if (spec !== undefined && normalizedScreenSpecKey(spec) !== normalizedScreenSpecKey(captured.spec)) throw new Error("筛选响应与已应用的条件不一致");
}
