import { $, escapeHtml, setMetricTone } from "./dom.js";
import { changeClass, formatAmount, formatNumber, toneByScore, toneByText } from "./format.js";
import {
  asArray,
  asObject,
  escapedJoin,
  levelToneClass,
  renderEscapedItems,
  renderList,
  renderOptionalTag,
} from "./workbench-render-utils.js";

export function renderAnalysis(data, { state, drawKline } = {}) {
  const analysis = asObject(data);
  const quote = asObject(analysis.quote);
  const actionAdvice = asObject(analysis.action_advice);
  const quality = asObject(analysis.data_quality);
  if (state) state.lastAnalysis = data;
  $("stockCode").textContent = stockCodeText(quote);
  $("stockName").textContent = quote.name || "--";
  $("stockPrice").textContent = formatNumber(quote.price);
  $("stockChange").textContent = `${formatNumber(quote.change)} / ${formatNumber(quote.change_pct)}%`;
  $("stockChange").className = changeClass(quote.change_pct);
  $("trendScore").textContent = `${analysis.trend_score ?? "--"}`;
  $("trendLabel").textContent = analysis.trend_label || "--";
  $("actionAdvice").textContent = actionAdvice.action
    ? `${actionAdvice.action} · 建议强度 ${actionAdvice.confidence ?? "--"}/100`
    : "--";
  $("support").textContent = formatNumber(analysis.support);
  $("resistance").textContent = formatNumber(analysis.resistance);
  $("ma5").textContent = formatNumber(analysis.ma5);
  $("ma20").textContent = formatNumber(analysis.ma20);
  $("dataQuality").textContent = analysis.data_quality ? `${quality.level || "--"} ${quality.score ?? "--"}分` : "--";
  $("summary").textContent = analysis.beginner_summary || "";
  setMetricTone("trendScore", toneByScore(analysis.trend_score, 68, 45));
  setMetricTone("trendLabel", toneByText(analysis.trend_label));
  setMetricTone("actionAdvice", toneByText(actionAdvice.action));
  setMetricTone("support", "");
  setMetricTone("resistance", "");
  setMetricTone("ma5", maTone(quote.price, analysis.ma5, "warn"));
  setMetricTone("ma20", maTone(quote.price, analysis.ma20, "risk"));
  setMetricTone("dataQuality", analysis.data_quality ? toneByScore(quality.score, 85, 70) : "warn");
  renderQuality(analysis.data_quality ? quality : null);
  renderSignalEvidence(analysis.signal_snapshot);
  renderSignals("buySignals", analysis.buy_points);
  renderSignals("sellSignals", analysis.sell_points);
  renderSignals("tSignals", analysis.t_plan);
  renderReview(analysis.review);
  if (typeof drawKline === "function") drawKline(asArray(analysis.klines), analysis.ma5, analysis.ma20);
}

export function renderInsights(data, state) {
  const insights = asObject(data);
  const overview = asObject(insights.overview);
  if (state) state.lastInsights = data;
  renderInsightOverview(overview);
  renderFactors(overview.factors);
  renderFundFlow(asObject(insights.fund_flow));
  renderOrderPressure(asObject(insights.order_pressure));
  renderStrategyCards(insights.strategy_cards);
  renderStockEvents(asObject(insights.events));
  renderFinancialHealth(asObject(insights.financial_health));
  renderValuation(asObject(insights.valuation));
  renderAbnormalEvents(asObject(insights.abnormal_events));
  renderLhb(asObject(insights.lhb));
  renderRuleMatches(asObject(insights.rule_matches));
}

function stockCodeText(quote) {
  const market = quote.market || "";
  const code = quote.code || "";
  return market || code ? `${market}${code}` : "--";
}

function maTone(price, average, lowerTone) {
  const current = Number(price);
  const ma = Number(average);
  if (!Number.isFinite(current) || !Number.isFinite(ma)) return "";
  return current >= ma ? "good" : lowerTone;
}

