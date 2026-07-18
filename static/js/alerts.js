import { DEFAULT_REQUEST_TIMEOUT_MS, createRequestScope, fetchJson, isAbortError } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { toggleInlineEditor } from "./inline-editor.js";

const ALERT_ACTIVITY_READ_ERROR = "\u63d0\u9192\u8bb0\u5f55\u8bfb\u53d6\u5931\u8d25";
const ALERT_ACTIVITY_FORMAT_ERROR = "\u63d0\u9192\u8bb0\u5f55\u683c\u5f0f\u5f02\u5e38";
let renderedAlertRules = null;

export const ALERT_CONDITION_LABELS = Object.freeze({
  price_above: "价格高于",
  price_below: "价格低于",
  change_pct_above: "涨幅高于",
  change_pct_below: "跌幅低于",
  trend_score_above: "趋势评分高于",
  trend_score_below: "趋势评分低于",
  break_support: "跌破支撑",
  break_resistance: "突破压力",
});

export async function loadAlerts(state, options = {}) {
  const outcome = await loadAlertsOutcome(state, options);
  return outcome.current;
}

async function loadAlertsOutcome(state, options = {}) {
  const request = beginAlertsReadRequest(state, options);
  try {
    return await refreshAlerts(state, request);
  } finally {
    finishAlertsReadRequest(state, request);
  }
}

export async function addAlertRule(state, options = {}) {
  const symbol = options.symbol || state.symbol;
  const conditionType = $("alertType").value;
  const rawThreshold = $("alertThreshold").value.trim();
  const allowsDynamicLevel = conditionType === "break_support" || conditionType === "break_resistance";
  if (!rawThreshold && !allowsDynamicLevel) throw new Error("请输入有效阈值");
  const threshold = rawThreshold ? Number(rawThreshold) : 0;
  if (!Number.isFinite(threshold)) throw new Error("请输入有效阈值");
  const request = beginAlertMutation(state, options, symbol);
  try {
    await fetchJson("/api/alerts", requestOptions(request, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        condition_type: conditionType,
        threshold,
        note: "本地个股研究提醒",
      }),
    }));
    if (!request.isCurrent()) return false;
    if ($("alertThreshold").value.trim() === rawThreshold) $("alertThreshold").value = "";
    return await finishAlertMutation(state, request, { actionLabel: "添加", locallyReconciled: false });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    finishAlertMutationRequest(state, request);
  }
}

export async function evaluateAlerts(state, options = {}) {
  const button = $("evaluateAlerts");
  const symbol = options.symbol || state.symbol;
  const activeRequest = state.alertEvaluationOwner;
  if (button.disabled && activeRequest && activeRequest.symbol !== symbol) {
    clearAlertEvaluationView(state);
    releaseAlertEvaluationRequest(state, activeRequest);
  }
  if (button.disabled) return false;
  const request = beginAlertMutation(state, options, symbol);
  request.button = button;
  request.previousButtonText = button.textContent;
  state.alertEvaluationOwner = request;
  let viewOwner = null;
  try {
    button.disabled = true;
    button.textContent = "检查中";
    viewOwner = claimAlertEvaluationView(state, request);
    if (!request.isCurrent() || !isAlertEvaluationViewOwner(state, viewOwner)) return false;
    renderAlertEvaluationPending(viewOwner);
    const result = await fetchJson(
      `/api/alerts/evaluate?symbol=${encodeURIComponent(request.symbol)}`,
      requestOptions(request, { method: "POST" })
    );
    if (!request.isCurrent() || !isAlertEvaluationViewOwner(state, viewOwner)) return false;
    renderAlertEvaluation(result, viewOwner);
    return await finishAlertMutation(state, request, { actionLabel: "检查", locallyReconciled: false });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    if (isAlertEvaluationViewOwner(state, viewOwner)) renderAlertEvaluationFailure(error, viewOwner);
    throw error;
  } finally {
    releaseAlertEvaluationRequest(state, request);
    finishAlertMutationRequest(state, request);
  }
}

export async function removeAlertRule(state, ruleId, options = {}) {
  const request = beginAlertMutation(state, options);
  try {
    await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, requestOptions(request, { method: "DELETE" }));
    if (!request.isCurrent()) return false;
    const locallyReconciled = reconcileAlertRuleRemoval(ruleId);
    return await finishAlertMutation(state, request, { actionLabel: "删除", locallyReconciled });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    finishAlertMutationRequest(state, request);
  }
}

