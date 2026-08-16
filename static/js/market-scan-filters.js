import { selectedMarketScanProbabilityHorizon } from "./market-scan-probability-view.js";

const MARKET_SCAN_TO_DISCOVERY_SORT = Object.freeze({
  rank: "rank",
  symbol: "symbol",
  score: "score",
  raw_score: "raw_score",
  trend_score: "trend",
  change_pct: "change",
  turnover_rate: "turnover",
  amount: "amount",
  data_quality_score: "quality",
});

const DISCOVERY_TO_MARKET_SCAN_SORT = Object.freeze(Object.fromEntries(
  Object.entries(MARKET_SCAN_TO_DISCOVERY_SORT).map(([market, discovery]) => [discovery, market])
));
const MARKET_SCAN_RESEARCH_SORTS = Object.freeze(new Set(["alpha_5d", "confidence", "risk", "tradability"]));
const MARKET_SCAN_SORTS = Object.freeze(new Set([
  ...Object.keys(MARKET_SCAN_TO_DISCOVERY_SORT),
  ...MARKET_SCAN_RESEARCH_SORTS,
]));
const DISCOVERY_COLUMN_VIEWS = Object.freeze(new Set(["overview", "trend", "liquidity", "risk", "research"]));

const RANGE_FIELDS = Object.freeze([
  ["score", "scoreMin", "scoreMax", "min_score", "max_score", 0, 100, "趋势强度"],
  ["trend", "trendMin", "trendMax", "min_trend_score", "max_trend_score", 0, 100, "趋势分"],
  ["change", "changeMin", "changeMax", "min_change_pct", "max_change_pct", -1000, 1000, "涨跌幅"],
  ["turnover", "turnoverMin", "turnoverMax", "min_turnover_rate", "max_turnover_rate", 0, 10000, "换手率"],
  ["amount", "amountMin", "amountMax", "min_amount", "max_amount", 0, 1e15, "成交额"],
  ["quality", "quality", "qualityMax", "min_data_quality_score", "max_data_quality_score", 0, 100, "数据质量"],
]);

export function marketScanFilterElements(root, requireElement) {
  const get = (id) => requireElement(root, id);
  const sort = get("marketScanSort");
  const sort2 = get("marketScanSort2");
  const sort3 = get("marketScanSort3");
  const order = get("marketScanOrder");
  const order2 = get("marketScanOrder2");
  const order3 = get("marketScanOrder3");
  return {
    filters: get("marketScanFilters"),
    status: get("marketScanStatus"),
    market: get("marketScanMarket"),
    industry: get("marketScanIndustry"),
    isSt: get("marketScanSt"),
    isNew: get("marketScanNew"),
    scoreMin: get("marketScanScoreMin"),
    scoreMax: get("marketScanScoreMax"),
    trendMin: get("marketScanTrendMin"),
    trendMax: get("marketScanTrendMax"),
    changeMin: get("marketScanChangeMin"),
    changeMax: get("marketScanChangeMax"),
    turnoverMin: get("marketScanTurnoverMin"),
    turnoverMax: get("marketScanTurnoverMax"),
    amountMin: get("marketScanAmountMin"),
    amountMax: get("marketScanAmountMax"),
    quality: get("marketScanQuality"),
    qualityMax: get("marketScanQualityMax"),
    confidenceMin: get("marketScanConfidenceMin"),
    riskMax: get("marketScanRiskMax"),
    tradabilityMin: get("marketScanTradabilityMin"),
    probabilityMin: get("marketScanProbabilityMin"),
    keyword: get("marketScanKeyword"),
    table: get("marketScanTable"),
    columnViews: [
      get("marketScanColumnOverview"), get("marketScanColumnTrend"), get("marketScanColumnLiquidity"),
      get("marketScanColumnRisk"), get("marketScanColumnResearch"),
    ],
    sort,
    sort2,
    sort3,
    sorts: [sort, sort2, sort3],
    order,
    order2,
    order3,
    orders: [order, order2, order3],
  };
}