function renderInsightOverview(overview) {
  const takeaways = asArray(overview.beginner_takeaways);
  const keyPrices = asArray(overview.key_prices);
  $("insightOverview").innerHTML = `
    <div class="overview-score">
      <div>
        <span>全景评分</span>
        <strong>${escapeHtml(overview.total_score)}<small>/100</small></strong>
      </div>
      <i>${escapeHtml(overview.total_level)}</i>
    </div>
    <div class="overview-content">
      <strong>主要矛盾</strong>
      <p>${escapeHtml(overview.main_conflict)}</p>
      <div class="takeaways">
        ${renderEscapedItems(takeaways, "span")}
      </div>
      <div class="key-price-list">
        ${renderList(
          keyPrices,
          (item) => `
            <div>
              <span>${escapeHtml(item.label)}</span>
              <strong>${formatNumber(item.price)}</strong>
              <small>${escapeHtml(item.note)}</small>
            </div>`
        )}
      </div>
    </div>
  `;
}

function renderFactors(items) {
  $("factorList").innerHTML = renderList(
    items,
    (item) => `
        <div class="factor-item">
          <div class="factor-head">
            <strong>${escapeHtml(item.name)}</strong>
            <span>${escapeHtml(item.score)} · ${escapeHtml(item.level)}</span>
          </div>
          <div class="score-bar"><i style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></i></div>
          <p>${escapeHtml(item.summary)}</p>
          ${renderEscapedItems(item.evidence, "small")}
          ${asArray(item.missing_data).length ? `<em>待补充：${escapedJoin(item.missing_data, "、")}</em>` : ""}
        </div>`
  );
}

function renderFundFlow(flow) {
  const windows = asArray(flow.windows);
  const notes = asArray(flow.notes);
  const nature = dataNatureLabel(flow.data_nature);
  const score = flow.data_nature === "unavailable" || !flow.data_nature ? "不可用" : `${flow.overall_score ?? "--"} · ${flow.level || "--"}`;
  $("fundFlowPanel").innerHTML = `
    <div class="flow-head">
      <strong>量价热度 ${escapeHtml(score)}</strong>
      <span>${escapeHtml(nature)} · ${escapeHtml(flow.source)}</span>
    </div>
    <p>${escapeHtml(flow.price_volume_relation)}</p>
    <div class="flow-windows">
      ${renderList(
        windows,
        (item) => `
          <div>
            <span>${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(flow.data_nature === "unavailable" || !flow.data_nature ? "--" : item.score)}</strong>
            <small>${escapeHtml(item.summary)}</small>
          </div>`
      )}
    </div>
    ${renderEscapedItems(notes, "small")}
  `;
}

function renderOrderPressure(order) {
  const notes = asArray(order.notes);
  $("orderPressurePanel").innerHTML = `
    <div class="flow-head">
      <strong>订单压力 · ${escapeHtml(dataNatureLabel(order.data_nature))}</strong>
      <span>${escapeHtml(order.pressure_level)} · ${escapeHtml(order.source)}</span>
    </div>
    <p>${escapeHtml(order.summary)}</p>
    <div class="mini-metrics">
      <span>买卖比：${order.bid_ask_ratio === null || order.bid_ask_ratio === undefined ? "--" : escapeHtml(order.bid_ask_ratio)}</span>
      <span>价差：${order.spread_pct === null || order.spread_pct === undefined ? "--" : `${formatNumber(order.spread_pct, 4)}%`}</span>
    </div>
    ${renderEscapedItems(notes, "small")}
  `;
}

function renderFinancialHealth(health) {
  const metrics = asArray(health.metrics);
  const scoreAvailable = health.score_available === true
    && health.formal_minimum_complete === true
    && health.score !== null
    && health.score !== undefined
    && Number.isFinite(Number(health.score));
  const title = scoreAvailable
    ? `财务体检 ${health.score} · ${health.level || "--"}`
    : "市场估值与交易体征 · 财务体检分不可用";
  $("financialPanel").innerHTML = `
    <div class="finance-head">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(health.source)}</span>
    </div>
    <p>${escapeHtml(health.summary)}</p>
    <div class="metric-stack">
      ${renderList(
        metrics,
        (item) => `
          <div>
            <strong>${escapeHtml(item.name)} <span>${escapeHtml(item.value)}</span></strong>
            <small>${escapeHtml(item.summary)}</small>
          </div>`,
        { limit: 5 }
      )}
    </div>
  `;
}

