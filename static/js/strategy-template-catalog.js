import { DEFAULT_REQUEST_TIMEOUT_MS } from "./api.js";

const API = "/api/strategy-lab/templates";
const ID_PATTERN = /^[a-z][a-z0-9_]{2,63}$/;
const REQUIRED_FIELD_PATTERN = /^[a-z][a-z0-9_.]{1,199}$/;
const METRIC_PATTERN = /^[a-z][a-z0-9_]{1,63}$/;
const SYMBOL_PATTERN = /^\d{6}\.(SH|SZ|BJ)$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const AVAILABILITIES = ["available_for_draft", "shadow_only", "unavailable"];
const CONTRACT_STATUSES = ["verified", "unavailable"];
const EFFICACY_STATUSES = ["not_generated", "insufficient_data", "unavailable"];
const PROFILE_OBJECTIVES = {
  conservative: { alpha_1d: .03, alpha_5d: .12, alpha_20d: .25, confidence: .20, risk: .25, tradability: .15 },
  balanced: { alpha_1d: .05, alpha_5d: .20, alpha_20d: .35, confidence: .15, risk: .15, tradability: .10 },
  aggressive: { alpha_1d: .10, alpha_5d: .25, alpha_20d: .40, confidence: .10, risk: .05, tradability: .10 },
};

export const STRATEGY_TEMPLATE_CATALOG_FIELDS = [
  "schema_version", "as_of_date", "selection_mode", "production_rule_version",
  "production_effect", "official_session_count", "templates", "catalog_digest",
];
export const STRATEGY_TEMPLATE_FIELDS = [
  "template_id", "version", "name", "family", "objective", "horizon", "availability",
  "strategy_spec", "contract_status", "efficacy_status", "regime_evidence_status",
  "required_fields", "missing_fields", "gate_reasons", "regime_hypotheses", "cost_notes",
  "risk_notes", "limitations", "template_digest",
];
export const STRATEGY_TEMPLATE_HORIZON_FIELDS = [
  "formation_sessions", "holding_sessions", "rebalance_sessions", "label",
];

const SPEC_FIELDS = [
  "name", "description", "schema_version", "universe", "exclusions", "hard_filters",
  "objectives", "profile", "portfolio_constraints", "rebalance_policy",
  "execution_policy", "evidence_policy",
];
const EXCLUSION_FIELDS = [
  "exclude_st", "exclude_new", "min_listing_days", "exclude_suspended",
  "min_history_sessions", "min_data_quality_score", "min_amount_cny",
];
const OBJECTIVE_FIELDS = ["alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"];
const PORTFOLIO_FIELDS = [
  "stock_count", "weighting_method", "max_stock_weight", "max_industry_positions",
  "max_industry_weight", "max_board_weight", "min_position_amount_cny",
  "max_notional_share_of_daily_amount", "custom_weights",
];
const REBALANCE_FIELDS = [
  "hold_sessions", "cadence", "rebalance_every_sessions", "buy_utility_threshold",
  "hold_utility_threshold",
];
const EXECUTION_FIELDS = [
  "t_plus_one", "respect_price_limits", "respect_suspensions", "cost_profile",
  "commission_rate", "minimum_commission_cny", "sell_stamp_duty_rate", "transfer_fee_rate",
  "buy_slippage_bps", "sell_slippage_bps",
];
const EVIDENCE_FIELDS = [
  "minimum_quality_score", "maximum_market_data_age_days", "maximum_fundamental_data_age_days",
  "allowed_sources", "blocked_sources", "require_verified_point_in_time_evidence",
];

export function validateStrategyTemplateCatalog(value) {
  const catalog = exactObject(value, STRATEGY_TEMPLATE_CATALOG_FIELDS, "策略模板目录");
  literal(catalog.schema_version, ["full-market-strategy-template-catalog-v1"], "schema_version");
  literal(catalog.as_of_date, ["2026-08-12"], "as_of_date");
  literal(catalog.selection_mode, ["exclusive"], "selection_mode");
  literal(catalog.production_rule_version, ["full-market-score-v4"], "production_rule_version");
  literal(catalog.production_effect, ["none"], "production_effect");
  literal(catalog.official_session_count, [2], "official_session_count");
  digest(catalog.catalog_digest, "catalog_digest");
  if (!Array.isArray(catalog.templates) || catalog.templates.length < 1 || catalog.templates.length > 50) {
    throw new Error("策略模板目录 templates 数量无效");
  }
  catalog.templates.forEach(validateStrategyTemplate);
  validateTemplateOrder(catalog.templates);
  return catalog;
}

