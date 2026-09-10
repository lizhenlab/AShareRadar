import { activeFuyaoJob } from "./fuyao-contracts.js";
import { renderFuyaoJobs } from "./fuyao-job-view.js";

export function renderFuyaoStatus(documentTarget, status, options = {}) {
  const summary = documentTarget.getElementById("fuyaoStatusSummary");
  if (summary) summary.textContent = options.message
    ? `${options.message}${status ? `；上次状态：${statusSummary(status)}` : ""}` : statusSummary(status);
  const enabled = !options.message && status?.enabled === true && status?.configured === true;
  const active = activeFuyaoJob(status, options.accepted);
  documentTarget.querySelectorAll("[data-fuyao-job], [data-fuyao-refresh]").forEach((button) => {
    const history = String(button.dataset.fuyaoJob || "").startsWith("history_");
    button.disabled = !enabled || Boolean(active) || options.submitting === true || history && !status?.download_hosts_configured;
  });
  const download = documentTarget.getElementById("fuyaoDownloadStatus");
  if (download) download.textContent = status?.download_hosts_configured
    ? "历史下载源已配置；文件先在后台校验，再发布为独立研究版本。"
    : "历史下载源尚未配置。配置完成后可使用完整历史与增量同步。";
  renderFuyaoJobs(documentTarget.getElementById("fuyaoJobs"), status, { ...options, active: Boolean(active) });
}

function statusSummary(status) {
  return status ? `数据源${status.enabled ? "已启用" : "未启用"} · API Key ${status.configured ? "已配置" : "未配置"} · 今日项目请求 ${status.persistent_daily_requests ?? 0} 次`
    : "正在读取本地接入状态…";
}