function dataNatureLabel(value) {
  return {
    derived: "衍生（derived）",
    estimated: "估算（estimated）",
    observed: "实测（observed）",
    unavailable: "不可用（unavailable）",
  }[value] || "不可用（unavailable，旧数据未标注）";
}

function renderValuation(valuation) {
  const metrics = valuationMetrics(valuation);
  const evidence = asArray(valuation.evidence);
  const watchPoints = asArray(valuation.watch_points);
  $("valuationPanel").innerHTML = `
    <div class="finance-head">
      <strong>估值 ${escapeHtml(valuation.score)} · ${escapeHtml(valuation.level)}</strong>
      <span>${escapeHtml(valuation.market_cap_text || valuation.source)}</span>
    </div>
    <p>${escapeHtml(valuation.summary)}</p>
    <div class="mini-metrics">
      ${renderEscapedItems(metrics, "span")}
    </div>
    ${renderEscapedItems(evidence, "small", { limit: 2 })}
    ${renderEscapedItems(watchPoints, "small", { limit: 3 })}
  `;
}

function valuationMetrics(valuation) {
  const anchor = valuation.valuation_anchor_label || "历史锚待确认";
  return [
    `PE：${formatOptionalNumber(valuation.pe)}`,
    `PB：${formatOptionalNumber(valuation.pb)}`,
    `${anchor}：PE ${formatOptionalPercent(valuation.pe_percentile)} / PB ${formatOptionalPercent(valuation.pb_percentile)}`,
    `同行分位：PE ${formatOptionalPercent(valuation.peer_pe_percentile)} / PB ${formatOptionalPercent(valuation.peer_pb_percentile)} · 样本 ${valuation.peer_sample_count || 0}`,
    `价格位置：${formatOptionalPercent(valuation.price_percentile)}`,
  ];
}

function formatOptionalNumber(value, digits = 2) {
  return formatNumber(value, digits);
}

function formatOptionalPercent(value) {
  const text = formatOptionalNumber(value, 1);
  return text === "--" ? text : `${text}%`;
}

function renderAbnormalEvents(summary) {
  const events = asArray(summary.events);
  $("abnormalPanel").innerHTML = `
    <div class="finance-head">
      <strong>${escapeHtml(summary.main_signal)} · ${escapeHtml(summary.level)}</strong>
      <span>评分 ${escapeHtml(summary.score)}</span>
    </div>
    <div class="event-list compact-list">
      ${renderList(events, renderAbnormalEvent, {
        limit: 4,
        empty: `<div class="stock-event"><strong>暂无明显异动</strong><p>当前未触发放量、跳空、长影线或涨跌停附近信号。</p></div>`,
      })}
    </div>
  `;
}

function renderAbnormalEvent(item) {
  return `
              <div class="stock-event">
                <strong>${escapeHtml(item.title)}<span>${escapeHtml(item.level)}</span></strong>
                <small>${escapeHtml(item.direction)} · ${escapeHtml(item.date)}</small>
                <p>${escapeHtml(item.description)}</p>
              </div>`;
}

function renderLhb(lhb) {
  const reasons = asArray(lhb.reasons);
  const actions = asArray(lhb.action_items);
  const available = lhb.available === true && lhb.capability_status !== "unavailable";
  const statusText = available ? "真实数据已接入" : "数据能力不可用";
  const capabilityMessage = lhb.capability_message || lhb.source || "未接入真实龙虎榜数据源。";
  $("lhbPanel").innerHTML = `
    <div class="finance-head">
      <strong>龙虎榜 · ${escapeHtml(statusText)}</strong>
      <span>${escapeHtml(available ? lhb.source : "未接入真实源")}</span>
    </div>
    <p>${escapeHtml(lhb.summary)}</p>
    ${available ? "" : `<small>${escapeHtml(capabilityMessage)}</small>`}
    ${reasons.length ? `<div class="event-actions"><small>量价异动核查依据（非龙虎榜证据）</small>${renderEscapedItems(reasons, "em", { limit: 4 })}</div>` : ""}
    ${actions.length ? `<div class="event-actions">${renderEscapedItems(actions, "em", { limit: 3 })}</div>` : ""}
    ${lhb.reliability ? `<small>可靠性：${escapeHtml(lhb.reliability)}</small>` : ""}
  `;
}

