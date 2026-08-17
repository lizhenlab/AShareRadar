import { escapeHtml } from "./dom.js";
import { formatAmount, formatNumber } from "./format.js";

const RANGE_LABELS = Object.freeze({
  score: "趋势强度",
  trend_score: "基础趋势分",
  change_pct: "涨跌幅",
  turnover_rate: "换手率",
  amount: "成交额",
  data_quality_score: "数据质量",
  confidence: "置信度研究分",
  risk: "风险研究分",
  tradability: "可交易性研究分",
});

const STATUS_LABELS = Object.freeze({ success: "有效排名", missing: "数据缺失", skipped: "已跳过", pending: "待处理" });
const MARKET_LABELS = Object.freeze({ SH: "沪市", SZ: "深市", BJ: "北交所" });

export function createMarketScanScreeningView(root) {
  const elements = screeningElements(root);
  return {
    elements,
    renderBreadth: (payload) => renderBreadth(elements, payload),
    renderBreadthError: (message) => renderRegionError(elements.breadth, message),
    renderCohortDiff: (payload) => renderCohortDiff(elements, payload),
    renderCohortDiffError: (message) => renderRegionError(elements.diff, message),
    renderEvaluation: (payload) => renderEvaluation(elements, payload),
    renderEvaluationError: (message) => renderRegionError(elements.evaluation, message),
    renderLoading: (runId) => renderLoading(elements, runId),
    renderNoRun: () => renderNoRun(elements),
    renderRequestFinished: () => renderRequestFinished(elements),
    renderScreenSpec: (spec) => renderScreenSpec(elements, spec),
    setColumnView: (value) => setColumnView(elements, value),
  };
}

export function renderScreenSpecChips(spec) {
  const chips = [];
  chips.push(spec.status ? STATUS_LABELS[spec.status] || spec.status : "全部状态");
  chips.push(spec.markets.length ? spec.markets.map((value) => MARKET_LABELS[value] || value).join("、") : "全部市场");
  if (spec.industries.length) chips.push(`行业：${spec.industries.join("、")}`);
  if (spec.is_st !== null) chips.push(spec.is_st ? "仅 ST" : "排除 ST");
  if (spec.is_new !== null) chips.push(spec.is_new ? "仅新股" : "排除新股");
  if (spec.keyword) chips.push(`搜索：${spec.keyword}`);
  Object.entries(spec.ranges).forEach(([field, range]) => chips.push(rangeChip(field, range)));
  spec.sort.forEach((sort, index) => chips.push(`${index + 1}级排序：${RANGE_LABELS[sort.field] || sort.field}${sort.order === "desc" ? "降序" : "升序"}`));
  return chips;
}

export function screeningNumber(value, digits = 0) {
  if (value === null || value === undefined) return "--";
  const number = Number(value);
  return Number.isFinite(number) ? formatNumber(number, digits) : "--";
}

function renderLoading(elements, runId) {
  elements.shell.setAttribute("aria-busy", "true");
  elements.refresh.disabled = true;
  resetScreenEvidence(elements, `正在绑定批次 #${runId}`);
  elements.summaryStatus.dataset.kind = "loading";
  elements.summaryStatus.textContent = `读取批次 #${runId}`;
  elements.feedback.className = "market-scan-screening-feedback loading";
  elements.feedback.textContent = "正在读取冻结市场宽度并评估当前筛选条件…";
  for (const region of [elements.breadth, elements.evaluation, elements.diff]) {
    region.innerHTML = '<p class="market-scan-screening-region-state">正在读取…</p>';
  }
}

function renderNoRun(elements) {
  elements.shell.setAttribute("aria-busy", "false");
  elements.refresh.disabled = true;
  resetScreenEvidence(elements, "尚未绑定冻结批次");
  elements.summaryStatus.dataset.kind = "idle";
  elements.summaryStatus.textContent = "暂无冻结榜单";
  elements.feedback.className = "market-scan-screening-feedback";
  elements.feedback.textContent = "请先选择一个已发布榜单；工作台不会请求当前行情来补造证据。";
  for (const region of [elements.breadth, elements.evaluation, elements.diff]) region.innerHTML = "";
}

function resetScreenEvidence(elements, message) {
  elements.evidence.textContent = message;
  elements.spec.innerHTML = "";
  elements.spec.removeAttribute("aria-label");
}

function renderRequestFinished(elements) {
  elements.shell.setAttribute("aria-busy", "false");
  elements.refresh.disabled = false;
  if (elements.summaryStatus.dataset.kind === "loading") {
    elements.summaryStatus.dataset.kind = "ready";
    elements.summaryStatus.textContent = "冻结证据已读取";
  }
}

