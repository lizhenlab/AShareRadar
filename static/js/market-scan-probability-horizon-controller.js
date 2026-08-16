import { DEFAULT_REQUEST_TIMEOUT_MS, isAbortError } from "./api.js";
import { validateResultPage } from "./market-scan-contracts.js";
import { samePublishedMarketScanRun } from "./market-scan-latest-loader.js";
import { isMarketScanReadBusy, marketScanReadBusyMessage } from "./market-scan-polling.js";

export function createMarketScanProbabilityHorizonController(options) {
  const context = {
    options,
    cache: null,
    lastGoodCache: null,
    lastValidatedQuery: null,
    needsTrustedRefresh: false,
    intentGeneration: 0,
    directInFlight: null,
    idleWaiters: [],
    trustedInFlight: null,
    refreshQueued: false,
    refreshBaseQuery: null,
    refreshPromise: null,
    queuedLoad: null,
  };
  return {
    acceptTrusted: (query) => acceptTrusted(context, query),
    change: () => changeHorizon(context),
    drain: () => drain(context),
    invalidate: (invalidateOptions) => invalidate(context, invalidateOptions),
    load: (loadOptions) => load(context, loadOptions),
    loadOnce: (loadOptions) => loadOnce(context, loadOptions),
    needsTrustedRefresh: () => context.needsTrustedRefresh,
    presentBusy: (error) => presentBusy(context, error),
    publicationChanged: (run) => publicationChanged(context, run),
    remember: (payload, query, identity) => remember(context, payload, query, identity),
    requestFinished: () => requestFinished(context),
    supersede: (supersedeOptions) => supersede(context, supersedeOptions),
    staleTrustedFailure: (error) => staleTrustedFailure(context, error),
    trustedChainStarted: () => trustedChainStarted(context),
    trustedReadFinished: () => trustedReadFinished(context),
    trustedReadStarted: (query, run) => trustedReadStarted(context, query, run),
    loadWithinGate: (loadOptions) => performLoad(context, { ...loadOptions, withinHeavyRead: true }),
    whenIdle: () => whenIdle(context),
  };
}

function invalidate(context, options = {}) {
  context.cache = null;
  if (options.clearLastGood) context.lastGoodCache = null;
}

function supersede(context, options = {}) {
  if (options.preserveCache) restoreLastGoodCache(context);
  else {
    invalidate(context, { clearLastGood: true });
    context.lastValidatedQuery = null;
  }
  context.refreshQueued = false;
  context.refreshBaseQuery = null;
  context.intentGeneration += 1;
  settleQueuedLoad(context, null);
  notifyIdle(context);
}

function publicationChanged(context, run) {
  const trustedPublication = samePublishedMarketScanRun(context.trustedInFlight?.selectedRun, run);
  const preserveOwnedWork = trustedPublication && (context.queuedLoad || context.refreshQueued);
  if (!preserveOwnedWork) return supersede(context);
  invalidate(context);
  context.lastValidatedQuery = null;
}

function remember(context, payload, query, identity) {
  const { options } = context;
  if (!validResultsQuery(query, payload?.run?.id)) return invalidate(context, { clearLastGood: true });
  try {
    const page = validateResultPage(structuredClone(payload), payload.run.id);
    if (!samePublishedMarketScanRun(page.run, options.resultRun())) {
      return invalidate(context, { clearLastGood: true });
    }
    const owner = resultContext(options, page.run);
    context.lastValidatedQuery = { ...owner, query };
    const cache = {
      ...owner,
      identityBinding: identityBinding(identity, owner.historyRunId, page.run.id),
      page,
      query,
    };
    context.cache = cache;
    context.lastGoodCache = cache;
    context.needsTrustedRefresh = false;
  } catch (_error) {
    invalidate(context, { clearLastGood: true });
  }
}

function restoreLastGoodCache(context) {
  const { options } = context;
  const candidate = context.cache || context.lastGoodCache;
  const run = options.resultRun();
  context.cache = candidate
    && contextMatches(candidate, options, run)
    && identityMatches(candidate.identityBinding, options.state.pollingIdentity)
    ? candidate
    : null;
}

function currentCache(context) {
  const { options } = context;
  const run = options.resultRun();
  if (
    !context.cache
    || !contextMatches(context.cache, options, run)
    || !identityMatches(context.cache.identityBinding, options.state.pollingIdentity)
  ) {
    invalidate(context);
    return null;
  }
  return context.cache;
}