export function marketScanQueryParams(elements, initial = {}) {
  const filters = readMarketScanFilters(elements);
  const params = new URLSearchParams(initial);
  params.set("status", filters.status);
  appendValues(params, "market", filters.markets);
  appendValues(params, "industry", filters.industries);
  setOptional(params, "is_st", filters.isSt);
  setOptional(params, "is_new", filters.isNew);
  for (const definition of RANGE_FIELDS) {
    const [field, _minElement, _maxElement, minQuery, maxQuery] = definition;
    setOptional(params, minQuery, filters.ranges[field]?.min);
    setOptional(params, maxQuery, filters.ranges[field]?.max);
  }
  setOptional(params, "min_confidence", filters.research.confidenceMin);
  setOptional(params, "max_risk", filters.research.riskMax);
  setOptional(params, "min_tradability", filters.research.tradabilityMin);
  if (filters.research.probabilityMin !== null) {
    params.set("probability_horizon", String(selectedMarketScanProbabilityHorizon(elements)));
    params.set("min_upside_probability", String(Number((filters.research.probabilityMin / 100).toFixed(6))));
  }
  setOptional(params, "keyword", filters.keyword);
  appendValues(params, "sort", filters.sort.map((item) => item.field));
  appendValues(params, "order", filters.sort.map((item) => item.order));
  return params;
}

export function buildDiscoveryPresetDefinition(nameValue, elements) {
  const name = String(nameValue || "").trim().slice(0, 80);
  if (!name) throw new Error("请输入方案名称");
  const filters = readMarketScanFilters(elements);
  const unsupported = [];
  if (filters.status !== "success") unsupported.push("状态");
  if (unsupported.length) throw new Error(`筛选方案暂不支持保存${unsupported.join("、")}，请清除后再保存`);
  if (filters.research.probabilityMin !== null) throw new Error("筛选方案暂不支持保存上涨概率条件，请清除后再保存");
  if (filters.sort.some((item) => !MARKET_SCAN_TO_DISCOVERY_SORT[item.field])) {
    throw new Error("筛选方案暂不支持保存研究维度排序，请改用生产榜单字段");
  }
  const criteria = {};
  if (filters.markets.length) criteria.market = filters.markets;
  if (filters.industries.length) criteria.industry = filters.industries;
  if (filters.isSt !== null) criteria.is_st = filters.isSt;
  if (filters.isNew !== null) criteria.is_new = filters.isNew;
  for (const [field] of RANGE_FIELDS) {
    if (filters.ranges[field]) criteria[field] = filters.ranges[field];
  }
  if (filters.research.confidenceMin !== null) criteria.confidence = { min: filters.research.confidenceMin };
  if (filters.research.riskMax !== null) criteria.risk = { max: filters.research.riskMax };
  if (filters.research.tradabilityMin !== null) criteria.tradability = { min: filters.research.tradabilityMin };
  if (filters.keyword) criteria.keyword = filters.keyword;
  return {
    name,
    criteria,
    sort: filters.sort.map(({ field, order }) => ({
      field: MARKET_SCAN_TO_DISCOVERY_SORT[field],
      order,
    })),
    column_view: selectedColumnView(elements),
  };
}

export function isDiscoveryPresetUiRepresentable(preset) {
  const criteria = preset?.criteria;
  if (!criteria || typeof criteria !== "object" || Array.isArray(criteria)) return false;
  const supported = new Set([
    "market", "industry", "is_st", "is_new", "confidence", "risk", "tradability", "keyword",
    ...RANGE_FIELDS.map(([field]) => field),
  ]);
  if (Object.entries(criteria).some(([field, value]) => value != null && !supported.has(field))) return false;
  if (!validList(criteria.market, 3) || !validList(criteria.industry, 20)) return false;
  for (const [field, , , , , minimum, maximum] of RANGE_FIELDS) {
    if (!validRange(criteria[field], minimum, maximum)) return false;
  }
  for (const field of ["confidence", "risk", "tradability"]) {
    if (!validRange(criteria[field], 0, 100)) return false;
  }
  if (criteria.keyword != null && (typeof criteria.keyword !== "string" || !criteria.keyword.trim() || criteria.keyword.length > 80)) return false;
  return validPresetSort(preset.sort) && DISCOVERY_COLUMN_VIEWS.has(preset.column_view || "overview");
}

