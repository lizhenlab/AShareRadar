import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { activeFuyaoJob, canonicalFinancialSymbol, financialJobRequest, isActiveFuyaoJob, verifiedFuyaoJob, verifiedStockObservation } from "./fuyao-contracts.js";
import { FuyaoJobController } from "./fuyao-job-controller.js";
import { renderFuyaoMarket } from "./fuyao-market-view.js";
import { renderFuyaoStatus } from "./fuyao-status-view.js";
import { renderFuyaoStock } from "./fuyao-stock-view.js";

export function createFuyaoController(options = {}) {
  return new FuyaoController(options);
}

class FuyaoController {
  constructor(options) {
    this.document = options.documentTarget || document;
    this.getSymbol = options.getSymbol || (() => "600519.SH");
    this.fetch = options.fetchJson || fetchJson;
    this.timer = null;
    this.status = null;
    this.accepted = null;
    this.stock = null;
    this.market = null;
    this.symbol = "";
    this.period = "";
    this.stockSeq = 0;
    this.statusSeq = 0;
    this.marketSeq = 0;
    this.submitting = false;
    this.writeGeneration = 0;
    this.jobs = new FuyaoJobController(this);
    this.disposed = false;
    this.events = new AbortController();
  }

  bind() {
    this.document.addEventListener("click", (event) => this.click(event), { signal: this.events.signal });
    this.document.addEventListener("change", (event) => this.change(event), { signal: this.events.signal });
    this.document.addEventListener("input", (event) => {
      if (event.target?.id === "fuyaoMarketFilter") this.renderMarket();
    }, { signal: this.events.signal });
    this.document.addEventListener("visibilitychange", () => {
      if (this.document.visibilityState === "visible" && activeFuyaoJob(this.status, this.accepted)) void this.readStatus();
    }, { signal: this.events.signal });
    this.resetStock(this.getSymbol());
    this.renderStatus();
  }

  node(id) { return this.document.getElementById(id); }

  resetStock(raw) {
    this.stockSeq += 1;
    this.stockAbort?.abort();
    this.symbol = canonicalFinancialSymbol(raw);
    this.stock = null;
    this.period = "";
    this.hideLegacyFinancial(false);
    renderFuyaoStock(this.node("fuyaoStockPanel"), null, { message: `${this.symbol} · 等待读取本地财报。` });
    this.renderStatus();
  }

  async loadStock(raw = this.getSymbol()) {
    const symbol = canonicalFinancialSymbol(raw);
    if (symbol !== this.symbol) this.resetStock(symbol);
    const sequence = ++this.stockSeq;
    this.stockAbort?.abort();
    this.stockAbort = new AbortController();
    if (!this.stock) {
      renderFuyaoStock(this.node("fuyaoStockPanel"), null, { message: `${symbol} · 正在读取本地财报…` });
      this.renderStatus();
    }
    if (!this.status) void this.readStatus();
    try {
      const value = await this.fetch(`/api/fuyao/stock?symbol=${encodeURIComponent(symbol)}`, {
        signal: this.stockAbort.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      });
      if (!this.currentStock(sequence, symbol)) return false;
      this.stock = verifiedStockObservation(value, symbol);
      this.renderStock();
      this.clearReadFeedback("财报刷新失败：");
      return true;
    } catch (error) {
      if (this.currentStock(sequence, symbol) && !isAbortError(error)) {
        if (this.stock) this.feedback(`财报刷新失败：${error.message}；已保留上次读取的财报。`, true);
        else {
          this.hideLegacyFinancial(false);
          renderFuyaoStock(this.node("fuyaoStockPanel"), null, { message: `${symbol} · 财报读取失败：${error.message}。可重新进入财务页重试。` });
        }
        this.renderStatus();
      }
      return false;
    }
  }

  currentStock(sequence, symbol) {
    return !this.disposed && sequence === this.stockSeq && symbol === canonicalFinancialSymbol(this.getSymbol());
  }

  renderStock() {
    renderFuyaoStock(this.node("fuyaoStockPanel"), this.stock, { period: this.period });
    this.hideLegacyFinancial(Boolean(this.stock?.financials?.periods?.length));
    this.renderStatus();
  }

  hideLegacyFinancial(hidden) {
    const legacy = this.node("financialPanel");
    if (legacy) legacy.hidden = hidden;
  }

  async loadData() {
    await Promise.allSettled([this.readStatus(), this.loadMarket()]);
  }

