const INSPECTION_KEYS = new Set([
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "Escape",
]);

export function chartInspectionAt(snapshot, localX) {
  const view = inspectionView(snapshot);
  const x = finiteNumber(localX);
  if (!view || x === null || x < view.bounds.left || x > view.bounds.right) return null;

  const index = Math.min(
    view.rows.length - 1,
    Math.max(0, Math.floor((x - view.bounds.left) / view.xStep)),
  );
  const row = view.rows[index];
  const open = finiteNumber(row && row.open);
  const high = finiteNumber(row && row.high);
  const low = finiteNumber(row && row.low);
  const close = finiteNumber(row && row.close);
  if ([open, high, low, close].some((value) => value === null)) return null;

  const item = Object.freeze({
    eventTime: eventTime(row),
    open,
    high,
    low,
    close,
    volume: finiteNumber(row.volume) ?? 0,
    ma5: alignedValue(view.ma5, index),
    ma20: alignedValue(view.ma20, index),
    source: boundedText(row.source, 120),
    fetchedAt: boundedText(row.fetchedAt, 40),
    fromCache: row.fromCache === true,
    fallbackUsed: row.fallbackUsed === true,
  });
  return Object.freeze({
    index,
    item,
    x: view.bounds.left + view.xStep * (index + 0.5),
    y: priceY(view, close),
  });
}

export function createChartInspector({ canvas, getSnapshot, onState } = {}) {
  if (
    !canvas
    || typeof canvas.addEventListener !== "function"
    || typeof canvas.removeEventListener !== "function"
    || typeof canvas.getBoundingClientRect !== "function"
  ) {
    throw new TypeError("createChartInspector requires an event-capable canvas");
  }
  if (typeof getSnapshot !== "function" || typeof onState !== "function") {
    throw new TypeError("createChartInspector requires getSnapshot and onState functions");
  }

  let activeIndex = null;
  let destroyed = false;
  const restoreTabIndex = makeKeyboardReachable(canvas);

  const emitIdle = () => {
    if (destroyed) return;
    activeIndex = null;
    onState({ phase: "idle" });
  };

  const emitActive = (inspection) => {
    if (destroyed || !inspection) return;
    activeIndex = inspection.index;
    onState({ phase: "active", ...inspection });
  };

  const onPointerMove = (event) => {
    const snapshot = getSnapshot();
    const point = pointerPoint(canvas, snapshot, event);
    if (!point) {
      emitIdle();
      return;
    }
    const inspection = chartInspectionAt(snapshot, point.x);
    if (inspection) emitActive(inspection);
    else emitIdle();
  };

  const onPointerLeave = (event) => {
    if (event && event.pointerType === "touch") return;
    emitIdle();
  };
  const onPointerCancel = () => emitIdle();
  const onBlur = () => emitIdle();

  const onKeyDown = (event) => {
    if (!INSPECTION_KEYS.has(event && event.key)) return;
    const snapshot = getSnapshot();
    const view = inspectionView(snapshot);
    if (!view) {
      emitIdle();
      return;
    }
    if (typeof event.preventDefault === "function") event.preventDefault();
    if (event.key === "Escape") {
      emitIdle();
      return;
    }

    const lastIndex = view.rows.length - 1;
    const currentIndex = activeIndex === null
      ? null
      : Math.min(lastIndex, Math.max(0, activeIndex));
    let nextIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = lastIndex;
    else if (event.key === "ArrowLeft") {
      nextIndex = currentIndex === null ? lastIndex : Math.max(0, currentIndex - 1);
    } else {
      nextIndex = currentIndex === null ? 0 : Math.min(lastIndex, currentIndex + 1);
    }
    const inspection = inspectionAtIndex(snapshot, view, nextIndex);
    if (inspection) emitActive(inspection);
    else emitIdle();
  };

  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerdown", onPointerMove);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("pointercancel", onPointerCancel);
  canvas.addEventListener("keydown", onKeyDown);
  canvas.addEventListener("blur", onBlur);

  return Object.freeze({
    destroy() {
      if (destroyed) return;
      destroyed = true;
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerdown", onPointerMove);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("pointercancel", onPointerCancel);
      canvas.removeEventListener("keydown", onKeyDown);
      canvas.removeEventListener("blur", onBlur);
      restoreTabIndex();
    },
  });
}

