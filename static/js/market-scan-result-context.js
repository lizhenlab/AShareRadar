import { samePublishedMarketScanRun } from "./market-scan-latest-loader.js";

export function resultContext(options, run) {
  return {
    browseMode: options.state.browseMode,
    historyRunId: options.state.selectedHistoryRunId,
    run: run ? structuredClone(run) : null,
  };
}

export function contextMatches(context, options, run) {
  return Boolean(
    context
    && context.browseMode === options.state.browseMode
    && context.historyRunId === options.state.selectedHistoryRunId
    && samePublishedMarketScanRun(context.run, run)
  );
}

export function identityMatches(binding, identity) {
  if (binding.kind === "none") return identity === null;
  if (binding.kind === "history") return binding.fingerprint === identity?.fingerprint;
  return binding.runId === identity?.latest_published?.run_id
    && binding.token === identity?.latest_published?.token;
}

export function identityBinding(identity, historyRunId, runId) {
  if (!identity) return { kind: "none" };
  if (historyRunId !== null) return { kind: "history", fingerprint: identity.fingerprint };
  return { kind: "published", runId, token: identity.latest_published?.token ?? null };
}
