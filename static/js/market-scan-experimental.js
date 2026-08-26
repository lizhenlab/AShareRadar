import { isAbortError } from "./api.js";
import { escapeHtml } from "./dom.js";
import { experimentalNavigation, locateExperimentalRun, matchesExperimentalNavigation } from "./market-scan-experimental-context.js";

function definition(kind) {
  if (kind === "net_h5") return { horizon: 5, target: "net_return_positive", offset: 6, reference: "next_session_open", label: "旧版 H5 扣费盈利",
    semantics: "旧版 H5（不是 D+5）：假定 D+1 开盘买入、D+6 收盘卖出，扣除模型成本后收益为正的概率。未完整考虑停牌、涨跌停和成交容量，不是可成交保证，不改变正式排名。" };
  const offset = { close_d1: 1, close_d2: 2, close_d5: 5 }[kind];
  if (!offset) invalid();
  return { horizon: offset, target: "close_return_positive", offset, reference: "signal_day_qfq_close", label: `D+${offset} 收盘上涨`,
    semantics: `D+${offset}：固定第 ${offset} 个交易日收盘价高于 D 日收盘价的概率（同一前复权口径）。D 为榜单采用的已收盘日线日期，不是盘中实时价格；不扣费，不代表买入后盈利或可成交。独立实验模型，不改变正式排名。` };
}