function renderScreenSpec(elements, spec) {
  const chips = renderScreenSpecChips(spec);
  elements.spec.innerHTML = chips.map((chip) => `<span>${escapeHtml(chip)}</span>`).join("");
  elements.spec.setAttribute("aria-label", `当前筛选条件：${chips.join("；")}`);
}

function renderBreadth(elements, payload) {
  const { population, score, change, evidence } = payload;
  elements.evidence.textContent = `${evidence.mode} · 数据日 ${evidence.data_date} · ${evidence.rule_version} · 摘要 ${payload.canonical_digest.slice(0, 12)}`;
  const statusSuccess = ownCount(population.by_status, "success");
  const scoreCoverage = `${screeningNumber(score.present_count)}/${screeningNumber(population.total)}`;
  const cards = [
    ["冻结股票池", screeningNumber(population.total)],
    ["有效排名", screeningNumber(statusSuccess)],
    ["分数覆盖", scoreCoverage],
    ["平均趋势强度", screeningNumber(score.mean, 2)],
    ["上涨 / 下跌", `${screeningNumber(change.advancing)} / ${screeningNumber(change.declining)}`],
    ["涨跌幅缺失", screeningNumber(change.missing)],
  ];
  elements.breadth.innerHTML = `<div class="market-scan-screening-metrics">${metricCards(cards)}</div>
    <div class="market-scan-screening-evidence-grid">
      <section><h5>趋势强度分布</h5>${scoreHistogram(score.bins, score.missing_count)}</section>
      <section><h5>市场构成</h5>${countList(population.by_market, MARKET_LABELS)}</section>
      <section class="market-scan-screening-industries"><h5>行业横截面</h5>${industryTable(payload.industries)}</section>
    </div>`;
}

function renderEvaluation(elements, payload) {
  elements.summaryStatus.dataset.kind = "ready";
  elements.summaryStatus.textContent = `命中 ${payload.matched_count}/${payload.population_count}`;
  elements.feedback.className = "market-scan-screening-feedback success";
  elements.feedback.textContent = `筛选摘要 ${payload.spec_digest.slice(0, 12)} · 证据摘要 ${payload.canonical_digest.slice(0, 12)} · 所有条件均在批次 #${payload.evidence.run_id} 的冻结行上评估。`;
  elements.evaluation.innerHTML = `<div class="market-scan-screening-section-head"><div><h5>筛选漏斗</h5><p>缺失值不会当作 0；有条件时按缺失原因淘汰。</p></div><strong>${screeningNumber(payload.matched_count)} 条命中</strong></div>
    ${funnelList(payload.funnel)}
    ${matchedExplanationList(payload.matched_explanations, payload.matched.items, payload.funnel)}
    <section class="market-scan-near-misses" aria-labelledby="marketScanNearMissTitle">
      <div class="market-scan-screening-section-head"><div><h5 id="marketScanNearMissTitle">近失候选</h5><p>仅差一个条件；趋势强度仍是序数研究状态，不代表上涨概率。</p></div></div>
      ${nearMissList(payload.near_misses)}
    </section>`;
}

function matchedExplanationList(explanations, items, funnel) {
  if (!explanations.length) return "";
  const names = new Map(items.map((item) => [item.symbol, item.name || item.code || item.symbol]));
  const labels = new Map(funnel.map((item) => [item.condition_code, item.label]));
  const rows = explanations.slice(0, 8).map((item) => `<li><strong>${escapeHtml(names.get(item.symbol) || item.symbol)}</strong><span>${escapeHtml(passedConditionText(item.passed_conditions, labels))}</span></li>`).join("");
  return `<section class="market-scan-matched-explanations" aria-labelledby="marketScanMatchedExplanationsTitle"><div class="market-scan-screening-section-head"><div><h5 id="marketScanMatchedExplanationsTitle">当前页入选理由</h5><p>条件代码来自与筛选漏斗相同的冻结条件编译器。</p></div></div><ul>${rows}</ul></section>`;
}

function passedConditionText(codes, labels) {
  if (codes.length === 1 && codes[0] === "all_conditions_passed") return "当前方案无额外条件，已通过基础股票池要求";
  return `通过：${codes.map((code) => labels.get(code) || code).join("、")}`;
}

