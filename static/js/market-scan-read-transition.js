export function createMarketScanReadTransition(options) {
  const context = { ...options, generation: 0, tail: Promise.resolve() };
  return {
    invalidateOwner: (invalidateOptions) => invalidateOwner(context, invalidateOptions),
    run: (operation) => scheduleOwnedRead(context, operation),
    transition: (operation, transitionOptions) => (
      scheduleTransition(context, operation, transitionOptions)
    ),
  };
}

function scheduleTransition(context, operation, options = {}) {
  const generation = invalidateOwner(context);
  context.probabilityHorizon.supersede(options);
  return scheduleOwnedRead(context, operation, generation);
}

function invalidateOwner(context, options = {}) {
  context.generation += 1;
  if (!options.allowTrustedSelection) {
    if (context.state.runRequest) context.state.runRequestSeq += 1;
    void context.latestSync.supersede().finally(() => context.probabilityHorizon.requestFinished?.());
  }
  return context.generation;
}

function scheduleOwnedRead(context, operation, generation = context.generation) {
  const predecessor = context.tail.catch(() => null);
  const owned = predecessor.then(() => (
    generation === context.generation
      ? operation({ isCurrent: () => generation === context.generation })
      : null
  ));
  context.tail = owned.catch(() => null);
  return owned;
}
