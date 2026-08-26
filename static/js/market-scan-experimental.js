import { isAbortError } from "./api.js";
import { escapeHtml } from "./dom.js";

export function createExperimentalProbabilityController({ root, request, onSelectStock = () => {} }) {
  const get = (name) => root?.getElementById?.(`marketScanExperimental${name}`);
  const elements = Object.fromEntries(["Panel", "Enable", "Controls", "Minimum", "Market", "Keyword", "Sort", "Status", "Evidence", "Table", "Rows", "Prev", "Next", "Page"].map((name) => [name, get(name)]));
  if (!elements.Panel || !elements.Enable) return { sync() {}, abort() {} };
  elements.Enable.checked = false;
  elements.Controls.hidden = true;
  let run = null, controller = null, sequence = 0, currentPage = 1, payload = null;
  const clear = () => { elements.Rows.innerHTML = ""; elements.Table.hidden = true; elements.Evidence.textContent = ""; payload = null; paginate(); };
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
    if (run?.id === nextRun?.id && run?.snapshot_digest === nextRun?.snapshot_digest) return;
    abort(); run = nextRun || null; currentPage = 1; clear();
    elements.Status.textContent = run ? "已关联当前显示榜单；勾选后开启独立实验筛选。" : "请先选择一个已发布的全市场榜单。";
    if (elements.Enable.checked && run) void load(1);
  }
  function selected() {
    const text = elements.Minimum.value.trim();
    const minimum = text === "" ? null : Number(text) / 100;
    if (minimum !== null && (!Number.isFinite(minimum) || minimum < 0 || minimum > 1)) throw new Error("概率阈值应为 0–100%");
    return { min_probability: minimum, market: elements.Market.value || null, keyword: elements.Keyword.value.trim(), sort: elements.Sort.value };
  }
  async function load(page) {
    abort(); clear();
    if (!elements.Enable.checked || !run) return;
    const runId = run.id;
    const mine = sequence;
    controller = new AbortController();
    elements.Panel.setAttribute?.("aria-busy", "true");
    elements.Status.textContent = "正在读取历史模型并计算当前榜单的实验概率…";
    try {
      const filters = selected();
      const params = new URLSearchParams({ acknowledge_experimental: "true", page: String(page), page_size: "50", sort: filters.sort });
      if (filters.min_probability !== null) params.set("min_probability", String(filters.min_probability));
      if (filters.market) params.set("market", filters.market);
      if (filters.keyword) params.set("keyword", filters.keyword);
      const value = await requestExperimentalProbability(request, `/api/market-scans/${encodeURIComponent(runId)}/experimental-probability?${params}`,
        controller.signal, () => { elements.Status.textContent = "榜单快照正在校验，实验筛选将自动重试…"; });
      if (mine !== sequence || run?.id !== runId || !elements.Enable.checked) return;
      if (value.base_snapshot_digest !== run.snapshot_digest) invalid();
      payload = validateExperimentalProbability(value, runId, page, filters);
      currentPage = page;
      render();
    } catch (error) {
      if (mine === sequence && !isAbortError(error)) elements.Status.textContent = `实验模式暂不可用：${error.message || "请求失败"}`;
    } finally {
      if (mine === sequence) { controller = null; elements.Panel.setAttribute?.("aria-busy", "false"); paginate(); }
    }
  }
  function render() {
    const model = payload.model;
    const evaluation = model.historical_recipe_evaluation || {};
    const metrics = evaluation.calibration_metrics?.calibrated || {};
    const bss = Number.isFinite(metrics.brier_skill_score) ? metrics.brier_skill_score.toFixed(4) : "未提供";
    elements.Status.textContent = `个人实验已开启 · 批次 #${payload.run_id} · 日线截至 ${payload.signal_date} · 可计算 ${payload.coverage.predicted_count}/${payload.coverage.successful_scan_count} 只，缺失或异常 ${payload.coverage.unavailable_count} 只不参与排序。`;
    elements.Evidence.textContent = `历史抽样 ${model.training_symbol_count} 只；训练 ${model.train_session_count} 日，独立校准 ${model.calibration_session_count} 日；标签截至 ${model.latest_label_date}。历史 H5 Brier 技能分 ${bss}；未通过正式验证，拟合校准器不等于已验证有效。盘中使用前一完整日线，不是实时概率。模型 ${payload.model_digest.slice(0, 12)}。`;
    elements.Rows.innerHTML = payload.items.map((item) => `<tr><td>${item.experimental_rank}</td><td>${item.base_rank ?? "—"}</td><td><button type="button" class="market-scan-stock-link" data-experimental-symbol="${escapeHtml(item.symbol)}">${escapeHtml(item.name)}<small>${escapeHtml(item.symbol)}</small></button></td><td>${(item.probability * 100).toFixed(2)}%</td><td>${item.base_score ?? "—"}</td><td>${item.training_universe_member ? "训练样本范围内" : "样本外股票·泛化未验证"}</td></tr>`).join("");
    if (!payload.items.length) elements.Rows.innerHTML = '<tr><td colspan="6">没有符合当前实验筛选条件的股票。</td></tr>';
    elements.Table.hidden = false;
  }
  elements.Enable.addEventListener("change", () => {
    elements.Controls.hidden = !elements.Enable.checked;
    if (elements.Enable.checked) void load(1);
    else { abort(); clear(); elements.Status.textContent = "个人实验已关闭；正式榜单未改变。"; }
  });
  elements.Controls.addEventListener("submit", (event) => { event.preventDefault(); void load(1); });
  elements.Prev.addEventListener("click", () => { if (payload && currentPage > 1) void load(currentPage - 1); });
  elements.Next.addEventListener("click", () => { if (payload && currentPage < payload.page_count) void load(currentPage + 1); });
  elements.Rows.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-experimental-symbol]");
    if (button) onSelectStock(button.dataset.experimentalSymbol);
  });
  return { sync, abort };
}