function renderCohortDiff(elements, payload) {
  if (!payload || payload.status === "unavailable") {
    elements.diff.innerHTML = `<p class="market-scan-screening-region-state">${escapeHtml(deltaUnavailableLabel(payload?.unavailable_reason))}</p>`;
    return;
  }
  const bucket = payload.top_buckets.find((item) => item.top_n === 100) || payload.top_buckets.at(-1) || {};
  const changes = payload.rank_score_changes || [];
  const cards = [
    ["Top100 新进入", screeningNumber(bucket.entrants?.length)],
    ["Top100 退出", screeningNumber(bucket.exits?.length)],
    ["Top100 保留", screeningNumber(bucket.retained_count)],
    ["排名上升", screeningNumber(changes.filter((item) => Number(item.rank_change) > 0).length)],
  ];
  const items = deltaDisplayItems(bucket, changes);
  elements.diff.innerHTML = `<div class="market-scan-screening-section-head"><div><h5>同 cohort 变化</h5><p>批次 #${escapeHtml(payload.previous?.run_id ?? "--")} → #${escapeHtml(payload.current.run_id)}；仅比较相同模式、股票池与规则版本。</p></div></div>
    <div class="market-scan-screening-metrics compact">${metricCards(cards)}</div>${diffItems(items)}`;
}

function setColumnView(elements, value) {
  const supported = new Set(["overview", "trend", "liquidity", "risk", "research"]);
  const selected = supported.has(value) ? value : "overview";
  elements.table.dataset.columnView = selected;
  elements.tableWrap.setAttribute("aria-label", `全市场扫描榜单，${columnViewLabel(selected)}列视图`);
  elements.columnInputs.forEach((input) => { input.checked = input.value === selected; });
}