  async loadMarket() {
    const sequence = ++this.marketSeq;
    if (!this.market) this.marketMessage("正在读取本地板块、情绪和历史研究记录…");
    try {
      const data = await this.fetch("/api/fuyao/market", { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      if (this.disposed || sequence !== this.marketSeq) return;
      this.market = data;
      this.renderMarket();
      this.clearReadFeedback("市场记录读取失败：");
    } catch (error) {
      if (!this.disposed && sequence === this.marketSeq) {
        const message = `市场记录读取失败：${error.message}。可点击“刷新本地状态”重试。`;
        if (!this.market) this.marketMessage(message);
        this.feedback(`${message}${this.market ? "已保留上次读取的市场记录。" : ""}`, true);
      }
    }
  }

  marketMessage(message) {
    const target = this.node("fuyaoMarketResults");
    if (target) target.textContent = message;
  }

  renderMarket() {
    renderFuyaoMarket(this.node("fuyaoMarketResults"), this.market, this.node("fuyaoMarketFilter")?.value || "");
  }

  async readStatus() {
    const sequence = ++this.statusSeq;
    clearTimeout(this.timer);
    const previous = activeFuyaoJob(this.status, this.accepted);
    try {
      const status = await this.fetch("/api/fuyao/status", { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      if (this.disposed || sequence !== this.statusSeq) return;
      const accepted = await this.jobs.reconcile(status, this.accepted);
      if (this.disposed || sequence !== this.statusSeq) return;
      if (isActiveFuyaoJob(accepted) && !status.jobs?.some((job) => job.id === accepted.id)) {
        status.jobs = [accepted, ...(status.jobs || [])];
      }
      this.status = status;
      this.accepted = accepted;
      this.renderStatus();
      const active = activeFuyaoJob(status, this.accepted);
      if (previous && !active) {
        void this.loadStock();
        void this.loadMarket();
      }
      if (active) this.scheduleStatus(2000);
    } catch (error) {
      if (this.disposed || sequence !== this.statusSeq) return;
      this.renderStatus(`接入状态读取失败：${error.message}`);
      if (previous) this.scheduleStatus(5000);
    }
  }

  scheduleStatus(delay) {
    clearTimeout(this.timer);
    if (this.document.visibilityState === "hidden") return;
    this.timer = setTimeout(() => { void this.readStatus(); }, delay);
  }

  renderStatus(message = "") {
    renderFuyaoStatus(this.document, this.status, { message, accepted: this.accepted, submitting: this.submitting });
    this.jobs.update(this.status, this.accepted);
  }

  click(event) {
    const button = event.target?.closest?.("[data-fuyao-job], [data-fuyao-refresh], [data-fuyao-action], #fuyaoReloadLocal");
    if (!button || button.disabled) return;
    if (button.dataset.fuyaoAction) {
      const action = button.dataset.fuyaoAction;
      if (action === "detail") void this.jobs.read(button.dataset.fuyaoId);
      else if (action === "close-detail") this.jobs.close();
      else if (["cancel", "retry"].includes(action)) void this.jobs.action(action, button.dataset.fuyaoId);
      return;
    }
    if (button.id === "fuyaoReloadLocal") { void this.loadData(); void this.loadStock(); return; }
    const currentOnly = Boolean(button.dataset.fuyaoRefresh);
    void this.submit(button.dataset.fuyaoJob || button.dataset.fuyaoRefresh, currentOnly);
  }

  change(event) {
    if (event.target?.id !== "fuyaoPeriod") return;
    this.period = event.target.value;
    this.renderStock();
  }

  async submit(kind, currentOnly = false) {
    if (this.submitting || activeFuyaoJob(this.status, this.accepted)) return;
    if (!this.status?.enabled || !this.status?.configured) { this.feedback("请先在服务配置中启用扶摇并配置 API Key。", true); return; }
    let payload;
    try { payload = this.buildRequest(kind, currentOnly); }
    catch (error) { this.feedback(error.message, true); return; }
    this.beginWrite();
    this.feedback(`正在提交后台任务${payload.symbols.length ? `（${payload.symbols.join("、")}）` : ""}…`);
    try {
      const job = await this.fetch("/api/fuyao/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      if (this.disposed) return;
      this.updateJob(verifiedFuyaoJob(job), true);
      this.feedback(`任务已提交${payload.symbols.length ? `（${payload.symbols.join("、")}）` : ""}，可以继续使用其他分析。`);
    } catch (error) {
      if (!this.disposed) this.feedback(`提交失败：${error.message}。可刷新本地状态核对任务是否已创建。`, true);
    } finally {
      this.endWrite();
    }
  }

  beginWrite() {
    this.submitting = true;
    this.writeGeneration += 1;
    this.statusSeq += 1;
    this.jobs.stopRead();
    clearTimeout(this.timer);
    this.renderStatus();
  }

  endWrite() {
    this.submitting = false;
    if (!this.disposed) { this.renderStatus(); void this.readStatus(); }
  }

  updateJob(job, accepted) {
    if (accepted) {
      this.writeGeneration += 1;
      this.statusSeq += 1;
      this.accepted = job;
    } else if (this.accepted?.id === job.id) this.accepted = job;
    if (this.status) {
      const jobs = [job, ...(this.status.jobs || []).filter((item) => item.id !== job.id)];
      const active = (this.status.active_jobs || []).filter((id) => id !== job.id);
      if (isActiveFuyaoJob(job)) active.push(job.id);
      this.status = { ...this.status, jobs, active_jobs: active };
    }
    this.renderStatus();
  }

  setWorkspace(view) {
    if (view !== "data") this.jobs.stopRead();
  }

  buildRequest(kind, currentOnly) {
    return financialJobRequest(kind, {
      currentSymbol: this.getSymbol(), symbols: currentOnly ? this.getSymbol() : this.node("fuyaoSymbols")?.value,
      indexSymbols: this.node("fuyaoIndexSymbols")?.value,
      period: currentOnly ? this.node("fuyaoPeriod")?.value?.split("|")[1] : this.node("fuyaoRequestPeriod")?.value,
    });
  }

  feedback(text, error = false) {
    for (const id of ["fuyaoJobFeedback", "fuyaoStockFeedback"]) {
      const node = this.node(id);
      if (!node) continue;
      node.textContent = text;
      node.classList.toggle("fuyao-error", error);
    }
  }

  clearReadFeedback(prefix) {
    for (const id of ["fuyaoJobFeedback", "fuyaoStockFeedback"]) {
      const node = this.node(id);
      if (node?.textContent.startsWith(prefix)) {
        node.textContent = "";
        node.classList.toggle("fuyao-error", false);
      }
    }
  }

  destroy() {
    this.disposed = true;
    this.stockSeq += 1;
    this.statusSeq += 1;
    this.marketSeq += 1;
    this.stockAbort?.abort();
    this.jobs.stopRead();
    this.events.abort();
    clearTimeout(this.timer);
  }
}
