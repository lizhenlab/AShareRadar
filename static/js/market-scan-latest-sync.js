import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import {
  marketScanPollingIdentityChanged,
  validateMarketScanPollingIdentity,
} from "./market-scan-polling-identity.js";
import { isMarketScanReadBusy } from "./market-scan-polling.js";

const MAX_IDENTITY_SYNC_ATTEMPTS = 2;

export function createMarketScanLatestSync(options) {
  const context = {
    ...options,
    attemptFingerprint: null,
    failedFingerprint: null,
    inFlight: null,
    inFlightKey: null,
  };
  return {
    abort: () => abortLatestSync(context),
    supersede: () => supersedeLatestSync(context),
    sync: (syncOptions) => syncLatest(context, syncOptions),
  };
}

function syncLatest(context, syncOptions = {}) {
  const key = `${context.state.browseMode}:${syncOptions.forceTrusted ? "force" : "poll"}`;
  if (context.inFlight && context.inFlightKey === key) return context.inFlight;
  if (context.inFlight) return context.inFlight;
  const operation = runLatestSync(context, syncOptions).finally(() => finishOperation(context, operation));
  context.inFlight = operation;
  context.inFlightKey = key;
  return operation;
}

async function runLatestSync(context, syncOptions) {
  const { polling, state } = context;
  if (state.actionBusy) return null;
  polling.clear();
  const sequence = context.beginRequest("runRequest", "runRequestSeq");
  if (syncOptions.renderLoading && !state.run) context.renderLoading();
  try {
    return await synchronizeIdentity(context, sequence, syncOptions);
  } catch (error) {
    if (!isAbortError(error) && context.isCurrentRequest("runRequestSeq", sequence)) {
      const deterministicFailure = rememberDeterministicFailure(context, error);
      context.handleError(error, { ...syncOptions, deterministicFailure });
    }
    else if (!isAbortError(error)) context.handleStaleError?.(error);
    return null;
  } finally {
    context.attemptFingerprint = null;
    context.finishRequest("runRequest", "runRequestSeq", sequence);
  }
}

async function synchronizeIdentity(context, sequence, syncOptions) {
  const { polling, state } = context;
  let before = await requestIdentity(context, sequence);
  context.attemptFingerprint = before.fingerprint;
  const explicitForce = Boolean(syncOptions.forceTrusted);
  if (!explicitForce && context.failedFingerprint === before.fingerprint) {
    polling.resetFailures();
    polling.scheduleDefault(state.run);
    return state.run;
  }
  if (context.failedFingerprint !== before.fingerprint) context.failedFingerprint = null;
  const forceTrusted = Boolean(explicitForce || state.pollingIdentity === null);
  if (!forceTrusted && !marketScanPollingIdentityChanged(state.pollingIdentity, before)) {
    polling.resetFailures();
    polling.scheduleDefault(state.run);
    return state.run;
  }
  for (let attempt = 0; attempt < MAX_IDENTITY_SYNC_ATTEMPTS; attempt += 1) {
    const staged = await context.stage(before, state.pollingIdentity, sequence, { forceTrusted });
    const after = await requestIdentity(context, sequence);
    if (!marketScanPollingIdentityChanged(before, after)) {
      context.commit(staged, after, syncOptions);
      context.failedFingerprint = null;
      state.pollingIdentity = after;
      polling.resetFailures();
      return staged.run;
    }
    before = after;
    context.attemptFingerprint = before.fingerprint;
  }
  throw new Error("扫描轮询身份持续变化，等待下一次同步");
}

async function requestIdentity(context, sequence) {
  const { state } = context;
  const payload = await context.request(
    `/api/market-scans/polling-identity?mode=${encodeURIComponent(state.browseMode)}`,
    { signal: state.runRequest.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS },
  );
  if (!context.isCurrentRequest("runRequestSeq", sequence)) throw abortedIdentityRequest();
  return validateMarketScanPollingIdentity(payload, state.browseMode);
}

function abortLatestSync(context) {
  if (context.inFlight) context.abortRequest("runRequest", "runRequestSeq");
  context.inFlight = null;
  context.inFlightKey = null;
}

function supersedeLatestSync(context) {
  const operation = context.inFlight;
  if (!operation) return Promise.resolve();
  const scope = context.state.runRequest;
  context.state.runRequestSeq += 1;
  return operation.finally(() => {
    if (context.state.runRequest !== scope) return;
    scope?.dispose?.();
    context.state.runRequest = null;
  });
}

function finishOperation(context, operation) {
  if (context.inFlight !== operation) return;
  context.inFlight = null;
  context.inFlightKey = null;
}

function abortedIdentityRequest() {
  return new DOMException("扫描轮询身份请求已失效", "AbortError");
}

function rememberDeterministicFailure(context, error) {
  if (!context.attemptFingerprint || !isDeterministicContractError(error)) return false;
  context.failedFingerprint = context.attemptFingerprint;
  return true;
}

function isDeterministicContractError(error) {
  if (isMarketScanReadBusy(error)) return false;
  const status = Number(error?.status);
  return error?.name === "MarketScanContractError"
    || error?.code === "market_scan_polling_identity_contract_error"
    || error?.message === "请求超时，请稍后重试"
    || (Number.isInteger(status) && status >= 400);
}
