import { compactErrorMessage } from "./errors.js";
import { parseQuoteStreamFrame, quoteStreamErrorMessage } from "./quote-stream-contracts.js";

export function createQuoteStreamController(options) {
  const settings = {
    ...options,
    createStream: options.createStream || ((url) => new globalThis.EventSource(url)),
    setTimeout: options.setTimeout || ((callback, delay) => globalThis.setTimeout(callback, delay)),
    clearTimeout: options.clearTimeout || ((timer) => globalThis.clearTimeout(timer)),
  };
  const owner = {
    settings, stream: null, context: null, subscriptionKey: "", sequence: 0,
    retryTimer: null, retryCount: 0, retryGeneration: 0, disposed: false,
  };
  return Object.freeze({
    start: (request = {}) => start(owner, request),
    reconcile: (request = {}) => reconcile(owner, request),
    stop: () => { if (!owner.disposed) stop(owner); },
    dispose() {
      if (owner.disposed) return;
      owner.disposed = true;
      stop(owner);
    },
    snapshot: () => Object.freeze({
      connected: owner.stream !== null,
      subscriptionKey: owner.subscriptionKey,
      retryScheduled: owner.retryTimer !== null,
      retryCount: owner.retryCount,
      disposed: owner.disposed,
    }),
  });
}

function reconcile(owner, { context = owner.settings.getContext() }) {
  if (owner.disposed || !owner.settings.canReconcile(context)) return false;
  const key = owner.settings.getSymbols().join(",");
  if (owner.stream && owner.subscriptionKey === key && owner.context
      && owner.context.symbol === context.symbol && owner.context.loadSeq === context.loadSeq) return false;
  return start(owner, { context });
}

function start(owner, { retry = false, context = owner.settings.getContext() }) {
  const { settings } = owner;
  if (owner.disposed || !settings.canConnect(context)) return false;
  clearRetryTimer(owner);
  if (!retry) owner.retryCount = 0;
  const sequence = ++owner.sequence;
  closeConnection(owner);
  owner.context = { symbol: context.symbol, loadSeq: context.loadSeq, signal: context.signal };
  settings.onStatus("connecting", "观察报价流连接中", "", false);
  const symbols = settings.getSymbols();
  if (!symbols.length) {
    owner.context = null;
    settings.onStatus("idle", "核心分析快照已加载；观察报价流未启动", "warn", false);
    return false;
  }
  let stream;
  try {
    stream = settings.createStream(`/api/stream/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
  } catch (error) {
    owner.context = null;
    const detail = compactErrorMessage(error.message || "创建失败");
    settings.onStatus("error", `观察报价流创建失败：${detail}`, "warn", false);
    return false;
  }
  owner.stream = stream;
  owner.subscriptionKey = symbols.join(",");
  stream.onmessage = (event) => {
    if (isCurrent(owner, stream, sequence, context)) renderFrame(owner, event);
  };
  stream.addEventListener("quote-error", (event) => {
    if (isCurrent(owner, stream, sequence, context)) settings.onStatus("error", quoteStreamErrorMessage(event), "warn", false);
  });
  stream.onerror = () => scheduleReconnect(owner, stream, sequence, context);
  return true;
}

function renderFrame(owner, event) {
  const { rows, error } = parseQuoteStreamFrame(event);
  const { settings } = owner;
  if (error) {
    settings.onStatus("invalid", error, "warn", false);
    return;
  }
  if (!rows.length) {
    settings.onStatus("connecting", "观察报价流暂无有效数据，等待下一帧", "warn", false);
    return;
  }
  try {
    settings.onRows(rows);
  } catch (error) {
    settings.onStatus("invalid", "观察报价流显示异常，已保留上一帧", "warn", false);
    return;
  }
  owner.retryCount = 0;
  settings.onStatus("ready", "核心分析快照已加载；观察报价流已收到有效帧", "ok", true);
}

function isCurrent(owner, stream, sequence, context) {
  return !owner.disposed && sequence === owner.sequence && stream === owner.stream && owner.settings.isContextCurrent(context);
}

function scheduleReconnect(owner, stream, sequence, context) {
  if (!isCurrent(owner, stream, sequence, context)) return;
  owner.settings.onStatus("reconnecting", "观察报价流连接波动，准备重连", "warn", false);
  if (owner.retryTimer !== null || owner.settings.isHidden()) return;
  stop(owner, { clearRetry: false, preserveStatus: true });
  const delay = Math.min(30000, 2000 * 2 ** Math.min(owner.retryCount, 4));
  owner.retryCount += 1;
  const generation = ++owner.retryGeneration;
  owner.retryTimer = owner.settings.setTimeout(() => {
    if (owner.disposed || generation !== owner.retryGeneration) return;
    owner.retryTimer = null;
    if (!owner.settings.isContextCurrent(context)) return;
    start(owner, { retry: true, context });
  }, delay);
}

function stop(owner, { clearRetry = true, preserveStatus = false } = {}) {
  if (clearRetry) clearRetryTimer(owner);
  if (owner.stream || owner.context) owner.sequence += 1;
  closeConnection(owner);
  if (!preserveStatus) owner.settings.onStatus("idle", "", "", false);
}

function closeConnection(owner) {
  if (owner.stream) owner.stream.close();
  owner.stream = null;
  owner.context = null;
  owner.subscriptionKey = "";
}

function clearRetryTimer(owner) {
  owner.retryGeneration += 1;
  if (owner.retryTimer === null) return;
  owner.settings.clearTimeout(owner.retryTimer);
  owner.retryTimer = null;
}