export function validateStrategyTemplate(value) {
  const item = exactObject(value, STRATEGY_TEMPLATE_FIELDS, "策略模板");
  patternText(item.template_id, ID_PATTERN, "template_id");
  integer(item.version, 1, Number.MAX_SAFE_INTEGER, "version");
  boundedText(item.name, 1, 80, "name");
  boundedText(item.family, 1, 80, "family");
  boundedText(item.objective, 1, 1000, "objective");
  validateHorizon(item.horizon);
  literal(item.availability, AVAILABILITIES, "availability");
  literal(item.contract_status, CONTRACT_STATUSES, "contract_status");
  literal(item.efficacy_status, EFFICACY_STATUSES, "efficacy_status");
  literal(item.regime_evidence_status, ["not_generated"], "regime_evidence_status");
  stringList(item.required_fields, "required_fields", 1, 50, REQUIRED_FIELD_PATTERN);
  stringList(item.missing_fields, "missing_fields", 0, 50, REQUIRED_FIELD_PATTERN);
  noteList(item.gate_reasons, "gate_reasons", 1, 30);
  noteList(item.regime_hypotheses, "regime_hypotheses", 1, 20);
  noteList(item.cost_notes, "cost_notes", 1, 20);
  noteList(item.risk_notes, "risk_notes", 1, 20);
  noteList(item.limitations, "limitations", 1, 30);
  digest(item.template_digest, "template_digest");
  validateTemplateAvailability(item);
  return item;
}

function validateHorizon(value) {
  const horizon = exactObject(value, STRATEGY_TEMPLATE_HORIZON_FIELDS, "horizon");
  integer(horizon.formation_sessions, 1, 1500, "formation_sessions");
  integer(horizon.holding_sessions, 1, 60, "holding_sessions");
  integer(horizon.rebalance_sessions, 1, 60, "rebalance_sessions");
  boundedText(horizon.label, 1, 80, "horizon.label");
}

function validateTemplateAvailability(item) {
  const missingSubset = item.missing_fields.every((field) => item.required_fields.includes(field));
  if (!missingSubset) throw new Error("missing_fields 必须是 required_fields 的子集");
  if (item.availability === "available_for_draft") return validateReadyTemplate(item);
  if (item.availability === "shadow_only") return validateShadowTemplate(item);
  validateUnavailableTemplate(item);
}

function validateReadyTemplate(item) {
  validateStrategySpec(item.strategy_spec, item.name);
  if (item.contract_status !== "verified" || item.efficacy_status !== "not_generated") {
    throw new Error("可载入模板状态必须是 verified/not_generated");
  }
  if (item.missing_fields.length) throw new Error("可载入模板不能包含 missing_fields");
  const policy = item.strategy_spec.rebalance_policy;
  if (policy.hold_sessions !== item.horizon.holding_sessions
      || policy.rebalance_every_sessions !== item.horizon.rebalance_sessions) {
    throw new Error("模板周期与 StrategySpec 不一致");
  }
}

function validateShadowTemplate(item) {
  if (item.strategy_spec !== null || item.contract_status !== "verified"
      || item.efficacy_status !== "insufficient_data" || item.missing_fields.length) {
    throw new Error("Shadow 模板状态组合无效");
  }
}

function validateUnavailableTemplate(item) {
  if (item.strategy_spec !== null || item.contract_status !== "unavailable"
      || item.efficacy_status !== "unavailable" || !item.missing_fields.length) {
    throw new Error("不可用模板状态组合无效");
  }
}

function validateStrategySpec(value, expectedName) {
  const spec = exactObject(value, SPEC_FIELDS, "strategy_spec");
  boundedText(spec.name, 1, 80, "strategy_spec.name");
  if (spec.name !== expectedName) throw new Error("模板名称与 StrategySpec 名称不一致");
  boundedText(spec.description, 0, 1000, "strategy_spec.description");
  literal(spec.schema_version, [1], "strategy_spec.schema_version");
  validateUniverse(spec.universe);
  validateExclusions(spec.exclusions);
  validateHardFilters(spec.hard_filters);
  validateObjectives(spec.objectives);
  literal(spec.profile, ["conservative", "balanced", "aggressive", "custom"], "strategy_spec.profile");
  if (spec.profile !== "custom" && !sameObjectives(spec.objectives, PROFILE_OBJECTIVES[spec.profile])) {
    throw new Error("命名策略画像不能覆盖固定目标权重");
  }
  validatePortfolio(spec.portfolio_constraints);
  validateRebalance(spec.rebalance_policy);
  validateExecution(spec.execution_policy);
  validateEvidence(spec.evidence_policy);
}

