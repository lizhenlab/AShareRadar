export function saveMarketScanExport(context, blob, disposition, run) {
  const filename = marketScanExportFilename(disposition, run);
  const urlApi = context.root?.defaultView?.URL || globalThis.URL;
  const objectUrl = urlApi.createObjectURL(blob);
  try {
    const anchor = context.root.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
  } finally {
    urlApi.revokeObjectURL(objectUrl);
  }
  return filename;
}

export function marketScanExportFilename(contentDisposition, run) {
  const fallbackPart = String(run?.quote_date || run?.id || "results").replace(/[^a-z0-9_-]/gi, "-");
  const fallback = `AShareRadar-market-scan-${fallbackPart}.xlsx`;
  const value = String(contentDisposition || "");
  const encoded = value.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  const plain = value.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
  let candidate = encoded || plain?.[1] || plain?.[2] || "";
  try { candidate = encoded ? decodeURIComponent(candidate.trim()) : candidate.trim(); } catch { return fallback; }
  candidate = candidate.split(/[\\/]/).at(-1).replace(/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "").trim();
  if (!candidate || !/\.xlsx$/i.test(candidate)) return fallback;
  return candidate.length <= 180 ? candidate : `${candidate.slice(0, 175)}.xlsx`;
}