export async function updateAlertRule(state, ruleId, payload, options = {}) {
  const request = beginAlertMutation(state, options);
  try {
    const responseItem = await fetchJson(`/api/alerts/${encodeURIComponent(ruleId)}`, requestOptions(request, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    if (!request.isCurrent()) return false;
    const locallyReconciled = reconcileAlertRuleUpdate(ruleId, responseItem, payload);
    return await finishAlertMutation(state, request, {
      actionLabel: alertUpdateActionLabel(payload),
      locallyReconciled,
    });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    finishAlertMutationRequest(state, request);
  }
}

async function finishAlertMutation(state, request, mutation = null) {
  if (!request.isCurrent()) return false;
  const refresh = beginAlertMutationRefresh(state, request);
  try {
    const outcome = await loadAlertsOutcome(state, {
      symbol: request.symbol,
      signal: refresh.signal,
      isCurrent: refresh.isCurrent,
      preserveOnError: true,
    });
    if (outcome.current && outcome.rulesError && mutation && request.isCurrent()) {
      renderAlertMutationReadbackWarning(mutation.actionLabel, outcome.rulesError, mutation.locallyReconciled);
    }
    return request.isCurrent();
  } finally {
    finishAlertMutationRefresh(state, refresh);
  }
}

async function refreshAlerts(state, request) {
  const [rulesResult, eventsResult] = await Promise.allSettled([
    fetchJson(`/api/alerts?symbol=${encodeURIComponent(request.symbol)}`, requestOptions(request)),
    fetchJson(`/api/alerts/events?symbol=${encodeURIComponent(request.symbol)}&limit=6`, requestOptions(request)),
  ]);
  if (!request.isCurrent() || hasAbortError(rulesResult, eventsResult)) {
    return { current: false, rulesError: null, eventsError: null };
  }
  const rulesError = rulesResult.status === "rejected" ? rulesResult.reason : null;
  const eventsError = eventsResult.status === "rejected"
    ? eventsResult.reason
    : Array.isArray(eventsResult.value)
      ? null
      : new TypeError(ALERT_ACTIVITY_FORMAT_ERROR);
  if (eventsError) {
    const message = eventsError instanceof TypeError && eventsError.message === ALERT_ACTIVITY_FORMAT_ERROR
      ? ALERT_ACTIVITY_FORMAT_ERROR
      : ALERT_ACTIVITY_READ_ERROR;
    syncResearchActivityAlerts(state, request.symbol, [], "unavailable", message);
  } else {
    syncResearchActivityAlerts(state, request.symbol, eventsResult.value, "ready", "");
  }
  if (!rulesError) {
    renderAlerts(rulesResult.value);
  } else if (!request.preserveOnError) {
    $("alertList").innerHTML = `<div class="alert-row"><strong>预警读取失败</strong><span>${escapeHtml(rulesError.message)}</span></div>`;
  }
  if (!eventsError) {
    renderAlertEvents(eventsResult.value);
  } else {
    $("alertEvents").innerHTML = `<div class="alert-event"><strong>事件读取失败</strong><p>${escapeHtml(eventsError.message)}</p></div>`;
  }
  return { current: true, rulesError, eventsError };
}

function reconcileAlertRuleUpdate(ruleId, responseItem, payload) {
  if (!Array.isArray(renderedAlertRules)) return false;
  const targetId = String(ruleId);
  let found = false;
  const responsePatch = objectRecord(responseItem);
  const fallbackPatch = objectRecord(payload);
  const updated = renderedAlertRules.map((item) => {
    if (String(item && item.id) !== targetId) return item;
    found = true;
    const next = { ...item, ...fallbackPatch, ...responsePatch, id: item.id };
    if (!Object.prototype.hasOwnProperty.call(responsePatch, "condition_label") && next.condition_type) {
      next.condition_label = ALERT_CONDITION_LABELS[next.condition_type] || next.condition_label;
    }
    return next;
  });
  if (found) renderAlerts(updated);
  return found;
}

function reconcileAlertRuleRemoval(ruleId) {
  if (!Array.isArray(renderedAlertRules)) return false;
  const targetId = String(ruleId);
  const remaining = renderedAlertRules.filter((item) => String(item && item.id) !== targetId);
  const found = remaining.length !== renderedAlertRules.length;
  if (found) renderAlerts(remaining);
  return found;
}

function objectRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function alertUpdateActionLabel(payload) {
  const updates = objectRecord(payload);
  const fields = Object.keys(updates);
  if (fields.length === 1 && fields[0] === "enabled" && typeof updates.enabled === "boolean") {
    return updates.enabled ? "启用" : "暂停";
  }
  return "更新";
}

function renderAlertMutationReadbackWarning(actionLabel, error, locallyReconciled) {
  const warning = `
    <div class="alert-row alert-readback-warning" role="status">
      <strong>预警已${escapeHtml(actionLabel)}，列表同步降级</strong>
      <span>服务端已接受本次操作；${escapeHtml(error && error.message ? error.message : "规则列表回读失败")}，请稍后刷新确认。</span>
    </div>`;
  const list = $("alertList");
  if (!locallyReconciled) {
    renderedAlertRules = null;
    list.innerHTML = warning;
    return;
  }
  if (typeof list.insertAdjacentHTML === "function") list.insertAdjacentHTML("beforeend", warning);
  else list.innerHTML += warning;
}

function syncResearchActivityAlerts(state, symbol, events, phase, message) {
  state.researchActivityAlerts = [...events];
  state.researchActivityAlertSource = { symbol, phase, message };
}

function beginAlertsReadRequest(state, options, symbol = options.symbol || state.symbol) {
  const requestId = Number(state.alertsReadSeq || 0) + 1;
  const stateSymbol = state.symbol;
  state.alertsReadSeq = requestId;
  const scope = createRequestScope(state.alertsReadRequest, options.signal);
  const request = {
    id: requestId,
    scope,
    signal: scope.signal,
    symbol,
    preserveOnError: Boolean(options.preserveOnError),
    isCurrent: () =>
      state.alertsReadSeq === requestId &&
      state.alertsReadRequest === scope &&
      !scope.signal.aborted &&
      (options.isCurrent ? options.isCurrent() : state.symbol === stateSymbol),
  };
  state.alertsReadRequest = scope;
  if (request.isCurrent()) syncAlertEvaluationSymbol(state, symbol);
  return request;
}

function beginAlertMutation(state, options, symbol = options.symbol || state.symbol) {
  const requestId = Number(state.alertMutationSeq || 0) + 1;
  const stateSymbol = state.symbol;
  // Keep persistence independent from the stock load that owns the UI tail.
  const scope = createRequestScope();
  const requests = mutationRequests(state);
  state.alertMutationSeq = requestId;
  requests.set(requestId, scope);
  return {
    id: requestId,
    scope,
    signal: scope.signal,
    contextSignal: options.signal,
    symbol,
    isCurrent: () =>
      requests.get(requestId) === scope &&
      !scope.signal.aborted &&
      (!options.signal || !options.signal.aborted) &&
      (options.isCurrent ? options.isCurrent() : state.symbol === stateSymbol),
  };
}

function beginAlertMutationRefresh(state, mutation) {
  const requestId = Number(state.alertMutationRefreshSeq || 0) + 1;
  const scope = createRequestScope(state.alertMutationRefreshRequest, mutation.contextSignal);
  state.alertMutationRefreshSeq = requestId;
  state.alertMutationRefreshRequest = scope;
  return {
    scope,
    signal: scope.signal,
    symbol: mutation.symbol,
    isCurrent: () =>
      mutation.isCurrent() &&
      state.alertMutationRefreshSeq === requestId &&
      state.alertMutationRefreshRequest === scope &&
      !scope.signal.aborted,
  };
}

function mutationRequests(state) {
  if (!(state.alertMutationRequests instanceof Map)) state.alertMutationRequests = new Map();
  return state.alertMutationRequests;
}

function requestOptions(request, options = {}) {
  return {
    ...options,
    signal: request.signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  };
}

function hasAbortError(...results) {
  return results.some((result) => result.status === "rejected" && isAbortError(result.reason));
}

function finishAlertsReadRequest(state, request) {
  if (state.alertsReadRequest === request.scope) state.alertsReadRequest = null;
  request.scope.dispose();
}

function finishAlertMutationRequest(state, request) {
  const requests = mutationRequests(state);
  if (requests.get(request.id) === request.scope) requests.delete(request.id);
  request.scope.dispose();
}

function finishAlertMutationRefresh(state, request) {
  if (state.alertMutationRefreshRequest === request.scope) state.alertMutationRefreshRequest = null;
  request.scope.dispose();
}

function claimAlertEvaluationView(state, request) {
  clearAlertEvaluationView(state);
  const round = Number(state.alertEvaluationSeq || 0) + 1;
  const owner = {
    request,
    round,
    symbol: request.symbol,
    contextSignal: request.contextSignal,
    onContextAbort: null,
  };
  state.alertEvaluationSeq = round;
  state.alertEvaluationViewOwner = owner;
  const target = $("alertEvaluation");
  target.dataset.symbol = owner.symbol;
  target.dataset.round = String(owner.round);
  target.hidden = false;
  if (owner.contextSignal) {
    owner.onContextAbort = () => clearAlertEvaluationView(state, owner);
    if (owner.contextSignal.aborted) owner.onContextAbort();
    else owner.contextSignal.addEventListener("abort", owner.onContextAbort, { once: true });
  }
  return owner;
}

function isAlertEvaluationViewOwner(state, owner) {
  if (!owner || state.alertEvaluationViewOwner !== owner) return false;
  const target = $("alertEvaluation");
  return target.dataset.symbol === owner.symbol && target.dataset.round === String(owner.round);
}

function syncAlertEvaluationSymbol(state, symbol) {
  const owner = state.alertEvaluationViewOwner;
  if (owner && owner.symbol !== symbol) clearAlertEvaluationView(state, owner);
}

function clearAlertEvaluationView(state, owner = state.alertEvaluationViewOwner) {
  if (owner && state.alertEvaluationViewOwner !== owner) return false;
  if (owner && owner.contextSignal && owner.onContextAbort) {
    owner.contextSignal.removeEventListener("abort", owner.onContextAbort);
  }
  if (owner) releaseAlertEvaluationRequest(state, owner.request);
  state.alertEvaluationViewOwner = null;
  const target = $("alertEvaluation");
  target.innerHTML = "";
  target.hidden = true;
  target.setAttribute?.("aria-busy", "false");
  delete target.dataset.symbol;
  delete target.dataset.round;
  return true;
}

function releaseAlertEvaluationRequest(state, request) {
  if (!request || state.alertEvaluationOwner !== request) return false;
  state.alertEvaluationOwner = null;
  if ($("evaluateAlerts") === request.button) {
    request.button.disabled = false;
    request.button.textContent = request.previousButtonText || "检查";
  }
  return true;
}

export function renderAlerts(items) {
  renderedAlertRules = items.map((item) => ({ ...item }));
  $("alertList").innerHTML = items.length
    ? items
        .map(
          (item, index) => {
            const editorId = `alert-editor-${escapeHtml(item.id || index)}`;
            return `
          <article class="alert-row ${item.enabled ? "" : "is-muted"}" data-alert-row="${escapeHtml(item.id)}">
            <div class="editable-row-summary">
              <div>
              <strong>${escapeHtml(item.name)}</strong>
              <span>${escapeHtml(item.condition_label)} ${formatNumber(item.threshold)} · ${escapeHtml(item.enabled ? item.last_state || "等待" : "已暂停")}</span>
              ${item.note ? `<small>${escapeHtml(item.note)}</small>` : ""}
              <small>触发 ${escapeHtml(item.trigger_count)} 次 · 冷却 ${escapeHtml(item.cooldown_seconds || 300)} 秒 · ${escapeHtml(item.last_checked_at || "尚未检查")}</small>
              </div>
              <div class="row-actions">
                <button type="button" class="mini-button" aria-label="编辑预警 ${escapeHtml(item.name)}" aria-expanded="false" aria-controls="${editorId}" data-alert-edit="${escapeHtml(item.id)}">编辑</button>
                <button type="button" class="mini-button" data-alert-toggle="${escapeHtml(item.id)}" data-alert-enabled="${item.enabled ? "false" : "true"}">${item.enabled ? "暂停" : "启用"}</button>
                <button type="button" class="icon-button" title="删除预警" aria-label="删除预警" data-alert-remove="${escapeHtml(item.id)}">×</button>
              </div>
            </div>
            <p class="row-action-feedback" role="alert" hidden></p>
            ${renderAlertEditor(item, editorId)}
          </article>`;
          }
        )
        .join("")
    : `<div class="alert-row"><strong>暂无预警</strong><span>添加价格、涨跌幅或趋势评分提醒。</span></div>`;
}

function renderAlertEditor(item, editorId) {
  return `
    <form class="inline-edit-form alert-edit-form" id="${editorId}" data-alert-edit-form data-alert-id="${escapeHtml(item.id)}" hidden>
      <div class="inline-edit-grid">
        <label><span>规则名称</span><input name="name" value="${escapeHtml(item.name)}" maxlength="40" autocomplete="off" required /></label>
        <label><span>预警类型</span><select name="condition_type">${alertConditionOptions(item.condition_type)}</select></label>
        <label><span>阈值</span><input name="threshold" type="number" step="0.01" value="${escapeHtml(item.threshold)}" required /></label>
        <label><span>冷却秒数</span><input name="cooldown_seconds" type="number" min="30" max="86400" step="1" value="${escapeHtml(item.cooldown_seconds || 300)}" required /></label>
        <label class="inline-edit-wide"><span>规则备注</span><input name="note" value="${escapeHtml(item.note || "")}" maxlength="160" autocomplete="off" placeholder="可留空" /></label>
      </div>
      <div class="inline-edit-actions">
        <p class="inline-edit-feedback" role="alert" hidden></p>
        <span>
          <button type="button" class="mini-button" data-alert-cancel="${escapeHtml(item.id)}">取消</button>
          <button type="submit" class="mini-button primary">保存</button>
        </span>
      </div>
    </form>`;
}

function alertConditionOptions(selected) {
  return Object.entries(ALERT_CONDITION_LABELS)
    .map(([value, label]) => `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`)
    .join("");
}

export function alertRuleUpdatesFromForm(form) {
  const name = formValue(form, "name");
  const conditionType = formValue(form, "condition_type");
  const rawThreshold = formValue(form, "threshold");
  const rawCooldown = formValue(form, "cooldown_seconds");
  if (!name) throw new Error("请输入规则名称");
  if (!Object.prototype.hasOwnProperty.call(ALERT_CONDITION_LABELS, conditionType)) throw new Error("请选择有效预警类型");
  const threshold = Number(rawThreshold);
  if (!rawThreshold || !Number.isFinite(threshold)) throw new Error("请输入有效阈值");
  const cooldownSeconds = Number(rawCooldown);
  if (!Number.isInteger(cooldownSeconds) || cooldownSeconds < 30 || cooldownSeconds > 86400) {
    throw new Error("冷却时间应为30到86400秒");
  }
  return {
    name,
    condition_type: conditionType,
    threshold,
    note: formValue(form, "note") || null,
    cooldown_seconds: cooldownSeconds,
  };
}

export function toggleAlertRuleEditor(button, forceOpen) {
  return toggleInlineEditor(
    button,
    { row: ".alert-row", form: ".alert-edit-form", button: "[data-alert-edit]" },
    forceOpen
  );
}

function formValue(form, name) {
  const control = form?.elements?.namedItem?.(name) || form?.querySelector?.(`[name="${name}"]`);
  return String(control?.value || "").trim();
}

export function renderAlertEvents(items) {
  $("alertEvents").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="alert-event ${item.event_type === "恢复" ? "recover" : ""}">
            <strong>${escapeHtml(item.name)} · ${escapeHtml(item.event_type || "触发")}</strong>
            <span>${escapeHtml(item.created_at)} · ${formatNumber(item.price)} / ${formatNumber(item.change_pct)}%</span>
            <p>${escapeHtml(item.message)}</p>
          </div>`
        )
        .join("")
    : `<div class="alert-event"><strong>暂无触发记录</strong><p>手动检查或后续调度触发后会记录在这里。</p></div>`;
}

function renderAlertEvaluationPending(owner) {
  const target = $("alertEvaluation");
  if (!matchesAlertEvaluationDomOwner(target, owner)) return false;
  target.hidden = false;
  target.setAttribute?.("aria-busy", "true");
  target.innerHTML = `
    <div class="alert-event">
      <strong>正在检查</strong>
      <p>正在评估当前股票的预警规则。</p>
    </div>
  `;
  return true;
}

function renderAlertEvaluationFailure(error, owner) {
  const target = $("alertEvaluation");
  if (!matchesAlertEvaluationDomOwner(target, owner)) return false;
  target.hidden = false;
  target.setAttribute?.("aria-busy", "false");
  target.innerHTML = `
    <div class="alert-event is-warning">
      <strong>检查失败</strong>
      <p>${escapeHtml(error && error.message ? error.message : "请稍后重试")}</p>
    </div>
  `;
  return true;
}

function matchesAlertEvaluationDomOwner(target, owner) {
  return !owner || (target.dataset.symbol === owner.symbol && target.dataset.round === String(owner.round));
}

export function renderAlertEvaluation(result, owner = null) {
  const target = $("alertEvaluation");
  if (!matchesAlertEvaluationDomOwner(target, owner)) return false;
  const failedCount = Number(result.failed_count || 0);
  const completedCount = Math.max(0, Number(result.checked_count || 0) - failedCount);
  target.hidden = false;
  target.setAttribute?.("aria-busy", "false");
  target.innerHTML = `
    <div class="alert-event ${failedCount ? "is-warning" : ""}">
      <strong>${failedCount ? "检查部分完成" : "检查完成"}</strong>
      <span>${escapeHtml(result.checked_at)} · 成功 ${escapeHtml(completedCount)} / ${escapeHtml(result.checked_count)} · 触发 ${escapeHtml(result.triggered_count)}</span>
      <p>新增触发记录 ${escapeHtml(result.new_event_count)} 条${failedCount ? `，失败 ${escapeHtml(failedCount)} 条，请稍后重试。` : "。"}</p>
    </div>
  `;
  return true;
}