function validateUniverse(value) {
  const universe = exactObject(value, ["boards"], "strategy_spec.universe");
  stringList(universe.boards, "strategy_spec.universe.boards", 1, 5);
  universe.boards.forEach((board) => literal(board, ["sh_main", "star", "sz_main", "chinext", "beijing"], "board"));
}

function validateExclusions(value) {
  const item = exactObject(value, EXCLUSION_FIELDS, "strategy_spec.exclusions");
  boolean(item.exclude_st, "exclude_st");
  boolean(item.exclude_new, "exclude_new");
  integer(item.min_listing_days, 0, 10000, "min_listing_days");
  boolean(item.exclude_suspended, "exclude_suspended");
  integer(item.min_history_sessions, 1, 1500, "min_history_sessions");
  integer(item.min_data_quality_score, 0, 100, "min_data_quality_score");
  finite(item.min_amount_cny, 0, 1e15, "min_amount_cny");
}

function validateHardFilters(value) {
  if (!Array.isArray(value) || value.length > 30) throw new Error("hard_filters 无效");
  value.forEach((candidate) => {
    const item = exactObject(candidate, ["field", "operator", "value", "period_sessions"], "hard_filter");
    patternText(item.field, METRIC_PATTERN, "hard_filter.field");
    literal(item.operator, ["eq", "ne", "gt", "gte", "lt", "lte", "between", "in"], "hard_filter.operator");
    validateFilterValue(item.value, item.operator);
    if (item.period_sessions !== null) integer(item.period_sessions, 1, 1500, "period_sessions");
  });
}

function validateFilterValue(value, operator) {
  const isList = Array.isArray(value);
  if (isList && !value.length) throw new Error("过滤条件列表不能为空");
  if (isList && !value.every((item) => typeof item === "string" || Number.isFinite(item))) {
    throw new Error("过滤条件列表值无效");
  }
  if (!isList && typeof value !== "boolean" && typeof value !== "string" && !Number.isFinite(value)) {
    throw new Error("过滤条件标量值无效");
  }
  if (operator === "between" && (!isList || value.length !== 2)) throw new Error("between 需要两个边界");
  if (operator === "in" && !isList) throw new Error("in 需要列表");
  if (!['between', 'in'].includes(operator) && isList) throw new Error(`${operator} 不能使用列表`);
}

function validateObjectives(value) {
  const item = exactObject(value, OBJECTIVE_FIELDS, "strategy_spec.objectives");
  OBJECTIVE_FIELDS.forEach((field) => finite(item[field], 0, 1, `objectives.${field}`));
  if (OBJECTIVE_FIELDS.every((field) => item[field] === 0)) throw new Error("目标权重不能全部为零");
}

function sameObjectives(left, right) {
  return OBJECTIVE_FIELDS.every((field) => left[field] === right[field]);
}

function validatePortfolio(value) {
  const item = exactObject(value, PORTFOLIO_FIELDS, "strategy_spec.portfolio_constraints");
  integer(item.stock_count, 1, 100, "stock_count");
  literal(item.weighting_method, ["equal", "risk_adjusted", "custom"], "weighting_method");
  finite(item.max_stock_weight, Number.MIN_VALUE, 1, "max_stock_weight");
  integer(item.max_industry_positions, 1, 100, "max_industry_positions");
  finite(item.max_industry_weight, Number.MIN_VALUE, 1, "max_industry_weight");
  finite(item.max_board_weight, Number.MIN_VALUE, 1, "max_board_weight");
  finite(item.min_position_amount_cny, 0, 1e9, "min_position_amount_cny");
  finite(item.max_notional_share_of_daily_amount, Number.MIN_VALUE, .05, "max_notional_share_of_daily_amount");
  validateCustomWeights(item.custom_weights, item);
}

function validateCustomWeights(value, portfolio) {
  const weights = objectValue(value, "custom_weights");
  const entries = Object.entries(weights);
  if (entries.length > 100) throw new Error("custom_weights 数量无效");
  entries.forEach(([symbol, weight]) => {
    patternText(symbol, SYMBOL_PATTERN, "custom_weights symbol");
    finite(weight, Number.MIN_VALUE, 1, `custom_weights.${symbol}`);
    if (weight > portfolio.max_stock_weight) throw new Error("自定义权重超过单股上限");
  });
  if ((portfolio.weighting_method === "custom") !== Boolean(entries.length)) throw new Error("自定义权重与加权方式不一致");
  if (entries.length > portfolio.stock_count || entries.reduce((sum, [, weight]) => sum + weight, 0) > 1.0000001) {
    throw new Error("custom_weights 组合约束无效");
  }
}

