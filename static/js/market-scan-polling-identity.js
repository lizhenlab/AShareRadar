const SCHEMA_VERSION = "market-scan-polling-identity-v1";
const AUTHORIZATION = "change_detection_only";
const MODES = new Set(["official", "intraday", "preopen"]);
const SHA256 = /^[0-9a-f]{64}$/;
const IDENTITY_FIELDS = Object.freeze([
  "authorization", "fingerprint", "latest", "latest_published", "request_mode", "schema_version",
]);
const TOKEN_FIELDS = Object.freeze(["run_id", "token"]);

export function validateMarketScanPollingIdentity(value, expectedMode) {
  const identity = exactObject(value, IDENTITY_FIELDS, "扫描轮询身份");
  if (identity.schema_version !== SCHEMA_VERSION) throw contractError("扫描轮询身份版本不受支持");
  if (identity.authorization !== AUTHORIZATION) throw contractError("扫描轮询身份不得携带授权");
  if (!MODES.has(identity.request_mode) || identity.request_mode !== expectedMode) {
    throw contractError("扫描轮询身份的模式不匹配");
  }
  const latest = pollingToken(identity.latest, "扫描轮询身份.latest");
  const published = pollingToken(identity.latest_published, "扫描轮询身份.latest_published");
  if (!SHA256.test(String(identity.fingerprint || ""))) throw contractError("扫描轮询身份指纹无效");
  if (latest.run_id === null && published.run_id !== null) {
    throw contractError("已发布轮询批次不能脱离全局最近批次");
  }
  if (latest.run_id !== null && published.run_id !== null && published.run_id > latest.run_id) {
    throw contractError("已发布轮询批次不能晚于全局最近批次");
  }
  if (latest.run_id === published.run_id && latest.token !== published.token) {
    throw contractError("同一批次的扫描轮询 token 不一致");
  }
  return Object.freeze({
    authorization: AUTHORIZATION,
    fingerprint: identity.fingerprint,
    latest,
    latest_published: published,
    request_mode: identity.request_mode,
    schema_version: SCHEMA_VERSION,
  });
}

export function marketScanPollingIdentityChanged(previous, next) {
  return (previous?.fingerprint ?? null) !== (next?.fingerprint ?? null);
}

export function marketScanPollingTokenChanged(previous, next) {
  return (previous?.token ?? null) !== (next?.token ?? null)
    || (previous?.run_id ?? null) !== (next?.run_id ?? null);
}

function pollingToken(value, context) {
  const token = exactObject(value, TOKEN_FIELDS, context);
  if (token.run_id !== null && (!Number.isInteger(token.run_id) || token.run_id < 1)) {
    throw contractError(`${context}.run_id 无效`);
  }
  if (!SHA256.test(String(token.token || ""))) throw contractError(`${context}.token 无效`);
  return Object.freeze({ run_id: token.run_id, token: token.token });
}

function exactObject(value, expectedFields, context) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw contractError(`${context} 必须是对象`);
  const actual = Object.keys(value).sort();
  const expected = [...expectedFields].sort();
  if (actual.length !== expected.length || actual.some((field, index) => field !== expected[index])) {
    throw contractError(`${context} 字段不符合合同`);
  }
  return value;
}

function contractError(message) {
  const error = new Error(message);
  error.code = "market_scan_polling_identity_contract_error";
  return error;
}
