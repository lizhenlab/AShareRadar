export const DEFAULT_CHART_ROW_LIMIT = 60;

const CHART_PADDING = Object.freeze({ left: 46, right: 16, top: 18, bottom: 28 });
const CANDLE_WIDTH_RATIO = 0.58;
const MAX_CANDLE_WIDTH = 12;
const MAX_VISIBLE_MARKS = 18;
const PRICE_GRID_SEGMENTS = 4;

export function drawKlineChart({
  canvas,
  rows,
  ma5,
  ma20,
  marks = [],
  activeCategories = new Set(),
  formatNumber = defaultFormatNumber,
  rowLimit,
  maxRows,
  showMarks = true,
  showMa5 = true,
  showMa20 = true,
} = {}) {
  const limit = chartRowLimit(rowLimit, maxRows);
  const validRows = validChartRows(rows);
  const visibleStartIndex = Math.max(0, validRows.length - limit);
  const data = validRows.slice(visibleStartIndex);
  const result = emptyDrawResult(rows, validRows, data, limit);
  if (!canvas || typeof canvas.getContext !== "function") {
    return { ...result, reason: "canvas-unavailable" };
  }

  const width = positiveDimension(canvas.clientWidth);
  const height = positiveDimension(canvas.clientHeight);
  if (!width || !height) return { ...result, reason: "canvas-has-no-size" };

  const ratio = chartPixelRatio();
  canvas.width = Math.max(1, Math.round(width * ratio));
  canvas.height = Math.max(1, Math.round(height * ratio));
  const ctx = canvas.getContext("2d");
  if (!ctx) return { ...result, width, height, reason: "context-unavailable" };

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.scale(ratio, ratio);
  if (!data.length) return { ...result, width, height, reason: "empty-data" };

  const chartWidth = width - CHART_PADDING.left - CHART_PADDING.right;
  const chartHeight = height - CHART_PADDING.top - CHART_PADDING.bottom;
  if (chartWidth <= 0 || chartHeight <= 0) {
    return { ...result, width, height, reason: "insufficient-chart-space" };
  }

  const ma5Values = movingAverageValues(validRows, visibleStartIndex, data.length, 5);
  const ma20Values = movingAverageValues(validRows, visibleStartIndex, data.length, 20);
  const visibleMa5Values = showMa5 ? ma5Values : [];
  const visibleMa20Values = showMa20 ? ma20Values : [];
  const scalePrices = chartScalePrices(data, [visibleMa5Values, visibleMa20Values]);
  const maxPrice = Math.max(...scalePrices.highs);
  const minPrice = Math.min(...scalePrices.lows);
  const range = Math.max(0.01, maxPrice - minPrice);
  const xStep = chartWidth / data.length;
  const candleWidth = Math.min(MAX_CANDLE_WIDTH, xStep * CANDLE_WIDTH_RATIO);
  const y = (price) => CHART_PADDING.top + (maxPrice - price) / range * chartHeight;
  const numberFormatter = typeof formatNumber === "function" ? formatNumber : defaultFormatNumber;

  drawGrid(ctx, width, CHART_PADDING, chartHeight, maxPrice, range, numberFormatter);
  drawCandles(ctx, data, y, CHART_PADDING.left, xStep, candleWidth);
  const ma5PointCount = showMa5
    ? drawPriceLine(ctx, ma5Values, "#2563eb", y, CHART_PADDING.left, xStep)
    : 0;
  const ma20PointCount = showMa20
    ? drawPriceLine(ctx, ma20Values, "#b7791f", y, CHART_PADDING.left, xStep)
    : 0;
  const markCount = showMarks
    ? drawChartMarks(
      ctx,
      data,
      marks,
      activeCategories,
      y,
      CHART_PADDING.left,
      xStep,
      height,
      width - CHART_PADDING.right,
    )
    : 0;
  const labels = drawEventLabels(ctx, data, width, height, CHART_PADDING);
  const inspection = chartInspectionSnapshot(
    data,
    showMa5 ? ma5Values : data.map(() => null),
    showMa20 ? ma20Values : data.map(() => null),
    width,
    height,
    xStep,
    minPrice,
    maxPrice,
  );

  return {
    ...result,
    drawn: true,
    width,
    height,
    xStep,
    candleWidth,
    markCount,
    ma5Drawn: ma5PointCount > 0,
    ma20Drawn: ma20PointCount > 0,
    ma5PointCount,
    ma20PointCount,
    minPrice,
    maxPrice,
    startLabel: labels.start,
    endLabel: labels.end,
    inspection,
    reason: null,
  };
}

