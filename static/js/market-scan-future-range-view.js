import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { isMarketScanTop100RefreshRun } from "./market-scan-contracts.js";

const GENERATION_STATUSES = new Set(["ready", "not_generated", "insufficient_data"]);
const OFFSET_VALUES = Object.freeze([1, 2, 3]);
const GROUP_CHOICES = Object.freeze(["top20", "top50", "top100", "all"]);

export function marketScanFutureRangeElements(root) {
  const get = (id) => requiredElement(root, id);
  const offsetControl = get("marketScanFutureRangeOffsetControl");
  const pathControl = get("marketScanFutureRangePathControl");
  return {
    research: get("marketScanFutureRangeResearch"), summaryStatus: get("marketScanFutureRangeSummaryStatus"),
    refresh: get("marketScanFutureRangeRefresh"), offsetControl, pathControl,
    offsetInputs: [...offsetControl.querySelectorAll("input")], pathInputs: [...pathControl.querySelectorAll("input")],
    group: get("marketScanFutureRangeGroup"), evidenceStatus: get("marketScanFutureRangeEvidenceStatus"),
    evidenceCount: get("marketScanFutureRangeEvidenceCount"), coverage: get("marketScanFutureRangeCoverage"),
    state: get("marketScanFutureRangeState"), content: get("marketScanFutureRangeContent"),
    metrics: get("marketScanFutureRangeMetrics"), groups: get("marketScanFutureRangeGroups"),
    probability: get("marketScanFutureRangeProbability"), keyword: get("marketScanFutureRangeKeyword"),
    details: get("marketScanFutureRangeDetails"), detailsHelp: get("marketScanFutureRangeDetailsHelp"),
    pagination: get("marketScanFutureRangePagination"), pageText: get("marketScanFutureRangePageText"),
    prev: get("marketScanFutureRangePrev"), next: get("marketScanFutureRangeNext"),
    limitations: get("marketScanFutureRangeLimitations"),
  };
}

export function selectedFutureRangeOptions(elements) {
  const offsetInput = elements.offsetInputs.find((input) => input.checked);
  const pathInput = elements.pathInputs.find((input) => input.checked);
  const offset = OFFSET_VALUES.includes(Number(offsetInput?.value)) ? Number(offsetInput.value) : 1;
  const path = pathInput?.value === "cumulative_path" ? "cumulative_path" : "specified_day";
  const group = GROUP_CHOICES.includes(elements.group.value) ? elements.group.value : "top100";
  return { offset, path, group, keyword: String(elements.keyword.value || "").trim() };
}

export function normalizeMarketScanFutureRangeResponse(value, expectedRunId) {
  const payload = requiredObject(value, "未来区间响应");
  const generation = String(payload.generation_status || "");
  if (!GENERATION_STATUSES.has(generation)) throw futureRangeContractError("generation_status 无效");
  const research = payload.research === null || payload.research === undefined
    ? null : requiredObject(payload.research, "未来区间响应.research");
  if (generation === "not_generated" && research !== null) throw futureRangeContractError("未生成状态不能携带 research");
  validateResearchRun(research, expectedRunId);
  return {
    ...payload, schema_version: String(payload.schema_version || ""), generation_status: generation,
    artifact: normalizeArtifact(payload.artifact), research,
    record_page: normalizeRecordPage(payload.record_page, research, expectedRunId),
  };
}

export function renderFutureRangeRun(elements, run) {
  const official = run?.mode === "official";
  const top100Refresh = isMarketScanTop100RefreshRun(run);
  setAttribute(elements.research, "aria-busy", "false");
  setData(elements.research, "generationStatus", "not_generated");
  elements.content.hidden = true;
  elements.pagination.hidden = true;
  elements.refresh.disabled = !official || top100Refresh;
  setText(elements.evidenceStatus, "尚未生成");
  setText(elements.evidenceCount, "--");
  setText(elements.coverage, official && !top100Refresh ? `盘后正式批次 #${run.id}` : "仅盘后正式全市场批次");
  if (run && !official) return renderUnavailable(elements, "盘中临时批次不可用，请切换到盘后正式榜单。", "盘中不可用");
  if (top100Refresh) return renderUnavailable(elements, "TOP100 快速更新不是全市场快照，请切换到其来源盘后正式全市场榜单。", "非全市场批次");
  const message = run ? "展开后读取当前盘后正式批次的冻结研究证据。" : "暂无可验证的盘后正式批次。";
  setState(elements, message, run ? "" : "warn");
  setSummaryStatus(elements, run ? "展开查看" : "等待盘后正式", run ? "idle" : "warn");
}