function renderRuleMatches(summary) {
  const matches = asArray(summary.matches);
  $("ruleMatches").innerHTML = renderList(
    matches,
    (item) => `
          <article class="rule-item">
            <div>
              <strong>${escapeHtml(item.name)}</strong>
              <span class="tag ${levelToneClass(item.level)}">${escapeHtml(item.status)} · ${escapeHtml(item.level)}</span>
            </div>
            <p>${escapeHtml(item.reason)}</p>
            <small>${escapeHtml((item.actions || [])[0] || item.invalidation)}</small>
          </article>`,
    { limit: 8, empty: `<article class="rule-item"><strong>暂无规则</strong><p>内置规则正在等待数据。</p></article>` }
  );
}

function renderStrategyCards(items) {
  $("strategyCards").innerHTML = renderList(
    items,
    (item) => `
      <article class="strategy-card">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <span class="tag ${levelToneClass(item.level)}">${escapeHtml(item.status)} · ${escapeHtml(item.level)}</span>
        </div>
        <p>${escapeHtml((item.current_evidence || [])[0] || "")}</p>
        <dl>
          <dt>参考价</dt><dd>${escapeHtml(item.reference_price)}</dd>
          <dt>失效</dt><dd>${escapeHtml(item.invalidation)}</dd>
          <dt>适合</dt><dd>${escapeHtml(item.suitable_for)}</dd>
        </dl>
      </article>`
  );
}

export function renderMinuteAnalysis(report) {
  const el = $("minuteAnalysis");
  if (!el || !report) return;
  const view = minuteAnalysisView(report);
  el.innerHTML = view.isUnavailable ? renderMinuteUnavailable(view) : renderMinuteDetails(view);
}

function minuteAnalysisView(report) {
  const tPlan = report.t_plan || {};
  const availability = minuteAvailabilityState(report);
  const missing = minuteMissingData(report, availability.status);
  return {
    report,
    tPlan,
    supports: asArray(report.supports),
    resistances: asArray(report.resistances),
    warnings: asArray(report.warnings),
    missing,
    availability: availability.status,
    availabilityReason: availability.reason,
    availabilityLabel: minuteAvailabilityLabel(availability.status),
    statusTone: minuteAvailabilityTone(availability.status),
    isUnavailable: availability.status === "unavailable",
    isDegraded: availability.status === "degraded",
  };
}

export function minuteAvailabilityState(report) {
  const hasExplicitAvailability = Boolean(
    report
    && typeof report === "object"
    && Object.prototype.hasOwnProperty.call(report, "availability")
  );
  const rawAvailability = hasExplicitAvailability ? report.availability : undefined;
  if (["ok", "degraded", "unavailable"].includes(rawAvailability)) {
    return {
      status: rawAvailability,
      reason: minuteAvailabilityReason(report, rawAvailability),
    };
  }
  if (hasExplicitAvailability) {
    return {
      status: "unavailable",
      reason: "分钟分析返回未知可用性状态，已按不可用处理。",
    };
  }
  const sampleCount = Number(report?.sample_count);
  const legacyMissing = asArray(report?.missing_data);
  const minuteKlineMissing = legacyMissing.some((item) => typeof item === "string" && item.includes("分钟K线"));
  if (!Number.isFinite(sampleCount) || sampleCount < 8 || minuteKlineMissing) {
    return {
      status: "unavailable",
      reason: "旧版分钟数据未声明可用性，且缺少可验证的分钟K线或有效样本不足 8 条，已按不可用处理。",
    };
  }
  return {
    status: "degraded",
    reason: "旧版分钟数据未声明可用性；价格结构仅作降级参考，量能与数据时效性待确认。",
  };
}

