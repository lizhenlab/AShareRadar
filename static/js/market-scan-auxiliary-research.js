import { createMarketScanFutureRangeController } from "./market-scan-future-range-controller.js";
import { createExperimentalProbabilityController } from "./market-scan-experimental.js";

export function createMarketScanAuxiliaryResearch(options) {
  const future = createMarketScanFutureRangeController(options);
  const experimental = createExperimentalProbabilityController(options);
  return {
    abort() { future.abort(); experimental.abort(); },
    sync(run) { future.sync(run); experimental.sync(run); },
  };
}
