import { MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS } from "./market-scan-latest-loader.js";

// A busy response has not started a snapshot read. Retry only that explicit,
// server-signalled case, sharing one deadline across requests and backoff.
export async function requestMarketScanRead(request, url, options = {}) {
  const { timeoutMs = MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS, ...requestOptions } = options;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error("冻结快照读取超时预算无效");
  const deadline = performance.now() + timeoutMs;
  const signal = requestOptions.signal;
  for (let attempt = 0; ; attempt += 1) {
    requireActive(signal);
    const remaining = deadline - performance.now();
    if (remaining <= 0) throw new Error("冻结快照读取等待超时，请稍后重试");
    try {
      const payload = await request(url, { ...requestOptions, timeoutMs: remaining });
      requireActive(signal);
      if (performance.now() >= deadline) throw new Error("冻结快照读取等待超时，请稍后重试");
      return payload;
    } catch (error) {
      requireActive(signal);
      const delay = retryDelay(error, attempt, deadline);
      if (delay === null) throw error;
      await waitForRetry(delay, signal);
    }
  }
}

function retryDelay(error, attempt, deadline) {
  const milliseconds = error?.retryAfterMs;
  if (attempt >= 30 || error?.status !== 503 || !Number.isFinite(milliseconds) || milliseconds < 0) return null;
  const delay = Math.max(250, Math.ceil(milliseconds));
  return performance.now() + delay < deadline ? delay : null;
}

function requireActive(signal) {
  if (signal?.aborted) throw new DOMException("冻结快照读取已取消", "AbortError");
}

function waitForRetry(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const finish = () => { signal?.removeEventListener("abort", cancel); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const cancel = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
      reject(new DOMException("冻结快照读取已取消", "AbortError"));
    };
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
  });
}