export function renderFutureRangeLoading(elements, pageOnly = false) {
  setAttribute(elements.research, "aria-busy", "true");
  elements.refresh.disabled = true;
  setAttribute(elements.refresh, "aria-busy", "true");
  setSummaryStatus(elements, "读取中", "busy");
  setState(elements, pageOnly ? "正在读取该页个股明细…" : "正在读取冻结的未来区间研究证据…", "");
}

export function renderFutureRangeFailure(elements, message) {
  setAttribute(elements.research, "aria-busy", "false");
  elements.refresh.disabled = false;
  setAttribute(elements.refresh, "aria-busy", "false");
  elements.content.hidden = true;
  elements.pagination.hidden = true;
  setSummaryStatus(elements, "证据不可用", "error");
  setState(elements, `未来区间证据读取失败：${message}`, "error");
}

export function renderMarketScanFutureRange(elements, payload, options) {
  setAttribute(elements.research, "aria-busy", "false");
  elements.refresh.disabled = false;
  setAttribute(elements.refresh, "aria-busy", "false");
  setData(elements.research, "generationStatus", payload.generation_status);
  if (payload.generation_status === "not_generated") return renderNotGenerated(elements);
  const research = payload.research || {};
  const selected = matchingGroup(research.groups, options.offset, options.group);
  renderEvidenceContext(elements, payload, research, selected);
  renderMetricCards(elements, research, selected, options);
  renderGroups(elements, research.groups, options);
  renderProbability(elements, research.probability_context, options.offset);
  renderDetails(elements, payload.record_page, options);
  setText(elements.limitations, limitationText(research.limitations, payload));
  elements.content.hidden = false;
  const insufficient = payload.generation_status === "insufficient_data" || research.status === "insufficient_data";
  setSummaryStatus(elements, insufficient ? "独立日期不足" : "冻结证据可用", insufficient ? "warn" : "ready");
  setState(elements, insufficient ? "已有描述性观察，但独立日期不足，不能据此判定策略有效。" : "", insufficient ? "warn" : "");
  elements.state.hidden = !insufficient;
}

function renderNotGenerated(elements) {
  elements.content.hidden = true;
  elements.pagination.hidden = true;
  setText(elements.evidenceStatus, "尚未生成");
  setText(elements.evidenceCount, "--");
  setSummaryStatus(elements, "尚未生成", "warn");
  setState(elements, "该批次没有未来区间研究归档；旧批次会安全降级，不显示 0 或 50% 占位值。", "warn");
}

function renderUnavailable(elements, message, summary) {
  setSummaryStatus(elements, summary, "warn");
  setState(elements, message, "warn");
}

function renderEvidenceContext(elements, payload, research, selected) {
  const ready = payload.generation_status === "ready" && research.status === "ok";
  setText(elements.evidenceStatus, ready ? "冻结证据可用" : "证据不足");
  setText(elements.evidenceCount, countPair(selected?.independent_session_count, selected?.sample_size ?? research.record_count));
  const run = objectValue(research.run);
  const source = objectValue(research.source);
  const date = run.data_date || run.quote_date || "--";
  setText(elements.coverage, `盘后正式 #${run.run_id || "--"} · ${date} · ${source.adjustment_mode || "qfq"}`);
}