export function applyDiscoveryPresetFields(preset, elements) {
  const criteria = preset?.criteria || {};
  applyDiscoveryCriteria(criteria, elements);
  applyDiscoverySort(preset?.sort, elements);
  applyColumnView(elements, preset?.column_view || "overview");
}

function applyDiscoveryCriteria(criteria, elements) {
  setElementValue(elements.status, "success");
  setElementValue(elements.keyword, criteria.keyword || "");
  setMarketValues(elements.market, Array.isArray(criteria.market) ? criteria.market : []);
  setElementValue(elements.industry, Array.isArray(criteria.industry) ? criteria.industry.join("，") : "");
  applyDiscoveryFlags(criteria, elements);
  setElementValue(elements.confidenceMin, criteria.confidence?.min ?? "");
  setElementValue(elements.riskMax, criteria.risk?.max ?? "");
  setElementValue(elements.tradabilityMin, criteria.tradability?.min ?? "");
  setElementValue(elements.probabilityMin, "");
  for (const [field, minElement, maxElement] of RANGE_FIELDS) {
    setElementValue(elements[minElement], criteria[field]?.min ?? "");
    setElementValue(elements[maxElement], criteria[field]?.max ?? "");
  }
}

function applyDiscoveryFlags(criteria, elements) {
  setElementValue(elements.isSt, booleanText(criteria.is_st));
  setElementValue(elements.isNew, booleanText(criteria.is_new));
}

function booleanText(value) {
  return typeof value === "boolean" ? String(value) : "";
}

function applyDiscoverySort(sort, elements) {
  const sorts = filterSortElements(elements);
  sorts.forEach(({ fieldElement, orderElement }, index) => {
    const item = sort?.[index];
    setElementValue(fieldElement, item ? DISCOVERY_TO_MARKET_SCAN_SORT[item.field] : "");
    setElementValue(orderElement, item?.order || (index === 0 ? "asc" : "desc"));
  });
}

export function readMarketScanFilters(elements) {
  const ranges = {};
  for (const [field, minElement, maxElement, , , minimum, maximum, label] of RANGE_FIELDS) {
    const range = readRange(elements[minElement], elements[maxElement], minimum, maximum, label);
    if (range) ranges[field] = range;
  }
  const sort = filterSortElements(elements)
    .map(({ fieldElement, orderElement }, index) => ({
      field: elementValue(fieldElement) || (index === 0 ? "rank" : ""),
      order: elementValue(orderElement) === "desc" ? "desc" : "asc",
    }))
    .filter((item) => item.field);
  if (!sort.length) sort.push({ field: "rank", order: "asc" });
  if (new Set(sort.map((item) => item.field)).size !== sort.length) throw new Error("排序字段不能重复");
  if (sort.some((item) => !MARKET_SCAN_SORTS.has(item.field))) throw new Error("排序字段无效");
  const research = {
    confidenceMin: boundedOptionalNumber(elements.confidenceMin, 0, 100, "最低置信度"),
    riskMax: boundedOptionalNumber(elements.riskMax, 0, 100, "最高风险分"),
    tradabilityMin: boundedOptionalNumber(elements.tradabilityMin, 0, 100, "最低可交易性"),
    probabilityMin: elements.probabilityMin?.disabled
      ? null
      : boundedOptionalNumber(elements.probabilityMin, 0, 100, "最低上涨概率"),
  };
  return {
    status: elementValue(elements.status) || "success",
    markets: marketValues(elements.market),
    industries: splitValues(elementValue(elements.industry), 20),
    isSt: booleanValue(elements.isSt),
    isNew: booleanValue(elements.isNew),
    ranges,
    research,
    keyword: elementValue(elements.keyword),
    sort,
  };
}

function filterSortElements(elements) {
  if (Array.isArray(elements.sorts) && elements.sorts.length) {
    return elements.sorts.slice(0, 3).map((fieldElement, index) => ({
      fieldElement,
      orderElement: elements.orders?.[index],
    }));
  }
  return [
    { fieldElement: elements.sort, orderElement: elements.order },
    { fieldElement: elements.sort2, orderElement: elements.order2 },
    { fieldElement: elements.sort3, orderElement: elements.order3 },
  ];
}

