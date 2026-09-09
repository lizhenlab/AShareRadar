import { buildDiscoveryPresetDefinition, isDiscoveryPresetUiRepresentable, validateDiscoveryPreset } from "./market-scan-contracts.js";

export function discoveryDefinitionKey(value) {
  return JSON.stringify(canonicalValue({
    name: value.name, criteria: value.criteria, sort: value.sort,
    column_view: value.column_view || "overview",
  }));
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().filter(key => value[key] != null)
    .map(key => [key, canonicalValue(value[key])]));
}

export function discoveryEditorKey(elements) {
  try {
    return discoveryDefinitionKey(buildDiscoveryPresetDefinition(elements.name.value, elements));
  } catch {
    return null;
  }
}

export function validateDiscoveryWriteReceipt(value, definition, previous, knownIds = []) {
  const preset = validateDiscoveryPreset(value);
  const identityMatches = previous
    ? preset.id === previous.id && preset.revision === previous.revision + 1
    : preset.revision === 1 && !knownIds.includes(preset.id);
  if (!identityMatches || discoveryDefinitionKey(preset) !== discoveryDefinitionKey(definition)) {
    throw new Error("保存回执与提交的方案身份、修订或定义不一致，请刷新方案列表并重新选择核对");
  }
  return preset;
}

export function validateDiscoveryDeleteReceipt(value, presetId) {
  if (value?.deleted !== true || value.preset_id !== presetId) {
    throw new Error("删除回执与请求的方案身份不一致，请刷新方案列表核对");
  }
  return value;
}

export function unknownDiscoveryWriteResult(error) {
  const status = Number(error?.status);
  return !Number.isInteger(status) || status < 400 || status >= 500 || status === 408;
}

export function discoveryPresetPageText(state) {
  return `方案第 ${state.pageCount ? state.page : 0} / ${state.pageCount} 页 · 当前页 ${state.presets.length} 项 / 共 ${state.total} 项`;
}

export function discoveryPresetSelectionText(state) {
  if (state.writeUnconfirmed) return "保存结果待核对：请刷新方案列表并重新选择具体方案核对，当前页面不会直接重试保存。";
  const preset = state.selected;
  if (!preset) return "另存会新建独立方案；更新只修改当前选中方案。";
  const location = state.presets.some(item => item.id === preset.id) ? "当前页" : "跨页保留，不在当前页";
  const editing = isDiscoveryPresetUiRepresentable(preset) ? "另存会新建副本。"
    : "兼容方案不能通过表单另存或更新；请先取消选择后自建条件，或导出原定义。";
  return `已选 #${preset.id} · 修订 ${preset.revision} · ${preset.name}（${location}）。应用及变化记录使用已保存定义；${editing}`;
}