function renderMetricCards(elements, research, group, options) {
  const metrics = objectValue(group?.metrics);
  const rank = matchingMetric(research.rank_ic, options.offset, "level_shift_hlc3_proxy");
  const monotonic = matchingMetric(research.monotonicity, options.offset, "level_shift_hlc3_proxy");
  const rangeCards = options.path === "cumulative_path" ? cumulativeMetricCards(metrics, rank, monotonic) : specifiedMetricCards(metrics, rank, monotonic);
  const cards = [...rangeCards, ...executionAggregateCards(metrics, options.offset)];
  elements.metrics.innerHTML = cards.map(metricCard).join("");
}

function specifiedMetricCards(metrics, rank, monotonic) {
  return [
    metricDefinition("最低位变化", metrics.level_shift_low, "D+目标日低点相对 D 日低点"),
    metricDefinition("典型价变化", metrics.level_shift_hlc3_proxy, "HLC3 典型价代理 · 非 VWAP"),
    metricDefinition("最高位变化", metrics.level_shift_high, "D+目标日高点相对 D 日高点"),
    metricDefinition("目标收盘收益", metrics.terminal_close_return, "以 D+1 可交易开盘为参考"),
    metricDefinition("累计 MFE", metrics.mfe, "截至目标日最大有利波动"),
    metricDefinition("累计 MAE", metrics.mae, "截至目标日最大不利波动"),
    rankDefinition(rank), monotonicDefinition(monotonic),
  ];
}

function cumulativeMetricCards(metrics, rank, monotonic) {
  return [
    metricDefinition("累计 MAE", metrics.mae, "D+1 开盘至目标日最差路径"),
    metricDefinition("累计 MFE", metrics.mfe, "D+1 开盘至目标日最佳路径"),
    metricDefinition("终值收盘收益", metrics.terminal_close_return, "目标日收盘相对 D+1 开盘"),
    metricDefinition("典型价区间平移", metrics.level_shift_hlc3_proxy, "HLC3 典型价代理 · 非 VWAP"),
    metricDefinition("最低位变化", metrics.level_shift_low, "目标日低点相对 D 日低点"),
    metricDefinition("最高位变化", metrics.level_shift_high, "目标日高点相对 D 日高点"),
    rankDefinition(rank), monotonicDefinition(monotonic),
  ];
}

function metricDefinition(label, summary, note) {
  const record = objectValue(summary);
  const value = finiteNumber(record.median) ?? finiteNumber(record.mean) ?? finiteNumber(record.value);
  const status = metricStatusText(record.status, value);
  return { label, value: percentageText(value), note: `${status} · ${record.median !== undefined ? "中位数" : "均值"} · ${intervalText(record.ci95)} · ${note}` };
}

function executionAggregateCards(metrics, offset) {
  if (offset === 1) return [{
    label: "可执行收益", value: "A股 T+1 不可执行", note: "D+1 高低点、HLC3、MFE/MAE 仅作区间诊断",
  }];
  return [
    metricDefinition("可执行净收益", metrics.net_return, `D+1 开盘至 D+${offset} 收盘 · 已计成本`),
    metricDefinition("可执行净超额", metrics.net_excess_return, "相对同批次等权市场基准"),
  ];
}

function rankDefinition(record) {
  const source = objectValue(record);
  return { label: "趋势 Rank IC", value: metricText(source.mean_rank_ic, 3), note: `${countText(source.independent_session_count)} 个独立日期 · ${intervalText(source.ci95, false)}` };
}

function monotonicDefinition(record) {
  const source = objectValue(record);
  const passed = typeof source.passed === "boolean" ? (source.passed ? "通过" : "未通过") : "证据不足";
  return { label: "十分位单调性", value: passed, note: `Spearman ${metricText(source.spearman, 3)} · ${countText(source.independent_session_count)} 个独立日期` };
}

function metricCard(card) {
  return `<div><span>${escapeHtml(card.label)}</span><strong>${escapeHtml(card.value)}</strong><small>${escapeHtml(card.note)}</small></div>`;
}

function renderGroups(elements, value, options) {
  const groups = arrayValue(value).filter((group) => Number(group.session_offset) === options.offset).sort(compareGroups);
  if (!groups.length) {
    elements.groups.innerHTML = '<tr><td colspan="7">当前周期暂无分组证据</td></tr>';
    return;
  }
  elements.groups.innerHTML = groups.slice(0, 14).map((group) => groupRow(group, options.group)).join("");
}

