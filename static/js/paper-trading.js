import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

const PAPER_RUN_TIMEOUT_MS = 120000;
const STATUS_LABELS = Object.freeze({
  pending: "等待入场",
  open: "持仓中",
  closed: "已平仓",
  skipped: "未入场",
  expired: "入场过期",
  data_unavailable: "数据不可用",
});
const SIDE_LABELS = Object.freeze({ buy: "买入", sell: "卖出" });
const REASON_LABELS = Object.freeze({
  strategy_entry: "策略入场",
  target_hit: "目标价触达",
  stop_hit: "止损价触达",
  target_stop_ambiguous: "同日双触发，按止损",
  horizon_close: "观察期结束",
  target_before_entry: "入场前已达目标",
  invalid_before_entry: "入场前已失效",
  entry_expired: "等待入场过期",
  t1_deferred_target: "T+1 延迟止盈",
  t1_deferred_stop: "T+1 延迟止损",
  t1_deferred_ambiguous: "T+1 双触发延迟退出",
});
const EVENT_CATEGORY_LABELS = Object.freeze({
  lifecycle: "生命周期",
  execution: "成交",
  risk: "风控",
  data: "数据",
  rule: "规则",
  cost: "成本",
});
const SHA256_HEX = /^[0-9a-f]{64}$/;

function requirePaperStrategy(value, expectedPlan = null) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("模拟策略格式异常");
  for (const field of ["id", "plan_id", "plan_revision", "advice_id"]) {
    if (!Number.isSafeInteger(Number(value[field])) || Number(value[field]) <= 0) throw new TypeError("模拟策略身份无效");
  }
  if (typeof value.symbol !== "string" || !value.symbol.trim()) throw new TypeError("模拟策略股票身份无效");
  if (!SHA256_HEX.test(String(value.plan_payload_digest || ""))) throw new TypeError("模拟策略计划摘要无效");
  if (expectedPlan && (
    Number(value.plan_id) !== Number(expectedPlan.id)
    || Number(value.plan_revision) !== Number(expectedPlan.revision)
    || Number(value.advice_id) !== Number(expectedPlan.advice_id)
    || value.symbol !== expectedPlan.symbol
    || value.plan_payload_digest !== expectedPlan.plan_payload_digest
  )) throw new TypeError("模拟策略与冻结复盘计划不一致");
  return value;
}