export function createExperimentalProbabilityController({ root, request, getNavigation, onSelectStock = () => {} }) {
  const get = (name) => root?.getElementById?.(`marketScanExperimental${name}`);
  const elements = Object.fromEntries(["Panel", "Enable", "Controls", "Kind", "Semantics", "ProbabilityLabel", "Minimum", "Market", "Keyword", "Sort", "Status", "Evidence", "Table", "Rows", "Prev", "Next", "Page"].map((name) => [name, get(name)]));
  if (!elements.Panel || !elements.Enable) return { sync() {}, abort() {}, navigationChanged() {} };
  elements.Enable.checked = false;
  elements.Kind.value = "close_d1";
  elements.Controls.hidden = true;
  let run = null, controller = null, sequence = 0, currentPage = 1, payload = null, navigationKey = "", independent = false;
  const clear = () => { elements.Rows.innerHTML = ""; elements.Table.hidden = true; elements.Evidence.textContent = ""; payload = null; paginate(); };
  function updateDefinition() {
    const choice = definition(elements.Kind.value);
    elements.Semantics.textContent = choice.semantics;
    elements.ProbabilityLabel.textContent = `${choice.label}概率`;
  }
  updateDefinition();
  function abort() {
    sequence += 1;
    if (controller) elements.Status.textContent = "实验读取已取消，可重新应用筛选。";
    controller?.abort(); controller = null;
    elements.Panel.setAttribute?.("aria-busy", "false");
  }
  function paginate() {
    elements.Prev.disabled = !payload || currentPage <= 1;
    elements.Next.disabled = !payload || currentPage >= payload.page_count;
    elements.Page.textContent = payload ? `第 ${currentPage} / ${payload.page_count || 1} 页 · 筛选后 ${payload.total} 只` : "";
  }
  function sync(nextRun) {
    const navigation = experimentalNavigation(getNavigation), key = JSON.stringify(navigation);
    const candidate = matchesExperimentalNavigation(nextRun, navigation) ? nextRun : null;
    if (key === navigationKey && ((!candidate && independent) || (run?.id === candidate?.id && run?.snapshot_digest === candidate?.snapshot_digest))) return;
    abort(); run = candidate; independent = false; navigationKey = key; currentPage = 1; clear();
    elements.Status.textContent = run ? "已关联当前显示榜单；勾选后开启独立实验筛选。" : "勾选后独立校验当前模式/所选历史批次，无需等待正式榜单加载。";
    if (elements.Enable.checked) void load(1);
  }
  function navigationChanged() {
    if (JSON.stringify(experimentalNavigation(getNavigation)) !== navigationKey) sync(null);
  }
  function selected() {
    const text = elements.Minimum.value.trim();
    const minimum = text === "" ? null : Number(text) / 100;
    if (minimum !== null && (!Number.isFinite(minimum) || minimum < 0 || minimum > 1)) throw new Error("概率阈值应为 0–100%");
    definition(elements.Kind.value);
    if (!["", "SH", "SZ", "BJ"].includes(elements.Market.value) || !["probability", "base_rank"].includes(elements.Sort.value)) invalid();
    return { prediction_kind: elements.Kind.value, min_probability: minimum, market: elements.Market.value || null, keyword: elements.Keyword.value.trim(), sort: elements.Sort.value };
  }
  async function load(page, paging = false) {
    const previous = payload;
    abort(); clear();
    if (!elements.Enable.checked) return;
    const mine = sequence;
    controller = new AbortController();
    elements.Panel.setAttribute?.("aria-busy", "true");
    elements.Status.textContent = "正在读取历史模型并计算当前榜单的实验概率…";
    try {
      const filters = selected();
      if (paging && (!previous || Object.keys(filters).some((key) => filters[key] !== previous.filters[key]))) {
        draftChanged(); return;
      }
      const navigation = experimentalNavigation(getNavigation), key = JSON.stringify(navigation);
      if (!matchesExperimentalNavigation(run, navigation) || key !== navigationKey) {
        const located = await locateExperimentalRun(request, navigation, controller.signal);
        if (!isCurrent(mine)) return;
        run = located; independent = true; navigationKey = key;
      }
      const source = run;
      if (!source) throw new Error("请先选择一个已发布的全市场榜单");
      const value = await requestExperimentalProbability(request, experimentalUrl(source.id, page, filters), controller.signal, () => busy(mine));
      if (!isCurrent(mine) || run?.id !== source.id) return;
      acceptResponse(value, source, page, filters, paging ? previous : null);
      payload = value;
      currentPage = page;
      render();
    } catch (error) {
      if (mine === sequence && !isAbortError(error)) elements.Status.textContent = `实验模式暂不可用：${error.message || "请求失败"}`;
    } finally {
      if (mine === sequence) { controller = null; elements.Panel.setAttribute?.("aria-busy", "false"); paginate(); }
    }
  }
  function busy(mine) {
    if (mine === sequence) elements.Status.textContent = "上一项实验校验尚未结束，完成后自动重试…";
  }
  function isCurrent(mine) { return mine === sequence && elements.Enable.checked; }
  function acceptResponse(value, source, page, filters, previous) {
    if (JSON.stringify(experimentalNavigation(getNavigation)) !== navigationKey) throw new Error("榜单选择已变化，请重新应用实验筛选");
    if (value.base_snapshot_digest !== source.snapshot_digest || value.signal_date !== source.data_date) invalid();
    validateExperimentalProbability(value, source.id, page, filters);
    if (previous && ["run_id", "base_snapshot_digest", "signal_date", "prediction_kind", "model_digest", "input_digest"].some((key) => value[key] !== previous[key])) {
      currentPage = 1; throw new Error("批次、模型或行情数据已更新，请重新应用筛选，从第一页查看，避免混用不同版本");
    }
  }
  function render() {
    const model = payload.model;
    const evaluation = model.historical_recipe_evaluation || {};
    const metrics = evaluation.calibration_metrics?.calibrated || {};
    const bss = Number.isFinite(metrics.brier_skill_score) ? metrics.brier_skill_score.toFixed(4) : "未提供";
    const choice = definition(payload.prediction_kind);
    const evaluationText = payload.prediction_kind === "net_h5" ? `历史 H5 Brier 技能分 ${bss}` : "此收盘方向目标尚未做独立样本外评估；未借用 H5 指标";
    const sourceNote = independent ? "独立校验批次（不表示正式榜单已完成校验）" : "当前显示批次";
    elements.Status.textContent = `个人实验已开启 · ${choice.label} · ${sourceNote} #${payload.run_id} · 基准 D=${payload.signal_date} · 目标 ${payload.target_session_date} · 可计算 ${payload.coverage.predicted_count}/${payload.coverage.successful_scan_count} 只，缺失或异常 ${payload.coverage.unavailable_count} 只不参与排序。${unavailableText(payload.coverage)}`;
    elements.Evidence.textContent = `历史抽样 ${model.training_symbol_count} 只；训练 ${model.train_session_count} 日，独立校准 ${model.calibration_session_count} 日；标签截至 ${model.latest_label_date}。校准期正例占比 ${(model.base_rate * 100).toFixed(1)}%（不同目标不可直接比较概率高低）。${evaluationText}；未通过正式验证，拟合校准器不等于已验证有效。盘中使用前一完整日线，不是实时概率。模型 ${payload.model_digest.slice(0, 12)}。`;
    elements.Rows.innerHTML = payload.items.map((item) => `<tr><td>${item.experimental_rank}</td><td>${item.base_rank ?? "—"}</td><td><button type="button" class="market-scan-stock-link" data-experimental-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.name)}<small>${escapeHtml(item.symbol)}</small></button></td><td>${(item.probability * 100).toFixed(2)}%</td><td>${item.base_score ?? "—"}</td><td>${item.training_universe_member ? "训练样本范围内" : "样本外股票·泛化未验证"}</td></tr>`).join("");
    if (!payload.items.length) elements.Rows.innerHTML = `<tr><td colspan="6">${payload.coverage.predicted_count ? "没有符合当前实验筛选条件的股票，可降低阈值或清除筛选。" : "当前批次没有可计算的股票，请根据上方缺失原因补齐日线；不会用默认概率代替。"}</td></tr>`;
    elements.Table.hidden = false;
  }
  elements.Enable.addEventListener("change", () => {
    elements.Controls.hidden = !elements.Enable.checked;
    if (elements.Enable.checked) void load(1);
    else { abort(); clear(); elements.Status.textContent = "个人实验已关闭；正式榜单未改变。"; }
  });
  elements.Controls.addEventListener("submit", (event) => { event.preventDefault(); void load(1); });
  elements.Kind.addEventListener("change", () => { abort(); clear(); currentPage = 1; updateDefinition(); void load(1); });
  function draftChanged() {
    abort(); clear(); currentPage = 1;
    elements.Status.textContent = "筛选条件已修改，请点击“应用实验筛选”，从第一页重新查看。";
  }
  for (const name of ["Minimum", "Market", "Keyword", "Sort"]) {
    elements[name].addEventListener("input", draftChanged);
    elements[name].addEventListener("change", draftChanged);
  }
  elements.Prev.addEventListener("click", () => { if (payload && currentPage > 1) void load(currentPage - 1, true); });
  elements.Next.addEventListener("click", () => { if (payload && currentPage < payload.page_count) void load(currentPage + 1, true); });
  elements.Rows.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-experimental-symbol]");
    if (button) onSelectStock(button.dataset.experimentalSymbol);
  });
  return { sync, abort, navigationChanged };
}