function groupRow(group, selectedKey) {
  const metrics = objectValue(group.metrics);
  const key = groupChoice(group);
  const count = countPair(group.independent_session_count, group.sample_size);
  const offset = Number(group.session_offset);
  return `<tr${key === selectedKey ? ' class="selected"' : ""}><th scope="row">${escapeHtml(groupLabel(group))}</th><td>${escapeHtml(count)}</td><td>${escapeHtml(summaryPercentage(metrics.level_shift_hlc3_proxy))}</td><td>${escapeHtml(summaryPercentage(metrics.mfe))}</td><td>${escapeHtml(summaryPercentage(metrics.mae))}</td><td>${escapeHtml(executionGroupText(metrics.net_return, offset))}</td><td>${escapeHtml(executionGroupText(metrics.net_excess_return, offset))}</td></tr>`;
}

function renderProbability(elements, value, offset) {
  const context = objectValue(value);
  if (context.status !== "available") {
    elements.probability.innerHTML = '<div><span>冻结概率证据</span><strong>尚不可对照</strong><small>不上屏 0 或 50% 占位值</small></div>';
    return;
  }
  const comparisons = probabilityComparisons(context, offset);
  if (!comparisons.length) {
    elements.probability.innerHTML = '<div><span>冻结概率证据</span><strong>上下文可用</strong><small>当前周期暂无聚合对照值</small></div>';
    return;
  }
  elements.probability.innerHTML = comparisons.slice(0, 6).map(probabilityCard).join("");
}

function probabilityComparisons(context, offset) {
  const source = arrayValue(context.comparisons).length ? context.comparisons : context.metrics;
  if (Array.isArray(source)) return source.filter((item) => !item.session_offset || Number(item.session_offset) === offset);
  return Object.entries(objectValue(source)).map(([label, value]) => ({ label, value }));
}

function probabilityCard(item) {
  const source = objectValue(item);
  const value = finiteNumber(source.value) ?? finiteNumber(source.rank_ic) ?? finiteNumber(source.correlation);
  return `<div><span>${escapeHtml(source.label || source.metric || "概率对照")}</span><strong>${escapeHtml(metricText(value, 3))}</strong><small>${escapeHtml(source.status === "insufficient_data" ? "独立日期不足" : "仅关联诊断，不改变排序")}</small></div>`;
}

function renderDetails(elements, page, options) {
  const records = filteredRecords(page.items, options.keyword);
  elements.detailsHelp.textContent = `目标 D+${options.offset} · ${options.path === "cumulative_path" ? "累计路径" : "指定日区间"} · 每页 20 条`;
  if (!records.length) elements.details.innerHTML = '<p class="market-scan-future-range-state">当前页没有匹配的个股明细。</p>';
  else elements.details.innerHTML = `<div class="market-scan-future-range-detail-list">${records.map((record) => detailCard(record, options)).join("")}</div>`;
  renderRecordPagination(elements, page);
}

function detailCard(record, options) {
  const offset = arrayValue(record.offsets).find((item) => Number(item.session_offset) === options.offset) || {};
  const identity = `${record.name || "--"} · ${record.symbol || "--"}`;
  const summary = detailSummary(offset, options.offset);
  const values = options.path === "cumulative_path" ? cumulativeDetailValues(record, offset) : specifiedDetailValues(record, offset);
  return `<details class="market-scan-future-range-detail-card"><summary><span><strong>${escapeHtml(identity)}</strong><small>冻结排名 ${escapeHtml(countText(record.rank))} · 趋势 ${escapeHtml(metricText(record.trend_score, 0))} · 概率 ${escapeHtml(recordProbabilityText(record.probability))}</small></span><em>${escapeHtml(summary)}</em></summary><dl class="market-scan-future-range-detail-grid">${values.map(detailValue).join("")}</dl></details>`;
}

