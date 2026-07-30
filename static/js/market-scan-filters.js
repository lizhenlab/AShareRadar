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

const RANGE_FIELDS = Object.freeze([
  ["score", "scoreMin", "scoreMax", "min_score", "max_score", 0, 100, "强势分"],
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
    keyword: get("marketScanKeyword"),
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
  if (filters.keyword) unsupported.push("搜索关键词");
  if (unsupported.length) throw new Error(`筛选方案暂不支持保存${unsupported.join("、")}，请清除后再保存`);
  const criteria = {};
  if (filters.markets.length) criteria.market = filters.markets;
  if (filters.industries.length) criteria.industry = filters.industries;
  if (filters.isSt !== null) criteria.is_st = filters.isSt;
  if (filters.isNew !== null) criteria.is_new = filters.isNew;
  for (const [field] of RANGE_FIELDS) {
    if (filters.ranges[field]) criteria[field] = filters.ranges[field];
  }
  return {
    name,
    criteria,
    sort: filters.sort.map(({ field, order }) => ({
      field: MARKET_SCAN_TO_DISCOVERY_SORT[field],
      order,
    })),
  };
}

export function isDiscoveryPresetUiRepresentable(preset) {
  const criteria = preset?.criteria;
  if (!criteria || typeof criteria !== "object" || Array.isArray(criteria)) return false;
  const supported = new Set(["market", "industry", "is_st", "is_new", ...RANGE_FIELDS.map(([field]) => field)]);
  if (Object.entries(criteria).some(([field, value]) => value != null && !supported.has(field))) return false;
  if (!validList(criteria.market, 3) || !validList(criteria.industry, 20)) return false;
  for (const [field, , , , , minimum, maximum] of RANGE_FIELDS) {
    if (!validRange(criteria[field], minimum, maximum)) return false;
  }
  return validPresetSort(preset.sort);
}

export function applyDiscoveryPresetFields(preset, elements) {
  const criteria = preset?.criteria || {};
  setElementValue(elements.status, "success");
  setElementValue(elements.keyword, "");
  setMarketValues(elements.market, Array.isArray(criteria.market) ? criteria.market : []);
  setElementValue(elements.industry, Array.isArray(criteria.industry) ? criteria.industry.join("，") : "");
  setElementValue(elements.isSt, typeof criteria.is_st === "boolean" ? String(criteria.is_st) : "");
  setElementValue(elements.isNew, typeof criteria.is_new === "boolean" ? String(criteria.is_new) : "");
  for (const [field, minElement, maxElement] of RANGE_FIELDS) {
    setElementValue(elements[minElement], criteria[field]?.min ?? "");
    setElementValue(elements[maxElement], criteria[field]?.max ?? "");
  }
  const sorts = filterSortElements(elements);
  sorts.forEach(({ fieldElement, orderElement }, index) => {
    const item = preset.sort?.[index];
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
  if (sort.some((item) => !MARKET_SCAN_TO_DISCOVERY_SORT[item.field])) throw new Error("排序字段无效");
  return {
    status: elementValue(elements.status) || "success",
    markets: marketValues(elements.market),
    industries: splitValues(elementValue(elements.industry), 20),
    isSt: booleanValue(elements.isSt),
    isNew: booleanValue(elements.isNew),
    ranges,
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
