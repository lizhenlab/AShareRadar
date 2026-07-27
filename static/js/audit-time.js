const SHANGHAI_OFFSET_MINUTES = 8 * 60;
const AUDIT_TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(?:([zZ])|([+-])(\d{2}):?(\d{2}))?)?$/;
const FORMATTERS = new Map();

export function auditTimestampEpoch(value) {
  if (typeof value !== "string") return null;
  const match = AUDIT_TIMESTAMP_PATTERN.exec(value.trim());
  if (!match) return null;
  const parts = timestampParts(match);
  if (!validTimestampParts(parts)) return null;
  const utcCandidate = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
    parts.millisecond,
  );
  if (!dateMatchesParts(new Date(utcCandidate), parts)) return null;
  const timestamp = utcCandidate - offsetMinutes(match, parts) * 60000;
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function formatAuditTimestamp(value, options = {}) {
  const fallback = typeof options.fallback === "string" ? options.fallback : "--";
  const timestamp = auditTimestampEpoch(value);
  if (timestamp === null) return fallback;
  const includeSeconds = options.includeSeconds !== false;
  const parts = formatter(includeSeconds).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const date = `${values.year}-${values.month}-${values.day}`;
  const time = `${values.hour}:${values.minute}${includeSeconds ? `:${values.second}` : ""}`;
  return `${date} ${time}`;
}

function formatter(includeSeconds) {
  const key = includeSeconds ? "seconds" : "minutes";
  if (!FORMATTERS.has(key)) {
    FORMATTERS.set(key, new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      ...(includeSeconds ? { second: "2-digit" } : {}),
      hourCycle: "h23",
    }));
  }
  return FORMATTERS.get(key);
}

function timestampParts(match) {
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: numberGroup(match, 4),
    minute: numberGroup(match, 5),
    second: numberGroup(match, 6),
    millisecond: Number(String(match[7] || "0").padEnd(3, "0").slice(0, 3)),
    offsetHour: numberGroup(match, 10),
    offsetMinute: numberGroup(match, 11),
  };
}

function numberGroup(match, index) {
  return match[index] ? Number(match[index]) : 0;
}

function validTimestampParts(parts) {
  return parts.year >= 1000
    && parts.month >= 1
    && parts.month <= 12
    && parts.day >= 1
    && parts.hour <= 23
    && parts.minute <= 59
    && parts.second <= 59
    && parts.offsetHour <= 23
    && parts.offsetMinute <= 59;
}

function dateMatchesParts(date, parts) {
  return date.getUTCFullYear() === parts.year
    && date.getUTCMonth() === parts.month - 1
    && date.getUTCDate() === parts.day
    && date.getUTCHours() === parts.hour
    && date.getUTCMinutes() === parts.minute
    && date.getUTCSeconds() === parts.second;
}

function offsetMinutes(match, parts) {
  if (match[8]) return 0;
  if (!match[9]) return SHANGHAI_OFFSET_MINUTES;
  const direction = match[9] === "-" ? -1 : 1;
  return direction * (parts.offsetHour * 60 + parts.offsetMinute);
}
