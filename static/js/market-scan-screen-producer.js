import { comparisonRunKey } from "./market-scan-comparison-contracts.js";
import { clearAppliedScreenContext, readAppliedScreenContext } from "./market-scan-screen-context.js";

const producers = new WeakMap();

export function screenProducer(element) { return element ? producers.get(element) || null : null; }

export function beginScreenProducer(element, kind, run, cancel = () => {}) {
  const previous = screenProducer(element);
  const owner = { kind, runId: run?.id, runKey: comparisonRunKey(run), cancel };
  if (element) producers.set(element, owner);
  if (previous && previous.kind !== kind) previous.cancel();
  clearAppliedScreenContext(element);
  return owner;
}

export function ownsScreenProducer(element, owner) {
  return Boolean(owner && (!element || screenProducer(element) === owner));
}

export function releaseScreenProducer(element, kind = null) {
  const owner = screenProducer(element);
  if (!owner || (kind !== null && owner.kind !== kind)) return;
  producers.delete(element);
  owner.cancel();
  clearAppliedScreenContext(element);
}

export function presetOwnsScreen(element, run) {
  const owner = screenProducer(element);
  if (owner?.kind !== "preset" || owner.runId !== run?.id) return false;
  const key = comparisonRunKey(run);
  return !key || !owner.runKey || owner.runKey === key;
}

export function releaseOtherScreenBatch(element, run) {
  const owner = screenProducer(element);
  if (owner && owner.runKey !== comparisonRunKey(run)) releaseScreenProducer(element);
}

// Preserve confirmed rows across document visibility changes; pending reads lose display ownership.
export function suspendScreenProducer(element) {
  if (!readAppliedScreenContext(element)) releaseScreenProducer(element);
}

export function ownsScreenProbe(element, probe) {
  if (!probe) return true;
  return probe.screenOwner
    ? ownsScreenProducer(element, probe.screenOwner)
    : probe.producerBefore === screenProducer(element);
}
