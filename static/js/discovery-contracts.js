import { validateUiSymbol } from "./symbols.js";

export function rankChangeLabel(value) {
  const movement = value?.movement;
  if (movement === "up") return `全市场排名上升 ${Math.abs(Number(value.rank_delta) || 0)}`;
  if (movement === "down") return `全市场排名下降 ${Math.abs(Number(value.rank_delta) || 0)}`;
  if (movement === "unchanged") return "全市场排名持平";
  if (movement === "new") return "全市场排名新进";
  if (movement === "exit") return "全市场排名离榜";
  return "全市场排名变化不可用";
}

export function normalizeDiscoveryLeaderboard(value) {
  const payload = objectValue(value, "筛选榜单响应");
  const preset = validateDiscoveryPreset(payload.preset);
  const runId = positiveInteger(payload.run_id, "筛选榜单运行批次");
  const items = arrayValue(payload.items, "筛选榜单响应.items").map((item, index) => (
    leaderboardItem(item, runId, `筛选榜单响应.items[${index}]`)
  ));
  const page = pageValues(payload, items, "筛选榜单响应");
  unique(items, (item) => item.symbol, "筛选榜单股票");
  unique(items, (item) => item.position, "筛选榜单位置");
  const offset = (page.page - 1) * page.page_size;
  if (items.some((item, index) => item.position !== offset + index + 1)) throw new Error("筛选榜单位置与当前分页不一致");
  return { preset, run_id: runId, rule_version: requiredString(payload.rule_version, "筛选榜单规则版本"), items, ...page };
}

export function validateDiscoveryPresetPage(value) {
  const payload = objectValue(value, "筛选方案列表响应");
  const items = arrayValue(payload.items, "筛选方案列表响应.items").map(validateDiscoveryPreset);
  unique(items, (item) => item.id, "筛选方案 id");
  return { ...payload, items, ...pageValues(payload, items, "筛选方案列表响应") };
}

export function validateDiscoveryPreset(value) {
  const preset = objectValue(value, "筛选方案响应");
  const name = requiredString(preset.name, "筛选方案名称").trim().slice(0, 80);
  objectValue(preset.criteria, "筛选方案条件");
  if (!Array.isArray(preset.sort) || !preset.sort.length) throw new Error("筛选方案响应缺少排序规则");
  return { ...preset, id: positiveInteger(preset.id, "筛选方案 ID"), name, revision: positiveInteger(preset.revision, "筛选方案修订号") };
}

export function validateDiscoveryRankChanges(value, runId) {
  const payload = objectValue(value, "排名变化响应");
  if (positiveInteger(payload.current_run_id, "当前排名批次") !== runId) throw new Error("排名变化响应的运行批次不匹配");
  if (typeof payload.comparable !== "boolean") throw new Error("排名变化响应 comparable 格式异常");
  const previousRunId = nullablePositiveInteger(payload.previous_run_id, "上一排名批次");
  const currentRule = requiredString(payload.current_rule_version, "当前排名规则版本");
  const previousRule = nullableRequiredString(payload.previous_rule_version, "上一排名规则版本");
  comparisonState(payload, previousRunId, currentRule, previousRule);
  const items = arrayValue(payload.items, "排名变化响应.items").map(rankChangeItem);
  unique(items, (item) => item.symbol, "排名变化股票");
  return { ...payload, previous_run_id: previousRunId, current_rule_version: currentRule, previous_rule_version: previousRule, items, ...pageValues(payload, items, "排名变化响应") };
}

