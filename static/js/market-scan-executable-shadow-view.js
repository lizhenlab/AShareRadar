import { escapeHtml } from "./dom.js";

const STATUS_LABELS = {
  ready: "草案就绪",
  no_trade: "不交易",
  blocked: "已阻断",
  selected: "入选",
  constraint_adjusted: "约束后入选",
};

export function executableShadowElements(root) {
  const ids = [
    "strategyExecutableShadow", "executableShadowForm", "executableShadowRunId",
    "executableShadowNotional", "executableShadowUseCurrent", "executableShadowGenerate",
    "executableShadowCancel", "executableShadowStatus", "executableShadowResult",
  ];
  if (!root?.getElementById) return null;
  const elements = Object.fromEntries(ids.map((id) => [id, root.getElementById(id)]));
  return completeElementContract(elements) ? elements : null;
}

export function renderExecutableShadowIdle(elements) {
  setBusy(elements, false);
  announce(elements, "尚未请求。请先确认正式全市场批次，再显式生成只读投影。", "idle");
  elements.executableShadowResult.dataset.state = "idle";
  elements.executableShadowResult.innerHTML = `
    <p>此面板不会随全市场榜单或策略实验室打开而自动计算。结果只改变本面板展示，不写数据库，来源批次生产评分与排名保持不变。</p>`;
}

export function renderExecutableShadowLoading(elements, runId) {
  setBusy(elements, true);
  announce(elements, `正在读取冻结批次 #${runId} 并生成只读投影…`, "loading");
  elements.executableShadowResult.dataset.state = "loading";
  elements.executableShadowResult.innerHTML = "<p>全市场冻结行较多，本次按需计算可能需要一段时间；可随时取消。</p>";
}

export function renderExecutableShadowError(elements, message) {
  setBusy(elements, false);
  announce(elements, message, "error");
  elements.executableShadowResult.dataset.state = "error";
  elements.executableShadowResult.innerHTML = `
    <div class="executable-shadow-local-error" role="alert"><strong>本面板不可用</strong><p>${escapeHtml(message)}</p><p>生产榜单与 Strategy Lab 其他区域未受影响。</p></div>`;
}

export function renderExecutableShadowCancelled(elements) {
  setBusy(elements, false);
  announce(elements, "已取消本次只读投影请求。", "idle");
  elements.executableShadowResult.dataset.state = "idle";
  elements.executableShadowResult.innerHTML = "<p>请求已取消；未写数据库，也未改变生产排名。</p>";
}

export function renderExecutableShadowReport(elements, report) {
  setBusy(elements, false);
  announce(elements, `批次 #${report.evidence.run_id} 的只读投影已校验并显示。`, "ready");
  elements.executableShadowResult.dataset.state = "ready";
  elements.executableShadowResult.innerHTML = [
    boundaryHtml(report),
    summaryHtml(report),
    gateHtml(report.gate_policy),
    exposureHtml(report.exposure_audit),
    selectedHtml(report.selected),
    exclusionsHtml(report.candidate_preview),
    limitationsHtml(report),
  ].join("");
}

function boundaryHtml(report) {
  const evidence = report.evidence;
  return `<div class="executable-shadow-boundary">
    <div><strong>research_shadow · 仅影子研究</strong><span>有效性：not_generated · 尚未生成</span></div>
    <div><strong>生产作用：none</strong><span>${escapeHtml(evidence.production_score_rule_version)} 保持不变 · 生产排名写入：否 · 数据库写入：否</span></div>
    <p>Alpha、风险、置信和可交易性是冻结截面序数分，不是收益率或上涨概率；当前为未验证 Alpha。</p>
  </div>`;
}