function currentBaseline(context) {
  const { options } = context;
  const run = options.resultRun();
  const baseline = context.lastValidatedQuery;
  return baseline && contextMatches(baseline, options, run)
    && validResultsQuery(baseline.query, run?.id)
    ? baseline
    : null;
}

function changeHorizon(context) {
  const { options } = context;
  options.clearResetTimer();
  const retained = currentCache(context);
  const { activeQuery, baselineQuery, queuedQuery } = horizonChangeQueries(context, retained);
  options.elements.probabilityMin.value = "";
  if (queuedQuery && !queryHasProbabilityMinimum(queuedQuery)) return;
  if (!queuedQuery && retained && !queryHasProbabilityMinimum(retained.query)) {
    options.view.renderProbabilityHorizon(retained.page);
    return;
  }
  if (!queuedQuery && activeQuery && !queryHasProbabilityMinimum(activeQuery)) return;
  if (!horizonRefreshHasOwner(context, retained, activeQuery, baselineQuery)) return;
  queueHorizonRefresh(context, retained, queuedQuery, baselineQuery);
}

function horizonChangeQueries(context, retained) {
  const queuedQuery = context.queuedLoad?.spec.query || null;
  const activeQuery = context.directInFlight?.query || context.trustedInFlight?.query || null;
  const baselineQuery = queuedQuery || retained?.query || activeQuery || currentBaseline(context)?.query || null;
  return { activeQuery, baselineQuery, queuedQuery };
}

function horizonRefreshHasOwner(context, retained, activeQuery, baselineQuery) {
  const { options } = context;
  const run = options.resultRun();
  const hasRenderedPage = Boolean(run && options.state.renderedResultRunId === run.id);
  return Boolean(baselineQuery && (retained || activeQuery || hasRenderedPage));
}

function queueHorizonRefresh(context, retained, queuedQuery, baselineQuery) {
  const { options } = context;
  const resetPage = queryHasProbabilityMinimum(baselineQuery);
  const unfiltered = unfilteredResultsQuery(baselineQuery, queryRunId(baselineQuery), { resetPage });
  if (!unfiltered) return renderUnsafeRefresh(context, options.resultRun()?.id ?? null);
  if (queuedQuery) settleQueuedLoad(context, null);
  context.intentGeneration += 1;
  context.refreshBaseQuery = unfiltered;
  context.refreshQueued = true;
  options.detachOwnedRead?.({ allowTrustedSelection: Boolean(context.trustedInFlight?.query) });
  if (resetPage) options.state.page = 1;
  if (retained) {
    options.view.renderProbabilityResearch(retained.page);
    options.view.renderResultsLoading();
  }
  void drain(context);
}

function load(context, loadOptions = {}) {
  const { options } = context;
  if (!options.state.surfaceActive) return Promise.resolve(null);
  if (options.state.actionBusy && !loadOptions.allowDuringAction) return Promise.resolve(null);
  if (loadOptions.horizonRefresh === true) return performLoad(context, loadOptions);
  const run = options.resultRun();
  context.intentGeneration += 1;
  context.refreshQueued = false;
  context.refreshBaseQuery = null;
  const spec = {
    ...loadOptions,
    context: resultContext(options, run),
    intentGeneration: context.intentGeneration,
    query: loadOptions.query || (run ? options.resultsUrl(run.id, options.state.page) : null),
  };
  options.detachOwnedRead?.({ allowTrustedSelection: Boolean(context.trustedInFlight?.query) });
  if (
    context.directInFlight
    || context.trustedInFlight
    || context.refreshPromise
    || options.state.runRequest
    || options.state.resultRequest
  ) {
    return queueLoad(context, spec);
  }
  return performLoad(context, spec);
}

async function performLoad(context, loadOptions) {
  const { options } = context;
  options.polling.clear();
  const runId = options.resultRun()?.id ?? null;
  const read = () => loadOnce(context, loadOptions);
  const outcome = loadOptions.withinHeavyRead || !options.withHeavyRead
    ? await read()
    : await options.withHeavyRead(read);
  if (!outcome) return null;
  if (outcome.ok) {
    options.polling.resetFailures();
    if (outcome.committed !== false) options.probabilityPolling.schedule(outcome.payload);
    return outcome.committed === false ? null : outcome.payload;
  }
  if (!outcome.aborted && runId !== null) {
    const recovery = handleLoadFailure(options, outcome.error, runId);
    if (loadOptions.withinHeavyRead) void recovery;
    else await recovery;
  }
  return null;
}

