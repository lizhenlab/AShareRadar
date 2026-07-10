export function drawKlineChart({
  canvas,
  rows,
  ma5,
  ma20,
  marks = [],
  activeCategories = new Set(),
  formatNumber = defaultFormatNumber,
}) {
  if (!canvas || typeof canvas.getContext !== "function") return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const data = validChartRows(rows).slice(-60);
  if (!data.length) return;
  const padding = { left: 46, right: 16, top: 18, bottom: 28 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const scalePrices = chartScalePrices(data, ma5, ma20);
  const maxPrice = Math.max(...scalePrices.highs);
  const minPrice = Math.min(...scalePrices.lows);
  const range = Math.max(0.01, maxPrice - minPrice);
  const xStep = chartWidth / data.length;
  const candleWidth = Math.max(4, Math.min(12, xStep * 0.58));
  const y = (price) => padding.top + (maxPrice - price) / range * chartHeight;

  drawGrid(ctx, width, height, padding, chartHeight, maxPrice, range, formatNumber);
  drawCandles(ctx, data, y, padding.left, xStep, candleWidth);
  drawPriceLine(ctx, data, 5, "#2563eb", y, padding.left, xStep);
  drawPriceLine(ctx, data, 20, "#b7791f", y, padding.left, xStep);
  drawChartMarks(ctx, data, marks, activeCategories, y, padding.left, xStep, height);
  drawDateLabels(ctx, data, width, height, padding);
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
  return { ...item, open, close, high, low, date: String((item && item.date) || "") };
}

function chartScalePrices(data, ma5, ma20) {
  const optionalLines = [positiveNumber(ma5), positiveNumber(ma20)].filter(Boolean);
  return {
    highs: [...data.map((item) => item.high), ...optionalLines],
    lows: [...data.map((item) => item.low), ...optionalLines],
  };
}

function drawGrid(ctx, width, height, padding, chartHeight, maxPrice, range, formatNumber) {
  ctx.strokeStyle = "#e6ebf2";
  ctx.lineWidth = 1;
  ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.fillStyle = "#667085";
  for (let i = 0; i <= 4; i += 1) {
    const py = padding.top + (chartHeight / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, py);
    ctx.lineTo(width - padding.right, py);
    ctx.stroke();
    const label = maxPrice - (range / 4) * i;
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

function drawChartMarks(ctx, data, marks, activeCategories, y, left, xStep, height) {
  const visibleMarks = visibleChartMarks(marks, activeCategories);
  if (!visibleMarks.length) return;
  const byDate = chartRowsByDate(data);
  visibleMarks.slice(0, 18).forEach((mark) => drawChartMark(ctx, mark, byDate, y, left, xStep, height));
}

function visibleChartMarks(marks, activeCategories) {
  return (marks || []).filter((mark) => mark.visible !== false && hasActiveCategory(activeCategories, mark.category));
}

function hasActiveCategory(activeCategories, category) {
  if (activeCategories && typeof activeCategories.has === "function") return activeCategories.has(category);
  if (Array.isArray(activeCategories)) return activeCategories.includes(category);
  return false;
}

function chartRowsByDate(data) {
  const byDate = new Map();
  data.forEach((item, index) => {
    byDate.set(String(item.date).slice(0, 10), { item, index });
  });
  return byDate;
}

function drawChartMark(ctx, mark, byDate, y, left, xStep, height) {
  const target = chartMarkTarget(mark, byDate);
  if (!target) return;
  const position = chartMarkPosition(mark, target, y, left, xStep, height);
  ctx.fillStyle = chartMarkColor(mark);
  ctx.beginPath();
  ctx.arc(position.x, position.y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#344054";
  ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.fillText(chartMarkLabel(mark), position.x + 6, position.y - 6);
}

function chartMarkTarget(mark, byDate) {
  const key = chartMarkDate(mark);
  return byDate.get(key) || byDate.get(key.replaceAll("/", "-"));
}

function chartMarkDate(mark) {
  return String(mark.kline_date || mark.date || "").slice(0, 10);
}

function chartMarkPosition(mark, target, y, left, xStep, height) {
  const x = left + xStep * target.index + xStep / 2;
  const price = positiveNumber(mark.price) || target.item.close;
  return {
    x,
    y: clamp(y(price), 18, height - 38),
  };
}

function chartMarkColor(mark) {
  if (mark.color) return mark.color;
  if (mark.level === "风险") return "#0f9f6e";
  if (mark.level === "积极") return "#d92d20";
  return "#b7791f";
}

function chartMarkLabel(mark) {
  return String(mark.label || mark.category).slice(0, 6);
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function drawPriceLine(ctx, data, windowSize, color, y, left, xStep) {
  if (data.length < windowSize) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  data.forEach((item, index) => {
    if (index < windowSize - 1) return;
    const slice = data.slice(index - windowSize + 1, index + 1);
    const avg = slice.reduce((sum, row) => sum + row.close, 0) / windowSize;
    const x = left + xStep * index + xStep / 2;
    const py = y(avg);
    if (index === windowSize - 1) ctx.moveTo(x, py);
    else ctx.lineTo(x, py);
  });
  ctx.stroke();
}

function drawDateLabels(ctx, data, width, height, padding) {
  ctx.fillStyle = "#667085";
  ctx.fillText(String(data[0].date).slice(5), padding.left, height - 8);
  ctx.fillText(String(data[data.length - 1].date).slice(5), width - padding.right - 38, height - 8);
}

function defaultFormatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(2);
}

function positiveNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}