function inspectionAtIndex(snapshot, view, index) {
  const x = view.bounds.left + view.xStep * (index + 0.5);
  return chartInspectionAt(snapshot, x);
}

function pointerPoint(canvas, snapshot, event) {
  const view = inspectionView(snapshot);
  if (!view) return null;
  const rect = canvas.getBoundingClientRect();
  const rectLeft = finiteNumber(rect && rect.left);
  const rectTop = finiteNumber(rect && rect.top);
  const rectWidth = positiveNumber(rect && rect.width);
  const rectHeight = positiveNumber(rect && rect.height);
  const clientX = finiteNumber(event && event.clientX);
  const clientY = finiteNumber(event && event.clientY);
  if (
    rectLeft === null
    || rectTop === null
    || !rectWidth
    || !rectHeight
    || clientX === null
    || clientY === null
  ) return null;

  const x = (clientX - rectLeft) * view.width / rectWidth;
  const y = (clientY - rectTop) * view.height / rectHeight;
  if (
    x < view.bounds.left
    || x > view.bounds.right
    || y < view.bounds.top
    || y > view.bounds.bottom
  ) return null;
  return { x, y };
}

function inspectionView(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const dimensions = inspectionDimensions(snapshot);
  const series = inspectionSeries(snapshot);
  if (!dimensions || !series) return null;
  const bounds = inspectionBounds(snapshot.bounds, dimensions.width, dimensions.height);
  if (!bounds) return null;
  return { ...dimensions, bounds, ...series };
}

function inspectionDimensions(snapshot) {
  const width = positiveNumber(snapshot.width);
  const height = positiveNumber(snapshot.height);
  const xStep = positiveNumber(snapshot.xStep);
  const minPrice = finiteNumber(snapshot.minPrice);
  const maxPrice = finiteNumber(snapshot.maxPrice);
  if (!width || !height || !xStep || minPrice === null || maxPrice === null || maxPrice < minPrice) return null;
  return { width, height, xStep, minPrice, maxPrice };
}

function inspectionSeries(snapshot) {
  const { rows, ma5, ma20 } = snapshot;
  if (!Array.isArray(rows) || !rows.length || !Array.isArray(ma5) || !Array.isArray(ma20)) return null;
  if (ma5.length !== rows.length || ma20.length !== rows.length) return null;
  return { rows, ma5, ma20 };
}

function inspectionBounds(bounds, width, height) {
  if (!bounds || typeof bounds !== "object") return null;
  const left = finiteNumber(bounds.left);
  const right = finiteNumber(bounds.right);
  const top = finiteNumber(bounds.top);
  const bottom = finiteNumber(bounds.bottom);
  if (
    left === null
    || right === null
    || top === null
    || bottom === null
    || left < 0
    || top < 0
    || right <= left
    || bottom <= top
    || right > width
    || bottom > height
  ) return null;
  return { left, right, top, bottom };
}

function priceY(view, price) {
  const range = Math.max(0.01, view.maxPrice - view.minPrice);
  const ratio = (view.maxPrice - price) / range;
  return Math.min(view.bounds.bottom, Math.max(view.bounds.top, view.bounds.top + ratio * (view.bounds.bottom - view.bounds.top)));
}

function boundedText(value, maxLength) {
  if (value === null || value === undefined) return "";
  return String(value).trim().slice(0, maxLength);
}

function makeKeyboardReachable(canvas) {
  if (
    typeof canvas.hasAttribute !== "function"
    || typeof canvas.setAttribute !== "function"
    || typeof canvas.removeAttribute !== "function"
    || canvas.hasAttribute("tabindex")
  ) return () => {};
  canvas.setAttribute("tabindex", "0");
  return () => canvas.removeAttribute("tabindex");
}

function eventTime(row) {
  return String(row.eventTime || row.timestamp || row.date || "").trim();
}

function alignedValue(values, index) {
  const value = values[index];
  return value === null || value === undefined ? null : finiteNumber(value);
}

function positiveNumber(value) {
  const number = finiteNumber(value);
  return number !== null && number > 0 ? number : null;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
