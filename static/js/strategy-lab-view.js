import { escapeHtml } from "./dom.js";

const STATUS_LABELS = {
  selected: "已入选", rejected: "未入选", constraint_adjusted: "约束后调整", unfilled: "无法成交",
  ready: "草案就绪", no_trade: "不交易", blocked: "已阻断",
  insufficient_data: "样本不足", eligible_for_manual_review: "仅可人工评审",
};

export function strategyLabElements(root) {
  const ids = [
    "strategyLab", "strategyLabStatus", "strategyLabAnnouncement", "strategyNaturalText", "strategyName",
    "strategyParse", "strategyParseOutput", "strategyProfile", "strategyStockCount", "strategyIndustryCount",
    "strategyCustomObjectivesField", "strategyObjectiveAlpha1d", "strategyObjectiveAlpha5d", "strategyObjectiveAlpha20d",
    "strategyObjectiveConfidence", "strategyObjectiveRisk", "strategyObjectiveTradability",
    "strategyWeightingMethod", "strategyMaxStockWeight", "strategyMaxIndustryWeight", "strategyMaxBoardWeight",
    "strategyMinPositionAmount", "strategyCapacityPct", "strategyCustomWeightsField", "strategyCustomWeights",
    "strategyBuyThreshold", "strategyHoldThreshold", "strategyHoldSessions", "strategyMinAmount", "strategyMinQuality", "strategySavedSelect", "strategyListRefresh",
    "strategyLoad", "strategyCopy", "strategySave", "strategyPlanState", "strategyPlanContent", "strategyNotional",
    "strategyExecuteLatest", "strategyReplayDate", "strategyExecuteReplay", "strategyCreateSimulation",
    "strategyCreateSchedule", "strategyArchive", "strategyLifecycleContent", "strategyExecutionContext", "strategyCandidateRows",
    "strategyCandidatePage", "strategyCandidatePrev", "strategyCandidateNext", "strategyPortfolioState",
    "strategyPortfolioContent", "strategyEvidenceRefresh", "strategyEvidenceContent", "strategyHistoryRefresh",
    "strategyCompareLeft", "strategyCompareRight", "strategyCompare", "strategyVersionLeft", "strategyVersionRight",
    "strategyVersionCompare", "strategyHistoryContent",
    "strategyCandidateDialog", "strategyCandidateDialogClose", "strategyCandidateDialogContent",
  ];
  return Object.fromEntries(ids.map((id) => [id, required(root, id)]));
}

export function renderStrategyList(elements, page, selectedId = null) {
  const items = page.items || [];
  elements.strategySavedSelect.innerHTML = items.length
    ? items.map((item) => `<option value="${item.strategy_id}">${escapeHtml(item.spec.name)} · v${item.strategy_version}${item.archived ? " · 已归档" : ""}</option>`).join("")
    : '<option value="">尚无策略</option>';
  const desired = selectedId && items.some((item) => item.strategy_id === selectedId) ? String(selectedId) : "";
  if (desired) elements.strategySavedSelect.value = desired;
  const enabled = Boolean(elements.strategySavedSelect.value);
  elements.strategyLoad.disabled = !enabled;
  elements.strategyCopy.disabled = !enabled;
}

export function renderParsedStrategy(elements, parsed) {
  const defaults = messageList("系统默认", parsed.applied_defaults, "");
  const ambiguities = messageList("待确认歧义", parsed.ambiguities, "warn");
  const unsupported = messageList("未支持条件", parsed.unsupported_clauses, "error");
  elements.strategyParseOutput.dataset.state = parsed.unsupported_clauses.length ? "blocked" : "ready";
  elements.strategyParseOutput.innerHTML = `
    <p><strong>原始文本：</strong>${escapeHtml(parsed.original_text)}</p>
    ${defaults}${ambiguities}${unsupported}
    <p><strong>确认方式：</strong>检查下方结构化字段；“趋势较强/风险较低”等模糊表达由目标画像明确化。点击“确认并保存”后才写入。</p>`;
  renderExecutionPlan(elements, parsed.compile);
}

export function renderExecutionPlan(elements, compiled) {
  const plan = compiled.execution_plan || {};
  const state = plan.executable ? "可执行" : "需处理";
  elements.strategyPlanState.textContent = state;
  elements.strategyPlanState.className = `strategy-state-pill ${plan.executable ? "ready" : "blocked"}`;
  const filters = (plan.expressions || []).map((item) => item.display).join("；") || "仅使用默认排除规则";
  elements.strategyPlanContent.innerHTML = `
    <div class="strategy-summary-grid">
      ${metric("股票池", (plan.board_labels || []).join(" / ") || "--")}
      ${metric("预计工作", plan.estimated_work || "--")}
      ${metric("是否启动扫描", plan.will_start_scan ? "是" : "否（dry-run）")}
      ${metric("过滤", filters)}
      ${metric("目标顺序", (plan.objective_order || []).join(" → ") || "--")}
      ${metric("交易约束", plan.execution_summary || "--")}
    </div>
    ${messageList("阻断原因", plan.blocked_reasons, "error")}
    ${messageList("编译警告", compiled.warnings, "warn")}`;
}

