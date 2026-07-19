import { createRequestScope } from "./api.js";
import { isActiveMarketScanRun, isMarketScanNotFoundError } from "./market-scan-contracts.js";

const DEFAULT_POLL_INTERVAL_MS = 2000;
const DEFAULT_IDLE_POLL_INTERVAL_MS = 30000;
const DEFAULT_RESULT_RETRY_INTERVAL_MS = 5000;
const DEFAULT_MAX_POLL_INTERVAL_MS = 30000;
const DEFAULT_FAILURE_FALLBACK_THRESHOLD = 3;

export function createMarketScanPolling(options) {
  const context = pollingContext(options);
  return {
    abortRequest: (scope, sequence) => abortRequest(context.state, scope, sequence),
    beginRequest: (scope, sequence) => beginRequest(context.state, scope, sequence),
    clear: () => clear(context),
    finishRequest: (scope, sequence, value) => finishRequest(context.state, scope, sequence, value),
    handleResultFailureAfterLatest: (error) => handleResultFailureAfterLatest(context, error),
    handleScopedFailure: (error, target) => handleScopedFailure(context, error, target),
    isCurrentRequest: (sequence, value) => isCurrentRequest(context.state, sequence, value),
    recoveryMessage,
    resetFailures: () => resetFailures(context.state),
    retryLatest: () => retryLatest(context),
    scheduleDefault: (run) => scheduleDefault(context, run),
  };
}

function pollingContext(options) {
  const poll = positiveInteger(options.pollIntervalMs, DEFAULT_POLL_INTERVAL_MS);
  return {
    callbacks: options.callbacks,
    failureFallbackThreshold: positiveInteger(
      options.failureFallbackThreshold,
      DEFAULT_FAILURE_FALLBACK_THRESHOLD
    ),
    intervals: {
      poll,
      idle: positiveInteger(options.idlePollIntervalMs, DEFAULT_IDLE_POLL_INTERVAL_MS),
      result: positiveInteger(options.resultRetryIntervalMs, DEFAULT_RESULT_RETRY_INTERVAL_MS),
      max: Math.max(poll, positiveInteger(options.maxPollIntervalMs, DEFAULT_MAX_POLL_INTERVAL_MS)),
    },
    isEnabled: typeof options.isEnabled === "function"
      ? options.isEnabled
      : () => Boolean(options.state.activated && options.state.visible),
    state: options.state,
  };
}

function scheduleDefault(context, run) {
  const active = isActiveMarketScanRun(run);
  schedule(context, active ? "run" : "latest", active ? context.intervals.poll : context.intervals.idle);
}

function schedule(context, target, delay) {
  clear(context);
  if (!context.isEnabled()) return;
  const callback = context.callbacks[target];
  if (typeof callback !== "function") throw new Error(`未知的扫描刷新目标：${target}`);
  context.state.pollTimer = setTimeout(() => {
    context.state.pollTimer = null;
    void callback();
  }, Math.max(0, Number(delay) || 0));
}

function clear({ state }) {
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function resetFailures(state) {
  state.consecutiveFailures = 0;
}

function retryLatest(context) {
  recordFailure(context.state);
  schedule(context, "latest", failureDelay(context, context.intervals.poll));
}

function handleScopedFailure(context, error, retryTarget) {
  recordFailure(context.state);
  if (shouldRecoverLatest(context, error)) return true;
  schedule(context, retryTarget, failureDelay(context, baseDelay(context, retryTarget)));
  return false;
}

function handleResultFailureAfterLatest(context, error) {
  recordFailure(context.state);
  const target = shouldRecoverLatest(context, error) ? "latest" : "results";
  schedule(context, target, failureDelay(context, context.intervals.result));
}

function recoveryMessage(error) {
  return isMarketScanNotFoundError(error)
    ? "原扫描记录已失效，正在同步最近扫描。"
    : "连续刷新失败，正在重新同步最近扫描。";
}

function recordFailure(state) {
  state.consecutiveFailures += 1;
}

function shouldRecoverLatest(context, error) {
  return isMarketScanNotFoundError(error)
    || context.state.consecutiveFailures >= context.failureFallbackThreshold;
}

function failureDelay(context, base) {
  const exponent = Math.max(0, context.state.consecutiveFailures - 1);
  return Math.min(context.intervals.max, base * (2 ** exponent));
}

function baseDelay(context, target) {
  return target === "results" ? context.intervals.result : context.intervals.poll;
}

function beginRequest(state, scopeField, sequenceField) {
  state[scopeField] = createRequestScope(state[scopeField]);
  state[sequenceField] += 1;
  return state[sequenceField];
}

function finishRequest(state, scopeField, sequenceField, sequence) {
  if (!isCurrentRequest(state, sequenceField, sequence)) return;
  state[scopeField]?.dispose();
  state[scopeField] = null;
}

function isCurrentRequest(state, sequenceField, sequence) {
  return state[sequenceField] === sequence;
}

function abortRequest(state, scopeField, sequenceField) {
  state[scopeField]?.abort();
  state[scopeField] = null;
  state[sequenceField] += 1;
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}
