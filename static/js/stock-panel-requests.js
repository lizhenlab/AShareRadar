import { createRequestScope } from "./api.js";

// Reads replace reads; independent writes only own their eventual UI refresh.
export function createStockPanelRequestOwner({ readPrefix, mutationPrefix }) {
  const refreshPrefix = `${mutationPrefix}Refresh`;
  return {
    beginRead: (state, options = {}, symbol = options.symbol || state.symbol) => beginRead(state, readPrefix, options, symbol),
    beginMutation: (state, options = {}, symbol = options.symbol || state.symbol) => beginMutation(state, mutationPrefix, options, symbol),
    beginRefresh: (state, mutation) => beginRefresh(state, refreshPrefix, mutation),
    finishRead: (state, request) => finishRead(state, readPrefix, request),
    finishMutation: (state, request) => finishMutation(state, mutationPrefix, request),
    finishRefresh: (state, request) => finishRead(state, refreshPrefix, request),
  };
}

function beginRead(state, prefix, options, symbol) {
  const requestId = Number(state[`${prefix}Seq`] || 0) + 1;
  const stateSymbol = state.symbol;
  const scope = createRequestScope(state[`${prefix}Request`], options.signal);
  state[`${prefix}Seq`] = requestId;
  state[`${prefix}Request`] = scope;
  return {
    id: requestId, scope, signal: scope.signal, symbol,
    isCurrent: () => state[`${prefix}Seq`] === requestId
      && state[`${prefix}Request`] === scope && !scope.signal.aborted
      && (options.isCurrent ? options.isCurrent() : state.symbol === stateSymbol),
  };
}

function beginMutation(state, prefix, options, symbol) {
  const requestId = Number(state[`${prefix}Seq`] || 0) + 1;
  const stateSymbol = state.symbol;
  // A stock switch may invalidate the UI tail, but must not abort persistence.
  const scope = createRequestScope();
  const requests = mutationRequests(state, prefix);
  state[`${prefix}Seq`] = requestId;
  requests.set(requestId, scope);
  return {
    id: requestId, scope, signal: scope.signal, contextSignal: options.signal, symbol,
    isCurrent: () => requests.get(requestId) === scope && !scope.signal.aborted
      && (!options.signal || !options.signal.aborted)
      && (options.isCurrent ? options.isCurrent() : state.symbol === stateSymbol),
  };
}

function beginRefresh(state, prefix, mutation) {
  const requestId = Number(state[`${prefix}Seq`] || 0) + 1;
  const scope = createRequestScope(state[`${prefix}Request`], mutation.contextSignal);
  state[`${prefix}Seq`] = requestId;
  state[`${prefix}Request`] = scope;
  return {
    scope, signal: scope.signal, symbol: mutation.symbol,
    isCurrent: () => mutation.isCurrent() && state[`${prefix}Seq`] === requestId
      && state[`${prefix}Request`] === scope && !scope.signal.aborted,
  };
}

function mutationRequests(state, prefix) {
  const key = `${prefix}Requests`;
  if (!(state[key] instanceof Map)) state[key] = new Map();
  return state[key];
}

function finishRead(state, prefix, request) {
  if (state[`${prefix}Request`] === request.scope) state[`${prefix}Request`] = null;
  request.scope.dispose();
}

function finishMutation(state, prefix, request) {
  const requests = mutationRequests(state, prefix);
  if (requests.get(request.id) === request.scope) requests.delete(request.id);
  request.scope.dispose();
}