export async function loadPaperTradingDashboard(state, options = {}) {
  const sequence = Number(state.paperTradingSeq || 0) + 1;
  state.paperTradingSeq = sequence;
  renderPaperLoading();
  try {
    const runId = Number(options.runId || 0);
    const url = runId > 0 ? `/api/paper-trading?run_id=${encodeURIComponent(runId)}` : "/api/paper-trading";
    const dashboard = await fetchJson(url, {
      signal: options.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (state.paperTradingSeq !== sequence) return false;
    requireDashboard(dashboard);
    state.paperTradingDashboard = dashboard;
    renderPaperTradingDashboard(state);
    setPaperFeedback("");
    return true;
  } catch (error) {
    if (isAbortError(error) || state.paperTradingSeq !== sequence) return false;
    renderPaperUnavailable(error);
    return false;
  }
}

export async function createPaperStrategy(state) {
  const planId = positiveNumber($("paperReviewPlan")?.value, "请选择复盘计划");
  const allocationPct = positiveNumber($("paperAllocationPct")?.value, "资金占比无效");
  const priority = finiteNumber($("paperPriority")?.value, 0, "策略优先级无效");
  const entryExpirySessions = positiveNumber($("paperEntryExpirySessions")?.value || 5, "入场有效期无效");
  const expectedPlan = (state.adviceReviewDashboardDetails || [])
    .map((item) => item?.plan)
    .find((plan) => Number(plan?.id) === planId);
  if (!expectedPlan) throw new Error("冻结复盘计划不存在或版本已变化");
  if (!SHA256_HEX.test(String(expectedPlan.plan_payload_digest || ""))) {
    throw new TypeError("冻结复盘计划摘要无效，请刷新后重试");
  }
  const strategy = requirePaperStrategy(await mutatePaper("/api/paper-trading/strategies", "POST", {
    plan_id: planId,
    expected_plan_revision: Number(expectedPlan.revision),
    expected_plan_payload_digest: expectedPlan.plan_payload_digest,
    allocation_pct: allocationPct,
    priority,
    entry_expiry_sessions: entryExpirySessions,
  }), expectedPlan);
  await loadPaperTradingDashboard(state);
  setPaperFeedback(`已将 ${strategy.symbol || "策略"} 加入模拟，请运行撮合`, "ok");
  return strategy;
}

export async function updatePaperTradingAccount(state) {
  const initialCash = positiveNumber($("paperInitialCash")?.value, "初始资金无效");
  const defaultCostProfile = String($("paperDefaultCostProfile")?.value || "base");
  const account = await mutatePaper("/api/paper-trading/account", "PATCH", {
    initial_cash: initialCash,
    default_cost_profile: defaultCostProfile,
  });
  await loadPaperTradingDashboard(state);
  setPaperFeedback("初始资金已保存", "ok");
  return account;
}

export async function runPaperTradingSimulation(state) {
  const date = String($("paperRunAsOf")?.value || "").trim();
  const payload = {
    ...(date ? { as_of: `${date}T23:59:59+08:00` } : {}),
    cost_profile: String($("paperCostProfile")?.value || "base"),
    benchmark_symbol: String($("paperBenchmarkSymbol")?.value || "").trim() || null,
  };
  const summary = await mutatePaper("/api/paper-trading/run", "POST", payload, PAPER_RUN_TIMEOUT_MS);
  if (!summary || typeof summary !== "object") throw new TypeError("模拟运行结果格式异常");
  requireDashboard(summary.dashboard);
  if (Number(summary.run_id || 0) !== Number(summary.dashboard.selected_run_id || 0)) {
    throw new TypeError("模拟运行与仪表盘身份不一致");
  }
  state.paperTradingDashboard = summary.dashboard;
  renderPaperTradingDashboard(state);
  setPaperFeedback(summary.dashboard?.latest_run?.message || "模拟撮合完成", "ok");
  return summary;
}

export async function selectPaperTradingRun(state, runId) {
  const selected = positiveNumber(runId, "请选择历史运行");
  const loaded = await loadPaperTradingDashboard(state, { runId: selected });
  if (loaded) setPaperFeedback(`已切换到运行 #${selected}`, "ok");
  return loaded;
}

export async function comparePaperTradingRuns(state) {
  const leftRunId = positiveNumber($("paperCompareLeft")?.value, "请选择左侧运行");
  const rightRunId = positiveNumber($("paperCompareRight")?.value, "请选择右侧运行");
  if (leftRunId === rightRunId) throw new Error("请选择两个不同运行");
  const comparison = await fetchJson(
    `/api/paper-trading/runs/compare?left_run_id=${encodeURIComponent(leftRunId)}&right_run_id=${encodeURIComponent(rightRunId)}`,
    { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
  );
  requireRunComparison(comparison, leftRunId, rightRunId);
  state.paperTradingComparison = comparison;
  renderRunComparison(comparison);
  setPaperFeedback(`已比较运行 #${leftRunId} 与 #${rightRunId}`, "ok");
  return comparison;
}

export async function deletePaperStrategy(state, strategyId, options = {}) {
  if (options.confirm && !options.confirm("删除这条尚未形成持仓的模拟策略？")) return false;
  await fetchJson(`/api/paper-trading/strategies/${encodeURIComponent(strategyId)}`, {
    method: "DELETE",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  await loadPaperTradingDashboard(state);
  setPaperFeedback("模拟策略已删除", "ok");
  return true;
}

export function syncPaperTradingPlans(state) {
  const select = $("paperReviewPlan");
  if (!select) return;
  const selected = String(select.value || "");
  const strategies = Array.isArray(state.paperTradingDashboard?.strategies)
    ? state.paperTradingDashboard.strategies
    : [];
  const used = new Set(strategies.map((item) => `${item.plan_id}:${item.plan_revision}`));
  const plans = (state.adviceReviewDashboardDetails || []).map((item) => item?.plan).filter(Boolean);
  select.innerHTML = plans.length
    ? `<option value="">选择冻结计划</option>${plans.map((plan) => paperPlanOption(plan, used)).join("")}`
    : `<option value="">暂无可用复盘计划</option>`;
  if (plans.some((plan) => String(plan.id) === selected)) select.value = selected;
}

export function selectPaperTradingPlan(state, planId) {
  syncPaperTradingPlans(state);
  const select = $("paperReviewPlan");
  if (!select) return false;
  select.value = String(planId || "");
  select.focus();
  return select.value === String(planId || "");
}

export function renderPaperTradingDashboard(state) {
  const dashboard = state.paperTradingDashboard || {};
  renderAccount(dashboard.account, dashboard.strategies);
  renderPerformance(dashboard.performance);
  renderRunControls(dashboard);
  renderRunMetadata(dashboard);
  renderStrategies(dashboard.strategies);
  renderPositions(dashboard.positions);
  renderTrades(dashboard.trades);
  renderEvents(dashboard.events);
  renderEquity(dashboard.equity_curve, dashboard.trades);
  renderNotes(dashboard.notes);
  if (state.paperTradingComparison) renderRunComparison(state.paperTradingComparison);
  syncPaperTradingPlans(state);
}

async function mutatePaper(url, method, payload, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  return fetchJson(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs,
  });
}

function requireDashboard(value) {
  if (!value || typeof value !== "object" || !value.account || !value.performance) {
    throw new TypeError("模拟交易数据格式异常");
  }
  if (!Array.isArray(value.strategies)) throw new TypeError("模拟策略列表格式异常");
  value.strategies.forEach((strategy) => requirePaperStrategy(strategy));
  if (!Array.isArray(value.runs)) throw new TypeError("模拟运行列表格式异常");
  value.runs.forEach(requirePaperRun);
  const selected = value.selected_run_id == null ? null : positiveNumber(value.selected_run_id, "模拟运行身份无效");
  if (selected !== null && !value.runs.some((run) => Number(run.id) === selected)) {
    throw new TypeError("选中的模拟运行不存在");
  }
  if (value.latest_run != null) {
    requirePaperRun(value.latest_run);
    if (!value.runs.some((run) => Number(run.id) === Number(value.latest_run.id))) {
      throw new TypeError("最新模拟运行不存在于运行列表");
    }
  }
  for (const field of ["positions", "trades", "events", "equity_curve", "cost_profiles", "notes"]) {
    if (!Array.isArray(value[field])) throw new TypeError(`模拟 ${field} 格式异常`);
  }
  if (selected !== null) {
    const selectedRun = value.runs.find((run) => Number(run.id) === selected);
    requireDashboardChildren(value, selectedRun);
  }
  return value;
}

function requirePaperRun(run) {
  if (!run || typeof run !== "object" || !Number.isSafeInteger(Number(run.id)) || Number(run.id) <= 0
    || !Number.isFinite(Date.parse(run.as_of)) || !SHA256_HEX.test(String(run.input_fingerprint || ""))
    || !SHA256_HEX.test(String(run.output_digest || ""))) throw new TypeError("模拟运行完整性摘要无效");
  for (const field of ["strategy_count", "execution_count", "closed_count", "data_unavailable_count"]) {
    if (!Number.isSafeInteger(Number(run[field])) || Number(run[field]) < 0) throw new TypeError("模拟运行计数无效");
  }
  return run;
}

function requireDashboardChildren(value, selectedRun) {
  const selectedRunId = Number(selectedRun.id);
  const strategies = new Map(value.strategies.map((strategy) => [Number(strategy.id), strategy]));
  value.trades.forEach((item) => requirePaperChild(item, selectedRunId, strategies, true));
  value.events.forEach((item) => requirePaperChild(item, selectedRunId, strategies, false));
  value.positions.forEach((item) => {
    if (!value.strategies.some((strategy) => Number(strategy.id) === Number(item?.strategy_id)
      && strategy.symbol === item?.symbol)) throw new TypeError("模拟持仓归属无效");
  });
  value.equity_curve.forEach((item) => {
    if (item?.run_id !== undefined && Number(item.run_id) !== selectedRunId) {
      throw new TypeError("模拟净值归属无效");
    }
    if (!String(item?.as_of_date || "")) throw new TypeError("模拟净值日期无效");
  });
  if (Number(selectedRun.strategy_count) !== value.strategies.length
    || Number(selectedRun.execution_count) !== value.trades.length
    || Number(selectedRun.closed_count) !== value.strategies.filter((item) => item.status === "closed").length
    || Number(selectedRun.data_unavailable_count) !== value.strategies.filter((item) => item.status === "data_unavailable").length) {
    throw new TypeError("模拟运行计数与子记录不一致");
  }
}

function requirePaperChild(item, selectedRunId, strategies, needsStrategy) {
  if (!item || typeof item !== "object" || Number(item.run_id) !== selectedRunId) {
    throw new TypeError("模拟子记录运行归属无效");
  }
  const strategyId = item.strategy_id == null ? null : Number(item.strategy_id);
  if (needsStrategy && (!Number.isSafeInteger(strategyId) || strategyId <= 0)) {
    throw new TypeError("模拟成交策略归属无效");
  }
  if (strategyId !== null) {
    const strategy = strategies.get(strategyId);
    if (!strategy || (item.symbol != null && item.symbol !== strategy.symbol)) {
      throw new TypeError("模拟子记录策略或股票归属无效");
    }
  }
}

function requireRunComparison(value, leftRunId, rightRunId) {
  if (!value || typeof value !== "object" || !value.left_performance || !value.right_performance
    || !value.deltas) throw new TypeError("模拟运行对比格式异常");
  requirePaperRun(value.left_run);
  requirePaperRun(value.right_run);
  if (Number(value.left_run.id) !== leftRunId || Number(value.right_run.id) !== rightRunId) {
    throw new TypeError("模拟运行对比身份不一致");
  }
  return value;
}

function renderAccount(account, strategies) {
  const input = $("paperInitialCash");
  const button = $("savePaperAccount");
  const defaultCost = $("paperDefaultCostProfile");
  if (input && account) input.value = String(account.initial_cash ?? "");
  if (defaultCost && account?.default_cost_profile) defaultCost.value = account.default_cost_profile;
  const locked = Array.isArray(strategies) && strategies.length > 0;
  if (input) input.disabled = locked;
  if (button) button.disabled = false;
}

function renderPerformance(performance = {}) {
  const target = $("paperTradingSummary");
  if (!target) return;
  const metrics = [
    ["总权益", money(performance.total_equity)],
    ["总收益", percent(performance.total_return_pct)],
    ["毛收益", percent(performance.gross_return_pct)],
    ["超额收益", performance.excess_return_pct == null ? "--" : percent(performance.excess_return_pct)],
    ["最大回撤", percent(performance.max_drawdown_pct)],
    ["可用资金", money(performance.cash_balance)],
    ["持仓市值", money(performance.market_value)],
    ["已实现盈亏", money(performance.realized_pnl)],
    ["成本侵蚀", `${money(performance.total_cost)} / ${percent(performance.cost_drag_pct)}`],
    ["费用 / 毛利润", performance.cost_to_gross_profit_pct == null ? "--" : percent(performance.cost_to_gross_profit_pct)],
    ["持仓 / 已平", `${numberText(performance.open_count, 0)} / ${numberText(performance.closed_count, 0)}`],
    ["胜率", performance.win_rate_pct == null ? "--" : percent(performance.win_rate_pct)],
    ["盈亏比", optionalNumber(performance.payoff_ratio)],
    ["单笔期望", performance.expectancy == null ? "--" : money(performance.expectancy)],
    ["利润因子", optionalNumber(performance.profit_factor)],
    ["换手 / 平均仓位", `${percent(performance.turnover_pct)} / ${percent(performance.average_exposure_pct)}`],
  ];
  target.innerHTML = metrics.map(([label, value]) => `
    <span><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></span>`).join("");
}

function renderStrategies(strategies) {
  const target = $("paperStrategyList");
  if (!target) return;
  const rows = Array.isArray(strategies) ? strategies : [];
  target.innerHTML = rows.length ? rows.map(strategyHtml).join("") : emptyState("尚未加入策略", "先建立复盘计划，再冻结到模拟账户。");
}

function strategyHtml(item) {
  const removable = !item.allocation_order && ["pending", "skipped", "expired", "data_unavailable"].includes(item.status);
  const result = item.realized_pnl == null ? "--" : `${money(item.realized_pnl)} / ${percent(item.return_pct)}`;
  return `<article class="paper-strategy-item" data-paper-strategy="${escapeHtml(item.id)}">
    <div><strong>${escapeHtml(item.symbol || "--")}</strong><span>计划 #${escapeHtml(item.plan_id)} · v${escapeHtml(item.plan_revision)} · 仓位 ${escapeHtml(percent(item.allocation_pct))} · 优先级 ${escapeHtml(item.priority ?? 0)}</span></div>
    <div><b>${escapeHtml(STATUS_LABELS[item.status] || item.status)}</b><span>${escapeHtml(strategyDates(item))}</span></div>
    <div><b>${escapeHtml(result)}</b><span>目标 ${escapeHtml(numberText(item.normalized_target_price ?? item.target_price))} · 止损 ${escapeHtml(numberText(item.normalized_stop_price ?? item.stop_price))} · 分配顺序 ${escapeHtml(item.allocation_order ?? "--")}</span></div>
    ${removable ? `<button type="button" class="icon-button" title="删除模拟策略" aria-label="删除模拟策略" data-paper-delete="${escapeHtml(item.id)}">×</button>` : ""}
    ${item.pending_exit_reason ? `<p>待执行：${escapeHtml(REASON_LABELS[item.pending_exit_reason] || item.pending_exit_reason)}</p>` : ""}
    ${item.rule_data_degraded ? `<p>规则元数据降级：历史 ST / 上市状态不完整</p>` : ""}
    ${item.error_message ? `<p>${escapeHtml(item.error_message)}</p>` : ""}
  </article>`;
}

function strategyDates(item) {
  if (item.exit_date) return `${item.entry_date || "未入场"} → ${item.exit_date}`;
  if (item.entry_date) return `${item.entry_date} 入场 · 持有 ${item.held_sessions || 0} 日`;
  return `${String(item.activation_market_time || "--").slice(0, 10)} 激活`;
}

function renderPositions(positions) {
  const target = $("paperPositionList");
  if (!target) return;
  const rows = Array.isArray(positions) ? positions : [];
  target.innerHTML = rows.length ? tableHtml(
    ["股票", "数量", "成本", "现价", "浮盈亏", "目标 / 止损"],
    rows.map((item) => [
      item.symbol,
      numberText(item.quantity, 0),
      money(item.cost_basis),
      numberText(item.last_price),
      `${money(item.unrealized_pnl)} / ${percent(item.return_pct)}`,
      `${numberText(item.target_price)} / ${numberText(item.stop_price)}`,
    ])
  ) : emptyState("当前没有持仓", "运行模拟后，这里显示尚未退出的策略。");
}

function renderTrades(trades) {
  const target = $("paperTradeList");
  if (!target) return;
  const rows = Array.isArray(trades) ? trades : [];
  target.innerHTML = rows.length ? tableHtml(
    ["日期", "股票", "方向", "价格", "数量", "成交额", "佣金", "印花税", "过户费", "滑点", "总成本", "原因"],
    rows.map((item) => [
      item.trade_date,
      item.symbol,
      SIDE_LABELS[item.side] || item.side,
      numberText(item.price),
      numberText(item.quantity, 0),
      money(item.gross_amount),
      money(item.commission_amount),
      money(item.stamp_duty_amount),
      money(item.transfer_fee_amount),
      money(item.slippage_amount),
      money(item.friction_amount),
      REASON_LABELS[item.reason] || item.reason,
    ])
  ) : emptyState("暂无模拟成交", "加入策略并运行模拟后生成成交记录。");
}

function renderEvents(events) {
  const target = $("paperEventList");
  if (!target) return;
  const rows = Array.isArray(events) ? events : [];
  target.innerHTML = rows.length ? tableHtml(
    ["序号", "日期", "股票", "分类", "事件", "说明"],
    rows.map((item) => [
      numberText(item.sequence, 0),
      item.event_date,
      item.symbol || "--",
      EVENT_CATEGORY_LABELS[item.category] || item.category,
      item.event_code,
      item.message,
    ])
  ) : emptyState("暂无事件流水", "运行模拟后显示入场、等待、风控、未成交和退出原因。");
}

function renderEquity(points, trades) {
  const target = $("paperEquityChart");
  if (!target) return;
  const rows = Array.isArray(points) ? points.filter((item) => Number.isFinite(Number(item?.total_equity))) : [];
  if (!rows.length) {
    target.innerHTML = emptyState("暂无净值曲线", "运行模拟后按交易日生成。");
    return;
  }
  const values = rows.map((item) => Number(item.total_equity));
  const benchmarkValues = rows
    .filter((item) => item.benchmark_equity != null)
    .map((item) => Number(item.benchmark_equity))
    .filter(Number.isFinite);
  const scaleValues = [...values, ...benchmarkValues];
  const low = Math.min(...scaleValues);
  const high = Math.max(...scaleValues);
  const range = high - low || 1;
  const polyline = values.map((value, index) => {
    const x = values.length === 1 ? 50 : index / (values.length - 1) * 100;
    const y = 92 - (value - low) / range * 80;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const benchmarkPolyline = rows.map((item, index) => {
    if (item.benchmark_equity == null) return null;
    const value = Number(item.benchmark_equity);
    if (!Number.isFinite(value)) return null;
    const x = rows.length === 1 ? 50 : index / (rows.length - 1) * 100;
    const y = 92 - (value - low) / range * 80;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).filter(Boolean).join(" ");
  const markers = (Array.isArray(trades) ? trades : []).map((trade) => {
    const index = rows.findIndex((item) => item.as_of_date === trade.trade_date);
    if (index < 0) return "";
    const x = rows.length === 1 ? 50 : index / (rows.length - 1) * 100;
    const y = 92 - (values[index] - low) / range * 80;
    return `<circle class="paper-trade-marker ${escapeHtml(trade.side)}" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="1.8"><title>${escapeHtml(SIDE_LABELS[trade.side] || trade.side)} ${escapeHtml(trade.symbol)} ${escapeHtml(numberText(trade.price))}</title></circle>`;
  }).join("");
  const drawdown = rows.map((item, index) => {
    const x = rows.length === 1 ? 50 : index / (rows.length - 1) * 100;
    const value = Math.max(-100, Math.min(0, Number(item.drawdown_pct) || 0));
    const y = 98 + value * 0.15;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const latest = rows.at(-1);
  target.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <polyline class="paper-equity-line" points="${polyline}" />
      ${benchmarkPolyline ? `<polyline class="paper-benchmark-line" points="${benchmarkPolyline}" />` : ""}
      <polyline class="paper-drawdown-line" points="${drawdown}" />
      ${markers}
    </svg>
    <div><span>${escapeHtml(rows[0].as_of_date)} · ${escapeHtml(money(values[0]))}</span><strong>${escapeHtml(latest.as_of_date)} · ${escapeHtml(money(latest.total_equity))} · ${escapeHtml(percent(latest.return_pct))}</strong></div>`;
}

function renderRunControls(dashboard) {
  const history = $("paperRunHistory");
  const left = $("paperCompareLeft");
  const right = $("paperCompareRight");
  const runs = Array.isArray(dashboard.runs) ? dashboard.runs : [];
  const options = paperRunOptions(runs);
  for (const target of [history, left, right]) {
    populatePaperRunSelect(target, runs, options);
  }
  selectPaperRunDefaults(history, left, right, runs, dashboard.selected_run_id);
  syncPaperExportLinks(Number(dashboard.selected_run_id || 0));
}

function paperRunOptions(runs) {
  return runs.map((run) => `<option value="${escapeHtml(run.id)}">#${escapeHtml(run.id)} · ${escapeHtml(String(run.as_of || "").slice(0, 10))} · ${escapeHtml(run.cost_profile_name || run.cost_profile_id)}</option>`).join("");
}

function populatePaperRunSelect(target, runs, options) {
  if (!target) return;
  const selected = String(target.value || "");
  target.innerHTML = `<option value="">选择运行</option>${options}`;
  if (runs.some((run) => String(run.id) === selected)) target.value = selected;
}

function selectPaperRunDefaults(history, left, right, runs, selectedRunId) {
  if (history && selectedRunId) history.value = String(selectedRunId);
  if (left && !left.value && runs[1]) left.value = String(runs[1].id);
  if (right && !right.value && runs[0]) right.value = String(runs[0].id);
}

function syncPaperExportLinks(runId) {
  const jsonLink = $("paperExportJson");
  const tradeLink = $("paperExportTrades");
  const eventLink = $("paperExportEvents");
  if (jsonLink) jsonLink.href = runId ? `/api/paper-trading/runs/${runId}/export.json` : "#";
  if (tradeLink) tradeLink.href = runId ? `/api/paper-trading/runs/${runId}/export.csv?dataset=trades` : "#";
  if (eventLink) eventLink.href = runId ? `/api/paper-trading/runs/${runId}/export.csv?dataset=events` : "#";
}

function renderRunMetadata(dashboard) {
  const target = $("paperRunMetadata");
  if (!target) return;
  const run = (dashboard.runs || []).find((item) => item.id === dashboard.selected_run_id);
  if (!run) {
    target.innerHTML = emptyState("尚无运行快照", "加入策略并运行后保存规则、成本、数据和输入指纹。");
    return;
  }
  const performance = dashboard.performance || {};
  const warnings = [performance.sample_warning, performance.risk_metric_message, run.benchmark_message].filter(Boolean);
  target.innerHTML = `<dl class="paper-run-metadata">
    <div><dt>运行 / 截至</dt><dd>#${escapeHtml(run.id)} · ${escapeHtml(run.as_of)}</dd></div>
    <div><dt>规则 / 成本</dt><dd>${escapeHtml(run.rule_version)} · ${escapeHtml(run.cost_profile_name)} · ${escapeHtml(run.cost_profile_version)}</dd></div>
    <div><dt>数据区间 / 来源</dt><dd>${escapeHtml(run.data_start_date || "--")} → ${escapeHtml(run.data_end_date || "--")} · ${escapeHtml((run.data_sources || []).join("、") || "--")}</dd></div>
    <div><dt>输入指纹</dt><dd><code>${escapeHtml(run.input_fingerprint || "--")}</code></dd></div>
    <div><dt>基准</dt><dd>${escapeHtml(run.benchmark_symbol || "未配置")} · ${escapeHtml(run.benchmark_status || "unavailable")}</dd></div>
  </dl>${warnings.length ? `<ul class="paper-run-warnings">${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}`;
}

function renderRunComparison(comparison) {
  const target = $("paperRunComparison");
  if (!target) return;
  if (!comparison?.left_run || !comparison?.right_run) {
    target.innerHTML = emptyState("等待运行对比", "选择两个不可变运行，比较规则或成本情景差异。");
    return;
  }
  const deltas = comparison.deltas || {};
  target.innerHTML = tableHtml(
    ["指标", `#${comparison.left_run.id}`, `#${comparison.right_run.id}`, "差值（右-左）"],
    [
      ["净收益", percent(comparison.left_performance.total_return_pct), percent(comparison.right_performance.total_return_pct), signedPercent(deltas.total_return_pct)],
      ["毛收益", percent(comparison.left_performance.gross_return_pct), percent(comparison.right_performance.gross_return_pct), signedPercent(deltas.gross_return_pct)],
      ["超额收益", percent(comparison.left_performance.excess_return_pct), percent(comparison.right_performance.excess_return_pct), signedPercent(deltas.excess_return_pct)],
      ["最大回撤", percent(comparison.left_performance.max_drawdown_pct), percent(comparison.right_performance.max_drawdown_pct), signedPercent(deltas.max_drawdown_pct)],
      ["总成本", money(comparison.left_performance.total_cost), money(comparison.right_performance.total_cost), signedMoney(deltas.total_cost)],
      ["费用 / 毛利润", optionalPercent(comparison.left_performance.cost_to_gross_profit_pct), optionalPercent(comparison.right_performance.cost_to_gross_profit_pct), optionalPercent(deltas.cost_to_gross_profit_pct)],
      ["胜率", percent(comparison.left_performance.win_rate_pct), percent(comparison.right_performance.win_rate_pct), signedPercent(deltas.win_rate_pct)],
      ["利润因子", optionalNumber(comparison.left_performance.profit_factor), optionalNumber(comparison.right_performance.profit_factor), optionalNumber(deltas.profit_factor)],
    ]
  );
}

function renderNotes(notes) {
  const target = $("paperTradingNotes");
  if (!target) return;
  const rows = Array.isArray(notes) ? notes : [];
  target.innerHTML = rows.map((note) => `<li>${escapeHtml(note)}</li>`).join("");
}

function paperPlanOption(plan, used) {
  const key = `${plan.id}:${plan.revision}`;
  const suffix = used.has(key) ? "（已加入）" : "";
  return `<option value="${escapeHtml(plan.id)}" ${used.has(key) ? "disabled" : ""}>${escapeHtml(plan.symbol || "--")} · ${escapeHtml(plan.hypothesis || `计划 #${plan.id}`)} · v${escapeHtml(plan.revision || 1)}${suffix}</option>`;
}

function tableHtml(headers, rows) {
  return `<table class="paper-data-table"><thead><tr>${headers.map((item) => `<th>${escapeHtml(item)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((item) => `<td>${escapeHtml(item ?? "--")}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function emptyState(title, detail) {
  return `<div class="review-plan-state"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

function renderPaperLoading() {
  const target = $("paperTradingSummary");
  if (target) target.innerHTML = emptyState("正在读取模拟账户", "");
}

function renderPaperUnavailable(error) {
  const target = $("paperTradingSummary");
  const message = Number(error?.status) >= 500 ? "模拟交易暂不可用" : error?.message || "模拟交易暂不可用";
  if (target) target.innerHTML = emptyState("模拟交易暂不可用", message);
  setPaperFeedback(message, "error");
}

function setPaperFeedback(message, tone = "") {
  const target = $("paperTradingFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
}

function positiveNumber(value, message) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(message);
  return number;
}

function finiteNumber(value, fallback, message) {
  if (value == null || String(value).trim() === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(message);
  return number;
}

function numberText(value, digits = 2) {
  return formatNumber(value, digits);
}

function money(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "--";
}

function percent(value) {
  return Number.isFinite(Number(value)) ? `${formatNumber(value)}%` : "--";
}

function optionalNumber(value) {
  return Number.isFinite(Number(value)) ? formatNumber(value) : "--";
}

function optionalPercent(value) {
  return value == null ? "--" : percent(value);
}

function signedPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${formatNumber(number)}%`;
}

function signedMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${money(number)}`;
}