function selectedColumnView(elements) {
  const selected = elements.columnViews?.find((input) => input.checked)?.value || elements.table?.dataset?.columnView || "overview";
  return DISCOVERY_COLUMN_VIEWS.has(selected) ? selected : "overview";
}

function applyColumnView(elements, value) {
  const selected = DISCOVERY_COLUMN_VIEWS.has(value) ? value : "overview";
  elements.columnViews?.forEach((input) => { input.checked = input.value === selected; });
  if (elements.table?.dataset) elements.table.dataset.columnView = selected;
  const wrap = elements.table?.closest?.(".market-scan-table-wrap");
  wrap?.setAttribute?.("aria-label", `全市场扫描榜单，${columnViewLabel(selected)}列视图`);
}

function columnViewLabel(value) {
  return ({ overview: "概览", trend: "趋势", liquidity: "流动性", risk: "风险", research: "研究" })[value] || "概览";
}

function readRange(minElement, maxElement, minimum, maximum, label) {
  const lower = optionalNumber(minElement, label);
  const upper = optionalNumber(maxElement, label);
  if (lower === null && upper === null) return null;
  if (lower !== null && (lower < minimum || lower > maximum)) throw new Error(`${label}下限超出允许范围`);
  if (upper !== null && (upper < minimum || upper > maximum)) throw new Error(`${label}上限超出允许范围`);
  if (lower !== null && upper !== null && lower > upper) throw new Error(`${label}范围下限不能大于上限`);
  return {
    ...(lower === null ? {} : { min: lower }),
    ...(upper === null ? {} : { max: upper }),
  };
}

function optionalNumber(element, label) {
  const value = elementValue(element);
  if (!value) return null;
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`${label}必须是有效数字`);
  return number;
}

function boundedOptionalNumber(element, minimum, maximum, label) {
  const value = optionalNumber(element, label);
  if (value !== null && (value < minimum || value > maximum)) {
    throw new Error(`${label}超出允许范围`);
  }
  return value;
}

function marketValues(element) {
  const selected = Array.from(element?.selectedOptions || []).map((option) => option.value);
  return uniqueValues(selected.length ? selected : splitValues(elementValue(element), 3), 3);
}

function setMarketValues(element, values) {
  const selected = new Set(values);
  if (element?.options) {
    Array.from(element.options).forEach((option) => { option.selected = selected.has(option.value); });
  }
  setElementValue(element, values[0] || "");
}

function splitValues(value, maximum) {
  return uniqueValues(String(value || "").split(/[,，、;；\n]+/), maximum);
}

function uniqueValues(values, maximum) {
  return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))].slice(0, maximum);
}

function booleanValue(element) {
  const value = elementValue(element);
  return value === "true" ? true : value === "false" ? false : null;
}

function elementValue(element) {
  return String(element?.value ?? "").trim();
}

function setElementValue(element, value) {
  if (element) element.value = String(value ?? "");
}

function appendValues(params, key, values) {
  params.delete(key);
  values.forEach((value) => params.append(key, String(value)));
}

function setOptional(params, key, value) {
  params.delete(key);
  if (value !== null && value !== undefined && String(value).trim() !== "") params.set(key, String(value));
}

function validList(value, maximum) {
  return value == null || (Array.isArray(value) && value.length >= 1 && value.length <= maximum
    && value.every((item) => typeof item === "string" && item.trim()));
}

function validRange(value, minimum, maximum) {
  if (value == null) return true;
  if (typeof value !== "object" || Array.isArray(value)) return false;
  if (Object.keys(value).some((key) => key !== "min" && key !== "max")) return false;
  const lower = value.min;
  const upper = value.max;
  if (lower == null && upper == null) return false;
  if ([lower, upper].some((item) => item != null && (!Number.isFinite(Number(item)) || item < minimum || item > maximum))) return false;
  return lower == null || upper == null || lower <= upper;
}

function validPresetSort(sort) {
  if (!Array.isArray(sort) || sort.length < 1 || sort.length > 3) return false;
  if (new Set(sort.map((item) => item?.field)).size !== sort.length) return false;
  return sort.every((item) => Boolean(DISCOVERY_TO_MARKET_SCAN_SORT[item?.field])
    && (item?.order === "asc" || item?.order === "desc"));
}