function specifiedDetailValues(record, offset) {
  const shift = objectValue(offset.level_shift);
  const reference = objectValue(objectValue(offset.d1_open_reference).specified_day);
  return [
    ["信号日", objectValue(record.d_bar).date || "--"], ["目标日", offset.target_session_date || "--"],
    ["低点平移", percentageText(shift.low)], ["典型价平移", percentageText(shift.hlc3_proxy)],
    ["高点平移", percentageText(shift.high)], ["相对入场低点", percentageText(reference.low)],
    ["相对入场典型价", percentageText(reference.hlc3_proxy)], ["相对入场收盘", percentageText(reference.close)],
    ["区间重叠", percentageText(objectValue(offset.interval_structure).overlap_ratio)],
    ...executionDetailValues(offset.execution, Number(offset.session_offset)),
  ];
}

function cumulativeDetailValues(record, offset) {
  const entry = objectValue(offset.d1_open_reference);
  const path = objectValue(entry.cumulative_path);
  return [
    ["信号日", objectValue(record.d_bar).date || "--"], ["入场日", entry.entry_date || "--"],
    ["目标日", offset.target_session_date || "--"], ["参考开盘", priceText(entry.entry_price)],
    ["累计 MAE", percentageText(path.mae)], ["累计 MFE", percentageText(path.mfe)],
    ["终值收盘", percentageText(path.terminal_close_return)], ["区间宽度", percentageText(objectValue(offset.interval_structure).normalized_width)],
    ["区间重叠", percentageText(objectValue(offset.interval_structure).overlap_ratio)],
    ...executionDetailValues(offset.execution, Number(offset.session_offset)),
  ];
}

function executionDetailValues(value, offset) {
  const execution = objectValue(value);
  if (offset === 1) return [
    ["可执行性", "A股 T+1 不可执行"], ["执行说明", "D+1 仅区间诊断，不作为可实现收益"],
  ];
  return [
    ["执行状态", executionStatusText(execution.status)], ["状态原因", executionReasonText(execution.reason)],
    ["入场 / 退出", `${execution.entry_date || "--"} / ${execution.exit_date || "--"}`],
    ["毛收益", percentageText(execution.gross_return)], ["成本拖累", percentageText(execution.cost_drag)],
    ["净收益", percentageText(execution.net_return)], ["市场基准净收益", percentageText(execution.market_benchmark_net_return)],
    ["净超额收益", percentageText(execution.net_excess_return)],
    ["成本模型", executionCostText(execution)],
  ];
}

function detailValue([label, value]) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

function renderRecordPagination(elements, page) {
  elements.pagination.hidden = page.total <= 0;
  setText(elements.pageText, `第 ${page.page}/${Math.max(page.page_count, 1)} 页 · 共 ${page.total} 条`);
  elements.prev.disabled = page.page <= 1;
  elements.next.disabled = page.page_count === 0 || page.page >= page.page_count;
}

function matchingGroup(value, offset, choice) {
  const groups = arrayValue(value).filter((item) => Number(item.session_offset) === offset);
  return groups.find((item) => groupChoice(item) === choice) || groups.find((item) => groupChoice(item) === "all") || null;
}

function matchingMetric(value, offset, metric) {
  return arrayValue(value).find((item) => Number(item.session_offset) === offset && String(item.metric) === metric) || null;
}

function groupChoice(group) {
  if (group.group_type === "all") return "all";
  if (group.group_type === "top_n") return `top${Number(group.group_value)}`;
  return group.group_type === "decile" ? String(group.group_value || "").toLowerCase() : "";
}

function groupLabel(group) {
  if (group.group_type === "all") return "全市场";
  if (group.group_type === "top_n") return `Top${Number(group.group_value)}`;
  if (group.group_type === "decile") return String(group.group_value || "十分位").toUpperCase();
  return String(group.group_value || group.group_type || "--");
}

function compareGroups(left, right) {
  const order = { top20: 1, top50: 2, top100: 3, all: 4 };
  const leftKey = groupChoice(left); const rightKey = groupChoice(right);
  return (order[leftKey] || 10 + decileNumber(leftKey)) - (order[rightKey] || 10 + decileNumber(rightKey));
}