export function validateDiscoveryResearchQueueResponse(value, expected) {
  const payload = objectValue(value, "研究队列响应");
  const owner = objectValue(expected, "研究队列请求身份");
  const ids = [positiveInteger(owner.runId, "请求批次"), positiveInteger(owner.presetId, "请求方案"), positiveInteger(owner.presetRevision, "请求修订")];
  const symbols = arrayValue(owner.symbols, "请求股票").map((symbol) => validSymbol(symbol, "请求股票"));
  if (!symbols.length || new Set(symbols).size !== symbols.length) throw new Error("研究队列请求股票不能为空或重复");
  const expectedSymbols = new Set(symbols);
  const items = arrayValue(payload.items, "研究队列响应.items").map((raw) => {
    const item = objectValue(raw, "研究队列项目");
    const symbol = validSymbol(item.symbol, "研究队列项目股票");
    if (!expectedSymbols.has(symbol)) throw new Error("研究队列响应含请求外股票");
    const actualIds = [positiveInteger(item.source_run_id, "来源批次"), positiveInteger(item.source_preset_id, "来源方案"), positiveInteger(item.source_preset_revision, "来源修订")];
    if (actualIds.some((id, index) => id !== ids[index])) throw new Error("研究队列响应身份不匹配");
    if (typeof item.added !== "boolean") throw new Error("研究队列项目 added 格式异常");
    requiredString(item.source_preset_name, "来源方案名称");
    requiredString(item.enqueued_at, "入队时间");
    return { ...item, symbol };
  });
  unique(items, (item) => item.symbol, "研究队列股票");
  if (items.length !== expectedSymbols.size) throw new Error("研究队列响应未完整覆盖请求股票");
  const added = nonNegativeInteger(payload.added_count, "新增数");
  const existing = nonNegativeInteger(payload.existing_count, "已存在数");
  const actualAdded = items.filter((item) => item.added).length;
  if (added !== actualAdded || existing !== items.length - actualAdded) throw new Error("研究队列响应计数不一致");
  return { ...payload, items, added_count: added, existing_count: existing };
}

function leaderboardItem(value, runId, context) {
  const item = objectValue(value, context);
  const symbol = validSymbol(item.symbol, `${context}.symbol`);
  const code = requiredString(item.code, `${context}.code`);
  const market = requiredString(item.market, `${context}.market`);
  if (symbol !== `${code}.${market}`) throw new Error(`${context} 的 symbol/code/market 不一致`);
  if (typeof item.is_st !== "boolean" || typeof item.is_new !== "boolean") throw new Error(`${context} 的 is_st/is_new 格式异常`);
  const position = positiveInteger(item.position, `${context}.position`);
  const quality = boundedInteger(item.quality, `${context}.quality`, 0, 100);
  const trend = boundedInteger(item.trend, `${context}.trend`, 0, 100);
  const change = finiteNumber(item.change, `${context}.change`, -1000, 1000);
  return { ...item, position, source_rank: positiveInteger(item.source_rank, `${context}.source_rank`), symbol, code, market, name: requiredString(item.name, `${context}.name`), industry: nullableString(item.industry, `${context}.industry`), quality, trend, score: boundedInteger(item.score, `${context}.score`, 0, 100), raw_score: finiteNumber(item.raw_score, `${context}.raw_score`, 0, 100), change, turnover: nullableNumber(item.turnover, `${context}.turnover`, 0), amount: finiteNumber(item.amount, `${context}.amount`, 0), run_id: runId, rank: position, status: "success", trend_score: trend, change_pct: change, turnover_rate: item.turnover ?? null, data_quality_score: quality };
}

function rankChangeItem(value, index) {
  const item = objectValue(value, `排名变化响应.items[${index}]`);
  const symbol = validSymbol(item.symbol, "排名变化股票");
  const code = requiredString(item.code, "排名变化代码");
  const market = requiredString(item.market, "排名变化市场");
  if (symbol !== `${code}.${market}`) throw new Error("排名变化 symbol/code/market 不一致");
  const previous = nullablePositiveInteger(item.previous_rank, "上一排名");
  const current = nullablePositiveInteger(item.current_rank, "当前排名");
  const delta = item.rank_delta === null ? null : integer(item.rank_delta, "排名差");
  const movement = item.movement;
  if (!["up", "down", "unchanged", "new", "exit", "unavailable"].includes(movement)) throw new Error("排名变化 movement 不受支持");
  validateRankMovement(movement, previous, current, delta);
  return { ...item, symbol, code, market, name: requiredString(item.name, "排名变化名称"), previous_rank: previous, current_rank: current, rank_delta: delta };
}