function summaryHtml(report) {
  const summary = report.summary;
  const evidence = report.evidence;
  return `<section class="executable-shadow-section" aria-labelledby="executableShadowSummaryTitle">
    <h5 id="executableShadowSummaryTitle">筛选与组合摘要</h5>
    <div class="executable-shadow-metrics">
      ${metric("冻结批次", `#${evidence.run_id} · ${evidence.data_date}`)}
      ${metric("状态", STATUS_LABELS[summary.status] || summary.status)}
      ${metric("评估 / 合格", `${count(summary.evaluated_count)} / ${count(summary.eligible_count)}`)}
      ${metric("入选 / 未入选", `${count(summary.selected_count)} / ${count(summary.rejected_count)}`)}
      ${metric("预计换手", percent(summary.estimated_turnover))}
      ${metric("预计往返成本", money(summary.estimated_round_trip_cost_cny))}
      ${metric("目标投资权重", percent(summary.target_invested_weight))}
      ${metric("剩余现金", money(summary.residual_cash_cny))}
      ${metric("约束补位 / 候选池", `${count(summary.replacement_attempt_count)} / ${summary.pool_exhausted ? "已耗尽" : "未耗尽"}`)}
      ${metric("PIT 已校验", `${count(summary.evidence_verified_count)} / ${count(evidence.verified_point_in_time_count)}`)}
    </div>
    ${messages("未充分投资原因", summary.underinvested_reason ? [summary.underinvested_reason] : [])}
    ${messages("不交易 / 阻断原因", summary.no_trade_reasons)}
    ${messages("计算说明", summary.notes)}
  </section>`;
}

function gateHtml(gate) {
  return `<section class="executable-shadow-section" aria-labelledby="executableShadowGateTitle">
    <h5 id="executableShadowGateTitle">硬筛选、风险与容量口径</h5>
    <div class="executable-shadow-metrics">
      ${metric("ST / 新股", `${gate.exclude_st ? "排除" : "--"} / ${gate.exclude_new ? "排除" : "--"}`)}
      ${metric("上市 / 历史最少", `${count(gate.minimum_listing_days)} 天 / ${count(gate.minimum_history_sessions)} 日`)}
      ${metric("最低冻结日成交额", money(gate.minimum_amount_cny))}
      ${metric("风险上限", number(gate.maximum_risk_score))}
      ${metric("可交易性下限", number(gate.minimum_tradability_score))}
      ${metric("容量参与率上限", percent(gate.maximum_notional_share_of_session_amount))}
    </div>
    <div class="executable-shadow-warning"><strong>ADV unavailable</strong><span>容量仅使用冻结当日成交额参与率代理，不是可信历史 ADV。</span></div>
    <div class="executable-shadow-warning"><strong>日线成交代理</strong><span>停牌仅用冻结日K成交额与原因文本；涨跌停/一字仅用冻结日K单一价格代理，不能证明盘口排队成交。</span></div>
  </section>`;
}

function exposureHtml(audit) {
  return `<section class="executable-shadow-section" aria-labelledby="executableShadowExposureTitle">
    <h5 id="executableShadowExposureTitle">风险、行业与板块暴露</h5>
    <div class="executable-shadow-metrics">
      ${metric("加权平均风险", number(audit.average_risk_score))}
      ${metric("加权平均可交易性", number(audit.average_tradability_score))}
      ${metric("入选权重 / Top10", `${percent(audit.selected_weight)} / ${percent(audit.top10_weight)}`)}
    </div>
    <div class="executable-shadow-exposures">
      ${weightList("行业暴露", audit.industry_weights)}
      ${weightList("板块暴露", audit.board_weights)}
    </div>
  </section>`;
}

function selectedHtml(selected) {
  const rows = selected.map((item, index) => `<tr>
    <td>${index + 1}</td><td>${optionalRank(item.original_rank)}</td><td>${optionalRank(item.utility_rank)}</td>
    <td><strong>${escapeHtml(item.name)}</strong><br><span>${escapeHtml(item.symbol)} · ${escapeHtml(item.board)}</span></td>
    <td>${escapeHtml(item.industry ?? "--")}</td><td>${number(item.risk)} / ${number(item.tradability)}</td>
    <td>${percent(item.target_weight)} / ${count(item.target_quantity)} 股</td>
    <td>${money(item.estimated_gross_amount_cny)} / ${money(item.estimated_round_trip_cost_cny)}</td>
    <td>${escapeHtml(STATUS_LABELS[item.status] || item.status)}</td>
  </tr>`).join("");
  return `<section class="executable-shadow-section" aria-labelledby="executableShadowSelectedTitle">
    <h5 id="executableShadowSelectedTitle">入选候选：原生产排名与 Shadow 顺序</h5>
    <div class="executable-shadow-table-wrap" role="region" aria-label="可执行候选 Shadow 入选表" tabindex="0">
      <table><thead><tr><th>Shadow 顺序</th><th>生产原排名</th><th>效用排名</th><th>股票</th><th>行业</th><th>风险 / 可交易</th><th>权重 / 数量</th><th>金额 / 成本</th><th>状态</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="9">当前约束下没有入选候选。</td></tr>'}</tbody></table>
    </div>
  </section>`;
}

function exclusionsHtml(preview) {
  const counts = new Map();
  preview.flatMap((item) => item.hard_filter_failures).forEach((reason) => {
    counts.set(reason, (counts.get(reason) || 0) + 1);
  });
  const rows = [...counts.entries()].sort((left, right) => right[1] - left[1]);
  if (!rows.length) return "";
  const items = rows.map(([reason, amount]) => `<li><span>${escapeHtml(reason)}</span><strong>${amount}</strong></li>`).join("");
  return `<section class="executable-shadow-section"><h5>候选预览中的主要硬筛选原因</h5><ul class="executable-shadow-reason-list">${items}</ul></section>`;
}

function limitationsHtml(report) {
  const items = report.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<section class="executable-shadow-section executable-shadow-limitations">
    <h5>研究边界与可复核摘要</h5><ul>${items}</ul>
    <p><strong>生产评分合同：</strong><code>${escapeHtml(report.evidence.production_score_spec_hash)}</code></p>
    <p><strong>Shadow 结果摘要：</strong><code>${escapeHtml(report.canonical_digest)}</code></p>
  </section>`;
}

