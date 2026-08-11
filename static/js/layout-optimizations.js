const MOBILE_QUERY = "(max-width: 820px)";

export function marketScanPageSize(elements) {
  const view = elements?.rows?.ownerDocument?.defaultView || globalThis;
  return view?.matchMedia?.(MOBILE_QUERY)?.matches ? 30 : 100;
}

export function observeMarketScanPageSize(elements, onChange) {
  const view = elements?.rows?.ownerDocument?.defaultView || globalThis;
  const media = view?.matchMedia?.(MOBILE_QUERY);
  if (!media || typeof onChange !== "function") return () => {};
  let pageSize = media.matches ? 30 : 100;
  const handleChange = () => {
    const nextPageSize = media.matches ? 30 : 100;
    if (nextPageSize === pageSize) return;
    pageSize = nextPageSize;
    onChange(nextPageSize);
  };
  if (typeof media.addEventListener === "function") media.addEventListener("change", handleChange);
  else media.addListener?.(handleChange);
  return () => {
    if (typeof media.removeEventListener === "function") media.removeEventListener("change", handleChange);
    else media.removeListener?.(handleChange);
  };
}

export function initializeLayoutOptimizations(root = globalThis.document, view = globalThis) {
  if (!root?.documentElement || root.documentElement.dataset.layoutOptimizations === "ready") return false;
  root.documentElement.dataset.layoutOptimizations = "ready";
  const mobile = view.matchMedia?.(MOBILE_QUERY);
  wireMarketScanAuxiliaryOrder(root, mobile);
  wireDisclosure(root, mobile, "marketScanHistoryToggle", "marketScanHistory", false, false);
  wireDisclosure(root, mobile, "marketScanFilterToggle", "marketScanFilterPanel", false, false);
  wireDisclosure(root, mobile, "marketScanDetailsToggle", "marketScanDetails", false, false);
  wireDetailsDisclosure(root, "marketScanStrategyToggle", "strategyLab");
  wireDisclosure(root, mobile, "watchFormToggle", "watchForm", true);
  wireCollapsiblePanels(root, mobile);
  wireQueryPanel(root, mobile);
  wireAdviceSearch(root);
  return true;
}

function wireMarketScanAuxiliaryOrder(root, mobile) {
  const actions = root.querySelector?.(".market-scan-layout-actions");
  const details = root.getElementById?.("marketScanDetails");
  const filters = root.getElementById?.("marketScanFilterPanel");
  const parent = actions?.parentNode;
  if (!parent || !details || !filters) return;
  const detailsMarker = root.createComment("market-scan-details-position");
  const filtersMarker = root.createComment("market-scan-filters-position");
  parent.insertBefore(detailsMarker, details);
  parent.insertBefore(filtersMarker, filters);
  const sync = () => {
    if (mobile?.matches) {
      const resultAnchor = actions.nextSibling;
      parent.insertBefore(filters, resultAnchor);
      parent.insertBefore(details, resultAnchor);
      return;
    }
    parent.insertBefore(details, detailsMarker.nextSibling);
    parent.insertBefore(filters, filtersMarker.nextSibling);
  };
  mobile?.addEventListener?.("change", sync);
  sync();
}

function wireDisclosure(root, mobile, buttonId, panelId, closedOnMobile, defaultExpanded = true) {
  const button = root.getElementById?.(buttonId);
  const panel = root.getElementById?.(panelId);
  if (!button || !panel) return;
  let touched = false;
  const render = (expanded) => {
    panel.hidden = !expanded;
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = button.dataset[expanded ? "openLabel" : "closedLabel"] || button.textContent;
  };
  const sync = () => {
    if (touched) return;
    render(defaultExpanded && !(mobile?.matches && closedOnMobile));
  };
  button.addEventListener("click", () => {
    touched = true;
    render(button.getAttribute("aria-expanded") !== "true");
  });
  mobile?.addEventListener?.("change", sync);
  sync();
}

function wireDetailsDisclosure(root, buttonId, panelId) {
  const button = root.getElementById?.(buttonId);
  const panel = root.getElementById?.(panelId);
  if (!button || !panel) return;
  const render = (expanded) => {
    panel.hidden = !expanded;
    panel.open = expanded;
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = button.dataset[expanded ? "openLabel" : "closedLabel"] || button.textContent;
  };
  button.addEventListener("click", () => render(button.getAttribute("aria-expanded") !== "true"));
  panel.addEventListener("toggle", () => {
    if (!panel.hidden && !panel.open) render(false);
  });
  render(false);
}

function wireCollapsiblePanels(root, mobile) {
  root.querySelectorAll?.("[data-layout-collapsible]").forEach((panel, index) => {
    const title = panel.querySelector?.(":scope > .panel-title");
    if (!title) return;
    if (!panel.id) panel.id = `layoutCollapsible${index + 1}`;
    const button = root.createElement("button");
    button.type = "button";
    button.className = "mini-button layout-toggle layout-collapse-toggle";
    button.setAttribute("aria-controls", panel.id);
    let touched = false;
    const render = (expanded) => {
      panel.classList.toggle("layout-panel-collapsed", !expanded);
      button.setAttribute("aria-expanded", String(expanded));
      button.textContent = expanded ? "收起" : "展开";
    };
    const sync = () => render(!(mobile?.matches && panel.dataset.layoutDefaultMobile === "closed" && !touched));
    button.addEventListener("click", () => {
      touched = true;
      render(button.getAttribute("aria-expanded") !== "true");
    });
    title.append(button);
    mobile?.addEventListener?.("change", sync);
    sync();
  });
}

function wireQueryPanel(root, mobile) {
  const body = root.body;
  const button = root.getElementById?.("queryPanelToggle");
  if (!body || !button) return;
  const userState = new Map();
  const currentView = () => body.dataset.primaryView || "research";
  const defaultCollapsed = () => Boolean(mobile?.matches && currentView() === "review");
  const render = (collapsed) => {
    body.classList.toggle("query-panel-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    button.textContent = collapsed ? "展开查询" : "收起查询";
  };
  const sync = () => render(userState.has(currentView()) ? userState.get(currentView()) : defaultCollapsed());
  button.addEventListener("click", () => {
    const collapsed = !body.classList.contains("query-panel-collapsed");
    userState.set(currentView(), collapsed);
    render(collapsed);
  });
  new MutationObserver(sync).observe(body, { attributes: true, attributeFilter: ["data-primary-view"] });
  mobile?.addEventListener?.("change", sync);
  sync();
}

function wireAdviceSearch(root) {
  const input = root.getElementById?.("reviewAdviceSearch");
  const select = root.getElementById?.("reviewAdviceId");
  const feedback = root.getElementById?.("reviewAdviceSearchFeedback");
  if (!input || !select) return;
  const filter = () => {
    const query = input.value.trim().toLocaleLowerCase("zh-CN");
    let visible = 0;
    Array.from(select.options).forEach((option, index) => {
      const matches = index === 0 || !query || option.textContent.toLocaleLowerCase("zh-CN").includes(query);
      option.hidden = !matches;
      if (index > 0 && matches) visible += 1;
    });
    if (select.selectedOptions?.[0]?.hidden) select.selectedIndex = 0;
    if (feedback) feedback.textContent = query ? `找到 ${visible} 条匹配快照` : "可先缩小快照范围，再从下方选择";
  };
  input.addEventListener("input", filter);
  new MutationObserver(filter).observe(select, { childList: true, subtree: true });
  filter();
}

if (typeof document !== "undefined" && typeof MutationObserver === "function") initializeLayoutOptimizations();
