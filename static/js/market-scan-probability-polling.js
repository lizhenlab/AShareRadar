import { isMarketScanProbabilitySourceCapturePending } from "./market-scan-probability-contracts.js";

const DEFAULT_MAX_CAPTURE_POLL_ATTEMPTS = 60;

export function createMarketScanProbabilityPolling({ options, polling, resultRun, state }) {
  const maximum = positiveInteger(
    options.probabilityCapturePollMaxAttempts,
    DEFAULT_MAX_CAPTURE_POLL_ATTEMPTS,
  );
  let pendingRunId = null;
  let attempts = 0;

  function schedule(payload) {
    const currentRunId = resultRun()?.id ?? null;
    const responseRunId = Number(payload?.run?.id);
    if (
      responseRunId === currentRunId
      && isMarketScanProbabilitySourceCapturePending(payload?.probability_research)
    ) {
      if (pendingRunId !== responseRunId) {
        pendingRunId = responseRunId;
        attempts = 0;
      }
      if (attempts < maximum) {
        polling.scheduleProbabilityResults();
        return;
      }
    }
    if (payload === null && currentRunId !== null && pendingRunId === currentRunId) {
      if (attempts < maximum) polling.scheduleProbabilityResults();
      else stop();
      return;
    }
    stop();
  }

  async function poll(loadResults) {
    if (pendingRunId === null || resultRun()?.id !== pendingRunId) {
      stop();
      return null;
    }
    if (attempts >= maximum) {
      stop();
      return null;
    }
    attempts += 1;
    return loadResults();
  }

  function retryTarget(runId) {
    if (pendingRunId !== runId) return "results";
    if (attempts < maximum) return "probabilityResults";
    stop();
    return null;
  }

  function stop() {
    reset();
    polling.scheduleDefault(state.run);
  }

  function reset() {
    pendingRunId = null;
    attempts = 0;
  }

  return {
    poll,
    retryTarget,
    schedule,
  };
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}