function weightList(title, weights) {
  const rows = Object.entries(weights).sort((left, right) => right[1] - left[1]);
  const items = rows.map(([label, value]) => `<li><span>${escapeHtml(label)}</span><strong>${percent(value)}</strong></li>`).join("");
  return `<div><h6>${escapeHtml(title)}</h6><ul>${items || "<li><span>无入选暴露</span><strong>--</strong></li>"}</ul></div>`;
}

function messages(title, values) {
  if (!values.length) return "";
  return `<div class="executable-shadow-messages"><strong>${escapeHtml(title)}</strong><ul>${values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`;
}

function metric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function money(value) {
  if (missing(value)) return "--";
  return `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
}

function percent(value) {
  if (missing(value)) return "--";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function number(value) {
  if (missing(value)) return "--";
  return Number(value).toFixed(2);
}

function count(value) {
  if (missing(value)) return "--";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function optionalRank(value) {
  return missing(value) ? "--" : String(value);
}

function missing(value) {
  return value === null || value === undefined || value === "";
}

function announce(elements, message, kind) {
  elements.executableShadowStatus.textContent = message;
  elements.executableShadowStatus.dataset.kind = kind;
}

function setBusy(elements, busy) {
  elements.strategyExecutableShadow.setAttribute("aria-busy", String(busy));
  elements.executableShadowRunId.disabled = busy;
  elements.executableShadowNotional.disabled = busy;
  elements.executableShadowUseCurrent.disabled = busy;
  elements.executableShadowGenerate.disabled = busy;
  elements.executableShadowCancel.hidden = !busy;
}

function completeElementContract(elements) {
  if (Object.values(elements).some((element) => !element)) return false;
  if (typeof elements.strategyExecutableShadow.setAttribute !== "function") return false;
  for (const name of ["executableShadowForm", "executableShadowUseCurrent", "executableShadowCancel"]) {
    if (typeof elements[name].addEventListener !== "function") return false;
  }
  return Boolean(elements.executableShadowStatus.dataset && elements.executableShadowResult.dataset);
}