function validateRebalance(value) {
  const item = exactObject(value, REBALANCE_FIELDS, "strategy_spec.rebalance_policy");
  integer(item.hold_sessions, 1, 60, "hold_sessions");
  literal(item.cadence, ["manual", "daily_after_close", "trading_day_intraday"], "cadence");
  integer(item.rebalance_every_sessions, 1, 60, "rebalance_every_sessions");
  finite(item.buy_utility_threshold, 0, 100, "buy_utility_threshold");
  finite(item.hold_utility_threshold, 0, 100, "hold_utility_threshold");
  if (item.hold_utility_threshold > item.buy_utility_threshold) throw new Error("持有阈值不能高于买入阈值");
}

function validateExecution(value) {
  const item = exactObject(value, EXECUTION_FIELDS, "strategy_spec.execution_policy");
  ["t_plus_one", "respect_price_limits", "respect_suspensions"].forEach((field) => literal(item[field], [true], field));
  literal(item.cost_profile, ["base", "conservative", "stress"], "cost_profile");
  finite(item.commission_rate, 0, .02, "commission_rate");
  finite(item.minimum_commission_cny, 0, 1000, "minimum_commission_cny");
  finite(item.sell_stamp_duty_rate, 0, .02, "sell_stamp_duty_rate");
  finite(item.transfer_fee_rate, 0, .01, "transfer_fee_rate");
  finite(item.buy_slippage_bps, 0, 1000, "buy_slippage_bps");
  finite(item.sell_slippage_bps, 0, 1000, "sell_slippage_bps");
}

function validateEvidence(value) {
  const item = exactObject(value, EVIDENCE_FIELDS, "strategy_spec.evidence_policy");
  integer(item.minimum_quality_score, 0, 100, "minimum_quality_score");
  integer(item.maximum_market_data_age_days, 0, 365, "maximum_market_data_age_days");
  integer(item.maximum_fundamental_data_age_days, 0, 730, "maximum_fundamental_data_age_days");
  stringList(item.allowed_sources, "allowed_sources", 0, 30, null, 80);
  stringList(item.blocked_sources, "blocked_sources", 0, 30, null, 80);
  boolean(item.require_verified_point_in_time_evidence, "require_verified_point_in_time_evidence");
  if (item.allowed_sources.some((source) => item.blocked_sources.includes(source))) throw new Error("证据来源不能同时允许和禁止");
}

function validateTemplateOrder(templates) {
  const identities = templates.map((item) => `${item.template_id}\u0000${String(item.version).padStart(16, "0")}`);
  const templateIds = templates.map((item) => item.template_id);
  if (new Set(templateIds).size !== templateIds.length) throw new Error("策略模板 ID 重复");
  if (identities.some((identity, index) => index && identity < identities[index - 1])) {
    throw new Error("策略模板未按 template_id/version 排序");
  }
}

export function createStrategyTemplateCatalog(options = {}) {
  const root = options.root || globalThis.document;
  const elements = catalogElements(root);
  if (!elements) return inertCatalog();
  const request = options.fetcher;
  const onLoadDraft = options.onLoadDraft || (async () => null);
  const state = { catalog: null, selectedId: null, source: null, dirty: false, busy: false, working: false };
  bindCatalogEvents(elements, state, onLoadDraft);

  async function load() {
    if (state.catalog) return state.catalog;
    setCatalogStatus(elements, "正在读取严格模板目录…", "loading");
    try {
      const payload = await request(API, { timeoutMs: options.timeoutMs || DEFAULT_REQUEST_TIMEOUT_MS });
      state.catalog = validateStrategyTemplateCatalog(payload);
      renderCatalog(elements, state);
      setCatalogStatus(elements, `已读取 ${state.catalog.templates.length} 个互斥策略镜头；环境匹配证据均未生成。`, "ready");
      elements.summary.textContent = `${state.catalog.templates.length} 个 · 截至 ${state.catalog.as_of_date}`;
      return state.catalog;
    } catch (error) {
      state.catalog = null;
      elements.cards.replaceChildren();
      elements.summary.textContent = "目录不可用";
      setCatalogStatus(elements, `模板目录校验失败，已拒绝展示：${String(error?.message || error)}`, "error");
      return null;
    }
  }

  function setBusy(busy) {
    const next = Boolean(busy);
    if (state.busy === next) return;
    state.busy = next;
    renderCatalog(elements, state);
  }

  function markCustom() {
    if (!state.source || state.dirty) return;
    state.dirty = true;
    renderDraftStatus(elements, state);
  }

  function clearSource(message = "尚未载入模板；编辑器保持当前草案。") {
    state.source = null;
    state.dirty = false;
    state.selectedId = null;
    renderCatalog(elements, state);
    elements.draftStatus.textContent = message;
    elements.draftStatus.dataset.state = "empty";
  }

  return { load, setBusy, markCustom, clearSource, state };
}