export function renderPortfolioDraft(elements, draft) {
  const summary = draft.summary;
  elements.strategyPortfolioState.textContent = STATUS_LABELS[summary.status] || summary.status;
  elements.strategyExecutionContext.textContent = `策略 v${draft.context.strategy_version} · 扫描 #${draft.context.market_scan_run_id} · 数据 ${draft.context.data_date}`;
  const rows = (draft.selected || []).slice(0, 20).map((item) => `
    <div class="strategy-portfolio-row"><span><strong>${escapeHtml(item.name)}</strong> ${escapeHtml(item.symbol)} · ${escapeHtml(item.board_label)}</span><span>${percent(item.target_weight)} · ${item.target_quantity} 股</span></div>`).join("");
  elements.strategyPortfolioContent.innerHTML = `
    <div class="strategy-summary-grid">
      ${metric("状态", STATUS_LABELS[summary.status] || summary.status)}
      ${metric("入选 / 评估", `${summary.selected_count} / ${summary.evaluated_count}`)}
      ${metric("证据已校验", summary.evidence_verified_count)}
      ${metric("预计换手", percent(summary.estimated_turnover))}
      ${metric("往返成本", money(summary.estimated_round_trip_cost_cny))}
      ${metric("剩余现金", money(summary.residual_cash_cny))}
    </div>
    ${messageList("no_trade 原因", summary.no_trade_reasons, "error")}
    <div class="strategy-portfolio-list">${rows || "<p>约束后没有可形成的组合。</p>"}</div>`;
}

export function renderCandidatePage(elements, page) {
  elements.strategyCandidateRows.innerHTML = page.items.length
    ? page.items.map(candidateRow).join("")
    : '<tr><td colspan="7">当前分页没有候选。</td></tr>';
  elements.strategyCandidatePage.textContent = `第 ${page.page} / ${page.page_count || 0} 页 · 共 ${page.total} 只`;
  elements.strategyCandidatePrev.disabled = page.page <= 1;
  elements.strategyCandidateNext.disabled = page.page_count === 0 || page.page >= page.page_count;
}

export function renderEvidence(elements, evidence) {
  if (!evidence) {
    elements.strategyEvidenceContent.innerHTML = "<p>尚未生成证据快照。显式刷新会读取已保留的离线只读评估产物，不会在请求内启动全量回测。</p>";
    return;
  }
  const promotion = evidence.promotion;
  const top = evidence.top_n[0] || {};
  const coverage = evidence.coverage[0] || {};
  elements.strategyEvidenceContent.innerHTML = `
    <div class="strategy-evidence-metrics">
      ${metric("证据状态", STATUS_LABELS[evidence.status] || evidence.status)}
      ${metric("独立交易日", `${promotion.observed_independent_session_count} / ${promotion.required_independent_session_count}`)}
      ${metric("全市场时点覆盖", percent(coverage.coverage_ratio))}
      ${metric("Rank IC", number(topValue(evidence.rank_evidence, "rank_ic")))}
      ${metric("ICIR", number(topValue(evidence.rank_evidence, "icir")))}
      ${metric("PBO / 多重检验", promotion.pbo_ready ? "已就绪" : "未就绪")}
      ${metric("自定义执行摘要", evidence.execution.evidence_digest_verified ? "校验通过" : "未校验")}
      ${metric("离线基线生成", evidence.baseline_generated_at || "未知")}
      ${metric("Top N 毛收益", percent(top.gross_return))}
      ${metric("Top N 净收益", percent(top.net_return))}
      ${metric("最大回撤 / MAE", `${percent(top.maximum_drawdown)} / ${percent(top.maximum_adverse_excursion)}`)}
    </div>
    ${messageList("晋级阻断", promotion.blockers, "error")}
    <p><strong>结论：</strong>${escapeHtml(promotion.conclusion)}</p>
    <p><strong>指纹：</strong><code>${escapeHtml(evidence.strategy_fingerprint)}</code></p>
    <p><strong>离线报告摘要：</strong><code>${escapeHtml(evidence.baseline_report_digest || "未知")}</code></p>
    ${messageList("数据来源", evidence.data_sources, "ready")}
    ${messageList("新鲜度", evidence.freshness_notes, "warn")}
    ${messageList("限制", evidence.limitations, "warn")}`;
}