async function handleLoadFailure(options, error, runId) {
  const retryTarget = options.probabilityPolling.retryTarget(runId);
  if (isMarketScanReadBusy(error)) {
    options.polling.retryBusy(error, retryTarget || "results");
    return;
  }
  if (retryTarget !== null && options.polling.handleScopedFailure(error, retryTarget)) {
    await options.recoverLatest(error);
  }
}

async function loadOnce(context, loadOptions = {}) {
  const { options } = context;
  if (!options.state.surfaceActive) return { ok: true, payload: null, skipped: true };
  const publishedRun = options.resultRun();
  if (!publishedRun) return clearMissingResultRun(context);
  const runId = publishedRun.id;
  const query = loadOptions.query || options.resultsUrl(runId, options.state.page);
  if (!validResultsQuery(query, runId)) return staleOutcome();
  invalidate(context);
  const sequence = options.beginRequest("resultRequest", "resultRequestSeq");
  const owned = {
    context: loadOptions.context || resultContext(options, publishedRun),
    generation: loadOptions.intentGeneration ?? context.intentGeneration,
    query,
    sequence,
  };
  context.directInFlight = owned;
  options.view.renderResultsLoading();
  return requestOwnedResult(context, owned, publishedRun, loadOptions);
}

function clearMissingResultRun(context) {
  const { options } = context;
  invalidate(context, { clearLastGood: true });
  options.state.renderedResultRunId = null;
  options.view.resetResultPresentation(options.state.run);
  return { ok: true, payload: null };
}