function bindCatalogEvents(elements, state, onLoadDraft) {
  elements.cards.addEventListener("change", (event) => {
    const radio = event.target.closest?.("[data-template-choice]");
    if (!radio) return;
    state.selectedId = radio.value;
    syncCatalogSelection(elements, state.selectedId);
  });
  elements.cards.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-template-load]");
    if (button) void loadSelectedTemplate(elements, state, onLoadDraft, button.dataset.templateLoad);
  });
}

async function loadSelectedTemplate(elements, state, onLoadDraft, templateId) {
  const item = state.catalog?.templates.find((template) => template.template_id === templateId);
  if (!item || item.availability !== "available_for_draft" || state.busy || state.working) return;
  state.selectedId = item.template_id;
  state.working = true;
  renderCatalog(elements, state);
  setCatalogStatus(elements, `正在载入“${item.name}”并进行 dry-run 编译…`, "loading");
  try {
    const compiled = await onLoadDraft(item);
    if (!compiled) throw new Error("dry-run 编译未通过，草案来源未生效");
    state.source = item;
    state.dirty = false;
    setCatalogStatus(elements, `已载入“${item.name}”研究草案；未保存、未扫描、未改变生产排名。`, "ready");
    renderDraftStatus(elements, state);
  } catch (error) {
    setCatalogStatus(elements, `模板载入失败：${String(error?.message || error)}`, "error");
  } finally {
    state.working = false;
    renderCatalog(elements, state);
  }
}

function renderCatalog(elements, state) {
  if (!state.catalog) return;
  elements.cards.innerHTML = state.catalog.templates.map((item) => templateCard(item, state)).join("");
  elements.cards.setAttribute("aria-busy", String(state.busy || state.working));
}

function syncCatalogSelection(elements, selectedId) {
  elements.cards.querySelectorAll(".strategy-template-card").forEach((card) => {
    const radio = card.querySelector("[data-template-choice]");
    card.dataset.selected = String(radio?.value === selectedId);
  });
}

export function strategyTemplateCardHtml(item, selected = false, busy = false) {
  return templateCard(item, { selectedId: selected ? item.template_id : null, busy, working: false });
}

function templateCard(item, state) {
  const selected = item.template_id === state.selectedId;
  const ready = item.availability === "available_for_draft";
  const disabled = !ready || state.busy || state.working;
  const reasonId = `strategy-template-reason-${item.template_id}`;
  const action = ready ? "载入研究草案" : item.availability === "shadow_only" ? "仅 Shadow，暂不可载入" : "字段不足，暂不可载入";
  return `<article class="strategy-template-card" data-availability="${escapeHtml(item.availability)}" data-selected="${selected}">
    <label class="strategy-template-choice">
      <input type="radio" name="strategyTemplateChoice" value="${escapeHtml(item.template_id)}" data-template-choice aria-describedby="${reasonId}"${selected ? " checked" : ""}${disabled ? " disabled" : ""} />
      <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.family)} · v${item.version} · ${escapeHtml(availabilityLabel(item.availability))}</small></span>
    </label>
    <p class="strategy-template-copy"><strong>目标：</strong>${escapeHtml(displayText(item.objective))}</p>
    <dl class="strategy-template-meta">
      ${metric("持有期", item.horizon.label)}
      ${metric("研究适用环境", "假设未匹配")}
      ${metric("合同状态", contractLabel(item.contract_status))}
      ${metric("收益有效性", efficacyLabel(item.efficacy_status))}
      ${metric("环境证据", "未生成")}
      ${metric("草案合同", item.strategy_spec === null ? null : "已提供")}
    </dl>
    <p class="strategy-template-copy"><strong>环境假设：</strong>${escapeHtml(joinNotes(item.regime_hypotheses))}</p>
    <p class="strategy-template-copy"><strong>成本：</strong>${escapeHtml(joinNotes(item.cost_notes))}</p>
    <p class="strategy-template-copy warning"><strong>风险：</strong>${escapeHtml(joinNotes(item.risk_notes))}</p>
    <p class="strategy-template-copy"><strong>限制：</strong>${escapeHtml(joinNotes(item.limitations))}</p>
    <p class="strategy-template-copy warning" id="${reasonId}"><strong>当前门禁：</strong>${escapeHtml(joinNotes(item.gate_reasons))}</p>
    <p class="strategy-template-digest" title="${escapeHtml(item.template_digest)}">template digest · ${escapeHtml(item.template_digest.slice(0, 12))}…</p>
    <button type="button" data-template-load="${escapeHtml(item.template_id)}" aria-describedby="${reasonId}"${disabled ? " disabled" : ""}>${escapeHtml(action)}</button>
  </article>`;
}