function minuteAvailabilityReason(report, availability) {
  const reason = typeof report?.availability_reason === "string" ? report.availability_reason.trim() : "";
  if (reason) return reason;
  return {
    ok: "分钟分析数据满足分析要求。",
    degraded: "分钟分析处于降级状态；价格结构仍可参考，受限结论以缺失数据提示为准。",
    unavailable: "分钟分析数据不可用，当前不形成盘中执行区间。",
  }[availability];
}

function minuteAvailabilityLabel(availability) {
  return {
    ok: "数据可用",
    degraded: "数据降级",
    unavailable: "数据不可用",
  }[availability] || "数据不可用";
}

function minuteAvailabilityTone(availability) {
  return {
    ok: "good",
    degraded: "warn",
    unavailable: "risk",
  }[availability] || "risk";
}

function minuteMissingData(report, availability) {
  const missing = asArray(report?.missing_data).filter((item) => typeof item === "string" && item.trim());
  if (missing.length) return missing;
  if (availability === "degraded") return ["分钟数据完整性或时效性"];
  if (availability === "unavailable") return ["可验证的分钟分析数据"];
  return [];
}

function renderMinuteUnavailable(view) {
  return `
      <div class="minute-status-card ${view.statusTone}">
        <div>
          <strong>分钟分析不可用</strong>
          <span>${escapeHtml(view.availabilityReason)}</span>
        </div>
        <i class="tag risk">${escapeHtml(view.availabilityLabel)}</i>
      </div>
      <div class="minute-empty">
        <strong>缺失数据：${escapeHtml(view.missing.join("、"))}</strong>
        <span>等待有效分钟K线恢复并重新分析后，再形成盘中参考区间。</span>
      </div>
    `;
}

function renderMinuteDetails(view) {
  return `
    ${view.isDegraded ? renderMinuteDegraded(view) : ""}
    ${renderMinuteHead(view)}
    <p>${escapeHtml(view.report.summary || "")}</p>
    <div class="minute-metrics">${renderMinuteMetrics(view.report)}</div>
    <div class="minute-zones">${renderMinuteZones(view)}</div>
    <div class="minute-steps">${renderMinuteSteps(view)}</div>
  `;
}

function renderMinuteDegraded(view) {
  return `
    <div class="minute-status-card warn">
      <div>
        <strong>分钟分析降级</strong>
        <span>${escapeHtml(view.availabilityReason)}</span>
      </div>
      <i class="tag">${escapeHtml(view.availabilityLabel)}</i>
    </div>
    <div class="minute-empty">
      <strong>受限数据：${escapeHtml(view.missing.join("、"))}</strong>
    </div>`;
}

function renderMinuteHead(view) {
  const { report, tPlan, statusTone } = view;
  return `
    <div class="minute-head">
      <div>
        <strong>${escapeHtml(report.trend_label)} · ${escapeHtml(report.momentum_label)}</strong>
        <span>${escapeHtml(report.interval)} · 样本 ${escapeHtml(report.sample_count)} · ${escapeHtml(report.source)} · 结论 ${escapeHtml(tPlan.suitability || "待确认")}</span>
      </div>
      <i class="${statusTone}" title="${escapeHtml(view.availabilityReason)}">${escapeHtml(view.availabilityLabel)}</i>
    </div>`;
}

function renderMinuteMetrics(report) {
  return `
      <span>最新价 <b>${formatNumber(report.latest_price)}</b></span>
      <span>区间涨跌 <b class="${changeClass(report.intraday_change_pct)}">${formatNumber(report.intraday_change_pct)}%</b></span>
      <span>盘中振幅 <b>${formatNumber(report.intraday_range_pct)}%</b></span>
      <span>量能 <b>${escapeHtml(report.volume_pulse || "--")}</b></span>`;
}

