function range(searchParams, minimumName, maximumName) {
  const minimumText = minimumName ? searchParams.get(minimumName) : null;
  const maximumText = maximumName ? searchParams.get(maximumName) : null;
  return {
    minimum: minimumText === null ? null : Number(minimumText),
    maximum: maximumText === null ? null : Number(maximumText),
  };
}

function within(value, bounds) {
  return (bounds.minimum === null || value >= bounds.minimum)
    && (bounds.maximum === null || value <= bounds.maximum);
}

export function filterMarketScanRange(items, searchParams, field, minimumName, maximumName) {
  const bounds = range(searchParams, minimumName, maximumName);
  return items.filter((item) => within(Number(item[field]), bounds));
}

export function filterMarketScanResearchRange(items, searchParams, field, minimumName, maximumName) {
  const bounds = range(searchParams, minimumName, maximumName);
  return items.filter((item) => {
    const value = Number(item.score_details?.components?.score_dimensions?.scores?.[field]);
    return within(value, bounds);
  });
}