function emptyDrawResult(rows, validRows, data, rowLimit) {
  return {
    drawn: false,
    rowCount: data.length,
    validRowCount: validRows.length,
    inputRowCount: Array.isArray(rows) ? rows.length : 0,
    rowLimit,
    width: 0,
    height: 0,
    xStep: 0,
    candleWidth: 0,
    markCount: 0,
    ma5Drawn: false,
    ma20Drawn: false,
    ma5PointCount: 0,
    ma20PointCount: 0,
    minPrice: null,
    maxPrice: null,
    startLabel: "",
    endLabel: "",
    inspection: null,
    reason: null,
  };
}

function chartInspectionSnapshot(data, ma5Values, ma20Values, width, height, xStep, minPrice, maxPrice) {
  const rows = data.map((item) => Object.freeze({
    eventTime: item.eventTime,
    date: item.date,
    timestamp: item.timestamp,
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close,
    volume: finiteNumberOrZero(item.volume),
    source: boundedChartText(item.source, 120),
    fetchedAt: boundedChartText(item.fetched_at, 40),
    fromCache: item.from_cache === true,
    fallbackUsed: item.fallback_used === true,
  }));
  const bounds = Object.freeze({
    left: CHART_PADDING.left,
    right: width - CHART_PADDING.right,
    top: CHART_PADDING.top,
    bottom: height - CHART_PADDING.bottom,
  });
  return Object.freeze({
    width,
    height,
    bounds,
    xStep,
    minPrice,
    maxPrice,
    rows: Object.freeze(rows),
    ma5: Object.freeze(ma5Values.map(finiteNumberOrNull)),
    ma20: Object.freeze(ma20Values.map(finiteNumberOrNull)),
  });
}

function boundedChartText(value, maxLength) {
  if (value === null || value === undefined) return "";
  return String(value).trim().slice(0, maxLength);
}

function chartRowLimit(rowLimit, maxRows) {
  return positiveInteger(rowLimit) || positiveInteger(maxRows) || DEFAULT_CHART_ROW_LIMIT;
}

function positiveInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
}