export function renderHistory(elements, executions, versions) {
  const options = (executions.items || []).map((item) => `<option value="${item.execution_id}">#${item.execution_id} · v${item.strategy_version} · ${escapeHtml(item.data_date)} · ${escapeHtml(item.kind)}</option>`).join("");
  elements.strategyCompareLeft.innerHTML = `<option value="">请选择</option>${options}`;
  elements.strategyCompareRight.innerHTML = `<option value="">请选择</option>${options}`;
  if ((executions.items || []).length > 1) {
    elements.strategyCompareLeft.value = String(executions.items[1].execution_id);
    elements.strategyCompareRight.value = String(executions.items[0].execution_id);
  }
  elements.strategyCompare.disabled = (executions.items || []).length < 2;
  const versionOptions = (versions.items || []).map((item) => `<option value="${item.revision}">v${item.revision} · ${escapeHtml(item.name)}</option>`).join("");
  elements.strategyVersionLeft.innerHTML = `<option value="">请选择</option>${versionOptions}`;
  elements.strategyVersionRight.innerHTML = `<option value="">请选择</option>${versionOptions}`;
  if ((versions.items || []).length > 1) {
    elements.strategyVersionLeft.value = String(versions.items[1].revision);
    elements.strategyVersionRight.value = String(versions.items[0].revision);
  }
  elements.strategyVersionCompare.disabled = (versions.items || []).length < 2;
  const revisionRows = (versions.items || []).map((item) => `<div class="strategy-history-row"><span>v${item.revision} · ${escapeHtml(item.name)}</span><code>${escapeHtml(item.fingerprint.slice(0, 12))}…</code></div>`).join("");
  elements.strategyHistoryContent.innerHTML = `<p>历史执行 ${executions.total || 0} 次；不可变版本 ${versions.total || 0} 个。</p><div class="strategy-history-list">${revisionRows || "暂无版本"}</div>`;
}

export function renderVersionComparison(elements, comparison) {
  const paths = (comparison.changed_paths || []).map((path) => `<li><code>${escapeHtml(path)}</code></li>`).join("");
  elements.strategyHistoryContent.innerHTML = `
    <p>StrategySpec v${comparison.left_revision} → v${comparison.right_revision}；语义指纹${comparison.left_fingerprint === comparison.right_fingerprint ? "相同" : "不同"}。</p>
    <div><strong>变更字段</strong><ul class="strategy-message-list">${paths || "<li>没有执行语义变更</li>"}</ul></div>`;
}