function renderDraftStatus(elements, state) {
  if (!state.source) return;
  elements.draftStatus.textContent = state.dirty
    ? `基于“${state.source.name}”修改 · 当前为自定义`
    : `当前草案来源：“${state.source.name}” v${state.source.version} · 尚未保存`;
  elements.draftStatus.dataset.state = state.dirty ? "custom" : "template";
}

function catalogElements(root) {
  const value = {
    cards: root?.getElementById?.("strategyTemplateCards"),
    status: root?.getElementById?.("strategyTemplateCatalogStatus"),
    summary: root?.getElementById?.("strategyTemplateCatalogSummary"),
    draftStatus: root?.getElementById?.("strategyTemplateDraftStatus"),
  };
  return Object.values(value).every(Boolean) ? value : null;
}

function setCatalogStatus(elements, text, kind) {
  elements.status.textContent = text;
  elements.status.dataset.kind = kind;
}

function metric(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(displayText(value))}</dd></div>`;
}

function availabilityLabel(value) {
  return { available_for_draft: "可载入研究草案", shadow_only: "仅 Shadow", unavailable: "当前不可用" }[value];
}

function contractLabel(value) {
  return { verified: "研究合同已校验", unavailable: "研究合同不可用" }[value];
}

function efficacyLabel(value) {
  return { not_generated: "收益有效性未生成", insufficient_data: "收益证据样本不足", unavailable: "收益有效性不可用" }[value];
}

function joinNotes(values) {
  return values?.length ? values.join("；") : "--";
}

function displayText(value) {
  return value === null || value === undefined || value === "" ? "--" : String(value);
}

function exactObject(value, fields, label) {
  const item = objectValue(value, label);
  const keys = Object.keys(item);
  if (keys.length !== fields.length || keys.some((key) => !fields.includes(key))
      || fields.some((key) => !Object.prototype.hasOwnProperty.call(item, key))) {
    throw new Error(`${label}字段不完整或包含未知字段`);
  }
  return item;
}

function objectValue(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}格式异常`);
  return value;
}

function boundedText(value, minimum, maximum, label) {
  if (typeof value !== "string" || value.trim() !== value || value.length < minimum || value.length > maximum) {
    throw new Error(`${label}文本无效`);
  }
}

function patternText(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) throw new Error(`${label}格式无效`);
}

function stringList(value, label, minimum, maximum, pattern = null, itemMaximum = 500) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) throw new Error(`${label}数量无效`);
  value.forEach((item) => {
    boundedText(item, 1, itemMaximum, `${label}[]`);
    if (pattern && !pattern.test(item)) throw new Error(`${label}[]格式无效`);
  });
  if (new Set(value).size !== value.length) throw new Error(`${label}不能包含重复项`);
}

function noteList(value, label, minimum, maximum) {
  stringList(value, label, minimum, maximum, null, 500);
}

function literal(value, allowed, label) {
  if (!allowed.includes(value)) throw new Error(`${label}状态无效`);
}

function integer(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error(`${label}整数无效`);
}

function finite(value, minimum, maximum, label) {
  if (!Number.isFinite(value) || value < minimum || value > maximum) throw new Error(`${label}数值无效`);
}

function boolean(value, label) {
  if (typeof value !== "boolean") throw new Error(`${label}布尔值无效`);
}

function digest(value, label) {
  if (typeof value !== "string" || !DIGEST_PATTERN.test(value)) throw new Error(`${label}摘要无效`);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

function inertCatalog() {
  return { load: async () => null, setBusy() {}, markCustom() {}, clearSource() {}, state: {} };
}