function validateRankMovement(movement, previous, current, delta) {
  if (["up", "down", "unchanged"].includes(movement)) {
    if (previous === null || current === null || delta !== previous - current) throw new Error("排名差与前后排名不一致");
    if ((movement === "up" && delta <= 0) || (movement === "down" && delta >= 0) || (movement === "unchanged" && delta !== 0)) throw new Error("movement 与排名差不一致");
  } else if (delta !== null) throw new Error("不可比较项目不能声明排名差");
  if (movement === "new" && (previous !== null || current === null)) throw new Error("新进状态与排名不一致");
  if (movement === "exit" && (previous === null || current !== null)) throw new Error("离榜状态与排名不一致");
}

function comparisonState(payload, previousId, currentRule, previousRule) {
  if (payload.comparable) {
    if (previousId === null || payload.reason !== null || previousRule !== currentRule) throw new Error("可比较状态与批次/规则不一致");
  } else if (!["no_previous_run", "rule_version_mismatch"].includes(payload.reason) || payload.items.length || payload.total !== 0 || payload.page_count !== 0) throw new Error("不可比较状态与内容不一致");
  else if (payload.reason === "no_previous_run" && (previousId !== null || previousRule !== null)) throw new Error("无上一批次状态不能声明上一批次");
  else if (payload.reason === "rule_version_mismatch" && (previousId === null || previousRule === null || previousRule === currentRule)) throw new Error("规则不一致状态缺少不同规则的上一批次");
}

function pageValues(payload, items, context) {
  const total = nonNegativeInteger(payload.total, `${context}.total`);
  const page = positiveInteger(payload.page, `${context}.page`);
  const pageSize = positiveInteger(payload.page_size, `${context}.page_size`);
  const pageCount = nonNegativeInteger(payload.page_count, `${context}.page_count`);
  const expectedItems = page > pageCount ? 0 : Math.min(pageSize, total - ((page - 1) * pageSize));
  if (pageCount !== (total ? Math.ceil(total / pageSize) : 0) || items.length !== expectedItems) throw new Error(`${context} 分页信息不一致`);
  return { total, page, page_size: pageSize, page_count: pageCount };
}

function objectValue(value, label) { if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}格式异常`); return value; }
function arrayValue(value, label) { if (!Array.isArray(value)) throw new Error(`${label}格式异常`); return value; }
function requiredString(value, label) { if (typeof value !== "string" || !value.trim()) throw new Error(`${label}格式异常`); return value; }
function nullableRequiredString(value, label) { return value == null ? null : requiredString(value, label); }
function nullableString(value, label) { if (value == null) return null; if (typeof value !== "string") throw new Error(`${label}格式异常`); return value; }
function integer(value, label) { if (!Number.isInteger(value)) throw new Error(`${label}格式异常`); return value; }
function positiveInteger(value, label) { const result = integer(value, label); if (result < 1) throw new Error(`${label}格式异常`); return result; }
function nullablePositiveInteger(value, label) { return value == null ? null : positiveInteger(value, label); }
function nonNegativeInteger(value, label) { const result = integer(value, label); if (result < 0) throw new Error(`${label}格式异常`); return result; }
function boundedInteger(value, label, min, max) { const result = integer(value, label); if (result < min || result > max) throw new Error(`${label}格式异常`); return result; }
function finiteNumber(value, label, min, max) { if (typeof value !== "number" || !Number.isFinite(value) || value < min || (max !== undefined && value > max)) throw new Error(`${label}格式异常`); return value; }
function nullableNumber(value, label, min, max) { if (value == null) return null; if (typeof value !== "number" || !Number.isFinite(value) || (min !== undefined && value < min) || (max !== undefined && value > max)) throw new Error(`${label}格式异常`); return value; }
function validSymbol(value, label) { const symbol = requiredString(value, label); if (validateUiSymbol(symbol) !== symbol) throw new Error(`${label}格式异常`); return symbol; }
function unique(items, selector, label) { const keys = items.map(selector); if (new Set(keys).size !== keys.length) throw new Error(`${label}不能重复`); }