function positiveDimension(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function chartPixelRatio() {
  const ratio = Number(globalThis.window?.devicePixelRatio ?? globalThis.devicePixelRatio ?? 1);
  return Number.isFinite(ratio) && ratio > 0 ? ratio : 1;
}

function validChartRows(rows) {
  if (!Array.isArray(rows)) return [];
  return rows.map(chartRow).filter(Boolean);
}

function chartRow(item) {
  const open = positiveNumber(item && item.open);
  const close = positiveNumber(item && item.close);
  const high = positiveNumber(item && item.high);
  const low = positiveNumber(item && item.low);
  if (!open || !close || !high || !low) return null;
  if (high < low || open > high || open < low || close > high || close < low) return null;
  const date = eventTimeText(item && item.date);
  const timestamp = eventTimeText(item && item.timestamp);
  return { ...item, open, close, high, low, date, timestamp, eventTime: date || timestamp };
}

function eventTimeText(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function chartScalePrices(data, movingAverageLines) {
  const optionalLines = movingAverageLines
    .flatMap((values) => values)
    .map(positiveNumber)
    .filter(Boolean);
  return {
    highs: [...data.map((item) => item.high), ...optionalLines],
    lows: [...data.map((item) => item.low), ...optionalLines],
  };
}

function drawGrid(ctx, width, padding, chartHeight, maxPrice, range, formatNumber) {
  ctx.strokeStyle = "#e6ebf2";
  ctx.lineWidth = 1;
  ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.fillStyle = "#667085";
  for (let index = 0; index <= PRICE_GRID_SEGMENTS; index += 1) {
    const py = padding.top + chartHeight / PRICE_GRID_SEGMENTS * index;
    ctx.beginPath();
    ctx.moveTo(padding.left, py);
    ctx.lineTo(width - padding.right, py);
    ctx.stroke();
    const label = maxPrice - range / PRICE_GRID_SEGMENTS * index;
    ctx.fillText(formatNumber(label), 6, py + 4);
  }
}

function drawCandles(ctx, data, y, left, xStep, candleWidth) {
  data.forEach((item, index) => {
    const x = left + xStep * index + xStep / 2;
    const up = item.close >= item.open;
    ctx.strokeStyle = up ? "#d92d20" : "#0f9f6e";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(x, y(item.high));
    ctx.lineTo(x, y(item.low));
    ctx.stroke();
    const top = y(Math.max(item.open, item.close));
    const bottom = y(Math.min(item.open, item.close));
    ctx.fillRect(x - candleWidth / 2, top, candleWidth, Math.max(2, bottom - top));
  });
}

function drawChartMarks(ctx, data, marks, activeCategories, y, left, xStep, height, right) {
  const byDate = chartRowsByDate(data);
  const visibleMarks = visibleChartMarks(marks, activeCategories).filter(
    (mark) => chartMarkTarget(mark, byDate),
  );
  const occupiedLabels = [];
  return visibleMarks.slice(0, MAX_VISIBLE_MARKS).reduce(
    (count, mark) => count + (drawChartMark(
      ctx,
      mark,
      byDate,
      y,
      left,
      xStep,
      height,
      right,
      occupiedLabels,
    ) ? 1 : 0),
    0,
  );
}

function visibleChartMarks(marks, activeCategories) {
  if (!Array.isArray(marks)) return [];
  return marks.filter(
    (mark) => mark && mark.visible !== false && hasActiveCategory(activeCategories, mark.category),
  );
}

function hasActiveCategory(activeCategories, category) {
  if (activeCategories && typeof activeCategories.has === "function") return activeCategories.has(category);
  if (Array.isArray(activeCategories)) return activeCategories.includes(category);
  return false;
}

function chartRowsByDate(data) {
  const byDate = new Map();
  data.forEach((item, index) => {
    const date = canonicalDate(item.date);
    if (date) byDate.set(date, { item, index });
  });
  return byDate;
}

function drawChartMark(ctx, mark, byDate, y, left, xStep, height, right, occupiedLabels) {
  const target = chartMarkTarget(mark, byDate);
  if (!target) return false;
  const position = chartMarkPosition(mark, target, y, left, xStep, height);
  ctx.fillStyle = chartMarkColor(mark);
  ctx.beginPath();
  ctx.arc(position.x, position.y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#344054";
  ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
  drawChartMarkLabel(ctx, chartMarkLabel(mark), position, left, right, height, occupiedLabels);
  return true;
}

function drawChartMarkLabel(ctx, label, position, left, right, height, occupiedLabels) {
  if (!label) return false;
  const labelWidth = measuredTextWidth(ctx, label);
  const x = chartMarkLabelX(position.x, labelWidth, left, right);
  for (const y of chartMarkLabelBaselines(position.y, height)) {
    const bounds = { left: x - 2, right: x + labelWidth + 2, top: y - 11, bottom: y + 3 };
    if (occupiedLabels.some((other) => chartLabelBoundsOverlap(bounds, other))) continue;
    occupiedLabels.push(bounds);
    ctx.fillText(label, x, y);
    return true;
  }
  return false;
}

function chartMarkLabelX(markX, labelWidth, left, right) {
  const preferred = markX + 6;
  const flipped = markX - labelWidth - 6;
  const candidate = preferred + labelWidth <= right ? preferred : flipped;
  return clamp(candidate, left, Math.max(left, right - labelWidth));
}

function chartMarkLabelBaselines(markY, height) {
  const minimum = CHART_PADDING.top + 11;
  const maximum = height - CHART_PADDING.bottom - 3;
  return Array.from({ length: MAX_VISIBLE_MARKS }, (_, index) => {
    if (index === 0) return clamp(markY - 6, minimum, maximum);
    const distance = Math.ceil(index / 2) * 14;
    const direction = index % 2 ? -1 : 1;
    return clamp(markY - 6 + direction * distance, minimum, maximum);
  });
}

function chartLabelBoundsOverlap(left, right) {
  return !(
    left.right <= right.left
    || left.left >= right.right
    || left.bottom <= right.top
    || left.top >= right.bottom
  );
}

function chartMarkTarget(mark, byDate) {
  const key = chartMarkDate(mark);
  return key ? byDate.get(key) : null;
}

function chartMarkDate(mark) {
  return canonicalDate(mark && (mark.kline_date || mark.date));
}

function canonicalDate(value) {
  const match = eventTimeText(value).match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:$|[T\s])/);
  if (!match) return "";
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1) return "";
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (day > daysInMonth) return "";
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function chartMarkPosition(mark, target, y, left, xStep, height) {
  const x = left + xStep * target.index + xStep / 2;
  const price = positiveNumber(mark.price) || target.item.close;
  return {
    x,
    y: clamp(y(price), CHART_PADDING.top, height - CHART_PADDING.bottom - 10),
  };
}

function chartMarkColor(mark) {
  if (mark.color) return mark.color;
  if (mark.level === "风险") return "#0f9f6e";
  if (mark.level === "积极") return "#d92d20";
  return "#b7791f";
}

function chartMarkLabel(mark) {
  return String(mark.label || mark.category || "").slice(0, 6);
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function movingAverageValues(rows, visibleStartIndex, visibleCount, windowSize) {
  const prefixSums = [0];
  rows.forEach((row) => prefixSums.push(prefixSums[prefixSums.length - 1] + row.close));
  return Array.from({ length: visibleCount }, (_, visibleIndex) => {
    const rowIndex = visibleStartIndex + visibleIndex;
    if (rowIndex + 1 < windowSize) return null;
    const total = prefixSums[rowIndex + 1] - prefixSums[rowIndex + 1 - windowSize];
    return total / windowSize;
  });
}

function drawPriceLine(ctx, values, color, y, left, xStep) {
  const points = values.flatMap((price, index) => (
    positiveNumber(price) ? [{ index, price }] : []
  ));
  if (!points.length) return 0;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  points.forEach((point, pointIndex) => {
    const x = left + xStep * point.index + xStep / 2;
    const py = y(point.price);
    if (pointIndex === 0) ctx.moveTo(x, py);
    else ctx.lineTo(x, py);
  });
  ctx.stroke();
  return points.length;
}

function drawEventLabels(ctx, data, width, height, padding) {
  const start = eventTimeLabel(data[0].eventTime);
  const end = eventTimeLabel(data[data.length - 1].eventTime);
  ctx.fillStyle = "#667085";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
  if (start) ctx.fillText(start, padding.left, height - 8);
  if (end && (data.length > 1 || end !== start)) {
    const labelWidth = measuredTextWidth(ctx, end);
    ctx.fillText(end, Math.max(padding.left, width - padding.right - labelWidth), height - 8);
  }
  return { start, end };
}

function eventTimeLabel(value) {
  const text = eventTimeText(value);
  const match = text.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2}))?/);
  if (!match) return text.slice(0, 16);
  const date = `${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`;
  if (!match[4]) return date;
  return `${date} ${match[4].padStart(2, "0")}:${match[5]}`;
}

function measuredTextWidth(ctx, value) {
  if (typeof ctx.measureText === "function") {
    const width = Number(ctx.measureText(value).width);
    if (Number.isFinite(width) && width >= 0) return width;
  }
  return String(value).length * 7;
}

function defaultFormatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(2);
}

function finiteNumberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function finiteNumberOrZero(value) {
  return finiteNumberOrNull(value) ?? 0;
}

function positiveNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}
