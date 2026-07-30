import { DEFAULT_WORKSPACE_PREFERENCES, PRIMARY_VIEW_OPTIONS } from "./workspace-preferences.js";

const BUTTON_SELECTOR = ".primary-navigation button[data-primary-view]";
const REGION_SELECTOR = "[data-primary-regions]";

export function createPrimaryNavigation({ root = document, onSelect = () => {} } = {}) {
  const navigation = root.getElementById?.("primaryNavigation") || null;
  const buttons = Array.from(root.querySelectorAll?.(BUTTON_SELECTOR) || []);
  const regions = Array.from(root.querySelectorAll?.(REGION_SELECTOR) || []);

  function render(view) {
    const target = normalizePrimaryView(view);
    if (root.body?.dataset) root.body.dataset.primaryView = target;
    buttons.forEach((button) => {
      const active = button.dataset.primaryView === target;
      button.classList?.toggle("active", active);
      button.setAttribute?.("aria-current", active ? "page" : "false");
    });
    regions.forEach((region) => {
      region.hidden = !region.dataset.primaryRegions?.split(/\s+/).includes(target);
    });
    return target;
  }

  function handleClick(event) {
    const button = event.target?.closest?.("button[data-primary-view]");
    if (!button || !navigation?.contains?.(button)) return;
    onSelect(button.dataset.primaryView);
  }

  navigation?.addEventListener?.("click", handleClick);

  return {
    render,
    destroy() {
      navigation?.removeEventListener?.("click", handleClick);
    },
  };
}

export function normalizePrimaryView(view) {
  return PRIMARY_VIEW_OPTIONS.includes(view) ? view : DEFAULT_WORKSPACE_PREFERENCES.primaryView;
}
