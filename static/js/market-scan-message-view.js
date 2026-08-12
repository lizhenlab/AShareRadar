const DISTRIBUTION_AUDIT_MARKER = "评分分布门禁";
const PUBLICATION_TRUST_MARKER = "未达到发布可信度：";
const PUBLICATION_BLOCKER_RE = /全市场报价快照跨度|发布覆盖不足|有效样本占比不足|报价时间不可解析|系统性同日滞后|尚有\s*\d+\s*只待处理|不能发布|没有生成有效排名|raw_score\s*可审计样本不足|raw_score\s*全部相同|前100名\s*raw_score\s*全部饱和/;
const DISTRIBUTION_ISSUE_RE = /评分分布退化|raw_score\s*可审计样本(?:不足|仅覆盖)|raw_score\s*全部相同|distinct raw score ratio\s*仅|最大并列组占比(?:达到|\s*\d)|0\/100\s*饱和率|前100名\s*raw_score\s*全部饱和/;
const SOURCE_WARNING_RE = /数据源|provider|调用未结束|冷却|备用源|批量行情|实时报价|akshare|tencent|腾讯行情|东方财富|BaoStock|扫描压力控制/i;

export function marketScanHeadlineMessage(message, publicationDiagnostics = null) {
  const structured = structuredDiagnostics(publicationDiagnostics);
  if (structured) return text(structured.headline) || text(message);
  const original = text(message);
  return splitDistributionAudit(original).main || original;
}

export function marketScanMessagePresentation(run) {
  const structured = structuredMessagePresentation(run);
  if (structured) return structured;
  const message = text(run?.message);
  const lastError = text(run?.last_error);
  const messageParts = splitDistributionAudit(message);
  const errorParts = splitDistributionAudit(lastError);
  const distributionAudit = messageParts.audit || errorParts.audit;
  return {
    headline: messageParts.main || message,
    publicationBlockers: publicationBlockerText(messageParts.main),
    passedGates: scoreDistributionPassed(run, distributionAudit, messageParts.main, errorParts.main)
      ? passedDistributionText(distributionAudit)
      : "",
    sourceWarnings: sourceWarningText(lastError || message),
  };
}

function structuredMessagePresentation(run) {
  const diagnostics = structuredDiagnostics(run?.publication_diagnostics);
  if (!diagnostics) return null;
  return {
    headline: text(diagnostics.headline) || text(run?.message),
    publicationBlockers: diagnosticDetails(diagnostics.blockers, 240),
    passedGates: passedGateDetails(diagnostics.passed_gates),
    sourceWarnings: diagnosticDetails(diagnostics.source_warnings, 280),
  };
}

function structuredDiagnostics(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!Array.isArray(value.blockers) || !Array.isArray(value.passed_gates) || !Array.isArray(value.source_warnings)) {
    return null;
  }
  return value;
}

function diagnosticDetails(items, limit) {
  return compact(items.map((item) => text(item?.detail)).filter(Boolean).join("；"), limit);
}

function passedGateDetails(items) {
  const details = items.map((item) => {
    const label = text(item?.label);
    const detail = text(item?.detail);
    if (!label) return detail;
    return detail ? `${label} · ${detail}` : label;
  }).filter(Boolean);
  return compact(details.join("；"), 360);
}

export function renderMarketScanMessageSummary(root, run) {
  const summary = root.getElementById("marketScanGateSummary");
  if (!summary) return;
  const presentation = marketScanMessagePresentation(run);
  const visible = [
    renderMessageRow(root, "marketScanPublicationBlockers", presentation.publicationBlockers),
    renderMessageRow(root, "marketScanPassedGates", presentation.passedGates),
    renderMessageRow(root, "marketScanSourceWarnings", presentation.sourceWarnings),
  ].some(Boolean);
  summary.hidden = !visible;
}

function splitDistributionAudit(value) {
  const original = text(value);
  const markerIndex = original.indexOf(DISTRIBUTION_AUDIT_MARKER);
  if (markerIndex < 0) return { main: original, audit: "" };
  const auditTail = original.slice(markerIndex).trim();
  const nextSection = auditTail.search(/[；;]\s*(?:发布阻断|数据源告警|已通过(?:门禁)?)\s*[：:]/);
  const audit = nextSection > 0 ? auditTail.slice(0, nextSection) : auditTail;
  if (!/raw_score样本|distinct ratio|最大并列组|0\/100饱和/.test(audit)) {
    return { main: original, audit: "" };
  }
  const main = original.slice(0, markerIndex).replace(
    /[；;]\s*已通过(?:门禁)?\s*[：:]\s*$/,
    "",
  );
  return {
    main: trimSeparators(main),
    audit: trimSeparators(audit),
  };
}