export async function requestExperimentalProbability(request, url, signal, onBusy = () => {}) {
  for (let attempt = 0; ; attempt += 1) {
    if (signal.aborted) throw new DOMException("实验读取已取消", "AbortError");
    try {
      return await request(url, { signal, timeoutMs: 90000 });
    } catch (error) {
      if (signal.aborted || attempt >= 8 || error.status !== 503 || !Number.isFinite(error.retryAfterMs)) throw error;
      onBusy();
      await waitForRetry(Math.min(5000, Math.max(1000, error.retryAfterMs)), signal);
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
  validateHeader(value, runId, page);
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
function validateHeader(value, runId, page) {
  const expected = { schema_version: "market-scan-personal-experimental-probability-v1", run_id: runId,
    mode: "personal_experimental", experimental: true, formal_filter_qualified: false, production_ranking_effect: "none",
    horizon: 5, target: "net_return_positive", page, page_size: 50 };
  if (!value || Object.entries(expected).some(([key, item]) => value[key] !== item)
      || !/^[0-9a-f]{64}$/.test(value.model_digest || "")) invalid();
}
function validateCoverage(value) {
  const coverage = value.coverage;
  if (!coverage || !value.model || ![coverage.successful_scan_count, coverage.predicted_count, coverage.unavailable_count].every(count)
      || coverage.predicted_count + coverage.unavailable_count !== coverage.successful_scan_count || value.total > coverage.predicted_count) invalid();
}
function validateItem(item, predictedCount) {
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(item.symbol || "") || typeof item.name !== "string" || typeof item.training_universe_member !== "boolean") invalid();
  if (!Number.isFinite(item.probability) || item.probability < 0 || item.probability > 1) invalid();
  if (![item.experimental_rank, item.base_rank, item.base_score].every(count) || item.experimental_rank < 1
      || item.experimental_rank > predictedCount || item.base_rank < 1 || item.base_score > 100) invalid();
}