function experimentalUrl(runId, page, filters) {
  const params = new URLSearchParams({ acknowledge_experimental: "true", prediction_kind: filters.prediction_kind, page: String(page), page_size: "50", sort: filters.sort });
  if (filters.min_probability !== null) params.set("min_probability", String(filters.min_probability));
  if (filters.market) params.set("market", filters.market);
  if (filters.keyword) params.set("keyword", filters.keyword);
  return `/api/market-scans/${encodeURIComponent(runId)}/experimental-probability?${params}`;
}

export async function requestExperimentalProbability(request, url, signal, onBusy = () => {}) {
  const deadline = performance.now() + 90000;
  for (let attempt = 0; ; attempt += 1) {
    if (signal.aborted) throw new DOMException("实验读取已取消", "AbortError");
    try {
      return await request(url, { signal, timeoutMs: Math.max(1, deadline - performance.now()) });
    } catch (error) {
      const delay = Math.min(5000, Math.max(1000, error.retryAfterMs));
      if (signal.aborted || attempt >= 60 || error.status !== 503 || !Number.isFinite(error.retryAfterMs) || performance.now() + delay >= deadline) throw error;
      onBusy();
      await waitForRetry(delay, signal);
    }
  }
}

function waitForRetry(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const finish = () => { signal.removeEventListener("abort", cancel); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const cancel = () => { clearTimeout(timer); signal.removeEventListener("abort", cancel); reject(new DOMException("实验读取已取消", "AbortError")); };
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
}

export function validateExperimentalProbability(value, runId, page, filters) {
  validateHeader(value, runId, page, filters.prediction_kind || "net_h5");
  if (!count(value.total) || value.page_count !== Math.ceil(value.total / 50) || !Array.isArray(value.items)) invalid();
  const expectedCount = Math.max(0, Math.min(50, value.total - (page - 1) * 50));
  if (value.items.length !== expectedCount || Object.keys(filters).some((key) => value.filters?.[key] !== filters[key])) invalid();
  validateCoverage(value);
  const seen = new Set();
  for (const item of value.items) {
    validateItem(item, value.coverage.predicted_count);
    if (seen.has(item.symbol)) invalid();
    seen.add(item.symbol);
  }
  return value;
}

function invalid() { throw new Error("实验响应身份、分页或概率数据无效"); }
function count(number) { return Number.isInteger(number) && number >= 0; }
function isoDate(value) { return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value; }
function validateHeader(value, runId, page, kind) {
  const choice = definition(kind);
  const expected = { schema_version: "market-scan-personal-experimental-probability-v1", run_id: runId,
    mode: "personal_experimental", experimental: true, formal_filter_qualified: false, production_ranking_effect: "none",
    prediction_kind: kind, horizon: choice.horizon, target: choice.target, reference: choice.reference,
    target_session_offset: choice.offset, page, page_size: 50 };
  if (!value || Object.entries(expected).some(([key, item]) => value[key] !== item)
      || ![value.model_digest, value.input_digest, value.base_snapshot_digest].every((digest) => typeof digest === "string" && /^[0-9a-f]{64}$/.test(digest))
      || !isoDate(value.signal_date) || !isoDate(value.target_session_date)
      || value.target_session_date <= value.signal_date) invalid();
  if (kind !== "net_h5") validateDirection(value, choice);
}
function validateDirection(value, choice) {
  const evidence = value.model?.direction_evidence;
  const expected = { label_version: "close-to-close-fixed-session-v1", reference: choice.reference,
    target_session_offset: choice.offset, comparison: "target_close_gt_signal_close", adjustment_mode: "qfq", fees_included: false, execution_modelled: false };
  if (!evidence || Object.entries(expected).some(([key, item]) => evidence[key] !== item)) invalid();
  const evaluation = value.model?.historical_recipe_evaluation;
  if (evaluation?.status !== "not_evaluated" || evaluation.target !== choice.target || evaluation.horizon !== choice.horizon) invalid();
}
function validateCoverage(value) {
  const coverage = value.coverage;
  if (!coverage || !value.model || ![coverage.successful_scan_count, coverage.predicted_count, coverage.unavailable_count].every(count)
      || coverage.predicted_count + coverage.unavailable_count !== coverage.successful_scan_count || value.total > coverage.predicted_count) invalid();
  const reasons = coverage.unavailable_reasons;
  if (!reasons || typeof reasons !== "object" || Array.isArray(reasons) || !Object.values(reasons).every(count)
      || Object.values(reasons).reduce((sum, amount) => sum + amount, 0) !== coverage.unavailable_count) invalid();
  if (!Number.isFinite(value.model.base_rate) || value.model.base_rate < 0 || value.model.base_rate > 1) invalid();
}
function unavailableText(coverage) {
  const labels = { missing_exact_date_or_21_bars: "基准日或21日日线不足", missing_fixed_session_window: "固定交易日窗口有缺口",
    no_volume_on_signal_date: "基准日无成交量", feature_out_of_distribution: "特征超出训练分布", invalid_or_mixed_history_contract: "日线口径异常" };
  return Object.entries(coverage.unavailable_reasons).map(([reason, amount]) => `${labels[reason] || reason} ${amount}只`).join("；");
}
function validateItem(item, predictedCount) {
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(item.symbol || "") || typeof item.name !== "string" || typeof item.training_universe_member !== "boolean") invalid();
  if (!Number.isFinite(item.probability) || item.probability < 0 || item.probability > 1) invalid();
  if (![item.experimental_rank, item.base_rank, item.base_score].every(count) || item.experimental_rank < 1
      || item.experimental_rank > predictedCount || item.base_rank < 1 || item.base_score > 100) invalid();
}