function renderMinuteZones(view) {
  return `
      ${renderMinuteZone("低吸参考", view.tPlan.low_zone, view.supports)}
      ${renderMinuteZone("高抛参考", view.tPlan.high_zone, view.resistances)}`;
}

function renderMinuteZone(title, zone, levels) {
  return `
      <div>
        <strong>${escapeHtml(title)}</strong>
        <b>${escapeHtml(zone || "--")}</b>
        ${minuteLevelItems(levels)}
      </div>`;
}

function renderMinuteSteps(view) {
  return `
      ${minuteStepItems(view.tPlan.execution_steps, 3)}
      ${minuteStepItems(view.tPlan.stop_conditions, 2, "risk")}
      ${minuteWarningItems(view.warnings, view.statusTone)}`;
}

function minuteLevelItems(items) {
  return renderList(
    items,
    (item) => `<span>${escapeHtml(item.label)} ${formatNumber(item.price)} · 强度 ${escapeHtml(item.strength)}</span>`,
    { limit: 2 }
  );
}

function minuteStepItems(items, limit, className = "") {
  return renderEscapedItems(items, "span", { limit, className });
}

function minuteWarningItems(items, statusTone) {
  const className = statusTone === "risk" ? "risk" : "warn";
  return renderEscapedItems(items, "span", { limit: 3, className });
}

function renderStockEvents(summary) {
  const stockEvents = asObject(summary);
  $("stockEvents").innerHTML = [
    renderStockEventList(stockEvents.events),
    renderStockEventFollowup(stockEvents),
  ]
    .filter(Boolean)
    .join("");
}

function renderStockEventList(events) {
  return renderList(events, renderStockEventCard, {
    empty: `<div class="stock-event"><strong>暂无事件</strong><p>当前没有可展示的真实或本地识别事件；未接入的外部源不会生成占位事件。</p></div>`,
  });
}

function renderStockEventCard(item) {
  return `
          <div class="stock-event">
            <strong>${escapeHtml(item.title)}<span>${escapeHtml(item.level)}</span></strong>
            <small>${eventMetaText(item)}</small>
            <p>${escapeHtml(item.description)}</p>
            ${renderOptionalTag("small", item.reliability, { prefix: "可靠性：" })}
            ${renderOptionalTag("em", item.action_hint)}
          </div>`;
}

function eventMetaText(item) {
  return [item.date, item.category, item.source].map(escapeHtml).join(" · ");
}

function renderStockEventFollowup(summary) {
  const steps = asArray(summary.next_steps).slice(0, 4);
  const missingSources = asArray(summary.missing_sources);
  const capabilities = asArray(summary.source_capabilities);
  if (!steps.length && !missingSources.length && !capabilities.length) return "";
  return `
      <div class="stock-event event-followup">
        <strong>下一步核查<span>清单</span></strong>
        ${renderEscapedItems(steps, "p")}
        ${renderEventSourceCapabilities(capabilities)}
        ${renderMissingSources(missingSources)}
      </div>`;
}

function renderEventSourceCapabilities(items) {
  if (!items.length) return "";
  return `<small>外部数据能力</small>${items
    .map((item) => {
      const capability = asObject(item);
      const status = capability.status === "available" ? "可用" : "不可用";
      return `<p><strong>${escapeHtml(capability.label || capability.key || "外部数据")}: ${status}</strong> ${escapeHtml(capability.detail || "")}</p>`;
    })
    .join("")}`;
}

function renderMissingSources(items) {
  return items.length ? `<small>待补数据：${escapedJoin(items, " / ")}</small>` : "";
}

function renderQuality(quality) {
  if (!quality) {
    $("qualityPanel").innerHTML = "";
    return;
  }
  const notes = asArray(quality.notes);
  const anomalies = asArray(quality.anomalies);
  const el = $("qualityPanel");
  el.className = `quality-panel ${toneByScore(quality.score, 85, 70)}`;
  el.innerHTML = `
    <div class="quality-head">
      <strong>${escapeHtml(quality.level)} · ${escapeHtml(quality.score)}分</strong>
      <span>${escapeHtml(quality.consistency_level)} · ${escapeHtml(quality.source)}</span>
    </div>
    <div class="quality-notes">
      ${renderEscapedItems(notes, "span")}
      ${anomalies.length ? `<span class="warn">异常：${escapeHtml(anomalies.join("；"))}</span>` : ""}
    </div>
  `;
}