async function requestOwnedResult(context, owned, publishedRun, loadOptions) {
  const { options } = context;
  try {
    const response = await options.request(owned.query, {
      signal: options.state.resultRequest.signal,
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    if (!options.isCurrentRequest("resultRequestSeq", owned.sequence)) return staleOutcome();
    const payload = validateResultPage(response, publishedRun.id);
    return commitOwnedResult(context, owned, payload);
  } catch (error) {
    return handleOwnedResultError(context, owned, publishedRun, loadOptions, error);
  } finally {
    finishOwnedResult(context, owned);
  }
}

function commitOwnedResult(context, owned, payload) {
  const { options } = context;
  if (!ownedRequestIsCurrent(context, owned, payload.run)) {
    return { ok: true, committed: false, payload };
  }
  options.state.page = payload.page;
  options.state.pageCount = payload.page_count;
  options.view.renderResults(payload);
  options.state.renderedResultRunId = payload.run.id;
  remember(context, payload, owned.query, options.state.pollingIdentity);
  return { ok: true, payload };
}

function handleOwnedResultError(context, owned, publishedRun, loadOptions, error) {
  const { options } = context;
  if (
    isAbortError(error)
    || !options.isCurrentRequest("resultRequestSeq", owned.sequence)
    || !ownedRequestIsCurrent(context, owned, publishedRun)
  ) {
    recordStaleTrustFailure(context, owned, error);
    return staleOutcome(error);
  }
  if (isMarketScanReadBusy(error)) {
    if (loadOptions.horizonRefresh === true) cancelRefresh(context);
    presentBusy(context, error);
    return { ok: false, aborted: false, busy: true, error };
  }
  invalidate(context, { clearLastGood: true });
  if (loadOptions.horizonRefresh === true) cancelRefresh(context);
  const message = options.resultErrorMessage(error);
  options.view.resetProbabilityResearch(publishedRun.id, { readError: true });
  options.view.renderResultState(message, "error");
  options.view.announce(message, `results-error:${publishedRun.id}:${String(error?.message || "")}`);
  return { ok: false, aborted: false, error };
}

function presentBusy(context, error) {
  const { options } = context;
  restoreLastGoodCache(context);
  const retained = currentCache(context);
  if (retained) {
    options.state.page = retained.page.page;
    options.state.pageCount = retained.page.page_count;
    options.state.renderedResultRunId = retained.page.run.id;
    options.view.renderProbabilityHorizon(retained.page);
  } else {
    options.view.resetProbabilityResearch(options.resultRun()?.id ?? null, { busyWait: true });
  }
  const message = marketScanReadBusyMessage(error, { retained: Boolean(retained) });
  options.view.renderResultsWaiting(message);
  if (!options.resultRun()) options.view.renderHeadline(message, "loading");
  options.view.announce(
    message,
    `results-busy:${options.resultRun()?.id ?? "none"}:${Math.ceil(Number(error?.retryAfterMs) || 0)}`,
  );
  return Boolean(retained);
}

function staleTrustedFailure(context, error) {
  return recordStaleTrustFailure(context, context.trustedInFlight, error);
}

function recordStaleTrustFailure(context, owned, error) {
  if (
    isAbortError(error)
    || !isDeterministicResultTrustFailure(error)
    || !contextMatches(owned?.context, context.options, context.options.resultRun())
  ) return false;
  invalidate(context, { clearLastGood: true });
  context.lastValidatedQuery = null;
  context.needsTrustedRefresh = true;
  return true;
}

function isDeterministicResultTrustFailure(error) {
  return Number(error?.status) === 409
    || error?.name === "MarketScanContractError"
    || error?.code === "market_scan_polling_identity_contract_error";
}

function finishOwnedResult(context, owned) {
  const { options } = context;
  if (context.directInFlight === owned) context.directInFlight = null;
  options.finishRequest("resultRequest", "resultRequestSeq", owned.sequence);
  void drain(context);
  notifyIdle(context);
}

function trustedChainStarted(context) {
  context.trustedInFlight = {
    context: resultContext(context.options, context.options.resultRun()),
    generation: context.intentGeneration,
    query: null,
    selectedRun: null,
  };
}

function trustedReadStarted(context, query, run) {
  invalidate(context);
  const owner = context.trustedInFlight || {};
  context.trustedInFlight = {
    context: owner.context || resultContext(context.options, context.options.resultRun()),
    generation: owner.generation ?? context.intentGeneration,
    query,
    selectedRun: run ? structuredClone(run) : null,
  };
  context.options.view.renderResultsLoading();
}

function acceptTrusted(context, query) {
  const trusted = context.trustedInFlight;
  return Boolean(
    trusted
    && trusted.query === query
    && trusted.generation === context.intentGeneration
    && contextMatches(trusted.context, context.options, context.options.resultRun())
    && trusted.selectedRun?.id === queryRunId(query)
    && validResultsQuery(query, queryRunId(query))
  );
}

function trustedReadFinished(context) {
  context.trustedInFlight = null;
  void drain(context);
  notifyIdle(context);
}

function queueLoad(context, spec) {
  settleQueuedLoad(context, null);
  return new Promise((resolve) => {
    context.queuedLoad = { resolve, spec };
    void drain(context);
  });
}

function settleQueuedLoad(context, value) {
  if (!context.queuedLoad) return;
  const pending = context.queuedLoad;
  context.queuedLoad = null;
  pending.resolve(value);
}

function drain(context) {
  const { options } = context;
  if (context.refreshPromise) return context.refreshPromise;
  if (options.state.runRequest || options.state.resultRequest) return null;
  if (context.directInFlight || context.trustedInFlight) return null;
  if (!context.queuedLoad && !context.refreshQueued) return null;
  const operation = runQueuedWork(context).finally(() => finishDrain(context, operation));
  context.refreshPromise = operation;
  return operation;
}

function finishDrain(context, operation) {
  if (context.refreshPromise === operation) context.refreshPromise = null;
  if (context.queuedLoad || context.refreshQueued) void drain(context);
  notifyIdle(context);
}

function requestFinished(context) {
  void drain(context);
  notifyIdle(context);
}

function whenIdle(context) {
  if (!ownedReadsBusy(context)) return Promise.resolve();
  return new Promise((resolve) => context.idleWaiters.push(resolve));
}

function notifyIdle(context) {
  if (ownedReadsBusy(context) || !context.idleWaiters.length) return;
  const waiters = context.idleWaiters.splice(0);
  waiters.forEach((resolve) => resolve());
}

function ownedReadsBusy(context) {
  const { state } = context.options;
  return Boolean(
    state.runRequest || state.resultRequest || context.directInFlight || context.trustedInFlight
    || context.refreshPromise || context.queuedLoad || context.refreshQueued
  );
}

async function runQueuedWork(context) {
  if (context.queuedLoad) return runQueuedLoad(context);
  context.refreshQueued = false;
  const run = context.options.resultRun();
  const query = unfilteredResultsQuery(context.refreshBaseQuery, run?.id, { resetPage: false });
  context.refreshBaseQuery = null;
  if (!run || !query) return renderUnsafeRefresh(context, run?.id ?? null);
  await performLoad(context, {
    context: resultContext(context.options, run),
    horizonRefresh: true,
    intentGeneration: context.intentGeneration,
    query,
  });
}

async function runQueuedLoad(context) {
  const pending = context.queuedLoad;
  context.queuedLoad = null;
  const spec = queuedSpecForCurrentRun(context, pending.spec);
  const value = spec ? await performLoad(context, spec) : null;
  pending.resolve(value);
}

function queuedSpecForCurrentRun(context, spec) {
  const { options } = context;
  const run = options.resultRun();
  if (contextMatches(spec.context, options, run)) return spec;
  if (
    !run
    || spec.context?.browseMode !== options.state.browseMode
    || spec.context?.historyRunId !== options.state.selectedHistoryRunId
  ) return null;
  const rebased = rebaseResultsQuery(spec.query, run.id);
  const query = unfilteredResultsQuery(rebased, run.id);
  if (!query) return null;
  options.elements.probabilityMin.value = "";
  options.state.page = 1;
  return { ...spec, context: resultContext(options, run), query };
}

function ownedRequestIsCurrent(context, owned, responseRun) {
  const run = context.options.resultRun();
  return owned.generation === context.intentGeneration
    && contextMatches(owned.context, context.options, run)
    && samePublishedMarketScanRun(responseRun, run);
}

function resultContext(options, run) {
  return {
    browseMode: options.state.browseMode,
    historyRunId: options.state.selectedHistoryRunId,
    run: run ? structuredClone(run) : null,
  };
}

function contextMatches(context, options, run) {
  return Boolean(
    context
    && context.browseMode === options.state.browseMode
    && context.historyRunId === options.state.selectedHistoryRunId
    && samePublishedMarketScanRun(context.run, run)
  );
}

function identityMatches(binding, identity) {
  if (binding.kind === "none") return identity === null;
  if (binding.kind === "history") return binding.fingerprint === identity?.fingerprint;
  return binding.runId === identity?.latest_published?.run_id
    && binding.token === identity?.latest_published?.token;
}

function cancelRefresh(context) {
  context.refreshQueued = false;
  context.refreshBaseQuery = null;
}

function renderUnsafeRefresh(context, runId) {
  cancelRefresh(context);
  invalidate(context, { clearLastGood: true });
  const message = "榜单读取失败：无法从已验证查询恢复";
  context.options.view.resetProbabilityResearch(runId, { readError: true });
  context.options.view.renderResultState(message, "error");
  context.options.view.announce(message, `results-error:${runId ?? "none"}:unsafe-query`);
}

export function unfilteredResultsQuery(query, runId, options = {}) {
  if (!validResultsQuery(query, runId)) return null;
  const parsed = parseResultsQuery(query);
  parsed.params.delete("probability_horizon");
  parsed.params.delete("min_upside_probability");
  if (options.resetPage !== false) parsed.params.set("page", "1");
  const search = parsed.params.toString();
  return `${parsed.path}${search ? `?${search}` : ""}`;
}

function validResultsQuery(query, runId) {
  if (typeof query !== "string" || !query || !Number.isInteger(Number(runId))) return false;
  const parsed = parseResultsQuery(query);
  return parsed !== null && parsed.runId === Number(runId);
}

function queryRunId(query) { return parseResultsQuery(query)?.runId ?? null; }

function queryHasProbabilityMinimum(query) {
  return parseResultsQuery(query)?.params.has("min_upside_probability") ?? false;
}

function rebaseResultsQuery(query, runId) {
  const parsed = parseResultsQuery(query);
  if (!parsed || !Number.isInteger(Number(runId))) return null;
  const search = parsed.params.toString();
  return `/api/market-scans/${encodeURIComponent(runId)}/results${search ? `?${search}` : ""}`;
}

function parseResultsQuery(query) {
  if (typeof query !== "string" || query.includes("#")) return null;
  const separator = query.indexOf("?");
  const path = separator === -1 ? query : query.slice(0, separator);
  const search = separator === -1 ? "" : query.slice(separator + 1);
  const match = path.match(/^\/api\/market-scans\/(\d+)\/results$/);
  if (!match) return null;
  return { path, params: new URLSearchParams(search), runId: Number(match[1]) };
}

function identityBinding(identity, historyRunId, runId) {
  if (!identity) return { kind: "none" };
  if (historyRunId !== null) return { kind: "history", fingerprint: identity.fingerprint };
  return { kind: "published", runId, token: identity.latest_published?.token ?? null };
}

function staleOutcome(error = null) { return { ok: false, aborted: true, error }; }