function metricCards(cards) {
  return cards.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function scoreHistogram(bins, missingCount) {
  if (!bins.length) return '<p class="market-scan-screening-region-state">没有可展示的分数区间。</p>';
  const maximum = Math.max(...bins.map((bin) => bin.count));
  const bars = bins.map((bin) => histogramBar(bin, maximum)).join("");
  return `<div class="market-scan-score-histogram" role="img" aria-label="趋势强度分布；缺失 ${screeningNumber(missingCount)} 条">${bars}</div>`;
}

function histogramBar(bin, maximum) {
  const width = maximum > 0 ? Math.max(2, (bin.count / maximum) * 100) : 0;
  const range = `${screeningNumber(bin.lower, 1)}–${screeningNumber(bin.upper, 1)}`;
  return `<div><span>${escapeHtml(range)}</span><i><b style="width:${width.toFixed(2)}%"></b></i><strong>${escapeHtml(screeningNumber(bin.count))}</strong></div>`;
}

function countList(counts, labels) {
  const entries = Object.entries(counts);
  if (!entries.length) return '<p class="market-scan-screening-region-state">暂无构成数据。</p>';
  return `<dl class="market-scan-screening-counts">${entries.map(([key, value]) => `<div><dt>${escapeHtml(labels[key] || key)}</dt><dd>${escapeHtml(screeningNumber(value))}</dd></div>`).join("")}</dl>`;
}

function industryTable(industries) {
  if (!industries.length) return '<p class="market-scan-screening-region-state">暂无行业数据。</p>';
  const rows = industries.slice(0, 10).map((item) => `<tr><td>${escapeHtml(item.industry || "未归类")}</td><td>${escapeHtml(screeningNumber(item.count))}</td><td>${escapeHtml(screeningNumber(item.score_present_count))}</td><td>${escapeHtml(screeningNumber(item.average_score, 2))}</td></tr>`).join("");
  return `<div class="market-scan-screening-table-wrap" tabindex="0"><table><thead><tr><th>行业</th><th>数量</th><th>有分数</th><th>平均趋势强度</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function funnelList(steps) {
  if (!steps.length) return '<p class="market-scan-screening-region-state">当前方案没有可执行条件。</p>';
  const rows = steps.map((step) => `<li><div><strong>${escapeHtml(step.label)}</strong><span>${escapeHtml(screeningNumber(step.input_count))} 输入</span></div><div class="market-scan-screening-funnel-bar"><i style="width:${funnelWidth(step)}%"></i></div><p><span>保留 ${escapeHtml(screeningNumber(step.matched_count))}</span><span>淘汰 ${escapeHtml(screeningNumber(step.excluded_count))}</span><span>缺失 ${escapeHtml(screeningNumber(step.missing_count))}</span></p></li>`).join("");
  return `<ol class="market-scan-screening-funnel">${rows}</ol>`;
}

function nearMissList(items) {
  if (!items.length) return '<p class="market-scan-screening-region-state">当前没有仅差一个条件的候选。</p>';
  return `<div class="market-scan-near-miss-list">${items.map(nearMissCard).join("")}</div>`;
}

function nearMissCard(record) {
  const item = record.item || {};
  const failures = record.failed_conditions.map((failure) => `${failure.label}${failure.missing ? "（证据缺失）" : ""}`).join("；");
  return `<article><div><strong>${escapeHtml(item.name || item.code || item.symbol || "--")}</strong><span>${escapeHtml(item.symbol || "--")}</span></div><dl><div><dt>排名</dt><dd>${escapeHtml(screeningNumber(item.rank))}</dd></div><div><dt>趋势强度</dt><dd>${escapeHtml(screeningNumber(item.score))}</dd></div><div><dt>成交额</dt><dd>${escapeHtml(item.amount === null || item.amount === undefined ? "--" : formatAmount(item.amount))}</dd></div></dl><p><strong>未命中：</strong>${escapeHtml(failures || "原因未提供")}</p></article>`;
}

function diffItems(items) {
  if (!items.length) return "";
  const rows = items.slice(0, 12).map((item) => `<li><strong>${escapeHtml(item.name || item.symbol || "--")}</strong><span>${escapeHtml(item.label || item.movement || "变化")}</span><small>${escapeHtml(item.reason || "同条件冻结比较")}</small></li>`).join("");
  return `<ul class="market-scan-screening-diff-list">${rows}</ul>`;
}

function deltaDisplayItems(bucket, changes) {
  const entrants = (bucket.entrants || []).map((item) => ({ ...item, label: "新进入 Top100", reason: reasonLabels(item.reason_codes) }));
  const exits = (bucket.exits || []).map((item) => ({ ...item, label: "退出 Top100", reason: reasonLabels(item.reason_codes) }));
  const unrankable = (bucket.present_but_unrankable || []).map((item) => ({ ...item, label: "当前不可排名", reason: reasonLabels(item.reason_codes) }));
  const movements = changes.filter((item) => Number(item.rank_change)).map((item) => ({
    ...item,
    label: Number(item.rank_change) > 0 ? `排名上升 ${Math.abs(item.rank_change)}` : `排名下降 ${Math.abs(item.rank_change)}`,
    reason: reasonLabels(item.reason_codes),
  }));
  return [...entrants, ...exits, ...unrankable, ...movements].slice(0, 12);
}

function reasonLabels(codes) {
  const labels = {
    crossed_into_top_n: "跨入阈值", crossed_out_of_top_n: "跌出阈值", became_rankable: "恢复可排名",
    instrument_new_in_current_universe: "当前股票池新增", instrument_absent_from_current_universe: "当前股票池不再存在",
    present_but_unrankable: "仍在股票池但不可排名", current_status_pending: "当前待处理", current_status_missing: "当前数据缺失",
    current_status_skipped: "当前已跳过", current_rank_missing: "当前排名缺失", rank_improved: "排名改善", rank_declined: "排名下降",
    score_increased: "原始分上升", score_decreased: "原始分下降",
  };
  return (Array.isArray(codes) ? codes : []).map((code) => labels[code] || code).join("、") || "同 cohort 冻结比较";
}

function deltaUnavailableLabel(reason) {
  return ({
    current_not_published: "当前批次尚未发布，不能生成正式变化证据。",
    current_not_full_market: "当前批次不是完整全市场 cohort，不能比较。",
    previous_same_cohort_not_found: "暂无相同模式、股票池和规则版本的前批次。",
  })[reason] || "暂无可比较的同 cohort 前批次。";
}

function renderRegionError(region, message) {
  region.innerHTML = `<p class="market-scan-screening-region-state error">${escapeHtml(message)}</p>`;
}

function rangeChip(field, range) {
  const label = RANGE_LABELS[field] || field;
  const lower = range.min === undefined ? "不限" : screeningNumber(range.min, 2);
  const upper = range.max === undefined ? "不限" : screeningNumber(range.max, 2);
  return `${label}：${lower}–${upper}`;
}

function funnelWidth(step) {
  if (!step.input_count) return step.matched_count ? "100.00" : "0.00";
  return ((step.matched_count / step.input_count) * 100).toFixed(2);
}

function ownCount(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key) ? value[key] : null;
}

function columnViewLabel(value) {
  return ({ overview: "概览", trend: "趋势", liquidity: "流动性", risk: "风险", research: "研究" })[value] || "概览";
}

function screeningElements(root) {
  const get = (id) => requiredElement(root, id);
  return {
    shell: get("marketScanScreeningWorkbench"), summaryStatus: get("marketScanScreeningSummaryStatus"),
    refresh: get("marketScanScreeningRefresh"), feedback: get("marketScanScreeningFeedback"),
    evidence: get("marketScanScreeningEvidence"), spec: get("marketScanScreeningSpec"),
    breadth: get("marketScanScreeningBreadth"), evaluation: get("marketScanScreeningEvaluation"),
    diff: get("marketScanScreeningDiff"), table: get("marketScanTable"), tableWrap: get("marketScanTableWrap"),
    columnInputs: Array.from(root.querySelectorAll('input[name="marketScanColumnView"]')),
  };
}

function requiredElement(root, id) {
  const element = root?.getElementById?.(id);
  if (!element) throw new Error(`可信筛选页面缺少 ${id}`);
  return element;
}