function normalizeRecordPage(value, research, runId) {
  const fallbackItems = arrayValue(research?.records);
  const source = value === null || value === undefined ? {} : requiredObject(value, "未来区间响应.record_page");
  const items = source.items === undefined ? fallbackItems : arrayValue(source.items);
  const page = positiveInteger(source.page, 1); const pageSize = positiveInteger(source.page_size, Math.max(items.length, 20));
  const total = nonnegativeInteger(source.total, items.length); const pageCount = nonnegativeInteger(source.page_count, total ? Math.ceil(total / pageSize) : 0);
  items.forEach((item) => validateRecordRun(item, runId));
  return { ...source, page, page_size: pageSize, total, page_count: pageCount, session_offset: Number(source.session_offset) || null, symbol: source.symbol || null, items };
}

function normalizeArtifact(value) {
  if (value === null || value === undefined) return null;
  const source = requiredObject(value, "未来区间响应.artifact");
  return { schema_version: String(source.schema_version || ""), generated_at: source.generated_at || null, integrity_digest: source.integrity_digest || null };
}

function validateResearchRun(research, expectedRunId) {
  if (!research) return;
  const run = requiredObject(research.run, "未来区间响应.research.run");
  if (Number(run.run_id) !== Number(expectedRunId)) throw futureRangeContractError("research.run_id 与请求批次不匹配");
  if (run.mode !== "official") throw futureRangeContractError("research 只能来自盘后正式批次");
}

function validateRecordRun(record, runId) {
  const source = requiredObject(record, "未来区间响应.record_page.items[]");
  if (source.run_id !== undefined && Number(source.run_id) !== Number(runId)) throw futureRangeContractError("明细 run_id 与请求批次不匹配");
  if (!Array.isArray(source.offsets)) throw futureRangeContractError("明细 offsets 必须是数组");
}

function filteredRecords(value, keyword) {
  const items = arrayValue(value); const query = String(keyword || "").trim().toLowerCase();
  if (!query) return items;
  const compact = query.replace(/[.\-\s]/g, "");
  return items.filter((item) => {
    const text = `${item.symbol || ""} ${item.name || ""}`.toLowerCase();
    return text.includes(query) || text.replace(/[.\-\s]/g, "").includes(compact);
  });
}

function recordProbabilityText(value) {
  const predictions = arrayValue(objectValue(value).predictions);
  const available = predictions.find((item) => finiteNumber(item.probability) !== null);
  return available ? percentageText(available.probability) : "证据不足";
}

function limitationText(value, payload) {
  const limitations = arrayValue(value).filter((item) => typeof item === "string" && item.trim());
  const integrity = payload.artifact?.integrity_digest ? `证据摘要 ${String(payload.artifact.integrity_digest).slice(0, 12)}…` : "";
  return ["HLC3 是典型价代理（非 VWAP）；高低点与 MFE/MAE 是路径诊断，不代表可实现收益。", ...limitations, integrity].filter(Boolean).join(" ");
}

function summaryPercentage(value) {
  const source = objectValue(value);
  return percentageText(finiteNumber(source.median) ?? finiteNumber(source.mean) ?? finiteNumber(source.value));
}

function executionGroupText(value, offset) {
  if (offset === 1) return "T+1 不可执行";
  const source = objectValue(value);
  const estimate = finiteNumber(source.median) ?? finiteNumber(source.mean) ?? finiteNumber(source.value);
  const status = metricStatusText(source.status, estimate);
  return `${status} · ${percentageText(estimate)}`;
}

function metricStatusText(status, value) {
  if (status === "ok") return "证据可用";
  if (status === "insufficient_data") return "证据不足";
  return value === null ? "尚无证据" : "描述值";
}

function detailSummary(offset, sessionOffset) {
  if (offset.fixed_session_status !== "available") return fixedSessionLabel(offset.fixed_session_status);
  if (sessionOffset === 1) return "D+1 · 仅区间诊断";
  return `D+${sessionOffset} · ${executionStatusText(objectValue(offset.execution).status)}`;
}