function renderSignalEvidence(snapshot) {
  if (!snapshot) {
    $("signalEvidence").innerHTML = "";
    return;
  }
  $("signalEvidence").innerHTML = `
    <div class="evidence-head">
      <strong>本次结论依据</strong>
      <span>${escapeHtml(snapshot.label)} · 信号证据充分度 ${escapeHtml(snapshot.confidence)}/100</span>
    </div>
    <p>${escapeHtml(snapshot.summary)}</p>
    <div class="evidence-grid">
      ${signalEvidenceGroups(snapshot).map(renderEvidenceGroup).join("")}
    </div>
    ${renderRiskNotes(snapshot.risk_notes)}
  `;
}

function signalEvidenceGroups(snapshot) {
  return [
    ["加分依据", asArray(snapshot.positive), "good"],
    ["风险扣分", asArray(snapshot.negative), "risk"],
    ["中性观察", asArray(snapshot.neutral), ""],
  ];
}

function renderEvidenceGroup([title, items, tone]) {
  return `
          <div class="evidence-group">
            <strong>${escapeHtml(title)}</strong>
            ${renderEvidenceItems(title, items, tone)}
          </div>`;
}

function renderEvidenceItems(title, items, tone) {
  const rows = asArray(items);
  return rows.length ? rows.map((item) => renderEvidenceItem(item, tone)).join("") : emptyEvidenceItem(title);
}

function renderEvidenceItem(item, tone) {
  return `
                      <span class="${tone}">
                        <b>${escapeHtml(item.name)} ${formatEvidenceImpact(item.impact)}</b>
                        <small>${escapeHtml(item.reason)}</small>
                      </span>`;
}

function formatEvidenceImpact(value) {
  const prefix = Number(value) > 0 ? "+" : "";
  return `${prefix}${escapeHtml(value)}`;
}

function emptyEvidenceItem(title) {
  return `<span><b>暂无</b><small>当前没有明显${escapeHtml(title)}。</small></span>`;
}

function renderRiskNotes(items) {
  const rows = asArray(items);
  return rows.length ? `<div class="evidence-risks">${renderEscapedItems(rows, "span")}</div>` : "";
}

function renderReview(review) {
  if (!review) {
    $("reviewSummary").textContent = "历史复盘暂不可用。";
    $("reviewPoints").innerHTML = "";
    $("reviewEvents").innerHTML = "";
    return;
  }
  const keyPoints = asArray(review.key_points);
  const events = asArray(review.events);
  $("reviewSummary").textContent = review.review_summary || "";
  $("reviewPoints").innerHTML = renderList(
    keyPoints,
    (item) => `
      <div class="review-point">
        <span>${escapeHtml(item.label)}</span>
        <strong class="${item.level === "风险" ? "down" : item.level === "积极" ? "up" : ""}">${escapeHtml(item.value)}</strong>
      </div>`
  );
  $("reviewEvents").innerHTML = renderList(
    events,
    (item) => `
          <div class="review-event">
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.date)} · ${escapeHtml(item.level)}</span>
            <p>${escapeHtml(item.description)}</p>
          </div>`,
    { empty: `<div class="review-event"><strong>暂无异常事件</strong><p>近阶段没有触发明显大涨、大跌或高波动事件。</p></div>` }
  );
}

function renderSignals(id, items) {
  $(id).innerHTML = renderList(items, (item) => {
      const tagClass = levelToneClass(item.level);
      return `
        <article class="signal-item">
          <strong>${escapeHtml(item.title)}<span class="tag ${tagClass}">${escapeHtml(item.level)}</span></strong>
          <p>${escapeHtml(item.reason)}</p>
        </article>`;
    });
}

