const shell = document.getElementById("marketScanScreeningWorkbench");
const table = document.getElementById("marketScanTable");
const tableWrap = document.getElementById("marketScanTableWrap");
const columnInputs = Array.from(document.querySelectorAll('input[name="marketScanColumnView"]'));
let controllerPromise = null;

columnInputs.forEach((input) => input.addEventListener("change", () => applyColumnView(input.value)));
shell?.addEventListener("toggle", () => { if (shell.open) void openWorkbench(); });
if (shell?.open) void openWorkbench();

function openWorkbench() {
  if (!controllerPromise) {
    controllerPromise = import("./market-scan-screening-controller.js")
      .then(({ createMarketScanScreeningController }) => createMarketScanScreeningController());
  }
  return controllerPromise.then((controller) => controller.open());
}

function applyColumnView(value) {
  if (!table || !tableWrap) return;
  const supported = new Set(["overview", "trend", "liquidity", "risk", "research"]);
  const selected = supported.has(value) ? value : "overview";
  table.dataset.columnView = selected;
  tableWrap.setAttribute("aria-label", `全市场扫描榜单，${columnViewLabel(selected)}列视图`);
  columnInputs.forEach((input) => { input.checked = input.value === selected; });
}

function columnViewLabel(value) {
  return ({ overview: "概览", trend: "趋势", liquidity: "流动性", risk: "风险", research: "研究" })[value] || "概览";
}