function publicationBlockerText(message) {
  const value = text(message);
  const trustIndex = value.indexOf(PUBLICATION_TRUST_MARKER);
  if (trustIndex >= 0) {
    return compact(value.slice(trustIndex + PUBLICATION_TRUST_MARKER.length).replace(/^发布阻断\s*[：:]\s*/, ""));
  }
  const blockerIndex = value.search(PUBLICATION_BLOCKER_RE);
  return blockerIndex >= 0 ? compact(value.slice(blockerIndex)) : "";
}

function scoreDistributionPassed(run, audit, message, lastError) {
  if (!audit || DISTRIBUTION_ISSUE_RE.test(`${message}；${lastError}`)) return false;
  const status = text(run?.score_distribution?.status || run?.score_distribution_status).toLowerCase();
  if (["failed", "degraded", "not-evaluated"].includes(status)) return false;
  if (["pass", "passed"].includes(status) || /评分分布门禁\s*[：:]?\s*通过/.test(audit)) return true;
  return distributionMetricsPass(audit);
}

function distributionMetricsPass(audit) {
  const sample = audit.match(/raw_score样本\s*(\d+)\/(\d+)/);
  const distinct = percent(audit, /distinct ratio\s*([\d.]+)%/);
  const maxTie = percent(audit, /最大并列组\s*\d+\/\d+[（(]([\d.]+)%/);
  const saturation = percent(audit, /0\/100饱和\s*\d+\/\d+[（(]([\d.]+)%/);
  const topTie = percent(audit, /前100并列\s*\d+\/\d+[（(]([\d.]+)%/);
  if (!sample || [distinct, maxTie, saturation, topTie].some((value) => value === null)) return false;
  const observed = Number(sample[1]) / Math.max(1, Number(sample[2]));
  return Number(sample[2]) >= 100
    && observed >= 0.99
    && maxTie < 50
    && saturation < 50
    && topTie < 50
    && !(distinct <= 2 && maxTie >= 25);
}

function passedDistributionText(audit) {
  return compact(audit.replace(/^评分分布门禁\s*/, "评分分布 · "), 360);
}

function sourceWarningText(value) {
  const explicit = explicitSourceWarning(value);
  if (explicit) return compact(explicit, 280);
  const main = splitDistributionAudit(value).main;
  const blockerIndex = main.search(PUBLICATION_BLOCKER_RE);
  const candidate = trimSeparators(blockerIndex >= 0 ? main.slice(0, blockerIndex) : main)
    .replace(/[；;]\s*发布阻断\s*[：:]?\s*$/, "");
  if (!candidate || !SOURCE_WARNING_RE.test(candidate)) return "";
  const sourceStart = candidate.search(SOURCE_WARNING_RE);
  return compact(candidate.slice(Math.max(0, sourceStart)), 280);
}

function explicitSourceWarning(value) {
  const original = text(value);
  const marker = original.match(/(?:^|[；;])\s*数据源告警\s*[：:]/);
  if (!marker || marker.index === undefined) return "";
  const tail = original.slice(marker.index + marker[0].length);
  const nextSection = tail.search(/[；;]\s*(?:发布阻断|已通过(?:门禁)?)\s*[：:]/);
  return trimSeparators(nextSection >= 0 ? tail.slice(0, nextSection) : tail);
}

function renderMessageRow(root, rowId, value) {
  const row = root.getElementById(rowId);
  const content = root.getElementById(`${rowId}Text`);
  if (!row || !content) return false;
  const visible = Boolean(value);
  row.hidden = !visible;
  content.textContent = visible ? value : "";
  content.title = visible ? value : "";
  return visible;
}

function percent(value, pattern) {
  const match = value.match(pattern);
  if (!match) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) ? parsed : null;
}

function compact(value, limit = 240) {
  const normalized = trimSeparators(value).replace(/\s+/g, " ");
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}…` : normalized;
}

function trimSeparators(value) {
  return text(value).replace(/^[\s；;]+|[\s；;]+$/g, "");
}

function text(value) {
  return String(value ?? "").trim();
}