export function renderSimulationPlan(elements, plan) {
  elements.strategyLifecycleContent.innerHTML = `
    <div class="strategy-summary-grid">
      ${metric("模拟计划", `#${plan.plan_id}`)}
      ${metric("状态", plan.status)}
      ${metric("纸面委托", `${(plan.orders || []).length} 条`)}
      ${metric("计划摘要", String(plan.plan_digest || "").slice(0, 12) + "…")}
    </div>
    ${messageList("研究边界", plan.disclaimers, "warn")}`;
}

export function renderSchedule(elements, schedule) {
  elements.strategyLifecycleContent.innerHTML = `
    <div class="strategy-summary-grid">
      ${metric("定时任务", `#${schedule.schedule_id}`)}
      ${metric("固定版本", `v${schedule.strategy_version}`)}
      ${metric("执行口径", schedule.cadence)}
      ${metric("状态", schedule.enabled ? "已启用" : "已停用")}
    </div>
    <p>任务固定绑定策略指纹 <code>${escapeHtml(String(schedule.strategy_fingerprint || "").slice(0, 16))}…</code>，不会跟随未来修订漂移。</p>`;
}

export function resetStrategyExecutionView(elements) {
  elements.strategyExecutionContext.textContent = "尚未执行";
  elements.strategyCandidateRows.innerHTML = '<tr><td colspan="7">执行策略后显示候选；每页最多 50 行。</td></tr>';
  elements.strategyCandidatePage.textContent = "--";
  elements.strategyCandidatePrev.disabled = true;
  elements.strategyCandidateNext.disabled = true;
  elements.strategyPortfolioState.textContent = "等待执行";
  elements.strategyPortfolioContent.innerHTML = "<p>组合草案会单独标记约束和无法成交状态，不会篡改生产原始排名。</p>";
  elements.strategyLifecycleContent.innerHTML = "<p>模拟计划和定时任务会固定绑定当前策略版本与全部证据指纹。</p>";
}

export function renderComparison(elements, comparison) {
  const rows = [
    ...changeRows("新增", comparison.added),
    ...changeRows("移除", comparison.removed),
    ...changeRows("保留但变化", comparison.retained_changed),
  ].join("");
  elements.strategyHistoryContent.innerHTML = `
    <p>策略指纹${comparison.same_strategy_fingerprint ? "相同" : "不同"}；评分规则${comparison.same_rule_version ? "相同" : "不同"}。</p>
    <div class="strategy-history-list">${rows || "<p>两个组合草案没有成员、排名或权重差异。</p>"}</div>`;
}

export function renderCandidateEvidence(elements, item) {
  const sensitivity = Object.entries(item.rank_sensitivity || {}).map(([key, shift]) => `${key}：${shift > 0 ? "+" : ""}${shift}`).join("；") || "本地扰动下无可用结果";
  elements.strategyCandidateDialogContent.innerHTML = `
    <div class="strategy-summary-grid">
      ${metric("生产原排名", item.original_rank || "--")}${metric("策略效用排名", item.utility_rank || "--")}${metric("Pareto 前沿", item.pareto_front ? "是" : "否")}
      ${metric("证据校验", item.evidence_verified ? "通过" : "未通过")}${metric("数据新鲜度", item.evidence_freshness)}${metric("组合状态", STATUS_LABELS[item.status] || item.status)}
    </div>
    ${messageList("为什么入选 / 未入选", item.reasons, "")}
    ${messageList("失败的硬条件", item.hard_filter_failures, "error")}
    ${messageList("进入候选的最小改变", item.minimum_changes, "warn")}
    <p><strong>边际贡献：</strong>${escapeHtml(contributionText(item.marginal_contributions))}</p>
    <p><strong>权重 ±10% 排名变化：</strong>${escapeHtml(sensitivity)}</p>
    <p><strong>排名说明：</strong>${escapeHtml(item.rank_change_reason)}</p>`;
  elements.strategyCandidateDialog.showModal?.();
}

export function setStrategyLabBusy(elements, busy, text, kind = "busy") {
  elements.strategyLabStatus.textContent = text;
  elements.strategyLabStatus.dataset.kind = kind;
  elements.strategyLab.setAttribute("aria-busy", String(Boolean(busy)));
  [elements.strategyParse, elements.strategySave, elements.strategyExecuteLatest, elements.strategyExecuteReplay].forEach((button) => {
    button.disabled = Boolean(busy) || button.dataset.available === "false";
  });
}

export function announceStrategyLab(elements, text, kind = "ready") {
  elements.strategyLabAnnouncement.textContent = text;
  elements.strategyLabStatus.textContent = text;
  elements.strategyLabStatus.dataset.kind = kind;
}

function candidateRow(item) {
  const ranks = `${item.original_rank || "--"} / ${item.utility_rank || "--"}`;
  const alpha = `${number(item.alpha_1d)} / ${number(item.alpha_5d)} / ${number(item.alpha_20d)}`;
  const qualities = `${number(item.confidence)} / ${number(item.risk)} / ${number(item.tradability)}`;
  return `<tr data-pareto="${Boolean(item.pareto_front)}"><td>${ranks}</td><td><strong>${escapeHtml(item.name)}</strong><br>${escapeHtml(item.symbol)} · ${escapeHtml(item.board_label)}</td><td>${number(item.utility_score)}${item.pareto_front ? " · Pareto" : ""}</td><td>${alpha}</td><td>${qualities}</td><td class="status-${escapeHtml(item.status)}">${escapeHtml(STATUS_LABELS[item.status] || item.status)}</td><td><button type="button" class="mini-button" data-strategy-candidate="${escapeHtml(item.symbol)}">证据与反事实</button></td></tr>`;
}

function messageList(title, values, kind) {
  if (!Array.isArray(values) || !values.length) return "";
  const items = values.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<div><strong>${escapeHtml(title)}</strong><ul class="strategy-message-list ${kind}">${items}</ul></div>`;
}

function metric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "--")}</strong></div>`;
}

function contributionText(values) {
  return Object.entries(values || {}).map(([name, value]) => `${name} ${number(value)}`).join("；") || "--";
}

function changeRows(label, values) {
  return (values || []).map((item) => `<div class="strategy-history-row"><span>${label} · ${escapeHtml(item.name)} ${escapeHtml(item.symbol)}</span><span>排名 ${item.left_rank || "--"} → ${item.right_rank || "--"} · 权重 ${percent(item.left_weight)} → ${percent(item.right_weight)}</span></div>`);
}

function topValue(values, key) {
  return Array.isArray(values) && values.length ? values[0][key] : null;
}

function money(value) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? `¥${numberValue.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}` : "--";
}

function percent(value) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? `${(numberValue * 100).toFixed(2)}%` : "--";
}

function number(value) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue.toFixed(2) : "--";
}

function required(root, id) {
  const element = root.getElementById(id);
  if (!element) throw new Error(`缺少策略实验室界面元素：${id}`);
  return element;
}
