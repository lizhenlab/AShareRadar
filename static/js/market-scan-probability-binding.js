export function normalizeProbabilityRunBinding(value, expectedRunId, requireObject, contractError, requirePositiveInteger) {
  if (value === null || value === undefined) return legacyProbabilityRunBinding(expectedRunId);
  const raw = requireObject(value, "probability_research.run_binding");
  const runId = requirePositiveInteger(raw.run_id, "probability_research.run_binding.run_id");
  if (runId !== expectedRunId) throw contractError("probability_research.run_binding.run_id 与请求批次不匹配");
  const status = String(raw.binding_status || "");
  if (!new Set(["verified", "legacy_unbound"]).has(status) || typeof raw.legacy !== "boolean") {
    throw contractError("probability_research.run_binding 状态无效");
  }
  if ((status === "verified") === raw.legacy) {
    throw contractError("probability_research.run_binding status/legacy 冲突");
  }
  if (status === "verified") validateVerifiedProbabilityBinding(raw, requireObject, contractError);
  return { ...raw, run_id: runId, binding_status: status };
}

function validateVerifiedProbabilityBinding(raw, requireObject, contractError) {
  const sha256 = /^[0-9a-f]{64}$/;
  const cohort = requireObject(raw.cohort_contract, "probability_research.run_binding.cohort_contract");
  const text = (value) => typeof value === "string" && value.trim() === value && value.length > 0;
  if (raw.mode !== "official" || !text(raw.scope) || !text(raw.rule_version)) {
    throw contractError("verified run_binding 缺少 official 全市场批次合同");
  }
  if (raw.quote_date !== raw.data_date || !/^\d{4}-\d{2}-\d{2}$/.test(String(raw.quote_date || ""))) {
    throw contractError("verified run_binding 行情日期合同无效");
  }
  if (!sha256.test(String(raw.scan_rule_hash || "")) || !text(raw.production_score_rule_version)
      || !sha256.test(String(raw.production_score_spec_hash || ""))) {
    throw contractError("verified run_binding 缺少独立的批次与生产评分合同哈希");
  }
  if (cohort.mode !== raw.mode || cohort.scope !== raw.scope || cohort.rule_version !== raw.rule_version) {
    throw contractError("verified run_binding cohort_contract 冲突");
  }
}

export function legacyProbabilityRunBinding(runId) {
  return { binding_status: "legacy_unbound", legacy: true, run_id: runId };
}

export function probabilityBindingLimitations(limitations, binding) {
  return binding.binding_status === "verified" && binding.legacy === false
    ? limitations
    : Array.from(new Set([...limitations, "legacy_run_binding_not_selection_eligible"]));
}
