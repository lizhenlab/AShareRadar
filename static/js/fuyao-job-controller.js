import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import { activeFuyaoJob, canRetryFuyaoJob, isActiveFuyaoJob, verifiedFuyaoJob } from "./fuyao-contracts.js";
import { renderFuyaoJobDetail } from "./fuyao-job-view.js";

export class FuyaoJobController {
  constructor(owner) {
    this.owner = owner;
    this.id = "";
    this.job = null;
    this.message = "";
    this.error = false;
    this.sequence = 0;
  }

  render() {
    renderFuyaoJobDetail(this.owner.node("fuyaoJobDetail"), this.job, { id: this.id, message: this.message, error: this.error,
      submitting: this.owner.submitting, active: Boolean(activeFuyaoJob(this.owner.status, this.owner.accepted)) });
  }

  async query(id, signal) {
    const value = await this.owner.fetch(`/api/fuyao/jobs/${encodeURIComponent(id)}`, { signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
    return verifiedFuyaoJob(value, id);
  }

  async reconcile(status, accepted) {
    if (!accepted) return null;
    const matched = status.jobs?.find((job) => job.id === accepted.id);
    if (matched || !isActiveFuyaoJob(accepted)) return matched || accepted;
    try { return await this.query(accepted.id); }
    catch (error) { if (error.status === 404) return null; throw error; }
  }

  async read(id) {
    if (!id || this.owner.disposed) return;
    this.stopRead();
    const sequence = this.sequence;
    const generation = this.owner.writeGeneration;
    this.abort = new AbortController();
    if (this.id !== id) this.job = null;
    this.id = id;
    this.message = "正在读取本地任务参数与进度…";
    this.error = false;
    this.render();
    try {
      const job = await this.query(id, this.abort.signal);
      if (!this.current(sequence, generation)) return;
      this.job = job;
      this.message = "";
      this.owner.updateJob(job, false);
    } catch (error) {
      if (!this.current(sequence, generation) || isAbortError(error)) return;
      this.message = `任务详情读取失败：${error.message}。可重新读取任务核对。`;
      this.error = true;
      this.render();
    }
  }

  current(sequence, generation) {
    return !this.owner.disposed && sequence === this.sequence && generation === this.owner.writeGeneration;
  }

  update(status, accepted) {
    const job = this.id && (status?.jobs?.find((item) => item.id === this.id) || (accepted?.id === this.id ? accepted : null));
    if (job) this.job = job;
    this.render();
  }

  async action(action, id) {
    const owner = this.owner;
    if (owner.submitting || owner.disposed) return;
    const job = owner.status?.jobs?.find((item) => item.id === id) || (owner.accepted?.id === id ? owner.accepted : null) || (this.job?.id === id ? this.job : null);
    if (action === "cancel" && job?.status !== "running") return;
    if (action === "retry" && (!canRetryFuyaoJob(job) || activeFuyaoJob(owner.status, owner.accepted))) return;
    owner.beginWrite();
    owner.feedback(action === "cancel" ? "正在请求停止任务…" : "正在提交未完成项，请等待任务回执…");
    try {
      const value = await owner.fetch(`/api/fuyao/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST", timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
      const result = verifiedFuyaoJob(value, action === "cancel" ? id : null);
      if (owner.disposed) return;
      this.id = result.id;
      this.job = result;
      this.message = "";
      this.error = false;
      owner.updateJob(result, true);
      owner.feedback(action === "cancel" ? "停止请求已接收；已有工作收尾后可开始下一项。" : "补做回执已返回，仅处理原任务尚未完成的项目。");
    } catch (error) {
      if (!owner.disposed) owner.feedback(`操作未确认：${error.message}。请查看任务详情或刷新本地状态核对；不会自动重发。`, true);
    } finally {
      owner.endWrite();
    }
  }

  stopRead() {
    this.sequence += 1;
    this.abort?.abort();
    if (this.message === "正在读取本地任务参数与进度…") {
      this.message = "读取已暂停，可重新读取任务核对最新进度。";
      this.render();
    }
  }

  close() {
    this.stopRead();
    this.id = "";
    this.job = null;
    this.render();
  }
}