export function renderMarket(indices, meta = {}) {
  const items = asArray(indices);
  const warning = sampleWarning(meta);
  const rows = renderList(
    items,
    (item) => `
      <div class="index-card">
        <span>${escapeHtml(item.name)}</span>
        <strong>${formatNumber(item.price)}</strong>
        <em class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</em>
      </div>`,
    {
      empty: warning
        ? `<div class="index-card"><span>市场指数暂不可用</span><strong>数据降级</strong><em>${escapeHtml(warning)}</em></div>`
        : `<div class="index-card"><span>市场概览</span><strong>暂无数据</strong><em>等待刷新</em></div>`,
    }
  );
  const warningRow = items.length && warning
    ? `<div class="index-card"><span>指数数据提示</span><strong>部分可用</strong><em>${escapeHtml(warning)}</em></div>`
    : "";
  $("marketStrip").innerHTML = rows + warningRow;
}

export function renderStrongStocks(items, meta = {}) {
  const rows = asArray(items).map(asObject);
  const scope = meta && meta.scope ? `${meta.scope} · 样本 ${meta.sample_count ?? rows.length}` : `样本 ${rows.length}`;
  const fallbackReason = meta && meta.fallback_reason ? String(meta.fallback_reason) : "";
  const warning = sampleWarning(meta);
  const scopeNote = fallbackReason
    ? `强股接口暂不可用，显示市场概览样本：${fallbackReason}`
    : warning
      ? `数据降级：${warning}`
    : "仅代表当前样本内排序，不是全市场涨幅榜。";
  $("leaderList").innerHTML = rows.length
    ? `<div class="leader-row leader-scope"><strong>${escapeHtml(scope)}</strong><span>${escapeHtml(scopeNote)}</span></div>` +
      renderList(
        rows,
        (item) => `
      <div class="leader-row">
        <div>
          <strong>${escapeHtml(item.name || "--")} <span>${escapeHtml(item.code || "--")}</span></strong>
          <small>${escapeHtml(item.reason || "样本数据待确认")}</small>
          <small>${escapedJoin(item.tags, " / ")}</small>
        </div>
        <div>
          <div class="leader-rank">${escapeHtml(item.rank ?? "--")}</div>
          <span>龙头 ${escapeHtml(item.leader_score ?? 0)}</span>
          <span class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</span>
        </div>
      </div>`,
        { limit: 8 }
      )
    : fallbackReason || warning
      ? `<div class="leader-row"><strong>观察池数据暂不可用</strong><span>${escapeHtml(fallbackReason || warning)}</span></div>`
      : `<div class="leader-row"><strong>暂无观察池排序</strong><span>等待行情刷新后重新计算。</span></div>`;
}

function sampleWarning(meta) {
  const fallbackReason = meta && typeof meta.fallback_reason === "string" ? meta.fallback_reason.trim() : "";
  if (fallbackReason) return fallbackReason;
  return asArray(meta && meta.warnings).find((item) => typeof item === "string" && item.trim())?.trim() || "";
}

export function renderQuotes(items) {
  const rows = asArray(items);
  $("quoteList").innerHTML = renderList(
    rows,
    (item) => `
      <div class="quote-row">
        <div>
          <strong>${escapeHtml(item.name)}</strong>
          <span>${escapeHtml(item.market)}${escapeHtml(item.code)} · 成交额 ${formatAmount(item.amount)}${escapeHtml(quoteCacheLabel(item))}</span>
        </div>
        <div>
          <strong>${formatNumber(item.price)}</strong>
          <span class="${changeClass(item.change_pct)}">${formatNumber(item.change_pct)}%</span>
        </div>
      </div>`,
    { empty: `<div class="quote-row"><strong>实时观察等待中</strong><span>行情连接成功后自动更新。</span></div>` }
  );
}

function quoteCacheLabel(item) {
  if (!item) return "";
  if (item.fallback_used) return " · 兜底缓存";
  if (item.from_cache) return " · 缓存";
  return "";
}