function executionStatusText(status) {
  if (status === "modelled") return "已建模";
  if (status === "unfilled") return "未成交";
  if (status === "data_unavailable") return "执行数据不可用";
  return "--";
}

function executionReasonText(reason) {
  const labels = {
    A_share_T_plus_1_no_same_session_exit: "A股 T+1：买入当日不可卖出",
    locked_limit_up: "涨停封单，无法假设买入", locked_limit_down: "跌停封单，无法假设卖出",
    suspended_or_zero_volume: "停牌或零成交量", daily_capacity_limit: "超过日成交容量限制",
    target_date_missing: "目标交易日尚未成熟", entry_date_missing: "入场交易日缺失",
    entry_or_previous_bar_missing: "入场行情证据缺失", exit_or_previous_bar_missing: "退出行情证据缺失",
    entry_rule_profile_degraded: "入场交易规则不完整", exit_rule_profile_degraded: "退出交易规则不完整",
  };
  return reason ? (labels[reason] || String(reason)) : "--";
}

function executionCostText(execution) {
  const parts = [execution.cost_profile_id, execution.cost_model_version, execution.execution_model]
    .map((value) => String(value || "").trim()).filter(Boolean);
  return parts.length ? parts.join(" · ") : "--";
}

function intervalText(value, percentage = true) {
  if (!Array.isArray(value) || value.length !== 2 || value.some((item) => finiteNumber(item) === null)) return "CI --";
  const format = percentage ? percentageText : (item) => metricText(item, 3);
  return `95% CI ${format(value[0])}–${format(value[1])}`;
}

function percentageText(value) {
  const number = finiteNumber(value);
  return number === null ? "--" : `${number > 0 ? "+" : ""}${formatNumber(number * 100, 2)}%`;
}

function metricText(value, digits = 2) {
  const number = finiteNumber(value);
  return number === null ? "--" : formatNumber(number, digits);
}

function priceText(value) {
  const number = finiteNumber(value);
  return number === null ? "--" : formatNumber(number, 2);
}

function countPair(sessions, observations) {
  return `${countText(sessions)} / ${countText(observations)}`;
}

function countText(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? String(number) : "--";
}

function fixedSessionLabel(value) {
  if (value === "not_mature") return "目标日未成熟";
  if (value === "unavailable") return "目标日不可用";
  return "无有效目标日";
}

function decileNumber(value) {
  const match = String(value).match(/q(\d+)/i);
  return match ? Number(match[1]) : 99;
}

function setSummaryStatus(elements, text, kind) {
  setText(elements.summaryStatus, text);
  setData(elements.summaryStatus, "kind", kind);
}

function setState(elements, text, kind) {
  elements.state.hidden = false;
  setText(elements.state, text);
  setData(elements.state, "kind", kind);
}

function setData(element, name, value) {
  if (element?.dataset) element.dataset[name] = String(value);
  else element?.setAttribute?.(`data-${name.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`)}`, String(value));
}

function setAttribute(element, name, value) {
  if (typeof element?.setAttribute === "function") element.setAttribute(name, String(value));
  else if (element) element[name] = String(value);
}

function setText(element, value) { element.textContent = String(value ?? "--"); }
function finiteNumber(value) { const number = Number(value); return value === null || value === undefined || value === "" || typeof value === "boolean" || !Number.isFinite(number) ? null : number; }
function positiveInteger(value, fallback) { const number = Number(value); return Number.isInteger(number) && number > 0 ? number : fallback; }
function nonnegativeInteger(value, fallback) { const number = Number(value); return Number.isInteger(number) && number >= 0 ? number : fallback; }
function arrayValue(value) { return Array.isArray(value) ? value : []; }
function objectValue(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }

function requiredElement(root, id) {
  const element = root.getElementById(id);
  if (!element) throw new Error(`缺少未来区间验证界面元素：${id}`);
  return element;
}

function requiredObject(value, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw futureRangeContractError(`${path} 必须是对象`);
  return value;
}

function futureRangeContractError(message) {
  const error = new Error(`未来区间接口响应格式异常：${message}`);
  error.name = "MarketScanFutureRangeContractError";
  return error;
}
